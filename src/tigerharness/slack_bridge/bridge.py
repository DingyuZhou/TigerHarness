"""Slack Socket-Mode bridge to the `claude_p` backend.

Two modes:

* **Single-persona** (legacy). One bridge serves one persona. Constructor
  takes a ``BridgeConfig`` + ``AgentConfig`` (existing call shape). No
  routing call, no reply prefix -- output looks identical to before.

* **Multi-persona** (PR4). One bridge serves N personas within a single
  team's Slack app. The first message of each new thread is routed
  through a one-shot ``router.detect_persona`` call to pick the addressed
  persona; subsequent messages in that thread reuse the choice. Each
  reply is prefixed with ``[<persona>]:`` for clarity. The team-awareness
  preamble injected into each persona's prompt tells them how to handle
  misroutes politely.

Flow per inbound message:
    1. Drop anything that isn't a real user message from the allowlist.
    2. Resolve the Slack thread key.
    3. Resolve the active persona (existing thread -> stored persona;
       new thread -> router.detect_persona).
    4. If the message has file attachments, download each via the bot
       token and stage them.
    5. Look up / create an agent-sdk ``Session`` for that thread (sessions
       are persona-specific because the AgentConfig carries the prompt).
    6. Dispatch via ``run_with_retry(backend, persona.agent_config, ...)``.
    7. Persist ``(session_id, persona)`` to ``ThreadStore``.
    8. Post ``result.final_output`` back into the thread, prefixed with
       ``[<persona>]:`` in multi-persona mode.

Serialisation: a per-thread ``asyncio.Lock`` ensures we don't dispatch
two turns into the same ``claude -p`` session at once.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from collections.abc import Awaitable, Callable

from tigerharness.agent_sdk import AgentConfig, get_backend, run_with_retry
from tigerharness.agent_sdk.types import AgentBackend, Session
from slack_bolt.async_app import AsyncApp

from .config import BridgeConfig
from .downloader import (
    Attachment,
    FileDownloader,
    SlackFileDownloader,
    augment_prompt,
)
from .persistence import ThreadStore, default_state_path
from .router import detect_persona


log = logging.getLogger("tigerharness.slack_bridge.bridge")


# Bash patterns blocked from the agent.
_SUDO_DENY = ["Bash(sudo:*)", "Bash(sudo)"]


# Internal name used for the single persona in a legacy single-tenant
# bridge. Multi-persona configs name their personas explicitly; users
# never see this string because the reply prefix is skipped for
# one-persona teams.
_SINGLE_PERSONA_NAME = "default"


# ---------------------------------------------------------------------------
# Team / persona data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PersonaSlot:
    """One persona within a team's bridge.

    ``agent_config.instructions`` is the persona's system prompt with
    the team-awareness preamble already appended (built once at lane
    load time -- see ``_build_persona_agent_config``).
    """
    name: str
    agent_config: AgentConfig
    tiger_memory_config_path: str = ""


@dataclass(frozen=True)
class TeamBridgeContext:
    """One team's bridge wiring.

    Multi-persona teams have N entries in ``personas``; single-persona
    (legacy single-tenant) has exactly one with name ``"default"``.
    The bridge skips the router and the reply prefix when N == 1.
    """
    team_name: str
    slack_app_token: str
    slack_bot_token: str
    allowed_user_ids: frozenset[str]
    agent_cwd: str
    personas: dict[str, PersonaSlot]   # name -> slot (canonical case)
    default_persona: str               # must be in personas
    tiger_memory_cli: str = ""
    persona_aliases: dict[str, list[str]] | None = None  # name -> aliases
    # "rebuild" (legacy claude -p, default) | "off" (in-session sweep
    # protocol owns it). See config.normalize_tiger_memory_trigger.
    tiger_memory_trigger: str = "rebuild"

    @property
    def is_multi_persona(self) -> bool:
        return len(self.personas) > 1


# ---------------------------------------------------------------------------
# Misc constants + helpers
# ---------------------------------------------------------------------------

_ACCEPTED_SUBTYPES = {"file_share"}


@dataclass
class _ThreadState:
    """One thread's in-memory state. ``persona`` is the active persona
    for this thread; never changes once set."""
    session: Session
    persona: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SlackBridge:
    """Wires a `slack_bolt.AsyncApp` to an `AgentBackend`.

    Construct via the legacy 4-arg form (BridgeConfig + AgentConfig +
    ThreadStore -- single-persona) or via the ``team_ctx=`` kwarg
    (multi-persona). The single-persona case is converted to a 1-entry
    ``TeamBridgeContext`` internally so there's one dispatch code path.
    """

    def __init__(
        self,
        cfg: BridgeConfig | None = None,
        backend: AgentBackend | None = None,
        agent_cfg: AgentConfig | None = None,
        store: ThreadStore | None = None,
        downloader: FileDownloader | None = None,
        *,
        team_ctx: TeamBridgeContext | None = None,
    ) -> None:
        if team_ctx is not None:
            self._team = team_ctx
        else:
            if cfg is None or agent_cfg is None:
                raise ValueError(
                    "SlackBridge: must provide (cfg + agent_cfg) for single-"
                    "persona mode, or team_ctx= for multi-persona mode"
                )
            self._team = _single_persona_team_context(cfg, agent_cfg)

        if backend is None or store is None:
            raise ValueError("SlackBridge: backend and store are required")

        self._backend = backend
        self._store = store
        self._downloader: FileDownloader = downloader or SlackFileDownloader(
            self._team.slack_bot_token
        )
        self._threads: dict[str, _ThreadState] = {}
        self._threads_guard = asyncio.Lock()

        self._shutting_down = asyncio.Event()
        self._in_flight = 0
        self._drained = asyncio.Event()
        self._drained.set()

        # Running total of LLM USD spent through this bridge. Includes
        # both router calls (one per new thread) and agent calls (one
        # per dispatch). Logged on shutdown.
        self.cost_so_far: float = 0.0

        self.app = AsyncApp(token=self._team.slack_bot_token)
        self._register_handlers()

    def _record_cost(self, cost_usd: object) -> None:
        """Accumulate LLM spend.

        Accepts ``object`` (not ``float | None``) on purpose: backends
        from arbitrary vendors may report cost in unexpected shapes
        (``None``, missing field, ``Decimal``, even a string). We
        prefer "log and drop" over crashing dispatch, since the only
        consequence of a missed update is a slightly-low spend total.
        """
        if cost_usd is None:
            return
        try:
            self.cost_so_far += float(cost_usd)
        except (TypeError, ValueError):
            # Logged so a "why is my cost reading low" investigation
            # has a thread to pull. Debug level: not a user-facing
            # error, just an unusual backend payload.
            log.debug(
                "ignoring cost_usd of unexpected type %s: %r",
                type(cost_usd).__name__, cost_usd,
            )

    # ----- shutdown -----

    def request_shutdown(self) -> None:
        """Signal the bridge to stop accepting new dispatches."""
        if not self._shutting_down.is_set():
            log.info(
                "shutdown requested -- draining %d in-flight dispatch(es), "
                "total LLM spend $%.4f",
                self._in_flight, self.cost_so_far,
            )
            self._shutting_down.set()

    async def wait_for_drain(self, timeout: float = 120.0) -> bool:
        """Wait up to *timeout* seconds for in-flight dispatches to finish."""
        try:
            await asyncio.wait_for(self._drained.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            log.warning(
                "drain timed out after %.0fs with %d dispatch(es) still in-flight",
                timeout, self._in_flight,
            )
            return False

    # ----- public for tests -----

    async def handle_message(self, event: dict[str, Any], say: Callable[..., Awaitable[Any]]) -> None:
        """Route a single inbound Slack message (DM or tracked-thread reply)."""
        if not _is_user_dm(event) and not self._is_tracked_thread_reply(event):
            return
        if event.get("user") not in self._team.allowed_user_ids:
            log.info("dropping message from non-allowlisted user %s", event.get("user"))
            return

        text = (event.get("text") or "").strip()
        await self._dispatch(event, text, say)

    async def handle_mention(self, event: dict[str, Any], say: Callable[..., Awaitable[Any]]) -> None:
        """Route a single inbound @mention in a channel."""
        if event.get("user") not in self._team.allowed_user_ids:
            log.info("dropping mention from non-allowlisted user %s", event.get("user"))
            return

        text = _strip_bot_mention(event.get("text") or "").strip()
        await self._dispatch(event, text, say)

    async def _dispatch(
        self,
        event: dict[str, Any],
        text: str,
        say: Callable[..., Awaitable[Any]],
    ) -> None:
        """Shared dispatch logic for DMs and channel mentions."""
        if self._shutting_down.is_set():
            log.info("rejecting dispatch -- bridge is shutting down")
            return

        files = event.get("files") or []
        if not text and not files:
            return

        thread_key = event.get("thread_ts") or event["ts"]

        attachments: list[Attachment] = []
        for f in files:
            a = await self._downloader.download(f, thread_key)
            if a is not None:
                attachments.append(a)

        if not text and not attachments:
            log.warning(
                "thread=%s all attachments failed and no caption; sending warning",
                thread_key,
            )
            await say(
                text=":warning: I saw your attachment but couldn't download it. "
                "File-fetch failed -- check the bridge logs.",
                thread_ts=thread_key,
            )
            return

        prompt = augment_prompt(text, attachments)
        prompt = _append_bridge_context(prompt, thread_key, event.get("channel"))

        # Reserve a drain slot for the entire dispatch lifecycle -- the
        # router LLM call, the session open, AND the agent run. Earlier
        # versions only counted the agent run, so wait_for_drain could
        # return True with router/init work still in flight, leaving
        # orphan claude subprocesses on SIGTERM.
        self._in_flight += 1
        self._drained.clear()
        try:
            state = await self._get_or_open_thread(thread_key, text)

            # The router LLM call took ~500ms-1s. Shutdown may have been
            # requested while we were waiting for the routing decision.
            # Bail out before opening claude sessions / running agents
            # we won't be able to drain in time.
            if self._shutting_down.is_set():
                log.info(
                    "thread=%s aborting dispatch -- shutdown caught after init",
                    thread_key,
                )
                return

            persona = self._team.personas[state.persona]

            # Two voices come out of this block:
            #   - persona_body: words from the persona (prefixed in multi mode)
            #   - bridge_body:  bridge-generated message (errors, empty reply --
            #                   never prefixed; "[Ayako]: backend error" would
            #                   wrongly imply Ayako is reporting the error)
            persona_body: str | None = None
            bridge_body: str | None = None
            async with state.lock:
                resume_id = state.session.id or "<new>"
                log.info(
                    "thread=%s persona=%s dispatch (resume=%s, chars=%d, files=%d)",
                    thread_key, state.persona, resume_id, len(prompt), len(attachments),
                )
                try:
                    result = await run_with_retry(
                        self._backend,
                        _with_thread_env(persona.agent_config, thread_key),
                        prompt,
                        session=state.session,
                        max_attempts=3,
                        label=f"thread={thread_key} persona={state.persona}",
                    )
                    if result.final_output:
                        persona_body = result.final_output
                    else:
                        bridge_body = "_(empty reply)_"
                    if state.session.id:
                        self._store.set(
                            thread_key,
                            state.session.id,
                            persona=state.persona,
                        )
                    # Track LLM spend: agent call cost contributes to the
                    # bridge's running tally alongside router calls.
                    self._record_cost(getattr(result, "cost_usd", None))
                    log.info(
                        "thread=%s persona=%s ok (session=%s, cost_usd=%s)",
                        thread_key, state.persona, state.session.id, result.cost_usd,
                    )
                except Exception as exc:
                    log.exception("backend failure for thread %s", thread_key)
                    bridge_body = f":warning: backend error: `{exc}`"

            if persona_body is not None:
                reply_text = _format_reply(persona_body, state.persona, self._team)
            else:
                # Bridge voice -- no persona prefix.
                reply_text = bridge_body or ""
            await say(text=reply_text, thread_ts=thread_key)
        finally:
            self._in_flight -= 1
            if self._in_flight == 0:  # pragma: no branch  # tested with single-request flows
                self._drained.set()

    def _is_tracked_thread_reply(self, event: dict[str, Any]) -> bool:
        """True iff this is a non-bot reply in a thread we're engaged in."""
        thread_ts = event.get("thread_ts")
        if not thread_ts:
            return False
        if event.get("bot_id"):
            return False
        subtype = event.get("subtype")
        if subtype and subtype not in _ACCEPTED_SUBTYPES:
            return False
        if thread_ts in self._threads:
            return True
        return self._store.get(thread_ts) is not None

    # ----- internals -----

    def _register_handlers(self) -> None:
        @self.app.event("message")
        async def _on_message(event: dict[str, Any], say: Any) -> None:  # noqa: ARG001  # pragma: no cover — bolt callback
            await self.handle_message(event, say)

        @self.app.event("app_mention")
        async def _on_mention(event: dict[str, Any], say: Any) -> None:  # noqa: ARG001  # pragma: no cover — bolt callback
            await self.handle_mention(event, say)

    async def _get_or_open_thread(self, key: str, first_text: str) -> _ThreadState:
        """Look up the thread's session + persona, or open a new one.

        On the first message of a new thread, runs the router to pick
        the active persona. On subsequent messages, reuses whatever was
        stored in ``ThreadStore``.

        Concurrency note: the router LLM call (~500ms-1s) and
        ``backend.open_session`` (also async) run **outside** the
        ``_threads_guard`` lock so multiple new threads on the same
        bridge can initialize in parallel. The lock only wraps the
        dict claim itself, with a double-check on entry. If two
        coroutines race for the same key, the loser closes its session
        and uses the winner's state.
        """
        # Fast path: thread already in memory. Short critical section.
        async with self._threads_guard:
            existing = self._threads.get(key)
            if existing is not None:
                return existing

        # Slow path: figure out persona + open a session WITHOUT holding
        # the guard. Multiple new threads can do this work concurrently.
        record = self._store.get_record(key)
        if record is not None:
            # Existing thread, resumed from disk.
            resume_id: str | None = record.session_id
            if record.persona and record.persona in self._team.personas:
                persona_name = record.persona
            else:
                # Pre-routing record (persona=None) OR a persona since
                # removed from the roster -- fall back to default.
                persona_name = self._team.default_persona
            log.info(
                "thread=%s resuming claude session %s (persona=%s)",
                key, resume_id, persona_name,
            )
        else:
            # Brand-new thread: route the first message to a persona.
            resume_id = None
            persona_name, cost = await detect_persona(
                self._backend,
                first_text,
                list(self._team.personas.keys()),
                self._team.default_persona,
                aliases=self._team.persona_aliases,
            )
            self._record_cost(cost)
            log.info(
                "thread=%s opening new claude session (persona=%s)",
                key, persona_name,
            )

        session = await self._backend.open_session(resume_id=resume_id)

        # Claim the slot under lock. Double-check in case another
        # coroutine raced ahead and inserted while we were in the
        # slow path -- if so, drop our session and return theirs.
        # Critical: session.close() can take 10s of ms on claude_p
        # (subprocess teardown), so we capture the loser session
        # under lock but await its close OUTSIDE the lock -- otherwise
        # we'd block other claimants on cleanup work.
        loser_session: Session | None = None
        async with self._threads_guard:
            existing = self._threads.get(key)
            if existing is not None:
                log.info(
                    "thread=%s lost init race; discarding spare session",
                    key,
                )
                loser_session = session
                state = existing
            else:
                state = _ThreadState(session=session, persona=persona_name)
                self._threads[key] = state
                # "off" -> the in-session sweep protocol owns the rebuild;
                # the daemon fires no legacy claude -p. Default "rebuild"
                # keeps the legacy trigger so existing deploys are unchanged.
                if self._team.tiger_memory_trigger == "rebuild":
                    _trigger_tiger_memory_rebuild(
                        self._team.personas[persona_name],
                        self._team.tiger_memory_cli,
                        key,
                    )

        if loser_session is not None:  # pragma: no cover — concurrency race
            try:
                await loser_session.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                log.warning(
                    "thread=%s race-loser session.close raised",
                    key, exc_info=True,
                )
        return state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _trigger_tiger_memory_rebuild(
    persona: PersonaSlot, tiger_memory_cli: str, thread_key: str
) -> None:
    """Fire `tiger-memory rebuild --background` for the active persona's
    memory config, if configured. Detached child; never blocks dispatch."""
    if not persona.tiger_memory_config_path:
        return
    cli = tiger_memory_cli or shutil.which("tiger-memory")
    if not cli:
        log.warning(
            "thread=%s TIGER_MEMORY_CONFIG set but `tiger-memory` CLI not "
            "found; skipping rebuild trigger.",
            thread_key,
        )
        return
    try:
        subprocess.Popen(
            [cli, "--config", persona.tiger_memory_config_path,
             "rebuild", "--background"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log.info("thread=%s fired tiger-memory rebuild", thread_key)
    except Exception:  # noqa: BLE001
        log.warning(
            "thread=%s tiger-memory rebuild trigger failed (ignored)",
            thread_key, exc_info=True,
        )


_BOT_MENTION_RE = re.compile(r"<@[A-Z0-9]+>\s*")


def _strip_bot_mention(text: str) -> str:
    """Strip all ``<@U...>`` / ``<@W...>`` Slack mention tokens.

    Matches user IDs (``U`` prefix), workspace IDs (``W`` prefix), and
    bot IDs (``B`` prefix). Any trailing whitespace immediately after
    the closing ``>`` is also consumed so the result doesn't start
    with a space when a leading mention is followed by " hello".
    """
    return _BOT_MENTION_RE.sub("", text)


def _append_bridge_context(prompt: str, thread_ts: str, channel: str | None) -> str:
    """Append a small metadata block so the agent knows which thread
    it's in (used by the slack-notify skill for `--thread` routing)."""
    lines = [f"slack_thread_ts: {thread_ts}"]
    if channel:
        lines.append(f"slack_channel: {channel}")
    return f"{prompt}\n\n[bridge-context]\n" + "\n".join(lines)


def _with_thread_env(config: AgentConfig, thread_ts: str) -> AgentConfig:
    """Return a per-turn copy of *config* carrying this thread's Slack
    ``thread_ts`` in ``extra["env"]`` so the claude_p backend injects it
    into the turn's subprocess as ``TIGERHARNESS_SLACK_THREAD_TS``.

    This is the harness-enforced half of drive-transcript suppression:
    an in-session ``journal claim --driver <p>`` reads that env var as a
    fallback for ``--drive-thread`` (see ``journal.cli.cmd_claim`` and
    ``docs/per-persona-journal-memory.md`` section 4), so a Slack-driven
    drive's transcript is registered for suppression even if the agent
    omits the flag. Set on every turn (inert unless the turn becomes a
    drive); a copy, never a mutation, so the persona's shared config and
    concurrent turns stay independent."""
    existing_env = config.extra.get("env") or {}
    return replace(
        config,
        extra={
            **config.extra,
            "env": {**existing_env, "TIGERHARNESS_SLACK_THREAD_TS": thread_ts},
        },
    )


def _is_user_dm(event: dict[str, Any]) -> bool:
    """True iff this is a real user DM (not bot, not subtype noise)."""
    if event.get("channel_type") != "im":
        return False
    if event.get("bot_id"):
        return False
    subtype = event.get("subtype")
    if subtype and subtype not in _ACCEPTED_SUBTYPES:
        return False
    return True


def _format_reply(text: str, persona_name: str, team: TeamBridgeContext) -> str:
    """Prefix the reply with ``[<persona>]:`` in multi-persona teams.

    Single-persona teams get the bare text so the user-visible output
    stays identical to the pre-PR4 bridge. Empty agent output still
    gets the prefix in multi-persona mode so the user can see *which*
    persona returned nothing.
    """
    if not team.is_multi_persona:
        return text
    return f"[{persona_name}]: {text}"


# ---------------------------------------------------------------------------
# Agent-config builders
# ---------------------------------------------------------------------------

def build_agent_config(cfg: BridgeConfig) -> AgentConfig:
    """Build a single-persona AgentConfig from a (legacy) BridgeConfig.

    Used by the single-tenant `_run_single` entrypoint. Multi-persona
    callers use `build_persona_agent_config` instead so each persona
    gets the team-awareness preamble.
    """
    instructions = ""
    if cfg.agent_prompt_path:
        prompt_path = Path(cfg.agent_prompt_path).expanduser()
        if prompt_path.exists():
            instructions = prompt_path.read_text()
        else:
            raise FileNotFoundError(
                f"Agent prompt not found at {prompt_path}. "
                "Set TIGERHARNESS_AGENT_PROMPT to a valid path."
            )
    else:
        # Loud at startup so operators notice they're running a generic
        # assistant. Easy to miss otherwise -- the bridge stays "up" but
        # replies have lost the persona.
        log.warning(
            "TIGERHARNESS_AGENT_PROMPT is unset; falling back to a generic "
            "'You are a helpful assistant.' prompt. Set it to a path "
            "(e.g. personas/sai.md) to give the agent its real persona."
        )
        instructions = "You are a helpful assistant."

    return AgentConfig(
        name="agent-slack",
        instructions=instructions,
        extra={
            "permission_mode": "bypassPermissions",
            "disallowed_tools": list(_SUDO_DENY),
        },
    )


def _team_awareness_preamble(
    persona_name: str, team_name: str, all_personas: list[str]
) -> str:
    """Routing-aware preamble appended to every persona's prompt in a
    multi-persona team. Tells the persona how to handle misroutes
    politely.

    Single-persona teams skip this preamble entirely -- there's nobody
    to redirect to.
    """
    others = [n for n in all_personas if n != persona_name]
    if not others:
        return ""
    others_str = ", ".join(others)
    team_descriptor = f"team {team_name}" if team_name else "your team"
    return (
        "\n\n---\n\n"
        f"You are {persona_name}, a member of {team_descriptor}. "
        f"Other team members reachable via separate Slack threads: {others_str}.\n"
        f"\n"
        f"If a user's message in this thread is clearly addressed to a different "
        f"team member (e.g. \"Hi {others[0]}\" when you are {persona_name}), "
        f"politely identify yourself, suggest the user start a new DM thread to "
        f"reach the intended team member, and optionally help with anything within "
        f"your own scope. Don't attempt to act as another team member.\n"
        f"\n"
        f"You don't need to prefix your replies with your own name -- the Slack "
        f"bridge automatically labels every reply with `[{persona_name}]:` so the "
        f"user knows who answered."
    )


def build_persona_agent_config(
    persona_name: str,
    prompt_text: str,
    team_name: str,
    all_personas: list[str],
) -> AgentConfig:
    """Compose a persona's AgentConfig: their prompt + the team-awareness
    preamble appended for multi-persona teams."""
    preamble = _team_awareness_preamble(persona_name, team_name, all_personas)
    return AgentConfig(
        name=f"agent-{persona_name}",
        instructions=prompt_text + preamble,
        extra={
            "permission_mode": "bypassPermissions",
            "disallowed_tools": list(_SUDO_DENY),
        },
    )


def _single_persona_team_context(
    cfg: BridgeConfig, agent_cfg: AgentConfig
) -> TeamBridgeContext:
    """Wrap a (legacy) single-tenant BridgeConfig + AgentConfig as a
    1-persona team context, so the rest of SlackBridge has one code path.

    The single persona is named ``"default"`` and skips the routing call
    + reply prefix at runtime (see ``is_multi_persona``).
    """
    slot = PersonaSlot(
        name=_SINGLE_PERSONA_NAME,
        agent_config=agent_cfg,
        tiger_memory_config_path=cfg.tiger_memory_config_path,
    )
    return TeamBridgeContext(
        team_name="",
        slack_app_token=cfg.slack_app_token,
        slack_bot_token=cfg.slack_bot_token,
        allowed_user_ids=cfg.allowed_user_ids,
        agent_cwd=cfg.agent_cwd,
        personas={_SINGLE_PERSONA_NAME: slot},
        default_persona=_SINGLE_PERSONA_NAME,
        tiger_memory_cli=cfg.tiger_memory_cli,
        tiger_memory_trigger=cfg.tiger_memory_trigger,
    )


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

def build_bridge(
    cfg: BridgeConfig, *, state_path: Path | None = None
) -> SlackBridge:
    """Single-persona factory (legacy + single-tenant entrypoint).

    Multi-lane callers in the multi-orchestrator use
    ``build_team_bridge`` instead. Kept stable so PR1's signature
    (``build_bridge(cfg, *, state_path=...)``) keeps working.
    """
    backend = get_backend("claude_p", cwd=cfg.agent_cwd)
    store = ThreadStore(state_path if state_path is not None else default_state_path())
    return SlackBridge(cfg, backend, build_agent_config(cfg), store)


def build_team_bridge(
    team_ctx: TeamBridgeContext, *, state_path: Path | None = None
) -> SlackBridge:
    """Multi-persona factory.

    Used by the multi-team orchestrator (`__main__._run_multi`). One
    team context per lane; each lane has its own backend (cwd-scoped),
    its own ThreadStore (state_path), and N personas with routing.
    """
    backend = get_backend("claude_p", cwd=team_ctx.agent_cwd)
    store = ThreadStore(state_path if state_path is not None else default_state_path())
    return SlackBridge(
        backend=backend, store=store, team_ctx=team_ctx,
    )

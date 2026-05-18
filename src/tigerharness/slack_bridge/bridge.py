"""Slack Socket-Mode bridge to the `claude_p` backend.

Flow per inbound message (DM or @mention in a channel):
    1. Drop anything that isn't a real user message from the allowlist.
    2. Resolve the Slack thread key (root `ts` or the existing `thread_ts`).
    3. If the message has file attachments, download each via the bot
       token and stage them.
    4. Look up / create an agent-sdk `Session` for that thread.
    5. Call `backend.run(cfg, prompt_with_file_paths, session=session)`.
    6. Post `result.final_output` back into the same thread.

Serialisation: a per-thread `asyncio.Lock` ensures we don't dispatch two
turns into the same `claude -p` session at once.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
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


log = logging.getLogger("tigerharness.slack_bridge")


# Bash patterns blocked from the agent.
_SUDO_DENY = ["Bash(sudo:*)", "Bash(sudo)"]


@dataclass
class _ThreadState:
    session: Session
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SlackBridge:
    """Wires a `slack_bolt.AsyncApp` to an `AgentBackend`.

    Held externally so tests can inject a fake backend and drive
    handlers directly.
    """

    def __init__(
        self,
        cfg: BridgeConfig,
        backend: AgentBackend,
        agent_cfg: AgentConfig,
        store: ThreadStore,
        downloader: FileDownloader | None = None,
    ) -> None:
        self._cfg = cfg
        self._backend = backend
        self._agent_cfg = agent_cfg
        self._store = store
        self._downloader: FileDownloader = downloader or SlackFileDownloader(
            cfg.slack_bot_token
        )
        self._threads: dict[str, _ThreadState] = {}
        self._threads_guard = asyncio.Lock()

        self._shutting_down = asyncio.Event()
        self._in_flight = 0
        self._drained = asyncio.Event()
        self._drained.set()

        self.app = AsyncApp(token=cfg.slack_bot_token)
        self._register_handlers()

    # ----- shutdown -----

    def request_shutdown(self) -> None:
        """Signal the bridge to stop accepting new dispatches."""
        if not self._shutting_down.is_set():
            log.info("shutdown requested -- draining %d in-flight dispatch(es)", self._in_flight)
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
        if event.get("user") not in self._cfg.allowed_user_ids:
            log.info("dropping message from non-allowlisted user %s", event.get("user"))
            return

        text = (event.get("text") or "").strip()
        await self._dispatch(event, text, say)

    async def handle_mention(self, event: dict[str, Any], say: Callable[..., Awaitable[Any]]) -> None:
        """Route a single inbound @mention in a channel."""
        if event.get("user") not in self._cfg.allowed_user_ids:
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

        state = await self._get_or_open_thread(thread_key)

        self._in_flight += 1
        self._drained.clear()
        try:
            async with state.lock:
                resume_id = state.session.id or "<new>"
                log.info(
                    "thread=%s dispatch (resume=%s, chars=%d, files=%d)",
                    thread_key, resume_id, len(prompt), len(attachments),
                )
                try:
                    result = await run_with_retry(
                        self._backend,
                        self._agent_cfg,
                        prompt,
                        session=state.session,
                        max_attempts=3,
                        label=f"thread={thread_key}",
                    )
                    reply = result.final_output or "_(empty reply)_"
                    if state.session.id:
                        self._store.set(thread_key, state.session.id)
                    log.info(
                        "thread=%s ok (session=%s, cost_usd=%s)",
                        thread_key, state.session.id, result.cost_usd,
                    )
                except Exception as exc:
                    log.exception("backend failure for thread %s", thread_key)
                    reply = f":warning: backend error: `{exc}`"

            await say(text=reply, thread_ts=thread_key)
        finally:
            self._in_flight -= 1
            if self._in_flight == 0:
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
        async def _on_message(event: dict[str, Any], say: Any) -> None:  # noqa: ARG001
            await self.handle_message(event, say)

        @self.app.event("app_mention")
        async def _on_mention(event: dict[str, Any], say: Any) -> None:  # noqa: ARG001
            await self.handle_mention(event, say)

    async def _get_or_open_thread(self, key: str) -> _ThreadState:
        async with self._threads_guard:
            state = self._threads.get(key)
            if state is None:
                resume_id = self._store.get(key)
                session = await self._backend.open_session(
                    resume_id=resume_id
                )
                if resume_id:
                    log.info(
                        "thread=%s resuming claude session %s",
                        key, resume_id,
                    )
                else:
                    log.info("thread=%s opening new claude session", key)
                state = _ThreadState(session=session)
                self._threads[key] = state
                _trigger_tiger_memory_rebuild(self._cfg, key)
        return state


def _trigger_tiger_memory_rebuild(cfg: BridgeConfig, thread_key: str) -> None:
    """Fire `tiger-memory rebuild --background` for this thread, if configured."""
    if not cfg.tiger_memory_config_path:
        return
    # Use explicit CLI path if configured, otherwise search PATH.
    cli = cfg.tiger_memory_cli or shutil.which("tiger-memory")
    if not cli:
        log.warning(
            "thread=%s TIGER_MEMORY_CONFIG set but `tiger-memory` CLI not "
            "found; skipping rebuild trigger.",
            thread_key,
        )
        return
    try:
        subprocess.Popen(
            [cli, "--config", cfg.tiger_memory_config_path,
             "rebuild", "--background"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log.info("thread=%s fired tiger-memory rebuild", thread_key)
    except Exception:  # noqa: BLE001
        log.warning(
            "thread=%s failed to spawn tiger-memory rebuild",
            thread_key, exc_info=True,
        )


# Slack encodes @mentions as `<@U0BOTID>`.
_MENTION_RE = re.compile(r"<@[UW][A-Z0-9]+>\s*")


def _strip_bot_mention(text: str) -> str:
    """Remove all `<@USERID>` mention tags from *text*."""
    return _MENTION_RE.sub("", text)


_ACCEPTED_SUBTYPES: frozenset[str] = frozenset({"file_share"})


def _append_bridge_context(prompt: str, thread_ts: str, channel: str | None) -> str:
    """Append a `[bridge-context]` block so the agent knows where to
    address replies/uploads."""
    lines = ["[bridge-context]", f"slack_thread_ts: {thread_ts}"]
    if channel:
        lines.append(f"slack_channel: {channel}")
    return f"{prompt}\n\n" + "\n".join(lines)


def _is_user_dm(event: dict[str, Any]) -> bool:
    """True iff this is a fresh human message in a DM channel."""
    if event.get("channel_type") != "im":
        return False
    subtype = event.get("subtype")
    if subtype and subtype not in _ACCEPTED_SUBTYPES:
        return False
    if event.get("bot_id"):
        return False
    return True


def build_agent_config(cfg: BridgeConfig) -> AgentConfig:
    """Build the agent's config from the bridge configuration."""
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


def build_bridge(cfg: BridgeConfig, *, state_path: Path | None = None) -> SlackBridge:
    """Compose the live wiring: real backend, real Slack app, persisted
    thread -> session map.

    *state_path* is where the thread -> session map is persisted. When
    ``None`` (single-tenant default), falls back to
    ``persistence.default_state_path()``. Multi-lane callers pass an
    explicit per-lane path so two bridges in one process don't fight
    over the same ``threads.json`` file.
    """
    backend = get_backend("claude_p", cwd=cfg.agent_cwd)
    store = ThreadStore(state_path if state_path is not None else default_state_path())
    return SlackBridge(cfg, backend, build_agent_config(cfg), store)

"""Per-thread persona routing for multi-persona slack-bridge lanes.

A single Slack app per team can DM N personas. The first message of a
new thread is sent through a one-shot routing call that asks the
backend (same vendor / model the persona itself uses) which team
member the user is addressing. The detected persona is stored on the
thread's record and reused for every subsequent message in that thread.

Design notes
------------

* **Same backend as the conversation.** No new SDK dependency, no
  vendor-specific hard-coding. If you swap claude_p for openai-agents
  tomorrow, this code keeps working.

* **Graceful degradation.** Network errors, parse failures, off-roster
  responses all fall back to ``default_persona`` -- the bridge stays up.
  The "wrong" persona then politely redirects via the team-awareness
  preamble (see ``bridge._build_persona_agent_config``).

* **Alias-aware.** Persona aliases (Chinese pinyin, alternate spellings,
  etc.) are included in the routing prompt and accepted in response
  parsing. The caller passes an optional ``aliases`` dict mapping
  canonical persona names to their alias lists; the router expands the
  prompt and builds a reverse index so both the LLM and the parser
  handle aliases correctly.

* **Mock-able.** ``detect_persona`` takes the backend as a parameter so
  unit tests can inject a fake backend with deterministic responses.
"""
from __future__ import annotations

import logging
from typing import Iterable

from tigerharness.agent_sdk import AgentConfig, run_with_retry
from tigerharness.agent_sdk.types import AgentBackend


log = logging.getLogger("tigerharness.slack_bridge.router")


_ROUTER_AGENT_NAME = "slack-bridge-router"
_DEFAULT_TOKEN = "default"


_ROUTER_SYSTEM_PROMPT = """\
You are a one-shot message router. Given a roster of team members and \
the first message of a Slack thread, identify which team member is \
being addressed.

Rules:
- If a team member is clearly addressed by name or by any of their \
listed aliases (e.g. "Hi Ayako" or "Hi 安西教练"), return the \
team member's CANONICAL name (the first name listed, before the aliases).
- A "Thread history" block may accompany the message: earlier messages \
from the same Slack thread, oldest first. When the message itself does \
not clearly address anyone, return the roster member (if any) whose \
name labels or signs the most recent team-authored message in that \
history (e.g. a message starting "[Ayako]: ..."). The message always \
wins over the history when they disagree.
- If no team member is clearly addressed, or the addressed name is \
not in the roster, return the literal word "default".
- Return EXACTLY one roster entry: a canonical name from the roster \
(verbatim, even when it contains a space), or the literal word \
"default". Reply with the name alone -- no commentary, no \
punctuation, no quotes, no formatting.\
"""


_ROUTER_DISALLOWED_TOOLS = [
    # The router is a one-token classifier. It has no business reading
    # files, running shells, fetching URLs, or doing anything other than
    # emitting text. List the common Claude Code tools explicitly so a
    # prompt-injection in an incoming Slack DM can't trick the router
    # into executing anything.
    "Bash",
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "NotebookRead",
    "NotebookEdit",
    "Task",
    "TodoWrite",
    "ExitPlanMode",
]


def _build_router_config() -> AgentConfig:
    """Routing-specific AgentConfig: terse instructions, no tools.

    No model override -- whatever the backend uses by default handles a
    one-token classification trivially.

    Tool surface is locked down via ``disallowed_tools`` covering every
    built-in tool we know about, plus ``permission_mode: plan`` which
    blocks any write/exec action the disallow list might miss, plus
    ``max_turns: 1`` so the model gets exactly one shot to emit its
    answer. Belt + suspenders + zip-tie.
    """
    return AgentConfig(
        name=_ROUTER_AGENT_NAME,
        instructions=_ROUTER_SYSTEM_PROMPT,
        extra={
            # `plan` mode prevents write/exec tool calls at the CLI level.
            "permission_mode": "plan",
            # Explicit deny list covering every common tool. Defense in depth.
            "disallowed_tools": list(_ROUTER_DISALLOWED_TOOLS),
            # Bound the conversation: one user message in, one reply out.
            "max_turns": 1,
        },
    )


#: Cap on the optional thread-history block in the routing prompt.
#: The transcript is oldest-first and the thread ROOT (e.g. the
#: notification DM carrying "[Anzai]: ...") is what identifies the
#: speaking persona, so head-truncation keeps the routing signal.
_CONTEXT_CAP = 3000


def _format_router_prompt(
    message: str,
    roster: Iterable[str],
    aliases: dict[str, list[str]] | None = None,
    context: str | None = None,
) -> str:
    names = list(roster)
    options = ", ".join(names + [_DEFAULT_TOKEN])
    # Cap the message body so a pathologically long first DM doesn't
    # blow up the routing token budget. 4 KB is plenty for a Slack DM
    # opener and still leaves headroom for the rest of the prompt.
    body = message[:4096]

    # Build roster lines. When aliases are provided, show them so the
    # LLM can match non-canonical names (e.g. Chinese pinyin, nicknames).
    if aliases:
        roster_lines: list[str] = []
        for n in names:
            aka = aliases.get(n)
            if aka:
                roster_lines.append(f"{n} (also known as: {', '.join(aka)})")
            else:
                roster_lines.append(n)
        roster_display = "; ".join(roster_lines)
    else:
        roster_display = ", ".join(names)

    parts = [f"Roster: {roster_display}\n"]
    if context:
        # Untracked-thread join: earlier thread messages help route a
        # reply that names nobody (e.g. "Yes, go ahead!" under a
        # "[Anzai]: task complete ..." notification).
        parts.append(
            "Thread history (earlier messages in this thread, oldest "
            f'first): """{context[:_CONTEXT_CAP]}"""\n'
        )
    parts.append(f'Message: """{body}"""\n\n')
    parts.append(f"Return one of: {options}")
    return "".join(parts)


def _build_alias_index(
    roster: list[str],
    aliases: dict[str, list[str]] | None,
) -> dict[str, str]:
    """Build a lowercase token -> canonical name reverse index.

    Includes each canonical name itself (lowercased) plus all its
    aliases. On collision, the first canonical name wins -- this
    mirrors the persona registry's first-registered-wins behavior.
    """
    idx: dict[str, str] = {}
    for name in roster:
        key = name.lower()
        if key not in idx:
            idx[key] = name
        if aliases:
            for alias in aliases.get(name, ()):
                akey = alias.lower()
                if akey not in idx:
                    idx[akey] = name
    return idx


def _parse_router_response(
    raw: str,
    roster: list[str],
    aliases: dict[str, list[str]] | None = None,
) -> str | None:
    """Return a matched persona name (canonical case from *roster*),
    or ``None`` if the response is unparseable or off-roster.

    When *aliases* is provided, the response is also checked against
    alias values, mapping back to the canonical persona name.

    The router is told to return one roster entry (which may contain
    an internal space). We tolerate trailing
    punctuation, whitespace, or surrounding quotes so a model that
    over-formats gets parsed correctly instead of forcing a fallback.
    """
    if not raw:
        return None
    token = raw.strip().strip(".\"'`").lower()
    if not token:
        return None
    if token == _DEFAULT_TOKEN:
        # Caller resolves "default" to the team's default_persona.
        return _DEFAULT_TOKEN
    # Build reverse index covering canonical names + aliases.
    idx = _build_alias_index(roster, aliases)
    canonical = idx.get(token)
    if canonical is not None:
        return canonical
    return None


async def detect_persona(
    backend: AgentBackend,
    message: str,
    roster: list[str],
    default_persona: str,
    aliases: dict[str, list[str]] | None = None,
    context: str | None = None,
) -> tuple[str, float]:
    """Route the first message of a new thread to a persona.

    Returns ``(persona_name, cost_usd)`` -- the matched persona (or
    *default_persona* on any failure path), and the actual cost of the
    routing call (0.0 when the call was skipped or failed before cost
    could be reported).

    *aliases* is an optional mapping of canonical persona name to a
    list of alternate names (nicknames, translations, pinyin, etc.).
    When provided, aliases are included in the LLM routing prompt and
    accepted in response parsing.

    *context* is an optional transcript of earlier messages in the
    thread (untracked-thread join -- see ``history.py``). It lets the
    router pick the persona who authored the thread's earlier messages
    when the reply itself names nobody.

    *default_persona* must itself be in *roster* -- the loader enforces
    that at startup, but we don't re-check here because the failure
    case is "return what was passed in" anyway.
    """
    if not roster:
        # No personas to route to. Shouldn't happen given loader
        # validation, but be defensive.
        return default_persona, 0.0

    # Single-persona teams: skip the LLM call entirely. The routing
    # decision is forced; the awareness preamble in bridge.py still
    # tells the persona about her teammates (zero in this case, so
    # the preamble degenerates).
    if len(roster) == 1:
        return roster[0], 0.0

    cfg = _build_router_config()
    prompt = _format_router_prompt(
        message, roster, aliases=aliases, context=context
    )

    try:
        session = await backend.open_session(resume_id=None)
        try:
            result = await run_with_retry(
                backend, cfg, prompt,
                session=session, max_attempts=2, label="router",
            )
        finally:
            try:
                await session.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                log.debug("router session.close raised", exc_info=True)
    except Exception:
        log.warning(
            "persona router failed; falling back to default=%s",
            default_persona, exc_info=True,
        )
        return default_persona, 0.0

    # Capture the call cost so the bridge can sum router + agent spend.
    cost = float(getattr(result, "cost_usd", None) or 0.0)

    raw = result.final_output or ""
    matched = _parse_router_response(raw, roster, aliases=aliases)
    if matched is None:
        log.info(
            "router response %r not in roster %s; falling back to default=%s",
            raw[:120], roster, default_persona,
        )
        return default_persona, cost
    if matched == _DEFAULT_TOKEN:
        return default_persona, cost
    log.info(
        "router selected persona=%s for message: %r (cost_usd=%.6f)",
        matched, message[:120], cost,
    )
    return matched, cost

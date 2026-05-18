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
- If a team member is clearly addressed by name (e.g. "Hi Ayako"), \
return that exact name.
- If no team member is clearly addressed, or the addressed name is \
not in the roster, return the literal word "default".
- Return EXACTLY one token: a name from the roster, or "default". \
No commentary, no punctuation, no quotes, no formatting.\
"""


def _build_router_config() -> AgentConfig:
    """Routing-specific AgentConfig: terse instructions, no tools.

    No model override -- whatever the backend uses by default handles a
    one-token classification trivially.
    """
    return AgentConfig(
        name=_ROUTER_AGENT_NAME,
        instructions=_ROUTER_SYSTEM_PROMPT,
        extra={
            "permission_mode": "bypassPermissions",
            # Strict short-answer setting where supported.
            "max_turns": 1,
        },
    )


def _format_router_prompt(message: str, roster: Iterable[str]) -> str:
    names = list(roster)
    options = ", ".join(names + [_DEFAULT_TOKEN])
    # Cap the message body so a pathologically long first DM doesn't
    # blow up the routing token budget. 4 KB is plenty for a Slack DM
    # opener and still leaves headroom for the rest of the prompt.
    body = message[:4096]
    return (
        f"Roster: {', '.join(names)}\n"
        f'Message: """{body}"""\n\n'
        f"Return one of: {options}"
    )


def _parse_router_response(raw: str, roster: list[str]) -> str | None:
    """Return a matched persona name (canonical case from *roster*),
    or ``None`` if the response is unparseable or off-roster.

    The router is told to return one token. We tolerate trailing
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
    for name in roster:
        if token == name.lower():
            return name
    return None


async def detect_persona(
    backend: AgentBackend,
    message: str,
    roster: list[str],
    default_persona: str,
) -> str:
    """Route the first message of a new thread to a persona.

    Returns a name from *roster* (canonical case), or *default_persona*
    on any failure path (backend error, off-roster response, empty).

    *default_persona* must itself be in *roster* -- the loader enforces
    that at startup, but we don't re-check here because the failure
    case is "return what was passed in" anyway.
    """
    if not roster:
        # No personas to route to. Shouldn't happen given loader
        # validation, but be defensive.
        return default_persona

    # Single-persona teams: skip the LLM call entirely. The routing
    # decision is forced; the awareness preamble in bridge.py still
    # tells the persona about her teammates (zero in this case, so
    # the preamble degenerates).
    if len(roster) == 1:
        return roster[0]

    cfg = _build_router_config()
    prompt = _format_router_prompt(message, roster)

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
        return default_persona

    raw = result.final_output or ""
    matched = _parse_router_response(raw, roster)
    if matched is None:
        log.info(
            "router response %r not in roster %s; falling back to default=%s",
            raw[:120], roster, default_persona,
        )
        return default_persona
    if matched == _DEFAULT_TOKEN:
        return default_persona
    log.info("router selected persona=%s for message: %r", matched, message[:120])
    return matched

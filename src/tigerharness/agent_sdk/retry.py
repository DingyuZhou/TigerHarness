"""Retry helper around ``AgentBackend.run``.

``claude -p`` (and any remote backend) is subject to transient failures:
stream idle timeouts, rate-limit-window edges, ephemeral network blips.
These don't reflect a real problem with the prompt — the right response
is to retry, not to surface ``:warning:`` to the user.

Usage::

    from tigerharness.agent_sdk import get_backend
    from tigerharness.agent_sdk.retry import run_with_retry

    backend = get_backend("claude_p")
    result = await run_with_retry(
        backend, cfg, prompt, session=session, max_attempts=3,
    )

Design picks
------------

- **Three attempts, exponential backoff (1 s, 2 s, 4 s).** Total wait
  ≤ 7 s. Anything bigger feels like a hang to an Operator on Slack. Anything
  smaller doesn't outlast a typical rate-limit window edge.
- **Retry every exception.** We don't have a reliable taxonomy of
  "transient vs permanent" for ``claude -p`` (errors come through as
  generic strings). Retrying everything is wasteful in the
  truly-permanent case (e.g. bad system prompt) but never destructive —
  the same prompt going through 3 times will just fail 3 times in ~7 s.
- **Logged at each retry boundary.** Caller can see in their service
  log how many attempts each call took, and which exception triggered
  the retry.
- **Cancellation propagates.** ``asyncio.CancelledError`` is re-raised
  immediately — we don't want a caller's cancel to be silently
  swallowed by a backoff sleep.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from .types import (
    AgentBackend,
    AgentConfig,
    ApprovalCallback,
    Event,
    InputMessage,
    RunResult,
    Session,
)


log = logging.getLogger("tigerharness.agent_sdk.retry")


DEFAULT_MAX_ATTEMPTS: int = 3
DEFAULT_BASE_DELAY_S: float = 1.0


def _fire(callback: Callable[[Any], None], arg: Any, *, what: str) -> None:
    """Call a caller-supplied progress callback, swallowing anything it
    raises.

    The guard lives here rather than in the callback's owner because
    ``on_event``/``on_retry`` are now part of this module's public
    signature and the next caller to pass one will not have read the
    design. An unguarded raise from ``on_event`` would be caught by the
    general handler below and trigger a full retry of the turn — a
    progress callback silently causing a duplicate agent run. From
    ``on_retry`` it is worse: that fires from inside the ``except``
    handler, so the callback's traceback would REPLACE the backend
    exception that actually broke the turn.
    """
    try:
        callback(arg)
    except Exception:
        log.warning(
            "run_with_retry: %s callback raised (ignored)",
            what,
            exc_info=True,
        )


async def _run_tapped(
    backend: AgentBackend,
    config: AgentConfig,
    prompt: str | list[InputMessage],
    *,
    session: Session | None,
    approval: ApprovalCallback | None,
    on_event: Callable[[Event], None],
) -> RunResult:
    """One attempt, driven through the stream so events are observable.

    Behaviour-neutral versus ``backend.run``: every backend implements
    ``run()`` as ``run_via_stream(self.run_stream(...))``
    (``backends/_base.py``). The ``async with`` satisfies the documented
    ``StreamHandle`` cleanup contract on normal completion, on exception
    AND on cancellation, and ``.result`` is read inside the block after
    the loop finishes — reading it earlier raises by design.
    """
    async with backend.run_stream(
        config, prompt, session=session, approval=approval
    ) as handle:
        async for event in handle:
            _fire(on_event, event, what="on_event")
        return handle.result


async def run_with_retry(
    backend: AgentBackend,
    config: AgentConfig,
    prompt: str | list[InputMessage],
    *,
    session: Session | None = None,
    approval: ApprovalCallback | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay_s: float = DEFAULT_BASE_DELAY_S,
    label: str = "",
    on_event: Callable[[Event], None] | None = None,
    on_retry: Callable[[int], None] | None = None,
) -> RunResult:
    """Wrap ``backend.run`` with at most ``max_attempts`` total tries.

    Backoff between attempts is exponential: ``base_delay_s * 2**(n-1)``
    for attempt n (1-indexed; no sleep before attempt 1). Default
    schedule (3 attempts, 1 s base) sleeps 1 s before attempt 2 and
    2 s before attempt 3 — total ≤ 3 s of pure-wait overhead, plus
    however long the calls themselves take.

    Args:
        backend: any ``AgentBackend``.
        config, prompt, session, approval: passed through to ``backend.run``.
        max_attempts: total tries; must be ≥ 1.
        base_delay_s: initial sleep before retry 1 (a.k.a. attempt 2).
        label: free-text tag included in log lines so the operator can
            tell which call retried (thread id, job id, etc.).
        on_event: optional progress tap. When supplied, each attempt is
            driven through ``backend.run_stream`` and every Event is
            forwarded. When ``None`` (the default) the ``backend.run``
            path is untouched, so existing callers are unchanged by
            construction rather than by assertion.
        on_retry: optional notification, called with the attempt number
            that just failed, immediately BEFORE the backoff sleep. The
            ordering is the point: called after the sleep it would only
            describe a window that has already closed, and a progress
            reporter would render a scheduled wait as a hang.

    Neither callback can break or retry a turn: both are invoked through
    a guard that logs and swallows.

    Raises:
        The last exception observed if every attempt failed.
        ``asyncio.CancelledError`` propagates immediately without retry.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1; got {max_attempts}")

    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            if on_event is None:
                return await backend.run(
                    config, prompt, session=session, approval=approval
                )
            return await _run_tapped(
                backend,
                config,
                prompt,
                session=session,
                approval=approval,
                on_event=on_event,
            )
        except asyncio.CancelledError:
            # User cancellation — never retry.
            raise
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts:
                log.warning(
                    "backend.run failed on attempt %d/%d (giving up): %s%r",
                    attempt, max_attempts,
                    f"[{label}] " if label else "",
                    exc,
                )
                raise
            delay = base_delay_s * (2 ** (attempt - 1))
            log.info(
                "backend.run failed on attempt %d/%d (%sretrying in %.1fs): %r",
                attempt, max_attempts,
                f"[{label}] " if label else "",
                delay, exc,
            )
            if on_retry is not None:
                _fire(on_retry, attempt, what="on_retry")
            await asyncio.sleep(delay)

    # Unreachable: loop either returned, raised on the final attempt,
    # or raised CancelledError. Defensive belt + braces:
    assert last_exc is not None  # pragma: no cover
    raise last_exc  # pragma: no cover

"""Mid-flight progress heartbeats for long Slack bridge turns.

A Slack turn emits nothing until it finishes, so the Operator cannot tell
a healthy 12-minute turn from one that died eight minutes ago.
:class:`TurnProgress` posts one parent message to a separate ops-log
channel when a turn crosses the first interval, threads a one-line pulse
under it every interval after that, and posts a closer when the turn ends
— success or failure. The DM thread the Operator converses in is never
touched.

Three rules, copied verbatim from ``autodrive/notifier.py``:

* **Model-free.** A pulse is a plain Slack HTTP POST. This module never
  spawns an agent and costs no model tokens.
* **Never breaks a turn.** Nothing here raises into the caller
  (``asyncio.CancelledError`` excepted — see below).
* **Degrades to silence.** No creds or no channel means a reporter that
  does nothing at all.

``CancelledError`` is the one exception that must propagate, from both
:meth:`TurnProgress.run` and :meth:`TurnProgress.finish`: the bridge
bounds both with ``asyncio.wait_for``, and that bound works only by
cancelling the coroutine at its ``to_thread`` await. Catching it here
would make those bounds fictional.

Observable failures (the rest of this feature is correctly silent):

* the Slack post failed — falsy return or exception — WARNING, here;
* configured creds but no ops-log channel, so inert — INFO, here.

The other four (reporter crashed, reporter would not stop, closer would
not post, event tap raised) come from ``slack_bridge.bridge`` and
``agent_sdk.retry``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from typing import Any

from ..agent_sdk.types import Event, RunStart, ToolCall
from .notify import SlackNotifier, _load_slack_bridge_dotenv


log = logging.getLogger("tigerharness.slack_bridge.progress")


#: The Operator asked for five minutes, uniform. Nothing else in the
#: suite checks this number -- every other test injects a millisecond
#: interval through the seam below -- so it has its own test.
DEFAULT_INTERVAL_S: float = 300.0

#: Channel names in precedence order. First non-empty value wins; an
#: empty value does NOT count as set. There is deliberately no DM
#: fallback: the whole point of the feature is keeping the DM clean.
CHANNEL_ENV_VARS: tuple[str, ...] = (
    "TIGERHARNESS_BRIDGE_PROGRESS_CHANNEL",
    "SLACK_NOTIFY_CHANNEL",
)

#: Max rendered length of the parent's prompt excerpt, and of a tool hint.
HEADER_MAX: int = 120
HINT_MAX: int = 60

#: Tools whose ``file_path`` argument is safe to render.
_PATH_TOOLS = frozenset({"Read", "Edit", "Write"})


# ---------------------------------------------------------------------------
# Redaction (an allowlist -- a denylist would pass the next new tool)
# ---------------------------------------------------------------------------

def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def tool_hint(name: str, arguments: Any) -> str:
    """The only argument-derived text that may reach Slack.

    ``Read``/``Edit``/``Write`` render their ``file_path``; ``Bash``
    renders the first whitespace-delimited token of its command; every
    other tool renders nothing, so a tool added next month is safe
    without anyone remembering this file.
    """
    # `claude -p` builds arguments from `blk.get("input") or {}`, so a
    # non-dict can arrive from the wire. Missing keys and non-string
    # values render no hint rather than `None`.
    args = arguments if isinstance(arguments, dict) else {}
    if name in _PATH_TOOLS:
        return _truncate(_as_str(args.get("file_path")), HINT_MAX)
    if name == "Bash":
        tokens = _as_str(args.get("command")).split(None, 1)
        if not tokens:
            return ""
        first = tokens[0]
        # `SECRET=xoxb-... cmd` is a legal command whose first token is
        # the assignment; the naive first-word rule would post it.
        if "=" in first:
            return ""
        return _truncate(first, HINT_MAX)
    return ""


def sanitize_header(text: str) -> str:
    """Flatten an excerpt of the Operator's message for a shared channel.

    Collapses all whitespace (a multi-line prompt would otherwise become
    a wall of text), drops backticks and fences, and truncates. This runs
    at the post site on whatever string the caller supplied, so a caller
    cannot leak by forgetting to sanitise.
    """
    return _truncate(" ".join(text.replace("`", " ").split()), HEADER_MAX)


def _minutes(seconds: float) -> int:
    return int(seconds // 60)


# ---------------------------------------------------------------------------
# The reporter
# ---------------------------------------------------------------------------

class TurnProgress:
    """Pulse loop for one bridge turn. Never raises into the turn."""

    def __init__(
        self,
        notifier: SlackNotifier | None,
        channel: str | None,
        *,
        header: str,
        interval_s: float = DEFAULT_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._notifier = notifier
        self._channel = channel
        self._header = header
        self._interval_s = interval_s
        self._clock = clock
        self._persona: str | None = None
        self._start = clock()
        self._last_event_at = self._start
        self._tool_calls = 0
        self._attempt = 1
        self._last_tool: ToolCall | None = None
        self._retrying = False
        self._compacting = False
        self._started = False
        self._parent_ts: str | None = None
        self._inert = notifier is None or not channel
        self._stop = asyncio.Event()

    # ---- signals from the turn (sync, no I/O, never raise) ----

    def on_event(self, event: Event) -> None:
        """Record liveness. Called from inside the stream loop."""
        self._last_event_at = self._clock()
        if isinstance(event, RunStart):
            # A retry re-runs the agent from scratch, so tool calls
            # repeat; the count is per-attempt, elapsed is not.
            self._tool_calls = 0
            self._retrying = False
        elif isinstance(event, ToolCall):
            self._tool_calls += 1
            self._last_tool = event

    def retrying(self, attempt: int) -> None:
        """*attempt* just failed; a backoff sleep follows.

        The sleep is quiet by design, so it must not render as a stall.
        Cleared by the next ``RunStart``.
        """
        self._retrying = True
        self._attempt = attempt + 1

    def compacting(self) -> None:
        """The bridge's idle-compact turn is running.

        Another quiet-by-design window, and the last one: nothing follows
        a compaction except teardown, so this never clears.
        """
        self._compacting = True

    def set_persona(self, persona: str) -> None:
        """Name the turn's persona, which the bridge learns after the
        reporter is constructed. The parent renders without it rather
        than waiting."""
        self._persona = persona

    def request_stop(self) -> None:
        """Cooperative stop. ``run`` returns at its next boundary, so an
        in-flight POST completes instead of being orphaned."""
        self._stop.set()

    @property
    def started(self) -> bool:
        """True once the parent message posted."""
        return self._started

    # ---- rendering ----

    def _render_parent(self) -> str:
        who = "still working"
        if self._persona:
            who = f"{self._persona} still working"
        return f':hourglass: {who} — "{sanitize_header(self._header)}"'

    @staticmethod
    def _render_tool(call: ToolCall) -> str:
        hint = tool_hint(call.name, call.arguments)
        if hint:
            return f"{call.name}({hint})"
        return call.name

    def _render_pulse(self) -> str:
        now = self._clock()
        parts = [
            f"{_minutes(now - self._start)}m",
            f"{self._tool_calls} tool calls",
        ]
        if self._compacting:
            parts.append("compacting session")
        elif self._retrying:
            parts.append(f"retrying (attempt {self._attempt})")
        else:
            idle = now - self._last_event_at
            if idle >= 2 * self._interval_s:
                parts.append(
                    f"no activity for {_minutes(idle)}m :warning:"
                )
            elif self._last_tool is not None:
                parts.append(
                    f"last: {self._render_tool(self._last_tool)}"
                )
            if self._attempt > 1:
                parts.append(f"attempt {self._attempt}")
        return " · ".join(parts)

    # ---- the single post site ----

    async def _post(self, text: str, *, thread_ts: str | None) -> str | None:
        """Every Slack call this module makes goes through here.

        The channel guard is here rather than in ``__init__`` because
        ``dm_text``/``post_text`` treat a falsy channel as "the
        Operator's DM" -- the one place a pulse must never land. A
        construction-time check would leave that door open to any later
        edit.

        ``SlackNotifier`` posts synchronously and we live in the bridge's
        event loop, so the call goes off-loop.
        """
        if self._notifier is None or not self._channel:
            return None
        try:
            ts = await asyncio.to_thread(
                self._notifier.post_text,
                text,
                channel=self._channel,
                thread_ts=thread_ts,
            )
        except Exception:
            log.warning(
                "progress: slack post raised for channel %s",
                self._channel,
                exc_info=True,
            )
            return None
        if not ts:
            log.warning(
                "progress: slack post failed for channel %s",
                self._channel,
            )
        return ts

    # ---- the loop ----

    async def _tick(self) -> None:
        if not self._started:
            ts = await self._post(self._render_parent(), thread_ts=None)
            if not ts:
                # Retrying a bad channel id every interval never
                # succeeds and logs forever. Go quiet instead.
                self._inert = True
                return
            self._parent_ts = ts
            self._started = True
        await self._post(self._render_pulse(), thread_ts=self._parent_ts)

    async def run(self) -> None:
        """Post a pulse per interval until stopped.

        Each iteration awaits the stop event with ``interval_s`` as a
        TIMEOUT: the timeout expiring means the interval elapsed (post),
        the event being set means stop (return). A turn shorter than one
        interval therefore posts nothing at all -- not even a parent.
        """
        if self._inert:
            return
        while True:
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._interval_s
                )
            except asyncio.TimeoutError:
                pass
            else:
                return
            try:
                await self._tick()
            except Exception:
                log.warning("progress: pulse failed", exc_info=True)
            if self._inert:
                return

    async def finish(self, *, ok: bool, detail: str = "") -> None:
        """Post the closer, if a parent was ever posted."""
        if not self._started:
            return
        elapsed = _minutes(self._clock() - self._start)
        if ok:
            text = (
                f":white_check_mark: done in {elapsed}m "
                f"· {self._tool_calls} tool calls"
            )
        else:
            text = f":x: failed after {elapsed}m"
            if detail:
                text += f" · {detail}"
        await self._post(text, thread_ts=self._parent_ts)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def resolve_progress_channel() -> str | None:
    """First non-empty candidate, scanned in order.

    An or-chain would let ``TIGERHARNESS_BRIDGE_PROGRESS_CHANNEL=""``
    win and silently disable the feature, which is indistinguishable
    from "not configured".
    """
    _load_slack_bridge_dotenv()
    for name in CHANNEL_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def build_turn_progress(
    header: str,
    *,
    interval_s: float = DEFAULT_INTERVAL_S,
) -> TurnProgress:
    """Resolve creds + channel and return a live or inert reporter.

    Never returns ``None`` and never raises: the caller must not have to
    branch on configuration.
    """
    notifier = SlackNotifier.try_load()
    channel = resolve_progress_channel()
    if notifier is not None and not channel:
        log.info(
            "progress: slack creds present but no ops-log channel "
            "(set %s); turn progress heartbeats are off",
            CHANNEL_ENV_VARS[0],
        )
    return TurnProgress(
        notifier, channel, header=header, interval_s=interval_s
    )

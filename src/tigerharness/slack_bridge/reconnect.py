"""Surviving a Socket Mode session that dies without saying so.

Two independent mechanisms, in order of how much they matter:

``run_catchup`` -- **the fix.** On every newly established session the
bridge asks Slack what it missed and feeds it through the normal
dispatch path. Correctness does not depend on noticing the outage in
time, only on noticing it eventually.

``SocketLivenessWatchdog`` -- **the mitigation.** Shrinks the window.
It runs on a dedicated OS thread, on purpose. Why that is not
over-engineering, from the 2026-08-17 incident:

slack_sdk's own detector (``monitor_current_session``) computes its
staleness threshold as ``ping_interval * 4`` -- 20 seconds at the
default ``ping_interval=5``. In production it reported *909 to 1052*
seconds, every time, for 41 consecutive reconnects. The observed
distribution clusters at ~930s, which is not a multiple of anything in
the SDK; it is this host's ``net.ipv4.tcp_retries2 = 15``
retransmission give-up budget (~925s). And in the incident the stale
verdict landed 0.9 seconds *after* the first inbound frame in 26
minutes: the detector woke because traffic returned, not because its
timer expired.

The lesson is not "the threshold is too high". Lowering ``ping_interval``
would lower a threshold that is never evaluated during the outage. The
lesson is that a detector sharing an event loop, a socket, and a network
with the failure it is meant to detect is not an independent observer.
So this watchdog: (a) lives on its own thread with its own clock, (b)
never writes to the socket it is judging -- it only reads timestamps
that *inbound* traffic advances, (c) logs from the thread **before**
touching the loop, so the "I think we are dead" record survives even if
the loop never runs the reconnect.

That last point is also the watchdog's honest limitation: forcing a
reconnect means scheduling a coroutine, and a wedged loop will not run
it. The watchdog is best-effort by construction. ``run_catchup`` is
what makes the window harmless.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .persistence import SeenLedger, _ts_sort_key

log = logging.getLogger("tigerharness.slack_bridge.reconnect")


def _env_flag(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_number(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning(
            "%s=%r is not a number; using the default %s", key, raw, default
        )
        return default


# ---------------------------------------------------------------------------
# Deliverable 1: notice a dead socket in seconds
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WatchdogConfig:
    """Tuning for :class:`SocketLivenessWatchdog`.

    The defaults are the safer, faster behaviour: on by default, and a
    30s silence threshold against the SDK's effective ~930s.
    """

    enabled: bool = True
    stale_after_s: float = 30.0
    poll_interval_s: float = 5.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> WatchdogConfig:
        e = os.environ if env is None else env
        return cls(
            enabled=_env_flag(e, "TIGERHARNESS_SLACK_WATCHDOG", True),
            stale_after_s=_env_number(
                e, "TIGERHARNESS_SLACK_WATCHDOG_STALE_S", 30.0
            ),
            poll_interval_s=_env_number(
                e, "TIGERHARNESS_SLACK_WATCHDOG_POLL_S", 5.0
            ),
        )


class SocketLivenessWatchdog:
    """Forces a Socket Mode reconnect when nothing has arrived for a while.

    "Nothing has arrived" is the maximum of two inbound-only clocks: the
    SDK's ``last_ping_pong_time`` (advanced when a pong comes back) and
    :meth:`mark_activity`, which the bridge calls on every frame Slack
    sends. Both can only move forward because the far end spoke. Nothing
    this class does can refresh them, which is the point -- a detector
    that can reassure itself is not a detector.
    """

    def __init__(
        self,
        client: Any,
        cfg: WatchdogConfig,
        *,
        lane: str = "",
        loop: asyncio.AbstractEventLoop | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._cfg = cfg
        self._lane = lane
        self._loop = loop
        self._now = now
        # Written from the event loop, read from the watchdog thread. A
        # float store/load is a single bytecode op under the GIL, so a
        # reader sees either the old or the new value and never a torn
        # one -- which is all this needs.
        self._last_activity = now()
        self._last_force = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ----- liveness input -----

    def mark_activity(self) -> None:
        """Record that Slack just sent us something."""
        self._last_activity = self._now()

    def _last_inbound(self) -> float:
        pong = getattr(self._client, "last_ping_pong_time", None)
        if isinstance(pong, (int, float)):
            return max(self._last_activity, float(pong))
        return self._last_activity

    # ----- the decision -----

    def check_once(self) -> bool:
        """One evaluation. ``True`` iff a reconnect was forced.

        Separate from the thread loop so the interesting logic is
        testable without sleeping.
        """
        now = self._now()
        silent_for = now - self._last_inbound()
        if silent_for < self._cfg.stale_after_s:
            return False
        # One force per threshold window. Without this, a loop that is
        # slow to reconnect would collect a reconnect request per poll.
        if now - self._last_force < self._cfg.stale_after_s:
            return False
        self._last_force = now
        log.warning(
            "lane=%s socket watchdog: nothing inbound for %.0fs "
            "(threshold %.0fs) -- forcing a Socket Mode reconnect",
            self._lane, silent_for, self._cfg.stale_after_s,
        )
        self._request_reconnect()
        return True

    def _request_reconnect(self) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            log.warning(
                "lane=%s socket watchdog has no live event loop to "
                "reconnect on; catch-up on the next session is the "
                "remaining safety net", self._lane,
            )
            return
        try:
            loop.call_soon_threadsafe(self._schedule_reconnect)
        except RuntimeError:
            # The loop shut down between the check and the call.
            log.warning(
                "lane=%s socket watchdog could not reach the event loop",
                self._lane, exc_info=True,
            )

    def _schedule_reconnect(self) -> None:
        asyncio.ensure_future(self._reconnect())

    async def _reconnect(self) -> None:
        try:
            await self._client.connect_to_new_endpoint(force=True)
        except Exception:  # noqa: BLE001 - reconnect is best-effort
            log.warning(
                "lane=%s watchdog-forced reconnect raised", self._lane,
                exc_info=True,
            )

    # ----- lifecycle -----

    def start(self) -> None:
        if not self._cfg.enabled:
            log.warning(
                "lane=%s socket watchdog disabled -- a dead socket will "
                "go unnoticed for as long as the Slack SDK takes "
                "(~15min observed)", self._lane,
            )
            return
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        self.mark_activity()
        self._thread = threading.Thread(
            target=self._run,
            name=f"slack-watchdog-{self._lane or 'bridge'}",
            daemon=True,
        )
        self._thread.start()
        log.info(
            "lane=%s socket watchdog started (stale_after=%.0fs poll=%.0fs)",
            self._lane, self._cfg.stale_after_s, self._cfg.poll_interval_s,
        )

    def _run(self) -> None:
        while not self._stop.wait(self._cfg.poll_interval_s):
            try:
                self.check_once()
            except Exception:  # noqa: BLE001 - the thread must not die
                log.warning(
                    "lane=%s socket watchdog check raised", self._lane,
                    exc_info=True,
                )

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------
# Deliverable 2: recover what was missed
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CatchupConfig:
    """Tuning for :func:`run_catchup`.

    The bounds exist so a bridge that was down overnight does not wake
    up and fire a hundred stale prompts at the agent. Anything they
    exclude is logged at WARNING -- a silent cap here would recreate
    the exact "it vanished and nobody said anything" failure that this
    module exists to remove.
    """

    enabled: bool = True
    max_age_s: float = 3600.0
    max_messages: int = 50

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> CatchupConfig:
        e = os.environ if env is None else env
        return cls(
            enabled=_env_flag(e, "TIGERHARNESS_SLACK_CATCHUP", True),
            max_age_s=_env_number(
                e, "TIGERHARNESS_SLACK_CATCHUP_MAX_AGE_S", 3600.0
            ),
            max_messages=int(
                _env_number(e, "TIGERHARNESS_SLACK_CATCHUP_MAX_MESSAGES", 50)
            ),
        )


class CatchupTarget(Protocol):
    """What :func:`run_catchup` needs from the bridge.

    Deliberately narrow: this module knows about Slack's history API and
    the bounds, the bridge knows about dispatch and its guards, and
    neither reaches into the other.
    """

    def catchup_threads(self) -> list[tuple[str, str]]:
        """``(channel, thread_ts)`` for every thread worth re-checking."""

    def is_replay_candidate(self, event: Mapping[str, Any]) -> bool:
        """Could this event plausibly be a missed inbound message?

        A history page is mostly the bridge's *own* replies. Rejecting
        those here keeps them from eating the redelivery budget. The
        bridge answers because the accept/reject rules are its own.
        """

    async def replay(self, event: dict[str, Any]) -> None:
        """Feed one recovered event through the normal guarded path."""


async def run_catchup(
    *,
    target: CatchupTarget,
    ledger: SeenLedger,
    web_client: Any,
    cfg: CatchupConfig,
    lane: str = "",
    now: Callable[[], float] = time.time,
) -> int:
    """Replay everything Slack accepted while we were not listening.

    Returns how many messages were handed to *target*. Safe to call on
    every session -- including the first one after a restart, which is
    its own kind of gap. Double delivery is impossible regardless of
    how often this runs: the ledger's compare-and-set, not this
    function's bookkeeping, is what decides whether a message is new.
    """
    if not cfg.enabled:
        log.warning(
            "lane=%s reconnect catch-up is disabled -- messages sent "
            "while the socket was down will NOT be recovered", lane,
        )
        return 0

    floor = now() - cfg.max_age_s
    budget = _Budget(cfg.max_messages, lane)
    replayed = 0
    too_old = 0
    # The passes overlap -- a DM's history and its threads' replies can
    # surface the same message. The ledger would stop the second
    # dispatch, but not before it had spent a slot of the redelivery
    # budget, which would push real messages out of the window.
    offered: set[tuple[str, str]] = set()

    def _claim(event: Mapping[str, Any]) -> bool:
        key = (str(event.get("channel")), str(event.get("ts")))
        if key in offered:
            return False
        offered.add(key)
        return True

    for channel, channel_type in ledger.channels():
        watermark = ledger.watermark(channel)
        # The age bound is mostly enforced by the cursor we hand Slack,
        # which means the messages it excludes never come back for us to
        # count. Saying so here is the difference between a bound and a
        # silent drop -- and this is exactly the "bridge was down
        # overnight" case the bound exists for.
        if watermark is not None and _ts_sort_key(watermark) < floor:
            log.warning(
                "lane=%s catch-up for %s starts at the %.0fs age bound, "
                "not at the last message it handled -- anything sent "
                "between them was NOT delivered", lane, channel,
                cfg.max_age_s,
            )
        messages = await _fetch(
            lambda oldest, c=channel: web_client.conversations_history(
                channel=c, oldest=oldest, limit=200,
            ),
            oldest=_oldest(watermark, floor),
            what=f"history of {channel}",
            lane=lane,
        )
        for msg in messages:
            event = dict(msg)
            event["channel"] = channel
            event.setdefault("channel_type", channel_type)
            if not target.is_replay_candidate(event) or not _claim(event):
                continue
            if _older_than(event, floor):
                too_old += 1
                continue
            if not budget.take():
                break
            await target.replay(event)
            replayed += 1

    for channel, thread_ts in target.catchup_threads():
        messages = await _fetch(
            lambda oldest, c=channel, t=thread_ts: web_client.conversations_replies(
                channel=c, ts=t, oldest=oldest, limit=200,
            ),
            # The age floor, NOT the channel watermark: a channel's
            # watermark advances with its top-level messages, so using it
            # here would hide a thread reply that is older than the last
            # DM but was never handled. Re-offering replies costs nothing
            # -- the ledger rejects the ones we already answered.
            oldest=f"{floor:.6f}",
            what=f"replies in {channel}/{thread_ts}",
            lane=lane,
        )
        for msg in messages:
            event = dict(msg)
            event["channel"] = channel
            event.setdefault("thread_ts", thread_ts)
            if not target.is_replay_candidate(event) or not _claim(event):
                continue
            if _older_than(event, floor):
                too_old += 1
                continue
            if not budget.take():
                break
            await target.replay(event)
            replayed += 1

    if too_old:
        log.warning(
            "lane=%s catch-up skipped %d message(s) older than the %.0fs "
            "age bound -- they were NOT delivered", lane, too_old,
            cfg.max_age_s,
        )
    log.info(
        "lane=%s catch-up offered %d message(s) for redelivery", lane, replayed
    )
    return replayed


class _Budget:
    """A countdown that complains exactly once when it runs out."""

    def __init__(self, limit: int, lane: str) -> None:
        self._left = limit
        self._limit = limit
        self._lane = lane
        self._warned = False

    def take(self) -> bool:
        if self._left > 0:
            self._left -= 1
            return True
        if not self._warned:
            self._warned = True
            log.warning(
                "lane=%s catch-up hit its %d-message bound -- the rest of "
                "the backlog was NOT delivered; raise "
                "TIGERHARNESS_SLACK_CATCHUP_MAX_MESSAGES to recover it",
                self._lane, self._limit,
            )
        return False


def _oldest(watermark: str | None, floor: float) -> str:
    """The ``oldest`` cursor to ask Slack for.

    Purely an efficiency hint -- the ledger is what prevents duplicates,
    so being too generous here costs an API page, not a double reply.
    """
    if watermark is None:
        return f"{floor:.6f}"
    return max(watermark, f"{floor:.6f}", key=_ts_sort_key)


def _older_than(event: Mapping[str, Any], floor: float) -> bool:
    return _ts_sort_key(str(event.get("ts", ""))) < floor


async def _fetch(
    call: Callable[[str], Any],
    *,
    oldest: str,
    what: str,
    lane: str,
) -> Iterable[Mapping[str, Any]]:
    """Run one history call. A failure is loud and empty, never fatal --
    one unreadable conversation must not cost the others their catch-up.
    """
    try:
        response = await call(oldest)
    except Exception:  # noqa: BLE001 - any Slack/transport error
        log.warning(
            "lane=%s catch-up could not read %s -- anything missed there "
            "stays missed", lane, what, exc_info=True,
        )
        return []
    messages = response.get("messages") or []
    if not isinstance(messages, list):  # pragma: no cover - defensive
        return []
    # Slack returns newest-first; replay in the order the Operator typed.
    return sorted(
        (m for m in messages if isinstance(m, dict)),
        key=lambda m: _ts_sort_key(str(m.get("ts", ""))),
    )

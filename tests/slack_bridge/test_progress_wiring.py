"""``_dispatch``-level wiring tests for the turn-progress reporter.

The seat split, restated from ``test_progress.py``'s docstring: that file
tests ``TurnProgress`` as a unit; this one tests what ``_dispatch``
*passes* it. Criteria 3b, 5 mode 4, 6, 6a, 6d, 6e(c), 6f, 7b(b), 7c(b)
and 7c(c) live here, plus §8's failures 3, 5 and 6 (the three the bridge
emits — a module cannot log its own failure to respond) and the two
wire-level rows the plan's table lacks: that ``on_event`` reaches the
pulse at all, and that the closer's ``ok``/``detail`` are derived from
the outcome rather than hardcoded.

Every test here is a **wire** test. The distinction is load-bearing: a
reporter method can be perfect and never be called, and the bugs that
shape produces are silent — a parent built from ``prompt`` instead of
``text`` leaves criterion 3 green while turning every parent in the ops
channel into 120 characters of identical injected boilerplate.

Two seams make the timing deterministic rather than hopeful:

* ``_Reporter.run`` waits on a **gate** the patched ``run_with_retry``
  opens. The reporter is constructed as the first statement in
  ``_dispatch`` but ``set_persona`` lands several awaits later, so
  without the gate "did the parent post before the persona arrived"
  would be a race against however long a ``ThreadStore`` write takes on
  the host. With it, the pulse clock starts inside the turn body.
* ``_until`` polls a real condition with a hard deadline. Nothing here
  sleeps for a fixed duration hoping a tick has landed, and a broken
  wire fails in milliseconds with a named condition instead of hanging.

``_until``'s ``AssertionError`` is raised inside the patched backend, so
``_dispatch``'s ``except Exception`` swallows it into a bridge-voice
error body. Success-path tests therefore call ``_assert_clean(say)``,
which turns that swallow back into a legible failure.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from unittest.mock import AsyncMock

import pytest

from tigerharness.agent_sdk.types import RunStart, ToolCall
from tigerharness.slack_bridge import bridge as bridge_mod
from tigerharness.slack_bridge.persistence import ThreadStore
from tigerharness.slack_bridge.progress import TurnProgress

# Reused rather than re-declared: two copies of a bridge fixture drift,
# and the drift is invisible until one of them stops resembling the
# constructor it fakes.
from .test_bridge_coverage2 import FakeResult, FakeSession, _make_bridge


BRIDGE_LOGGER = "tigerharness.slack_bridge.bridge"
OPS_CHANNEL = "C-OPS"

#: Pulse interval for a wired turn. Small enough that a two-tick test
#: (the stall boundary is inclusive at ``2 * interval_s``) stays inside
#: §9's ~50ms budget, large enough that a tick cannot land between two
#: adjacent awaits of the turn body.
TICK = 0.005


# ---------- Fakes ----------------------------------------------------------

class _FakeNotifier:
    """Records ``(text, channel, thread_ts)`` and returns a ts."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str | None]] = []

    def post_text(
        self,
        text: str,
        *,
        channel: str | None = None,
        thread_ts: str | None = None,
    ) -> str | None:
        self.calls.append((text, channel, thread_ts))
        return f"ts-{len(self.calls)}"

    @property
    def texts(self) -> list[str]:
        return [call[0] for call in self.calls]


class _BlockingCloserNotifier(_FakeNotifier):
    """Blocks the CLOSER post until released — never the parent.

    Keyed on the closer's own emoji rather than a call index because
    the number of pulses before it is timing-dependent. The 5s ceiling
    is a belt: the test releases in a ``finally``, but a worker thread
    parked forever on a failed assertion would outlive the suite.
    """

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()

    def post_text(
        self,
        text: str,
        *,
        channel: str | None = None,
        thread_ts: str | None = None,
    ) -> str | None:
        ts = super().post_text(text, channel=channel, thread_ts=thread_ts)
        if text.startswith((":white_check_mark:", ":x:")):
            self.release.wait(timeout=5.0)
        return ts


class _Reporter(TurnProgress):
    """A real reporter that records what the teardown did to it."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.gate = asyncio.Event()
        self.run_finished = False
        self.stop_requested = False
        self.closer: tuple[bool, str] | None = None

    def request_stop(self) -> None:
        super().request_stop()
        self.stop_requested = True

    async def run(self) -> None:
        await self.gate.wait()
        try:
            await super().run()
        finally:
            # A ``finally`` because criterion 6 asks whether the task
            # FINISHED, not whether it returned normally.
            self.run_finished = True

    async def finish(self, *, ok: bool, detail: str = "") -> None:
        # Recorded before delegating: `finish` no-ops when no parent was
        # posted, so the arguments _dispatch derived would otherwise be
        # unobservable on exactly the short turns that exercise them.
        self.closer = (ok, detail)
        await super().finish(ok=ok, detail=detail)


class _CrashingReporter(_Reporter):
    """§8 failure 3: the reporter task raises."""

    async def run(self) -> None:
        raise RuntimeError("reporter exploded")


class _StubbornReporter(_Reporter):
    """Ignores ``request_stop()``, so only the backstop can end it."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.cancelled = False

    async def run(self) -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.run_finished = True


class _SlowCloser(_Reporter):
    """The closer never returns on its own."""

    async def finish(self, *, ok: bool, detail: str = "") -> None:
        self.closer = (ok, detail)
        await asyncio.sleep(30)


# ---------- Harness --------------------------------------------------------

async def _detect(*_args: object, **_kwargs: object) -> tuple[str, float]:
    return ("alpha", 0.001)


async def _until(
    predicate: object, what: str, *, timeout: float = 3.0
) -> None:
    """Poll *predicate* until true. Never sleeps a fixed duration."""
    deadline = time.monotonic() + timeout
    while not predicate():  # type: ignore[operator]
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(TICK / 5)


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cls: type[_Reporter] = _Reporter,
    notifier: _FakeNotifier | None = None,
    interval_s: float = TICK,
) -> tuple[list[_Reporter], _FakeNotifier]:
    """Swap in a recording factory for ``bridge.build_turn_progress``.

    Builds a REAL ``TurnProgress`` subclass against a fake notifier, so
    everything between ``_dispatch`` and the Slack POST is the shipped
    code path; only the transport and the interval are substituted.
    """
    notif = notifier if notifier is not None else _FakeNotifier()
    made: list[_Reporter] = []

    def _build(header: str, **_kwargs: object) -> TurnProgress:
        reporter = cls(notif, OPS_CHANNEL, header=header, interval_s=interval_s)
        made.append(reporter)
        return reporter

    monkeypatch.setattr(bridge_mod, "build_turn_progress", _build)
    monkeypatch.setattr(bridge_mod, "detect_persona", _detect)
    return made, notif


def _bridge(tmp_path, *, session_id: str = "sess-001"):
    store = ThreadStore(tmp_path / "threads.json")
    bridge, backend = _make_bridge(store)
    backend.open_session = AsyncMock(return_value=FakeSession(id=session_id))
    return bridge, backend


def _event(text: str = "hello ops", ts: str = "100.0") -> dict[str, object]:
    return {"channel_type": "im", "user": "U0CEO", "text": text, "ts": ts}


def _assert_clean(say: AsyncMock) -> None:
    """Fail loudly when `_dispatch` swallowed a harness assertion."""
    say.assert_awaited_once()
    assert "backend error" not in say.call_args[1]["text"]


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == BRIDGE_LOGGER and record.levelno == logging.WARNING
    ]


# ---------- 3b: which string the parent quotes ------------------------------

@pytest.mark.asyncio
async def test_c3b_parent_quotes_the_operator_text_not_the_prompt(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The header is ``text``. By the time ``prompt`` exists it carries
    ``_append_bridge_context``'s block and, on a new session, the whole
    briefing/defer-test injection — all of which sanitises perfectly and
    is identical on every turn."""
    # A falsy session id makes this the first turn of a NEW session, so
    # the long injection at bridge.py:450 is really in `prompt`.
    bridge, _ = _bridge(tmp_path, session_id="")
    made, notif = _install(monkeypatch)

    async def _run(*_args: object, **_kwargs: object) -> FakeResult:
        made[0].gate.set()
        await _until(lambda: notif.calls, "the parent post")
        return FakeResult()

    monkeypatch.setattr(bridge_mod, "run_with_retry", _run)

    say = AsyncMock()
    await bridge.handle_message(_event("refresh the knowledge index"), say)

    _assert_clean(say)
    parent = notif.texts[0]
    assert "refresh the knowledge index" in parent
    assert "[bridge-context]" not in parent
    assert "briefing/README.md" not in parent
    # 4a at the wire: nothing this turn posted went anywhere but the
    # ops channel — in particular not to the DM the feature protects.
    assert {call[1] for call in notif.calls} == {OPS_CHANNEL}


# ---------- 6e(c): the persona reaches the parent ---------------------------

@pytest.mark.asyncio
async def test_c6e_dispatch_names_the_persona_on_the_posted_parent(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§4b makes the unset case render gracefully, so a `_dispatch` that
    never calls ``set_persona`` ships a green suite and an ops channel
    whose parents cannot be told apart."""
    bridge, _ = _bridge(tmp_path)
    made, notif = _install(monkeypatch)

    async def _run(*_args: object, **_kwargs: object) -> FakeResult:
        made[0].gate.set()
        await _until(lambda: notif.calls, "the parent post")
        return FakeResult()

    monkeypatch.setattr(bridge_mod, "run_with_retry", _run)

    say = AsyncMock()
    await bridge.handle_message(_event(), say)

    _assert_clean(say)
    assert notif.texts[0].startswith(":hourglass: alpha still working")


# ---------- the event tap, at the wire --------------------------------------

@pytest.mark.asyncio
async def test_wire_on_event_reaches_the_pulse_redacted(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``on_event`` is the reporter's own bound method, its effect shows
    up in a posted pulse, and the redaction survives the trip."""
    bridge, _ = _bridge(tmp_path)
    made, notif = _install(monkeypatch)
    seen: list[dict[str, object]] = []

    async def _run(*_args: object, **kwargs: object) -> FakeResult:
        seen.append(kwargs)
        made[0].gate.set()
        kwargs["on_event"](  # type: ignore[operator]
            ToolCall(
                id="t1",
                name="Read",
                arguments={
                    "file_path": "/tmp/notes.md",
                    "token": "xoxb-DEADBEEF",
                },
            )
        )
        await _until(
            lambda: any("last: Read(/tmp/notes.md)" in t for t in notif.texts),
            "a pulse naming the tool",
        )
        return FakeResult()

    monkeypatch.setattr(bridge_mod, "run_with_retry", _run)

    say = AsyncMock()
    await bridge.handle_message(_event(), say)

    _assert_clean(say)
    # `retry.py` takes bare callables and must never see a TurnProgress;
    # this asserts the binding without agent_sdk knowing the type.
    assert seen[0]["on_event"].__self__ is made[0]  # type: ignore[union-attr]
    assert seen[0]["on_retry"].__self__ is made[0]  # type: ignore[union-attr]
    pulse = next(t for t in notif.texts if "last: Read" in t)
    assert "1 tool calls" in pulse
    assert not any("xoxb-DEADBEEF" in t for t in notif.texts)


# ---------- 5 mode 4 / §8 failure 3: the reporter crashes -------------------

@pytest.mark.asyncio
async def test_c5_mode4_reporter_crash_leaves_the_turn_and_drain_intact(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    bridge, _ = _bridge(tmp_path)
    made, _notif = _install(monkeypatch, cls=_CrashingReporter)

    async def _run(*_args: object, **_kwargs: object) -> FakeResult:
        return FakeResult()

    monkeypatch.setattr(bridge_mod, "run_with_retry", _run)

    say = AsyncMock()
    with caplog.at_level(logging.WARNING, logger=BRIDGE_LOGGER):
        await bridge.handle_message(_event(), say)

    _assert_clean(say)
    assert bridge._drained.is_set()
    assert any("progress reporter crashed" in m for m in _warnings(caplog))
    # Failure 3, not 5: the two must stay distinguishable in the log.
    assert not any("did not stop" in m for m in _warnings(caplog))
    assert made[0].closer == (True, "")


# ---------- 6: the reporter task is always stopped --------------------------

@pytest.mark.asyncio
async def test_c6_reporter_task_finishes_on_the_success_path(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, _ = _bridge(tmp_path)
    made, notif = _install(monkeypatch)

    async def _run(*_args: object, **_kwargs: object) -> FakeResult:
        made[0].gate.set()
        await _until(lambda: notif.calls, "the parent post")
        return FakeResult()

    monkeypatch.setattr(bridge_mod, "run_with_retry", _run)

    say = AsyncMock()
    await bridge.handle_message(_event(), say)

    _assert_clean(say)
    assert made[0].run_finished is True
    assert bridge._drained.is_set()
    assert made[0].closer == (True, "")
    assert notif.texts[-1].startswith(":white_check_mark: done in")


@pytest.mark.asyncio
async def test_c6_reporter_task_finishes_on_the_exception_path(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """And the closer tells the truth: ``ok``/``detail`` come from the
    captured outcome, not from a hardcoded True (§7)."""
    bridge, _ = _bridge(tmp_path)
    made, notif = _install(monkeypatch)

    async def _run(*_args: object, **_kwargs: object) -> FakeResult:
        made[0].gate.set()
        await _until(lambda: notif.calls, "the parent post")
        raise RuntimeError("backend went away")

    monkeypatch.setattr(bridge_mod, "run_with_retry", _run)

    say = AsyncMock()
    await bridge.handle_message(_event(), say)

    say.assert_awaited_once()
    assert "backend error" in say.call_args[1]["text"]
    assert made[0].run_finished is True
    assert bridge._drained.is_set()
    assert made[0].closer == (False, "RuntimeError: backend went away")
    assert notif.texts[-1].startswith(":x: failed after")


@pytest.mark.asyncio
async def test_c6_a_reporter_that_ignores_the_stop_is_cancelled(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """§8 failure 5. The cooperative stop is the mechanism;
    ``wait_for(STOP_DRAIN_S)`` is the backstop, and this is the only
    test that proves the backstop exists."""
    bridge, _ = _bridge(tmp_path)
    made, _notif = _install(monkeypatch, cls=_StubbornReporter)
    monkeypatch.setattr(bridge_mod, "STOP_DRAIN_S", 0.01)

    async def _run(*_args: object, **_kwargs: object) -> FakeResult:
        return FakeResult()

    monkeypatch.setattr(bridge_mod, "run_with_retry", _run)

    say = AsyncMock()
    with caplog.at_level(logging.WARNING, logger=BRIDGE_LOGGER):
        await bridge.handle_message(_event(), say)

    _assert_clean(say)
    assert made[0].cancelled is True
    assert bridge._drained.is_set()
    assert any("did not stop within" in m for m in _warnings(caplog))
    # Failure 5, not 3 -- "would not stop" sends a reader to the budget,
    # "crashed" sends them into the pulse loop hunting a bug.
    assert not any("reporter crashed" in m for m in _warnings(caplog))


# ---------- 6a: a cancelled turn still drains -------------------------------

@pytest.mark.asyncio
async def test_c6a_cancelled_turn_stays_cancelled_and_still_drains(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All four in one test: the pre-§7 shape satisfied the first and
    failed the rest. The fourth is the closer — a cancelled turn is a
    turn that did NOT finish, and the ops channel must not say it did."""
    bridge, _ = _bridge(tmp_path)
    made, notif = _install(monkeypatch)
    never = asyncio.Event()

    async def _run(*_args: object, **_kwargs: object) -> FakeResult:
        made[0].gate.set()
        await never.wait()
        return FakeResult()

    monkeypatch.setattr(bridge_mod, "run_with_retry", _run)

    say = AsyncMock()
    task = asyncio.create_task(bridge.handle_message(_event(), say))
    await _until(lambda: notif.calls, "the parent post")
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert bridge._in_flight == 0
    assert bridge._drained.is_set()
    assert made[0].closer == (False, "CancelledError")
    assert notif.texts[-1].startswith(":x: failed after")


@pytest.mark.asyncio
async def test_c6a_a_cancel_during_the_teardown_still_reaches_the_accounting(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression §7's NESTING exists for, and the only test that
    can see it.

    A cancellation delivered at the turn body (the test above) unwinds
    into the ``finally`` and asyncio does not re-deliver it, so the
    teardown's awaits complete and the accounting is reached either
    way. Flatten the two blocks into peer statements and nothing there
    fails. It fails here: a SECOND cancel, landing while the teardown
    is parked in ``wait_for(progress_task)``, raises straight past
    ``self._in_flight -= 1``. ``_drained`` is then never set and
    ``wait_for_drain`` blocks until timeout on every SIGTERM — worse
    than the undercount bridge.py:354-358 records as already fixed
    once, because that one returned early rather than never returning.
    """
    bridge, _ = _bridge(tmp_path)
    made, notif = _install(monkeypatch, cls=_StubbornReporter)
    # A ceiling, not the mechanism: the cancel below must land first,
    # but a broken test should fail in seconds rather than in 35.
    monkeypatch.setattr(bridge_mod, "STOP_DRAIN_S", 5.0)
    never = asyncio.Event()

    async def _run(*_args: object, **_kwargs: object) -> FakeResult:
        await never.wait()
        return FakeResult()

    monkeypatch.setattr(bridge_mod, "run_with_retry", _run)

    say = AsyncMock()
    task = asyncio.create_task(bridge.handle_message(_event(), say))
    await _until(lambda: made, "the reporter to be built")
    task.cancel()
    # The reporter ignores the stop, so the teardown is now parked in
    # `wait_for(progress_task)` -- exactly where the second cancel has
    # to land for this test to mean anything.
    await _until(lambda: made[0].stop_requested, "the cooperative stop")
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert bridge._in_flight == 0
    assert bridge._drained.is_set()
    assert made[0].cancelled is True
    # No closer: this cancel propagates out of the teardown before
    # `finish` is reached. §7 records that trade-off deliberately.
    assert made[0].closer is None
    assert notif.calls == []


# ---------- 6d: the closer is bounded too -----------------------------------

@pytest.mark.asyncio
async def test_c6d_a_hung_closer_does_not_hold_the_drain_open(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """§8 failure 6. ``_drained`` is the assertion that matters: a
    closer that delays the flag is what turns a shutdown into dropped
    ``claude`` subprocesses (§7)."""
    bridge, _ = _bridge(tmp_path)
    made, notif = _install(monkeypatch, cls=_SlowCloser)
    monkeypatch.setattr(bridge_mod, "FINISH_POST_S", 0.01)

    async def _run(*_args: object, **_kwargs: object) -> FakeResult:
        made[0].gate.set()
        await _until(lambda: notif.calls, "the parent post")
        return FakeResult()

    monkeypatch.setattr(bridge_mod, "run_with_retry", _run)

    say = AsyncMock()
    with caplog.at_level(logging.WARNING, logger=BRIDGE_LOGGER):
        await bridge.handle_message(_event(), say)

    _assert_clean(say)
    assert bridge._drained.is_set()
    assert made[0].closer == (True, "")
    assert any("closer did not post within" in m for m in _warnings(caplog))


@pytest.mark.asyncio
async def test_c6d_a_blocking_closer_post_is_bounded_at_the_transport(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """The same bound, one layer lower: a real ``_post`` parked in
    ``asyncio.to_thread``. The release is in a ``finally`` so a failing
    assertion cannot leave a worker thread wedged."""
    notifier = _BlockingCloserNotifier()
    bridge, _ = _bridge(tmp_path)
    made, notif = _install(monkeypatch, notifier=notifier)
    monkeypatch.setattr(bridge_mod, "FINISH_POST_S", 0.01)

    async def _run(*_args: object, **_kwargs: object) -> FakeResult:
        made[0].gate.set()
        await _until(lambda: notif.calls, "the parent post")
        return FakeResult()

    monkeypatch.setattr(bridge_mod, "run_with_retry", _run)

    say = AsyncMock()
    try:
        with caplog.at_level(logging.WARNING, logger=BRIDGE_LOGGER):
            await bridge.handle_message(_event(), say)

        _assert_clean(say)
        assert bridge._drained.is_set()
        assert any(
            "closer did not post within" in m for m in _warnings(caplog)
        )
    finally:
        notifier.release.set()


# ---------- 6f: SIGTERM's actual path ---------------------------------------

@pytest.mark.asyncio
async def test_c6f_a_turn_in_flight_at_shutdown_reports_drained(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``request_shutdown()`` does not cancel dispatches (§1, §7), so
    THIS is the SIGTERM path, not 6a. A False here is the branch that
    drops ``claude`` subprocesses."""
    bridge, _ = _bridge(tmp_path)
    made, notif = _install(monkeypatch)

    async def _run(*_args: object, **_kwargs: object) -> FakeResult:
        made[0].gate.set()
        bridge.request_shutdown()
        await _until(lambda: notif.calls, "the parent post")
        return FakeResult()

    monkeypatch.setattr(bridge_mod, "run_with_retry", _run)

    say = AsyncMock()
    await bridge.handle_message(_event(), say)

    _assert_clean(say)
    assert await bridge.wait_for_drain(timeout=1.0) is True
    assert made[0].run_finished is True


# ---------- 7b(b): the backoff window is not a stall ------------------------

@pytest.mark.asyncio
async def test_c7b_backoff_renders_retrying_and_reverts_after_run_start(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, _ = _bridge(tmp_path)
    made, notif = _install(monkeypatch)

    async def _run(*_args: object, **kwargs: object) -> FakeResult:
        made[0].gate.set()
        kwargs["on_retry"](1)  # type: ignore[operator]
        await _until(
            lambda: any("retrying (attempt 2)" in t for t in notif.texts),
            "a pulse inside the backoff window",
        )
        marker = len(notif.calls)
        kwargs["on_event"](  # type: ignore[operator]
            RunStart(session_id="s-1", model="m-1")
        )
        await _until(
            lambda: len(notif.calls) > marker,
            "a pulse after the retry cleared",
        )
        return FakeResult()

    monkeypatch.setattr(bridge_mod, "run_with_retry", _run)

    say = AsyncMock()
    await bridge.handle_message(_event(), say)

    _assert_clean(say)
    backoff = next(t for t in notif.texts if "retrying (attempt 2)" in t)
    assert "no activity" not in backoff
    after = [t for t in notif.texts if t.startswith("0m")]
    assert "retrying" not in after[-1]


# ---------- 7c: the compaction window, both halves --------------------------

@pytest.mark.asyncio
async def test_c7c_dispatch_marks_the_idle_compact_window(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wire half. ``compacting()`` must sit INSIDE ``_compact_turn``
    — the callable ``maybe_compact`` invokes only when it really
    compacts — so the patched hook here invokes it rather than merely
    returning True."""
    bridge, _ = _bridge(tmp_path)
    made, notif = _install(monkeypatch)
    invoked: list[str] = []

    async def _run(*_args: object, **kwargs: object) -> FakeResult:
        made[0].gate.set()
        if "idle-compact" in str(kwargs.get("label", "")):
            return FakeResult()
        await _until(lambda: notif.calls, "the parent post")
        return FakeResult()

    async def _maybe_compact(
        send_turn, cfg, usage, *, already_compacted: bool, label: str = ""
    ) -> bool:
        invoked.append(label)
        await send_turn("/compact")
        await _until(
            lambda: any("compacting session" in t for t in notif.texts),
            "a pulse inside the compaction window",
        )
        return True

    monkeypatch.setattr(bridge_mod, "run_with_retry", _run)
    monkeypatch.setattr(bridge_mod, "maybe_compact", _maybe_compact)

    say = AsyncMock()
    await bridge.handle_message(_event(), say)

    _assert_clean(say)
    assert invoked, "maybe_compact was never reached"
    marked = next(t for t in notif.texts if "compacting session" in t)
    assert "no activity" not in marked


@pytest.mark.asyncio
async def test_c7c_a_turn_without_compaction_still_reports_a_stall(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative half, and what makes the other two mean anything:
    ``compacting`` never clears, so marking the window one call site too
    early would disable stall detection for the rest of every turn.

    The pulses asserted on are fired from INSIDE the no-op
    ``maybe_compact`` window, which is the only place the §3.6 bug is
    visible: a ``compacting()`` hoisted out of ``_compact_turn`` runs
    here, on a turn that is not compacting at all. Asserted from the
    turn body instead, this test passes against the hoisted version —
    the pulses would all pre-date the bad call site."""
    bridge, _ = _bridge(tmp_path)
    made, notif = _install(monkeypatch)

    async def _run(*_args: object, **_kwargs: object) -> FakeResult:
        made[0].gate.set()
        return FakeResult()

    async def _maybe_compact(
        send_turn, cfg, usage, *, already_compacted: bool, label: str = ""
    ) -> bool:
        # The gate said no. Hold the window open past `2 * interval_s`
        # of silence so the stall warning has to render.
        await _until(
            lambda: any("no activity" in t for t in notif.texts),
            "a stall pulse while maybe_compact declines",
        )
        return False

    monkeypatch.setattr(bridge_mod, "run_with_retry", _run)
    monkeypatch.setattr(bridge_mod, "maybe_compact", _maybe_compact)

    say = AsyncMock()
    await bridge.handle_message(_event(), say)

    _assert_clean(say)
    stall = next(t for t in notif.texts if "no activity" in t)
    assert stall.endswith(":warning:")
    assert not any("compacting" in t for t in notif.texts)

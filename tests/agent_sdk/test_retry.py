"""Tests for ``agent_sdk.retry.run_with_retry``."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tigerharness.agent_sdk import AgentConfig, run_with_retry
from tests.agent_sdk._helpers import asyncio_test
from tigerharness.agent_sdk.errors import StreamNotConsumedError
from tigerharness.agent_sdk.types import (
    Event,
    RunDone,
    RunResult,
    RunStart,
    ToolCall,
)


# ---------- Fakes ------------------------------------------------------------

class _FakeBackend:
    """Fails the first ``fail_n`` calls with the given exception, then
    succeeds. Records every call so tests can verify retry counts.
    """

    def __init__(self, *, fail_n: int = 0, exc: Exception | None = None) -> None:
        self.fail_n = fail_n
        self.exc = exc or RuntimeError("transient")
        self.call_count = 0

    async def run(
        self,
        config: AgentConfig,
        prompt: Any,
        *,
        session: Any = None,
        approval: Any = None,
    ) -> RunResult:
        self.call_count += 1
        if self.call_count <= self.fail_n:
            raise self.exc
        return RunResult(
            final_output=f"ok-after-{self.call_count}",
            transcript=[],
            stop_reason="end_turn",
            usage=None,
            cost_usd=0.0,
            raw=None,
        )


CFG = AgentConfig(name="t")


# ---------- Tests ------------------------------------------------------------

@asyncio_test
async def test_success_on_first_attempt_no_retries() -> None:
    backend = _FakeBackend(fail_n=0)
    result = await run_with_retry(backend, CFG, "hi", max_attempts=3, base_delay_s=0)
    assert result.final_output == "ok-after-1"
    assert backend.call_count == 1


@asyncio_test
async def test_recovers_on_second_attempt() -> None:
    backend = _FakeBackend(fail_n=1)
    result = await run_with_retry(backend, CFG, "hi", max_attempts=3, base_delay_s=0)
    assert result.final_output == "ok-after-2"
    assert backend.call_count == 2


@asyncio_test
async def test_recovers_on_third_attempt() -> None:
    backend = _FakeBackend(fail_n=2)
    result = await run_with_retry(backend, CFG, "hi", max_attempts=3, base_delay_s=0)
    assert result.final_output == "ok-after-3"
    assert backend.call_count == 3


@asyncio_test
async def test_raises_after_exhausting_all_attempts() -> None:
    backend = _FakeBackend(fail_n=10, exc=RuntimeError("kaboom"))
    with pytest.raises(RuntimeError, match="kaboom"):
        await run_with_retry(backend, CFG, "hi", max_attempts=3, base_delay_s=0)
    assert backend.call_count == 3


@asyncio_test
async def test_cancellation_propagates_without_retry() -> None:
    class _CancelBackend:
        def __init__(self) -> None:
            self.call_count = 0

        async def run(self, *a: Any, **k: Any) -> RunResult:
            self.call_count += 1
            raise asyncio.CancelledError("user-cancelled")

    backend = _CancelBackend()
    with pytest.raises(asyncio.CancelledError):
        await run_with_retry(backend, CFG, "hi", max_attempts=3, base_delay_s=0)  # type: ignore[arg-type]
    # Critically: only ONE attempt, not three. Cancellation must not be
    # absorbed by the retry loop.
    assert backend.call_count == 1


@asyncio_test
async def test_zero_attempts_is_rejected() -> None:
    backend = _FakeBackend()
    with pytest.raises(ValueError, match="max_attempts"):
        await run_with_retry(backend, CFG, "hi", max_attempts=0, base_delay_s=0)


@asyncio_test
async def test_single_attempt_mode_no_retry_loop() -> None:
    """max_attempts=1 should still surface the exception cleanly — the
    helper degenerates to a plain backend.run() in this case."""
    backend = _FakeBackend(fail_n=1, exc=ValueError("bang"))
    with pytest.raises(ValueError, match="bang"):
        await run_with_retry(backend, CFG, "hi", max_attempts=1, base_delay_s=0)
    assert backend.call_count == 1


@asyncio_test
async def test_backoff_actually_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Confirm exponential backoff scheduling: 2 retries → 2 sleeps
    of base_delay × 1 and base_delay × 2."""
    sleeps: list[float] = []

    async def _fake_sleep(d: float) -> None:
        sleeps.append(d)

    monkeypatch.setattr("tigerharness.agent_sdk.retry.asyncio.sleep", _fake_sleep)

    backend = _FakeBackend(fail_n=2)
    await run_with_retry(backend, CFG, "hi", max_attempts=3, base_delay_s=0.5)
    assert sleeps == [0.5, 1.0]  # 0.5×1, 0.5×2


@asyncio_test
async def test_session_and_approval_are_forwarded() -> None:
    """Make sure we don't accidentally swallow keyword args."""
    seen: dict[str, Any] = {}

    class _RecordingBackend:
        async def run(self, config, prompt, *, session=None, approval=None):
            seen["session"] = session
            seen["approval"] = approval
            return RunResult(
                final_output="x", transcript=[], stop_reason="end_turn",
                usage=None, cost_usd=0.0, raw=None,
            )

    fake_session = object()
    fake_approval = object()
    await run_with_retry(
        _RecordingBackend(), CFG, "hi",  # type: ignore[arg-type]
        session=fake_session,  # type: ignore[arg-type]
        approval=fake_approval,  # type: ignore[arg-type]
        max_attempts=2,
        base_delay_s=0,
    )
    assert seen["session"] is fake_session
    assert seen["approval"] is fake_approval


@asyncio_test
async def test_label_appears_in_log_messages(caplog: pytest.LogCaptureFixture) -> None:
    backend = _FakeBackend(fail_n=1)
    with caplog.at_level("INFO", logger="tigerharness.agent_sdk.retry"):
        await run_with_retry(
            backend, CFG, "hi",
            max_attempts=3, base_delay_s=0, label="thread=T123",
        )
    # The label should appear in the retry log line.
    retry_records = [r for r in caplog.records if "retrying" in r.message]
    assert retry_records, "expected at least one retry log line"
    assert any("thread=T123" in r.message for r in retry_records)


# ---------- The progress tap (on_event / on_retry) ---------------------------
#
# `slack_bridge.progress` needs to see events mid-turn and to be told
# when a retry is scheduled. The tap must be inert for every existing
# caller, and neither callback may break or duplicate a turn.

_EVENTS: tuple[Event, ...] = (
    RunStart(session_id="s1", model="m"),
    ToolCall(id="t1", name="Read", arguments={"file_path": "a.py"}),
    RunDone(
        final_output="done", stop_reason="end_turn", usage=None,
        cost_usd=0.0,
    ),
)


class _FakeHandle:
    """A StreamHandle faithful on the two contracts retry.py relies on:
    ``result`` raises until the iterator is exhausted, and ``__aexit__``
    always runs.
    """

    def __init__(
        self,
        events: tuple[Event, ...],
        result: RunResult,
        *,
        exc: Exception | None = None,
    ) -> None:
        self._events = list(events)
        self._result = result
        self._exc = exc
        self._i = 0
        self._complete = False
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> "_FakeHandle":
        self.entered = True
        return self

    async def __aexit__(self, *a: Any) -> None:
        self.exited = True

    def __aiter__(self) -> "_FakeHandle":
        return self

    async def __anext__(self) -> Event:
        if self._i >= len(self._events):
            if self._exc is not None:
                raise self._exc
            self._complete = True
            raise StopAsyncIteration
        event = self._events[self._i]
        self._i += 1
        return event

    @property
    def result(self) -> RunResult:
        if not self._complete:
            raise StreamNotConsumedError("not consumed")
        return self._result


class _StreamBackend:
    """Records which path the caller took: ``run`` or ``run_stream``."""

    def __init__(
        self,
        *,
        fail_n: int = 0,
        exc: Exception | None = None,
        events: tuple[Event, ...] = _EVENTS,
    ) -> None:
        self.fail_n = fail_n
        self.exc = exc or RuntimeError("transient")
        self.events = events
        self.run_calls = 0
        self.stream_calls = 0
        self.handles: list[_FakeHandle] = []

    async def run(self, *a: Any, **k: Any) -> RunResult:
        self.run_calls += 1
        return _result(f"run-{self.run_calls}")

    def run_stream(self, *a: Any, **k: Any) -> _FakeHandle:
        self.stream_calls += 1
        fail = self.stream_calls <= self.fail_n
        handle = _FakeHandle(
            self.events,
            _result(f"stream-{self.stream_calls}"),
            exc=self.exc if fail else None,
        )
        self.handles.append(handle)
        return handle


def _result(text: str) -> RunResult:
    return RunResult(
        final_output=text, transcript=[], stop_reason="end_turn",
        usage=None, cost_usd=0.0, raw=None,
    )


@asyncio_test
async def test_no_callbacks_leaves_the_plain_run_path_untouched() -> None:
    """Existing callers are unchanged by construction, not by assertion:
    with no tap, ``run_stream`` is never even built."""
    backend = _StreamBackend()
    result = await run_with_retry(
        backend,  # type: ignore[arg-type]
        CFG, "hi", max_attempts=3, base_delay_s=0,
    )
    assert result.final_output == "run-1"
    assert backend.run_calls == 1
    assert backend.stream_calls == 0


@asyncio_test
async def test_on_event_switches_to_the_stream_path_and_taps_events() -> None:
    backend = _StreamBackend()
    seen: list[Event] = []
    result = await run_with_retry(
        backend, CFG, "hi",  # type: ignore[arg-type]
        max_attempts=3, base_delay_s=0, on_event=seen.append,
    )
    assert result.final_output == "stream-1"
    assert backend.run_calls == 0
    assert seen == list(_EVENTS)


@asyncio_test
async def test_stream_path_result_is_read_after_full_consumption() -> None:
    """``.result`` raises until the iterator is exhausted, so a helper
    that read it early would fail here rather than in production."""
    backend = _StreamBackend()
    await run_with_retry(
        backend, CFG, "hi",  # type: ignore[arg-type]
        max_attempts=3, base_delay_s=0, on_event=lambda e: None,
    )
    assert backend.handles[0].entered
    assert backend.handles[0].exited


@asyncio_test
async def test_stream_path_cleans_up_the_handle_on_failure() -> None:
    """The ``async with`` is the whole reason the tap is safe: a mid-turn
    blow-up must still run backend cleanup before the retry."""
    backend = _StreamBackend(fail_n=1)
    await run_with_retry(
        backend, CFG, "hi",  # type: ignore[arg-type]
        max_attempts=3, base_delay_s=0, on_event=lambda e: None,
    )
    assert backend.stream_calls == 2
    assert all(h.exited for h in backend.handles)


@asyncio_test
async def test_on_retry_fires_once_per_retry_with_the_failed_attempt() -> None:
    backend = _StreamBackend(fail_n=2)
    attempts: list[int] = []
    await run_with_retry(
        backend, CFG, "hi",  # type: ignore[arg-type]
        max_attempts=3, base_delay_s=0,
        on_event=lambda e: None, on_retry=attempts.append,
    )
    assert attempts == [1, 2]


@asyncio_test
async def test_on_retry_fires_before_the_backoff_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordering is the point: called after the sleep, the notification
    would only describe a window that has already closed, and a progress
    reporter would render a scheduled wait as a hang."""
    order: list[str] = []

    async def _fake_sleep(d: float) -> None:
        order.append("sleep")

    monkeypatch.setattr(
        "tigerharness.agent_sdk.retry.asyncio.sleep", _fake_sleep
    )
    backend = _FakeBackend(fail_n=1)
    await run_with_retry(
        backend, CFG, "hi", max_attempts=3, base_delay_s=1,
        on_retry=lambda n: order.append(f"retry-{n}"),
    )
    assert order == ["retry-1", "sleep"]


@asyncio_test
async def test_on_retry_without_on_event_keeps_the_plain_run_path() -> None:
    """The two taps are independent: a caller may want retry notices
    without paying for the stream path."""
    backend = _StreamBackend(fail_n=1)
    attempts: list[int] = []

    async def _run(*a: Any, **k: Any) -> RunResult:
        backend.run_calls += 1
        if backend.run_calls == 1:
            raise RuntimeError("transient")
        return _result("run-2")

    backend.run = _run  # type: ignore[method-assign]
    result = await run_with_retry(
        backend, CFG, "hi",  # type: ignore[arg-type]
        max_attempts=3, base_delay_s=0, on_retry=attempts.append,
    )
    assert result.final_output == "run-2"
    assert backend.stream_calls == 0
    assert attempts == [1]


@asyncio_test
async def test_raising_on_event_does_not_retry_the_turn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unguarded, the raise would be caught by the general handler and
    trigger a full re-run — a progress callback silently causing a
    duplicate agent run."""
    backend = _StreamBackend()

    def _boom(event: Event) -> None:
        raise RuntimeError("callback exploded")

    with caplog.at_level("WARNING", logger="tigerharness.agent_sdk.retry"):
        result = await run_with_retry(
            backend, CFG, "hi",  # type: ignore[arg-type]
            max_attempts=3, base_delay_s=0, on_event=_boom,
        )
    assert result.final_output == "stream-1"
    assert backend.stream_calls == 1
    assert any("on_event callback raised" in r.getMessage()
               for r in caplog.records)


@asyncio_test
async def test_raising_on_retry_does_not_replace_the_backend_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``on_retry`` fires from inside the ``except`` handler, so an
    unguarded traceback would REPLACE the exception that broke the
    turn."""
    backend = _FakeBackend(fail_n=10, exc=RuntimeError("kaboom"))

    def _boom(attempt: int) -> None:
        raise ValueError("callback exploded")

    with caplog.at_level("WARNING", logger="tigerharness.agent_sdk.retry"):
        with pytest.raises(RuntimeError, match="kaboom"):
            await run_with_retry(
                backend, CFG, "hi",
                max_attempts=3, base_delay_s=0, on_retry=_boom,
            )
    assert backend.call_count == 3
    assert any("on_retry callback raised" in r.getMessage()
               for r in caplog.records)


@asyncio_test
async def test_cancellation_on_the_stream_path_propagates_without_retry(
) -> None:
    backend = _StreamBackend(
        fail_n=1, exc=asyncio.CancelledError("user-cancelled"),
    )
    with pytest.raises(asyncio.CancelledError):
        await run_with_retry(
            backend, CFG, "hi",  # type: ignore[arg-type]
            max_attempts=3, base_delay_s=0, on_event=lambda e: None,
        )
    assert backend.stream_calls == 1
    assert backend.handles[0].exited

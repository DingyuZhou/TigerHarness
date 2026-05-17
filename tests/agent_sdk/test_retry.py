"""Tests for ``agent_sdk.retry.run_with_retry``."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tigerharness.agent_sdk import AgentConfig, run_with_retry
from tests.agent_sdk._helpers import asyncio_test
from tigerharness.agent_sdk.types import RunResult


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

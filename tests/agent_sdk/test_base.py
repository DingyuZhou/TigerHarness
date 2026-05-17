"""Tests for ``agent_sdk.backends._base``."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from tigerharness.agent_sdk import (
    Event,
    NormalizedMessage,
    RunDone,
    RunResult,
    RunStart,
    StreamNotConsumedError,
)
from tigerharness.agent_sdk.backends._base import BaseStreamHandle, run_via_stream
from tests.agent_sdk._helpers import asyncio_test


# ---------- A toy stream handle for exercising BaseStreamHandle --------------


class _ToyHandle(BaseStreamHandle):
    """Yields RunStart, RunDone(end_turn) without spawning anything."""

    def __init__(
        self,
        *,
        n_events: int = 1,
        raise_on_cancel: bool = False,
        cancel_exc: type[BaseException] = RuntimeError,
    ) -> None:
        super().__init__()
        self._n_events = n_events
        self._cancelled = False
        self._raise_on_cancel = raise_on_cancel
        self._cancel_exc = cancel_exc
        self._start(self._iter())

    async def cancel(self, *, after_turn: bool = False) -> None:
        if self._raise_on_cancel:
            raise self._cancel_exc("cancel boom")
        self._cancelled = True

    async def _iter(self) -> AsyncIterator[Event]:
        try:
            yield RunStart(session_id="sess", model="model")
            for i in range(self._n_events - 1):
                # Allow a chance to be cancelled between yields.
                await asyncio.sleep(0)
                yield RunStart(session_id=f"chunk-{i}", model="model")
        finally:
            pass
        # Build a final result so consume-to-end works.
        self._result = RunResult(
            final_output="ok",
            transcript=[NormalizedMessage(role="user",
                                          content=[]),
                        NormalizedMessage(role="assistant",
                                          content=[])],
            stop_reason="interrupted" if self._cancelled else "end_turn",
            usage=None, cost_usd=None,
        )
        yield RunDone(
            final_output="ok",
            stop_reason="interrupted" if self._cancelled else "end_turn",
            usage=None, cost_usd=None,
        )


class TestBaseStreamHandle:
    @asyncio_test
    async def test_consume_to_end_populates_result(self) -> None:
        handle = _ToyHandle()
        events = []
        async for ev in handle:
            events.append(type(ev).__name__)
        assert events == ["RunStart", "RunDone"]
        assert handle.is_complete is True
        assert handle.result.final_output == "ok"
        assert handle.result.stop_reason == "end_turn"

    @asyncio_test
    async def test_result_before_completion_raises(self) -> None:
        handle = _ToyHandle()
        with pytest.raises(StreamNotConsumedError):
            _ = handle.result
        assert handle.is_complete is False
        # Drain so the test doesn't leak.
        async for _ in handle:
            pass

    @asyncio_test
    async def test_async_with_completes_normally(self) -> None:
        async with _ToyHandle() as handle:
            async for _ in handle:
                pass
            assert handle.result.final_output == "ok"

    @asyncio_test
    async def test_async_with_break_runs_aexit(self) -> None:
        handle = _ToyHandle(n_events=10)
        async with handle:
            async for ev in handle:
                if isinstance(ev, RunStart):
                    break  # exit early
        # After __aexit__, cancel was called and the generator was aclose()'d.
        assert handle._cancelled is True

    @asyncio_test
    async def test_async_with_swallows_cancel_errors(self) -> None:
        # A backend that raises in cancel() must not break __aexit__.
        async with _ToyHandle(n_events=10, raise_on_cancel=True) as handle:
            async for ev in handle:
                if isinstance(ev, RunStart):
                    break

    @asyncio_test
    async def test_async_with_swallows_notimplemented_cancel(self) -> None:
        # cancel() raising NotImplementedError is the expected case for
        # backends that don't support cancellation. __aexit__ must tolerate it.
        async with _ToyHandle(
            n_events=10,
            raise_on_cancel=True,
            cancel_exc=NotImplementedError,
        ) as handle:
            async for ev in handle:
                if isinstance(ev, RunStart):
                    break

    @asyncio_test
    async def test_async_with_swallows_aclose_errors(self) -> None:
        # If the underlying generator's aclose() blows up, __aexit__
        # still completes cleanly.
        class _BrokenGen:
            async def aclose(self) -> None:
                raise RuntimeError("aclose boom")

            def __aiter__(self): return self
            async def __anext__(self):
                raise StopAsyncIteration

        handle = _ToyHandle()
        # Replace the working generator with one whose aclose raises.
        handle._gen = _BrokenGen()  # type: ignore[assignment]
        async with handle:
            pass

    @asyncio_test
    async def test_unstarted_handle_raises(self) -> None:
        # Using the base class directly without _start() must error clearly.
        bare = BaseStreamHandle()
        with pytest.raises(RuntimeError, match="not started"):
            await bare.__anext__()

    @asyncio_test
    async def test_cancel_default_raises_notimplemented(self) -> None:
        bare = BaseStreamHandle()
        with pytest.raises(NotImplementedError):
            await bare.cancel()


# ---------- run_via_stream helper --------------------------------------------


class TestRunViaStream:
    @asyncio_test
    async def test_drains_and_returns_result(self) -> None:
        handle = _ToyHandle(n_events=3)
        result = await run_via_stream(handle)
        assert isinstance(result, RunResult)
        assert result.final_output == "ok"
        assert handle.is_complete is True

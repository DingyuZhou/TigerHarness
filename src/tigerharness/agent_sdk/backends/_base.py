"""Helpers shared by backend implementations."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ..errors import StreamNotConsumedError
from ..types import Event, RunResult, StreamHandle


class BaseStreamHandle:
    """Convenience base class for backends.

    Subclasses implement ``_iter()`` as an ``async`` generator that yields
    Events and, before returning, sets ``self._result`` to a populated
    ``RunResult``. The base class wires up ``__aiter__`` / ``__anext__``,
    ``.result`` and ``.is_complete`` for free.
    """

    def __init__(self) -> None:
        self._result: RunResult | None = None
        self._gen: AsyncIterator[Event] | None = None

    def _start(self, gen: AsyncIterator[Event]) -> None:
        self._gen = gen

    def __aiter__(self) -> "BaseStreamHandle":
        return self

    async def __anext__(self) -> Event:
        if self._gen is None:
            raise RuntimeError("Stream not started; subclass forgot to call _start().")
        return await self._gen.__anext__()

    async def __aenter__(self) -> "BaseStreamHandle":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        # Best-effort cleanup: ask the backend to cancel, then close the
        # underlying generator so its `finally` block reaps any subprocess.
        if not self.is_complete:
            try:
                await self.cancel()
            except NotImplementedError:
                pass
            except Exception:
                pass
        if self._gen is not None and hasattr(self._gen, "aclose"):
            try:
                await self._gen.aclose()  # type: ignore[union-attr]
            except Exception:
                pass

    @property
    def result(self) -> RunResult:
        if self._result is None:
            raise StreamNotConsumedError(
                "Stream has not been fully consumed yet. Iterate to completion "
                "first, or read .result inside the `async with` block after "
                "finishing the loop."
            )
        return self._result

    @property
    def is_complete(self) -> bool:
        return self._result is not None

    async def cancel(self, *, after_turn: bool = False) -> None:  # pragma: no cover
        raise NotImplementedError("This backend does not support cancellation.")


async def run_via_stream(handle: StreamHandle) -> RunResult:
    """Drain a stream handle and return its final RunResult.

    Backends usually implement ``run()`` as ``return await
    run_via_stream(self.run_stream(...))`` so the streaming and non-streaming
    paths share one code path.
    """
    async for _ in handle:
        pass
    return handle.result

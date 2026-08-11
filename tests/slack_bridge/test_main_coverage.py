"""Coverage-push tests for slack_bridge/__main__.py lifecycle edges not
reached by test_main_multi.py: `_drive_handlers` without lane_names and
the close_async-raises cleanup path.
"""
from __future__ import annotations

import asyncio
import os
import signal as sig_mod
from unittest.mock import AsyncMock, MagicMock

import pytest

from tigerharness.slack_bridge.__main__ import _drive_handlers


def _mock_bridge() -> MagicMock:
    b = MagicMock()
    b.app = MagicMock()
    b.request_shutdown = MagicMock()
    b.wait_for_drain = AsyncMock(return_value=True)
    return b


def _mock_handler() -> AsyncMock:
    h = AsyncMock()

    async def blocking_start():
        await asyncio.sleep(60)

    h.start_async = blocking_start
    h.close_async = AsyncMock()
    return h


async def _sigterm_soon():
    await asyncio.sleep(0.05)
    os.kill(os.getpid(), sig_mod.SIGTERM)


class TestDriveHandlersWithoutLaneNames:
    @pytest.mark.asyncio
    async def test_drains_and_closes_without_lane_names(self):
        """lane_names is optional (embedder-style call): the no-name branch
        must still drain and close cleanly on SIGTERM."""
        bridge = _mock_bridge()
        handler = _mock_handler()

        sig_task = asyncio.create_task(_sigterm_soon())
        await _drive_handlers([handler], [bridge])
        await sig_task

        bridge.request_shutdown.assert_called_once()
        handler.close_async.assert_awaited_once()


class TestCloseAsyncFailure:
    @pytest.mark.asyncio
    async def test_close_async_exception_swallowed(self):
        """handler.close_async raising is logged, not fatal -- shutdown
        still completes."""
        bridge = _mock_bridge()
        handler = _mock_handler()
        handler.close_async = AsyncMock(side_effect=RuntimeError("close fail"))

        sig_task = asyncio.create_task(_sigterm_soon())
        await _drive_handlers([handler], [bridge], lane_names=["shohoku"])
        await sig_task

        bridge.request_shutdown.assert_called_once()

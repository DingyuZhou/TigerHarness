"""Coverage-push tests for slack_bridge/__main__.py — targeting lines 37-48, 53, 89.

The graceful shutdown flow involves:
  - _graceful_shutdown async function (lines 37-48)
  - _on_signal calling _graceful_shutdown (line 53)
  - __name__ == "__main__" guard (line 89)
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestGracefulShutdownViaSIGTERM:
    """Lines 37-48, 53: exercise the actual signal→_graceful_shutdown flow.

    We mock load/build_bridge/AsyncSocketModeHandler but let _run() set
    up real signal handlers. Then we send SIGTERM which fires
    _on_signal→_graceful_shutdown→drain→close→shutdown_complete.set().
    """

    @pytest.mark.asyncio
    async def test_sigterm_triggers_graceful_shutdown_drained(self):
        """Lines 37-41: drain succeeds."""
        import os
        import signal as sig_mod
        from tigerharness.slack_bridge.__main__ import _run

        mock_cfg = MagicMock()
        mock_cfg.slack_app_token = "xapp-test"
        mock_cfg.agent_cwd = "/tmp"
        mock_cfg.allowed_user_ids = {"U123"}

        mock_bridge = MagicMock()
        mock_bridge.app = MagicMock()
        mock_bridge.request_shutdown = MagicMock()
        mock_bridge.wait_for_drain = AsyncMock(return_value=True)

        mock_handler = AsyncMock()
        mock_handler.close_async = AsyncMock()

        # start_async blocks forever; SIGTERM will interrupt via shutdown
        async def blocking_start():
            await asyncio.sleep(60)

        mock_handler.start_async = blocking_start

        with patch("tigerharness.slack_bridge.__main__.load", return_value=mock_cfg), \
             patch("tigerharness.slack_bridge.__main__.build_bridge", return_value=mock_bridge), \
             patch("tigerharness.slack_bridge.__main__.AsyncSocketModeHandler", return_value=mock_handler):

            async def send_signal_soon():
                await asyncio.sleep(0.05)
                os.kill(os.getpid(), sig_mod.SIGTERM)

            signal_task = asyncio.create_task(send_signal_soon())
            await _run()
            await signal_task

        mock_bridge.request_shutdown.assert_called_once()
        mock_handler.close_async.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sigterm_drain_timeout(self):
        """Lines 42-43: drain times out."""
        import os
        import signal as sig_mod
        from tigerharness.slack_bridge.__main__ import _run

        mock_cfg = MagicMock()
        mock_cfg.slack_app_token = "xapp-test"
        mock_cfg.agent_cwd = "/tmp"
        mock_cfg.allowed_user_ids = {"U123"}

        mock_bridge = MagicMock()
        mock_bridge.app = MagicMock()
        mock_bridge.request_shutdown = MagicMock()
        mock_bridge.wait_for_drain = AsyncMock(return_value=False)

        mock_handler = AsyncMock()
        mock_handler.close_async = AsyncMock()

        async def blocking_start():
            await asyncio.sleep(60)

        mock_handler.start_async = blocking_start

        with patch("tigerharness.slack_bridge.__main__.load", return_value=mock_cfg), \
             patch("tigerharness.slack_bridge.__main__.build_bridge", return_value=mock_bridge), \
             patch("tigerharness.slack_bridge.__main__.AsyncSocketModeHandler", return_value=mock_handler):

            async def send_signal_soon():
                await asyncio.sleep(0.05)
                os.kill(os.getpid(), sig_mod.SIGTERM)

            signal_task = asyncio.create_task(send_signal_soon())
            await _run()
            await signal_task

        mock_bridge.wait_for_drain.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_async_exception_swallowed(self):
        """Lines 46-47: handler.close_async raising is logged, not fatal."""
        import os
        import signal as sig_mod
        from tigerharness.slack_bridge.__main__ import _run

        mock_cfg = MagicMock()
        mock_cfg.slack_app_token = "xapp-test"
        mock_cfg.agent_cwd = "/tmp"
        mock_cfg.allowed_user_ids = {"U123"}

        mock_bridge = MagicMock()
        mock_bridge.app = MagicMock()
        mock_bridge.request_shutdown = MagicMock()
        mock_bridge.wait_for_drain = AsyncMock(return_value=True)

        mock_handler = AsyncMock()
        mock_handler.close_async = AsyncMock(side_effect=RuntimeError("close fail"))

        async def blocking_start():
            await asyncio.sleep(60)

        mock_handler.start_async = blocking_start

        with patch("tigerharness.slack_bridge.__main__.load", return_value=mock_cfg), \
             patch("tigerharness.slack_bridge.__main__.build_bridge", return_value=mock_bridge), \
             patch("tigerharness.slack_bridge.__main__.AsyncSocketModeHandler", return_value=mock_handler):

            async def send_signal_soon():
                await asyncio.sleep(0.05)
                os.kill(os.getpid(), sig_mod.SIGTERM)

            signal_task = asyncio.create_task(send_signal_soon())
            await _run()  # should complete without raising
            await signal_task


class TestMainFunction:
    """Test main() wraps _run with asyncio.run and handles KeyboardInterrupt."""

    def test_main_keyboard_interrupt(self):
        from tigerharness.slack_bridge.__main__ import main

        with patch("tigerharness.slack_bridge.__main__.asyncio.run",
                   side_effect=KeyboardInterrupt()):
            # Should not raise
            main()

    def test_main_normal(self):
        from tigerharness.slack_bridge.__main__ import main

        with patch("tigerharness.slack_bridge.__main__.asyncio.run"):
            main()

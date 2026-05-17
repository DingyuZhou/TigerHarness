"""Additional __main__ tests — graceful shutdown, KeyboardInterrupt."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tigerharness.slack_bridge.__main__ import _run, main, _DRAIN_TIMEOUT_S


class TestGracefulShutdown:
    @pytest.mark.asyncio
    async def test_graceful_shutdown_flow(self):
        """Test _graceful_shutdown when signal fires."""
        mock_cfg = MagicMock()
        mock_cfg.slack_app_token = "xapp-test"
        mock_cfg.agent_cwd = "/tmp"
        mock_cfg.allowed_user_ids = {"U123"}

        mock_bridge = MagicMock()
        mock_bridge.app = MagicMock()
        mock_bridge.request_shutdown = MagicMock()
        mock_bridge.wait_for_drain = AsyncMock(return_value=True)

        mock_handler = MagicMock()
        mock_handler.start_async = AsyncMock()
        mock_handler.close_async = AsyncMock()

        with patch("tigerharness.slack_bridge.__main__.load", return_value=mock_cfg):
            with patch("tigerharness.slack_bridge.__main__.build_bridge", return_value=mock_bridge):
                with patch("tigerharness.slack_bridge.__main__.AsyncSocketModeHandler", return_value=mock_handler):
                    await _run()

    @pytest.mark.asyncio
    async def test_drain_timeout_path(self):
        """Test when drain times out."""
        mock_cfg = MagicMock()
        mock_cfg.slack_app_token = "xapp-test"
        mock_cfg.agent_cwd = "/tmp"
        mock_cfg.allowed_user_ids = {"U123"}

        mock_bridge = MagicMock()
        mock_bridge.app = MagicMock()
        mock_bridge.request_shutdown = MagicMock()
        mock_bridge.wait_for_drain = AsyncMock(return_value=False)

        mock_handler = MagicMock()
        mock_handler.start_async = AsyncMock()
        mock_handler.close_async = AsyncMock()

        with patch("tigerharness.slack_bridge.__main__.load", return_value=mock_cfg):
            with patch("tigerharness.slack_bridge.__main__.build_bridge", return_value=mock_bridge):
                with patch("tigerharness.slack_bridge.__main__.AsyncSocketModeHandler", return_value=mock_handler):
                    await _run()


class TestMainKeyboardInterrupt:
    def test_keyboard_interrupt_handled(self):
        with patch("tigerharness.slack_bridge.__main__.asyncio.run",
                    side_effect=KeyboardInterrupt):
            main()  # Should not raise

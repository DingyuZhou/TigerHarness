"""Tests for __main__.py entrypoints (task_runner and slack_bridge)."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestTaskRunnerMain:
    def test_module_exists(self):
        spec = importlib.util.find_spec("tigerharness.task_runner.__main__")
        assert spec is not None

    def test_runs_as_subprocess(self):
        """Verify `python -m tigerharness.task_runner` runs without import errors."""
        result = subprocess.run(
            [sys.executable, "-m", "tigerharness.task_runner", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        # --help should exit 0 and show usage
        assert result.returncode == 0
        assert "usage" in result.stdout.lower() or "tigerharness" in result.stdout.lower()


class TestSlackBridgeMain:
    def test_module_exists(self):
        spec = importlib.util.find_spec("tigerharness.slack_bridge.__main__")
        assert spec is not None

    def test_main_function_exists(self):
        """Import the main function without triggering asyncio.run."""
        # We can't import __main__ directly (it would call main()),
        # but we can verify the module structure.
        from tigerharness.slack_bridge import __main__ as mod
        assert hasattr(mod, "main")
        assert hasattr(mod, "_run")

    @pytest.mark.asyncio
    async def test_run_starts_and_shuts_down(self):
        """Test _run() with mocked Slack handler."""
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
                    # start_async should return quickly (simulating a shutdown)
                    from tigerharness.slack_bridge.__main__ import _run
                    await _run()
                    mock_handler.start_async.assert_called_once()

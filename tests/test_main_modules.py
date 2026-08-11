"""Tests for __main__.py entrypoints (slack_bridge)."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestJournalMain:
    def test_module_exists(self):
        spec = importlib.util.find_spec("tigerharness.journal.__main__")
        assert spec is not None

    def test_runs_as_subprocess(self):
        """Verify ``python -m tigerharness.journal`` boots and the help
        path returns a clean exit."""
        result = subprocess.run(
            [sys.executable, "-m", "tigerharness.journal", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert "journal" in result.stdout.lower()

    def test_main_module_execution(self, monkeypatch):
        """Cover the ``sys.exit(main())`` line by executing the module
        via ``runpy`` with patched ``main``. Mirrors the pattern used
        for the top-level CLI in test_coverage_100.py."""
        import runpy
        from tigerharness.journal import cli as journal_cli
        monkeypatch.setattr(journal_cli, "main", lambda argv=None: 0)
        with pytest.raises(SystemExit) as exc_info:
            runpy.run_module(
                "tigerharness.journal", run_name="__main__",
            )
        assert exc_info.value.code == 0


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
        assert hasattr(mod, "_run_multi")

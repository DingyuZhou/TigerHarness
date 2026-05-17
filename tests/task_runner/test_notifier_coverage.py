"""Coverage-push tests for notifier.py — targeting lines:
77-78 (dotenv read OSError), 186-187 (notify=False skip),
257-258 (result.txt read OSError).
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tigerharness.task_runner.notifier import (
    _load_bridge_dotenv_into_env,
    notify_job_start,
    notify_job_end,
    _render,
)
from tigerharness.task_runner.registry import JobMeta, JobStore


class TestDotenvOSError:
    """Lines 77-78: OSError reading .env is swallowed."""

    def test_oserror_swallowed(self, tmp_path: Path, monkeypatch):
        # Create a .env that will fail to read
        env_file = tmp_path / ".env"
        env_file.write_text("SLACK_BOT_TOKEN=xoxb-test")

        with patch("tigerharness.task_runner.notifier._find_slack_env_file",
                   return_value=env_file):
            orig = Path.read_text

            def bad_read(self, *a, **kw):
                if self.name == ".env":
                    raise OSError("read error")
                return orig(self, *a, **kw)

            with patch.object(Path, "read_text", bad_read):
                # Should not raise
                _load_bridge_dotenv_into_env()


class TestNotifyFalseSkip:
    """Lines 186-187: notify=False skips DM."""

    def test_notify_false_skips_start(self):
        meta = MagicMock()
        meta.notify = False
        meta.job_id = "test123"

        result = notify_job_start(meta)
        assert result == ""


class TestBuildEndMessageResultOSError:
    """Lines 257-258: OSError reading result.txt → empty preview."""

    def test_result_oserror(self, tmp_path: Path):
        store = JobStore(tmp_path)
        meta = JobMeta(
            job_id="test123",
            persona="tester",
            prompt_chars=10,
            max_iters=3,
            compact_every=0,
            continuation="",
            name="test",
            cwd="/tmp",
            started_at=time.time(),
            status="done",
            pid=None,
            current_iter=3,
            session_id="",
            last_update=time.time(),
        )
        store.set(meta)
        # Create result file, then make it unreadable
        result_path = store.result_path("test123")
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text("Some result")

        orig = Path.read_text

        def bad_read(self, *a, **kw):
            if self.name == "result.txt":
                raise OSError("read error")
            return orig(self, *a, **kw)

        with patch.object(Path, "read_text", bad_read):
            msg = _render(meta, store)

        assert "test123" in msg
        # Should not contain the result preview
        assert "Some result" not in msg

"""Additional notifier tests — credential resolution, _post_json edge cases."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tigerharness.task_runner.notifier import (
    _find_slack_env_file,
    _load_bridge_dotenv_into_env,
    _resolve_creds,
    notify_job_end,
    notify_job_start,
)
from tigerharness.task_runner.registry import JobMeta, JobStore


class TestFindSlackEnvFile:
    def test_explicit_env_var(self, tmp_path: Path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("SLACK_BOT_TOKEN=xoxb-test\n")
        monkeypatch.setenv("TIGERHARNESS_SLACK_ENV", str(env_file))
        monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
        result = _find_slack_env_file()
        assert result == env_file

    def test_bridge_dir_env_var(self, tmp_path: Path, monkeypatch):
        bridge_dir = tmp_path / "bridge"
        bridge_dir.mkdir()
        env_file = bridge_dir / ".env"
        env_file.write_text("SLACK_BOT_TOKEN=xoxb-test\n")
        monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
        monkeypatch.setenv("TIGERHARNESS_SLACK_BRIDGE_DIR", str(bridge_dir))
        result = _find_slack_env_file()
        assert result == env_file

    def test_no_env_returns_none(self, monkeypatch):
        monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
        monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
        result = _find_slack_env_file()
        assert result is None


class TestLoadBridgeDotenv:
    def test_loads_keys(self, tmp_path: Path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "SLACK_BOT_TOKEN=xoxb-new\nALLOWED_SLACK_USER_IDS=U0CEO\n"
        )
        monkeypatch.setenv("TIGERHARNESS_SLACK_ENV", str(env_file))
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("ALLOWED_SLACK_USER_IDS", raising=False)
        _load_bridge_dotenv_into_env()
        assert os.environ.get("SLACK_BOT_TOKEN") == "xoxb-new"

    def test_doesnt_overwrite_existing(self, tmp_path: Path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("SLACK_BOT_TOKEN=xoxb-new\n")
        monkeypatch.setenv("TIGERHARNESS_SLACK_ENV", str(env_file))
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-original")
        _load_bridge_dotenv_into_env()
        assert os.environ["SLACK_BOT_TOKEN"] == "xoxb-original"

    def test_handles_unreadable_env(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("TIGERHARNESS_SLACK_ENV", str(tmp_path / "nonexistent"))
        # Should not raise
        _load_bridge_dotenv_into_env()


class TestResolveCreds:
    def test_returns_token_and_user(self, monkeypatch):
        monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
        monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_CEO_USER_ID", "U0CEO")
        result = _resolve_creds()
        assert result == ("xoxb-test", "U0CEO")

    def test_falls_back_to_allowed_user_ids(self, monkeypatch):
        monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
        monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.delenv("SLACK_CEO_USER_ID", raising=False)
        monkeypatch.setenv("ALLOWED_SLACK_USER_IDS", "U0FIRST, U0SECOND")
        result = _resolve_creds()
        assert result == ("xoxb-test", "U0FIRST")

    def test_returns_none_without_token(self, monkeypatch):
        monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
        monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        result = _resolve_creds()
        assert result is None

    def test_returns_none_without_user(self, monkeypatch):
        monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
        monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.delenv("SLACK_CEO_USER_ID", raising=False)
        monkeypatch.delenv("ALLOWED_SLACK_USER_IDS", raising=False)
        result = _resolve_creds()
        assert result is None


import time


def _make_meta(job_id="test123", **kw):
    defaults = dict(
        job_id=job_id, persona="helper", prompt_chars=42, max_iters=5,
        compact_every=5, continuation="", name="test", cwd="/tmp",
        started_at=time.time(), status="done", pid=None, current_iter=5,
        session_id="s1", last_update=time.time(),
    )
    defaults.update(kw)
    return JobMeta(**defaults)


class TestNotifyJobEnd:
    def test_end_exception_swallowed(self, tmp_path, monkeypatch):
        """notify_job_end should never raise even on unexpected errors."""
        monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
        monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_CEO_USER_ID", "U0CEO")
        meta = _make_meta()
        store = JobStore(tmp_path)
        with patch("tigerharness.task_runner.notifier._post_json",
                    side_effect=RuntimeError("boom")):
            result = notify_job_end(meta, store)
            assert result is False

    def test_end_with_thread_ts(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
        monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_CEO_USER_ID", "U0CEO")
        meta = _make_meta(slack_thread_ts="123.456")
        store = JobStore(tmp_path)
        with patch("tigerharness.task_runner.notifier._post_json",
                    return_value={"ok": True}):
            result = notify_job_end(meta, store)
            assert result is True


class TestNotifyJobStart:
    def test_start_returns_ts(self, monkeypatch):
        monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
        monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_CEO_USER_ID", "U0CEO")
        meta = _make_meta(status="running")
        with patch("tigerharness.task_runner.notifier._post_json",
                    return_value={"ok": True, "ts": "999.888"}):
            ts = notify_job_start(meta)
            assert ts == "999.888"

    def test_start_exception_returns_empty(self, monkeypatch):
        monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
        monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_CEO_USER_ID", "U0CEO")
        meta = _make_meta(status="running")
        with patch("tigerharness.task_runner.notifier._post_json",
                    side_effect=RuntimeError("boom")):
            ts = notify_job_start(meta)
            assert ts == ""

    def test_start_with_existing_thread(self, monkeypatch):
        monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
        monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_CEO_USER_ID", "U0CEO")
        meta = _make_meta(status="running", slack_thread_ts="111.222")
        with patch("tigerharness.task_runner.notifier._post_json",
                    return_value={"ok": True}):
            ts = notify_job_start(meta)
            assert ts == "111.222"

    def test_start_post_fails_returns_empty(self, monkeypatch):
        monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
        monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_CEO_USER_ID", "U0CEO")
        meta = _make_meta(status="running")
        with patch("tigerharness.task_runner.notifier._post_json",
                    return_value=None):
            ts = notify_job_start(meta)
            assert ts == ""

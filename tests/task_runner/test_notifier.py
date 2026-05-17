"""Notifier tests: cred resolution, rendering, best-effort semantics."""

from __future__ import annotations

import json
import time
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tigerharness.task_runner.notifier import (
    _find_slack_env_file,
    _load_bridge_dotenv_into_env,
    _post_json,
    _render,
    _resolve_creds,
    notify_job_end,
    notify_job_start,
)
from tigerharness.task_runner.registry import JobMeta, JobStore


def _make_meta(job_id: str = "abc12345", **over) -> JobMeta:
    base = dict(
        job_id=job_id,
        persona="test-agent",
        prompt_chars=42,
        max_iters=5,
        compact_every=5,
        continuation="",
        name="test-job",
        cwd="/tmp",
        started_at=time.time(),
        status="done",
        pid=None,
        current_iter=3,
        session_id="sess123",
        last_update=time.time(),
        total_cost_usd=0.0123,
    )
    base.update(over)
    return JobMeta(**base)


def test_resolve_creds_missing_token(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
    monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
    assert _resolve_creds() is None


def test_resolve_creds_with_token_and_user(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CEO_USER_ID", "U0TEST")
    monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
    monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
    result = _resolve_creds()
    assert result == ("xoxb-test", "U0TEST")


def test_resolve_creds_fallback_to_allowed_ids(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.delenv("SLACK_CEO_USER_ID", raising=False)
    monkeypatch.setenv("ALLOWED_SLACK_USER_IDS", "U0FIRST,U0SECOND")
    monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
    monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
    result = _resolve_creds()
    assert result == ("xoxb-test", "U0FIRST")


def test_find_slack_env_file_explicit(monkeypatch, tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("SLACK_BOT_TOKEN=xoxb-x\n")
    monkeypatch.setenv("TIGERHARNESS_SLACK_ENV", str(env_file))
    assert _find_slack_env_file() == env_file


def test_find_slack_env_file_bridge_dir(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("SLACK_BOT_TOKEN=xoxb-x\n")
    monkeypatch.setenv("TIGERHARNESS_SLACK_BRIDGE_DIR", str(tmp_path))
    assert _find_slack_env_file() == env_file


def test_render_done(tmp_path: Path):
    store = JobStore(tmp_path)
    meta = _make_meta()
    store.set(meta)
    # Write a result
    store.result_path(meta.job_id).write_text("task complete")
    text = _render(meta, store)
    assert "done" in text
    assert "test-job" in text
    assert "$0.0123" in text
    assert "task complete" in text


def test_render_error(tmp_path: Path):
    store = JobStore(tmp_path)
    meta = _make_meta(status="error", error="something broke\ndetails")
    store.set(meta)
    text = _render(meta, store)
    assert "something broke" in text


def test_notify_job_end_no_creds(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
    monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
    store = JobStore(tmp_path)
    meta = _make_meta()
    store.set(meta)
    # Should return False but never raise
    assert notify_job_end(meta, store) is False


def test_notify_job_end_quiet(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CEO_USER_ID", "U0TEST")
    store = JobStore(tmp_path)
    meta = _make_meta(notify=False)
    store.set(meta)
    assert notify_job_end(meta, store) is False


def test_notify_job_start_no_creds(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
    monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
    meta = _make_meta(status="running")
    assert notify_job_start(meta) == ""


def test_notify_job_end_success(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CEO_USER_ID", "U0CEO")
    monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
    monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
    monkeypatch.delenv("SLACK_NOTIFY_CHANNEL", raising=False)
    store = JobStore(tmp_path)
    meta = _make_meta()
    store.set(meta)
    store.result_path(meta.job_id).write_text("all done")

    with patch("tigerharness.task_runner.notifier._post_json", return_value={"ok": True, "ts": "1.1"}):
        result = notify_job_end(meta, store)
    assert result is True


def test_notify_job_end_with_thread(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CEO_USER_ID", "U0CEO")
    monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
    monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
    monkeypatch.delenv("SLACK_NOTIFY_CHANNEL", raising=False)
    store = JobStore(tmp_path)
    meta = _make_meta(slack_thread_ts="9999.1234")
    store.set(meta)

    with patch("tigerharness.task_runner.notifier._post_json", return_value={"ok": True}) as mock:
        notify_job_end(meta, store)
    payload = mock.call_args[0][2]
    assert payload["thread_ts"] == "9999.1234"


def test_notify_job_start_success(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CEO_USER_ID", "U0CEO")
    monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
    monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
    monkeypatch.delenv("SLACK_NOTIFY_CHANNEL", raising=False)
    meta = _make_meta(status="running", max_iters=10, name="cool-task")

    with patch("tigerharness.task_runner.notifier._post_json", return_value={"ok": True, "ts": "anchor.ts"}):
        ts = notify_job_start(meta)
    assert ts == "anchor.ts"


def test_notify_job_start_existing_thread(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CEO_USER_ID", "U0CEO")
    monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
    monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
    monkeypatch.delenv("SLACK_NOTIFY_CHANNEL", raising=False)
    meta = _make_meta(status="running", slack_thread_ts="existing.ts")

    with patch("tigerharness.task_runner.notifier._post_json", return_value={"ok": True, "ts": "new.ts"}):
        ts = notify_job_start(meta)
    # Should keep the existing thread, not the new message ts
    assert ts == "existing.ts"


def test_notify_channel_override(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CEO_USER_ID", "U0CEO")
    monkeypatch.setenv("SLACK_NOTIFY_CHANNEL", "C0OPS")
    monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
    monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
    store = JobStore(tmp_path)
    meta = _make_meta()
    store.set(meta)

    with patch("tigerharness.task_runner.notifier._post_json", return_value={"ok": True}) as mock:
        notify_job_end(meta, store)
    payload = mock.call_args[0][2]
    assert payload["channel"] == "C0OPS"


def test_notify_job_start_post_fails(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CEO_USER_ID", "U0CEO")
    monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
    monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
    monkeypatch.delenv("SLACK_NOTIFY_CHANNEL", raising=False)
    meta = _make_meta(status="running")

    with patch("tigerharness.task_runner.notifier._post_json", return_value=None):
        ts = notify_job_start(meta)
    assert ts == ""


# ---------------------------------------------------------------------------
# _post_json HTTP tests
# ---------------------------------------------------------------------------

class TestPostJson:
    def test_success(self):
        resp_body = json.dumps({"ok": True, "ts": "1.1"}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_body
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("tigerharness.task_runner.notifier.urllib.request.urlopen", return_value=mock_resp):
            result = _post_json("chat.postMessage", "xoxb-t", {"text": "hi"})
        assert result is not None
        assert result["ok"] is True

    def test_transport_error(self):
        with patch(
            "tigerharness.task_runner.notifier.urllib.request.urlopen",
            side_effect=urllib.error.URLError("timeout"),
        ):
            result = _post_json("chat.postMessage", "xoxb-t", {"text": "hi"})
        assert result is None

    def test_not_ok_response(self):
        resp_body = json.dumps({"ok": False, "error": "channel_not_found"}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_body
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("tigerharness.task_runner.notifier.urllib.request.urlopen", return_value=mock_resp):
            result = _post_json("chat.postMessage", "xoxb-t", {"text": "hi"})
        assert result is None


# ---------------------------------------------------------------------------
# _load_bridge_dotenv_into_env tests
# ---------------------------------------------------------------------------

class TestLoadBridgeDotenv:
    def test_loads_from_env_file(self, monkeypatch, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("SLACK_BOT_TOKEN=xoxb-loaded\nSLACK_CEO_USER_ID=U0LOADED\n")
        monkeypatch.setenv("TIGERHARNESS_SLACK_ENV", str(env_file))
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_CEO_USER_ID", raising=False)
        monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
        _load_bridge_dotenv_into_env()
        import os
        assert os.environ.get("SLACK_BOT_TOKEN") == "xoxb-loaded"

    def test_does_not_overwrite_existing(self, monkeypatch, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("SLACK_BOT_TOKEN=from-file\n")
        monkeypatch.setenv("TIGERHARNESS_SLACK_ENV", str(env_file))
        monkeypatch.setenv("SLACK_BOT_TOKEN", "already-set")
        monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
        _load_bridge_dotenv_into_env()
        import os
        assert os.environ["SLACK_BOT_TOKEN"] == "already-set"

    def test_no_file_is_noop(self, monkeypatch):
        monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
        monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
        # Should not raise
        _load_bridge_dotenv_into_env()

    def test_handles_comments_and_blanks(self, monkeypatch, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("# comment\n\nKEY1=val1\n  KEY2=\"val2\"\n")
        monkeypatch.setenv("TIGERHARNESS_SLACK_ENV", str(env_file))
        monkeypatch.delenv("KEY1", raising=False)
        monkeypatch.delenv("KEY2", raising=False)
        monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
        _load_bridge_dotenv_into_env()
        import os
        assert os.environ.get("KEY1") == "val1"
        assert os.environ.get("KEY2") == "val2"


# ---------------------------------------------------------------------------
# notify_stuck_escalation
# ---------------------------------------------------------------------------

from tigerharness.task_runner.notifier import notify_stuck_escalation


class TestNotifyStuckEscalation:
    def test_skipped_without_creds(self, monkeypatch):
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
        monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
        meta = _make_meta()
        ok = notify_stuck_escalation(meta, iter_num=3)
        assert ok is False

    def test_posts_with_rotating_light_icon(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")
        monkeypatch.setenv("SLACK_CEO_USER_ID", "UCEO")
        monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
        monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
        monkeypatch.delenv("SLACK_NOTIFY_CHANNEL", raising=False)

        captured = {}

        def fake_post(endpoint, token, payload):
            captured.update({"endpoint": endpoint, "token": token, "payload": payload})
            return {"ok": True, "ts": "9.9"}

        with patch("tigerharness.task_runner.notifier._post_json", fake_post):
            meta = _make_meta()
            meta.slack_thread_ts = "1778713006.341509"
            ok = notify_stuck_escalation(
                meta, iter_num=4,
                detail="iteration ran > 20 min; cancelling and marking error",
            )

        assert ok is True
        body = captured["payload"]
        assert ":rotating_light:" in body["text"]
        assert "iter 4" in body["text"]
        assert "abc12345" in body["text"]
        assert "20 min" in body["text"]
        assert body["thread_ts"] == "1778713006.341509"
        assert captured["endpoint"] == "chat.postMessage"

    def test_respects_notify_false(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")
        monkeypatch.setenv("SLACK_CEO_USER_ID", "UCEO")
        monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
        monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)

        def must_not_call(*a, **kw):
            raise AssertionError("must not POST when notify=False")

        with patch("tigerharness.task_runner.notifier._post_json", must_not_call):
            meta = _make_meta()
            meta.notify = False
            ok = notify_stuck_escalation(meta, iter_num=2)
        assert ok is False

    def test_never_raises_on_post_failure(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")
        monkeypatch.setenv("SLACK_CEO_USER_ID", "UCEO")
        monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
        monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)

        def boom(*a, **kw):
            raise urllib.error.URLError("network down")

        with patch("tigerharness.task_runner.notifier._post_json", boom):
            meta = _make_meta()
            ok = notify_stuck_escalation(meta, iter_num=1)
        # Must not raise; returns False.
        assert ok is False

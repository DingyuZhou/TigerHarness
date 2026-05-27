"""Additional notifier tests — credential resolution, _post_json edge cases."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tigerharness.task_runner.notifier import (
    _find_slack_env_file,
    _first_allowed_user_from_yaml,
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


# ---------------------------------------------------------------------------
# Team-folder credential discovery (cwd/configs/.env + yaml)
# ---------------------------------------------------------------------------

class TestFindSlackEnvTeamFolder:
    """_find_slack_env_file discovers cwd/configs/.env for team layouts."""

    def test_team_configs_env(self, tmp_path: Path, monkeypatch):
        """When cwd is the team root and configs/.env exists, find it."""
        monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
        monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
        configs = tmp_path / "configs"
        configs.mkdir()
        env_file = configs / ".env"
        env_file.write_text("SLACK_BOT_TOKEN=xoxb-team\n")
        monkeypatch.chdir(tmp_path)
        assert _find_slack_env_file() == env_file

    def test_explicit_env_takes_precedence(self, tmp_path: Path, monkeypatch):
        """TIGERHARNESS_SLACK_ENV wins over cwd/configs/.env."""
        explicit = tmp_path / "explicit.env"
        explicit.write_text("SLACK_BOT_TOKEN=xoxb-explicit\n")
        configs = tmp_path / "configs"
        configs.mkdir()
        (configs / ".env").write_text("SLACK_BOT_TOKEN=xoxb-team\n")
        monkeypatch.setenv("TIGERHARNESS_SLACK_ENV", str(explicit))
        monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        assert _find_slack_env_file() == explicit

    def test_no_configs_dir_returns_none(self, tmp_path: Path, monkeypatch):
        """When no candidates exist, returns None."""
        monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
        monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        assert _find_slack_env_file() is None


class TestFirstAllowedUserFromYaml:
    """_first_allowed_user_from_yaml reads slack-bridge.yaml fragments."""

    def test_reads_first_user_id(self, tmp_path: Path):
        yaml_file = tmp_path / "slack-bridge.yaml"
        yaml_file.write_text(
            "default_persona: Ayako\n"
            "allowed_user_ids:\n"
            "  - U0FIRST\n"
            "  - U0SECOND\n"
        )
        assert _first_allowed_user_from_yaml(yaml_file) == "U0FIRST"

    def test_missing_file_returns_none(self, tmp_path: Path):
        assert _first_allowed_user_from_yaml(tmp_path / "nope.yaml") is None

    def test_empty_list_returns_none(self, tmp_path: Path):
        yaml_file = tmp_path / "slack-bridge.yaml"
        yaml_file.write_text("allowed_user_ids: []\n")
        assert _first_allowed_user_from_yaml(yaml_file) is None

    def test_no_yaml_key_returns_none(self, tmp_path: Path):
        yaml_file = tmp_path / "slack-bridge.yaml"
        yaml_file.write_text("default_persona: Ayako\n")
        assert _first_allowed_user_from_yaml(yaml_file) is None

    def test_invalid_yaml_returns_none(self, tmp_path: Path):
        yaml_file = tmp_path / "slack-bridge.yaml"
        yaml_file.write_text(": : : invalid\n")
        assert _first_allowed_user_from_yaml(yaml_file) is None

    def test_non_dict_yaml_returns_none(self, tmp_path: Path):
        yaml_file = tmp_path / "slack-bridge.yaml"
        yaml_file.write_text("- just\n- a\n- list\n")
        assert _first_allowed_user_from_yaml(yaml_file) is None

    def test_no_pyyaml_returns_none(self, tmp_path: Path):
        yaml_file = tmp_path / "slack-bridge.yaml"
        yaml_file.write_text("allowed_user_ids:\n  - U0FIRST\n")
        with patch.dict("sys.modules", {"yaml": None}):
            # force ImportError on `import yaml`
            import importlib
            with patch("builtins.__import__", side_effect=lambda name, *a, **kw:
                        (_ for _ in ()).throw(ImportError("no yaml"))
                        if name == "yaml" else importlib.__import__(name, *a, **kw)):
                assert _first_allowed_user_from_yaml(yaml_file) is None


class TestResolveCredsTeamFolder:
    """_resolve_creds uses cwd/configs/slack-bridge.yaml for user ID."""

    def test_yaml_based_user_resolution(self, tmp_path: Path, monkeypatch):
        """When .env has the token and yaml has the user ID, creds resolve."""
        configs = tmp_path / "configs"
        configs.mkdir()
        (configs / ".env").write_text(
            "SLACK_BOT_TOKEN=xoxb-team\n"
            "SLACK_NOTIFY_CHANNEL=C0OPS\n"
        )
        (configs / "slack-bridge.yaml").write_text(
            "default_persona: Ayako\n"
            "allowed_user_ids:\n"
            "  - U0TEAMLEAD\n"
        )
        monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
        monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_CEO_USER_ID", raising=False)
        monkeypatch.delenv("ALLOWED_SLACK_USER_IDS", raising=False)
        monkeypatch.chdir(tmp_path)
        result = _resolve_creds()
        assert result == ("xoxb-team", "U0TEAMLEAD")

    def test_dual_post_channel_and_thread(self, tmp_path: Path, monkeypatch):
        """When SLACK_NOTIFY_CHANNEL and thread_ts are both set, posts to both."""
        configs = tmp_path / "configs"
        configs.mkdir()
        (configs / ".env").write_text(
            "SLACK_BOT_TOKEN=xoxb-team\n"
            "SLACK_NOTIFY_CHANNEL=C0OPS\n"
        )
        (configs / "slack-bridge.yaml").write_text(
            "allowed_user_ids:\n  - U0TEAMLEAD\n"
        )
        monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
        monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_CEO_USER_ID", raising=False)
        monkeypatch.delenv("ALLOWED_SLACK_USER_IDS", raising=False)
        monkeypatch.delenv("SLACK_NOTIFY_CHANNEL", raising=False)
        monkeypatch.chdir(tmp_path)
        store = JobStore(tmp_path)
        meta = _make_meta(slack_thread_ts="999.123")
        store.set(meta)

        calls = []
        def fake_post(endpoint, token, payload):
            calls.append(payload.copy())
            return {"ok": True, "ts": "posted.ts"}

        with patch("tigerharness.task_runner.notifier._post_json", fake_post):
            result = notify_job_end(meta, store)
        assert result is True
        # Two posts: one to ops-log (no thread_ts), one to DM (with thread_ts)
        assert len(calls) == 2
        assert calls[0]["channel"] == "C0OPS"
        assert "thread_ts" not in calls[0]
        assert calls[1]["channel"] == "U0TEAMLEAD"
        assert calls[1]["thread_ts"] == "999.123"

    def test_ceo_env_var_beats_yaml(self, tmp_path: Path, monkeypatch):
        """SLACK_CEO_USER_ID takes precedence over yaml."""
        configs = tmp_path / "configs"
        configs.mkdir()
        (configs / ".env").write_text("SLACK_BOT_TOKEN=xoxb-team\n")
        (configs / "slack-bridge.yaml").write_text(
            "allowed_user_ids:\n  - U0YAML\n"
        )
        monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
        monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.setenv("SLACK_CEO_USER_ID", "U0CEO")
        monkeypatch.chdir(tmp_path)
        result = _resolve_creds()
        assert result is not None
        assert result[1] == "U0CEO"

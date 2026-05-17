"""Config loading tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from tigerharness.slack_bridge.config import load


def test_load_missing_vars(monkeypatch):
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ALLOWED_SLACK_USER_IDS", raising=False)
    monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
    with pytest.raises(SystemExit, match="missing required"):
        load()


def test_load_wrong_prefix(monkeypatch):
    monkeypatch.setenv("SLACK_APP_TOKEN", "wrong-prefix")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-valid")
    monkeypatch.setenv("ALLOWED_SLACK_USER_IDS", "U0CEO")
    monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
    with pytest.raises(SystemExit, match="xapp-"):
        load()


def test_load_bad_user_ids(monkeypatch):
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-valid")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-valid")
    monkeypatch.setenv("ALLOWED_SLACK_USER_IDS", "BADID123")
    monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
    with pytest.raises(SystemExit, match="don't start with U or W"):
        load()


def test_load_success(monkeypatch):
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-valid")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-valid")
    monkeypatch.setenv("ALLOWED_SLACK_USER_IDS", "U0CEO,W0ADMIN")
    monkeypatch.setenv("TIGERHARNESS_AGENT_CWD", "/my/project")
    monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
    cfg = load()
    assert cfg.slack_app_token == "xapp-valid"
    assert cfg.slack_bot_token == "xoxb-valid"
    assert cfg.allowed_user_ids == frozenset({"U0CEO", "W0ADMIN"})
    assert cfg.agent_cwd == "/my/project"


def test_load_from_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SLACK_APP_TOKEN=xapp-fromfile\n"
        "SLACK_BOT_TOKEN=xoxb-fromfile\n"
        "ALLOWED_SLACK_USER_IDS=U0FILE\n"
    )
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ALLOWED_SLACK_USER_IDS", raising=False)
    monkeypatch.setenv("TIGERHARNESS_SLACK_ENV", str(env_file))
    cfg = load()
    assert cfg.slack_app_token == "xapp-fromfile"
    assert cfg.allowed_user_ids == frozenset({"U0FILE"})


def test_load_empty_user_ids(monkeypatch):
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-valid")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-valid")
    monkeypatch.setenv("ALLOWED_SLACK_USER_IDS", "  ,  ,  ")
    monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
    with pytest.raises(SystemExit, match="empty after parsing"):
        load()

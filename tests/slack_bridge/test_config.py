"""Config loading tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from tigerharness.slack_bridge.config import (
    load,
    normalize_tiger_memory_trigger,
)


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


# ---------------------------------------------------------------------------
# tiger_memory_trigger
# ---------------------------------------------------------------------------

class TestNormalizeTigerMemoryTrigger:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("rebuild", "rebuild"),
            ("off", "off"),
            ("", "rebuild"),       # empty -> legacy default
            (None, "rebuild"),     # unset -> legacy default
            ("  OFF  ", "off"),    # case + whitespace tolerant
            ("Rebuild", "rebuild"),
            # YAML 1.1 coerces a bare `off` to the boolean False; recover
            # the user's intent instead of falling back to the default.
            (False, "off"),
        ],
    )
    def test_valid_values(self, raw, expected):
        assert normalize_tiger_memory_trigger(raw) == expected

    def test_unknown_raises_value_error(self):
        with pytest.raises(ValueError, match="unknown tiger_memory_trigger"):
            normalize_tiger_memory_trigger("nope")

    def test_yaml_true_bool_is_rejected(self):
        # `on`/`yes`/`true` -> YAML True -> no valid mode -> error (not a
        # silent fallback). The repr in the message reflects the bool.
        with pytest.raises(ValueError, match="unknown tiger_memory_trigger True"):
            normalize_tiger_memory_trigger(True)

    def test_error_message_carries_where(self):
        with pytest.raises(ValueError, match="lane 'shohoku'"):
            normalize_tiger_memory_trigger("bogus", where="lane 'shohoku'")


def _base_env(monkeypatch):
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-valid")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-valid")
    monkeypatch.setenv("ALLOWED_SLACK_USER_IDS", "U0CEO")
    monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)


def test_load_trigger_defaults_to_rebuild(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.delenv("TIGER_MEMORY_TRIGGER", raising=False)
    assert load().tiger_memory_trigger == "rebuild"


def test_load_trigger_explicit_off(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("TIGER_MEMORY_TRIGGER", "off")
    assert load().tiger_memory_trigger == "off"


def test_load_trigger_bad_value_is_system_exit(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("TIGER_MEMORY_TRIGGER", "garbage")
    with pytest.raises(SystemExit, match="unknown tiger_memory_trigger"):
        load()

"""Notify module tests: cred loading, CLI, SlackNotifier."""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tigerharness.slack_bridge.notify import (
    SlackNotifier,
    _Creds,
    _load_creds,
    _load_slack_bridge_dotenv,
    _put_bytes,
    _resolve_dm_channel,
    _resolve_target_user_id,
    _slack_post_form,
    _slack_post_json,
    main,
)


def test_resolve_target_user_id_explicit(monkeypatch):
    monkeypatch.setenv("SLACK_CEO_USER_ID", "U0EXPLICIT")
    assert _resolve_target_user_id() == "U0EXPLICIT"


def test_resolve_target_user_id_fallback(monkeypatch):
    monkeypatch.delenv("SLACK_CEO_USER_ID", raising=False)
    monkeypatch.setenv("ALLOWED_SLACK_USER_IDS", "U0FIRST, U0SECOND")
    assert _resolve_target_user_id() == "U0FIRST"


def test_resolve_target_user_id_empty(monkeypatch):
    monkeypatch.delenv("SLACK_CEO_USER_ID", raising=False)
    monkeypatch.delenv("ALLOWED_SLACK_USER_IDS", raising=False)
    assert _resolve_target_user_id() is None


def test_load_creds_missing_token(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
    assert _load_creds() is None


def test_load_creds_success(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CEO_USER_ID", "U0CEO")
    monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
    creds = _load_creds()
    assert creds is not None
    assert creds.bot_token == "xoxb-test"
    assert creds.target_user_id == "U0CEO"


def test_try_load_none_when_no_creds(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
    assert SlackNotifier.try_load() is None


def test_cli_text_no_creds(monkeypatch, capsys):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
    ret = main(["text", "hello"])
    assert ret == 2
    assert "not configured" in capsys.readouterr().err


def test_cli_file_no_creds(monkeypatch, capsys):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
    ret = main(["file", "--file", "/tmp/nonexistent.png"])
    assert ret == 2


# ---------------------------------------------------------------------------
# HTTP layer tests (mocked urllib)
# ---------------------------------------------------------------------------

class TestSlackPostJson:
    def test_success(self):
        resp_body = json.dumps({"ok": True, "ts": "1234.5"}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_body
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("tigerharness.slack_bridge.notify.urllib.request.urlopen", return_value=mock_resp):
            result = _slack_post_json("chat.postMessage", "xoxb-t", {"text": "hi"})
        assert result["ok"] is True

    def test_transport_error(self):
        with patch(
            "tigerharness.slack_bridge.notify.urllib.request.urlopen",
            side_effect=urllib.error.URLError("timeout"),
        ):
            result = _slack_post_json("chat.postMessage", "xoxb-t", {"text": "hi"})
        assert result["ok"] is False


class TestSlackPostForm:
    def test_success(self):
        resp_body = json.dumps({"ok": True}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_body
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("tigerharness.slack_bridge.notify.urllib.request.urlopen", return_value=mock_resp):
            result = _slack_post_form("conversations.open", "xoxb-t", {"users": "U0"})
        assert result["ok"] is True


class TestResolveDmChannel:
    def test_success(self):
        with patch(
            "tigerharness.slack_bridge.notify._slack_post_form",
            return_value={"ok": True, "channel": {"id": "D0CHAN123"}},
        ):
            assert _resolve_dm_channel("xoxb-t", "U0CEO") == "D0CHAN123"

    def test_failure(self):
        with patch(
            "tigerharness.slack_bridge.notify._slack_post_form",
            return_value={"ok": False, "error": "user_not_found"},
        ):
            assert _resolve_dm_channel("xoxb-t", "U0BAD") is None


class TestPutBytes:
    def test_success(self):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("tigerharness.slack_bridge.notify.urllib.request.urlopen", return_value=mock_resp):
            assert _put_bytes("https://upload.url", b"filedata") is True

    def test_failure(self):
        with patch(
            "tigerharness.slack_bridge.notify.urllib.request.urlopen",
            side_effect=urllib.error.URLError("network"),
        ):
            assert _put_bytes("https://upload.url", b"filedata") is False


# ---------------------------------------------------------------------------
# SlackNotifier integration tests (mocked HTTP)
# ---------------------------------------------------------------------------

class TestSlackNotifierDmText:
    def _notifier(self):
        return SlackNotifier(_Creds(bot_token="xoxb-test", target_user_id="U0CEO"))

    def test_success(self):
        n = self._notifier()
        with patch(
            "tigerharness.slack_bridge.notify._slack_post_json",
            return_value={"ok": True, "ts": "1.1"},
        ) as mock:
            assert n.dm_text("hello") is True
            mock.assert_called_once()
            payload = mock.call_args[0][2]
            assert payload["text"] == "hello"
            assert payload["channel"] == "U0CEO"

    def test_with_thread(self):
        n = self._notifier()
        with patch(
            "tigerharness.slack_bridge.notify._slack_post_json",
            return_value={"ok": True},
        ) as mock:
            n.dm_text("hi", thread_ts="1234.5678")
            payload = mock.call_args[0][2]
            assert payload["thread_ts"] == "1234.5678"

    def test_with_channel_override(self):
        n = self._notifier()
        with patch(
            "tigerharness.slack_bridge.notify._slack_post_json",
            return_value={"ok": True},
        ) as mock:
            n.dm_text("hi", channel="C0CUSTOM")
            payload = mock.call_args[0][2]
            assert payload["channel"] == "C0CUSTOM"

    def test_failure(self):
        n = self._notifier()
        with patch(
            "tigerharness.slack_bridge.notify._slack_post_json",
            return_value={"ok": False, "error": "channel_not_found"},
        ):
            assert n.dm_text("hello") is False


class TestSlackNotifierDmFile:
    def _notifier(self):
        return SlackNotifier(_Creds(bot_token="xoxb-test", target_user_id="U0CEO"))

    def test_missing_file(self):
        n = self._notifier()
        assert n.dm_file("/nonexistent/path.png") is False

    def test_empty_file(self, tmp_path):
        n = self._notifier()
        f = tmp_path / "empty.txt"
        f.write_text("")
        assert n.dm_file(str(f)) is False

    def test_success(self, tmp_path):
        n = self._notifier()
        f = tmp_path / "chart.png"
        f.write_bytes(b"PNG_DATA_HERE")

        with patch(
            "tigerharness.slack_bridge.notify._resolve_dm_channel",
            return_value="D0CHAN",
        ), patch(
            "tigerharness.slack_bridge.notify._slack_post_form",
            side_effect=[
                # Step 1: getUploadURLExternal
                {"ok": True, "upload_url": "https://upload.slack.com/x", "file_id": "F123"},
                # Step 3: completeUploadExternal
                {"ok": True},
            ],
        ), patch(
            "tigerharness.slack_bridge.notify._put_bytes",
            return_value=True,
        ):
            assert n.dm_file(str(f), caption="My chart") is True

    def test_step1_fails(self, tmp_path):
        n = self._notifier()
        f = tmp_path / "chart.png"
        f.write_bytes(b"PNG")

        with patch(
            "tigerharness.slack_bridge.notify._resolve_dm_channel",
            return_value="D0CHAN",
        ), patch(
            "tigerharness.slack_bridge.notify._slack_post_form",
            return_value={"ok": False, "error": "invalid_auth"},
        ):
            assert n.dm_file(str(f)) is False

    def test_upload_fails(self, tmp_path):
        n = self._notifier()
        f = tmp_path / "chart.png"
        f.write_bytes(b"PNG")

        with patch(
            "tigerharness.slack_bridge.notify._resolve_dm_channel",
            return_value="D0CHAN",
        ), patch(
            "tigerharness.slack_bridge.notify._slack_post_form",
            return_value={"ok": True, "upload_url": "https://x", "file_id": "F1"},
        ), patch(
            "tigerharness.slack_bridge.notify._put_bytes",
            return_value=False,
        ):
            assert n.dm_file(str(f)) is False

    def test_dm_channel_resolve_fails(self, tmp_path):
        n = self._notifier()
        f = tmp_path / "chart.png"
        f.write_bytes(b"PNG")

        with patch(
            "tigerharness.slack_bridge.notify._resolve_dm_channel",
            return_value=None,
        ):
            assert n.dm_file(str(f)) is False


# ---------------------------------------------------------------------------
# CLI with mocked notifier
# ---------------------------------------------------------------------------

class TestCliWithMocks:
    def test_text_success(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_CEO_USER_ID", "U0CEO")
        monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
        with patch.object(SlackNotifier, "dm_text", return_value=True):
            ret = main(["text", "hello world"])
        assert ret == 0

    def test_text_failure(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_CEO_USER_ID", "U0CEO")
        monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
        with patch.object(SlackNotifier, "dm_text", return_value=False):
            ret = main(["text", "hello world"])
        assert ret == 1

    def test_file_success(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_CEO_USER_ID", "U0CEO")
        monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
        f = tmp_path / "test.txt"
        f.write_text("content")
        with patch.object(SlackNotifier, "dm_file", return_value=True):
            ret = main(["file", "--file", str(f)])
        assert ret == 0


# ---------------------------------------------------------------------------
# dotenv loading
# ---------------------------------------------------------------------------

class TestDotenvLoading:
    def test_loads_from_env_file(self, monkeypatch, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("SLACK_BOT_TOKEN=xoxb-from-file\nSLACK_CEO_USER_ID=U0FILE\n")
        monkeypatch.setenv("TIGERHARNESS_SLACK_ENV", str(env_file))
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_CEO_USER_ID", raising=False)
        _load_slack_bridge_dotenv()
        import os
        assert os.environ.get("SLACK_BOT_TOKEN") == "xoxb-from-file"

    def test_env_vars_not_overwritten(self, monkeypatch, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("SLACK_BOT_TOKEN=from-file\n")
        monkeypatch.setenv("TIGERHARNESS_SLACK_ENV", str(env_file))
        monkeypatch.setenv("SLACK_BOT_TOKEN", "already-set")
        _load_slack_bridge_dotenv()
        import os
        assert os.environ["SLACK_BOT_TOKEN"] == "already-set"

    def test_loads_from_cwd_configs_env(self, monkeypatch, tmp_path):
        """Team-folder convention: `tigerharness init` puts the team's
        .env at <team>/configs/.env. When an agent's cwd is the team
        root, notify must find it without TIGERHARNESS_SLACK_ENV set."""
        team_root = tmp_path
        configs = team_root / "configs"
        configs.mkdir()
        (configs / ".env").write_text(
            "SLACK_BOT_TOKEN=xoxb-from-team-configs\nSLACK_CEO_USER_ID=U0CFG\n"
        )
        monkeypatch.chdir(team_root)
        monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_CEO_USER_ID", raising=False)
        # The team root must NOT also have a top-level .env or that one
        # wins (it appears earlier in the candidates list -- intentional).
        assert not (team_root / ".env").exists()
        _load_slack_bridge_dotenv()
        import os
        assert os.environ["SLACK_BOT_TOKEN"] == "xoxb-from-team-configs"
        assert os.environ["SLACK_CEO_USER_ID"] == "U0CFG"

    def test_cwd_dotenv_beats_configs_dotenv(self, monkeypatch, tmp_path):
        """If both <cwd>/.env and <cwd>/configs/.env exist, the top-level
        one wins (it appears earlier in the candidate list). This
        preserves the legacy single-team workflow where users put .env
        at the project root."""
        (tmp_path / ".env").write_text("SLACK_BOT_TOKEN=xoxb-from-root\n")
        configs = tmp_path / "configs"
        configs.mkdir()
        (configs / ".env").write_text("SLACK_BOT_TOKEN=xoxb-from-configs\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        _load_slack_bridge_dotenv()
        import os
        assert os.environ["SLACK_BOT_TOKEN"] == "xoxb-from-root"

"""Additional notify tests — HTTP helpers, SlackNotifier, CLI commands."""
from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tigerharness.slack_bridge.notify import (
    SlackNotifier,
    _Creds,
    _load_creds,
    _resolve_dm_channel,
    _slack_post_form,
    _slack_post_json,
    main,
)


class TestSlackPostJson:
    def test_success(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"ok": True}).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("tigerharness.slack_bridge.notify.urllib.request.urlopen",
                    return_value=mock_resp):
            result = _slack_post_json("chat.postMessage", "xoxb-test", {"text": "hi"})
        assert result["ok"] is True

    def test_url_error(self):
        with patch("tigerharness.slack_bridge.notify.urllib.request.urlopen",
                    side_effect=urllib.error.URLError("timeout")):
            result = _slack_post_json("chat.postMessage", "xoxb-test", {"text": "hi"})
        assert result["ok"] is False


class TestSlackPostForm:
    def test_success(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"ok": True}).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("tigerharness.slack_bridge.notify.urllib.request.urlopen",
                    return_value=mock_resp):
            result = _slack_post_form("conversations.open", "xoxb-test", {"users": "U0CEO"})
        assert result["ok"] is True

    def test_url_error(self):
        with patch("tigerharness.slack_bridge.notify.urllib.request.urlopen",
                    side_effect=urllib.error.URLError("connection refused")):
            result = _slack_post_form("conversations.open", "xoxb-test", {"users": "U0CEO"})
        assert result["ok"] is False


class TestResolveDmChannel:
    def test_success(self):
        with patch("tigerharness.slack_bridge.notify._slack_post_form",
                    return_value={"ok": True, "channel": {"id": "D0CHAN"}}):
            result = _resolve_dm_channel("xoxb-test", "U0CEO")
        assert result == "D0CHAN"

    def test_failure(self):
        with patch("tigerharness.slack_bridge.notify._slack_post_form",
                    return_value={"ok": False}):
            result = _resolve_dm_channel("xoxb-test", "U0CEO")
        assert result is None


class TestLoadCreds:
    def test_no_token(self, monkeypatch):
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
        monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
        result = _load_creds()
        assert result is None

    def test_no_target_user(self, monkeypatch):
        monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
        monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.delenv("SLACK_CEO_USER_ID", raising=False)
        monkeypatch.delenv("SLACK_ALLOWED_USER_IDS", raising=False)
        monkeypatch.delenv("ALLOWED_SLACK_USER_IDS", raising=False)
        result = _load_creds()
        assert result is None

    def test_with_valid_creds(self, monkeypatch):
        monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
        monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_CEO_USER_ID", "U0CEO")
        result = _load_creds()
        assert result is not None
        assert result.bot_token == "xoxb-test"


class TestSlackNotifier:
    def test_dm_text_success(self):
        creds = _Creds(bot_token="xoxb-test", target_user_id="U0CEO")
        n = SlackNotifier(creds)
        with patch("tigerharness.slack_bridge.notify._slack_post_json",
                    return_value={"ok": True}):
            assert n.dm_text("hello") is True

    def test_dm_text_failure(self):
        creds = _Creds(bot_token="xoxb-test", target_user_id="U0CEO")
        n = SlackNotifier(creds)
        with patch("tigerharness.slack_bridge.notify._slack_post_json",
                    return_value={"ok": False, "error": "not_authed"}):
            assert n.dm_text("hello") is False

    def test_dm_text_with_thread(self):
        creds = _Creds(bot_token="xoxb-test", target_user_id="U0CEO")
        n = SlackNotifier(creds)
        with patch("tigerharness.slack_bridge.notify._slack_post_json",
                    return_value={"ok": True}) as mock:
            n.dm_text("hello", thread_ts="123.456")
        payload = mock.call_args[0][2]
        assert payload["thread_ts"] == "123.456"

    def test_dm_file_missing_file(self, tmp_path: Path):
        creds = _Creds(bot_token="xoxb-test", target_user_id="U0CEO")
        n = SlackNotifier(creds)
        assert n.dm_file(tmp_path / "nonexistent.txt") is False

    def test_dm_file_empty_file(self, tmp_path: Path):
        creds = _Creds(bot_token="xoxb-test", target_user_id="U0CEO")
        n = SlackNotifier(creds)
        f = tmp_path / "empty.txt"
        f.write_text("")
        assert n.dm_file(f) is False

    def test_dm_file_full_flow(self, tmp_path: Path):
        creds = _Creds(bot_token="xoxb-test", target_user_id="U0CEO")
        n = SlackNotifier(creds)
        f = tmp_path / "test.txt"
        f.write_text("content here")
        with patch("tigerharness.slack_bridge.notify._resolve_dm_channel", return_value="D0CHAN"):
            with patch("tigerharness.slack_bridge.notify._slack_post_form") as mock_form:
                mock_form.side_effect = [
                    {"ok": True, "upload_url": "https://upload.example.com", "file_id": "F123"},
                    {"ok": True},
                ]
                with patch("tigerharness.slack_bridge.notify._put_bytes", return_value=True):
                    assert n.dm_file(f, caption="test upload") is True

    def test_dm_file_step1_fails(self, tmp_path: Path):
        creds = _Creds(bot_token="xoxb-test", target_user_id="U0CEO")
        n = SlackNotifier(creds)
        f = tmp_path / "test.txt"
        f.write_text("content")
        with patch("tigerharness.slack_bridge.notify._resolve_dm_channel", return_value="D0CHAN"):
            with patch("tigerharness.slack_bridge.notify._slack_post_form",
                        return_value={"ok": False}):
                assert n.dm_file(f) is False

    def test_dm_file_step1_missing_url(self, tmp_path: Path):
        creds = _Creds(bot_token="xoxb-test", target_user_id="U0CEO")
        n = SlackNotifier(creds)
        f = tmp_path / "test.txt"
        f.write_text("content")
        with patch("tigerharness.slack_bridge.notify._resolve_dm_channel", return_value="D0CHAN"):
            with patch("tigerharness.slack_bridge.notify._slack_post_form",
                        return_value={"ok": True, "upload_url": "", "file_id": ""}):
                assert n.dm_file(f) is False

    def test_dm_file_step3_fails(self, tmp_path: Path):
        creds = _Creds(bot_token="xoxb-test", target_user_id="U0CEO")
        n = SlackNotifier(creds)
        f = tmp_path / "test.txt"
        f.write_text("content")
        with patch("tigerharness.slack_bridge.notify._resolve_dm_channel", return_value="D0CHAN"):
            with patch("tigerharness.slack_bridge.notify._slack_post_form") as mock_form:
                mock_form.side_effect = [
                    {"ok": True, "upload_url": "https://up.example.com", "file_id": "F1"},
                    {"ok": False, "error": "upload_failed"},
                ]
                with patch("tigerharness.slack_bridge.notify._put_bytes", return_value=True):
                    assert n.dm_file(f) is False

    def test_dm_file_no_dm_channel(self, tmp_path: Path):
        creds = _Creds(bot_token="xoxb-test", target_user_id="U0CEO")
        n = SlackNotifier(creds)
        f = tmp_path / "test.txt"
        f.write_text("content")
        with patch("tigerharness.slack_bridge.notify._resolve_dm_channel", return_value=None):
            assert n.dm_file(f) is False


class TestCLI:
    def test_text_command(self):
        with patch("tigerharness.slack_bridge.notify.SlackNotifier.try_load") as mock_load:
            mock_n = MagicMock()
            mock_n.dm_text.return_value = True
            mock_load.return_value = mock_n
            ret = main(["text", "hello world"])
        assert ret == 0

    def test_text_command_no_creds(self, capsys):
        with patch("tigerharness.slack_bridge.notify.SlackNotifier.try_load", return_value=None):
            ret = main(["text", "hello"])
        assert ret == 2

    def test_file_command(self, tmp_path: Path):
        f = tmp_path / "test.txt"
        f.write_text("content")
        with patch("tigerharness.slack_bridge.notify.SlackNotifier.try_load") as mock_load:
            mock_n = MagicMock()
            mock_n.dm_file.return_value = True
            mock_load.return_value = mock_n
            ret = main(["file", "--file", str(f)])
        assert ret == 0

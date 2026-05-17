"""Bridge tests: message routing, thread tracking, shutdown."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tigerharness.slack_bridge.bridge import (
    SlackBridge,
    _append_bridge_context,
    _is_user_dm,
    _strip_bot_mention,
    build_agent_config,
)
from tigerharness.slack_bridge.config import BridgeConfig
from tigerharness.slack_bridge.persistence import ThreadStore


@pytest.fixture
def cfg():
    return BridgeConfig(
        slack_app_token="xapp-test",
        slack_bot_token="xoxb-test",
        allowed_user_ids=frozenset({"U0CEO"}),
        agent_cwd="/tmp",
    )


@pytest.fixture
def store(tmp_path):
    return ThreadStore(tmp_path / "threads.json")


@pytest.fixture
def fake_backend():
    @dataclass
    class FakeSession:
        id: str = "sess-001"
        async def close(self):
            pass

    @dataclass
    class FakeResult:
        final_output: str = "Hello from Sai"
        cost_usd: float = 0.005

    backend = AsyncMock()
    backend.open_session = AsyncMock(return_value=FakeSession())
    return backend, FakeResult()


class TestHelpers:
    def test_strip_bot_mention(self):
        assert _strip_bot_mention("<@U0BOT123> hello") == "hello"
        assert _strip_bot_mention("<@U0BOT123>  hello  <@W0OTHER>world") == "hello  world"
        assert _strip_bot_mention("no mention") == "no mention"

    def test_is_user_dm(self):
        assert _is_user_dm({"channel_type": "im", "user": "U0CEO"})
        assert not _is_user_dm({"channel_type": "channel"})
        assert not _is_user_dm({"channel_type": "im", "subtype": "message_changed"})
        assert not _is_user_dm({"channel_type": "im", "bot_id": "B123"})
        # file_share subtype is accepted
        assert _is_user_dm({"channel_type": "im", "subtype": "file_share"})

    def test_append_bridge_context(self):
        result = _append_bridge_context("hello", "1234.5678", "C0CHAN")
        assert "[bridge-context]" in result
        assert "slack_thread_ts: 1234.5678" in result
        assert "slack_channel: C0CHAN" in result

    def test_append_bridge_context_no_channel(self):
        result = _append_bridge_context("hello", "1234.5678", None)
        assert "slack_thread_ts: 1234.5678" in result
        assert "slack_channel" not in result


class TestBuildAgentConfig:
    def test_with_prompt_file(self, tmp_path, monkeypatch):
        prompt = tmp_path / "agent.md"
        prompt.write_text("You are a test agent.")
        cfg = BridgeConfig(
            slack_app_token="xapp-x",
            slack_bot_token="xoxb-x",
            allowed_user_ids=frozenset({"U0X"}),
            agent_cwd="/tmp",
            agent_prompt_path=str(prompt),
        )
        agent_cfg = build_agent_config(cfg)
        assert agent_cfg.instructions == "You are a test agent."

    def test_without_prompt_file(self, caplog):
        cfg = BridgeConfig(
            slack_app_token="xapp-x",
            slack_bot_token="xoxb-x",
            allowed_user_ids=frozenset({"U0X"}),
            agent_cwd="/tmp",
        )
        with caplog.at_level("WARNING", logger="tigerharness.slack_bridge"):
            agent_cfg = build_agent_config(cfg)
        assert "helpful assistant" in agent_cfg.instructions
        # Loud at startup so operators notice the persona is missing.
        assert any(
            "TIGERHARNESS_AGENT_PROMPT" in rec.message
            for rec in caplog.records
        ), [r.message for r in caplog.records]

    def test_missing_prompt_file(self, tmp_path):
        cfg = BridgeConfig(
            slack_app_token="xapp-x",
            slack_bot_token="xoxb-x",
            allowed_user_ids=frozenset({"U0X"}),
            agent_cwd="/tmp",
            agent_prompt_path=str(tmp_path / "nonexistent.md"),
        )
        with pytest.raises(FileNotFoundError):
            build_agent_config(cfg)


class TestTriggerTigerMemoryRebuild:
    def test_no_config_path_is_noop(self, cfg):
        from tigerharness.slack_bridge.bridge import _trigger_tiger_memory_rebuild
        # cfg has no tiger_memory_config_path by default
        _trigger_tiger_memory_rebuild(cfg, "thread-1")  # should not raise

    def test_cli_not_found(self):
        from tigerharness.slack_bridge.bridge import _trigger_tiger_memory_rebuild
        cfg = BridgeConfig(
            slack_app_token="xapp-x",
            slack_bot_token="xoxb-x",
            allowed_user_ids=frozenset({"U0X"}),
            agent_cwd="/tmp",
            tiger_memory_config_path="/tmp/config.yaml",
            tiger_memory_cli="",  # not set
        )
        with patch("tigerharness.slack_bridge.bridge.shutil.which", return_value=None):
            # Should log warning but not raise
            _trigger_tiger_memory_rebuild(cfg, "thread-1")

    def test_explicit_cli_spawns(self):
        from tigerharness.slack_bridge.bridge import _trigger_tiger_memory_rebuild
        cfg = BridgeConfig(
            slack_app_token="xapp-x",
            slack_bot_token="xoxb-x",
            allowed_user_ids=frozenset({"U0X"}),
            agent_cwd="/tmp",
            tiger_memory_config_path="/tmp/config.yaml",
            tiger_memory_cli="/usr/bin/tiger-memory",
        )
        with patch("tigerharness.slack_bridge.bridge.subprocess.Popen") as mock_popen:
            _trigger_tiger_memory_rebuild(cfg, "thread-1")
        mock_popen.assert_called_once()
        cmd = mock_popen.call_args[0][0]
        assert "/usr/bin/tiger-memory" in cmd
        assert "--config" in cmd
        assert "rebuild" in cmd

    def test_spawn_failure_logged_not_raised(self):
        from tigerharness.slack_bridge.bridge import _trigger_tiger_memory_rebuild
        cfg = BridgeConfig(
            slack_app_token="xapp-x",
            slack_bot_token="xoxb-x",
            allowed_user_ids=frozenset({"U0X"}),
            agent_cwd="/tmp",
            tiger_memory_config_path="/tmp/config.yaml",
            tiger_memory_cli="/usr/bin/tiger-memory",
        )
        with patch("tigerharness.slack_bridge.bridge.subprocess.Popen", side_effect=OSError("fail")):
            _trigger_tiger_memory_rebuild(cfg, "thread-1")  # should not raise


class TestSlackBridge:
    @pytest.fixture
    def bridge(self, cfg, store, fake_backend):
        from tigerharness.agent_sdk import AgentConfig
        backend, fake_result = fake_backend
        agent_cfg = AgentConfig(name="test", instructions="test")
        downloader = MagicMock()
        downloader.download = AsyncMock(return_value=None)
        b = SlackBridge(cfg, backend, agent_cfg, store, downloader=downloader)
        return b, backend, fake_result

    @pytest.mark.asyncio
    async def test_drops_non_allowed_user(self, bridge):
        b, backend, _ = bridge
        say = AsyncMock()
        event = {"channel_type": "im", "user": "U0STRANGER", "text": "hi", "ts": "1.1"}
        await b.handle_message(event, say)
        say.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_drops_non_dm(self, bridge):
        b, backend, _ = bridge
        say = AsyncMock()
        event = {"channel_type": "channel", "user": "U0CEO", "text": "hi", "ts": "1.1"}
        await b.handle_message(event, say)
        say.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dispatches_valid_dm(self, bridge):
        from unittest.mock import patch
        b, backend, fake_result = bridge

        with patch("tigerharness.slack_bridge.bridge.run_with_retry", return_value=fake_result):
            say = AsyncMock()
            event = {"channel_type": "im", "user": "U0CEO", "text": "hello", "ts": "1.1"}
            await b.handle_message(event, say)

        say.assert_awaited_once()
        call_kwargs = say.call_args[1]
        assert call_kwargs["thread_ts"] == "1.1"
        assert "Hello from Sai" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_shutdown_rejects_new(self, bridge):
        b, backend, _ = bridge
        b.request_shutdown()
        say = AsyncMock()
        event = {"channel_type": "im", "user": "U0CEO", "text": "hi", "ts": "1.1"}
        await b.handle_message(event, say)
        say.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_drain_when_empty(self, bridge):
        b, _, _ = bridge
        result = await b.wait_for_drain(timeout=1.0)
        assert result is True

    @pytest.mark.asyncio
    async def test_tracked_thread_reply(self, bridge):
        """A reply in a tracked thread (via store) should be dispatched."""
        from unittest.mock import patch as _patch
        b, backend, fake_result = bridge

        # Pre-seed the store with a tracked thread
        b._store.set("parent.ts", "existing-sess")

        with _patch("tigerharness.slack_bridge.bridge.run_with_retry", return_value=fake_result):
            say = AsyncMock()
            # This is a thread reply (thread_ts present), not a DM
            event = {
                "channel_type": "channel",
                "user": "U0CEO",
                "text": "follow up",
                "ts": "child.ts",
                "thread_ts": "parent.ts",
            }
            await b.handle_message(event, say)

        say.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_untracked_thread_reply_dropped(self, bridge):
        """A reply in an untracked thread should be silently dropped."""
        b, backend, _ = bridge
        say = AsyncMock()
        event = {
            "channel_type": "channel",
            "user": "U0CEO",
            "text": "random thread reply",
            "ts": "child.ts",
            "thread_ts": "unknown-parent.ts",
        }
        await b.handle_message(event, say)
        say.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mention_strips_tag(self, bridge):
        from unittest.mock import patch
        b, backend, fake_result = bridge

        with patch("tigerharness.slack_bridge.bridge.run_with_retry", return_value=fake_result):
            say = AsyncMock()
            event = {
                "user": "U0CEO",
                "text": "<@U0BOT123> what's up",
                "ts": "2.2",
                "channel": "C0CHAN",
            }
            await b.handle_mention(event, say)

        say.assert_awaited_once()

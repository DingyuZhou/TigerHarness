"""Additional bridge tests — mention ACL, empty dispatch, file-fail warning,
backend error, drain timeout."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tigerharness.slack_bridge.bridge import SlackBridge, _trigger_tiger_memory_rebuild
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
        final_output: str = "Reply text"
        cost_usd: float = 0.002

    backend = AsyncMock()
    backend.open_session = AsyncMock(return_value=FakeSession())
    return backend, FakeResult()


@pytest.fixture
def bridge(cfg, store, fake_backend):
    from tigerharness.agent_sdk import AgentConfig
    backend, fake_result = fake_backend
    agent_cfg = AgentConfig(name="test", instructions="test")
    downloader = MagicMock()
    downloader.download = AsyncMock(return_value=None)
    b = SlackBridge(cfg, backend, agent_cfg, store, downloader=downloader)
    return b, backend, fake_result


class TestMentionEdgeCases:
    @pytest.mark.asyncio
    async def test_mention_non_allowed_user_dropped(self, bridge):
        b, _, _ = bridge
        say = AsyncMock()
        event = {"user": "U0STRANGER", "text": "<@U0BOT> hi", "ts": "3.3"}
        await b.handle_mention(event, say)
        say.assert_not_awaited()


class TestDispatchEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_text_and_no_files(self, bridge):
        """Empty message with no files should be silently dropped."""
        b, _, _ = bridge
        say = AsyncMock()
        event = {"channel_type": "im", "user": "U0CEO", "text": "", "ts": "4.4"}
        await b.handle_message(event, say)
        say.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_files_all_fail_sends_warning(self, bridge):
        """When all file downloads fail, bridge sends a warning."""
        b, _, fake_result = bridge
        # Downloader returns None (failed) for all files
        b._downloader.download = AsyncMock(return_value=None)
        say = AsyncMock()
        event = {
            "channel_type": "im",
            "user": "U0CEO",
            "text": "",  # no text
            "ts": "5.5",
            "files": [{"id": "F1"}, {"id": "F2"}],
        }
        await b.handle_message(event, say)
        say.assert_awaited_once()
        call_kwargs = say.call_args[1]
        assert "warning" in call_kwargs["text"].lower() or ":warning:" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_backend_error_returns_warning(self, bridge):
        """When backend fails, bridge posts error message."""
        b, backend, _ = bridge
        with patch("tigerharness.slack_bridge.bridge.run_with_retry",
                    side_effect=RuntimeError("connection lost")):
            say = AsyncMock()
            event = {"channel_type": "im", "user": "U0CEO", "text": "hi", "ts": "6.6"}
            await b.handle_message(event, say)
        say.assert_awaited_once()
        reply = say.call_args[1]["text"]
        assert "warning" in reply.lower() or "error" in reply.lower()


class TestDrain:
    @pytest.mark.asyncio
    async def test_drain_timeout(self, bridge):
        """Drain should return False when timeout expires with work in-flight."""
        b, _, _ = bridge
        # Simulate in-flight work
        b._in_flight = 1
        b._drained.clear()
        result = await b.wait_for_drain(timeout=0.1)
        assert result is False

"""Coverage-push tests for bridge.py — targeting lines:
152 (successful file download), 212/215/217 (_is_tracked_thread_reply branches).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tigerharness.slack_bridge.bridge import SlackBridge
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


@dataclass
class FakeSession:
    id: str = "sess-001"

    async def close(self):
        pass


@dataclass
class FakeResult:
    final_output: str = "Reply text"
    cost_usd: float = 0.002


@dataclass
class FakeAttachment:
    file_id: str = "F001"
    name: str = "doc.pdf"
    mimetype: str = "application/pdf"
    size: int = 1024
    path: Path = Path("/tmp/doc.pdf")


@pytest.fixture
def bridge_with_files(cfg, store):
    """Bridge whose downloader returns a successful attachment."""
    from tigerharness.agent_sdk import AgentConfig

    backend = AsyncMock()
    backend.open_session = AsyncMock(return_value=FakeSession())
    agent_cfg = AgentConfig(name="test", instructions="test")

    downloader = MagicMock()
    downloader.download = AsyncMock(return_value=FakeAttachment())

    b = SlackBridge(cfg, backend, agent_cfg, store, downloader=downloader)
    return b, backend


class TestFileDownloadSuccess:
    """Line 152: successful file download → attachment appended."""

    @pytest.mark.asyncio
    async def test_file_downloaded_and_dispatched(self, bridge_with_files):
        b, backend = bridge_with_files

        with patch("tigerharness.slack_bridge.bridge.run_with_retry",
                   return_value=FakeResult()):
            say = AsyncMock()
            event = {
                "channel_type": "im",
                "user": "U0CEO",
                "text": "Check this file",
                "ts": "7.7",
                "files": [{"id": "F1"}],
            }
            await b.handle_message(event, say)

        say.assert_awaited_once()
        reply = say.call_args[1]["text"]
        assert reply == "Reply text"


class TestIsTrackedThreadReply:
    """Lines 212, 215, 217: _is_tracked_thread_reply branches."""

    @pytest.fixture
    def bridge(self, cfg, store):
        from tigerharness.agent_sdk import AgentConfig
        backend = AsyncMock()
        backend.open_session = AsyncMock(return_value=FakeSession())
        agent_cfg = AgentConfig(name="test", instructions="test")
        downloader = MagicMock()
        b = SlackBridge(cfg, backend, agent_cfg, store, downloader=downloader)
        return b

    def test_no_thread_ts(self, bridge):
        """Line 209: no thread_ts → False."""
        event = {"ts": "1.1", "user": "U0CEO"}
        assert bridge._is_tracked_thread_reply(event) is False

    def test_bot_message(self, bridge):
        """Line 212: bot_id present → False."""
        event = {"ts": "1.1", "thread_ts": "1.0", "bot_id": "B123"}
        assert bridge._is_tracked_thread_reply(event) is False

    def test_bad_subtype(self, bridge):
        """Line 215: subtype not in accepted set → False."""
        event = {"ts": "1.1", "thread_ts": "1.0", "subtype": "channel_join"}
        assert bridge._is_tracked_thread_reply(event) is False

    def test_tracked_in_memory(self, bridge):
        """Line 217: thread_ts in self._threads → True."""
        # Simulate an active thread
        bridge._threads["1.0"] = MagicMock()
        event = {"ts": "1.1", "thread_ts": "1.0", "user": "U0CEO"}
        assert bridge._is_tracked_thread_reply(event) is True

    def test_tracked_in_store(self, bridge):
        """Line 218: thread_ts in store → True."""
        bridge._store.set("1.0", "sess-old")
        event = {"ts": "1.1", "thread_ts": "1.0", "user": "U0CEO"}
        assert bridge._is_tracked_thread_reply(event) is True

    def test_untracked(self, bridge):
        """Neither in memory nor store → False."""
        event = {"ts": "1.1", "thread_ts": "999.0", "user": "U0CEO"}
        assert bridge._is_tracked_thread_reply(event) is False

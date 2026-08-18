"""The 2026-08-17 incident, as tests.

The Operator sent a message while the Socket Mode session was silently
dead. Slack accepted it; the bridge never saw it; nothing anywhere
reported an error. These tests hold the two properties that make that
impossible to repeat, and the one property that makes the cure safe:

* a message sent during the gap IS delivered on reconnect,
* a message delivered normally is NEVER delivered twice, even if the
  bridge is killed mid-turn and restarted,
* anything the bounds exclude is LOGGED, never silently dropped.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from tigerharness.slack_bridge.bridge import SlackBridge
from tigerharness.slack_bridge.config import BridgeConfig
from tigerharness.slack_bridge.persistence import ThreadStore
from tigerharness.slack_bridge.reconnect import CatchupConfig, run_catchup

DM = "D0B4L5V7RFG"
OPERATOR = "U0CEO"


@pytest.fixture
def cfg():
    return BridgeConfig(
        slack_app_token="xapp-test",
        slack_bot_token="xoxb-test",
        allowed_user_ids=frozenset({OPERATOR}),
        agent_cwd="/tmp",
    )


@dataclass
class FakeSession:
    id: str = "sess-001"

    async def close(self) -> None:
        pass


@dataclass
class FakeResult:
    final_output: str = "Reply text"
    cost_usd: float = 0.002
    usage: object = None


def make_bridge(cfg, state_path):
    from tigerharness.agent_sdk import AgentConfig

    backend = AsyncMock()
    backend.open_session = AsyncMock(return_value=FakeSession())
    downloader = MagicMock()
    downloader.download = AsyncMock(return_value=None)
    b = SlackBridge(
        cfg,
        backend,
        AgentConfig(name="test", instructions="test"),
        ThreadStore(state_path),
        downloader=downloader,
    )
    b.app.client.chat_postMessage = AsyncMock()
    return b, backend


@pytest.fixture
def bridge(cfg, tmp_path):
    return make_bridge(cfg, tmp_path / "threads.json")


def history_client(*pages):
    """A web client whose ``conversations_history`` returns *pages* once."""
    client = MagicMock()
    client.conversations_history = AsyncMock(
        return_value={"messages": list(pages)}
    )
    client.conversations_replies = AsyncMock(return_value={"messages": []})
    return client


def dm(ts: str, text: str = "PR merged") -> dict:
    return {"user": OPERATOR, "text": text, "ts": ts, "channel_type": "im"}


class TestGapIsRecovered:
    """Acceptance criterion 2."""

    @pytest.mark.asyncio
    async def test_message_sent_while_socket_was_dead_is_delivered(
        self, bridge, monkeypatch
    ):
        b, backend = bridge
        # One normal message, so the ledger knows this DM exists at all.
        monkeypatch.setattr(
            "tigerharness.slack_bridge.bridge.run_with_retry",
            AsyncMock(return_value=FakeResult()),
        )
        await b.handle_message(
            dict(dm("1786900916.000100"), channel=DM), AsyncMock()
        )

        # ... then the socket dies and the Operator's next message only
        # ever reaches Slack's servers.
        missed = dict(dm("1786900916.787969"), channel=DM)
        replayed = await run_catchup(
            target=b,
            ledger=b.seen_ledger,
            web_client=history_client(missed),
            cfg=CatchupConfig(),
            now=lambda: 1786900920.0,
        )

        assert replayed == 1
        b.app.client.chat_postMessage.assert_awaited_once()
        assert (
            b.app.client.chat_postMessage.await_args.kwargs["channel"] == DM
        )

    @pytest.mark.asyncio
    async def test_replay_honours_the_allowlist(self, bridge, monkeypatch):
        b, _ = bridge
        monkeypatch.setattr(
            "tigerharness.slack_bridge.bridge.run_with_retry",
            AsyncMock(return_value=FakeResult()),
        )
        await b.handle_message(
            dict(dm("1786900916.000100"), channel=DM), AsyncMock()
        )
        b.app.client.chat_postMessage.reset_mock()

        intruder = dict(
            dm("1786900916.500000"), channel=DM, user="U0STRANGER"
        )
        await run_catchup(
            target=b,
            ledger=b.seen_ledger,
            web_client=history_client(intruder),
            cfg=CatchupConfig(),
            now=lambda: 1786900920.0,
        )
        b.app.client.chat_postMessage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_replay_is_refused_while_shutting_down(
        self, bridge, monkeypatch
    ):
        b, _ = bridge
        monkeypatch.setattr(
            "tigerharness.slack_bridge.bridge.run_with_retry",
            AsyncMock(return_value=FakeResult()),
        )
        await b.handle_message(
            dict(dm("1786900916.000100"), channel=DM), AsyncMock()
        )
        b.app.client.chat_postMessage.reset_mock()
        b.request_shutdown()

        await run_catchup(
            target=b,
            ledger=b.seen_ledger,
            web_client=history_client(dict(dm("1786900916.500000"), channel=DM)),
            cfg=CatchupConfig(),
            now=lambda: 1786900920.0,
        )
        b.app.client.chat_postMessage.assert_not_awaited()
        # Refused, not consumed: the ledger must still consider it new so
        # the next session can deliver it.
        assert not b.seen_ledger.was_seen(DM, "1786900916.500000")


class TestNeverTwice:
    """Acceptance criterion 3 -- the main correctness risk."""

    @pytest.mark.asyncio
    async def test_replaying_an_already_answered_message_is_a_no_op(
        self, bridge, monkeypatch
    ):
        b, _ = bridge
        run = AsyncMock(return_value=FakeResult())
        monkeypatch.setattr(
            "tigerharness.slack_bridge.bridge.run_with_retry", run
        )
        event = dict(dm("1786900916.787969"), channel=DM)
        await b.handle_message(dict(event), AsyncMock())
        assert run.await_count == 1

        await run_catchup(
            target=b,
            ledger=b.seen_ledger,
            web_client=history_client(dict(event)),
            cfg=CatchupConfig(),
            now=lambda: 1786900920.0,
        )
        assert run.await_count == 1

    @pytest.mark.asyncio
    async def test_no_double_run_across_a_restart_mid_turn(
        self, cfg, tmp_path, monkeypatch
    ):
        """The killed-mid-turn case: the claim is on disk BEFORE the
        agent runs, so a restart cannot re-run side-effectful work."""
        state = tmp_path / "threads.json"
        event = dict(dm("1786900916.787969", "push the branch"), channel=DM)

        first, _ = make_bridge(cfg, state)
        died = AsyncMock(side_effect=RuntimeError("SIGKILL mid-turn"))
        monkeypatch.setattr(
            "tigerharness.slack_bridge.bridge.run_with_retry", died
        )
        await first.handle_message(dict(event), AsyncMock())
        assert died.await_count == 1

        # New process, same state directory.
        second, _ = make_bridge(cfg, state)
        run = AsyncMock(return_value=FakeResult())
        monkeypatch.setattr(
            "tigerharness.slack_bridge.bridge.run_with_retry", run
        )
        await run_catchup(
            target=second,
            ledger=second.seen_ledger,
            web_client=history_client(dict(event)),
            cfg=CatchupConfig(),
            now=lambda: 1786900920.0,
        )
        assert run.await_count == 0


class TestNothingIsDroppedQuietly:
    """Acceptance criterion 5."""

    @pytest.mark.asyncio
    async def test_age_bound_is_logged(self, bridge, monkeypatch, caplog):
        b, _ = bridge
        monkeypatch.setattr(
            "tigerharness.slack_bridge.bridge.run_with_retry",
            AsyncMock(return_value=FakeResult()),
        )
        await b.handle_message(
            dict(dm("1786900916.000100"), channel=DM), AsyncMock()
        )
        with caplog.at_level(logging.WARNING):
            await run_catchup(
                target=b,
                ledger=b.seen_ledger,
                web_client=history_client(dict(dm("100.000000"), channel=DM)),
                cfg=CatchupConfig(max_age_s=60.0),
                now=lambda: 1786900920.0,
            )
        assert "older than" in caplog.text

    @pytest.mark.asyncio
    async def test_count_bound_is_logged(self, bridge, monkeypatch, caplog):
        b, _ = bridge
        monkeypatch.setattr(
            "tigerharness.slack_bridge.bridge.run_with_retry",
            AsyncMock(return_value=FakeResult()),
        )
        await b.handle_message(
            dict(dm("1786900916.000100"), channel=DM), AsyncMock()
        )
        backlog = [
            dict(dm(f"1786900917.00{i:04d}"), channel=DM) for i in range(5)
        ]
        with caplog.at_level(logging.WARNING):
            replayed = await run_catchup(
                target=b,
                ledger=b.seen_ledger,
                web_client=history_client(*backlog),
                cfg=CatchupConfig(max_messages=2),
                now=lambda: 1786900920.0,
            )
        assert replayed == 2
        assert "bound" in caplog.text

    @pytest.mark.asyncio
    async def test_a_gap_wider_than_the_age_bound_is_announced(
        self, bridge, monkeypatch, caplog
    ):
        """The bound is mostly enforced by the cursor we send Slack, so
        the excluded messages never come back to be counted. Without
        this warning, "down overnight" would silently lose the night."""
        b, _ = bridge
        monkeypatch.setattr(
            "tigerharness.slack_bridge.bridge.run_with_retry",
            AsyncMock(return_value=FakeResult()),
        )
        await b.handle_message(
            dict(dm("1786800000.000100"), channel=DM), AsyncMock()
        )
        with caplog.at_level(logging.WARNING):
            await run_catchup(
                target=b,
                ledger=b.seen_ledger,
                web_client=history_client(),
                cfg=CatchupConfig(max_age_s=3600.0),
                now=lambda: 1786900920.0,  # ~28h after the last message
            )
        assert "starts at the" in caplog.text
        assert "NOT delivered" in caplog.text

    @pytest.mark.asyncio
    async def test_no_warning_when_the_gap_fits_inside_the_bound(
        self, bridge, monkeypatch, caplog
    ):
        b, _ = bridge
        monkeypatch.setattr(
            "tigerharness.slack_bridge.bridge.run_with_retry",
            AsyncMock(return_value=FakeResult()),
        )
        await b.handle_message(
            dict(dm("1786900916.000100"), channel=DM), AsyncMock()
        )
        with caplog.at_level(logging.WARNING):
            await run_catchup(
                target=b,
                ledger=b.seen_ledger,
                web_client=history_client(),
                cfg=CatchupConfig(max_age_s=3600.0),
                now=lambda: 1786900920.0,
            )
        assert "starts at the" not in caplog.text

    @pytest.mark.asyncio
    async def test_unroutable_message_is_logged_not_swallowed(
        self, bridge, caplog
    ):
        b, _ = bridge
        # A channel message with no thread and no `im` type: the bridge
        # has no way to route it offline.
        with caplog.at_level(logging.WARNING):
            await b.replay(
                {"user": OPERATOR, "text": "hi", "ts": "9.9", "channel": "C0X"}
            )
        assert "cannot route" in caplog.text
        b.app.client.chat_postMessage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disabled_catchup_says_so(self, bridge, caplog):
        b, _ = bridge
        with caplog.at_level(logging.WARNING):
            replayed = await run_catchup(
                target=b,
                ledger=b.seen_ledger,
                web_client=history_client(),
                cfg=CatchupConfig(enabled=False),
            )
        assert replayed == 0
        assert "NOT be recovered" in caplog.text

    @pytest.mark.asyncio
    async def test_a_failing_history_call_is_loud_and_not_fatal(
        self, bridge, monkeypatch, caplog
    ):
        b, _ = bridge
        monkeypatch.setattr(
            "tigerharness.slack_bridge.bridge.run_with_retry",
            AsyncMock(return_value=FakeResult()),
        )
        await b.handle_message(
            dict(dm("1786900916.000100"), channel=DM), AsyncMock()
        )
        client = history_client()
        client.conversations_history = AsyncMock(
            side_effect=RuntimeError("slack is down too")
        )
        with caplog.at_level(logging.WARNING):
            replayed = await run_catchup(
                target=b,
                ledger=b.seen_ledger,
                web_client=client,
                cfg=CatchupConfig(),
                now=lambda: 1786900920.0,
            )
        assert replayed == 0
        assert "stays missed" in caplog.text


class TestThreadCatchup:
    @pytest.mark.asyncio
    async def test_missed_thread_reply_is_replayed(self, bridge, monkeypatch):
        b, _ = bridge
        run = AsyncMock(return_value=FakeResult())
        monkeypatch.setattr(
            "tigerharness.slack_bridge.bridge.run_with_retry", run
        )
        parent = "1786900916.000100"
        await b.handle_message(dict(dm(parent), channel=DM), AsyncMock())
        assert b.catchup_threads() == [(DM, parent)]

        b.app.client.chat_postMessage.reset_mock()
        client = history_client()
        client.conversations_replies = AsyncMock(
            return_value={
                "messages": [
                    {
                        "user": OPERATOR,
                        "text": "and the tag?",
                        "ts": "1786900917.000200",
                        "thread_ts": parent,
                    }
                ]
            }
        )
        replayed = await run_catchup(
            target=b,
            ledger=b.seen_ledger,
            web_client=client,
            cfg=CatchupConfig(),
            now=lambda: 1786900920.0,
        )
        assert replayed == 1
        b.app.client.chat_postMessage.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_reply_older_than_the_channel_watermark_still_arrives(
        self, bridge, monkeypatch
    ):
        """A DM's watermark advances with its top-level messages. If the
        thread pass used it as a cursor, a thread reply that predates the
        newest DM would be filtered out by Slack and lost forever."""
        b, _ = bridge
        monkeypatch.setattr(
            "tigerharness.slack_bridge.bridge.run_with_retry",
            AsyncMock(return_value=FakeResult()),
        )
        parent = "1786900916.000100"
        await b.handle_message(dict(dm(parent), channel=DM), AsyncMock())
        # A newer top-level DM pushes the channel watermark past the
        # missed reply below.
        await b.handle_message(
            dict(dm("1786900919.000000"), channel=DM), AsyncMock()
        )
        b.app.client.chat_postMessage.reset_mock()

        client = history_client()
        client.conversations_replies = AsyncMock(
            return_value={
                "messages": [
                    {
                        "user": OPERATOR,
                        "text": "and the tag?",
                        "ts": "1786900917.000200",  # older than the watermark
                        "thread_ts": parent,
                    }
                ]
            }
        )
        replayed = await run_catchup(
            target=b, ledger=b.seen_ledger, web_client=client,
            cfg=CatchupConfig(), now=lambda: 1786900920.0,
        )
        assert replayed == 1
        assert (
            client.conversations_replies.await_args.kwargs["oldest"]
            == "1786897320.000000"
        )

    @pytest.mark.asyncio
    async def test_our_own_replies_do_not_eat_the_budget(
        self, bridge, monkeypatch
    ):
        b, _ = bridge
        monkeypatch.setattr(
            "tigerharness.slack_bridge.bridge.run_with_retry",
            AsyncMock(return_value=FakeResult()),
        )
        await b.handle_message(
            dict(dm("1786900916.000100"), channel=DM), AsyncMock()
        )
        ours = {
            "bot_id": "B0BRIDGE",
            "text": "[Anzai]: on it",
            "ts": "1786900917.000000",
        }
        noise = {
            "subtype": "channel_join",
            "user": OPERATOR,
            "ts": "1786900917.000001",
        }
        replayed = await run_catchup(
            target=b,
            ledger=b.seen_ledger,
            web_client=history_client(dict(ours, channel=DM), dict(noise, channel=DM)),
            cfg=CatchupConfig(),
            now=lambda: 1786900920.0,
        )
        assert replayed == 0

"""Wiring: one Socket Mode hook feeds both the watchdog and the catch-up.

``message_listeners`` sees every frame Slack sends, including the
``hello`` that opens a session -- so the liveness clock and the catch-up
trigger come from the same place, and neither has to poll the socket's
state to find out it is alive.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tigerharness.slack_bridge.__main__ import attach_reconnect_guards
from tigerharness.slack_bridge.reconnect import (
    CatchupConfig,
    WatchdogConfig,
    _Budget,
    _oldest,
    run_catchup,
)


def make_handler():
    handler = MagicMock()
    handler.client = MagicMock()
    handler.client.message_listeners = []
    handler.client.last_ping_pong_time = None
    handler.client.connect_to_new_endpoint = AsyncMock()
    return handler


def make_bridge():
    bridge = MagicMock()
    bridge.seen_ledger = MagicMock()
    bridge.app.client = MagicMock()
    return bridge


@pytest.fixture
def wired():
    handler, bridge = make_handler(), make_bridge()
    wd = attach_reconnect_guards(
        handler, bridge, "Shohoku",
        WatchdogConfig(enabled=False), CatchupConfig(),
    )
    yield handler, bridge, wd
    wd.stop()


class TestTheHook:
    @pytest.mark.asyncio
    async def test_hello_triggers_a_catch_up(self, wired):
        handler, bridge, _ = wired
        listener = handler.client.message_listeners[0]
        with patch(
            "tigerharness.slack_bridge.__main__.run_catchup",
            AsyncMock(return_value=0),
        ) as catchup:
            await listener(handler.client, {"type": "hello"}, "{}")
        catchup.assert_awaited_once()
        assert catchup.await_args.kwargs["target"] is bridge
        assert catchup.await_args.kwargs["lane"] == "Shohoku"

    @pytest.mark.asyncio
    async def test_an_ordinary_frame_only_marks_liveness(self, wired):
        handler, _, wd = wired
        listener = handler.client.message_listeners[0]
        before = wd._last_activity
        await asyncio.sleep(0.01)
        with patch(
            "tigerharness.slack_bridge.__main__.run_catchup",
            AsyncMock(return_value=0),
        ) as catchup:
            await listener(handler.client, {"type": "events_api"}, "{}")
        catchup.assert_not_awaited()
        assert wd._last_activity > before

    @pytest.mark.asyncio
    async def test_a_session_opened_mid_catch_up_is_not_lost(
        self, wired, caplog
    ):
        """A `hello` arriving during a catch-up marks a LATER gap than
        the one being replayed. Dropping it would leave that gap
        unrecovered, so it queues a re-run."""
        handler, _, _ = wired
        listener = handler.client.message_listeners[0]
        gate = asyncio.Event()
        runs = 0

        async def _slow(**_):
            nonlocal runs
            runs += 1
            if runs == 1:
                await gate.wait()
            return 0

        with patch("tigerharness.slack_bridge.__main__.run_catchup", _slow):
            first = asyncio.create_task(
                listener(handler.client, {"type": "hello"}, "{}")
            )
            await asyncio.sleep(0)
            with caplog.at_level(logging.INFO):
                await listener(handler.client, {"type": "hello"}, "{}")
            gate.set()
            await first

        assert runs == 2
        assert "queued a re-run" in caplog.text

    @pytest.mark.asyncio
    async def test_a_reconnect_storm_coalesces_to_one_re_run(self, wired):
        """Three `hello`s during one run is still one re-run, not three:
        the later passes would cover the same window."""
        handler, _, _ = wired
        listener = handler.client.message_listeners[0]
        gate = asyncio.Event()
        runs = 0

        async def _slow(**_):
            nonlocal runs
            runs += 1
            if runs == 1:
                await gate.wait()
            return 0

        with patch("tigerharness.slack_bridge.__main__.run_catchup", _slow):
            first = asyncio.create_task(
                listener(handler.client, {"type": "hello"}, "{}")
            )
            await asyncio.sleep(0)
            for _ in range(3):
                await listener(handler.client, {"type": "hello"}, "{}")
            gate.set()
            await first

        assert runs == 2

    @pytest.mark.asyncio
    async def test_a_failing_catch_up_never_kills_the_socket_loop(
        self, wired, caplog
    ):
        handler, _, _ = wired
        listener = handler.client.message_listeners[0]
        with patch(
            "tigerharness.slack_bridge.__main__.run_catchup",
            AsyncMock(side_effect=RuntimeError("slack 500")),
        ):
            with caplog.at_level(logging.WARNING):
                await listener(handler.client, {"type": "hello"}, "{}")
        assert "NOT recovered" in caplog.text

    @pytest.mark.asyncio
    async def test_the_watchdog_is_started_and_stoppable(self):
        handler, bridge = make_handler(), make_bridge()
        wd = attach_reconnect_guards(
            handler, bridge, "Shohoku",
            WatchdogConfig(poll_interval_s=0.01), CatchupConfig(),
        )
        try:
            assert wd._thread is not None
            assert wd._thread.is_alive()
        finally:
            wd.stop()
            wd._thread.join(timeout=2.0)
        assert not wd._thread.is_alive()


class TestRunMultiTearsDownWatchdogs:
    @pytest.mark.asyncio
    async def test_watchdogs_stop_even_if_the_drive_raises(self, tmp_path):
        from tigerharness.slack_bridge.__main__ import _run_multi

        lane = MagicMock()
        lane.name = "Shohoku"
        lane.state_path = tmp_path / "threads.json"
        cfg = MagicMock()
        cfg.lanes = [lane]
        stopped = []

        wd = MagicMock()
        wd.stop.side_effect = lambda: stopped.append(True)

        with patch(
            "tigerharness.slack_bridge.__main__.build_team_bridge",
            return_value=make_bridge(),
        ), patch(
            "tigerharness.slack_bridge.__main__.AsyncSocketModeHandler",
            return_value=make_handler(),
        ), patch(
            "tigerharness.slack_bridge.__main__.attach_reconnect_guards",
            return_value=wd,
        ), patch(
            "tigerharness.slack_bridge.__main__._drive_handlers",
            AsyncMock(side_effect=RuntimeError("boom")),
        ):
            with pytest.raises(RuntimeError):
                await _run_multi(cfg)
        assert stopped == [True]

    @pytest.mark.asyncio
    async def test_startup_names_a_message_the_last_process_never_answered(
        self, tmp_path, capsys
    ):
        """The at-most-once trade is only honest if the dropped message
        is announced. Startup is where a stranded claim gets said."""
        from tigerharness.slack_bridge.__main__ import _run_multi

        lane = MagicMock()
        lane.name = "Shohoku"
        lane.state_path = tmp_path / "threads.json"
        cfg = MagicMock()
        cfg.lanes = [lane]

        bridge = make_bridge()
        bridge.seen_ledger.take_unfinished.return_value = [
            ("D0B4L5V7RFG", "1786900916.787969")
        ]

        with patch(
            "tigerharness.slack_bridge.__main__.build_team_bridge",
            return_value=bridge,
        ), patch(
            "tigerharness.slack_bridge.__main__.AsyncSocketModeHandler",
            return_value=make_handler(),
        ), patch(
            "tigerharness.slack_bridge.__main__.attach_reconnect_guards",
            return_value=MagicMock(),
        ), patch(
            "tigerharness.slack_bridge.__main__._drive_handlers", AsyncMock()
        ):
            try:
                # `_run_multi` reconfigures logging with force=True, which
                # evicts caplog's handler -- so read the stream it writes.
                await _run_multi(cfg)
            finally:
                # Re-bind to the REAL stderr: capsys closes its buffer at
                # teardown, and a root handler still pointing at it makes
                # every later test's first log raise on a closed file.
                logging.basicConfig(force=True, stream=sys.__stderr__)

        out = capsys.readouterr()
        logged = out.err + out.out
        assert "never answered" in logged
        assert "1786900916.787969" in logged
        assert "will NOT be retried" in logged


class TestCatchupInternals:
    """The bounds have to behave on the thread pass too, not just the
    channel pass -- both feed the same budget."""

    @pytest.mark.asyncio
    async def test_thread_pass_respects_candidate_age_and_budget(self, caplog):
        target = MagicMock()
        target.catchup_threads.return_value = [("D0X", "100.000000")]
        target.is_replay_candidate.side_effect = (
            lambda e: not e.get("bot_id")
        )
        target.replay = AsyncMock()

        ledger = MagicMock()
        ledger.channels.return_value = []
        ledger.watermark.return_value = None

        web = MagicMock()
        web.conversations_replies = AsyncMock(
            return_value={
                "messages": [
                    {"ts": "100.000001", "bot_id": "B1"},      # not a candidate
                    {"ts": "1.000000", "user": "U1"},           # too old
                    {"ts": "1000.000001", "user": "U1"},        # replayed
                    {"ts": "1000.000002", "user": "U1"},        # over budget
                ]
            }
        )
        with caplog.at_level(logging.WARNING):
            replayed = await run_catchup(
                target=target, ledger=ledger, web_client=web,
                cfg=CatchupConfig(max_age_s=60.0, max_messages=1),
                now=lambda: 1000.0,
            )
        assert replayed == 1
        assert target.replay.await_count == 1
        assert "older than" in caplog.text
        assert "1-message bound" in caplog.text

    def test_the_budget_complains_exactly_once(self, caplog):
        budget = _Budget(1, "Shohoku")
        assert budget.take() is True
        with caplog.at_level(logging.WARNING):
            assert budget.take() is False
            assert budget.take() is False
        assert caplog.text.count("bound") == 1

    def test_oldest_without_a_watermark_is_the_age_floor(self):
        assert _oldest(None, 1786900000.0) == "1786900000.000000"

    def test_oldest_prefers_whichever_is_newer(self):
        assert _oldest("1786900500.0", 1786900000.0) == "1786900500.0"
        assert _oldest("100.0", 1786900000.0) == "1786900000.000000"

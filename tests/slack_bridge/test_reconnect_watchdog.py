"""The liveness watchdog, and why it does not trust the event loop.

slack_sdk's own detector reported "disconnected for 909..1052 seconds"
against a threshold of 20. These tests pin the properties that make this
watchdog a different kind of thing: its clock is injected (not the
loop's), nothing it does can refresh its own liveness input, and it logs
its verdict before it needs the loop for anything.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from tigerharness.slack_bridge.reconnect import (
    CatchupConfig,
    SocketLivenessWatchdog,
    WatchdogConfig,
)


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def make_client():
    client = MagicMock()
    client.last_ping_pong_time = None
    client.connect_to_new_endpoint = AsyncMock()
    return client


class TestStaleDetection:
    def test_quiet_socket_is_declared_dead_in_seconds(self, caplog):
        clock = FakeClock()
        client = make_client()
        loop = MagicMock()
        loop.is_closed.return_value = False
        wd = SocketLivenessWatchdog(
            client, WatchdogConfig(stale_after_s=30.0), loop=loop, now=clock,
            lane="Shohoku",
        )
        clock.advance(29.0)
        assert wd.check_once() is False

        clock.advance(2.0)
        with caplog.at_level(logging.WARNING):
            assert wd.check_once() is True
        assert "forcing a Socket Mode reconnect" in caplog.text
        assert "lane=Shohoku" in caplog.text or "Shohoku" in caplog.text
        loop.call_soon_threadsafe.assert_called_once()

    def test_inbound_traffic_keeps_it_quiet(self):
        clock = FakeClock()
        wd = SocketLivenessWatchdog(
            make_client(), WatchdogConfig(stale_after_s=30.0),
            loop=MagicMock(), now=clock,
        )
        for _ in range(10):
            clock.advance(20.0)
            wd.mark_activity()
            assert wd.check_once() is False

    def test_a_pong_also_counts_as_alive(self):
        clock = FakeClock()
        client = make_client()
        wd = SocketLivenessWatchdog(
            client, WatchdogConfig(stale_after_s=30.0),
            loop=MagicMock(), now=clock,
        )
        clock.advance(100.0)
        client.last_ping_pong_time = clock.t - 1.0
        assert wd.check_once() is False

    def test_it_does_not_re_fire_every_poll(self):
        clock = FakeClock()
        loop = MagicMock()
        loop.is_closed.return_value = False
        wd = SocketLivenessWatchdog(
            make_client(),
            WatchdogConfig(stale_after_s=30.0, poll_interval_s=1.0),
            loop=loop, now=clock,
        )
        clock.advance(31.0)
        assert wd.check_once() is True
        for _ in range(20):
            clock.advance(1.0)
            wd.check_once()
        assert loop.call_soon_threadsafe.call_count == 1
        clock.advance(30.0)
        assert wd.check_once() is True
        assert loop.call_soon_threadsafe.call_count == 2


class TestItSurvivesWhatItWatches:
    def test_a_dead_loop_still_gets_a_log_line(self, caplog):
        """The whole point: the verdict is recorded even when the thing
        that would act on it is gone."""
        clock = FakeClock()
        loop = MagicMock()
        loop.is_closed.return_value = True
        wd = SocketLivenessWatchdog(
            make_client(), WatchdogConfig(stale_after_s=30.0),
            loop=loop, now=clock,
        )
        clock.advance(31.0)
        with caplog.at_level(logging.WARNING):
            assert wd.check_once() is True
        assert "forcing a Socket Mode reconnect" in caplog.text
        assert "no live event loop" in caplog.text
        loop.call_soon_threadsafe.assert_not_called()

    def test_no_loop_at_all_is_survivable(self, caplog):
        clock = FakeClock()
        wd = SocketLivenessWatchdog(
            make_client(), WatchdogConfig(stale_after_s=30.0),
            loop=None, now=clock,
        )
        clock.advance(31.0)
        with caplog.at_level(logging.WARNING):
            assert wd.check_once() is True
        assert "no live event loop" in caplog.text

    def test_a_loop_that_refuses_the_call_is_logged(self, caplog):
        clock = FakeClock()
        loop = MagicMock()
        loop.is_closed.return_value = False
        loop.call_soon_threadsafe.side_effect = RuntimeError("loop closed")
        wd = SocketLivenessWatchdog(
            make_client(), WatchdogConfig(stale_after_s=30.0),
            loop=loop, now=clock,
        )
        clock.advance(31.0)
        with caplog.at_level(logging.WARNING):
            assert wd.check_once() is True
        assert "could not reach the event loop" in caplog.text


class TestReconnectScheduling:
    @pytest.mark.asyncio
    async def test_scheduling_actually_reconnects(self):
        client = make_client()
        wd = SocketLivenessWatchdog(client, WatchdogConfig())
        wd._schedule_reconnect()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        client.connect_to_new_endpoint.assert_awaited_once_with(force=True)

    @pytest.mark.asyncio
    async def test_a_failing_reconnect_is_logged_not_raised(self, caplog):
        client = make_client()
        client.connect_to_new_endpoint = AsyncMock(
            side_effect=RuntimeError("network still down")
        )
        wd = SocketLivenessWatchdog(client, WatchdogConfig())
        with caplog.at_level(logging.WARNING):
            await wd._reconnect()
        assert "raised" in caplog.text


class TestThreadLifecycle:
    @pytest.mark.asyncio
    async def test_start_runs_checks_on_its_own_thread(self):
        client = make_client()
        seen: list[str] = []

        class Recording(SocketLivenessWatchdog):
            def check_once(self) -> bool:
                seen.append(threading.current_thread().name)
                return False

        wd = Recording(
            client, WatchdogConfig(stale_after_s=30.0, poll_interval_s=0.01)
        )
        wd.start()
        deadline = time.time() + 2.0
        while not seen and time.time() < deadline:
            await asyncio.sleep(0.01)
        wd.stop()
        assert seen, "watchdog thread never ran a check"
        assert seen[0] != threading.current_thread().name

    @pytest.mark.asyncio
    async def test_a_raising_check_does_not_kill_the_thread(self, caplog):
        calls: list[int] = []

        class Exploding(SocketLivenessWatchdog):
            def check_once(self) -> bool:
                calls.append(1)
                raise RuntimeError("boom")

        wd = Exploding(
            make_client(),
            WatchdogConfig(stale_after_s=30.0, poll_interval_s=0.01),
        )
        with caplog.at_level(logging.WARNING):
            wd.start()
            deadline = time.time() + 2.0
            while len(calls) < 3 and time.time() < deadline:
                await asyncio.sleep(0.01)
            wd.stop()
        assert len(calls) >= 3
        assert "check raised" in caplog.text

    def test_an_injected_loop_is_kept(self):
        """The lane wiring hands the watchdog its own loop; ``start``
        must not go looking for a different one."""
        loop = MagicMock()
        wd = SocketLivenessWatchdog(
            make_client(), WatchdogConfig(poll_interval_s=0.01), loop=loop
        )
        wd.start()
        try:
            assert wd._loop is loop
        finally:
            wd.stop()

    def test_disabled_watchdog_starts_nothing_and_says_why(self, caplog):
        wd = SocketLivenessWatchdog(
            make_client(), WatchdogConfig(enabled=False)
        )
        with caplog.at_level(logging.WARNING):
            wd.start()
        assert "disabled" in caplog.text
        assert wd._thread is None


class TestConfig:
    def test_defaults_are_the_safe_ones(self):
        wd = WatchdogConfig.from_env({})
        assert wd.enabled is True
        assert wd.stale_after_s == 30.0
        c = CatchupConfig.from_env({})
        assert c.enabled is True
        assert c.max_age_s == 3600.0
        assert c.max_messages == 50

    def test_env_overrides(self):
        wd = WatchdogConfig.from_env(
            {
                "TIGERHARNESS_SLACK_WATCHDOG": "off",
                "TIGERHARNESS_SLACK_WATCHDOG_STALE_S": "12",
                "TIGERHARNESS_SLACK_WATCHDOG_POLL_S": "3",
            }
        )
        assert wd == WatchdogConfig(False, 12.0, 3.0)
        c = CatchupConfig.from_env(
            {
                "TIGERHARNESS_SLACK_CATCHUP": "yes",
                "TIGERHARNESS_SLACK_CATCHUP_MAX_AGE_S": "7200",
                "TIGERHARNESS_SLACK_CATCHUP_MAX_MESSAGES": "9",
            }
        )
        assert c == CatchupConfig(True, 7200.0, 9)

    def test_a_junk_number_falls_back_loudly(self, caplog):
        with caplog.at_level(logging.WARNING):
            wd = WatchdogConfig.from_env(
                {"TIGERHARNESS_SLACK_WATCHDOG_STALE_S": "soon"}
            )
        assert wd.stale_after_s == 30.0
        assert "is not a number" in caplog.text

    def test_from_env_reads_the_process_environment(self, monkeypatch):
        monkeypatch.setenv("TIGERHARNESS_SLACK_WATCHDOG_STALE_S", "45")
        assert WatchdogConfig.from_env().stale_after_s == 45.0
        monkeypatch.setenv("TIGERHARNESS_SLACK_CATCHUP_MAX_MESSAGES", "7")
        assert CatchupConfig.from_env().max_messages == 7

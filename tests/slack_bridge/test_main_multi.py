"""Tests for the multi-lane orchestrator in slack_bridge/__main__.py.

Covers ``_LaneFilter``, ``_setup_logging``, ``_drive_handlers`` (with
lane_names), ``_run_multi``, and ``main()`` env-var dispatch.

Single-tenant tests live in test_main_coverage.py / test_main_extra.py.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal as sig_mod
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tigerharness.slack_bridge.__main__ import (
    _LaneFilter,
    _drive_handlers,
    _lane_var,
    _run_multi,
    _setup_logging,
    main,
)
from tigerharness.slack_bridge.config import BridgeConfig
from tigerharness.slack_bridge.multi import LaneConfig, MultiBridgeConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_lane(name: str, *, app_token: str = "xapp-x") -> LaneConfig:
    return LaneConfig(
        name=name,
        bridge_cfg=BridgeConfig(
            slack_app_token=app_token,
            slack_bot_token=f"xoxb-{name}",
            allowed_user_ids=frozenset({"U0CEO"}),
            agent_cwd=f"/tmp/{name}",
        ),
        state_path=Path(f"/tmp/state-{name}/threads.json"),
    )


def _mock_bridge() -> MagicMock:
    """A bridge mock with the surface _drive_handlers and _run_multi touch."""
    b = MagicMock()
    b.app = MagicMock()
    b.request_shutdown = MagicMock()
    b.wait_for_drain = AsyncMock(return_value=True)
    return b


def _mock_handler() -> AsyncMock:
    """A handler that blocks in start_async() until something cancels it."""
    h = AsyncMock()
    async def blocking_start():
        await asyncio.sleep(60)
    h.start_async = blocking_start
    h.close_async = AsyncMock()
    return h


# ---------------------------------------------------------------------------
# _LaneFilter
# ---------------------------------------------------------------------------

class TestLaneFilter:
    def test_adds_lane_attribute_from_contextvar(self):
        flt = _LaneFilter()
        rec = logging.LogRecord(
            name="x", level=logging.INFO, pathname="", lineno=0,
            msg="hi", args=(), exc_info=None,
        )
        # Default contextvar is "" so attribute is "".
        assert flt.filter(rec) is True
        assert rec.lane == ""

    def test_reads_contextvar_when_set(self):
        flt = _LaneFilter()
        token = _lane_var.set("shohoku")
        try:
            rec = logging.LogRecord(
                name="x", level=logging.INFO, pathname="", lineno=0,
                msg="hi", args=(), exc_info=None,
            )
            flt.filter(rec)
            assert rec.lane == "shohoku"
        finally:
            _lane_var.reset(token)


# ---------------------------------------------------------------------------
# _setup_logging
# ---------------------------------------------------------------------------

class TestSetupLogging:
    def test_multi_format_includes_lane(self, capsys: pytest.CaptureFixture):
        """Multi mode emits records formatted with `lane=<value>`."""
        _setup_logging(multi=True)
        try:
            token = _lane_var.set("shohoku")
            try:
                logging.getLogger("tigerharness.slack_bridge").info("hello")
            finally:
                _lane_var.reset(token)
            captured = capsys.readouterr()
            # logging.StreamHandler defaults to stderr.
            output = captured.err + captured.out
            assert "lane=shohoku" in output
            assert "hello" in output
        finally:
            # Reset to default config so subsequent tests aren't affected.
            _setup_logging(multi=False)

    def test_single_format_omits_lane(self, capsys: pytest.CaptureFixture):
        """Single mode keeps the legacy format -- no lane= clutter."""
        _setup_logging(multi=False)
        logging.getLogger("tigerharness.slack_bridge").info("hello")
        captured = capsys.readouterr()
        output = captured.err + captured.out
        assert "lane=" not in output
        assert "hello" in output


# ---------------------------------------------------------------------------
# _drive_handlers with lane_names (multi-lane behavior)
# ---------------------------------------------------------------------------

class TestDriveHandlersMulti:
    @pytest.mark.asyncio
    async def test_sigterm_drains_all_lanes_concurrently(self):
        """Two lanes, SIGTERM, both bridges drain + both handlers close."""
        bridges = [_mock_bridge() for _ in range(2)]
        handlers = [_mock_handler() for _ in range(2)]
        lane_names = ["shohoku", "tigers"]

        async def send_sigterm():
            await asyncio.sleep(0.05)
            os.kill(os.getpid(), sig_mod.SIGTERM)

        sig_task = asyncio.create_task(send_sigterm())
        await _drive_handlers(handlers, bridges, lane_names=lane_names)
        await sig_task

        for b in bridges:
            b.request_shutdown.assert_called_once()
            b.wait_for_drain.assert_awaited_once()
        for h in handlers:
            h.close_async.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_drain_timeout_in_one_lane_still_proceeds(self, caplog):
        """One lane's wait_for_drain returns False (timeout). Shutdown
        proceeds; warning is logged. Other lane unaffected."""
        bridges = [_mock_bridge(), _mock_bridge()]
        bridges[1].wait_for_drain = AsyncMock(return_value=False)
        handlers = [_mock_handler(), _mock_handler()]
        lane_names = ["a", "b"]

        async def send_sigterm():
            await asyncio.sleep(0.05)
            os.kill(os.getpid(), sig_mod.SIGTERM)

        sig_task = asyncio.create_task(send_sigterm())
        with caplog.at_level("WARNING", logger="tigerharness.slack_bridge"):
            await _drive_handlers(handlers, bridges, lane_names=lane_names)
        await sig_task

        # Warning logged exactly once across the whole drain.
        assert any(
            "drain timed out" in rec.message for rec in caplog.records
        ), [r.message for r in caplog.records]
        # Both handlers still closed.
        for h in handlers:
            h.close_async.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_lane_contextvar_set_per_task(self):
        """When lane_names is passed, each handler runs inside a task
        where the lane contextvar is set to the right name."""
        observed: list[str] = []

        async def recording_start():
            # Read the contextvar from within the handler task.
            observed.append(_lane_var.get())
            await asyncio.sleep(60)

        handlers = []
        for _ in range(2):
            h = AsyncMock()
            h.start_async = recording_start
            h.close_async = AsyncMock()
            handlers.append(h)
        bridges = [_mock_bridge() for _ in range(2)]

        async def send_sigterm():
            await asyncio.sleep(0.1)
            os.kill(os.getpid(), sig_mod.SIGTERM)

        sig_task = asyncio.create_task(send_sigterm())
        await _drive_handlers(handlers, bridges, lane_names=["shohoku", "tigers"])
        await sig_task

        assert sorted(observed) == ["shohoku", "tigers"]


# ---------------------------------------------------------------------------
# _run_multi
# ---------------------------------------------------------------------------

class TestRunMulti:
    @pytest.mark.asyncio
    async def test_builds_one_bridge_handler_per_lane(self):
        """`_run_multi` calls build_bridge once per lane (with the right
        cfg + state_path) and AsyncSocketModeHandler once per lane (with
        the right app_token)."""
        multi_cfg = MultiBridgeConfig(lanes=(
            _make_lane("shohoku", app_token="xapp-1"),
            _make_lane("tigers", app_token="xapp-2"),
        ))
        built_bridges = [_mock_bridge() for _ in range(2)]
        built_handlers = [_mock_handler() for _ in range(2)]

        async def send_sigterm():
            await asyncio.sleep(0.05)
            os.kill(os.getpid(), sig_mod.SIGTERM)

        with patch(
            "tigerharness.slack_bridge.__main__.build_bridge",
            side_effect=built_bridges,
        ) as build_bridge_mock, patch(
            "tigerharness.slack_bridge.__main__.AsyncSocketModeHandler",
            side_effect=built_handlers,
        ) as handler_mock:
            sig_task = asyncio.create_task(send_sigterm())
            await _run_multi(multi_cfg)
            await sig_task

        assert build_bridge_mock.call_count == 2
        # state_path was passed per-lane.
        passed_paths = [
            call.kwargs.get("state_path") for call in build_bridge_mock.call_args_list
        ]
        assert passed_paths == [
            Path("/tmp/state-shohoku/threads.json"),
            Path("/tmp/state-tigers/threads.json"),
        ]
        # Handler constructed with each lane's app_token.
        passed_tokens = [
            call.args[1] for call in handler_mock.call_args_list
        ]
        assert passed_tokens == ["xapp-1", "xapp-2"]


# ---------------------------------------------------------------------------
# main() dispatch
# ---------------------------------------------------------------------------

class TestMainDispatch:
    def test_dispatches_to_multi_when_bridges_config_set(
        self, monkeypatch, tmp_path: Path
    ):
        idx = tmp_path / "slack-bridge.yaml"
        idx.write_text("lanes:\n  - dummy\n")
        monkeypatch.setenv("TIGERHARNESS_BRIDGES_CONFIG", str(idx))

        fake_multi_cfg = MultiBridgeConfig(lanes=(_make_lane("dummy"),))
        with patch(
            "tigerharness.slack_bridge.__main__.load_multi",
            return_value=fake_multi_cfg,
        ) as load_multi_mock, patch(
            "tigerharness.slack_bridge.__main__._run_multi",
            new_callable=AsyncMock,
        ) as run_multi_mock, patch(
            "tigerharness.slack_bridge.__main__._run_single",
            new_callable=AsyncMock,
        ) as run_single_mock:
            main()

        load_multi_mock.assert_called_once_with(Path(str(idx)))
        run_multi_mock.assert_awaited_once_with(fake_multi_cfg)
        run_single_mock.assert_not_awaited()

    def test_dispatches_to_single_when_bridges_config_unset(
        self, monkeypatch
    ):
        monkeypatch.delenv("TIGERHARNESS_BRIDGES_CONFIG", raising=False)
        with patch(
            "tigerharness.slack_bridge.__main__._run_single",
            new_callable=AsyncMock,
        ) as run_single_mock, patch(
            "tigerharness.slack_bridge.__main__._run_multi",
            new_callable=AsyncMock,
        ) as run_multi_mock:
            main()
        run_single_mock.assert_awaited_once()
        run_multi_mock.assert_not_awaited()

    def test_dispatches_to_single_when_bridges_config_empty_string(
        self, monkeypatch
    ):
        """Whitespace-only env var is treated the same as unset."""
        monkeypatch.setenv("TIGERHARNESS_BRIDGES_CONFIG", "   ")
        with patch(
            "tigerharness.slack_bridge.__main__._run_single",
            new_callable=AsyncMock,
        ) as run_single_mock:
            main()
        run_single_mock.assert_awaited_once()

    def test_keyboard_interrupt_in_multi_path(self, monkeypatch, tmp_path: Path):
        idx = tmp_path / "slack-bridge.yaml"
        idx.write_text("lanes:\n  - dummy\n")
        monkeypatch.setenv("TIGERHARNESS_BRIDGES_CONFIG", str(idx))
        with patch(
            "tigerharness.slack_bridge.__main__.load_multi",
            return_value=MultiBridgeConfig(lanes=(_make_lane("dummy"),)),
        ), patch(
            "tigerharness.slack_bridge.__main__.asyncio.run",
            side_effect=KeyboardInterrupt(),
        ):
            main()  # Should not raise.

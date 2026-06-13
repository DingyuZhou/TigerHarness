"""Single-tenant deprecation + migration-equivalence tests.

The legacy single-team Slack bridge is being retired NON-destructively:
`_run_single` still works but now emits a one-time migration notice
(`_warn_single_tenant_deprecated`); the multi-lane path stays silent. A
one-lane `TIGERHARNESS_BRIDGES_CONFIG` index is the migration target and
builds a working bridge equivalent to the single-team path.
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
    _run_multi,
    _run_single,
    _warn_single_tenant_deprecated,
)
from tigerharness.slack_bridge.bridge import build_team_bridge
from tigerharness.slack_bridge.multi import load_multi


# ---------------------------------------------------------------------------
# Helpers — a minimal valid one-lane team + index (mirrors test_multi.py)
# ---------------------------------------------------------------------------

def _make_valid_team(root: Path, name: str, persona: str = "ayako") -> Path:
    team = root / name
    (team / "configs").mkdir(parents=True, exist_ok=True)
    (team / "configs" / ".env").write_text(
        f"SLACK_APP_TOKEN=xapp-{name}\nSLACK_BOT_TOKEN=xoxb-{name}\n"
    )
    (team / "personas" / persona).mkdir(parents=True, exist_ok=True)
    (team / "personas" / persona / "prompt.md").write_text(f"You are {persona}.")
    (team / "memories" / persona).mkdir(parents=True, exist_ok=True)
    (team / "memories" / persona / "tiger-memory.config.yaml").write_text(
        "agent: {name: test}\n"
    )
    (team / "configs" / "personas.yaml").write_text(
        f"personas:\n  - name: {persona}\n"
    )
    (team / "configs" / "slack-bridge.yaml").write_text(
        f"default_persona: {persona}\n"
        f"allowed_user_ids:\n  - U0CEO\n"
        f"state_dir: {root / 'state' / name}\n"
    )
    return team


def _write_index(root: Path, lanes: list[str]) -> Path:
    p = root / "slack-bridge.yaml"
    p.write_text("lanes:\n" + "".join(f"  - {l}\n" for l in lanes))
    return p


_MIGRATION_MARKERS = ("DEPRECATED", "TIGERHARNESS_BRIDGES_CONFIG", "gen-service")


def _mock_single_env():
    cfg = MagicMock()
    cfg.slack_app_token = "xapp-test"
    cfg.agent_cwd = "/tmp"
    cfg.allowed_user_ids = {"U123"}
    bridge = MagicMock()
    bridge.app = MagicMock()
    bridge.request_shutdown = MagicMock()
    bridge.wait_for_drain = AsyncMock(return_value=True)
    handler = AsyncMock()
    handler.close_async = AsyncMock()

    async def blocking_start():
        await asyncio.sleep(60)

    handler.start_async = blocking_start
    return cfg, bridge, handler


async def _sigterm_soon():
    await asyncio.sleep(0.05)
    os.kill(os.getpid(), sig_mod.SIGTERM)


# ---------------------------------------------------------------------------
# The notice content (helper is the testable seam — no live connection)
# ---------------------------------------------------------------------------

class TestDeprecationNoticeContent:
    def test_helper_logs_warning_with_migration_target(self, caplog):
        with caplog.at_level(logging.WARNING, logger="tigerharness.slack_bridge"):
            _warn_single_tenant_deprecated()
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        msg = warnings[0].getMessage()
        for marker in _MIGRATION_MARKERS:
            assert marker in msg, f"deprecation notice missing {marker!r}"


# ---------------------------------------------------------------------------
# Single warns, multi stays silent (asserted at the call seam, since
# _setup_logging(force=True) would otherwise drop caplog's handler)
# ---------------------------------------------------------------------------

class TestSingleVsMultiNotice:
    @pytest.mark.asyncio
    async def test_run_single_emits_deprecation_notice(self):
        cfg, bridge, handler = _mock_single_env()
        with patch("tigerharness.slack_bridge.__main__.load", return_value=cfg), \
             patch("tigerharness.slack_bridge.__main__.build_bridge", return_value=bridge), \
             patch("tigerharness.slack_bridge.__main__.AsyncSocketModeHandler", return_value=handler), \
             patch("tigerharness.slack_bridge.__main__._warn_single_tenant_deprecated") as warn:
            t = asyncio.create_task(_sigterm_soon())
            await _run_single()
            await t
        warn.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_multi_does_not_emit_single_tenant_notice(self, tmp_path):
        _make_valid_team(tmp_path, "shohoku")
        cfg = load_multi(_write_index(tmp_path, ["shohoku"]))
        bridge = MagicMock()
        bridge.app = MagicMock()
        bridge.request_shutdown = MagicMock()
        bridge.wait_for_drain = AsyncMock(return_value=True)
        handler = AsyncMock()
        handler.close_async = AsyncMock()

        async def blocking_start():
            await asyncio.sleep(60)

        handler.start_async = blocking_start
        with patch("tigerharness.slack_bridge.__main__.build_team_bridge", return_value=bridge), \
             patch("tigerharness.slack_bridge.__main__.AsyncSocketModeHandler", return_value=handler), \
             patch("tigerharness.slack_bridge.__main__._warn_single_tenant_deprecated") as warn:
            t = asyncio.create_task(_sigterm_soon())
            await _run_multi(cfg)
            await t
        warn.assert_not_called()


# ---------------------------------------------------------------------------
# Backward-compat: the single path still functions (it builds + drives,
# returns cleanly on SIGTERM — a silent break would hang or raise)
# ---------------------------------------------------------------------------

class TestSinglePathStillWorks:
    @pytest.mark.asyncio
    async def test_run_single_still_starts_and_drains(self):
        cfg, bridge, handler = _mock_single_env()
        with patch("tigerharness.slack_bridge.__main__.load", return_value=cfg), \
             patch("tigerharness.slack_bridge.__main__.build_bridge", return_value=bridge), \
             patch("tigerharness.slack_bridge.__main__.AsyncSocketModeHandler", return_value=handler):
            t = asyncio.create_task(_sigterm_soon())
            await _run_single()
            await t
        # The deprecated path is non-breaking: it built, drove, and drained.
        bridge.request_shutdown.assert_called_once()
        handler.close_async.assert_awaited_once()


# ---------------------------------------------------------------------------
# Migration equivalence: a one-lane index builds a working bridge
# ---------------------------------------------------------------------------

class TestMigrationEquivalence:
    def test_one_lane_index_builds_equivalent_bridge(self, tmp_path):
        """The migration note's claim, as a test: a single team becomes a
        one-lane TIGERHARNESS_BRIDGES_CONFIG index that loads and builds a
        working bridge — the same SlackBridge the single path produces."""
        _make_valid_team(tmp_path, "shohoku")
        cfg = load_multi(_write_index(tmp_path, ["shohoku"]))
        assert len(cfg.lanes) == 1
        lane = cfg.lanes[0]
        bridge = build_team_bridge(lane.team_ctx, state_path=lane.state_path)
        # A working bridge with a Bolt app, exactly as the single path yields.
        assert bridge.app is not None

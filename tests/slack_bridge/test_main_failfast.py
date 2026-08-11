"""Fail-fast startup + migration-target tests (ADR 0009).

The single-tenant env fallback is gone: ``main()`` without
``TIGERHARNESS_BRIDGES_CONFIG`` must exit with a migration pointer, not
silently run a one-off deployment shape. The migration target — a
one-lane index — must build a working bridge.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from tigerharness.slack_bridge.__main__ import main
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


# The stranded user's breadcrumbs: what happened, where to migrate, how.
_MIGRATION_MARKERS = (
    "REMOVED",
    "ADR 0009",
    "TIGERHARNESS_BRIDGES_CONFIG",
    "gen-service",
    "docs/slack-bridge.md",
)


# ---------------------------------------------------------------------------
# main() fails fast without the index (no silent fallback)
# ---------------------------------------------------------------------------

class TestMainFailsFastWithoutIndex:
    def test_unset_index_exits_with_migration_pointer(self, monkeypatch):
        monkeypatch.delenv("TIGERHARNESS_BRIDGES_CONFIG", raising=False)
        with pytest.raises(SystemExit) as exc_info:
            main()
        msg = str(exc_info.value)
        for marker in _MIGRATION_MARKERS:
            assert marker in msg, f"fail-fast message missing {marker!r}"

    def test_blank_index_treated_as_unset(self, monkeypatch):
        # Whitespace-only value is stripped -> same fail-fast, not a
        # confusing "file not found" on a blank path.
        monkeypatch.setenv("TIGERHARNESS_BRIDGES_CONFIG", "   ")
        with pytest.raises(SystemExit, match="ADR 0009"):
            main()


# ---------------------------------------------------------------------------
# Migration equivalence: a one-lane index builds a working bridge
# ---------------------------------------------------------------------------

class TestMigrationEquivalence:
    def test_one_lane_index_builds_working_bridge(self, tmp_path):
        """The migration note's claim, as a test: a single team becomes a
        one-lane TIGERHARNESS_BRIDGES_CONFIG index that loads and builds a
        working bridge with a Bolt app."""
        _make_valid_team(tmp_path, "shohoku")
        cfg = load_multi(_write_index(tmp_path, ["shohoku"]))
        assert len(cfg.lanes) == 1
        lane = cfg.lanes[0]
        bridge = build_team_bridge(lane.team_ctx, state_path=lane.state_path)
        assert bridge.app is not None

    def test_main_runs_the_index_when_set(self, monkeypatch, tmp_path):
        _make_valid_team(tmp_path, "shohoku")
        idx = _write_index(tmp_path, ["shohoku"])
        monkeypatch.setenv("TIGERHARNESS_BRIDGES_CONFIG", str(idx))
        with patch(
            "tigerharness.slack_bridge.__main__._run_multi",
            new_callable=AsyncMock,
        ) as run_multi_mock:
            main()
        run_multi_mock.assert_awaited_once()
        (called_cfg,) = run_multi_mock.await_args.args
        assert [lane.name for lane in called_cfg.lanes] == ["shohoku"]

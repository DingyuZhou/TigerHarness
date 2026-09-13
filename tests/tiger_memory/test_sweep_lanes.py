"""Lane restriction + own-only mode of the team sweep gate (ADR 0012 part 2)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from textwrap import dedent

from tigerharness.tiger_memory.cli import main
from tigerharness.tiger_memory.sweep import (
    lane_members,
    maybe_sweep_roster,
    plan_team_sweep,
    read_sweep_state,
    try_claim_sweep,
    write_sweep_state,
)

NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)

_ROSTER = """\
default_persona: Ayako
default_vendor: claude
personas:
  - name: Ayako
  - name: Anzai
  - name: Rukawa
    vendor: chatgpt
    model: gpt-6-astra
  - name: Mitsui
    vendor: chatgpt
    model: gpt-6-astra
"""


def _team(tmp_path: Path, roster: str = _ROSTER) -> Path:
    (tmp_path / "configs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "configs" / "personas.yaml").write_text(roster)
    mem = tmp_path / "memories"
    for name in ("Ayako", "Anzai", "Rukawa", "Mitsui"):
        (mem / name).mkdir(parents=True, exist_ok=True)
        (mem / name / "tiger-memory.config.yaml").write_text("store: {root: .}\n")
    return mem


class TestLaneMembers:
    def test_groups_by_policy(self, tmp_path: Path) -> None:
        mem = _team(tmp_path)
        assert lane_members(mem, "Ayako") == {"Ayako", "Anzai"}
        assert lane_members(mem, "Rukawa") == {"Rukawa", "Mitsui"}
        # An unknown name resolves to the team default lane.
        assert lane_members(mem, "Nobody") == {"Ayako", "Anzai"}

    def test_no_roster_or_bad_shape_means_no_restriction(self, tmp_path: Path) -> None:
        assert lane_members(tmp_path / "memories", "Ayako") is None
        mem = _team(tmp_path, "default_vendor: claude\npersonas: {}\n")
        assert lane_members(mem, "Ayako") is None
        mem = _team(tmp_path, "personas:\n  - 3\n  - name: ''\n  - name: Ayako\n")
        assert lane_members(mem, "Ayako") == {"Ayako"}

    def test_malformed_vendor_logs_and_lifts_restriction(self, tmp_path: Path, caplog) -> None:
        mem = _team(tmp_path, "personas:\n  - name: Ayako\n    vendor: gemini\n")
        with caplog.at_level(logging.WARNING, logger="tigerharness.tiger_memory.sweep"):
            assert lane_members(mem, "Ayako") is None
        assert "lane restriction unavailable" in caplog.text


class TestPlanAllowed:
    def test_allowed_filters_the_others_only(self, tmp_path: Path) -> None:
        mem = _team(tmp_path)
        plan = plan_team_sweep(mem, max_personas=None, own_persona="Rukawa", allowed={"Rukawa", "Mitsui"})
        assert [t.name for t in plan.targets] == ["Rukawa", "Mitsui"]
        assert plan.remaining == 0 and plan.all_personas == 4
        # The own persona is never filtered, even when not in `allowed`.
        plan = plan_team_sweep(mem, max_personas=None, own_persona="Rukawa", allowed={"Ayako"})
        assert [t.name for t in plan.targets] == ["Rukawa", "Ayako"]
        # The cap and `remaining` count allowed others only.
        plan = plan_team_sweep(mem, max_personas=1, allowed={"Ayako", "Anzai", "Mitsui"})
        assert len(plan.targets) == 1 and plan.remaining == 2


class TestForceOwnOnly:
    def test_own_pending_claims_own_only_even_when_team_due(self, tmp_path: Path) -> None:
        write_sweep_state(tmp_path, {"last_sweep_at": (NOW - timedelta(days=9)).isoformat()})
        res = try_claim_sweep(tmp_path, now=NOW, token="A", own_persona="Rukawa", own_pending=True, force_own_only=True)
        assert res.claimed and res.scope == "own-only"
        assert read_sweep_state(tmp_path)["own_persona"] == "Rukawa"

    def test_no_pending_is_not_due_even_when_team_due(self, tmp_path: Path) -> None:
        write_sweep_state(tmp_path, {"last_sweep_at": (NOW - timedelta(days=9)).isoformat()})
        res = try_claim_sweep(tmp_path, now=NOW, token="A", own_persona="Rukawa", own_pending=False, force_own_only=True)
        assert (res.claimed, res.reason) == (False, "not_due")

    def test_maybe_sweep_roster_passes_both_through(self, tmp_path: Path) -> None:
        mem = _team(tmp_path)
        write_sweep_state(mem, {"last_sweep_at": (NOW - timedelta(days=9)).isoformat()})
        d = maybe_sweep_roster(
            mem, now=NOW, token="A", max_personas=None, own_persona="Rukawa",
            own_pending=True, allowed={"Rukawa", "Mitsui"}, force_own_only=True,
        )
        assert d.ran and d.scope == "own-only"
        assert [t.name for t in d.plan.targets] == ["Rukawa"]
        write_sweep_state(mem, {"last_sweep_at": (NOW - timedelta(days=9)).isoformat()})
        d = maybe_sweep_roster(
            mem, now=NOW, token="B", max_personas=None, own_persona="Rukawa",
            own_pending=True, allowed={"Rukawa", "Mitsui"},
        )
        assert d.scope == "team"
        assert [t.name for t in d.plan.targets] == ["Rukawa", "Mitsui"]


class TestCli:
    def _cfg(self, tmp_path: Path) -> str:
        mem = _team(tmp_path)
        cfg_path = tmp_path / "cfg.yaml"
        cfg_path.write_text(dedent(f"""\
            agent: {{name: Rukawa, role: dev}}
            store: {{root: {mem}/Rukawa}}
            sources:
              - kind: claude_code
                project_path: {tmp_path}/proj
            summarizer: {{backend: anthropic, model: m, prompts: default/v1}}
        """))
        return str(cfg_path)

    def test_lane_of_restricts_and_reports(self, tmp_path: Path, capsys) -> None:
        cfg = self._cfg(tmp_path)
        rc = main(["--config", cfg, "sweep-plan", "--token", "T", "--max-personas", "9", "--lane-of", "Rukawa"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["ran"] and out["scope"] == "team"
        assert out["lane"] == {"of": "Rukawa", "members": ["Mitsui", "Rukawa"]}
        assert out["own_only"] is False
        assert [t["name"] for t in out["targets"]] == ["Rukawa", "Mitsui"]

    def test_without_lane_of_reports_null(self, tmp_path: Path, capsys) -> None:
        cfg = self._cfg(tmp_path)
        rc = main(["--config", cfg, "sweep-plan", "--token", "T"])
        assert rc == 0
        assert json.loads(capsys.readouterr().out)["lane"] is None

    def test_own_only_needs_own_persona(self, tmp_path: Path, capsys) -> None:
        cfg = self._cfg(tmp_path)
        rc = main(["--config", cfg, "sweep-plan", "--own-only"])
        assert rc == 2
        assert "--own-only needs --own-persona" in capsys.readouterr().err

    def test_own_only_with_nothing_pending_is_not_due(self, tmp_path: Path, capsys) -> None:
        cfg = self._cfg(tmp_path)
        rc = main(["--config", cfg, "sweep-plan", "--own-persona", "Rukawa", "--own-only", "--token", "T"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["ran"] is False and out["reason"] == "not_due" and out["own_only"] is True

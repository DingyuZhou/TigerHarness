"""Tests for ``journal.lanes`` -- who owns a unit of work and which
vendor/model lane it must run on (ADR 0012)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tigerharness.journal import lanes, walk
from tigerharness.journal.models import CompilePhase, State, Status
from tigerharness.journal.paths import JournalPaths
from tigerharness.vendors import ModelPolicy

from tests.journal.test_cli import _seed_workflow_graph, _sf


ROSTER = """\
default_persona: Ayako
default_vendor: claude
default_model: claude-opus-5
personas:
  - name: Ayako
  - name: Akagi
  - name: Rukawa
    vendor: chatgpt
    model: gpt-6-astra
"""


@pytest.fixture
def team(tmp_path: Path) -> Path:
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "personas.yaml").write_text(ROSTER)
    return tmp_path


@pytest.fixture
def paths(team: Path) -> JournalPaths:
    p = JournalPaths(root=team / "journal")
    p.ensure()
    return p


class TestLane:
    def test_key_and_from_policy(self) -> None:
        lane = lanes.Lane.from_policy(ModelPolicy("chatgpt", "codex_exec", "gpt-6-astra"))
        assert lane.key == "chatgpt/gpt-6-astra"
        assert lanes.Lane("claude", "claude_p", None).key == "claude"
        assert lane == lanes.Lane("chatgpt", "codex_exec", "gpt-6-astra")  # hashable/equal

    def test_team_root_for(self, team: Path, tmp_path: Path) -> None:
        assert lanes.team_root_for(team / "journal") == team
        assert lanes.team_root_for(tmp_path / "elsewhere" / "journal") is None

    def test_lane_of(self, team: Path) -> None:
        assert lanes.lane_of(team, "Rukawa").key == "chatgpt/gpt-6-astra"
        assert lanes.lane_of(team, "Ayako").key == "claude/claude-opus-5"
        assert lanes.lane_of(team, None).key == "claude/claude-opus-5"
        assert lanes.lane_of(None, "anyone").key == "claude"


class TestLaneCheck:
    def test_same_and_different(self, team: Path) -> None:
        ok = lanes.lane_check(team, "Ayako", "Akagi")
        assert ok.same and ok.driver_lane == ok.owner_lane
        bad = lanes.lane_check(team, "Ayako", "Rukawa")
        assert not bad.same
        msg = bad.refusal("t1")
        assert "belongs to Rukawa on lane chatgpt/gpt-6-astra" in msg
        assert "this drive runs Ayako on lane claude/claude-opus-5" in msg
        assert "--any-lane" in msg

    def test_no_owner_means_team_default(self, team: Path) -> None:
        check = lanes.lane_check(team, "Rukawa", None)
        assert not check.same
        assert "the team default persona" in check.refusal("t1")

    def test_no_team_root_or_driver_is_always_same(self, tmp_path: Path) -> None:
        assert lanes.lane_check(None, "Ayako", "Rukawa").same
        assert lanes.lane_check(tmp_path, None, "Rukawa").same

    def test_malformed_vendor_raises(self, tmp_path: Path) -> None:
        (tmp_path / "configs").mkdir()
        (tmp_path / "configs" / "personas.yaml").write_text(
            "personas:\n  - name: X\n    vendor: gemini\n"
        )
        with pytest.raises(ValueError, match="unknown model vendor"):
            lanes.lane_check(tmp_path, "X", "X")


class TestWorkOwner:
    def test_task_owner_is_the_assigned_persona(self, paths: JournalPaths) -> None:
        st = Status.new(id="t1", title="T", persona="Rukawa")
        assert lanes.work_owner(paths, st) == "Rukawa"

    def test_workflow_before_compile_is_the_captain(self, paths: JournalPaths) -> None:
        st = Status.new_workflow(id="wf", title="W", playbook_name="default")
        assert st.compile_phase != CompilePhase.COMPLETE
        assert lanes.work_owner(paths, st) is None  # no captain -> team default
        st.persona = "Akagi"
        assert lanes.work_owner(paths, st) == "Akagi"

    def test_workflow_walk_positions(self, paths: JournalPaths) -> None:
        steps = [
            _sf("plan", "Akagi", "planner", "build", "plan", "__escalate__"),
            _sf("build", "Rukawa", "developer", "__done__", "build", "__escalate__"),
        ]
        _seed_workflow_graph(paths, "wf1", steps, "plan")
        st = Status.from_json(paths.status_json("wf1").read_text())
        # No walk.json yet: the entrypoint step's persona.
        assert lanes.work_owner(paths, st) == "Akagi"
        # Walk at 'build': that step's persona.
        walk.write(paths, walk.initial("wf1", "build"))
        assert lanes.work_owner(paths, st) == "Rukawa"
        # Terminal: the captain (None here -> team default).
        walk.write(paths, walk.initial("wf1", "__done__"))
        assert lanes.work_owner(paths, st) is None
        # A walk at an unknown step falls back to the captain.
        st.persona = "Ayako"
        walk.write(paths, walk.initial("wf1", "ghost"))
        assert lanes.work_owner(paths, st) == "Ayako"

    def test_workflow_without_readable_graph_falls_back(self, paths: JournalPaths) -> None:
        st = Status.new_workflow(id="wf2", title="W", playbook_name="default")
        st.compile_pending = False
        st.compile_phase = CompilePhase.COMPLETE
        st.persona = "Akagi"
        (paths.active / "wf2").mkdir(parents=True)
        # No orchestration.json at all.
        assert lanes.work_owner(paths, st) == "Akagi"
        # Malformed orchestration.json.
        (paths.active / "wf2" / "orchestration.json").write_text("{bad")
        assert lanes.work_owner(paths, st) == "Akagi"
        # An orchestration whose entrypoint is not a string.
        (paths.active / "wf2" / "orchestration.json").write_text(json.dumps({"entrypoint": 3}))
        assert lanes.work_owner(paths, st) == "Akagi"
        # A corrupt walk.json is treated as "walk not started".
        (paths.active / "wf2" / "orchestration.json").write_text(json.dumps({"entrypoint": "plan"}))
        (paths.active / "wf2" / "walk.json").write_text("{nope")
        assert lanes.work_owner(paths, st) == "Akagi"

    def test_step_owner(self, paths: JournalPaths) -> None:
        _seed_workflow_graph(
            paths, "wf3",
            [_sf("plan", "Akagi", "planner", "__done__", "plan", "__escalate__")],
            "plan",
        )
        assert lanes.step_owner(paths, "wf3", "plan") == "Akagi"
        assert lanes.step_owner(paths, "wf3", "missing") is None


class TestDeferredOwner:
    def test_reads_entry_persona(self, paths: JournalPaths) -> None:
        from tigerharness.journal.deferred import defer_entry
        e = defer_entry(
            paths, title="x", team="Shohoku", payload_text="p",
            kind="task", persona="Rukawa",
        )
        assert lanes.deferred_owner(paths, e.id) == "Rukawa"
        e2 = defer_entry(paths, title="y", team="Shohoku", payload_text="p")
        assert lanes.deferred_owner(paths, e2.id) is None
        assert lanes.deferred_owner(paths, "no-such-entry") is None

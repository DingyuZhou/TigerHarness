"""Tests for tigerharness.dismiss.

The tests are organized around the dismiss module's surface:

  - YAML readers/editors  (line-based parsers, no pyyaml dep)
  - Plan builders         (build_team_plan, build_persona_plan)
  - Preview rendering     (render_preview)
  - Execution             (execute_plan, with injected subprocess + IO)
  - Interactive picker    (_pick_target, _prompt_yes_no)
  - main() CLI flow       (happy paths + every abort branch)
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tigerharness import dismiss as dismiss_mod
from tigerharness.dismiss import (
    _SYSTEMCTL_TIMEOUT_S,
    DismissPlan,
    FileEdit,
    FileRemoval,
    ServiceAction,
    _format_path,
    _has_persona_entry,
    _is_safe_state_dir,
    _maybe_abort,
    _pick_target,
    _prompt_yes_no,
    _read_default_persona,
    _read_env_files_from_unit,
    _read_persona_aliases,
    _read_lanes_from_index,
    _read_state_dir,
    _remove_lane_from_index,
    _remove_persona_entry_from_yaml,
    _unquote,
    build_persona_plan,
    build_team_plan,
    execute_plan,
    main as dismiss_main,
    render_preview,
)


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_xdg_state(tmp_path, monkeypatch):
    """Point the journal's XDG root at a fresh tmp dir so the team-plan
    T8 reminder (global journal root) never depends on the developer's
    real ~/.local/state."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))


def _make_team(
    teams_root: Path,
    team: str,
    personas: list[str],
    *,
    with_slack_env: bool = False,
    with_fragment: str | None = None,  # "default_persona" or "legacy" or None
    fragment_state_dir: str | None = None,
    default_persona_value: str | None = None,
) -> Path:
    """Build a synthetic team layout under *teams_root*.

    Mirrors `tigerharness init`'s output shape so the dismiss tests
    exercise the real on-disk schema rather than a private stub.
    """
    team_dir = teams_root / team
    (team_dir / "configs").mkdir(parents=True)
    yaml_text = "personas_dir: ../personas\n\npersonas:\n"
    for p in personas:
        yaml_text += (
            f"  - name: {p}\n"
            f"    cwd: ..\n"
            f"    prompt_file: {p}/prompt\n"
            f'    description: "{p}"\n'
        )
    (team_dir / "configs" / "personas.yaml").write_text(yaml_text)
    for p in personas:
        pdir = team_dir / "personas" / p
        pdir.mkdir(parents=True)
        (pdir / "prompt.md").write_text(f"You are {p}.\n")
        # Memory store directory + a file inside, so removals exercise
        # the recursive-rmtree path.
        mdir = team_dir / "memories" / p
        mdir.mkdir(parents=True)
        (mdir / "tiger-memory.config.yaml").write_text("agent:\n  name: x\n")
        (mdir / "archive").mkdir()
        (mdir / "archive" / "entry.md").write_text("entry")
    if with_slack_env:
        (team_dir / "configs" / ".env").write_text("SLACK_BOT_TOKEN=xoxb-1\n")
    if with_fragment:
        default_field = (
            "persona" if with_fragment == "legacy" else "default_persona"
        )
        default_value = default_persona_value or personas[0]
        frag_lines = [
            f"# fragment for team {team}",
            f"{default_field}: {default_value}",
            "allowed_user_ids:",
            "  - U0123",
        ]
        if fragment_state_dir is not None:
            frag_lines.append(f"state_dir: {fragment_state_dir}")
        (team_dir / "configs" / "slack-bridge.yaml").write_text(
            "\n".join(frag_lines) + "\n"
        )
    return team_dir


def _write_index(teams_root: Path, lanes: list[str]) -> Path:
    """Write a top-level slack-bridge.yaml index listing *lanes*."""
    index = teams_root / "slack-bridge.yaml"
    text = "# top-level multi-bridge index\nlanes:\n"
    for ln in lanes:
        text += f"  - {ln}\n"
    index.write_text(text)
    return index


class _InputQueue:
    """Sequential input() stub. Raises IndexError if the test exhausts
    the script -- which is what we want: an exhausted queue means the
    test setup didn't anticipate the prompt sequence."""

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str = "") -> str:
        self.prompts.append(prompt)
        return self._answers.pop(0)


# ---------------------------------------------------------------------------
# Line-based YAML readers / editors
# ---------------------------------------------------------------------------

class TestReadLanesFromIndex:
    def test_returns_empty_when_file_missing(self, tmp_path: Path) -> None:
        assert _read_lanes_from_index(tmp_path / "missing.yaml") == []

    def test_parses_simple_lanes_list(self, tmp_path: Path) -> None:
        index = tmp_path / "slack-bridge.yaml"
        index.write_text("lanes:\n  - shohoku\n  - tigers\n")
        assert _read_lanes_from_index(index) == ["shohoku", "tigers"]

    def test_skips_comments_inside_lanes(self, tmp_path: Path) -> None:
        index = tmp_path / "slack-bridge.yaml"
        index.write_text(
            "# top comment\n"
            "lanes:\n"
            "  # mid-list comment\n"
            "  - shohoku\n"
            "  - tigers\n"
        )
        assert _read_lanes_from_index(index) == ["shohoku", "tigers"]

    def test_block_ends_at_unindented_line(self, tmp_path: Path) -> None:
        index = tmp_path / "slack-bridge.yaml"
        index.write_text(
            "lanes:\n"
            "  - shohoku\n"
            "other_section:\n"
            "  - not_a_lane\n"
        )
        assert _read_lanes_from_index(index) == ["shohoku"]

    def test_no_lanes_header_returns_empty(self, tmp_path: Path) -> None:
        index = tmp_path / "slack-bridge.yaml"
        index.write_text("# placeholder index, no lanes yet\n")
        assert _read_lanes_from_index(index) == []

    def test_blank_line_inside_lanes_block_is_skipped(
        self, tmp_path: Path
    ) -> None:
        # A blank line inside the lanes block isn't a comment, doesn't
        # terminate the block (no leading char to test), and doesn't
        # match the bullet regex -- so it's just ignored.
        index = tmp_path / "slack-bridge.yaml"
        index.write_text(
            "lanes:\n"
            "  - shohoku\n"
            "\n"
            "  - tigers\n"
        )
        assert _read_lanes_from_index(index) == ["shohoku", "tigers"]


class TestRemoveLaneFromIndex:
    def test_removes_named_lane_preserves_rest(self) -> None:
        text = "lanes:\n  - shohoku\n  - tigers\n  - bulls\n"
        out = _remove_lane_from_index(text, "tigers")
        assert out == "lanes:\n  - shohoku\n  - bulls\n"

    def test_idempotent_when_lane_absent(self) -> None:
        text = "lanes:\n  - shohoku\n"
        assert _remove_lane_from_index(text, "tigers") == text

    def test_does_not_touch_bullets_outside_lanes(self) -> None:
        text = (
            "lanes:\n"
            "  - shohoku\n"
            "other:\n"
            "  - tigers\n"  # NOT a lane: must not be removed
        )
        out = _remove_lane_from_index(text, "tigers")
        assert "other:\n  - tigers\n" in out

    def test_preserves_comments_in_lanes_block(self) -> None:
        text = (
            "lanes:\n"
            "  # comment about shohoku\n"
            "  - shohoku\n"
            "  - tigers\n"
        )
        out = _remove_lane_from_index(text, "shohoku")
        assert "# comment about shohoku" in out
        assert "  - shohoku\n" not in out
        assert "  - tigers\n" in out


class TestReadDefaultPersona:
    def test_returns_none_when_file_missing(self, tmp_path: Path) -> None:
        assert _read_default_persona(tmp_path / "nope.yaml") is None

    def test_reads_default_persona_field(self, tmp_path: Path) -> None:
        frag = tmp_path / "frag.yaml"
        frag.write_text("default_persona: ayako\nallowed_user_ids: []\n")
        assert _read_default_persona(frag) == "ayako"

    def test_reads_legacy_persona_field(self, tmp_path: Path) -> None:
        frag = tmp_path / "frag.yaml"
        frag.write_text("# legacy fragment\npersona: ayako\n")
        assert _read_default_persona(frag) == "ayako"

    def test_returns_none_when_field_absent(self, tmp_path: Path) -> None:
        frag = tmp_path / "frag.yaml"
        frag.write_text("allowed_user_ids: []\n")
        assert _read_default_persona(frag) is None

    def test_skips_comment_lines(self, tmp_path: Path) -> None:
        frag = tmp_path / "frag.yaml"
        frag.write_text("# default_persona: never_this\ndefault_persona: real\n")
        assert _read_default_persona(frag) == "real"


class TestReadStateDir:
    def test_returns_none_when_file_missing(self, tmp_path: Path) -> None:
        assert _read_state_dir(tmp_path / "nope.yaml") is None

    def test_reads_and_expands_tilde(self, tmp_path: Path) -> None:
        frag = tmp_path / "frag.yaml"
        frag.write_text("state_dir: ~/state/slack/x\n")
        out = _read_state_dir(frag)
        assert out is not None
        assert "~" not in str(out)
        assert str(out).endswith("/state/slack/x")

    def test_returns_none_when_field_absent(self, tmp_path: Path) -> None:
        frag = tmp_path / "frag.yaml"
        frag.write_text("default_persona: x\n")
        assert _read_state_dir(frag) is None

    def test_skips_commented_line(self, tmp_path: Path) -> None:
        frag = tmp_path / "frag.yaml"
        frag.write_text("# state_dir: ignored\ndefault_persona: y\n")
        assert _read_state_dir(frag) is None


class TestRemovePersonaEntryFromYaml:
    YAML = (
        "personas_dir: ../personas\n\n"
        "personas:\n"
        "  - name: ayako\n"
        "    cwd: ..\n"
        '    description: "manager"\n'
        "  - name: sakuragi\n"
        "    cwd: ..\n"
        '    description: "rebounds"\n'
        "  - name: rukawa\n"
        "    cwd: ..\n"
    )

    def test_remove_middle_entry(self) -> None:
        out = _remove_persona_entry_from_yaml(self.YAML, "sakuragi")
        assert "- name: sakuragi" not in out
        assert "- name: ayako" in out
        assert "- name: rukawa" in out
        # ayako's body fields stay intact
        assert '    description: "manager"' in out

    def test_remove_first_entry(self) -> None:
        out = _remove_persona_entry_from_yaml(self.YAML, "ayako")
        assert "- name: ayako" not in out
        # The body fields of ayako should also be gone
        assert '    description: "manager"' not in out
        assert "- name: sakuragi" in out

    def test_remove_last_entry(self) -> None:
        out = _remove_persona_entry_from_yaml(self.YAML, "rukawa")
        assert "- name: rukawa" not in out
        # No trailing body lines after sakuragi's description left
        assert "- name: sakuragi" in out

    def test_absent_persona_is_noop(self) -> None:
        out = _remove_persona_entry_from_yaml(self.YAML, "nobody")
        assert out == self.YAML


# ---------------------------------------------------------------------------
# Plan builders -- team
# ---------------------------------------------------------------------------

class TestBuildTeamPlan:
    def test_invalid_name_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="invalid team name"):
            build_team_plan(team="bad name!", teams_root=tmp_path)

    def test_nonexistent_team_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="not found"):
            build_team_plan(team="ghost", teams_root=tmp_path)

    def test_single_tenant_team_minimal_plan(self, tmp_path: Path) -> None:
        _make_team(tmp_path, "shohoku", ["ayako"], with_slack_env=True)
        plan = build_team_plan(
            team="shohoku", teams_root=tmp_path, home=tmp_path / "home",
        )
        assert plan.kind == "team"
        assert plan.target_name == "shohoku"
        # The team_dir is the one and only removal in single-tenant
        # without a runtime state dir.
        rm_paths = [r.path for r in plan.removals]
        assert (tmp_path / "shohoku").resolve() in rm_paths
        assert plan.edits == ()
        assert plan.service_actions == ()
        # Slack reminder is present (env file existed)
        assert any("api.slack.com" in r for r in plan.manual_reminders)

    def test_multi_team_with_remaining_lanes_edits_index(
        self, tmp_path: Path
    ) -> None:
        _make_team(
            tmp_path, "shohoku", ["ayako"],
            with_slack_env=True, with_fragment="default_persona",
            fragment_state_dir=str(tmp_path / "state" / "shohoku"),
        )
        _make_team(
            tmp_path, "tigers", ["sai"],
            with_slack_env=True, with_fragment="default_persona",
        )
        (tmp_path / "state" / "shohoku").mkdir(parents=True)
        (tmp_path / "state" / "shohoku" / "threads.json").write_text("{}")
        _write_index(tmp_path, ["shohoku", "tigers"])

        plan = build_team_plan(
            team="shohoku", teams_root=tmp_path,
            home=tmp_path / "home",
        )
        # Index gets EDITED, not removed (other lanes remain)
        assert len(plan.edits) == 1
        assert plan.edits[0].path.name == "slack-bridge.yaml"
        assert "tigers" in plan.edits[0].new_content
        assert "shohoku" not in plan.edits[0].new_content
        # State dir is in removals
        rm_paths = [r.path for r in plan.removals]
        assert (tmp_path / "state" / "shohoku") in rm_paths
        # No service actions (other lanes still need the bridge)
        assert plan.service_actions == ()

    def test_multi_team_last_team_full_teardown(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        unit_dir = home / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        # Realistic gen-service output: the unit references this
        # root's env file -- under content-based discovery, that
        # reference is what makes it THIS root's unit.
        (unit_dir / "slack-bridge-multi.service").write_text(
            f"[Unit]\n[Service]\n"
            f"EnvironmentFile={tmp_path}/multi-bridge.env\n"
        )
        _make_team(
            tmp_path, "shohoku", ["ayako"],
            with_slack_env=True, with_fragment="default_persona",
        )
        _write_index(tmp_path, ["shohoku"])
        (tmp_path / "multi-bridge.env").write_text(
            "TIGERHARNESS_BRIDGES_CONFIG=...\n"
        )

        plan = build_team_plan(
            team="shohoku", teams_root=tmp_path, home=home,
        )
        rm_paths = [r.path for r in plan.removals]
        # Index and env file are removed (not edited)
        assert plan.edits == ()
        assert tmp_path / "slack-bridge.yaml" in [p.resolve() for p in rm_paths]
        assert tmp_path / "multi-bridge.env" in rm_paths
        # Systemd teardown actions are present in stable order:
        # stop_disable_unit -> remove_unit_file -> daemon_reload
        kinds = [s.kind for s in plan.service_actions]
        assert kinds == [
            "stop_disable_unit", "remove_unit_file", "daemon_reload",
        ]
        # Journalctl reminder is added
        assert any("journalctl" in r for r in plan.manual_reminders)

    def test_last_team_never_touches_another_roots_bridge(
        self, tmp_path: Path
    ) -> None:
        """The 2026-06-12 incident, as a regression test: dismissing
        the last lane of root B must not name, stop, or delete
        anything belonging to root A -- and the refusal is loud."""
        home = tmp_path / "home"
        unit_dir = home / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        root_a = tmp_path / "teams"
        root_a.mkdir()
        (root_a / "multi-bridge.env").write_text(
            f"TIGERHARNESS_BRIDGES_CONFIG={root_a}/slack-bridge.yaml\n"
        )
        # The legacy global unit name, owned by root A.
        (unit_dir / "slack-bridge-multi.service").write_text(
            f"[Service]\nEnvironmentFile={root_a}/multi-bridge.env\n"
        )
        root_b = tmp_path / "my-teams"
        root_b.mkdir()
        _make_team(
            root_b, "inkstone", ["scribe"],
            with_slack_env=True, with_fragment="default_persona",
        )
        _write_index(root_b, ["inkstone"])

        plan = build_team_plan(
            team="inkstone", teams_root=root_b, home=home,
        )
        rm_paths = [r.path.resolve() for r in plan.removals]
        # Root A's env file survives; root A's unit is untouched.
        assert (root_a / "multi-bridge.env").resolve() not in rm_paths
        assert plan.service_actions == ()
        # The refusal is loud and names what was scanned + excluded.
        joined = "\n".join(plan.manual_reminders)
        assert "slack-bridge-multi.service" in joined
        assert str(root_b) in joined
        # Root B's own index still gets cleaned up.
        assert (root_b / "slack-bridge.yaml").resolve() in rm_paths

    def test_last_team_matches_per_root_unit_and_spares_others(
        self, tmp_path: Path
    ) -> None:
        """Two roots, two per-root units: dismissal tears down only
        the operated root's unit; ownership via env-file location."""
        home = tmp_path / "home"
        unit_dir = home / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        root_a = tmp_path / "teams"
        root_a.mkdir()
        (root_a / "multi-bridge.env").write_text(
            "TIGERHARNESS_BRIDGES_CONFIG=x\n"
        )
        (unit_dir / "slack-bridge-multi-teams-abc123.service").write_text(
            f"[Service]\nEnvironmentFile={root_a}/multi-bridge.env\n"
        )
        root_b = tmp_path / "my-teams"
        root_b.mkdir()
        _make_team(
            root_b, "inkstone", ["scribe"],
            with_slack_env=True, with_fragment="default_persona",
        )
        _write_index(root_b, ["inkstone"])
        (root_b / "multi-bridge.env").write_text(
            "TIGERHARNESS_BRIDGES_CONFIG=y\n"
        )
        (unit_dir / "slack-bridge-multi-my-teams-def456.service").write_text(
            f"[Service]\nEnvironmentFile={root_b}/multi-bridge.env\n"
        )

        plan = build_team_plan(
            team="inkstone", teams_root=root_b, home=home,
        )
        rm_paths = [r.path.resolve() for r in plan.removals]
        assert (root_b / "multi-bridge.env").resolve() in rm_paths
        assert (root_a / "multi-bridge.env").resolve() not in rm_paths
        targets = [s.target for s in plan.service_actions]
        assert "slack-bridge-multi-my-teams-def456.service" in targets
        assert not any("teams-abc123" in t for t in targets)
        unit_removals = [
            s.target for s in plan.service_actions
            if s.kind == "remove_unit_file"
        ]
        assert unit_removals == [
            str(unit_dir / "slack-bridge-multi-my-teams-def456.service")
        ]

    def test_last_team_discovers_new_scheme_unit(
        self, tmp_path: Path
    ) -> None:
        """After the `multi-` rename, a current-scheme
        slack-bridge-<root>-<hash>.service owned by this root is still
        found by the broadened glob and torn down by content."""
        home = tmp_path / "home"
        unit_dir = home / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        _make_team(
            tmp_path, "shohoku", ["ayako"],
            with_slack_env=True, with_fragment="default_persona",
        )
        _write_index(tmp_path, ["shohoku"])
        (tmp_path / "multi-bridge.env").write_text(
            "TIGERHARNESS_BRIDGES_CONFIG=x\n"
        )
        # Current naming scheme: no `multi-` infix.
        (unit_dir / "slack-bridge-teams-abc123.service").write_text(
            f"[Service]\nEnvironmentFile={tmp_path}/multi-bridge.env\n"
        )

        plan = build_team_plan(
            team="shohoku", teams_root=tmp_path, home=home,
        )
        targets = [s.target for s in plan.service_actions]
        assert "slack-bridge-teams-abc123.service" in targets
        assert (tmp_path / "multi-bridge.env") in [
            r.path for r in plan.removals
        ]

    def test_old_scheme_unit_dismissed_while_foreign_root_spared(
        self, tmp_path: Path
    ) -> None:
        """Backward-compat regression in the 2026-06-12 INCIDENT SHAPE.

        One unit_dir holds two units: an OLD
        `slack-bridge-multi-<root>-<hash>.service` owned by THIS (operated)
        root, and a current-scheme unit owned by ANOTHER root. A single
        plan-build must tear down the owned old-scheme unit AND its in-root
        env file WHILE leaving the foreign root's unit and env file
        untouched -- the two directions proven together, since testing them
        in isolation can both pass while the mixed case regresses.
        """
        home = tmp_path / "home"
        unit_dir = home / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        # Foreign root A -- must be spared entirely. Current scheme name.
        root_a = tmp_path / "teams"
        root_a.mkdir()
        (root_a / "multi-bridge.env").write_text(
            "TIGERHARNESS_BRIDGES_CONFIG=a\n"
        )
        (unit_dir / "slack-bridge-teams-aaa111.service").write_text(
            f"[Service]\nEnvironmentFile={root_a}/multi-bridge.env\n"
        )
        # Operated root B owns a unit under the OLD `multi-` scheme.
        root_b = tmp_path / "my-teams"
        root_b.mkdir()
        _make_team(
            root_b, "inkstone", ["scribe"],
            with_slack_env=True, with_fragment="default_persona",
        )
        _write_index(root_b, ["inkstone"])
        (root_b / "multi-bridge.env").write_text(
            "TIGERHARNESS_BRIDGES_CONFIG=b\n"
        )
        (unit_dir / "slack-bridge-multi-my-teams-bbb222.service").write_text(
            f"[Service]\nEnvironmentFile={root_b}/multi-bridge.env\n"
        )

        plan = build_team_plan(
            team="inkstone", teams_root=root_b, home=home,
        )
        rm_paths = [r.path.resolve() for r in plan.removals]
        targets = [s.target for s in plan.service_actions]
        # Owned OLD-scheme unit + its in-root env file are torn down.
        assert "slack-bridge-multi-my-teams-bbb222.service" in targets
        assert (root_b / "multi-bridge.env").resolve() in rm_paths
        # Foreign root's unit + env file are untouched (the incident).
        assert not any("teams-aaa111" in t for t in targets)
        assert (root_a / "multi-bridge.env").resolve() not in rm_paths

    def test_single_tenant_unit_not_swept_by_broadened_glob(
        self, tmp_path: Path
    ) -> None:
        """The broadened `slack-bridge-*.service` glob must NOT match the
        legacy single-tenant `slack-bridge.service` (no trailing `-`), even
        when its env resolves inside the operated root -- it predates the
        multi-team layout and stays untouched (audit T9)."""
        home = tmp_path / "home"
        unit_dir = home / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        _make_team(
            tmp_path, "shohoku", ["ayako"],
            with_slack_env=True, with_fragment="default_persona",
        )
        _write_index(tmp_path, ["shohoku"])
        (tmp_path / "multi-bridge.env").write_text(
            "TIGERHARNESS_BRIDGES_CONFIG=x\n"
        )
        # Single-tenant unit whose env points inside this root.
        (unit_dir / "slack-bridge.service").write_text(
            f"[Service]\nEnvironmentFile={tmp_path}/multi-bridge.env\n"
        )

        plan = build_team_plan(
            team="shohoku", teams_root=tmp_path, home=home,
        )
        # Not discovered, not torn down, not named anywhere as a target.
        assert plan.service_actions == ()
        assert not any(
            "slack-bridge.service" in s.target for s in plan.service_actions
        )
        # And the operator is told plainly that nothing matched.
        assert any(
            "no slack-bridge-*.service unit files found" in r
            for r in plan.manual_reminders
        )

    def test_owned_unit_with_outside_env_file_is_not_cross_deleted(
        self, tmp_path: Path
    ) -> None:
        """A unit owned via its bridges-config (env file parked outside
        the root) is stopped, but the outside env file is never
        deleted -- manual reminder instead."""
        home = tmp_path / "home"
        unit_dir = home / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        root_b = tmp_path / "my-teams"
        root_b.mkdir()
        _make_team(
            root_b, "inkstone", ["scribe"],
            with_slack_env=True, with_fragment="default_persona",
        )
        _write_index(root_b, ["inkstone"])
        outside_env = tmp_path / "elsewhere.env"
        outside_env.write_text(
            f"TIGERHARNESS_BRIDGES_CONFIG={root_b}/slack-bridge.yaml\n"
        )
        (unit_dir / "slack-bridge-multi-custom.service").write_text(
            f"[Service]\nEnvironmentFile={outside_env}\n"
        )

        plan = build_team_plan(
            team="inkstone", teams_root=root_b, home=home,
        )
        rm_paths = [r.path.resolve() for r in plan.removals]
        assert outside_env.resolve() not in rm_paths
        targets = [s.target for s in plan.service_actions]
        assert "slack-bridge-multi-custom.service" in targets
        assert any(str(outside_env) in r for r in plan.manual_reminders)

    def test_owned_unit_with_missing_env_file_skips_removal(
        self, tmp_path: Path
    ) -> None:
        """A unit owned via bridges-config whose env path no longer
        exists: unit is torn down, nothing scheduled for the dead
        path."""
        home = tmp_path / "home"
        unit_dir = home / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        root_b = tmp_path / "my-teams"
        root_b.mkdir()
        _make_team(
            root_b, "inkstone", ["scribe"],
            with_slack_env=True, with_fragment="default_persona",
        )
        _write_index(root_b, ["inkstone"])
        gone_env = root_b / "multi-bridge.env"  # never created
        (unit_dir / "slack-bridge-multi-x.service").write_text(
            f"[Service]\nEnvironmentFile={gone_env}\n"
        )
        plan = build_team_plan(
            team="inkstone", teams_root=root_b, home=home,
        )
        rm_paths = [r.path.resolve() for r in plan.removals]
        assert gone_env.resolve() not in rm_paths
        assert "slack-bridge-multi-x.service" in [
            s.target for s in plan.service_actions
        ]

    def test_last_team_no_candidate_units_is_loud(
        self, tmp_path: Path
    ) -> None:
        """Zero unit files at all: the canonical env file is still
        cleaned and the operator is told nothing was found."""
        home = tmp_path / "home"
        home.mkdir()
        _make_team(
            tmp_path, "shohoku", ["ayako"],
            with_slack_env=True, with_fragment="default_persona",
        )
        _write_index(tmp_path, ["shohoku"])
        (tmp_path / "multi-bridge.env").write_text(
            "TIGERHARNESS_BRIDGES_CONFIG=x\n"
        )

        plan = build_team_plan(
            team="shohoku", teams_root=tmp_path, home=home,
        )
        rm_paths = [r.path for r in plan.removals]
        assert tmp_path / "multi-bridge.env" in rm_paths
        assert plan.service_actions == ()
        assert any(
            "no slack-bridge-*.service unit files found" in r
            for r in plan.manual_reminders
        )

    def test_multi_team_last_team_without_unit_skips_service_actions(
        self, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        _make_team(
            tmp_path, "shohoku", ["ayako"],
            with_slack_env=True, with_fragment="default_persona",
        )
        _write_index(tmp_path, ["shohoku"])

        plan = build_team_plan(
            team="shohoku", teams_root=tmp_path, home=home,
        )
        assert plan.service_actions == ()
        # No journalctl reminder when there was no unit to stop
        assert not any("journalctl" in r for r in plan.manual_reminders)

    def test_team_without_slack_skips_slack_reminder(
        self, tmp_path: Path
    ) -> None:
        _make_team(tmp_path, "shohoku", ["ayako"], with_slack_env=False)
        plan = build_team_plan(
            team="shohoku", teams_root=tmp_path, home=tmp_path / "home",
        )
        assert not any("api.slack.com" in r for r in plan.manual_reminders)

    def test_index_present_but_team_not_listed_is_treated_as_single_tenant(
        self, tmp_path: Path
    ) -> None:
        # An index file can exist (created by a sibling team) yet not
        # reference this team -- in which case dismissing this team must
        # neither edit the index nor remove it.
        _make_team(tmp_path, "shohoku", ["ayako"], with_slack_env=True)
        _make_team(tmp_path, "tigers", ["sai"], with_slack_env=True)
        _write_index(tmp_path, ["tigers"])

        plan = build_team_plan(
            team="shohoku", teams_root=tmp_path, home=tmp_path / "home",
        )
        assert plan.edits == ()
        index_in_removals = any(
            p.path.name == "slack-bridge.yaml" for p in plan.removals
        )
        assert not index_in_removals


# ---------------------------------------------------------------------------
# Plan builders -- persona
# ---------------------------------------------------------------------------

class TestBuildPersonaPlan:
    def test_invalid_team_name_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="invalid team name"):
            build_persona_plan(
                team="bad!", persona="ayako", teams_root=tmp_path,
            )

    def test_invalid_persona_name_rejected(self, tmp_path: Path) -> None:
        _make_team(tmp_path, "shohoku", ["ayako"])
        with pytest.raises(ValueError, match="invalid persona name"):
            build_persona_plan(
                team="shohoku", persona="bad!", teams_root=tmp_path,
            )

    def test_nonexistent_team_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="team .* not found"):
            build_persona_plan(
                team="ghost", persona="ayako", teams_root=tmp_path,
            )

    def test_nonexistent_persona_rejected(self, tmp_path: Path) -> None:
        _make_team(tmp_path, "shohoku", ["ayako"])
        with pytest.raises(ValueError, match="persona .* not found"):
            build_persona_plan(
                team="shohoku", persona="ghost", teams_root=tmp_path,
            )

    def test_refuses_last_persona(self, tmp_path: Path) -> None:
        _make_team(tmp_path, "shohoku", ["ayako"])
        with pytest.raises(ValueError, match="only persona"):
            build_persona_plan(
                team="shohoku", persona="ayako", teams_root=tmp_path,
            )

    def test_refuses_default_persona_in_fragment(self, tmp_path: Path) -> None:
        _make_team(
            tmp_path, "shohoku", ["ayako", "sakuragi"],
            with_fragment="default_persona",
            default_persona_value="ayako",
        )
        with pytest.raises(ValueError, match="default_persona"):
            build_persona_plan(
                team="shohoku", persona="ayako", teams_root=tmp_path,
            )

    def test_happy_path_single_tenant(self, tmp_path: Path) -> None:
        _make_team(tmp_path, "shohoku", ["ayako", "sakuragi"])
        plan = build_persona_plan(
            team="shohoku", persona="sakuragi", teams_root=tmp_path,
        )
        assert plan.kind == "persona"
        assert plan.target_name == "shohoku/sakuragi"
        rm_paths = [r.path for r in plan.removals]
        assert tmp_path / "shohoku" / "personas" / "sakuragi" in rm_paths
        assert tmp_path / "shohoku" / "memories" / "sakuragi" in rm_paths
        # personas.yaml edit is present
        assert len(plan.edits) == 1
        assert "- name: sakuragi" not in plan.edits[0].new_content
        assert plan.service_actions == ()

    def test_happy_path_multi_team(self, tmp_path: Path) -> None:
        _make_team(
            tmp_path, "shohoku", ["ayako", "sakuragi"],
            with_fragment="default_persona",
            default_persona_value="ayako",  # ayako is default, sakuragi is not
        )
        plan = build_persona_plan(
            team="shohoku", persona="sakuragi", teams_root=tmp_path,
        )
        assert plan.kind == "persona"
        # Multi-team adds no new files to dismiss (the fragment stays --
        # only the personas.yaml entry is removed).
        assert any(e.path.name == "personas.yaml" for e in plan.edits)

    def test_persona_without_yaml_entry_skips_edit(
        self, tmp_path: Path
    ) -> None:
        # If somehow the persona dir exists but personas.yaml doesn't
        # list them, the plan should still proceed -- just without an
        # edit. (This shouldn't normally happen, but be defensive.)
        _make_team(tmp_path, "shohoku", ["ayako", "sakuragi"])
        # Remove sakuragi's entry from yaml, leaving only the prompt dir
        yaml_path = tmp_path / "shohoku" / "configs" / "personas.yaml"
        new = _remove_persona_entry_from_yaml(
            yaml_path.read_text(), "sakuragi"
        )
        yaml_path.write_text(new)

        plan = build_persona_plan(
            team="shohoku", persona="sakuragi", teams_root=tmp_path,
        )
        assert plan.edits == ()
        # But the dir is still scheduled for removal
        assert any(
            r.path.name == "sakuragi" for r in plan.removals
        )

    def test_persona_without_memory_dir_omits_memory_removal(
        self, tmp_path: Path
    ) -> None:
        _make_team(tmp_path, "shohoku", ["ayako", "sakuragi"])
        # Wipe sakuragi's memory dir
        import shutil as _shutil
        _shutil.rmtree(tmp_path / "shohoku" / "memories" / "sakuragi")

        plan = build_persona_plan(
            team="shohoku", persona="sakuragi", teams_root=tmp_path,
        )
        mem_in_removals = any(
            r.path.name == "sakuragi"
            and "memory" in r.description
            for r in plan.removals
        )
        assert not mem_in_removals


# ---------------------------------------------------------------------------
# render_preview
# ---------------------------------------------------------------------------

class TestPersonaPlanGapAudit:
    """T5 gap-audit patches: journal-default refusal (P5), dangling
    compile_personas reminder (P6), zombie active-task reminder (P7),
    prose-curation reminder (P10)."""

    def _add_journal_default(self, team_dir: Path, name: str) -> None:
        yaml_path = team_dir / "configs" / "personas.yaml"
        yaml_path.write_text(
            f"default_persona: {name}\n" + yaml_path.read_text()
        )

    def test_refuses_journal_default_persona(self, tmp_path: Path) -> None:
        team_dir = _make_team(tmp_path, "tigers", ["ayako", "anzai"])
        self._add_journal_default(team_dir, "ayako")
        with pytest.raises(ValueError, match="default_persona"):
            build_persona_plan(
                team="tigers", persona="ayako", teams_root=tmp_path,
            )

    def test_non_default_persona_still_dismissible(
        self, tmp_path: Path,
    ) -> None:
        team_dir = _make_team(tmp_path, "tigers", ["ayako", "anzai"])
        self._add_journal_default(team_dir, "ayako")
        plan = build_persona_plan(
            team="tigers", persona="anzai", teams_root=tmp_path,
        )
        assert plan.kind == "persona"

    def test_missing_journal_default_key_is_fine(
        self, tmp_path: Path,
    ) -> None:
        _make_team(tmp_path, "tigers", ["ayako", "anzai"])
        plan = build_persona_plan(
            team="tigers", persona="anzai", teams_root=tmp_path,
        )
        assert plan.kind == "persona"

    def test_workflow_yaml_direct_hit_reminds(self, tmp_path: Path) -> None:
        team_dir = _make_team(tmp_path, "tigers", ["ayako", "anzai"])
        (team_dir / "configs" / "workflow.yaml").write_text(
            "compile_personas:\n  ayako: anzai\n"
        )
        plan = build_persona_plan(
            team="tigers", persona="anzai", teams_root=tmp_path,
        )
        assert any(
            "compile_personas" in r for r in plan.manual_reminders
        )

    def test_workflow_yaml_alias_hit_reminds(self, tmp_path: Path) -> None:
        team_dir = _make_team(tmp_path, "tigers", ["ayako", "anzai"])
        yaml_path = team_dir / "configs" / "personas.yaml"
        yaml_path.write_text(yaml_path.read_text().replace(
            "  - name: anzai\n",
            "  - name: anzai\n    aliases: [anzai, Coach]\n",
        ))
        (team_dir / "configs" / "workflow.yaml").write_text(
            "compile_personas:\n  drafter: Coach\n"
        )
        plan = build_persona_plan(
            team="tigers", persona="anzai", teams_root=tmp_path,
        )
        assert any("Coach" in r for r in plan.manual_reminders)

    def test_workflow_yaml_absent_or_clean_no_reminder(
        self, tmp_path: Path,
    ) -> None:
        team_dir = _make_team(tmp_path, "tigers", ["ayako", "anzai"])
        plan = build_persona_plan(
            team="tigers", persona="anzai", teams_root=tmp_path,
        )
        assert not any(
            "compile_personas" in r for r in plan.manual_reminders
        )
        (team_dir / "configs" / "workflow.yaml").write_text(
            "compile_personas:\n  drafter: ayako\n"
        )
        plan = build_persona_plan(
            team="tigers", persona="anzai", teams_root=tmp_path,
        )
        assert not any(
            "compile_personas" in r for r in plan.manual_reminders
        )

    def test_active_task_assignment_reminds(self, tmp_path: Path) -> None:
        team_dir = _make_team(tmp_path, "tigers", ["ayako", "anzai"])
        tdir = team_dir / "journal" / "active" / "20260611-x-abc"
        tdir.mkdir(parents=True)
        (tdir / "status.json").write_text(
            '{"persona": "anzai", "state": "pending"}'
        )
        # Malformed sibling is skipped, not fatal.
        bad = team_dir / "journal" / "active" / "20260611-bad-def"
        bad.mkdir(parents=True)
        (bad / "status.json").write_text("{not json")
        plan = build_persona_plan(
            team="tigers", persona="anzai", teams_root=tmp_path,
        )
        assert any("20260611-x-abc" in r for r in plan.manual_reminders)

    def test_workflow_yaml_unreadable_returns_empty(
        self, tmp_path: Path,
    ) -> None:
        # OSError branch: a directory where the file should be.
        team_dir = _make_team(tmp_path, "tigers", ["ayako", "anzai"])
        (team_dir / "configs" / "workflow.yaml").mkdir()
        plan = build_persona_plan(
            team="tigers", persona="anzai", teams_root=tmp_path,
        )
        assert not any(
            "compile_personas" in r for r in plan.manual_reminders
        )

    def test_active_task_other_persona_not_listed(
        self, tmp_path: Path,
    ) -> None:
        # The non-matching valid-status loop branch.
        team_dir = _make_team(tmp_path, "tigers", ["ayako", "anzai"])
        tdir = team_dir / "journal" / "active" / "20260611-y-zzz"
        tdir.mkdir(parents=True)
        (tdir / "status.json").write_text(
            '{"persona": "ayako", "state": "pending"}'
        )
        plan = build_persona_plan(
            team="tigers", persona="anzai", teams_root=tmp_path,
        )
        assert not any(
            "journal/active" in r for r in plan.manual_reminders
        )

    def test_no_journal_dir_no_task_reminder(self, tmp_path: Path) -> None:
        _make_team(tmp_path, "tigers", ["ayako", "anzai"])
        plan = build_persona_plan(
            team="tigers", persona="anzai", teams_root=tmp_path,
        )
        assert not any(
            "journal/active" in r for r in plan.manual_reminders
        )

    def test_prose_reminder_only_when_docs_exist(
        self, tmp_path: Path,
    ) -> None:
        team_dir = _make_team(tmp_path, "tigers", ["ayako", "anzai"])
        plan = build_persona_plan(
            team="tigers", persona="anzai", teams_root=tmp_path,
        )
        assert not any(
            "Prose mentions" in r for r in plan.manual_reminders
        )
        (team_dir / "knowledge").mkdir()
        plan = build_persona_plan(
            team="tigers", persona="anzai", teams_root=tmp_path,
        )
        assert any("Prose mentions" in r for r in plan.manual_reminders)


class TestTeamPlanXdgJournalReminder:
    """T8: a global/XDG journal root gets a shared-state reminder on
    team dismissal; absent root stays silent."""

    def test_existing_xdg_root_reminds(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        xdg = tmp_path / "xdg-state"
        (xdg / "tigerharness-journal").mkdir(parents=True)
        monkeypatch.setenv("XDG_STATE_HOME", str(xdg))
        _make_team(tmp_path, "tigers", ["ayako"])
        plan = build_team_plan(
            team="tigers", teams_root=tmp_path, home=tmp_path,
        )
        assert any(
            "global journal root" in r for r in plan.manual_reminders
        )

    def test_absent_xdg_root_silent(self, tmp_path: Path) -> None:
        _make_team(tmp_path, "tigers", ["ayako"])
        plan = build_team_plan(
            team="tigers", teams_root=tmp_path, home=tmp_path,
        )
        assert not any(
            "global journal root" in r for r in plan.manual_reminders
        )


class TestRenderPreview:
    def test_empty_plan_still_renders(self) -> None:
        plan = DismissPlan(
            kind="team", target_name="x",
            removals=(), edits=(), service_actions=(),
            manual_reminders=(),
        )
        out = render_preview(plan)
        assert "(none)" in out
        assert "IRREVERSIBLE" in out

    def test_full_plan_renders_all_sections(self) -> None:
        plan = DismissPlan(
            kind="team", target_name="shohoku",
            removals=(FileRemoval(Path("/a/b"), "team dir"),),
            edits=(FileEdit(Path("/a/c"), "edit foo", "new"),),
            service_actions=(
                ServiceAction("daemon_reload", "", "reload"),
            ),
            manual_reminders=("delete the slack app",),
        )
        out = render_preview(plan)
        assert "REMOVE" in out
        assert "/a/b" in out
        assert "EDIT" in out
        assert "/a/c" in out
        assert "Systemd actions" in out
        assert "reload" in out
        assert "Out of scope" in out
        assert "delete the slack app" in out


# ---------------------------------------------------------------------------
# execute_plan
# ---------------------------------------------------------------------------

class _SubprocessRecorder:
    """Stand-in for subprocess.run that just records calls."""
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        self.kwargs.append(kwargs)
        return None


class TestExecutePlan:
    def test_removes_dir_recursively(self, tmp_path: Path) -> None:
        target = tmp_path / "victim"
        (target / "nested").mkdir(parents=True)
        (target / "nested" / "file.txt").write_text("x")
        plan = DismissPlan(
            kind="team", target_name="victim",
            removals=(FileRemoval(target, "team dir"),),
            edits=(), service_actions=(), manual_reminders=(),
        )
        out_log: list[str] = []
        errs = execute_plan(plan, out=out_log.append)
        assert errs == 0
        assert not target.exists()
        assert any("removed" in line for line in out_log)

    def test_removes_file(self, tmp_path: Path) -> None:
        f = tmp_path / "file.txt"
        f.write_text("x")
        plan = DismissPlan(
            kind="team", target_name="x",
            removals=(FileRemoval(f, "file"),),
            edits=(), service_actions=(), manual_reminders=(),
        )
        assert execute_plan(plan, out=lambda _s: None) == 0
        assert not f.exists()

    def test_silently_skips_missing_paths(self, tmp_path: Path) -> None:
        plan = DismissPlan(
            kind="team", target_name="x",
            removals=(FileRemoval(tmp_path / "absent", "missing"),),
            edits=(), service_actions=(), manual_reminders=(),
        )
        assert execute_plan(plan, out=lambda _s: None) == 0

    def test_edits_files_in_place(self, tmp_path: Path) -> None:
        f = tmp_path / "thing.yaml"
        f.write_text("old\n")
        plan = DismissPlan(
            kind="team", target_name="x",
            removals=(),
            edits=(FileEdit(f, "rewrite", "new\n"),),
            service_actions=(), manual_reminders=(),
        )
        assert execute_plan(plan, out=lambda _s: None) == 0
        assert f.read_text() == "new\n"

    def test_edit_skipped_if_path_absent(self, tmp_path: Path) -> None:
        plan = DismissPlan(
            kind="team", target_name="x",
            removals=(),
            edits=(FileEdit(tmp_path / "no", "x", "y"),),
            service_actions=(), manual_reminders=(),
        )
        assert execute_plan(plan, out=lambda _s: None) == 0

    def test_systemd_actions_emit_expected_subprocess_calls(
        self, tmp_path: Path
    ) -> None:
        unit_file = tmp_path / "slack-bridge-multi.service"
        unit_file.write_text("[Unit]\n")
        plan = DismissPlan(
            kind="team", target_name="x",
            removals=(),
            edits=(),
            service_actions=(
                ServiceAction(
                    "stop_disable_unit",
                    "slack-bridge-multi.service",
                    "stop & disable",
                ),
                ServiceAction(
                    "remove_unit_file",
                    str(unit_file),
                    "rm unit",
                ),
                ServiceAction(
                    "daemon_reload", "", "reload",
                ),
            ),
            manual_reminders=(),
        )
        recorder = _SubprocessRecorder()
        out_log: list[str] = []
        errs = execute_plan(
            plan, run_subprocess=recorder, out=out_log.append,
        )
        assert errs == 0
        assert not unit_file.exists()
        # stop + disable + daemon-reload = 3 subprocess calls. Stop is
        # blocking now (no --no-block) so that the upcoming state-dir
        # rmtree doesn't race a still-draining bridge.
        assert len(recorder.calls) == 3
        assert "stop" in recorder.calls[0]
        assert "--no-block" not in recorder.calls[0]
        assert "disable" in recorder.calls[1]
        assert "daemon-reload" in recorder.calls[2]

    def test_remove_unit_file_skips_when_already_gone(
        self, tmp_path: Path
    ) -> None:
        # A previous run (or external cleanup) may have removed the unit
        # file already. Replaying the action should be a no-op, not an
        # error.
        plan = DismissPlan(
            kind="team", target_name="x",
            removals=(), edits=(),
            service_actions=(
                ServiceAction(
                    "remove_unit_file",
                    str(tmp_path / "already-gone.service"),
                    "rm unit",
                ),
            ),
            manual_reminders=(),
        )
        out_log: list[str] = []
        errs = execute_plan(
            plan,
            run_subprocess=_SubprocessRecorder(),
            out=out_log.append,
        )
        assert errs == 0
        # No "removed ..." line printed, because the file wasn't there.
        assert not any("removed" in m for m in out_log)

    def test_unknown_service_kind_counts_as_error(self) -> None:
        plan = DismissPlan(
            kind="team", target_name="x",
            removals=(), edits=(),
            service_actions=(
                ServiceAction("totally_invented", "x", "?"),
            ),
            manual_reminders=(),
        )
        err_log: list[str] = []
        errs = execute_plan(
            plan,
            run_subprocess=_SubprocessRecorder(),
            out=lambda _s: None,
            err=err_log.append,
        )
        assert errs == 1
        assert any("unknown" in m for m in err_log)

    def test_oserror_during_service_action_counted(self, tmp_path: Path) -> None:
        def raising(*_args, **_kwargs):
            raise OSError("simulated systemctl failure")

        plan = DismissPlan(
            kind="team", target_name="x",
            removals=(), edits=(),
            service_actions=(
                ServiceAction(
                    "stop_disable_unit",
                    "slack-bridge-multi.service",
                    "stop & disable",
                ),
            ),
            manual_reminders=(),
        )
        err_log: list[str] = []
        errs = execute_plan(
            plan,
            run_subprocess=raising,
            out=lambda _s: None,
            err=err_log.append,
        )
        assert errs == 1
        assert any("simulated" in m for m in err_log)

    def test_oserror_during_edit_counted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Atomic edit goes tmp.write_text -> os.replace. Simulate the
        # rename step failing (e.g. permission denied on the target).
        f = tmp_path / "x.yaml"
        f.write_text("old\n")

        def raising_replace(*_args, **_kwargs):
            raise OSError("denied")

        monkeypatch.setattr(
            "tigerharness.dismiss.os.replace", raising_replace,
        )
        plan = DismissPlan(
            kind="team", target_name="x",
            removals=(),
            edits=(FileEdit(f, "rewrite", "new\n"),),
            service_actions=(), manual_reminders=(),
        )
        err_log: list[str] = []
        errs = execute_plan(
            plan, out=lambda _s: None, err=err_log.append,
        )
        assert errs == 1
        assert any("denied" in m for m in err_log)

    def test_oserror_during_removal_counted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        f = tmp_path / "x.txt"
        f.write_text("z")

        def raising_unlink(self, *_args, **_kwargs):
            raise OSError("eperm")

        monkeypatch.setattr(Path, "unlink", raising_unlink)
        plan = DismissPlan(
            kind="team", target_name="x",
            removals=(FileRemoval(f, "x"),),
            edits=(), service_actions=(), manual_reminders=(),
        )
        err_log: list[str] = []
        errs = execute_plan(
            plan, out=lambda _s: None, err=err_log.append,
        )
        assert errs == 1
        assert any("eperm" in m for m in err_log)


# ---------------------------------------------------------------------------
# _prompt_yes_no
# ---------------------------------------------------------------------------

class TestPromptYesNo:
    def test_empty_returns_default_false(self) -> None:
        q = _InputQueue([""])
        assert _prompt_yes_no("ok?", default=False, input_fn=q) is False

    def test_empty_returns_default_true(self) -> None:
        q = _InputQueue([""])
        assert _prompt_yes_no("ok?", default=True, input_fn=q) is True

    def test_yes_variants(self) -> None:
        assert _prompt_yes_no(
            "?", default=False, input_fn=_InputQueue(["y"]),
        ) is True
        assert _prompt_yes_no(
            "?", default=False, input_fn=_InputQueue(["yes"]),
        ) is True
        assert _prompt_yes_no(
            "?", default=False, input_fn=_InputQueue(["YES"]),
        ) is True

    def test_no_variants(self) -> None:
        assert _prompt_yes_no(
            "?", default=True, input_fn=_InputQueue(["n"]),
        ) is False
        assert _prompt_yes_no(
            "?", default=True, input_fn=_InputQueue(["no"]),
        ) is False

    def test_invalid_then_valid_retries(self) -> None:
        q = _InputQueue(["maybe", "huh", "y"])
        out_log: list[str] = []
        assert _prompt_yes_no(
            "?", default=False, input_fn=q, out=out_log.append,
        ) is True
        assert any("'y'" in m for m in out_log)


# ---------------------------------------------------------------------------
# _pick_target
# ---------------------------------------------------------------------------

class TestPickTarget:
    def test_no_teams_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="no teams"):
            _pick_target(tmp_path, input_fn=_InputQueue([]))

    def test_flat_layout_refused(self, tmp_path: Path) -> None:
        # teams_root IS itself a team
        (tmp_path / "configs").mkdir()
        (tmp_path / "configs" / "personas.yaml").write_text(
            "personas: []\n"
        )
        with pytest.raises(ValueError, match="itself a team"):
            _pick_target(tmp_path, input_fn=_InputQueue([]))

    def test_pick_team(self, tmp_path: Path) -> None:
        # Single-team auto-skip: no Team prompt fires.
        _make_team(tmp_path, "shohoku", ["ayako"])
        q = _InputQueue(["1"])
        kind, team, persona = _pick_target(
            tmp_path, input_fn=q, out=lambda _s: None,
        )
        assert (kind, team, persona) == ("team", "shohoku", None)

    def test_pick_persona(self, tmp_path: Path) -> None:
        # Single-team auto-skip: queue is kind, persona.
        _make_team(tmp_path, "shohoku", ["ayako", "sakuragi"])
        q = _InputQueue(["2", "2"])
        kind, team, persona = _pick_target(
            tmp_path, input_fn=q, out=lambda _s: None,
        )
        assert (kind, team, persona) == ("persona", "shohoku", "sakuragi")

    def test_invalid_kind_retries(self, tmp_path: Path) -> None:
        _make_team(tmp_path, "shohoku", ["ayako"])
        q = _InputQueue(["9", "wat", "1"])
        out_log: list[str] = []
        kind, *_ = _pick_target(
            tmp_path, input_fn=q, out=out_log.append,
        )
        assert kind == "team"
        assert any("1 or 2" in m for m in out_log)

    def test_invalid_team_selection_retries(self, tmp_path: Path) -> None:
        # 2+ teams needed -- single-team case auto-skips the prompt.
        _make_team(tmp_path, "shohoku", ["ayako"])
        _make_team(tmp_path, "tigers", ["sai"])
        q = _InputQueue(["1", "99", "x", "1"])
        out_log: list[str] = []
        _pick_target(tmp_path, input_fn=q, out=out_log.append)
        assert any("number 1-2" in m for m in out_log)

    def test_invalid_persona_selection_retries(self, tmp_path: Path) -> None:
        _make_team(tmp_path, "shohoku", ["ayako", "sakuragi"])
        # Single-team auto-skips team prompt -> queue is kind + bad
        # persona inputs + valid persona idx.
        q = _InputQueue(["2", "99", "blah", "1"])
        out_log: list[str] = []
        _pick_target(tmp_path, input_fn=q, out=out_log.append)
        assert any("number 1-2" in m for m in out_log)

    def test_team_with_no_personas_for_persona_dismiss(
        self, tmp_path: Path
    ) -> None:
        # Hand-build a team with personas.yaml but no persona prompt dirs
        team_dir = tmp_path / "shell"
        (team_dir / "configs").mkdir(parents=True)
        (team_dir / "configs" / "personas.yaml").write_text(
            "personas: []\n"
        )
        # Single-team auto-skip: queue is just the kind selection.
        q = _InputQueue(["2"])
        with pytest.raises(ValueError, match="no personas"):
            _pick_target(tmp_path, input_fn=q, out=lambda _s: None)


# ---------------------------------------------------------------------------
# main() CLI flow
# ---------------------------------------------------------------------------

class TestMainCli:
    def _patch_main_io(
        self,
        monkeypatch: pytest.MonkeyPatch,
        answers: list[str],
        subprocess_recorder: _SubprocessRecorder | None = None,
    ) -> _InputQueue:
        q = _InputQueue(answers)
        monkeypatch.setattr("builtins.input", q)
        if subprocess_recorder is not None:
            monkeypatch.setattr(
                "tigerharness.dismiss.subprocess.run",
                subprocess_recorder,
            )
        return q

    def test_dry_run_exits_zero_without_changes(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _make_team(tmp_path, "shohoku", ["ayako"])
        self._patch_main_io(monkeypatch, ["1"])
        rc = dismiss_main(["--dir", str(tmp_path), "--dry-run"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "dry-run" in captured.out
        # Team dir still exists
        assert (tmp_path / "shohoku").exists()

    def test_full_team_dismiss_happy_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        _make_team(tmp_path, "shohoku", ["ayako"], with_slack_env=True)

        # picker=team (auto-skip team prompt), backup=y, type-name=shohoku
        self._patch_main_io(
            monkeypatch, ["1", "y", "shohoku"],
            subprocess_recorder=_SubprocessRecorder(),
        )
        rc = dismiss_main(["--dir", str(tmp_path)])
        assert rc == 0
        assert not (tmp_path / "shohoku").exists()
        out = capsys.readouterr().out
        assert "done." in out
        assert "Remaining manual steps" in out

    def test_persona_dismiss_happy_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _make_team(tmp_path, "shohoku", ["ayako", "sakuragi"])
        # 2=persona-kind, (auto-skip team), 2=sakuragi, backup=y, type-name
        self._patch_main_io(
            monkeypatch, ["2", "2", "y", "shohoku/sakuragi"],
        )
        rc = dismiss_main(["--dir", str(tmp_path)])
        assert rc == 0
        assert not (tmp_path / "shohoku" / "personas" / "sakuragi").exists()
        assert not (tmp_path / "shohoku" / "memories" / "sakuragi").exists()
        yaml = (tmp_path / "shohoku" / "configs" / "personas.yaml").read_text()
        assert "sakuragi" not in yaml
        # ayako survives
        assert (tmp_path / "shohoku" / "personas" / "ayako").exists()

    def test_backup_no_aborts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _make_team(tmp_path, "shohoku", ["ayako"])
        self._patch_main_io(monkeypatch, ["1", "n"])
        rc = dismiss_main(["--dir", str(tmp_path)])
        assert rc == 1
        out = capsys.readouterr().out
        assert "back up first" in out
        # Nothing deleted
        assert (tmp_path / "shohoku").exists()

    def test_typed_name_mismatch_aborts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _make_team(tmp_path, "shohoku", ["ayako"])
        self._patch_main_io(monkeypatch, ["1", "y", "wrong"])
        rc = dismiss_main(["--dir", str(tmp_path)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "expected" in err
        assert (tmp_path / "shohoku").exists()

    def test_value_error_in_planning_exits_one(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # No teams = ValueError from _pick_target
        self._patch_main_io(monkeypatch, [])
        rc = dismiss_main(["--dir", str(tmp_path)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "no teams" in err

    def test_eof_during_picker_returns_130(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _make_team(tmp_path, "shohoku", ["ayako"])

        def raising_input(_prompt=""):
            raise EOFError

        monkeypatch.setattr("builtins.input", raising_input)
        rc = dismiss_main(["--dir", str(tmp_path)])
        assert rc == 130
        assert "aborted" in capsys.readouterr().err

    def test_eof_during_confirm_returns_130(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _make_team(tmp_path, "shohoku", ["ayako"])
        # picker answer (kind, then auto-skip team), then EOFError on backup
        answers = iter(["1"])

        def fake_input(_prompt=""):
            try:
                return next(answers)
            except StopIteration:
                raise EOFError

        monkeypatch.setattr("builtins.input", fake_input)
        rc = dismiss_main(["--dir", str(tmp_path)])
        assert rc == 130

    def test_execute_errors_propagate_as_exit_one(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _make_team(tmp_path, "shohoku", ["ayako"])
        self._patch_main_io(monkeypatch, ["1", "y", "shohoku"])

        def raising_rmtree(*_args, **_kwargs):
            raise OSError("simulated rmtree failure")

        monkeypatch.setattr(
            "tigerharness.dismiss.shutil.rmtree", raising_rmtree,
        )
        rc = dismiss_main(["--dir", str(tmp_path)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "simulated" in err
        assert "completed with 1 error" in err

    def test_main_with_no_manual_reminders_omits_section(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Persona dismiss has no manual reminders
        _make_team(tmp_path, "shohoku", ["ayako", "sakuragi"])
        self._patch_main_io(
            monkeypatch,
            ["2", "2", "y", "shohoku/sakuragi"],
        )
        rc = dismiss_main(["--dir", str(tmp_path)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "Remaining manual steps" not in out


# ---------------------------------------------------------------------------
# Module entrypoint
# ---------------------------------------------------------------------------

def test_module_main_entrypoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the `if __name__ == "__main__"` line by importing the
    module under that guard."""
    import runpy
    _make_team(tmp_path, "shohoku", ["ayako"])
    monkeypatch.setattr("sys.argv", [
        "tigerharness-dismiss", "--dir", str(tmp_path), "--dry-run",
    ])
    monkeypatch.setattr("builtins.input", _InputQueue(["1"]))
    with pytest.raises(SystemExit) as exc_info:
        runpy.run_module("tigerharness.dismiss", run_name="__main__")
    assert exc_info.value.code == 0


# ---------------------------------------------------------------------------
# CLI top-level dispatch
# ---------------------------------------------------------------------------

def test_top_level_cli_dispatches_dismiss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from tigerharness.cli import main as cli_main
    _make_team(tmp_path, "shohoku", ["ayako"])
    monkeypatch.setattr("builtins.input", _InputQueue(["1"]))
    rc = cli_main(["dismiss", "--dir", str(tmp_path), "--dry-run"])
    assert rc == 0
    assert "Dismiss plan" in capsys.readouterr().out


def test_top_level_cli_usage_lists_dismiss(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from tigerharness.cli import main as cli_main
    rc = cli_main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "dismiss" in out


# ===========================================================================
# Critique-round fixes: B1, B2, B3, P1, P2, N1, N2, N3
# ===========================================================================

# ---------------------------------------------------------------------------
# B1 -- _has_persona_entry (whitespace-tolerant detection)
# ---------------------------------------------------------------------------

class TestHasPersonaEntry:
    def test_canonical_format(self) -> None:
        text = "personas:\n  - name: ayako\n    cwd: ..\n"
        assert _has_persona_entry(text, "ayako") is True

    def test_double_space_after_colon(self) -> None:
        # User hand-edited to "- name:  ayako" -- previously missed.
        text = "personas:\n  - name:  ayako\n    cwd: ..\n"
        assert _has_persona_entry(text, "ayako") is True

    def test_absent_persona_returns_false(self) -> None:
        text = "personas:\n  - name: ayako\n"
        assert _has_persona_entry(text, "sakuragi") is False

    def test_substring_match_does_not_count(self) -> None:
        # "ayako_extra" must not match "ayako"
        text = "personas:\n  - name: ayako_extra\n"
        assert _has_persona_entry(text, "ayako") is False

    def test_b1_persona_plan_includes_edit_for_double_spaced_entry(
        self, tmp_path: Path
    ) -> None:
        # End-to-end regression: the planning step must produce an edit
        # even when the user's personas.yaml has off-canonical whitespace.
        _make_team(tmp_path, "shohoku", ["ayako", "sakuragi"])
        yaml_path = tmp_path / "shohoku" / "configs" / "personas.yaml"
        text = yaml_path.read_text().replace(
            "  - name: sakuragi\n", "  - name:  sakuragi\n",
        )
        yaml_path.write_text(text)
        plan = build_persona_plan(
            team="shohoku", persona="sakuragi", teams_root=tmp_path,
        )
        assert plan.edits, (
            "expected an edit for a present-but-double-spaced entry"
        )
        assert "sakuragi" not in plan.edits[0].new_content


# ---------------------------------------------------------------------------
# B2 -- _is_safe_state_dir + plan integration
# ---------------------------------------------------------------------------

class TestIsSafeStateDir:
    def test_canonical_state_dir_is_safe(self, tmp_path: Path) -> None:
        # tmp_path is plenty deep, last component will match the team.
        sd = tmp_path / "state" / "slack-bridge" / "shohoku"
        sd.mkdir(parents=True)
        assert _is_safe_state_dir(sd, "shohoku") is True

    def test_mismatched_name_is_unsafe(self, tmp_path: Path) -> None:
        sd = tmp_path / "state" / "slack-bridge" / "tigers"
        sd.mkdir(parents=True)
        assert _is_safe_state_dir(sd, "shohoku") is False

    def test_shallow_path_is_unsafe(self) -> None:
        # depth < 4 -- even if the name matches, refuse.
        assert _is_safe_state_dir(Path("/shohoku"), "shohoku") is False


class TestPlanRefusesUnsafeStateDir:
    def test_unsafe_state_dir_skipped_with_reminder(
        self, tmp_path: Path
    ) -> None:
        # state_dir points OUTSIDE the canonical location and doesn't
        # carry the team name. Plan must refuse to nuke it.
        rogue = tmp_path / "elsewhere" / "important-stuff"
        rogue.mkdir(parents=True)
        (rogue / "data").write_text("x")
        _make_team(
            tmp_path, "shohoku", ["ayako"],
            with_slack_env=True,
            with_fragment="default_persona",
            fragment_state_dir=str(rogue),
        )
        plan = build_team_plan(
            team="shohoku", teams_root=tmp_path,
            home=tmp_path / "home",
        )
        rm_paths = [r.path for r in plan.removals]
        assert rogue not in rm_paths
        assert any(
            "doesn't look like a per-team state directory" in r
            for r in plan.manual_reminders
        )


# ---------------------------------------------------------------------------
# B3 -- _read_env_files_from_unit + plan integration
# ---------------------------------------------------------------------------

class TestOwnershipHelpers:
    """_resolves_inside + _read_bridges_config_from_env -- the
    content-based ownership signals behind the root-containment fix."""

    def test_resolves_inside_none_and_outside(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        assert dismiss_mod._resolves_inside(None, root) is False
        assert dismiss_mod._resolves_inside(root / "x.env", root) is True
        assert dismiss_mod._resolves_inside(tmp_path / "y.env", root) is False

    def test_resolves_inside_oserror_counts_as_outside(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        target = root / "x.env"
        real_resolve = Path.resolve

        def flaky_resolve(self, *a, **k):
            if self.name == "x.env":
                raise OSError("loop")
            return real_resolve(self, *a, **k)

        monkeypatch.setattr(Path, "resolve", flaky_resolve)
        assert dismiss_mod._resolves_inside(target, root) is False

    def test_read_bridges_config_skips_comments_and_empty(
        self, tmp_path: Path,
    ) -> None:
        env = tmp_path / "a.env"
        env.write_text(
            "# a comment\n"
            "TIGERHARNESS_BRIDGES_CONFIG=\n"   # empty value -> keep looking
            "TIGERHARNESS_BRIDGES_CONFIG='/x/y.yaml'\n"
        )
        assert dismiss_mod._read_bridges_config_from_env(env) == Path("/x/y.yaml")

    def test_read_bridges_config_absent_or_unreadable(
        self, tmp_path: Path,
    ) -> None:
        missing = tmp_path / "missing.env"
        assert dismiss_mod._read_bridges_config_from_env(missing) is None
        other = tmp_path / "other.env"
        other.write_text("SOMETHING_ELSE=1\n")
        assert dismiss_mod._read_bridges_config_from_env(other) is None


class TestReadEnvFilesFromUnit:
    def test_returns_empty_when_unit_missing(self, tmp_path: Path) -> None:
        assert _read_env_files_from_unit(tmp_path / "no.service") == []

    def test_reads_single_environment_file(self, tmp_path: Path) -> None:
        unit = tmp_path / "x.service"
        unit.write_text(
            "[Unit]\nDescription=x\n"
            "[Service]\nEnvironmentFile=/etc/foo.env\nExecStart=/bin/true\n"
        )
        assert _read_env_files_from_unit(unit) == [Path("/etc/foo.env")]

    def test_expands_tilde(self, tmp_path: Path) -> None:
        unit = tmp_path / "x.service"
        unit.write_text("EnvironmentFile=~/foo.env\n")
        result = _read_env_files_from_unit(unit)
        assert len(result) == 1
        assert "~" not in str(result[0])

    def test_skips_commented_environment_file(
        self, tmp_path: Path
    ) -> None:
        unit = tmp_path / "x.service"
        unit.write_text("# EnvironmentFile=/ignored\n")
        assert _read_env_files_from_unit(unit) == []

    def test_reads_multiple_environment_file_lines(
        self, tmp_path: Path
    ) -> None:
        unit = tmp_path / "x.service"
        unit.write_text(
            "EnvironmentFile=/etc/a.env\n"
            "EnvironmentFile=/etc/b.env\n"
        )
        assert _read_env_files_from_unit(unit) == [
            Path("/etc/a.env"), Path("/etc/b.env"),
        ]


class TestPlanUsesUnitEnvFile:
    def test_env_file_from_unit_used_and_in_root_default_cleaned(
        self, tmp_path: Path
    ) -> None:
        # Unit references a custom env file inside the root; the
        # canonical `multi-bridge.env` is also present. Full teardown
        # of this root's bridge removes BOTH -- the custom one because
        # the owned unit references it, the canonical one because it
        # lives inside the operated root by construction and is this
        # root's bridge debris either way. (Behavior change with the
        # 2026-06 root-containment fix: the old plan left the stale
        # default behind.)
        home = tmp_path / "home"
        unit_dir = home / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        custom_env = tmp_path / "custom-bridges.env"
        custom_env.write_text("TIGERHARNESS_BRIDGES_CONFIG=...\n")
        (tmp_path / "multi-bridge.env").write_text("# default, unused\n")
        (unit_dir / "slack-bridge-multi.service").write_text(
            f"EnvironmentFile={custom_env}\n"
        )

        _make_team(
            tmp_path, "shohoku", ["ayako"],
            with_slack_env=True, with_fragment="default_persona",
        )
        _write_index(tmp_path, ["shohoku"])

        plan = build_team_plan(
            team="shohoku", teams_root=tmp_path, home=home,
        )
        rm_paths = [r.path for r in plan.removals]
        assert custom_env in rm_paths
        assert (tmp_path / "multi-bridge.env") in rm_paths

    def test_falls_back_to_default_env_file_when_unit_absent(
        self, tmp_path: Path
    ) -> None:
        # No unit file at all -- fall back to <teams-root>/multi-bridge.env.
        home = tmp_path / "home"
        home.mkdir()
        (tmp_path / "multi-bridge.env").write_text("...")
        _make_team(
            tmp_path, "shohoku", ["ayako"],
            with_slack_env=True, with_fragment="default_persona",
        )
        _write_index(tmp_path, ["shohoku"])

        plan = build_team_plan(
            team="shohoku", teams_root=tmp_path, home=home,
        )
        rm_paths = [r.path for r in plan.removals]
        assert (tmp_path / "multi-bridge.env") in rm_paths

    def test_unit_env_file_skipped_if_path_missing(
        self, tmp_path: Path
    ) -> None:
        # Unit references an env file that doesn't exist on disk --
        # just skip it (nothing to remove).
        home = tmp_path / "home"
        unit_dir = home / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        (unit_dir / "slack-bridge-multi.service").write_text(
            "EnvironmentFile=/nonexistent.env\n"
        )
        _make_team(
            tmp_path, "shohoku", ["ayako"],
            with_slack_env=True, with_fragment="default_persona",
        )
        _write_index(tmp_path, ["shohoku"])

        plan = build_team_plan(
            team="shohoku", teams_root=tmp_path, home=home,
        )
        rm_paths = [r.path for r in plan.removals]
        # Neither the nonexistent path nor (since the unit DID reference
        # one) the default fallback is present.
        assert Path("/nonexistent.env") not in rm_paths
        assert (tmp_path / "multi-bridge.env") not in rm_paths

    def test_unit_with_duplicate_env_files_deduplicates(
        self, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        unit_dir = home / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        env_file = tmp_path / "shared.env"
        env_file.write_text("...")
        (unit_dir / "slack-bridge-multi.service").write_text(
            f"EnvironmentFile={env_file}\n"
            f"EnvironmentFile={env_file}\n"
        )
        _make_team(
            tmp_path, "shohoku", ["ayako"],
            with_slack_env=True, with_fragment="default_persona",
        )
        _write_index(tmp_path, ["shohoku"])

        plan = build_team_plan(
            team="shohoku", teams_root=tmp_path, home=home,
        )
        env_removals = [
            r for r in plan.removals if r.path == env_file
        ]
        assert len(env_removals) == 1


# ---------------------------------------------------------------------------
# P1 -- atomic file writes
# ---------------------------------------------------------------------------

class TestAtomicEditWrites:
    def test_uses_os_replace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The atomic-write path goes through os.replace; verify the
        # call lands.
        f = tmp_path / "thing.yaml"
        f.write_text("old\n")
        replace_calls: list[tuple[str, str]] = []
        original = os.replace

        def recording_replace(src, dst):
            replace_calls.append((str(src), str(dst)))
            return original(src, dst)

        monkeypatch.setattr(
            "tigerharness.dismiss.os.replace", recording_replace,
        )
        plan = DismissPlan(
            kind="team", target_name="x",
            removals=(),
            edits=(FileEdit(f, "rewrite", "new\n"),),
            service_actions=(), manual_reminders=(),
        )
        assert execute_plan(plan, out=lambda _s: None) == 0
        assert f.read_text() == "new\n"
        assert len(replace_calls) == 1
        # Source path is the .dismiss-tmp scratch file
        assert replace_calls[0][0].endswith(".dismiss-tmp")
        # Destination is the real target
        assert replace_calls[0][1] == str(f)

    def test_no_tmp_file_left_after_success(
        self, tmp_path: Path
    ) -> None:
        f = tmp_path / "thing.yaml"
        f.write_text("old\n")
        plan = DismissPlan(
            kind="team", target_name="x", removals=(),
            edits=(FileEdit(f, "rewrite", "new\n"),),
            service_actions=(), manual_reminders=(),
        )
        execute_plan(plan, out=lambda _s: None)
        leftover = list(tmp_path.glob("*.dismiss-tmp"))
        assert leftover == []


# ---------------------------------------------------------------------------
# P2 -- blocking systemctl stop + timeout on all systemctl calls
# ---------------------------------------------------------------------------

class TestSystemctlBlockingAndTimeout:
    def _plan(self, unit_file: Path) -> DismissPlan:
        return DismissPlan(
            kind="team", target_name="x", removals=(), edits=(),
            service_actions=(
                ServiceAction(
                    "stop_disable_unit",
                    "slack-bridge-multi.service",
                    "stop & disable",
                ),
                ServiceAction(
                    "remove_unit_file", str(unit_file), "rm unit",
                ),
                ServiceAction(
                    "daemon_reload", "", "reload",
                ),
            ),
            manual_reminders=(),
        )

    def test_stop_is_blocking(self, tmp_path: Path) -> None:
        unit = tmp_path / "x.service"
        unit.write_text("x")
        recorder = _SubprocessRecorder()
        execute_plan(
            self._plan(unit),
            run_subprocess=recorder, out=lambda _s: None,
        )
        # First call is stop; --no-block must NOT appear.
        assert "stop" in recorder.calls[0]
        assert "--no-block" not in recorder.calls[0]

    def test_all_systemctl_calls_carry_a_timeout(
        self, tmp_path: Path
    ) -> None:
        unit = tmp_path / "x.service"
        unit.write_text("x")
        recorder = _SubprocessRecorder()
        execute_plan(
            self._plan(unit),
            run_subprocess=recorder, out=lambda _s: None,
        )
        for i, kw in enumerate(recorder.kwargs):
            assert kw.get("timeout"), (
                f"call {i} ({recorder.calls[i]}) missing timeout kwarg"
            )

    def test_timeout_expired_counts_as_error(
        self, tmp_path: Path
    ) -> None:
        unit = tmp_path / "x.service"
        unit.write_text("x")

        def hanging(*args, **kwargs):
            import subprocess as _sp
            raise _sp.TimeoutExpired(cmd=args[0], timeout=1.0)

        err_log: list[str] = []
        errs = execute_plan(
            self._plan(unit),
            run_subprocess=hanging,
            out=lambda _s: None, err=err_log.append,
        )
        # Both the stop and daemon-reload paths raise -- 2 errors.
        assert errs >= 1
        # subprocess.TimeoutExpired.__str__ produces "...timed out..."
        assert any("timed out" in m.lower() for m in err_log)


# ---------------------------------------------------------------------------
# N1 -- single-team auto-skip in the picker
# ---------------------------------------------------------------------------

class TestSingleTeamAutoSkip:
    def test_single_team_skips_team_selection_prompt(
        self, tmp_path: Path
    ) -> None:
        _make_team(tmp_path, "shohoku", ["ayako"])
        # Only one team -- after picking kind=1, no team prompt should
        # be issued. Queue has just the kind selection.
        q = _InputQueue(["1"])
        out_log: list[str] = []
        kind, team, persona = _pick_target(
            tmp_path, input_fn=q, out=out_log.append,
        )
        assert (kind, team, persona) == ("team", "shohoku", None)
        # The picker printed "Only one team available: shohoku ..."
        assert any("Only one team available" in m for m in out_log)
        # And it did NOT prompt for a team number.
        assert not any(p.startswith("Team [1-") for p in q.prompts)

    def test_two_teams_still_prompt(self, tmp_path: Path) -> None:
        _make_team(tmp_path, "shohoku", ["ayako"])
        _make_team(tmp_path, "tigers", ["sai"])
        q = _InputQueue(["1", "1"])
        _pick_target(tmp_path, input_fn=q, out=lambda _s: None)
        # Team prompt was issued
        assert any(p.startswith("Team [1-") for p in q.prompts)


# ---------------------------------------------------------------------------
# N2 -- relative paths in preview
# ---------------------------------------------------------------------------

class TestFormatPath:
    def test_returns_relative_when_under_base(self, tmp_path: Path) -> None:
        p = tmp_path / "a" / "b"
        assert _format_path(p, tmp_path) == "a/b"

    def test_returns_absolute_when_outside_base(
        self, tmp_path: Path
    ) -> None:
        p = Path("/etc/passwd")
        assert _format_path(p, tmp_path) == "/etc/passwd"

    def test_returns_absolute_when_base_is_none(
        self, tmp_path: Path
    ) -> None:
        p = tmp_path / "a"
        assert _format_path(p, None) == str(p)


class TestRenderPreviewWithTeamsRoot:
    def test_relative_paths_used_under_teams_root(
        self, tmp_path: Path
    ) -> None:
        team_path = tmp_path / "shohoku"
        plan = DismissPlan(
            kind="team", target_name="shohoku",
            removals=(FileRemoval(team_path, "team dir"),),
            edits=(), service_actions=(), manual_reminders=(),
        )
        out = render_preview(plan, teams_root=tmp_path)
        assert "shohoku" in out
        assert str(team_path) not in out  # not the absolute form

    def test_paths_outside_teams_root_stay_absolute(
        self, tmp_path: Path
    ) -> None:
        plan = DismissPlan(
            kind="team", target_name="x",
            removals=(FileRemoval(Path("/etc/passwd"), "x"),),
            edits=(), service_actions=(), manual_reminders=(),
        )
        out = render_preview(plan, teams_root=tmp_path)
        assert "/etc/passwd" in out


# ---------------------------------------------------------------------------
# N3 -- q/quit aborts the picker
# ---------------------------------------------------------------------------

class TestMaybeAbort:
    def test_q_raises(self) -> None:
        with pytest.raises(KeyboardInterrupt):
            _maybe_abort("q")

    def test_quit_raises(self) -> None:
        with pytest.raises(KeyboardInterrupt):
            _maybe_abort("quit")

    def test_uppercase_q_raises(self) -> None:
        with pytest.raises(KeyboardInterrupt):
            _maybe_abort("Q")

    def test_normal_input_passes_through(self) -> None:
        _maybe_abort("1")  # no exception


# ===========================================================================
# Second critique round: B4 (quote-stripping), B5 (relative state_dir),
# P3 (tmp cleanup), timeout bump
# ===========================================================================

# ---------------------------------------------------------------------------
# B4 -- _unquote + integration with each line reader
# ---------------------------------------------------------------------------

class TestUnquote:
    def test_double_quoted(self) -> None:
        assert _unquote('"foo"') == "foo"

    def test_single_quoted(self) -> None:
        assert _unquote("'foo'") == "foo"

    def test_unquoted_passthrough(self) -> None:
        assert _unquote("foo") == "foo"

    def test_mismatched_quotes_left_alone(self) -> None:
        # We only strip a fully-matching pair -- mismatched is suspicious
        # and we'd rather keep them visible than silently munge.
        assert _unquote("\"foo'") == "\"foo'"

    def test_single_quote_char_left_alone(self) -> None:
        # A single character can't be a quoted pair.
        assert _unquote('"') == '"'

    def test_empty_string(self) -> None:
        assert _unquote("") == ""


class TestQuotedYamlInputs:
    def test_quoted_default_persona_detected(self, tmp_path: Path) -> None:
        frag = tmp_path / "frag.yaml"
        frag.write_text('default_persona: "ayako"\n')
        assert _read_default_persona(frag) == "ayako"

    def test_quoted_legacy_persona_detected(self, tmp_path: Path) -> None:
        frag = tmp_path / "frag.yaml"
        frag.write_text("persona: 'ayako'\n")
        assert _read_default_persona(frag) == "ayako"

    def test_quoted_state_dir_path(self, tmp_path: Path) -> None:
        frag = tmp_path / "frag.yaml"
        frag.write_text('state_dir: "~/state/x"\n')
        out = _read_state_dir(frag)
        assert out is not None
        assert '"' not in str(out)
        assert str(out).endswith("/state/x")

    def test_quoted_lane_in_index_parsed(self, tmp_path: Path) -> None:
        index = tmp_path / "slack-bridge.yaml"
        index.write_text('lanes:\n  - "shohoku"\n  - tigers\n')
        assert _read_lanes_from_index(index) == ["shohoku", "tigers"]

    def test_quoted_lane_in_index_removable(self) -> None:
        text = 'lanes:\n  - "shohoku"\n  - tigers\n'
        out = _remove_lane_from_index(text, "shohoku")
        # The literal `- "shohoku"` line is dropped despite the quotes;
        # without _unquote the match would have missed and left it.
        assert '"shohoku"' not in out
        assert "  - tigers\n" in out

    def test_quoted_default_persona_blocks_persona_dismiss(
        self, tmp_path: Path,
    ) -> None:
        # End-to-end regression: quoted default_persona must still trip
        # the "can't dismiss the team's default" refusal.
        _make_team(
            tmp_path, "shohoku", ["ayako", "sakuragi"],
            with_fragment="default_persona",
        )
        frag = tmp_path / "shohoku" / "configs" / "slack-bridge.yaml"
        text = frag.read_text().replace(
            "default_persona: ayako",
            'default_persona: "ayako"',
        )
        frag.write_text(text)
        with pytest.raises(ValueError, match="default_persona"):
            build_persona_plan(
                team="shohoku", persona="ayako", teams_root=tmp_path,
            )


# ---------------------------------------------------------------------------
# B5 -- relative state_dir resolves against the team dir
# ---------------------------------------------------------------------------

class TestRelativeStateDir:
    def test_absolute_path_unchanged_with_base(self, tmp_path: Path) -> None:
        frag = tmp_path / "frag.yaml"
        frag.write_text("state_dir: /tmp/some/abs\n")
        result = _read_state_dir(frag, base=tmp_path)
        assert result == Path("/tmp/some/abs")

    def test_relative_path_resolved_against_base(self, tmp_path: Path) -> None:
        frag = tmp_path / "frag.yaml"
        frag.write_text("state_dir: state/x\n")
        result = _read_state_dir(frag, base=tmp_path)
        assert result == (tmp_path / "state" / "x").resolve()

    def test_relative_path_without_base_returns_relative(
        self, tmp_path: Path,
    ) -> None:
        frag = tmp_path / "frag.yaml"
        frag.write_text("state_dir: state/x\n")
        result = _read_state_dir(frag)  # no base
        assert result == Path("state/x")
        assert not result.is_absolute()

    def test_team_plan_resolves_relative_state_dir(
        self, tmp_path: Path,
    ) -> None:
        # state_dir relative to the team dir, with the team name as the
        # leaf so the B2 safety check accepts it. The slack-bridge
        # loader uses the same `_resolve(rel, team_dir)` convention --
        # dismiss must agree.
        _make_team(
            tmp_path, "shohoku", ["ayako"],
            with_slack_env=True, with_fragment="default_persona",
        )
        absolute_state = tmp_path / "shohoku" / "state" / "shohoku"
        absolute_state.mkdir(parents=True)
        (absolute_state / "threads.json").write_text("{}")
        frag = tmp_path / "shohoku" / "configs" / "slack-bridge.yaml"
        frag.write_text(
            frag.read_text() + "state_dir: state/shohoku\n"
        )

        plan = build_team_plan(
            team="shohoku", teams_root=tmp_path,
            home=tmp_path / "home",
        )
        rm_paths = [r.path for r in plan.removals]
        assert absolute_state.resolve() in rm_paths


# ---------------------------------------------------------------------------
# P3 -- tmp file cleanup on os.replace failure
# ---------------------------------------------------------------------------

class TestAtomicWriteFailureCleanup:
    def test_tmp_removed_when_replace_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        f = tmp_path / "thing.yaml"
        f.write_text("old\n")

        def raising_replace(*_args, **_kwargs):
            raise OSError("permission denied")

        monkeypatch.setattr(
            "tigerharness.dismiss.os.replace", raising_replace,
        )
        plan = DismissPlan(
            kind="team", target_name="x", removals=(),
            edits=(FileEdit(f, "rewrite", "new\n"),),
            service_actions=(), manual_reminders=(),
        )
        errs = execute_plan(plan, out=lambda _s: None, err=lambda _s: None)
        assert errs == 1
        # Original file untouched (atomic-write guarantee)
        assert f.read_text() == "old\n"
        # No tmp left behind
        leftover = list(tmp_path.glob("*.dismiss-tmp"))
        assert leftover == [], f"unexpected tmp leftovers: {leftover}"

    def test_no_unlink_when_write_text_failed_before_creating_tmp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # If write_text itself raises (e.g. parent dir not writable),
        # no tmp ever exists. The cleanup must NOT then try to unlink
        # a nonexistent path -- exercises the `if tmp.exists()` False
        # branch in the cleanup block.
        f = tmp_path / "thing.yaml"
        f.write_text("old\n")

        original = Path.write_text

        def raising_write(self, *args, **kwargs):
            if str(self).endswith(".dismiss-tmp"):
                raise OSError("write_text refused")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", raising_write)
        plan = DismissPlan(
            kind="team", target_name="x", removals=(),
            edits=(FileEdit(f, "rewrite", "new\n"),),
            service_actions=(), manual_reminders=(),
        )
        errs = execute_plan(plan, out=lambda _s: None, err=lambda _s: None)
        assert errs == 1
        # Original file untouched
        assert f.read_text() == "old\n"

    def test_cleanup_swallows_unlink_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # If both replace AND the cleanup-unlink fail, we still surface
        # a single edit error, not a crash. Exercises the inner
        # try/except in the cleanup block.
        f = tmp_path / "thing.yaml"
        f.write_text("old\n")

        def raising_replace(*_args, **_kwargs):
            raise OSError("replace failed")

        original_unlink = Path.unlink

        def raising_unlink(self, *args, **kwargs):
            if str(self).endswith(".dismiss-tmp"):
                raise OSError("unlink failed too")
            return original_unlink(self, *args, **kwargs)

        monkeypatch.setattr(
            "tigerharness.dismiss.os.replace", raising_replace,
        )
        monkeypatch.setattr(Path, "unlink", raising_unlink)
        plan = DismissPlan(
            kind="team", target_name="x", removals=(),
            edits=(FileEdit(f, "rewrite", "new\n"),),
            service_actions=(), manual_reminders=(),
        )
        # Should not raise, should report exactly 1 edit error.
        errs = execute_plan(plan, out=lambda _s: None, err=lambda _s: None)
        assert errs == 1


# ---------------------------------------------------------------------------
# Timeout bump -- must exceed the unit template's TimeoutStopSec=120
# ---------------------------------------------------------------------------

class TestSystemctlTimeoutExceedsDrainBudget:
    def test_timeout_constant_exceeds_unit_drain_budget(self) -> None:
        # Drain budget in services/slack-bridge-multi.service is 120s.
        # Our subprocess timeout MUST exceed that or a clean drain
        # races with our hangup.
        assert _SYSTEMCTL_TIMEOUT_S > 120.0

    def test_actual_systemctl_calls_pass_the_constant(
        self, tmp_path: Path,
    ) -> None:
        unit = tmp_path / "x.service"
        unit.write_text("x")
        plan = DismissPlan(
            kind="team", target_name="x", removals=(), edits=(),
            service_actions=(
                ServiceAction(
                    "stop_disable_unit",
                    "slack-bridge-multi.service",
                    "stop & disable",
                ),
                ServiceAction(
                    "remove_unit_file", str(unit), "rm unit",
                ),
                ServiceAction(
                    "daemon_reload", "", "reload",
                ),
            ),
            manual_reminders=(),
        )
        recorder = _SubprocessRecorder()
        execute_plan(
            plan, run_subprocess=recorder, out=lambda _s: None,
        )
        for kw in recorder.kwargs:
            assert kw.get("timeout") == _SYSTEMCTL_TIMEOUT_S


# ===========================================================================
# Third critique round: B6 (quoted persona names in personas.yaml),
# B7 (trailing YAML comments), N5 (.git/ warning)
# ===========================================================================

# ---------------------------------------------------------------------------
# B6 -- quoted persona names in personas.yaml
# ---------------------------------------------------------------------------

class TestQuotedPersonaName:
    YAML_DOUBLE_QUOTED = (
        "personas_dir: ../personas\n\n"
        "personas:\n"
        '  - name: "ayako"\n'
        "    cwd: ..\n"
        "  - name: sakuragi\n"
        "    cwd: ..\n"
    )
    YAML_SINGLE_QUOTED = (
        "personas:\n"
        "  - name: 'ayako'\n"
        "    cwd: ..\n"
        "  - name: sakuragi\n"
        "    cwd: ..\n"
    )

    def test_has_entry_detects_double_quoted(self) -> None:
        assert _has_persona_entry(self.YAML_DOUBLE_QUOTED, "ayako") is True

    def test_has_entry_detects_single_quoted(self) -> None:
        assert _has_persona_entry(self.YAML_SINGLE_QUOTED, "ayako") is True

    def test_remover_handles_quoted_entry(self) -> None:
        out = _remove_persona_entry_from_yaml(
            self.YAML_DOUBLE_QUOTED, "ayako",
        )
        assert '- name: "ayako"' not in out
        assert "- name: sakuragi" in out

    def test_plan_includes_edit_for_quoted_entry(
        self, tmp_path: Path,
    ) -> None:
        # End-to-end: a hand-quoted entry must trigger the personas.yaml
        # edit, not just the persona-dir removal.
        _make_team(tmp_path, "shohoku", ["ayako", "sakuragi"])
        yaml_path = tmp_path / "shohoku" / "configs" / "personas.yaml"
        text = yaml_path.read_text().replace(
            "  - name: sakuragi\n", '  - name: "sakuragi"\n',
        )
        yaml_path.write_text(text)
        plan = build_persona_plan(
            team="shohoku", persona="sakuragi", teams_root=tmp_path,
        )
        assert plan.edits, "expected an edit for a quoted entry"
        assert '"sakuragi"' not in plan.edits[0].new_content


# ---------------------------------------------------------------------------
# B7 -- trailing YAML comments in value lines
# ---------------------------------------------------------------------------

class TestTrailingComments:
    def test_lane_line_with_comment_parsed(self, tmp_path: Path) -> None:
        index = tmp_path / "slack-bridge.yaml"
        index.write_text(
            "lanes:\n"
            "  - shohoku  # primary lane\n"
            "  - tigers   # secondary\n"
        )
        assert _read_lanes_from_index(index) == ["shohoku", "tigers"]

    def test_lane_line_with_comment_removable(self) -> None:
        text = "lanes:\n  - shohoku  # primary\n  - tigers\n"
        out = _remove_lane_from_index(text, "shohoku")
        assert "shohoku" not in out
        assert "  - tigers\n" in out

    def test_default_persona_with_comment(self, tmp_path: Path) -> None:
        frag = tmp_path / "frag.yaml"
        frag.write_text("default_persona: ayako  # team lead\n")
        assert _read_default_persona(frag) == "ayako"

    def test_state_dir_with_comment(self, tmp_path: Path) -> None:
        frag = tmp_path / "frag.yaml"
        frag.write_text("state_dir: ~/state/x  # custom location\n")
        result = _read_state_dir(frag)
        assert result is not None
        assert str(result).endswith("/state/x")
        assert "#" not in str(result)

    def test_env_file_with_comment(self, tmp_path: Path) -> None:
        unit = tmp_path / "x.service"
        unit.write_text("EnvironmentFile=/etc/foo.env  # central\n")
        assert _read_env_files_from_unit(unit) == [Path("/etc/foo.env")]

    def test_persona_entry_with_comment(self, tmp_path: Path) -> None:
        text = (
            "personas:\n"
            "  - name: ayako  # the team manager\n"
            "    cwd: ..\n"
            "  - name: sakuragi\n"
        )
        assert _has_persona_entry(text, "ayako") is True
        out = _remove_persona_entry_from_yaml(text, "ayako")
        assert "ayako" not in out
        assert "sakuragi" in out

    def test_state_dir_path_with_literal_hash_preserved(
        self, tmp_path: Path,
    ) -> None:
        # The trailing-comment match requires whitespace before the
        # '#'; a '#' inside the path itself (no preceding space) must
        # not be treated as a comment.
        frag = tmp_path / "frag.yaml"
        frag.write_text("state_dir: /var/state-#1/shohoku\n")
        result = _read_state_dir(frag)
        assert result == Path("/var/state-#1/shohoku")


# ---------------------------------------------------------------------------
# N5 -- .git/ warning before team dismiss
# ---------------------------------------------------------------------------

class TestGitWarning:
    def test_git_repo_in_team_dir_adds_reminder(
        self, tmp_path: Path,
    ) -> None:
        _make_team(tmp_path, "shohoku", ["ayako"], with_slack_env=True)
        (tmp_path / "shohoku" / ".git").mkdir()
        (tmp_path / "shohoku" / ".git" / "HEAD").write_text(
            "ref: refs/heads/main\n"
        )
        plan = build_team_plan(
            team="shohoku", teams_root=tmp_path,
            home=tmp_path / "home",
        )
        assert any(
            "git repo" in r and "unpushed" in r
            for r in plan.manual_reminders
        ), f"missing git reminder; got {plan.manual_reminders}"

    def test_no_git_no_reminder(self, tmp_path: Path) -> None:
        _make_team(tmp_path, "shohoku", ["ayako"], with_slack_env=True)
        plan = build_team_plan(
            team="shohoku", teams_root=tmp_path,
            home=tmp_path / "home",
        )
        assert not any(
            "git repo" in r for r in plan.manual_reminders
        )


class TestPickerQuitAborts:
    def test_q_at_kind_prompt(self, tmp_path: Path) -> None:
        _make_team(tmp_path, "shohoku", ["ayako"])
        q = _InputQueue(["q"])
        with pytest.raises(KeyboardInterrupt):
            _pick_target(tmp_path, input_fn=q, out=lambda _s: None)

    def test_q_at_team_prompt(self, tmp_path: Path) -> None:
        _make_team(tmp_path, "a", ["x"])
        _make_team(tmp_path, "b", ["y"])
        q = _InputQueue(["1", "q"])
        with pytest.raises(KeyboardInterrupt):
            _pick_target(tmp_path, input_fn=q, out=lambda _s: None)

    def test_q_at_persona_prompt(self, tmp_path: Path) -> None:
        _make_team(tmp_path, "shohoku", ["ayako", "sakuragi"])
        q = _InputQueue(["2", "q"])  # kind=persona, then q on team
        # Quits at team prompt? Actually with single team it
        # auto-skips, so we land on the persona prompt. Build with 1
        # team so the persona prompt is reached directly.
        with pytest.raises(KeyboardInterrupt):
            _pick_target(tmp_path, input_fn=q, out=lambda _s: None)

    def test_q_in_main_returns_130(
        self, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _make_team(tmp_path, "shohoku", ["ayako"])
        monkeypatch.setattr("builtins.input", _InputQueue(["q"]))
        rc = dismiss_main(["--dir", str(tmp_path)])
        assert rc == 130
        assert "aborted" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Space-containing names (dismiss side)
# ---------------------------------------------------------------------------

class TestSpacedNames:
    """Space-containing names through dismiss's validators and its
    hand-rolled personas.yaml / fragment / index line parsers."""

    def test_grammar_parity_with_init(self, tmp_path: Path) -> None:
        # Same grammar as init: single internal spaces valid,
        # everything else still rejected.
        _make_team(tmp_path, "shohoku", ["Chuan Ying", "sakuragi"])
        plan = build_persona_plan(
            team="shohoku", persona="Chuan Ying", teams_root=tmp_path,
        )
        assert plan.target_name == "shohoku/Chuan Ying"

    # (Leading/trailing spaces are stripped before validation --
    # parity with init -- so only internal-spacing violations reject.)
    @pytest.mark.parametrize("bad", ["Chuan  Ying", "Chuan\tYing"])
    def test_rejects_non_grammar_spacing(
        self, tmp_path: Path, bad: str,
    ) -> None:
        _make_team(tmp_path, "shohoku", ["ayako", "sakuragi"])
        with pytest.raises(ValueError, match="invalid persona name"):
            build_persona_plan(
                team="shohoku", persona=bad, teams_root=tmp_path,
            )

    def test_plan_finds_and_removes_exactly_the_spaced_persona(
        self, tmp_path: Path,
    ) -> None:
        # The find-and-remove path: the plan must locate the spaced
        # persona's folders AND strip exactly her personas.yaml entry.
        _make_team(tmp_path, "shohoku", ["Chuan Ying", "ayako"])
        plan = build_persona_plan(
            team="shohoku", persona="Chuan Ying", teams_root=tmp_path,
        )
        rm_paths = [r.path for r in plan.removals]
        assert tmp_path / "shohoku" / "personas" / "Chuan Ying" in rm_paths
        assert tmp_path / "shohoku" / "memories" / "Chuan Ying" in rm_paths
        assert len(plan.edits) == 1
        new_yaml = plan.edits[0].new_content
        assert "- name: Chuan Ying" not in new_yaml
        assert "- name: ayako" in new_yaml

    def test_entry_detection_quoted_and_unquoted(self) -> None:
        assert _has_persona_entry("  - name: Chuan Ying\n", "Chuan Ying")
        assert _has_persona_entry('  - name: "Chuan Ying"\n', "Chuan Ying")
        assert _has_persona_entry(
            "  - name: Chuan Ying  # QA\n", "Chuan Ying"
        )
        # A spaced query must not match a prefix-only row.
        assert not _has_persona_entry("  - name: Chuan\n", "Chuan Ying")

    def test_remove_entry_quoted_form(self) -> None:
        yaml_text = (
            "personas:\n"
            '  - name: "Chuan Ying"\n'
            "    cwd: ..\n"
            "  - name: ayako\n"
            "    cwd: ..\n"
        )
        out = _remove_persona_entry_from_yaml(yaml_text, "Chuan Ying")
        assert "Chuan Ying" not in out
        assert "- name: ayako" in out

    def test_read_default_persona_spaced(self, tmp_path: Path) -> None:
        frag = tmp_path / "slack-bridge.yaml"
        frag.write_text("default_persona: Chuan Ying  # coordinator\n")
        assert _read_default_persona(frag) == "Chuan Ying"

    def test_lane_index_round_trip_spaced_team(self) -> None:
        index = "lanes:\n  - Tiger Team\n  - shohoku\n"
        out = _remove_lane_from_index(index, "Tiger Team")
        assert "Tiger Team" not in out
        assert "  - shohoku\n" in out

    def test_read_persona_aliases_spaced(self) -> None:
        yaml_text = (
            "personas:\n"
            "  - name: Chuan Ying\n"
            "    aliases: [Chuan, 'C Y']\n"
        )
        assert _read_persona_aliases(yaml_text, "Chuan Ying") == [
            "Chuan", "C Y",
        ]

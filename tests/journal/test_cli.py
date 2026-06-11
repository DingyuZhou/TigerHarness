"""Tests for ``tigerharness.journal.cli``: new / list / status / sweep."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tigerharness.journal import drive_sessions, worklog
from tigerharness.journal.cli import build_parser, main
from tigerharness.journal.models import State, Status
from tigerharness.journal.paths import JournalPaths


def _seed(paths: JournalPaths, task_id: str, **over) -> None:
    paths.ensure()
    (paths.active / task_id).mkdir(exist_ok=True)
    base = dict(
        id=task_id, title=f"Task {task_id}", kind="task", persona="P",
        state=State.PENDING, sessions=0, max_sessions=5,
        created_at="2026-06-02T08:00:00Z",
        updated_at="2026-06-02T08:00:00Z",
        next_action="", session_ref=None,
    )
    base.update(over)
    s = Status(**base)
    paths.status_json(task_id).write_text(s.to_json())


def _sf(id, persona, role, on_approve, on_revise, on_block):
    """A minimal valid StepFrontmatter for graph seeding."""
    from tigerharness.journal.wfcore.models import StepFrontmatter
    return StepFrontmatter(
        id=id, persona=persona, role=role,
        on_approve=on_approve, on_revise=on_revise, on_block=on_block,
        max_iters=3, timeout_sec=600,
    )


def _seed_workflow_graph(
    paths: JournalPaths, task_id: str, steps, entrypoint,
    *, state=State.IN_PROGRESS, session_ref="tok",
):
    """Seed a fully-landed kind=workflow task: status.json
    (compile_phase=complete), orchestration.json, and one steps/<id>.md
    per step. ``steps`` is a list of StepFrontmatter; edges are derived
    from each step's own routing triple. Mirrors what land-compile
    writes, so step-done's readers see real on-disk shapes."""
    from tigerharness.journal.compile_cli import _render_frontmatter
    from tigerharness.journal.models import CompilePhase
    from tigerharness.journal.wfcore.models import (
        Orchestration,
        WorkflowConfig,
    )
    paths.ensure()
    tdir = paths.active / task_id
    (tdir / "steps").mkdir(parents=True, exist_ok=True)
    st = Status.new_workflow(
        id=task_id, title=f"WF {task_id}", playbook_name="default",
    )
    st.state = state
    st.session_ref = session_ref
    st.compile_pending = False
    st.compile_phase = CompilePhase.COMPLETE
    paths.status_json(task_id).write_text(st.to_json())
    for sf in steps:
        (tdir / "steps" / f"{sf.id}.md").write_text(
            f"---\n{_render_frontmatter(sf)}---\n"
        )
    orch = Orchestration(
        task_id=task_id, team="Shohoku", playbook="default",
        playbook_sha256="0" * 64,
        steps=[sf.id for sf in steps], entrypoint=entrypoint,
        compiled_at="2026-06-02T08:00:00Z", compiled_by="Anzai",
        edges={sf.id: sf.edges for sf in steps},
        workflow_config=WorkflowConfig(human_gate=False),
    )
    (tdir / "orchestration.json").write_text(
        json.dumps(orch.to_dict(), indent=2) + "\n"
    )


def _linear_steps():
    """A 2-step linear graph: plan(Akagi) -> build(Rukawa) -> __done__.
    REVISE self-loops; BLOCK escalates."""
    return [
        _sf("plan", "Akagi", "planner", "build", "plan", "__escalate__"),
        _sf("build", "Rukawa", "developer", "__done__", "build",
            "__escalate__"),
    ]


def _note(tmp_path, text="Did the step; notes here.\n", name="note.md"):
    p = tmp_path / name
    p.write_text(text)
    return str(p)


@pytest.fixture
def journal_dir(tmp_path):
    return tmp_path / "journal"


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

class TestParser:
    def test_requires_subcommand(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_unknown_subcommand(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["frobnicate"])


# ---------------------------------------------------------------------------
# new
# ---------------------------------------------------------------------------

class TestCmdNew:
    def test_creates_task_and_prints_summary(
        self, tmp_path, journal_dir, capsys,
    ):
        prd = tmp_path / "brief.md"
        prd.write_text("# Cache eviction\nAdd LRU to redis.\n")
        rc = main([
            "--journal-dir", str(journal_dir),
            "new", "--prd", str(prd), "--persona", "Mitsui",
            "--max-sessions", "3",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Scaffolded:" in out
        assert "Cache eviction" in out
        # File landed on disk.
        paths = JournalPaths(root=journal_dir)
        assert paths.list_active_ids()

    def test_early_exit_defaults_off_and_flag_turns_on(
        self, tmp_path, journal_dir,
    ):
        prd = tmp_path / "brief.md"
        prd.write_text("# T\nbody\n")
        paths = JournalPaths(root=journal_dir)
        # Default: no --early-exit -> run the full budget (exactly N).
        assert main(["--journal-dir", str(journal_dir),
                     "new", "--prd", str(prd), "--persona", "P"]) == 0
        tid = paths.list_active_ids()[0]
        assert Status.from_json(
            paths.status_json(tid).read_text()
        ).early_exit is False
        # With --early-exit -> driver may stop when done.
        assert main(["--journal-dir", str(journal_dir), "new", "--prd",
                     str(prd), "--persona", "P", "--early-exit"]) == 0
        other = [i for i in paths.list_active_ids() if i != tid][0]
        assert Status.from_json(
            paths.status_json(other).read_text()
        ).early_exit is True

    def test_missing_prd_returns_2(self, journal_dir, capsys):
        rc = main([
            "--journal-dir", str(journal_dir),
            "new", "--prd", "/nope/missing.md", "--persona", "P",
        ])
        assert rc == 2
        assert "not found" in capsys.readouterr().err

    def test_unsupported_kind_returns_2(
        self, tmp_path, journal_dir, capsys,
    ):
        """argparse rejects arbitrary --kind values. Phase 1.5 accepts
        only `task` and `workflow`; anything else exits 2 from argparse
        with the choices-error on stderr."""
        prd = tmp_path / "b.md"
        prd.write_text("# T\nb\n")
        with pytest.raises(SystemExit) as exc:
            main([
                "--journal-dir", str(journal_dir),
                "new", "--prd", str(prd), "--persona", "P",
                "--kind", "lab-notebook",
            ])
        assert exc.value.code == 2

    def test_explicit_title_propagates(
        self, tmp_path, journal_dir, capsys,
    ):
        prd = tmp_path / "b.md"
        prd.write_text("# From PRD\nbody\n")
        rc = main([
            "--journal-dir", str(journal_dir),
            "new", "--prd", str(prd), "--persona", "P",
            "--title", "Explicit",
            "--slug", "short",
        ])
        assert rc == 0
        paths = JournalPaths(root=journal_dir)
        tid = paths.list_active_ids()[0]
        assert "-short-" in tid
        status = Status.from_json(paths.status_json(tid).read_text())
        assert status.title == "Explicit"

    def test_blank_persona_returns_2(
        self, tmp_path, journal_dir, capsys,
    ):
        prd = tmp_path / "b.md"
        prd.write_text("# T\nbody\n")
        rc = main([
            "--journal-dir", str(journal_dir),
            "new", "--prd", str(prd), "--persona", "   ",
        ])
        assert rc == 2
        assert "persona" in capsys.readouterr().err.lower()

    def test_task_mode_rejects_workflow_only_flags(
        self, tmp_path, journal_dir, capsys,
    ):
        prd = tmp_path / "b.md"
        prd.write_text("# T\nb\n")
        rc = main([
            "--journal-dir", str(journal_dir),
            "new", "--prd", str(prd), "--persona", "P",
            "--playbook", "default",
        ])
        assert rc == 2
        assert "workflow-only" in capsys.readouterr().err

    def test_task_mode_requires_prd(self, journal_dir, capsys):
        rc = main([
            "--journal-dir", str(journal_dir),
            "new", "--persona", "P",
        ])
        assert rc == 2
        assert "--prd is required" in capsys.readouterr().err

    def test_task_mode_requires_persona(self, tmp_path, journal_dir, capsys):
        prd = tmp_path / "b.md"
        prd.write_text("# T\nb\n")
        rc = main([
            "--journal-dir", str(journal_dir),
            "new", "--prd", str(prd),
        ])
        assert rc == 2
        err = capsys.readouterr().err
        assert "--persona is required" in err
        # Mentions the team default fallback so the operator knows
        # where to set a default.
        assert "default_persona" in err

    def test_task_mode_uses_team_default_persona_when_omitted(
        self, tmp_path, journal_dir, capsys, monkeypatch,
    ):
        """When the cwd is a team root with `default_persona: X` in
        personas.yaml, `journal new --kind task` omits `--persona` and
        falls back to X."""
        # Stand up a fake team root with default_persona and chdir to it.
        team = tmp_path / "teams" / "Tigers"
        (team / "configs").mkdir(parents=True)
        (team / "configs" / "personas.yaml").write_text(
            "default_persona: Scout\n"
            "personas:\n  - name: Scout\n"
        )
        # Scout's prompt.md must exist for the validate-persona check.
        (team / "personas" / "Scout").mkdir(parents=True)
        (team / "personas" / "Scout" / "prompt.md").write_text("hi\n")
        monkeypatch.chdir(team)
        prd = tmp_path / "b.md"
        prd.write_text("# T\nb\n")
        rc = main([
            "--journal-dir", str(journal_dir),
            "new", "--prd", str(prd),
        ])
        assert rc == 0, capsys.readouterr().err
        # The scaffolder used the team default.
        paths = JournalPaths(root=journal_dir)
        tid = paths.list_active_ids()[0]
        s = Status.from_json(paths.status_json(tid).read_text())
        assert s.persona == "Scout"

    def test_task_mode_explicit_persona_overrides_default(
        self, tmp_path, journal_dir, capsys, monkeypatch,
    ):
        """An explicit `--persona Chief` wins over the team default."""
        team = tmp_path / "teams" / "Tigers"
        (team / "configs").mkdir(parents=True)
        (team / "configs" / "personas.yaml").write_text(
            "default_persona: Scout\n"
            "personas:\n  - name: Scout\n  - name: Chief\n"
        )
        for p in ("Scout", "Chief"):
            (team / "personas" / p).mkdir(parents=True)
            (team / "personas" / p / "prompt.md").write_text("hi\n")
        monkeypatch.chdir(team)
        prd = tmp_path / "b.md"
        prd.write_text("# T\nb\n")
        rc = main([
            "--journal-dir", str(journal_dir),
            "new", "--prd", str(prd), "--persona", "Chief",
        ])
        assert rc == 0
        paths = JournalPaths(root=journal_dir)
        tid = paths.list_active_ids()[0]
        s = Status.from_json(paths.status_json(tid).read_text())
        assert s.persona == "Chief"

    def test_task_mode_default_persona_resolves_alias(
        self, tmp_path, journal_dir, capsys, monkeypatch,
    ):
        """If the team writes `default_persona: Mumu` and Mumu is an
        alias of Kogure, the scaffolded task uses the canonical
        Kogure as the assignee."""
        team = tmp_path / "teams" / "Shohoku"
        (team / "configs").mkdir(parents=True)
        (team / "configs" / "personas.yaml").write_text(
            "default_persona: Mumu\n"
            "personas:\n"
            "  - name: Kogure\n"
            "    aliases: [Mumu]\n"
        )
        (team / "personas" / "Kogure").mkdir(parents=True)
        (team / "personas" / "Kogure" / "prompt.md").write_text("hi\n")
        monkeypatch.chdir(team)
        prd = tmp_path / "b.md"
        prd.write_text("# T\nb\n")
        rc = main([
            "--journal-dir", str(journal_dir),
            "new", "--prd", str(prd),
        ])
        assert rc == 0, capsys.readouterr().err
        paths = JournalPaths(root=journal_dir)
        tid = paths.list_active_ids()[0]
        s = Status.from_json(paths.status_json(tid).read_text())
        assert s.persona == "Kogure"

    def test_task_mode_explicit_persona_passes_through_unvalidated(
        self, tmp_path, journal_dir, capsys, monkeypatch,
    ):
        """Iteration on the review fix: explicit --persona is NOT
        roster-validated. Phase 1 behaviour preserved -- the operator
        typing the value seconds ago can correct the late "no
        prompt.md" error from the legacy loader. Only the default_persona
        fallback path gets the safety gate (it's a yaml typo that
        sticks around).
        """
        team = tmp_path / "teams" / "Tigers"
        (team / "configs").mkdir(parents=True)
        (team / "configs" / "personas.yaml").write_text(
            "personas:\n  - name: Scout\n"
        )
        (team / "personas" / "Scout").mkdir(parents=True)
        (team / "personas" / "Scout" / "prompt.md").write_text("hi\n")
        monkeypatch.chdir(team)
        prd = tmp_path / "b.md"
        prd.write_text("# T\nb\n")
        rc = main([
            "--journal-dir", str(journal_dir),
            "new", "--prd", str(prd), "--persona", "Mistui",  # typo
        ])
        # Phase 1 behaviour: stored as-is, no early error.
        assert rc == 0, capsys.readouterr().err
        paths = JournalPaths(root=journal_dir)
        tid = paths.list_active_ids()[0]
        s = Status.from_json(paths.status_json(tid).read_text())
        assert s.persona == "Mistui"

    def test_task_mode_default_persona_typo_rejected(
        self, tmp_path, journal_dir, capsys, monkeypatch,
    ):
        """default_persona in personas.yaml pointing at a non-existent
        persona is also rejected -- the typo would otherwise affect
        every subsequent scaffold."""
        team = tmp_path / "teams" / "Tigers"
        (team / "configs").mkdir(parents=True)
        (team / "configs" / "personas.yaml").write_text(
            "default_persona: Mistui\n"  # typo (Mitsui)
            "personas:\n  - name: Mitsui\n"
        )
        (team / "personas" / "Mitsui").mkdir(parents=True)
        (team / "personas" / "Mitsui" / "prompt.md").write_text("hi\n")
        monkeypatch.chdir(team)
        prd = tmp_path / "b.md"
        prd.write_text("# T\nb\n")
        rc = main([
            "--journal-dir", str(journal_dir),
            "new", "--prd", str(prd),
        ])
        assert rc == 2
        err = capsys.readouterr().err
        assert "'Mistui'" in err
        assert "default_persona" in err
        # Error message points specifically at the yaml file the
        # operator needs to fix.
        assert "personas.yaml" in err

    def test_task_mode_no_persona_and_no_default_still_errors(
        self, tmp_path, journal_dir, capsys, monkeypatch,
    ):
        """A cwd that IS a team root but has no `default_persona:` key
        still errors when --persona is omitted (same as Phase 1)."""
        team = tmp_path / "teams" / "Plain"
        (team / "configs").mkdir(parents=True)
        (team / "configs" / "personas.yaml").write_text(
            "personas:\n  - name: Mitsui\n"
        )
        monkeypatch.chdir(team)
        prd = tmp_path / "b.md"
        prd.write_text("# T\nb\n")
        rc = main([
            "--journal-dir", str(journal_dir),
            "new", "--prd", str(prd),
        ])
        assert rc == 2
        assert "--persona is required" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# new --kind workflow
# ---------------------------------------------------------------------------

class TestCmdNewWorkflow:
    """Workflow-mode scaffolder tests. We import the helper that builds a
    team root + persona prompts from test_workflow_scaffold to stay
    consistent with the rest of Phase 1.5."""

    @pytest.fixture
    def team_root(self, tmp_path, monkeypatch):
        from tests.journal.test_workflow_scaffold import _make_team
        root = _make_team(tmp_path)
        monkeypatch.chdir(root)
        # Drop the playbook in the canonical location.
        (root / "workflow").mkdir()
        (root / "workflow" / "default.md").write_text(
            "# default\n\nAnzai drafts. Akagi reviews. Ayako reviews.\n",
        )
        return root

    def test_workflow_happy_path_inline_brief(
        self, team_root, journal_dir, capsys,
    ):
        rc = main([
            "--journal-dir", str(journal_dir),
            "new", "--kind", "workflow",
            "--playbook", "default",
            "--task-brief", "Inline brief body.",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Scaffolded:" in out
        assert "kind:         workflow" in out
        assert "captain:      (none)" in out
        # task_dir landed
        paths = JournalPaths(root=journal_dir)
        assert paths.list_active_ids()

    def test_workflow_uses_playbook_default_captain_when_omitted(
        self, team_root, journal_dir, capsys,
    ):
        """If the playbook has `default_captain:` in an HTML-comment
        YAML block and the CLI omits --captain, the playbook default
        wins."""
        (team_root / "workflow" / "default.md").write_text(
            "# default\n\n"
            "<!--\n"
            "default_captain: Mitsui\n"
            "-->\n\n"
            "Anzai drafts. Akagi reviews. Ayako reviews.\n",
        )
        rc = main([
            "--journal-dir", str(journal_dir),
            "new", "--kind", "workflow",
            "--playbook", "default",
            "--task-brief", "B",
        ])
        assert rc == 0, capsys.readouterr().err
        assert "captain:      Mitsui" in capsys.readouterr().out

    def test_workflow_explicit_captain_overrides_playbook_default(
        self, team_root, journal_dir, capsys,
    ):
        """An explicit --captain wins over playbook default_captain."""
        (team_root / "workflow" / "default.md").write_text(
            "<!--\ndefault_captain: Mitsui\n-->\n\n"
            "Anzai drafts. Akagi reviews. Ayako reviews.\n",
        )
        rc = main([
            "--journal-dir", str(journal_dir),
            "new", "--kind", "workflow",
            "--playbook", "default",
            "--task-brief", "B",
            "--captain", "Akagi",
        ])
        assert rc == 0
        assert "captain:      Akagi" in capsys.readouterr().out

    def test_workflow_playbook_default_captain_resolves_alias(
        self, team_root, journal_dir, capsys,
    ):
        """If the playbook says `default_captain: Mumu` and Mumu
        aliases Kogure, the scaffolded task carries Kogure."""
        # Set up Kogure with Mumu alias on the team.
        (team_root / "configs" / "personas.yaml").write_text(
            "personas:\n"
            "  - name: Anzai\n"
            "  - name: Akagi\n"
            "  - name: Ayako\n"
            "  - name: Mitsui\n"
            "  - name: Kogure\n"
            "    aliases: [Mumu]\n"
        )
        (team_root / "personas" / "Kogure").mkdir(parents=True)
        (team_root / "personas" / "Kogure" / "prompt.md").write_text("hi\n")
        (team_root / "workflow" / "default.md").write_text(
            "<!--\ndefault_captain: Mumu\n-->\n\n"
            "Anzai drafts. Akagi reviews. Ayako reviews.\n",
        )
        rc = main([
            "--journal-dir", str(journal_dir),
            "new", "--kind", "workflow",
            "--playbook", "default",
            "--task-brief", "B",
        ])
        assert rc == 0, capsys.readouterr().err
        assert "captain:      Kogure" in capsys.readouterr().out

    def test_workflow_brief_file(
        self, tmp_path, team_root, journal_dir, capsys,
    ):
        bf = tmp_path / "brief.md"
        bf.write_text("# Title\nFrom file.\n")
        rc = main([
            "--journal-dir", str(journal_dir),
            "new", "--kind", "workflow",
            "--playbook", "default",
            "--brief-file", str(bf),
            "--captain", "Mitsui",
        ])
        assert rc == 0
        assert "captain:      Mitsui" in capsys.readouterr().out

    def test_workflow_rejects_prd(self, journal_dir, capsys):
        rc = main([
            "--journal-dir", str(journal_dir),
            "new", "--kind", "workflow",
            "--prd", "/some/prd.md",
        ])
        assert rc == 2
        assert "--prd is task-only" in capsys.readouterr().err

    def test_workflow_requires_playbook(self, journal_dir, capsys):
        rc = main([
            "--journal-dir", str(journal_dir),
            "new", "--kind", "workflow",
            "--task-brief", "B",
        ])
        assert rc == 2
        assert "--playbook is required" in capsys.readouterr().err

    def test_workflow_brief_mutually_exclusive(
        self, tmp_path, journal_dir, capsys,
    ):
        bf = tmp_path / "b.md"
        bf.write_text("x")
        rc = main([
            "--journal-dir", str(journal_dir),
            "new", "--kind", "workflow",
            "--playbook", "default",
            "--task-brief", "B", "--brief-file", str(bf),
        ])
        assert rc == 2
        assert "mutually exclusive" in capsys.readouterr().err

    def test_workflow_brief_required(self, journal_dir, capsys):
        rc = main([
            "--journal-dir", str(journal_dir),
            "new", "--kind", "workflow",
            "--playbook", "default",
        ])
        assert rc == 2
        assert "--task-brief or --brief-file is required" in \
            capsys.readouterr().err

    def test_workflow_rejects_persona(self, journal_dir, capsys):
        rc = main([
            "--journal-dir", str(journal_dir),
            "new", "--kind", "workflow",
            "--playbook", "default", "--task-brief", "B",
            "--persona", "P",
        ])
        assert rc == 2
        assert "--persona is task-only" in capsys.readouterr().err

    def test_workflow_brief_file_not_found(
        self, journal_dir, team_root, capsys,
    ):
        rc = main([
            "--journal-dir", str(journal_dir),
            "new", "--kind", "workflow",
            "--playbook", "default",
            "--brief-file", "/no/such/file.md",
        ])
        assert rc == 2
        assert "brief file not found" in capsys.readouterr().err

    def test_workflow_playbook_not_found(
        self, journal_dir, team_root, capsys,
    ):
        rc = main([
            "--journal-dir", str(journal_dir),
            "new", "--kind", "workflow",
            "--playbook", "no-such-playbook",
            "--task-brief", "B",
        ])
        assert rc == 2
        assert "playbook no-such-playbook.md not found" in \
            capsys.readouterr().err

    def test_workflow_missing_persona_returns_2(
        self, tmp_path, monkeypatch, journal_dir, capsys,
    ):
        """If the team is missing one of the compile personas, the pre-flight
        in new_workflow_task raises MissingPersonaError which the CLI
        catches separately for a clean exit 2."""
        from tests.journal.test_workflow_scaffold import _make_team
        from tigerharness.journal.scaffold import COMPILE_PERSONAS
        partial = list(COMPILE_PERSONAS)
        partial.remove("Akagi")
        root = _make_team(tmp_path, personas=partial)
        monkeypatch.chdir(root)
        (root / "workflow").mkdir()
        (root / "workflow" / "default.md").write_text(
            "# default\nAnzai drafts.\n",
        )
        rc = main([
            "--journal-dir", str(journal_dir),
            "new", "--kind", "workflow",
            "--playbook", "default", "--task-brief", "B",
        ])
        assert rc == 2
        err = capsys.readouterr().err
        assert "missing" in err.lower() and "Akagi" in err

    def test_workflow_scaffold_error_returns_2(
        self, team_root, journal_dir, capsys,
    ):
        """A blank --captain (whitespace-only) -> Status.new_workflow
        rejects via JournalModelError, which the CLI surfaces."""
        rc = main([
            "--journal-dir", str(journal_dir),
            "new", "--kind", "workflow",
            "--playbook", "default", "--task-brief", "B",
            "--captain", "   ",
        ])
        assert rc == 2
        assert "captain" in capsys.readouterr().err.lower()

    def test_workflow_max_sessions_default_is_10(
        self, team_root, journal_dir, capsys,
    ):
        """--max-sessions left at the task default of 5 is treated as
        'unset' and bumped to 10 for workflow mode."""
        rc = main([
            "--journal-dir", str(journal_dir),
            "new", "--kind", "workflow",
            "--playbook", "default", "--task-brief", "B",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "max_sessions: 10" in out

    def test_workflow_max_sessions_explicit_passes_through(
        self, team_root, journal_dir, capsys,
    ):
        rc = main([
            "--journal-dir", str(journal_dir),
            "new", "--kind", "workflow",
            "--playbook", "default", "--task-brief", "B",
            "--max-sessions", "7",
        ])
        assert rc == 0
        assert "max_sessions: 7" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

class TestCmdList:
    def test_empty(self, journal_dir, capsys):
        rc = main(["--journal-dir", str(journal_dir), "list"])
        assert rc == 0
        assert "No active tasks" in capsys.readouterr().out

    def test_empty_table_when_active_exists_but_no_tasks(
        self, journal_dir, capsys,
    ):
        """Covers the path where ``active/`` exists on disk (so the
        ``not paths.active.is_dir()`` early-out doesn't fire) but is
        empty (so the row+malformed lists are both empty)."""
        JournalPaths(root=journal_dir).ensure()
        rc = main(["--journal-dir", str(journal_dir), "list"])
        assert rc == 0
        assert "No active tasks" in capsys.readouterr().out

    def test_empty_json(self, journal_dir, capsys):
        rc = main([
            "--journal-dir", str(journal_dir),
            "list", "--format", "json",
        ])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "[]"

    def test_renders_table(self, journal_dir, capsys):
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "20260602-a-11111111", state=State.PENDING)
        _seed(paths, "20260602-b-22222222", state=State.IN_PROGRESS,
              sessions=1)
        rc = main(["--journal-dir", str(journal_dir), "list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "20260602-a-11111111" in out
        assert "20260602-b-22222222" in out
        assert "pending" in out
        assert "KIND" in out
        # Task kind shown in its own column.
        assert "task" in out

    def test_renders_workflow_row(self, journal_dir, capsys):
        """Workflow row shows kind=workflow, compile phase appended to
        STATE, and (none) when captain is omitted."""
        from tigerharness.journal.models import CompilePhase
        paths = JournalPaths(root=journal_dir)
        paths.ensure()
        (paths.active / "20260602-w-33333333").mkdir(exist_ok=True)
        s = Status(
            id="20260602-w-33333333",
            title="WF",
            kind="workflow",
            persona=None,
            state=State.PENDING,
            sessions=0,
            max_sessions=10,
            created_at="2026-06-02T08:00:00Z",
            updated_at="2026-06-02T08:00:00Z",
            next_action="",
            session_ref=None,
            compile_pending=True,
            compile_phase=CompilePhase.DRAFTING,
            playbook_name="default",
        )
        paths.status_json("20260602-w-33333333").write_text(s.to_json())
        rc = main(["--journal-dir", str(journal_dir), "list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "20260602-w-33333333" in out
        assert "workflow" in out
        assert "pending/drafting" in out
        assert "(none)" in out

    def test_json_renders_workflow_compile_fields(
        self, journal_dir, capsys,
    ):
        from tigerharness.journal.models import CompilePhase
        paths = JournalPaths(root=journal_dir)
        paths.ensure()
        (paths.active / "20260602-w-44444444").mkdir(exist_ok=True)
        s = Status(
            id="20260602-w-44444444",
            title="WF",
            kind="workflow",
            persona="Mitsui",
            state=State.PENDING,
            sessions=0,
            max_sessions=10,
            created_at="2026-06-02T08:00:00Z",
            updated_at="2026-06-02T08:00:00Z",
            next_action="",
            session_ref=None,
            compile_pending=True,
            compile_phase=CompilePhase.PENDING,
            playbook_name="default",
        )
        paths.status_json("20260602-w-44444444").write_text(s.to_json())
        rc = main([
            "--journal-dir", str(journal_dir),
            "list", "--format", "json",
        ])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        row = payload["active"][0]
        assert row["kind"] == "workflow"
        assert row["compile_pending"] is True
        assert row["compile_phase"] == "pending"

    def test_renders_json(self, journal_dir, capsys):
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "20260602-a-11111111")
        rc = main([
            "--journal-dir", str(journal_dir),
            "list", "--format", "json",
        ])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert isinstance(payload, dict)
        row = payload["active"][0]
        assert row["id"] == "20260602-a-11111111"
        assert payload["malformed"] == []
        # Phase 1.5 contract: kind=task JSON must NOT carry the
        # workflow-only compile fields, so the row round-trips through
        # Status.from_dict (which rejects compile_pending on task).
        assert "compile_pending" not in row
        assert "compile_phase" not in row

    def test_malformed_surfaced(self, journal_dir, capsys):
        paths = JournalPaths(root=journal_dir).ensure()
        (paths.active / "20260602-bad-99999999").mkdir()
        (paths.active / "20260602-bad-99999999" / "status.json").write_text(
            "{not json"
        )
        rc = main(["--journal-dir", str(journal_dir), "list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Malformed" in out
        assert "20260602-bad-99999999" in out


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

class TestCmdStatus:
    def test_prints_json_of_known_task(self, journal_dir, capsys):
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "20260602-a-11111111")
        rc = main([
            "--journal-dir", str(journal_dir),
            "status", "20260602-a-11111111",
        ])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["id"] == "20260602-a-11111111"
        # Task statuses must not leak workflow-only compile fields.
        assert "compile_pending" not in payload
        assert "compile_phase" not in payload

    def test_json_round_trip_workflow(self, journal_dir, capsys):
        """`journal status` output for a kind=workflow task must be
        consumable by `Status.from_json` -- i.e. must carry both
        compile_pending and compile_phase, and must NOT omit them."""
        from tigerharness.journal.models import CompilePhase, Status
        paths = JournalPaths(root=journal_dir)
        paths.ensure()
        (paths.active / "20260602-w-55555555").mkdir(exist_ok=True)
        s = Status(
            id="20260602-w-55555555",
            title="WF",
            kind="workflow",
            persona="Mitsui",
            state=State.PENDING,
            sessions=0,
            max_sessions=10,
            created_at="2026-06-02T08:00:00Z",
            updated_at="2026-06-02T08:00:00Z",
            next_action="",
            session_ref=None,
            compile_pending=True,
            compile_phase=CompilePhase.PENDING,
            playbook_name="default",
        )
        paths.status_json("20260602-w-55555555").write_text(s.to_json())
        rc = main([
            "--journal-dir", str(journal_dir),
            "status", "20260602-w-55555555",
        ])
        assert rc == 0
        # Round-trip: the printed JSON parses back into an identical Status.
        round_tripped = Status.from_json(capsys.readouterr().out)
        assert round_tripped.kind == "workflow"
        assert round_tripped.compile_pending is True
        assert round_tripped.compile_phase == CompilePhase.PENDING

    def test_unknown_task_returns_1(self, journal_dir, capsys):
        # Ensure structure exists so missing-id is what we test, not
        # missing-dir.
        JournalPaths(root=journal_dir).ensure()
        rc = main([
            "--journal-dir", str(journal_dir),
            "status", "20260602-nope-99999999",
        ])
        assert rc == 1
        assert "no task" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# sweep
# ---------------------------------------------------------------------------

class TestCmdSweep:
    def test_empty_prints_summary(self, journal_dir, capsys):
        rc = main(["--journal-dir", str(journal_dir), "sweep"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "0 pending" in out

    def test_archives_and_lists_actionable(self, journal_dir, capsys):
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "p1", state=State.PENDING)
        _seed(paths, "d1", state=State.DONE)
        # Detached in_progress -> idle/resumable.
        _seed(paths, "ip-idle", state=State.IN_PROGRESS,
              updated_at="2026-06-02T08:00:00Z")
        rc = main([
            "--journal-dir", str(journal_dir),
            "sweep", "--stuck-timeout", "1000000",  # never crashed
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Archived" in out and "d1" in out
        # Finish-before-start: the resumable in_progress task is the
        # pick, ahead of the pending one.
        assert "Actionable" in out and "ip-idle" in out
        assert "1 resumable" in out
        assert "1 pending" in out

    def test_blocked_and_malformed_surfaced(self, journal_dir, capsys):
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "bl1", state=State.BLOCKED)
        # Malformed
        (paths.active / "bad").mkdir()
        (paths.active / "bad" / "status.json").write_text("{not")
        rc = main(["--journal-dir", str(journal_dir), "sweep"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Blocked" in out
        assert "bl1" in out
        assert "Malformed" in out

    def test_json_format(self, journal_dir, capsys):
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "p1", state=State.PENDING)
        rc = main([
            "--journal-dir", str(journal_dir),
            "sweep", "--format", "json",
        ])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["pending"] == ["p1"]
        assert payload["actionable"] == ["p1"]

    def test_text_lists_busy_tasks(self, journal_dir, capsys):
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "b1", state=State.IN_PROGRESS, session_ref="held",
              updated_at="2026-06-02T08:00:00Z")
        # huge timeout -> attached + fresh -> busy -> shown in the
        # "Busy (LEAVE ALONE...)" text section.
        rc = main(["--journal-dir", str(journal_dir), "sweep",
                   "--stuck-timeout", "100000000"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Busy" in out and "b1" in out

    def test_bad_env_stuck_timeout_returns_2(
        self, journal_dir, capsys, monkeypatch,
    ):
        monkeypatch.setenv("TIGERHARNESS_JOURNAL_STUCK_TIMEOUT", "nope")
        rc = main(["--journal-dir", str(journal_dir), "sweep"])
        assert rc == 2
        assert "must be an integer" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# default journal dir (no --journal-dir arg)
# ---------------------------------------------------------------------------

class TestDefaultJournalDir:
    def test_uses_env_override(
        self, tmp_path, monkeypatch, capsys,
    ):
        """Without ``--journal-dir`` the CLI falls back to
        ``default_journal_root()``, which reads the env."""
        target = tmp_path / "from-env"
        monkeypatch.setenv("TIGERHARNESS_JOURNAL_DIR", str(target))
        rc = main(["sweep"])
        assert rc == 0
        # The sweep ensured active/ exists.
        assert (target / "active").is_dir()


# ---------------------------------------------------------------------------

class TestCmdClaimRelease:
    def test_claim_pending_starts_and_attaches(self, journal_dir, capsys):
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.PENDING)
        rc = main(["--journal-dir", str(journal_dir), "claim", "t1",
                   "--format", "json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["task_id"] == "t1"
        assert payload["session_ref"]
        assert payload["sessions"] == 1
        s = Status.from_json(paths.status_json("t1").read_text())
        assert s.state is State.IN_PROGRESS
        assert s.session_ref == payload["session_ref"]

    def test_claim_idle_in_progress_resumes_and_bumps_sessions(self, journal_dir):
        paths = JournalPaths(root=journal_dir)
        # Detached in_progress -> idle; resumable immediately.
        _seed(paths, "t1", state=State.IN_PROGRESS, sessions=1,
              updated_at="2026-06-02T08:00:00Z")
        rc = main(["--journal-dir", str(journal_dir), "claim", "t1"])
        assert rc == 0
        s = Status.from_json(paths.status_json("t1").read_text())
        assert s.session_ref is not None
        assert s.sessions == 2

    def test_claim_busy_refused(self, journal_dir, capsys):
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.IN_PROGRESS, session_ref="held",
              updated_at="2026-06-02T08:00:00Z")
        # Huge timeout -> attached + fresh -> busy -> refuse.
        rc = main(["--journal-dir", str(journal_dir), "claim", "t1",
                   "--stuck-timeout", "100000000"])
        assert rc == 1
        assert "busy" in capsys.readouterr().err
        s = Status.from_json(paths.status_json("t1").read_text())
        assert s.session_ref == "held"  # not stolen

    def test_claim_crashed_is_reclaimable(self, journal_dir):
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.IN_PROGRESS, session_ref="dead",
              updated_at="2026-06-02T08:00:00Z")
        # Tiny timeout -> attached + stale -> crashed -> reclaimable.
        rc = main(["--journal-dir", str(journal_dir), "claim", "t1",
                   "--stuck-timeout", "1"])
        assert rc == 0
        s = Status.from_json(paths.status_json("t1").read_text())
        assert s.session_ref is not None
        assert s.session_ref != "dead"  # re-attached with our token

    def test_claim_done_refused(self, journal_dir, capsys):
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.DONE)
        rc = main(["--journal-dir", str(journal_dir), "claim", "t1"])
        assert rc == 1
        assert "not claimable" in capsys.readouterr().err

    def test_release_detaches_and_keeps_in_progress(self, journal_dir):
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.IN_PROGRESS, session_ref="tok",
              updated_at="2026-06-02T08:00:00Z")
        rc = main(["--journal-dir", str(journal_dir), "release", "t1",
                   "--next-action", "resume here"])
        assert rc == 0
        s = Status.from_json(paths.status_json("t1").read_text())
        assert s.session_ref is None  # detached -> instantly resumable
        assert s.state is State.IN_PROGRESS
        assert s.next_action == "resume here"

    def test_release_to_done(self, journal_dir):
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.IN_PROGRESS, session_ref="tok")
        rc = main(["--journal-dir", str(journal_dir), "release", "t1",
                   "--state", "done"])
        assert rc == 0
        s = Status.from_json(paths.status_json("t1").read_text())
        assert s.state is State.DONE
        assert s.session_ref is None

    def test_release_session_ref_mismatch_refused(self, journal_dir, capsys):
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.IN_PROGRESS, session_ref="real",
              updated_at="2026-06-02T08:00:00Z")
        rc = main(["--journal-dir", str(journal_dir), "release", "t1",
                   "--session-ref", "wrong"])
        assert rc == 1
        assert "does not match" in capsys.readouterr().err
        s = Status.from_json(paths.status_json("t1").read_text())
        assert s.session_ref == "real"  # untouched

    def test_release_to_blocked(self, journal_dir):
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.IN_PROGRESS, session_ref="tok")
        rc = main(["--journal-dir", str(journal_dir), "release", "t1",
                   "--state", "blocked", "--next-action", "needs human"])
        assert rc == 0
        s = Status.from_json(paths.status_json("t1").read_text())
        assert s.state is State.BLOCKED
        assert s.session_ref is None
        assert s.next_action == "needs human"

    def test_claim_at_cap_self_heals_to_blocked(self, journal_dir, capsys):
        paths = JournalPaths(root=journal_dir)
        # in_progress, idle (detached), already at the session cap.
        _seed(paths, "t1", state=State.IN_PROGRESS, sessions=3, max_sessions=3)
        rc = main(["--journal-dir", str(journal_dir), "claim", "t1"])
        assert rc == 1
        assert "session cap" in capsys.readouterr().err
        s = Status.from_json(paths.status_json("t1").read_text())
        assert s.state is State.BLOCKED    # self-healed, not run past cap
        assert s.session_ref is None
        assert s.sessions == 3             # NOT bumped to 4

    def test_claim_lost_when_another_session_won_the_race(
        self, journal_dir, capsys, monkeypatch,
    ):
        # The compare-and-set re-read must detect that a racing claim
        # overwrote our token, and back off (rc=1, "claim lost").
        from tigerharness.journal import cli as _cli
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.PENDING)
        real = _cli._read_status_or_none
        calls = {"n": 0}

        def fake(p, tid):
            calls["n"] += 1
            s = real(p, tid)
            if calls["n"] >= 2 and s is not None:
                s.session_ref = "won-by-someone-else"  # simulate the race
            return s

        monkeypatch.setattr(_cli, "_read_status_or_none", fake)
        rc = main(["--journal-dir", str(journal_dir), "claim", "t1"])
        assert rc == 1
        assert "claim lost" in capsys.readouterr().err

    def test_release_refuses_a_done_task(self, journal_dir, capsys):
        # `release` must not resurrect a terminal (done) task.
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.DONE)
        rc = main(["--journal-dir", str(journal_dir), "release", "t1",
                   "--state", "in_progress"])
        assert rc == 1
        assert "done" in capsys.readouterr().err
        s = Status.from_json(paths.status_json("t1").read_text())
        assert s.state is State.DONE  # not resurrected

    def test_claim_then_release_workflow_preserves_compile_state(self, journal_dir):
        # claim does the workflow pickup (in_progress + attach + sessions),
        # leaving compile_pending intact; release detaches without touching it.
        paths = JournalPaths(root=journal_dir)
        paths.ensure()
        (paths.active / "wf1").mkdir()
        st = Status.new_workflow(id="wf1", title="W", playbook_name="default")
        paths.status_json("wf1").write_text(st.to_json())

        assert main(["--journal-dir", str(journal_dir), "claim", "wf1"]) == 0
        s = Status.from_json(paths.status_json("wf1").read_text())
        assert s.state is State.IN_PROGRESS
        assert s.session_ref is not None      # attached
        assert s.sessions == 1                # bumped
        assert s.compile_pending is True      # compile state untouched
        assert s.kind == "workflow"

        assert main(["--journal-dir", str(journal_dir), "release", "wf1",
                     "--next-action", "resume compile"]) == 0
        s = Status.from_json(paths.status_json("wf1").read_text())
        assert s.session_ref is None          # detached -> idle/resumable
        assert s.state is State.IN_PROGRESS
        assert s.compile_pending is True      # still mid-compile

    def test_claim_unknown_task_errors(self, journal_dir, capsys):
        rc = main(["--journal-dir", str(journal_dir), "claim", "nope"])
        assert rc == 1
        assert "no task" in capsys.readouterr().err

    def test_release_unknown_task_errors(self, journal_dir, capsys):
        rc = main(["--journal-dir", str(journal_dir), "release", "nope"])
        assert rc == 1
        assert "no task" in capsys.readouterr().err

    def test_claim_bad_env_stuck_timeout_returns_2(
        self, journal_dir, capsys, monkeypatch,
    ):
        monkeypatch.setenv("TIGERHARNESS_JOURNAL_STUCK_TIMEOUT", "notanint")
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.PENDING)
        rc = main(["--journal-dir", str(journal_dir), "claim", "t1"])
        assert rc == 2
        assert "TIGERHARNESS_JOURNAL_STUCK_TIMEOUT" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Phase 1b: per-persona worklog side-effects on claim / release
# ---------------------------------------------------------------------------

class TestClaimDriverWorklog:
    """``--driver`` makes ``claim`` leave a thin driver trace; without it
    the plain subscription backend keeps its no-worklog behaviour."""

    def test_no_driver_writes_no_worklog(self, journal_dir):
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.PENDING)
        assert main(["--journal-dir", str(journal_dir), "claim", "t1"]) == 0
        assert worklog.list_entries(paths, "t1") == []
        assert not paths.worklog("t1").exists()

    def test_pending_claim_records_new(self, journal_dir):
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.PENDING)
        rc = main(["--journal-dir", str(journal_dir), "claim", "t1",
                   "--driver", "Anzai"])
        assert rc == 0
        [e] = worklog.list_entries(paths, "t1")
        assert e.persona == "Anzai"
        assert e.step == "drive"
        assert e.reason == "new"
        assert e.role == "driver"
        assert e.kind == "task"
        assert "Drove" in e.body

    def test_idle_resume_records_resume(self, journal_dir):
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.IN_PROGRESS, sessions=1,
              updated_at="2026-06-02T08:00:00Z")
        rc = main(["--journal-dir", str(journal_dir), "claim", "t1",
                   "--driver", "Anzai"])
        assert rc == 0
        [e] = worklog.list_entries(paths, "t1")
        assert e.reason == "resume"

    def test_crashed_rescue_records_rescue(self, journal_dir):
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.IN_PROGRESS, session_ref="dead",
              updated_at="2026-06-02T08:00:00Z")
        rc = main(["--journal-dir", str(journal_dir), "claim", "t1",
                   "--driver", "Anzai", "--stuck-timeout", "1"])
        assert rc == 0
        [e] = worklog.list_entries(paths, "t1")
        assert e.reason == "rescue"

    def test_workflow_claim_records_driver_entry(self, journal_dir):
        paths = JournalPaths(root=journal_dir)
        paths.ensure()
        (paths.active / "wf1").mkdir()
        st = Status.new_workflow(id="wf1", title="W", playbook_name="default")
        paths.status_json("wf1").write_text(st.to_json())
        rc = main(["--journal-dir", str(journal_dir), "claim", "wf1",
                   "--driver", "Anzai"])
        assert rc == 0
        [e] = worklog.list_entries(paths, "wf1")
        assert e.kind == "workflow"
        assert e.reason == "new"

    def test_worklog_write_failure_is_warned_not_fatal(
        self, journal_dir, capsys, monkeypatch,
    ):
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.PENDING)

        def _boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(worklog, "write_entry", _boom)
        rc = main(["--journal-dir", str(journal_dir), "claim", "t1",
                   "--driver", "Anzai"])
        # The claim itself still succeeds; the trace is best-effort.
        assert rc == 0
        assert "warning" in capsys.readouterr().err.lower()
        s = Status.from_json(paths.status_json("t1").read_text())
        assert s.state is State.IN_PROGRESS


class TestClaimDriveThreadRegistry:
    """``--drive-thread`` records the drive's Slack thread to the
    drive-session registry so tiger-memory skips its transcript; without
    it, no registry is written."""

    def test_no_drive_thread_writes_no_registry(self, journal_dir, monkeypatch):
        # Hermetic: no flag AND no env var -> nothing to register.
        monkeypatch.delenv("TIGERHARNESS_SLACK_THREAD_TS", raising=False)
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.PENDING)
        assert main(["--journal-dir", str(journal_dir), "claim", "t1",
                     "--driver", "Anzai"]) == 0
        assert not paths.drive_sessions_json.exists()

    def test_env_thread_ts_fallback_when_driver_set(
        self, journal_dir, monkeypatch,
    ):
        # Harness-enforced: the bridge sets TIGERHARNESS_SLACK_THREAD_TS;
        # with --driver, claim registers it without an explicit flag.
        monkeypatch.setenv("TIGERHARNESS_SLACK_THREAD_TS", "555.42")
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.PENDING)
        # --allow-api-drive: with the env var set the rail guard would
        # otherwise refuse (Slack schedules, never drives); the override
        # is exactly the path where the registration fallback still fires.
        assert main(["--journal-dir", str(journal_dir), "claim", "t1",
                     "--driver", "Anzai", "--allow-api-drive"]) == 0
        assert drive_sessions.registered_threads(
            paths.drive_sessions_json
        ) == {"555.42"}
        rec = json.loads(paths.drive_sessions_json.read_text())["555.42"]
        assert rec["driver"] == "Anzai"

    def test_env_thread_ts_ignored_without_driver(
        self, journal_dir, monkeypatch,
    ):
        # The env fallback is gated on --driver: a plain (non-drive) turn
        # must not suppress its own transcript (no worklog replaces it).
        monkeypatch.setenv("TIGERHARNESS_SLACK_THREAD_TS", "555.42")
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.PENDING)
        assert main(["--journal-dir", str(journal_dir), "claim", "t1",
                     "--allow-api-drive"]) == 0
        assert not paths.drive_sessions_json.exists()

    def test_explicit_drive_thread_beats_env(self, journal_dir, monkeypatch):
        # An explicit --drive-thread overrides the env fallback.
        monkeypatch.setenv("TIGERHARNESS_SLACK_THREAD_TS", "999.99")
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.PENDING)
        assert main(["--journal-dir", str(journal_dir), "claim", "t1",
                     "--driver", "Anzai", "--drive-thread", "171.99",
                     "--allow-api-drive"]) == 0
        assert drive_sessions.registered_threads(
            paths.drive_sessions_json
        ) == {"171.99"}

    def test_drive_thread_registers(self, journal_dir):
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.PENDING)
        rc = main(["--journal-dir", str(journal_dir), "claim", "t1",
                   "--driver", "Anzai", "--drive-thread", "171.99"])
        assert rc == 0
        assert drive_sessions.registered_threads(
            paths.drive_sessions_json
        ) == {"171.99"}
        rec = json.loads(paths.drive_sessions_json.read_text())["171.99"]
        assert rec["task_id"] == "t1"
        assert rec["driver"] == "Anzai"

    def test_drive_thread_without_driver_registers_null_driver(
        self, journal_dir,
    ):
        # The two flags are orthogonal: a thread can be registered even
        # without a --driver trace (driver recorded as null).
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.PENDING)
        assert main(["--journal-dir", str(journal_dir), "claim", "t1",
                     "--drive-thread", "171.99"]) == 0
        rec = json.loads(paths.drive_sessions_json.read_text())["171.99"]
        assert rec["driver"] is None

    def test_registry_write_failure_is_warned_not_fatal(
        self, journal_dir, capsys, monkeypatch,
    ):
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.PENDING)

        def _boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(drive_sessions, "register", _boom)
        rc = main(["--journal-dir", str(journal_dir), "claim", "t1",
                   "--driver", "Anzai", "--drive-thread", "171.99"])
        # The claim itself still succeeds; registration is best-effort.
        assert rc == 0
        assert "warning" in capsys.readouterr().err.lower()
        s = Status.from_json(paths.status_json("t1").read_text())
        assert s.state is State.IN_PROGRESS


class TestReleaseTaskCompletionGate:
    """Marking a kind=task DONE in a drive requires the assigned persona's
    work note (--output): the note is the ticket to advance."""

    def _note(self, tmp_path, text="Implemented X; tests green.\n"):
        p = tmp_path / "note.md"
        p.write_text(text)
        return str(p)

    def test_done_with_output_writes_persona_entry(
        self, journal_dir, tmp_path,
    ):
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.IN_PROGRESS, session_ref="tok",
              persona="Rukawa")
        rc = main(["--journal-dir", str(journal_dir), "release", "t1",
                   "--state", "done", "--driver", "Anzai",
                   "--output", self._note(tmp_path)])
        assert rc == 0
        s = Status.from_json(paths.status_json("t1").read_text())
        assert s.state is State.DONE
        [e] = worklog.list_entries(paths, "t1")
        assert e.persona == "Rukawa"          # the assignee, not the driver
        assert e.step == "task-work"
        assert "Implemented X" in e.body

    def test_done_without_output_refused(self, journal_dir, capsys):
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.IN_PROGRESS, session_ref="tok",
              persona="Rukawa")
        rc = main(["--journal-dir", str(journal_dir), "release", "t1",
                   "--state", "done", "--driver", "Anzai"])
        assert rc == 1
        assert "--output" in capsys.readouterr().err
        s = Status.from_json(paths.status_json("t1").read_text())
        assert s.state is State.IN_PROGRESS   # NOT marked done
        assert worklog.list_entries(paths, "t1") == []

    def test_done_with_empty_output_refused(
        self, journal_dir, tmp_path, capsys,
    ):
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.IN_PROGRESS, session_ref="tok",
              persona="Rukawa")
        rc = main(["--journal-dir", str(journal_dir), "release", "t1",
                   "--state", "done", "--driver", "Anzai",
                   "--output", self._note(tmp_path, "   \n")])
        assert rc == 1
        assert "empty" in capsys.readouterr().err
        s = Status.from_json(paths.status_json("t1").read_text())
        assert s.state is State.IN_PROGRESS

    def test_done_with_unreadable_output_refused(
        self, journal_dir, tmp_path, capsys,
    ):
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.IN_PROGRESS, session_ref="tok",
              persona="Rukawa")
        missing = str(tmp_path / "nope.md")
        rc = main(["--journal-dir", str(journal_dir), "release", "t1",
                   "--state", "done", "--driver", "Anzai",
                   "--output", missing])
        assert rc == 1
        assert "cannot read" in capsys.readouterr().err
        s = Status.from_json(paths.status_json("t1").read_text())
        assert s.state is State.IN_PROGRESS

    def test_done_with_note_write_failure_refused(
        self, journal_dir, tmp_path, capsys, monkeypatch,
    ):
        # A disk failure writing the work note must refuse CLEANLY (rc=1),
        # leaving the task in_progress + resumable -- not escape as a
        # traceback out of an un-guarded main(). Mirrors the step-done gate.
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.IN_PROGRESS, session_ref="tok",
              persona="Rukawa")

        def _boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(worklog, "write_entry", _boom)
        rc = main(["--journal-dir", str(journal_dir), "release", "t1",
                   "--state", "done", "--driver", "Anzai",
                   "--output", self._note(tmp_path)])
        assert rc == 1
        assert "could not write the work note" in capsys.readouterr().err
        s = Status.from_json(paths.status_json("t1").read_text())
        assert s.state is State.IN_PROGRESS   # NOT marked done -> resumable

    def test_clean_stop_in_progress_needs_no_output(self, journal_dir):
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.IN_PROGRESS, session_ref="tok",
              persona="Rukawa")
        rc = main(["--journal-dir", str(journal_dir), "release", "t1",
                   "--driver", "Anzai"])
        assert rc == 0
        assert worklog.list_entries(paths, "t1") == []  # no note on a pause

    def test_no_driver_done_keeps_plain_behaviour(self, journal_dir):
        # The plain subscription backend (no --driver) marks done with no
        # note requirement and no worklog side-effect.
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.IN_PROGRESS, session_ref="tok",
              persona="Rukawa")
        rc = main(["--journal-dir", str(journal_dir), "release", "t1",
                   "--state", "done"])
        assert rc == 0
        s = Status.from_json(paths.status_json("t1").read_text())
        assert s.state is State.DONE
        assert worklog.list_entries(paths, "t1") == []

    def test_workflow_done_not_gated_by_output(self, journal_dir):
        # The kind=task --output gate must NOT fire for workflows (their
        # per-step notes come from step-done). A workflow done is instead
        # gated on the walk having reached __done__ (Phase 1c) -- so seed
        # a completed walk and confirm done succeeds with no --output.
        from tigerharness.journal import walk
        paths = JournalPaths(root=journal_dir)
        _seed_workflow_graph(paths, "wf1", _linear_steps(), "plan")
        walk.write(paths, walk.WalkState(task_id="wf1", current="__done__"))
        rc = main(["--journal-dir", str(journal_dir), "release", "wf1",
                   "--state", "done", "--driver", "Anzai"])
        assert rc == 0
        s = Status.from_json(paths.status_json("wf1").read_text())
        assert s.state is State.DONE

    def test_full_drive_cycle_two_entries(self, journal_dir, tmp_path):
        # claim (driver trace) + release done (assignee work note) leave
        # two correctly-attributed entries for the one task.
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.PENDING, persona="Rukawa")
        assert main(["--journal-dir", str(journal_dir), "claim", "t1",
                     "--driver", "Anzai"]) == 0
        assert main(["--journal-dir", str(journal_dir), "release", "t1",
                     "--state", "done", "--driver", "Anzai",
                     "--output", self._note(tmp_path)]) == 0
        entries = worklog.list_entries(paths, "t1")
        assert [(e.seq, e.persona, e.step) for e in entries] == [
            (1, "Anzai", "drive"),
            (2, "Rukawa", "task-work"),
        ]


# ---------------------------------------------------------------------------
# Phase 1c: step-done graph-walk gate
# ---------------------------------------------------------------------------

class TestStepDone:
    """``journal step-done`` writes the acting persona's worklog entry
    (attributed from the compiled step file) and advances the walk cursor
    in-order. The per-step counterpart to the kind=task release gate."""

    def _run(self, journal_dir, task, step, verdict, output, **extra):
        argv = ["--journal-dir", str(journal_dir), "step-done",
                "--task", task, "--step", step, "--verdict", verdict,
                "--output", output]
        for k, v in extra.items():
            argv += [f"--{k.replace('_', '-')}", v]
        return main(argv)

    def test_writes_persona_entry_and_advances(self, journal_dir, tmp_path):
        from tigerharness.journal import walk
        paths = JournalPaths(root=journal_dir)
        _seed_workflow_graph(paths, "wf1", _linear_steps(), "plan")
        rc = self._run(journal_dir, "wf1", "plan", "APPROVE",
                       _note(tmp_path))
        assert rc == 0
        [e] = worklog.list_entries(paths, "wf1")
        assert e.persona == "Akagi"          # from the step file, not a flag
        assert e.role == "planner"
        assert e.step == "plan"
        assert e.kind == "workflow"
        assert e.verdict == "APPROVE"
        assert "Did the step" in e.body
        # cursor moved to the on_approve target
        assert walk.read(paths, "wf1").current == "build"

    def test_json_output(self, journal_dir, tmp_path, capsys):
        paths = JournalPaths(root=journal_dir)
        _seed_workflow_graph(paths, "wf1", _linear_steps(), "plan")
        rc = self._run(journal_dir, "wf1", "plan", "APPROVE",
                       _note(tmp_path), format="json")
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["next"] == "build"
        assert payload["terminal"] is False
        assert payload["persona"] == "Akagi"
        assert payload["worklog_seq"] == 1

    def test_revise_self_loops_then_approves(self, journal_dir, tmp_path):
        from tigerharness.journal import walk
        paths = JournalPaths(root=journal_dir)
        _seed_workflow_graph(paths, "wf1", _linear_steps(), "plan")
        assert self._run(journal_dir, "wf1", "plan", "REVISE",
                         _note(tmp_path, name="r1.md")) == 0
        # REVISE routes plan -> plan; cursor stays, entry recorded.
        assert walk.read(paths, "wf1").current == "plan"
        assert len(worklog.list_entries(paths, "wf1")) == 1
        assert self._run(journal_dir, "wf1", "plan", "APPROVE",
                         _note(tmp_path, name="r2.md")) == 0
        assert walk.read(paths, "wf1").current == "build"
        assert len(worklog.list_entries(paths, "wf1")) == 2

    def test_walk_to_done_terminal(self, journal_dir, tmp_path, capsys):
        from tigerharness.journal import walk
        paths = JournalPaths(root=journal_dir)
        _seed_workflow_graph(paths, "wf1", _linear_steps(), "plan")
        assert self._run(journal_dir, "wf1", "plan", "APPROVE",
                         _note(tmp_path, name="a.md")) == 0
        rc = self._run(journal_dir, "wf1", "build", "APPROVE",
                       _note(tmp_path, name="b.md"))
        assert rc == 0
        assert walk.read(paths, "wf1").current == "__done__"
        assert "walk complete" in capsys.readouterr().out

    def test_block_escalates(self, journal_dir, tmp_path, capsys):
        from tigerharness.journal import walk
        paths = JournalPaths(root=journal_dir)
        _seed_workflow_graph(paths, "wf1", _linear_steps(), "plan")
        rc = self._run(journal_dir, "wf1", "plan", "BLOCK", _note(tmp_path))
        assert rc == 0
        assert walk.read(paths, "wf1").current == "__escalate__"
        assert "escalated" in capsys.readouterr().out

    def test_out_of_order_refused(self, journal_dir, tmp_path, capsys):
        paths = JournalPaths(root=journal_dir)
        _seed_workflow_graph(paths, "wf1", _linear_steps(), "plan")
        # The walk hasn't started; entrypoint is plan, so build is wrong.
        rc = self._run(journal_dir, "wf1", "build", "APPROVE",
                       _note(tmp_path))
        assert rc == 1
        assert "out-of-order" in capsys.readouterr().err
        assert worklog.list_entries(paths, "wf1") == []

    def test_unknown_step_refused(self, journal_dir, tmp_path, capsys):
        paths = JournalPaths(root=journal_dir)
        _seed_workflow_graph(paths, "wf1", _linear_steps(), "plan")
        rc = self._run(journal_dir, "wf1", "ghost", "APPROVE",
                       _note(tmp_path))
        assert rc == 1
        assert "not in the compiled graph" in capsys.readouterr().err

    def test_terminal_walk_refuses_further(
        self, journal_dir, tmp_path, capsys,
    ):
        from tigerharness.journal import walk
        paths = JournalPaths(root=journal_dir)
        _seed_workflow_graph(paths, "wf1", _linear_steps(), "plan")
        walk.write(paths, walk.WalkState(task_id="wf1", current="__done__"))
        rc = self._run(journal_dir, "wf1", "plan", "APPROVE",
                       _note(tmp_path))
        assert rc == 1
        assert "already terminal" in capsys.readouterr().err

    def test_empty_output_refused(self, journal_dir, tmp_path, capsys):
        paths = JournalPaths(root=journal_dir)
        _seed_workflow_graph(paths, "wf1", _linear_steps(), "plan")
        rc = self._run(journal_dir, "wf1", "plan", "APPROVE",
                       _note(tmp_path, "   \n"))
        assert rc == 1
        assert "empty" in capsys.readouterr().err
        assert worklog.list_entries(paths, "wf1") == []

    def test_unreadable_output_refused(self, journal_dir, tmp_path, capsys):
        paths = JournalPaths(root=journal_dir)
        _seed_workflow_graph(paths, "wf1", _linear_steps(), "plan")
        rc = self._run(journal_dir, "wf1", "plan", "APPROVE",
                       str(tmp_path / "nope.md"))
        assert rc == 1
        assert "cannot read --output" in capsys.readouterr().err

    def test_non_workflow_refused(self, journal_dir, tmp_path, capsys):
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.IN_PROGRESS)
        rc = self._run(journal_dir, "t1", "plan", "APPROVE", _note(tmp_path))
        assert rc == 1
        assert "workflow-only" in capsys.readouterr().err

    def test_not_landed_refused(self, journal_dir, tmp_path, capsys):
        from tigerharness.journal.models import CompilePhase
        paths = JournalPaths(root=journal_dir)
        paths.ensure()
        (paths.active / "wf1").mkdir()
        st = Status.new_workflow(id="wf1", title="W", playbook_name="default")
        st.state = State.IN_PROGRESS
        st.compile_phase = CompilePhase.DRAFTING
        paths.status_json("wf1").write_text(st.to_json())
        rc = self._run(journal_dir, "wf1", "plan", "APPROVE", _note(tmp_path))
        assert rc == 1
        assert "compile_phase=drafting" in capsys.readouterr().err

    def test_no_such_task(self, journal_dir, tmp_path, capsys):
        rc = self._run(journal_dir, "ghost", "plan", "APPROVE",
                       _note(tmp_path))
        assert rc == 1
        assert "no task" in capsys.readouterr().err

    def test_session_ref_mismatch_refused(
        self, journal_dir, tmp_path, capsys,
    ):
        paths = JournalPaths(root=journal_dir)
        _seed_workflow_graph(paths, "wf1", _linear_steps(), "plan",
                             session_ref="real")
        rc = self._run(journal_dir, "wf1", "plan", "APPROVE",
                       _note(tmp_path), session_ref="stray")
        assert rc == 1
        assert "does not match" in capsys.readouterr().err

    def test_session_ref_match_allows(self, journal_dir, tmp_path):
        paths = JournalPaths(root=journal_dir)
        _seed_workflow_graph(paths, "wf1", _linear_steps(), "plan",
                             session_ref="real")
        rc = self._run(journal_dir, "wf1", "plan", "APPROVE",
                       _note(tmp_path), session_ref="real")
        assert rc == 0

    def test_missing_orchestration_refused(
        self, journal_dir, tmp_path, capsys,
    ):
        from tigerharness.journal.models import CompilePhase
        paths = JournalPaths(root=journal_dir)
        paths.ensure()
        (paths.active / "wf1").mkdir()
        st = Status.new_workflow(id="wf1", title="W", playbook_name="default")
        st.state = State.IN_PROGRESS
        st.compile_pending = False
        st.compile_phase = CompilePhase.COMPLETE
        paths.status_json("wf1").write_text(st.to_json())
        rc = self._run(journal_dir, "wf1", "plan", "APPROVE", _note(tmp_path))
        assert rc == 1
        assert "no orchestration.json" in capsys.readouterr().err

    def test_malformed_orchestration_refused(
        self, journal_dir, tmp_path, capsys,
    ):
        paths = JournalPaths(root=journal_dir)
        _seed_workflow_graph(paths, "wf1", _linear_steps(), "plan")
        (paths.active / "wf1" / "orchestration.json").write_text("{bad")
        rc = self._run(journal_dir, "wf1", "plan", "APPROVE", _note(tmp_path))
        assert rc == 1
        assert "cannot read orchestration.json" in capsys.readouterr().err

    def test_invalid_orchestration_shape_refused(
        self, journal_dir, tmp_path, capsys,
    ):
        paths = JournalPaths(root=journal_dir)
        _seed_workflow_graph(paths, "wf1", _linear_steps(), "plan")
        # Valid JSON, invalid Orchestration (entrypoint not in steps).
        (paths.active / "wf1" / "orchestration.json").write_text(
            json.dumps({"task_id": "wf1", "steps": ["plan"],
                        "entrypoint": "ghost"})
        )
        rc = self._run(journal_dir, "wf1", "plan", "APPROVE", _note(tmp_path))
        assert rc == 1
        assert "malformed" in capsys.readouterr().err

    def test_corrupt_walk_json_refused(self, journal_dir, tmp_path, capsys):
        paths = JournalPaths(root=journal_dir)
        _seed_workflow_graph(paths, "wf1", _linear_steps(), "plan")
        paths.walk_json("wf1").write_text("{not json")
        rc = self._run(journal_dir, "wf1", "plan", "APPROVE", _note(tmp_path))
        assert rc == 1
        assert "cannot read walk.json" in capsys.readouterr().err

    def test_step_file_unreadable_refused(
        self, journal_dir, tmp_path, capsys,
    ):
        paths = JournalPaths(root=journal_dir)
        _seed_workflow_graph(paths, "wf1", _linear_steps(), "plan")
        (paths.active / "wf1" / "steps" / "plan.md").unlink()
        rc = self._run(journal_dir, "wf1", "plan", "APPROVE", _note(tmp_path))
        assert rc == 1
        assert "cannot read step file" in capsys.readouterr().err

    def test_no_edges_for_step_refused(self, journal_dir, tmp_path, capsys):
        paths = JournalPaths(root=journal_dir)
        _seed_workflow_graph(paths, "wf1", _linear_steps(), "plan")
        # Strip the edges map (valid Orchestration: edges may be a subset
        # of steps). step-done can then find no route for the step.
        orch_path = paths.active / "wf1" / "orchestration.json"
        data = json.loads(orch_path.read_text())
        data["edges"] = {}
        orch_path.write_text(json.dumps(data))
        rc = self._run(journal_dir, "wf1", "plan", "APPROVE", _note(tmp_path))
        assert rc == 1
        assert "no edges for step" in capsys.readouterr().err

    def test_worklog_write_failure_does_not_advance(
        self, journal_dir, tmp_path, capsys, monkeypatch,
    ):
        from tigerharness.journal import walk
        paths = JournalPaths(root=journal_dir)
        _seed_workflow_graph(paths, "wf1", _linear_steps(), "plan")

        def _boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(worklog, "write_entry", _boom)
        rc = self._run(journal_dir, "wf1", "plan", "APPROVE", _note(tmp_path))
        assert rc == 1
        assert "could not write worklog" in capsys.readouterr().err
        # Walk not advanced -- no cursor written at all.
        assert walk.read(paths, "wf1") is None


class TestReleaseWorkflowGate:
    """Marking a kind=workflow DONE in a drive requires the walk to have
    reached __done__: every step that ran left its memory via step-done."""

    def test_done_with_complete_walk_allowed(self, journal_dir):
        from tigerharness.journal import walk
        paths = JournalPaths(root=journal_dir)
        _seed_workflow_graph(paths, "wf1", _linear_steps(), "plan")
        walk.write(paths, walk.WalkState(task_id="wf1", current="__done__"))
        rc = main(["--journal-dir", str(journal_dir), "release", "wf1",
                   "--state", "done", "--driver", "Anzai"])
        assert rc == 0
        s = Status.from_json(paths.status_json("wf1").read_text())
        assert s.state is State.DONE

    def test_done_refused_when_walk_incomplete(self, journal_dir, capsys):
        from tigerharness.journal import walk
        paths = JournalPaths(root=journal_dir)
        _seed_workflow_graph(paths, "wf1", _linear_steps(), "plan")
        walk.write(paths, walk.WalkState(task_id="wf1", current="build"))
        rc = main(["--journal-dir", str(journal_dir), "release", "wf1",
                   "--state", "done", "--driver", "Anzai"])
        assert rc == 1
        assert "not __done__" in capsys.readouterr().err
        s = Status.from_json(paths.status_json("wf1").read_text())
        assert s.state is State.IN_PROGRESS

    def test_done_refused_when_no_walk(self, journal_dir, capsys):
        paths = JournalPaths(root=journal_dir)
        _seed_workflow_graph(paths, "wf1", _linear_steps(), "plan")
        rc = main(["--journal-dir", str(journal_dir), "release", "wf1",
                   "--state", "done", "--driver", "Anzai"])
        assert rc == 1
        assert "no walk state" in capsys.readouterr().err

    def test_done_refused_when_walk_corrupt(self, journal_dir, capsys):
        paths = JournalPaths(root=journal_dir)
        _seed_workflow_graph(paths, "wf1", _linear_steps(), "plan")
        paths.walk_json("wf1").write_text("{bad")
        rc = main(["--journal-dir", str(journal_dir), "release", "wf1",
                   "--state", "done", "--driver", "Anzai"])
        assert rc == 1
        assert "cannot read walk.json" in capsys.readouterr().err

    def test_blocked_not_gated(self, journal_dir):
        # Escalation (release --state blocked) needs no walk-complete: the
        # steps that ran already left their notes via step-done.
        paths = JournalPaths(root=journal_dir)
        _seed_workflow_graph(paths, "wf1", _linear_steps(), "plan")
        rc = main(["--journal-dir", str(journal_dir), "release", "wf1",
                   "--state", "blocked", "--driver", "Anzai"])
        assert rc == 0
        s = Status.from_json(paths.status_json("wf1").read_text())
        assert s.state is State.BLOCKED

    def test_no_driver_done_not_gated(self, journal_dir):
        # The plain subscription backend (no --driver) keeps its behaviour:
        # no walk-complete requirement, no worklog side-effect.
        paths = JournalPaths(root=journal_dir)
        _seed_workflow_graph(paths, "wf1", _linear_steps(), "plan")
        rc = main(["--journal-dir", str(journal_dir), "release", "wf1",
                   "--state", "done"])
        assert rc == 0
        s = Status.from_json(paths.status_json("wf1").read_text())
        assert s.state is State.DONE

    def test_full_workflow_walk_then_done(self, journal_dir, tmp_path):
        # End-to-end: claim (driver trace) -> step plan -> step build ->
        # release done. Memory: 1 driver entry + 2 step entries, each
        # attributed to the right persona.
        paths = JournalPaths(root=journal_dir)
        _seed_workflow_graph(paths, "wf1", _linear_steps(), "plan",
                             state=State.PENDING, session_ref=None)
        assert main(["--journal-dir", str(journal_dir), "claim", "wf1",
                     "--driver", "Anzai"]) == 0
        assert main(["--journal-dir", str(journal_dir), "step-done",
                     "--task", "wf1", "--step", "plan", "--verdict",
                     "APPROVE", "--output", _note(tmp_path, name="p.md")]) == 0
        assert main(["--journal-dir", str(journal_dir), "step-done",
                     "--task", "wf1", "--step", "build", "--verdict",
                     "APPROVE", "--output", _note(tmp_path, name="b.md")]) == 0
        assert main(["--journal-dir", str(journal_dir), "release", "wf1",
                     "--state", "done", "--driver", "Anzai"]) == 0
        entries = worklog.list_entries(paths, "wf1")
        assert [(e.persona, e.step) for e in entries] == [
            ("Anzai", "drive"),
            ("Akagi", "plan"),
            ("Rukawa", "build"),
        ]


class TestClaimRailGuard:
    """The cost-discipline rail guard: a bridge-spawned session (the
    slack bridge exports TIGERHARNESS_SLACK_THREAD_TS into every turn)
    may only SCHEDULE journal tasks, never drive them. The guard runs
    before any status read, so a refused claim changes nothing."""

    def test_bridge_env_refuses_claim(self, journal_dir, monkeypatch, capsys):
        monkeypatch.setenv("TIGERHARNESS_SLACK_THREAD_TS", "555.42")
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.PENDING)
        before = paths.status_json("t1").read_bytes()
        assert main(["--journal-dir", str(journal_dir),
                     "claim", "t1", "--driver", "Anzai"]) == 1
        err = capsys.readouterr().err
        assert "claim refused" in err
        assert "TIGERHARNESS_SLACK_THREAD_TS" in err
        assert "--allow-api-drive" in err
        assert "schedule" in err
        # Guard-first placement: status.json is byte-unchanged (state
        # still pending, sessions not bumped, no session_ref).
        assert paths.status_json("t1").read_bytes() == before

    def test_refusal_logs_warning_in_claim_refused_family(
        self, journal_dir, monkeypatch, caplog,
    ):
        import logging

        monkeypatch.setenv("TIGERHARNESS_SLACK_THREAD_TS", "555.42")
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.PENDING)
        with caplog.at_level(logging.WARNING):
            assert main(["--journal-dir", str(journal_dir),
                         "claim", "t1"]) == 1
        assert any(
            r.levelno == logging.WARNING
            and r.getMessage().startswith("claim refused:")
            and "bridge session" in r.getMessage()
            for r in caplog.records
        )

    def test_override_flag_allows_claim(self, journal_dir, monkeypatch):
        monkeypatch.setenv("TIGERHARNESS_SLACK_THREAD_TS", "555.42")
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.PENDING)
        assert main(["--journal-dir", str(journal_dir), "claim", "t1",
                     "--driver", "Anzai", "--allow-api-drive"]) == 0
        # The deliberate override keeps the registration fallback alive.
        assert drive_sessions.registered_threads(
            paths.drive_sessions_json
        ) == {"555.42"}

    def test_no_env_claims_unaffected(self, journal_dir, monkeypatch):
        # The no-false-positive direction: interactive sessions (no env
        # var) claim exactly as before, no flag needed.
        monkeypatch.delenv("TIGERHARNESS_SLACK_THREAD_TS", raising=False)
        paths = JournalPaths(root=journal_dir)
        _seed(paths, "t1", state=State.PENDING)
        assert main(["--journal-dir", str(journal_dir),
                     "claim", "t1", "--driver", "Anzai"]) == 0

    def test_bridge_env_does_not_block_scheduling(
        self, journal_dir, monkeypatch, tmp_path, capsys,
    ):
        # The schedule-only promise, pinned: with the bridge env set,
        # ``journal new`` (the LLM-free scaffolder) still works -- the
        # guard's scope is claim-only, forever.
        monkeypatch.setenv("TIGERHARNESS_SLACK_THREAD_TS", "555.42")
        prd = tmp_path / "prd.md"
        prd.write_text("# Scheduled from Slack\nbody\n", encoding="utf-8")
        assert main(["--journal-dir", str(journal_dir), "new",
                     "--prd", str(prd), "--persona", "Anzai"]) == 0
        out = capsys.readouterr().out
        assert "Scaffolded:" in out

"""Tests for ``tigerharness.journal.cli``: new / list / status / sweep."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
        assert payload["active"][0]["id"] == "20260602-a-11111111"
        assert payload["malformed"] == []

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
        _seed(paths, "ip-fresh", state=State.IN_PROGRESS,
              updated_at="2026-06-02T08:00:00Z")
        rc = main([
            "--journal-dir", str(journal_dir),
            "sweep", "--stuck-timeout", "1000000",  # never stale
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "p1" in out
        assert "Archived" in out and "d1" in out
        assert "Fresh in_progress" in out and "ip-fresh" in out

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

"""Tests for the Phase 1.5 in-session compile CLI subcommands
(``compile-context``, ``compile-prompts``, ``validate-graph``,
``land-compile``, ``abort``, ``validate-personas``).

The handlers are pure Python with no ``claude -p`` calls. We exercise
each by constructing an ``argparse.Namespace`` directly and asserting
on stdout / stderr / exit code / on-disk side effects.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from tigerharness.journal import compile_cli
from tigerharness.journal.compile_cli import (
    _compile_dir,
    _guess_team_for_status,
    _load_workflow_status,
    _paths_from_args,
    _read_brief_and_playbook,
    _render_frontmatter,
    _roster_for_task,
    _write_atomic,
    build_subparsers,
    cmd_abort,
    cmd_compile_context,
    cmd_compile_prompts,
    cmd_land_compile,
    cmd_validate_graph,
    cmd_validate_personas,
)
from tigerharness.journal.models import CompilePhase, State, Status
from tigerharness.journal.paths import JournalPaths
from tigerharness.journal.scaffold import COMPILE_PERSONAS, new_workflow_task


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_team(root: Path, *, personas: list[str] | None = None,
               name: str = "Shohoku") -> Path:
    team = root / "teams" / name
    (team / "configs").mkdir(parents=True)
    if personas is None:
        personas = list(COMPILE_PERSONAS) + ["Mitsui"]
    lines = ["personas:\n"]
    for p in personas:
        lines.append(f"  - name: {p}\n")
    (team / "configs" / "personas.yaml").write_text("".join(lines))
    for p in personas:
        pdir = team / "personas" / p
        pdir.mkdir(parents=True)
        (pdir / "prompt.md").write_text(f"You are {p}.\n")
    return team


@pytest.fixture
def journal_dir(tmp_path):
    d = tmp_path / "journal"
    d.mkdir()
    return d


@pytest.fixture
def team_root(tmp_path, monkeypatch):
    """Build a complete team root and chdir into it so
    ``_roster_for_task`` + ``_guess_team_for_status`` can find it."""
    root = _make_team(tmp_path)
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def scaffolded(team_root, journal_dir):
    """Scaffold one workflow task. Returns (task_id, paths)."""
    playbook_text = (
        "# A playbook\n\n"
        "Anzai drafts. Akagi critiques. Ayako critiques. Mitsui executes.\n"
    )
    paths = JournalPaths(root=journal_dir)
    result = new_workflow_task(
        brief_text="# Title\nBody.\n",
        playbook_text=playbook_text,
        playbook_name="default",
        team_root=team_root,
        paths=paths,
        captain="Mitsui",
    )
    return result.task_id, paths


# A valid steps bundle the drafter parser can consume.
_VALID_BUNDLE = (
    "```steps-bundle\n"
    "## step: 01-anzai-plan\n"
    "---\n"
    "id: 01-anzai-plan\n"
    "persona: Anzai\n"
    "role: planner\n"
    "on_approve: __done__\n"
    "on_revise: 01-anzai-plan\n"
    "on_block: __escalate__\n"
    "max_iters: 5\n"
    "timeout_sec: 1800\n"
    "parallel_with: []\n"
    "---\n"
    "Plan the work.\n"
    "```\n"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestPathsFromArgs:
    def test_explicit_journal_dir(self, tmp_path):
        ns = argparse.Namespace(journal_dir=str(tmp_path))
        paths = _paths_from_args(ns)
        assert paths.root == tmp_path.resolve()

    def test_default_when_unset(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TIGERHARNESS_JOURNAL_DIR", str(tmp_path))
        ns = argparse.Namespace(journal_dir="")
        paths = _paths_from_args(ns)
        assert paths.root == tmp_path.resolve()

    def test_missing_attr(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TIGERHARNESS_JOURNAL_DIR", str(tmp_path))
        ns = argparse.Namespace()  # no journal_dir at all
        paths = _paths_from_args(ns)
        assert paths.root == tmp_path.resolve()


class TestLoadWorkflowStatus:
    def test_happy_path(self, scaffolded):
        task_id, paths = scaffolded
        s = _load_workflow_status(paths, task_id)
        assert isinstance(s, Status)
        assert s.kind == "workflow"

    def test_unsafe_id(self, journal_dir):
        paths = JournalPaths(root=journal_dir)
        msg = _load_workflow_status(paths, "../escape")
        assert isinstance(msg, str)
        assert "not path-safe" in msg

    def test_missing(self, journal_dir):
        paths = JournalPaths(root=journal_dir)
        msg = _load_workflow_status(paths, "2026-05-30-x-mitsui-abc12")
        assert isinstance(msg, str)
        assert "no active workflow task" in msg

    def test_malformed_status(self, scaffolded):
        task_id, paths = scaffolded
        paths.status_json(task_id).write_text("not json at all")
        msg = _load_workflow_status(paths, task_id)
        assert isinstance(msg, str)
        assert "malformed" in msg

    def test_task_kind_rejected(self, journal_dir):
        from tigerharness.journal.scaffold import new_task
        paths = JournalPaths(root=journal_dir)
        r = new_task(prd_text="# t\nb\n", persona="P", paths=paths)
        msg = _load_workflow_status(paths, r.task_id)
        assert isinstance(msg, str)
        assert "kind='task'" in msg


class TestCompileDir:
    def test_path_under_task(self, scaffolded):
        task_id, paths = scaffolded
        cd = _compile_dir(paths, task_id)
        assert cd == paths.task_dir(task_id) / "compile"

    def test_getter_is_pure(self, scaffolded):
        """Calling _compile_dir does not create the directory."""
        task_id, paths = scaffolded
        cd = _compile_dir(paths, task_id)
        assert not cd.exists()


class TestWriteAtomic:
    def test_writes_content(self, tmp_path):
        p = tmp_path / "sub" / "f.txt"
        _write_atomic(p, "hello\n")
        assert p.read_text() == "hello\n"

    def test_creates_parents(self, tmp_path):
        p = tmp_path / "a" / "b" / "c" / "f.txt"
        _write_atomic(p, "x")
        assert p.read_text() == "x"

    def test_overwrites_existing(self, tmp_path):
        p = tmp_path / "f.txt"
        p.write_text("old")
        _write_atomic(p, "new")
        assert p.read_text() == "new"

    def test_unlink_after_rename_swallows_missing(self, tmp_path, monkeypatch):
        """When os.replace renames the temp away, the finally-block's
        os.unlink raises FileNotFoundError -- which is swallowed."""
        # Force the path to exist already so the rename code path runs.
        p = tmp_path / "f.txt"
        p.write_text("a")
        _write_atomic(p, "b")
        assert p.read_text() == "b"


class TestReadBriefAndPlaybook:
    def test_reads_scaffolded_files(self, scaffolded):
        task_id, paths = scaffolded
        brief, playbook = _read_brief_and_playbook(paths, task_id)
        assert "Title" in brief
        assert "Anzai drafts" in playbook


class TestRosterForTask:
    def test_returns_sorted_roster_in_team_root(self, scaffolded):
        task_id, paths = scaffolded
        roster = _roster_for_task(paths, task_id)
        assert "Anzai" in roster
        assert roster == sorted(roster)

    def test_returns_empty_outside_team_root(self, scaffolded,
                                              monkeypatch, tmp_path):
        task_id, paths = scaffolded
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        assert _roster_for_task(paths, task_id) == []


class TestGuessTeamForStatus:
    def test_returns_cwd_name_when_team_root(self, team_root):
        # team_root fixture chdir's into the team root.
        assert _guess_team_for_status() == "Shohoku"

    def test_returns_unknown_when_not_team_root(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert _guess_team_for_status() == "unknown"


class TestRenderFrontmatter:
    def test_skips_body_renders_list_inline(self):
        from tigerharness.workflow_runner.compile.drafter import (
            _parse_response,
        )
        steps = _parse_response(_VALID_BUNDLE)
        rendered = _render_frontmatter(steps[0])
        assert "id: 01-anzai-plan\n" in rendered
        assert "parallel_with: []\n" in rendered
        assert "body:" not in rendered


# ---------------------------------------------------------------------------
# compile-context
# ---------------------------------------------------------------------------

class TestCompileContext:
    def test_prints_all_sections(self, scaffolded, capsys):
        task_id, paths = scaffolded
        ns = argparse.Namespace(
            journal_dir=str(paths.root), task_id=task_id,
        )
        rc = cmd_compile_context(ns)
        assert rc == 0
        out = capsys.readouterr().out
        assert "# compile-context for task" in out
        assert "## Task" in out
        assert "## Roster" in out
        assert "## Brief" in out
        assert "## Playbook" in out
        assert "## Drafter prompt" in out

    def test_error_on_unknown_task(self, journal_dir, capsys):
        ns = argparse.Namespace(
            journal_dir=str(journal_dir),
            task_id="2026-05-30-nope-mitsui-abc12",
        )
        rc = cmd_compile_context(ns)
        assert rc == 1
        assert "no active workflow task" in capsys.readouterr().err

    def test_empty_roster_block(self, scaffolded, tmp_path,
                                 monkeypatch, capsys):
        """If cwd is not a team root, the printed roster section says so."""
        task_id, paths = scaffolded
        elsewhere = tmp_path / "out"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        ns = argparse.Namespace(
            journal_dir=str(paths.root), task_id=task_id,
        )
        rc = cmd_compile_context(ns)
        assert rc == 0
        assert "(roster unresolved" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# compile-prompts
# ---------------------------------------------------------------------------

class TestCompilePrompts:
    def test_drafter_no_feedback(self, scaffolded, capsys):
        task_id, paths = scaffolded
        ns = argparse.Namespace(
            journal_dir=str(paths.root), task=task_id, kind="drafter",
            feedback=None, draft=None, trace=None,
        )
        rc = cmd_compile_prompts(ns)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Playbook" in out and "Task brief" in out

    def test_drafter_with_feedback(self, scaffolded, capsys):
        task_id, paths = scaffolded
        ns = argparse.Namespace(
            journal_dir=str(paths.root), task=task_id, kind="drafter",
            feedback="Re-do step 2.", draft=None, trace=None,
        )
        rc = cmd_compile_prompts(ns)
        assert rc == 0
        assert "Re-do step 2." in capsys.readouterr().out

    def test_error_on_unknown_task(self, journal_dir, capsys):
        ns = argparse.Namespace(
            journal_dir=str(journal_dir),
            task="2026-05-30-nope-mitsui-abc12",
            kind="drafter", feedback=None, draft=None, trace=None,
        )
        rc = cmd_compile_prompts(ns)
        assert rc == 1
        assert "no active workflow task" in capsys.readouterr().err

    def test_akagi_requires_draft(self, scaffolded, capsys):
        task_id, paths = scaffolded
        ns = argparse.Namespace(
            journal_dir=str(paths.root), task=task_id, kind="akagi",
            feedback=None, draft=None, trace="some-trace",
        )
        rc = cmd_compile_prompts(ns)
        assert rc == 2
        assert "--draft is required" in capsys.readouterr().err

    def test_akagi_requires_trace(self, scaffolded, tmp_path, capsys):
        task_id, paths = scaffolded
        d = tmp_path / "draft.md"
        d.write_text(_VALID_BUNDLE)
        ns = argparse.Namespace(
            journal_dir=str(paths.root), task=task_id, kind="akagi",
            feedback=None, draft=str(d), trace=None,
        )
        rc = cmd_compile_prompts(ns)
        assert rc == 2
        assert "--trace is required" in capsys.readouterr().err

    def test_akagi_cannot_read_draft(self, scaffolded, tmp_path, capsys):
        task_id, paths = scaffolded
        t = tmp_path / "trace.txt"
        t.write_text("trace")
        ns = argparse.Namespace(
            journal_dir=str(paths.root), task=task_id, kind="akagi",
            feedback=None, draft=str(tmp_path / "nope.md"), trace=str(t),
        )
        rc = cmd_compile_prompts(ns)
        assert rc == 1
        assert "cannot read draft/trace" in capsys.readouterr().err

    def test_akagi_draft_unparseable(self, scaffolded, tmp_path, capsys):
        task_id, paths = scaffolded
        d = tmp_path / "draft.md"
        d.write_text("no bundle here\n")
        t = tmp_path / "trace.txt"
        t.write_text("trace")
        ns = argparse.Namespace(
            journal_dir=str(paths.root), task=task_id, kind="akagi",
            feedback=None, draft=str(d), trace=str(t),
        )
        rc = cmd_compile_prompts(ns)
        assert rc == 1
        assert "does not parse" in capsys.readouterr().err

    def test_akagi_happy(self, scaffolded, tmp_path, capsys):
        task_id, paths = scaffolded
        d = tmp_path / "draft.md"
        d.write_text(_VALID_BUNDLE)
        t = tmp_path / "trace.txt"
        t.write_text("(dry-run trace)\n")
        ns = argparse.Namespace(
            journal_dir=str(paths.root), task=task_id, kind="akagi",
            feedback=None, draft=str(d), trace=str(t),
        )
        rc = cmd_compile_prompts(ns)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Akagi" in out or "AKAGI" in out or "critic" in out.lower()
        assert "(dry-run trace)" in out

    def test_ayako_happy(self, scaffolded, tmp_path, capsys):
        task_id, paths = scaffolded
        d = tmp_path / "draft.md"
        d.write_text(_VALID_BUNDLE)
        t = tmp_path / "trace.txt"
        t.write_text("(trace)\n")
        ns = argparse.Namespace(
            journal_dir=str(paths.root), task=task_id, kind="ayako",
            feedback=None, draft=str(d), trace=str(t),
        )
        rc = cmd_compile_prompts(ns)
        assert rc == 0
        assert "(trace)" in capsys.readouterr().out

    def test_appends_trailing_newline_when_missing(
        self, scaffolded, tmp_path, capsys, monkeypatch,
    ):
        """If the assembled prompt does not end in \\n, the handler adds one.
        We patch _build_prompt to return a no-newline string to drive the
        else-branch."""
        task_id, paths = scaffolded
        import tigerharness.workflow_runner.compile.drafter as drafter_mod
        monkeypatch.setattr(
            drafter_mod, "_build_prompt",
            lambda **kw: "no-newline-prompt",
        )
        ns = argparse.Namespace(
            journal_dir=str(paths.root), task=task_id, kind="drafter",
            feedback=None, draft=None, trace=None,
        )
        rc = cmd_compile_prompts(ns)
        assert rc == 0
        out = capsys.readouterr().out
        assert out == "no-newline-prompt\n"


# ---------------------------------------------------------------------------
# validate-graph
# ---------------------------------------------------------------------------

class TestValidateGraph:
    def test_happy(self, scaffolded, tmp_path, capsys):
        task_id, paths = scaffolded
        d = tmp_path / "draft.md"
        d.write_text(_VALID_BUNDLE)
        ns = argparse.Namespace(
            journal_dir=str(paths.root), task=task_id, draft=str(d),
        )
        rc = cmd_validate_graph(ns)
        out = capsys.readouterr().out
        envelope = json.loads(out)
        assert envelope["ok"] is True
        assert envelope["errors"] == []
        assert isinstance(envelope["trace"], str)
        assert rc == 0

    def test_parse_failure_returns_1(self, scaffolded, tmp_path, capsys):
        task_id, paths = scaffolded
        d = tmp_path / "draft.md"
        d.write_text("nothing parseable\n")
        ns = argparse.Namespace(
            journal_dir=str(paths.root), task=task_id, draft=str(d),
        )
        rc = cmd_validate_graph(ns)
        assert rc == 1
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["ok"] is False
        assert envelope["errors"][0]["validator"] == "parse"

    def test_unknown_task_returns_2(self, journal_dir, tmp_path, capsys):
        d = tmp_path / "draft.md"
        d.write_text(_VALID_BUNDLE)
        ns = argparse.Namespace(
            journal_dir=str(journal_dir),
            task="2026-05-30-nope-mitsui-abc12",
            draft=str(d),
        )
        rc = cmd_validate_graph(ns)
        assert rc == 2
        assert "no active workflow task" in capsys.readouterr().err

    def test_unreadable_draft_returns_2(self, scaffolded, capsys):
        task_id, paths = scaffolded
        ns = argparse.Namespace(
            journal_dir=str(paths.root), task=task_id,
            draft="/no/such/file.md",
        )
        rc = cmd_validate_graph(ns)
        assert rc == 2
        assert "cannot read draft" in capsys.readouterr().err

    def test_semantic_failure_returns_1(self, scaffolded, tmp_path, capsys):
        """Bundle parses but persona is not in roster -> validators reject."""
        task_id, paths = scaffolded
        bad = _VALID_BUNDLE.replace("persona: Anzai", "persona: NotInRoster")
        d = tmp_path / "draft.md"
        d.write_text(bad)
        ns = argparse.Namespace(
            journal_dir=str(paths.root), task=task_id, draft=str(d),
        )
        rc = cmd_validate_graph(ns)
        assert rc == 1
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["ok"] is False
        assert any(e["validator"] == "roster" for e in envelope["errors"])


# ---------------------------------------------------------------------------
# land-compile
# ---------------------------------------------------------------------------

class TestLandCompile:
    def _ns(self, paths, task_id, draft, transcript, rounds=2):
        return argparse.Namespace(
            journal_dir=str(paths.root),
            task=task_id,
            draft=str(draft),
            transcript=str(transcript),
            rounds=rounds,
        )

    def test_happy_path(self, scaffolded, tmp_path, capsys):
        task_id, paths = scaffolded
        d = tmp_path / "draft.md"
        d.write_text(_VALID_BUNDLE)
        t = tmp_path / "transcript.md"
        t.write_text("Round 1: APPROVE.\n")
        rc = cmd_land_compile(self._ns(paths, task_id, d, t))
        assert rc == 0
        # Canonical files exist.
        task_dir = paths.task_dir(task_id)
        assert (task_dir / "orchestration.json").is_file()
        assert (task_dir / "compile_critique.md").read_text() == \
            "Round 1: APPROVE.\n"
        assert (task_dir / "steps" / "01-anzai-plan.md").is_file()
        # status.json flipped LAST.
        status = Status.from_json(paths.status_json(task_id).read_text())
        assert status.compile_pending is False
        assert status.compile_phase == CompilePhase.COMPLETE
        # compile/final/ cleaned up.
        assert not (task_dir / "compile" / "final").exists()
        out = capsys.readouterr().out
        assert f"landed: {task_id}" in out
        assert "steps: 1" in out

    def test_unknown_task_returns_1(self, journal_dir, tmp_path, capsys):
        d = tmp_path / "draft.md"
        d.write_text(_VALID_BUNDLE)
        t = tmp_path / "t.md"
        t.write_text("x")
        ns = argparse.Namespace(
            journal_dir=str(journal_dir),
            task="2026-05-30-nope-mitsui-abc12",
            draft=str(d), transcript=str(t), rounds=1,
        )
        rc = cmd_land_compile(ns)
        assert rc == 1
        assert "no active workflow task" in capsys.readouterr().err

    def test_unreadable_draft_or_transcript(
        self, scaffolded, tmp_path, capsys,
    ):
        task_id, paths = scaffolded
        t = tmp_path / "t.md"
        t.write_text("x")
        ns = argparse.Namespace(
            journal_dir=str(paths.root), task=task_id,
            draft="/no/such.md", transcript=str(t), rounds=1,
        )
        rc = cmd_land_compile(ns)
        assert rc == 1
        assert "cannot read draft/transcript" in capsys.readouterr().err

    def test_unparseable_draft(self, scaffolded, tmp_path, capsys):
        task_id, paths = scaffolded
        d = tmp_path / "draft.md"
        d.write_text("no bundle\n")
        t = tmp_path / "t.md"
        t.write_text("x")
        rc = cmd_land_compile(self._ns(paths, task_id, d, t))
        assert rc == 1
        assert "does not parse" in capsys.readouterr().err

    def test_tier1_revalidation_failure_returns_1(
        self, scaffolded, tmp_path, capsys,
    ):
        task_id, paths = scaffolded
        bad = _VALID_BUNDLE.replace("persona: Anzai", "persona: NotInRoster")
        d = tmp_path / "draft.md"
        d.write_text(bad)
        t = tmp_path / "t.md"
        t.write_text("x")
        rc = cmd_land_compile(self._ns(paths, task_id, d, t))
        assert rc == 1
        err = capsys.readouterr().err
        assert "Tier 1 re-validation failed" in err
        assert "[roster]" in err

    def test_overwrites_existing_steps_dir(
        self, scaffolded, tmp_path, capsys,
    ):
        """If a prior steps/ dir exists (e.g. a re-landed compile),
        land-compile rmtree's it before moving the staged dir in."""
        task_id, paths = scaffolded
        task_dir = paths.task_dir(task_id)
        (task_dir / "steps").mkdir()
        (task_dir / "steps" / "stale.md").write_text("old")
        d = tmp_path / "draft.md"
        d.write_text(_VALID_BUNDLE)
        t = tmp_path / "t.md"
        t.write_text("x")
        rc = cmd_land_compile(self._ns(paths, task_id, d, t))
        assert rc == 0
        assert not (task_dir / "steps" / "stale.md").exists()
        assert (task_dir / "steps" / "01-anzai-plan.md").is_file()

    def test_clears_orphan_files_in_staged_steps_dir(
        self, scaffolded, tmp_path,
    ):
        """A prior crashed land-compile may leave files in
        compile/final/steps/ -- they MUST be wiped before the new
        draft's step files are written, else the orphans leak into
        the canonical steps/ via shutil.move."""
        task_id, paths = scaffolded
        # Pre-seed a stale orphan in the staged compile/final/steps/.
        stale_dir = paths.task_dir(task_id) / "compile" / "final" / "steps"
        stale_dir.mkdir(parents=True)
        (stale_dir / "ghost.md").write_text("ghost-of-a-crashed-run")
        d = tmp_path / "draft.md"
        d.write_text(_VALID_BUNDLE)
        t = tmp_path / "t.md"
        t.write_text("x")
        rc = cmd_land_compile(self._ns(paths, task_id, d, t))
        assert rc == 0
        # The ghost must not have made it into canonical steps/.
        canonical_steps = paths.task_dir(task_id) / "steps"
        assert (canonical_steps / "01-anzai-plan.md").is_file()
        assert not (canonical_steps / "ghost.md").exists()

    def test_final_dir_cleanup_swallows_oserror(
        self, scaffolded, tmp_path, monkeypatch, capsys,
    ):
        """If shutil.rmtree of compile/final fails (e.g. perm error), the
        handler proceeds and still flips status. Patch rmtree to raise."""
        task_id, paths = scaffolded
        d = tmp_path / "draft.md"
        d.write_text(_VALID_BUNDLE)
        t = tmp_path / "t.md"
        t.write_text("x")
        import tigerharness.journal.compile_cli as mod
        real_rmtree = mod.shutil.rmtree
        calls = {"n": 0}
        def flaky(path, *a, **k):
            calls["n"] += 1
            # First call is the canonical-steps overwrite (won't happen
            # here since no steps/ exists). The cleanup call at the very
            # end is the one we want to make fail.
            if "final" in str(path):
                raise OSError("simulated permission error")
            return real_rmtree(path, *a, **k)
        monkeypatch.setattr(mod.shutil, "rmtree", flaky)
        rc = cmd_land_compile(self._ns(paths, task_id, d, t))
        assert rc == 0
        # status still flipped despite cleanup failure.
        s = Status.from_json(paths.status_json(task_id).read_text())
        assert s.compile_phase == CompilePhase.COMPLETE


# ---------------------------------------------------------------------------
# abort
# ---------------------------------------------------------------------------

class TestCompileRetry:
    def _make_failed(self, scaffolded):
        """Drive a scaffolded workflow into compile_phase=failed."""
        from tigerharness.journal.compile_cli import cmd_compile_fail
        task_id, paths = scaffolded
        ns = argparse.Namespace(
            journal_dir=str(paths.root), task_id=task_id,
            reason="compile failed at critiquing: Akagi BLOCK",
        )
        rc = cmd_compile_fail(ns)
        assert rc == 0
        # Pre-seed compile/round-NN files so we can assert the wipe.
        cd = paths.task_dir(task_id) / "compile"
        cd.mkdir(parents=True, exist_ok=True)
        (cd / "round-01-draft.md").write_text("attempt-1 draft")
        (cd / "round-01-akagi.md").write_text("Akagi BLOCK")
        (cd / "transcript.md").write_text("Round 1: BLOCK")
        return task_id, paths

    def test_happy_path_resets_state_and_wipes_compile(
        self, scaffolded, capsys,
    ):
        from tigerharness.journal.compile_cli import cmd_compile_retry
        task_id, paths = self._make_failed(scaffolded)
        # Pre-flip sessions to a non-zero value to verify reset.
        s = Status.from_json(paths.status_json(task_id).read_text())
        s.sessions = 3
        paths.status_json(task_id).write_text(s.to_json())
        ns = argparse.Namespace(
            journal_dir=str(paths.root), task_id=task_id,
        )
        rc = cmd_compile_retry(ns)
        assert rc == 0
        s2 = Status.from_json(paths.status_json(task_id).read_text())
        assert s2.state == State.PENDING
        assert s2.compile_pending is True
        assert s2.compile_phase == CompilePhase.PENDING
        assert s2.sessions == 0
        assert s2.session_ref is None
        assert "Compile retry requested" in s2.next_action
        # compile/ wiped.
        assert not (paths.task_dir(task_id) / "compile").exists()
        out = capsys.readouterr().out
        assert "compile-retry:" in out

    def test_missing_compile_dir_is_ok(self, scaffolded, capsys):
        """If compile/ doesn't exist (failure was before any rounds
        written), the retry still succeeds."""
        from tigerharness.journal.compile_cli import (
            cmd_compile_fail, cmd_compile_retry,
        )
        task_id, paths = scaffolded
        # compile_fail without seeding compile/.
        cmd_compile_fail(argparse.Namespace(
            journal_dir=str(paths.root), task_id=task_id,
            reason="failed at tier1_pre: no fence in drafter output",
        ))
        assert not (paths.task_dir(task_id) / "compile").exists()
        ns = argparse.Namespace(
            journal_dir=str(paths.root), task_id=task_id,
        )
        rc = cmd_compile_retry(ns)
        assert rc == 0
        s = Status.from_json(paths.status_json(task_id).read_text())
        assert s.compile_phase == CompilePhase.PENDING

    def test_brief_and_playbook_snapshot_preserved(
        self, scaffolded,
    ):
        """The task's brief + playbook snapshot must survive a retry --
        a retry is a re-compile, not a re-scaffold."""
        from tigerharness.journal.compile_cli import cmd_compile_retry
        task_id, paths = self._make_failed(scaffolded)
        td = paths.task_dir(task_id)
        brief_before = (td / "task_brief.md").read_text()
        playbook_before = (td / "playbook_snapshot.md").read_text()
        cmd_compile_retry(argparse.Namespace(
            journal_dir=str(paths.root), task_id=task_id,
        ))
        assert (td / "task_brief.md").read_text() == brief_before
        assert (td / "playbook_snapshot.md").read_text() == playbook_before

    def test_progress_md_preserved(self, scaffolded):
        """progress.md is the human-readable log; surviving a retry."""
        from tigerharness.journal.compile_cli import cmd_compile_retry
        task_id, paths = self._make_failed(scaffolded)
        td = paths.task_dir(task_id)
        # Append a line to progress.md to verify it survives.
        (td / "progress.md").write_text("# progress\n\nattempt 1 failed\n")
        cmd_compile_retry(argparse.Namespace(
            journal_dir=str(paths.root), task_id=task_id,
        ))
        assert "attempt 1 failed" in (td / "progress.md").read_text()

    def test_rejects_non_failed_phase(self, scaffolded, capsys):
        """A task in compile_phase=pending (i.e. never tried) cannot
        be retried -- there's nothing to retry. Same for complete."""
        from tigerharness.journal.compile_cli import cmd_compile_retry
        task_id, paths = scaffolded
        ns = argparse.Namespace(
            journal_dir=str(paths.root), task_id=task_id,
        )
        rc = cmd_compile_retry(ns)
        assert rc == 1
        err = capsys.readouterr().err
        assert "not in compile_phase=failed" in err
        assert "currently pending" in err

    def test_unknown_task_returns_1(self, journal_dir, capsys):
        from tigerharness.journal.compile_cli import cmd_compile_retry
        ns = argparse.Namespace(
            journal_dir=str(journal_dir),
            task_id="2026-05-30-nope-mitsui-abc12",
        )
        rc = cmd_compile_retry(ns)
        assert rc == 1
        assert "no active workflow task" in capsys.readouterr().err

    def test_updated_at_refreshed(self, scaffolded):
        from tigerharness.journal.compile_cli import cmd_compile_retry
        task_id, paths = self._make_failed(scaffolded)
        before = Status.from_json(paths.status_json(task_id).read_text())
        # Force updated_at backward so we can detect the refresh.
        before.updated_at = "2020-01-01T00:00:00Z"
        paths.status_json(task_id).write_text(before.to_json())
        cmd_compile_retry(argparse.Namespace(
            journal_dir=str(paths.root), task_id=task_id,
        ))
        after = Status.from_json(paths.status_json(task_id).read_text())
        assert after.updated_at > before.updated_at


class TestCompileFail:
    def test_happy(self, scaffolded, capsys):
        from tigerharness.journal.compile_cli import cmd_compile_fail
        task_id, paths = scaffolded
        ns = argparse.Namespace(
            journal_dir=str(paths.root), task_id=task_id,
            reason="compile failed at critiquing: Akagi BLOCK -- bad fanout",
        )
        rc = cmd_compile_fail(ns)
        assert rc == 0
        # Task is STILL in active/ (NOT archived).
        assert paths.status_json(task_id).exists()
        s = Status.from_json(paths.status_json(task_id).read_text())
        assert s.state == State.BLOCKED
        assert s.compile_phase == CompilePhase.FAILED
        assert "Akagi BLOCK" in s.next_action
        out = capsys.readouterr().out
        assert "compile-failed:" in out
        assert "state=blocked, compile_phase=failed" in out
        assert "journal abort" in out

    def test_unknown_task_returns_1(self, journal_dir, capsys):
        from tigerharness.journal.compile_cli import cmd_compile_fail
        ns = argparse.Namespace(
            journal_dir=str(journal_dir),
            task_id="2026-05-30-nope-mitsui-abc12",
            reason="x",
        )
        rc = cmd_compile_fail(ns)
        assert rc == 1
        assert "no active workflow task" in capsys.readouterr().err

    def test_already_done_returns_1(self, scaffolded, capsys):
        from tigerharness.journal.compile_cli import cmd_compile_fail
        task_id, paths = scaffolded
        s = Status.from_json(paths.status_json(task_id).read_text())
        s.state = State.DONE
        paths.status_json(task_id).write_text(s.to_json())
        ns = argparse.Namespace(
            journal_dir=str(paths.root), task_id=task_id, reason="x",
        )
        rc = cmd_compile_fail(ns)
        assert rc == 1
        assert "cannot mark compile-failed" in capsys.readouterr().err


class TestAbort:
    def test_happy(self, scaffolded, capsys):
        task_id, paths = scaffolded
        ns = argparse.Namespace(
            journal_dir=str(paths.root), task_id=task_id,
        )
        rc = cmd_abort(ns)
        assert rc == 0
        # Task moved out of active/.
        assert not paths.status_json(task_id).exists()
        # Archived to done/.
        assert (paths.root / "done" / task_id / "status.json").is_file()
        out = capsys.readouterr().out
        assert f"aborted + archived: {task_id}" in out

    def test_unknown_task_returns_1(self, journal_dir, capsys):
        ns = argparse.Namespace(
            journal_dir=str(journal_dir),
            task_id="2026-05-30-nope-mitsui-abc12",
        )
        rc = cmd_abort(ns)
        assert rc == 1
        assert "no active workflow task" in capsys.readouterr().err

    def test_already_done_returns_1(self, scaffolded, capsys):
        task_id, paths = scaffolded
        # Flip status to DONE manually -- by hand to keep within active/.
        s = Status.from_json(paths.status_json(task_id).read_text())
        s.state = State.DONE
        paths.status_json(task_id).write_text(s.to_json())
        ns = argparse.Namespace(
            journal_dir=str(paths.root), task_id=task_id,
        )
        rc = cmd_abort(ns)
        assert rc == 1
        assert "already done" in capsys.readouterr().err

    def test_postmortem_message_in_status(self, scaffolded, capsys):
        """status.next_action records phase and the abort cause before
        archive happens."""
        task_id, paths = scaffolded
        ns = argparse.Namespace(
            journal_dir=str(paths.root), task_id=task_id,
        )
        rc = cmd_abort(ns)
        assert rc == 0
        archived = paths.root / "done" / task_id / "status.json"
        s = Status.from_json(archived.read_text())
        assert "Aborted by" in s.next_action
        assert "compile_phase=pending" in s.next_action
        assert s.state == State.DONE

    def test_archive_failure_returns_1(
        self, scaffolded, monkeypatch, capsys,
    ):
        task_id, paths = scaffolded
        import tigerharness.journal.compile_cli as mod
        from tigerharness.journal.paths import JournalPathError
        monkeypatch.setattr(
            mod.JournalPaths, "archive",
            lambda self, tid: (_ for _ in ()).throw(
                JournalPathError("simulated"),
            ),
        )
        ns = argparse.Namespace(
            journal_dir=str(paths.root), task_id=task_id,
        )
        rc = cmd_abort(ns)
        assert rc == 1
        assert "archive failed" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# validate-personas
# ---------------------------------------------------------------------------

class TestValidatePersonas:
    def test_happy(self, team_root, capsys):
        ns = argparse.Namespace(team="Shohoku")
        rc = cmd_validate_personas(ns)
        assert rc == 0
        out = capsys.readouterr().out
        assert "ok:" in out

    def test_missing_team(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("TIGERHARNESS_TEAMS_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        ns = argparse.Namespace(team="DoesNotExist")
        rc = cmd_validate_personas(ns)
        assert rc == 1
        assert "team root not found" in capsys.readouterr().err

    def test_missing_persona(self, tmp_path, monkeypatch, capsys):
        # Build a team WITHOUT one of the compile personas.
        partial = list(COMPILE_PERSONAS)
        partial.remove("Akagi")
        team = _make_team(tmp_path, personas=partial)
        monkeypatch.chdir(team)
        ns = argparse.Namespace(team="Shohoku")
        rc = cmd_validate_personas(ns)
        assert rc == 1
        err = capsys.readouterr().err
        assert "missing prompt.md" in err
        assert "Akagi" in err


# ---------------------------------------------------------------------------
# build_subparsers wiring
# ---------------------------------------------------------------------------

class TestBuildSubparsers:
    def test_registers_all_eight(self):
        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest="cmd")
        build_subparsers(sub)
        names = sorted(sub.choices.keys())
        assert names == sorted([
            "compile-context", "compile-prompts", "validate-graph",
            "land-compile", "compile-fail", "compile-retry", "abort",
            "validate-personas",
        ])

    def test_each_sets_func(self):
        from tigerharness.journal.compile_cli import (
            cmd_compile_fail, cmd_compile_retry,
        )
        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest="cmd")
        build_subparsers(sub)
        for name, expected in [
            ("compile-context", cmd_compile_context),
            ("compile-prompts", cmd_compile_prompts),
            ("validate-graph", cmd_validate_graph),
            ("land-compile", cmd_land_compile),
            ("compile-fail", cmd_compile_fail),
            ("compile-retry", cmd_compile_retry),
            ("abort", cmd_abort),
            ("validate-personas", cmd_validate_personas),
        ]:
            assert sub.choices[name].get_default("func") is expected

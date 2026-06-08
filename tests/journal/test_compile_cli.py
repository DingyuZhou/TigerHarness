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

from tigerharness.journal import compile_cli, worklog
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
        out = capsys.readouterr().out
        assert "(roster unresolved" in out
        # Even without a team root, the compile-personas mapping is
        # surfaced -- defaults to Anzai/Akagi/Ayako.
        assert "## Compile personas (role -> name)" in out
        assert "drafter: Anzai" in out

    def test_prints_team_overridden_compile_personas(
        self, scaffolded, tmp_path, monkeypatch, capsys,
    ):
        """Phase 2: when the team's workflow.yaml overrides the role ->
        persona mapping, compile-context surfaces the OVERRIDE so the
        session knows which persona to adopt."""
        task_id, paths = scaffolded
        # `team_root` fixture chdir'd into a Shohoku team root that has
        # Anzai/Akagi/Ayako on disk. Drop a workflow.yaml to override.
        team_root_dir = Path.cwd()
        (team_root_dir / "configs" / "workflow.yaml").write_text(
            "compile_personas:\n"
            "  drafter: Mitsui\n"  # use an existing roster persona
        )
        ns = argparse.Namespace(
            journal_dir=str(paths.root), task_id=task_id,
        )
        rc = cmd_compile_context(ns)
        assert rc == 0
        out = capsys.readouterr().out
        assert "## Compile personas (role -> name)" in out
        assert "drafter: Mitsui" in out
        assert "akagi: Akagi" in out  # still default

    def test_prints_playbook_name(self, scaffolded, capsys):
        """Phase 2: the playbook name shows up in the Task section."""
        task_id, paths = scaffolded
        ns = argparse.Namespace(
            journal_dir=str(paths.root), task_id=task_id,
        )
        rc = cmd_compile_context(ns)
        assert rc == 0
        assert "playbook: default" in capsys.readouterr().out


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

    def test_refreshes_heartbeat(self, scaffolded, tmp_path):
        """Final-review fix: land-compile must refresh updated_at on
        the same atomic write that flips compile_pending/compile_phase
        so the sweep doesn't reclassify a freshly-landed task as
        stale. Matches the sibling CLIs (append-steps, compile-fail,
        compile-retry)."""
        task_id, paths = scaffolded
        # Force updated_at backward.
        before = Status.from_json(paths.status_json(task_id).read_text())
        before.updated_at = "2020-01-01T00:00:00Z"
        paths.status_json(task_id).write_text(before.to_json())
        d = tmp_path / "draft.md"
        d.write_text(_VALID_BUNDLE)
        t = tmp_path / "transcript.md"
        t.write_text("Round 1: APPROVE.\n")
        cmd_land_compile(self._ns(paths, task_id, d, t))
        after = Status.from_json(paths.status_json(task_id).read_text())
        assert after.updated_at > before.updated_at

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
# Phase 1d: compile worklog normalisation
# ---------------------------------------------------------------------------

class TestExtractVerdict:
    """``_extract_verdict`` pulls the WORKFLOW: trailer best-effort."""

    def test_none_when_absent(self):
        assert compile_cli._extract_verdict("just prose\n") is None

    def test_last_match_wins(self):
        body = "WORKFLOW: REVISE -- a\nmore text\nWORKFLOW: APPROVE\n"
        assert compile_cli._extract_verdict(body) == "APPROVE"

    def test_block(self):
        assert compile_cli._extract_verdict(
            "verdict below\nWORKFLOW: BLOCK -- unsalvageable\n"
        ) == "BLOCK"


class TestEmitCompileWorklog:
    """``_emit_compile_worklog`` turns compile/round-NN-<token>.md files
    into persona-attributed worklog entries (Phase 1d)."""

    _PERSONAS = {"drafter": "Anzai", "akagi": "Akagi", "ayako": "Ayako"}

    def _status(self, paths, task_id):
        return Status.from_json(paths.status_json(task_id).read_text())

    def _round(self, paths, task_id, name, text):
        cd = _compile_dir(paths, task_id)
        cd.mkdir(parents=True, exist_ok=True)
        (cd / name).write_text(text, encoding="utf-8")
        return cd

    def test_no_compile_dir_returns_zero(self, scaffolded):
        task_id, paths = scaffolded
        cd = _compile_dir(paths, task_id)
        if cd.exists():
            import shutil
            shutil.rmtree(cd)
        n = compile_cli._emit_compile_worklog(
            paths, self._status(paths, task_id), self._PERSONAS,
        )
        assert n == 0

    def test_writes_one_entry_per_round_file(self, scaffolded):
        task_id, paths = scaffolded
        self._round(paths, task_id, "round-01-draft.md",
                    "draft body\nWORKFLOW: APPROVE\n")
        self._round(paths, task_id, "round-01-akagi.md",
                    "akagi review\nWORKFLOW: REVISE -- fix it\n")
        self._round(paths, task_id, "round-01-ayako.md",
                    "ayako review\nWORKFLOW: APPROVE\n")
        n = compile_cli._emit_compile_worklog(
            paths, self._status(paths, task_id), self._PERSONAS,
        )
        assert n == 3
        entries = worklog.list_entries(paths, task_id)
        # Ordered draft -> akagi -> ayako within the round; attribution
        # + verdict stamped from the mapping and the trailer.
        assert [
            (e.persona, e.role, e.step, e.verdict, e.kind) for e in entries
        ] == [
            ("Anzai", "drafter", "compile-draft", "APPROVE", "workflow"),
            ("Akagi", "akagi", "compile-akagi", "REVISE", "workflow"),
            ("Ayako", "ayako", "compile-ayako", "APPROVE", "workflow"),
        ]
        # Body preserved verbatim; objective carries the round number.
        assert "draft body" in entries[0].body
        assert entries[0].objective == "Compile round 01 drafter turn"
        assert entries[0].ended_at  # land-time stamp present

    def test_multiple_rounds_ordered_by_round_then_turn(self, scaffolded):
        task_id, paths = scaffolded
        # Write out of order to prove the deterministic sort.
        self._round(paths, task_id, "round-02-draft.md", "r2 draft")
        self._round(paths, task_id, "round-01-ayako.md", "r1 ayako")
        self._round(paths, task_id, "round-01-draft.md", "r1 draft")
        n = compile_cli._emit_compile_worklog(
            paths, self._status(paths, task_id), self._PERSONAS,
        )
        assert n == 3
        entries = worklog.list_entries(paths, task_id)
        assert [e.objective for e in entries] == [
            "Compile round 01 drafter turn",
            "Compile round 01 ayako turn",
            "Compile round 02 drafter turn",
        ]

    def test_ignores_non_round_files_and_dirs(self, scaffolded):
        task_id, paths = scaffolded
        cd = self._round(paths, task_id, "round-01-draft.md", "draft")
        (cd / "transcript.md").write_text("full transcript")
        (cd / "round-01.json").write_text("{}")
        (cd / "final").mkdir()  # a directory -- must be skipped
        n = compile_cli._emit_compile_worklog(
            paths, self._status(paths, task_id), self._PERSONAS,
        )
        assert n == 1

    def test_unmapped_role_skipped(self, scaffolded, capsys):
        task_id, paths = scaffolded
        self._round(paths, task_id, "round-01-akagi.md", "akagi review")
        n = compile_cli._emit_compile_worklog(
            paths, self._status(paths, task_id), {"drafter": "Anzai"},
        )
        assert n == 0
        assert "no compile persona mapped for role 'akagi'" \
            in capsys.readouterr().err

    def test_unreadable_round_file_skipped(
        self, scaffolded, monkeypatch, capsys,
    ):
        task_id, paths = scaffolded
        self._round(paths, task_id, "round-01-draft.md", "draft")
        status = self._status(paths, task_id)  # capture before patch
        real_read = Path.read_text

        def boom(self, *a, **k):
            if self.name == "round-01-draft.md":
                raise OSError("nope")
            return real_read(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", boom)
        n = compile_cli._emit_compile_worklog(paths, status, self._PERSONAS)
        assert n == 0
        assert "cannot read compile round file" in capsys.readouterr().err

    def test_write_failure_skipped(self, scaffolded, monkeypatch, capsys):
        task_id, paths = scaffolded
        self._round(paths, task_id, "round-01-draft.md", "draft")
        status = self._status(paths, task_id)

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(compile_cli.worklog, "write_entry", boom)
        n = compile_cli._emit_compile_worklog(paths, status, self._PERSONAS)
        assert n == 0
        assert "failed to write worklog entry" in capsys.readouterr().err


class TestLandCompileWorklog:
    """Phase 1d integration: land-compile emits the compile worklog."""

    def test_land_emits_drafter_worklog(self, scaffolded, tmp_path, capsys):
        task_id, paths = scaffolded
        # Seed a round file as the in-session compile loop would have.
        cd = _compile_dir(paths, task_id)
        cd.mkdir(parents=True, exist_ok=True)
        (cd / "round-01-draft.md").write_text(
            _VALID_BUNDLE + "\nWORKFLOW: APPROVE\n", encoding="utf-8")
        d = tmp_path / "draft.md"
        d.write_text(_VALID_BUNDLE)
        t = tmp_path / "transcript.md"
        t.write_text("Round 1: APPROVE.\n")
        rc = cmd_land_compile(argparse.Namespace(
            journal_dir=str(paths.root), task=task_id,
            draft=str(d), transcript=str(t), rounds=1,
        ))
        assert rc == 0
        entries = worklog.list_entries(paths, task_id)
        assert [(e.persona, e.step) for e in entries] == [
            ("Anzai", "compile-draft"),
        ]
        assert "worklog entries: 1" in capsys.readouterr().out

    def test_land_worklog_failure_does_not_break_landing(
        self, scaffolded, tmp_path, monkeypatch, capsys,
    ):
        task_id, paths = scaffolded
        d = tmp_path / "draft.md"
        d.write_text(_VALID_BUNDLE)
        t = tmp_path / "transcript.md"
        t.write_text("x")

        def boom(*a, **k):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(compile_cli, "_emit_compile_worklog", boom)
        rc = cmd_land_compile(argparse.Namespace(
            journal_dir=str(paths.root), task=task_id,
            draft=str(d), transcript=str(t), rounds=1,
        ))
        assert rc == 0
        s = Status.from_json(paths.status_json(task_id).read_text())
        assert s.compile_phase == CompilePhase.COMPLETE
        assert "compile worklog normalisation failed" \
            in capsys.readouterr().err


# ---------------------------------------------------------------------------
# abort
# ---------------------------------------------------------------------------

class TestAppendSteps:
    """Phase 3: `journal append-steps` extends a landed graph at runtime."""

    def _landed(self, team_root, journal_dir):
        """Drive a workflow task to compile_phase=complete and return
        (task_id, paths)."""
        from tigerharness.journal.scaffold import new_workflow_task
        paths = JournalPaths(root=journal_dir)
        result = new_workflow_task(
            brief_text="# Goal\nShip.\n",
            playbook_text=(
                "# Playbook\n\nAnzai drafts. Mitsui implements.\n"
            ),
            playbook_name="default",
            team_root=team_root,
            paths=paths,
            captain="Mitsui",
        )
        task_id = result.task_id
        # Land a minimal compile by calling cmd_land_compile directly
        # with a valid bundle, the same way the scripted driver does.
        bundle = (
            "```steps-bundle\n"
            "## step: 01-anzai-plan\n"
            "---\n"
            "id: 01-anzai-plan\n"
            "persona: Anzai\n"
            "role: planner\n"
            "on_approve: 02-mitsui-impl\n"
            "on_revise: 01-anzai-plan\n"
            "on_block: __escalate__\n"
            "max_iters: 5\n"
            "timeout_sec: 1800\n"
            "parallel_with: []\n"
            "---\n"
            "Plan.\n"
            "## step: 02-mitsui-impl\n"
            "---\n"
            "id: 02-mitsui-impl\n"
            "persona: Mitsui\n"
            "role: developer\n"
            "on_approve: __done__\n"
            "on_revise: 02-mitsui-impl\n"
            "on_block: __escalate__\n"
            "max_iters: 5\n"
            "timeout_sec: 1800\n"
            "parallel_with: []\n"
            "---\n"
            "Implement.\n"
            "```\n"
        )
        td = paths.task_dir(task_id)
        # Write transcript + draft as files for cmd_land_compile.
        compile_dir = td / "compile"
        compile_dir.mkdir(parents=True, exist_ok=True)
        draft_path = compile_dir / "round-01-draft.md"
        draft_path.write_text(bundle, encoding="utf-8")
        transcript_path = compile_dir / "transcript.md"
        transcript_path.write_text("Round 1: APPROVE\n", encoding="utf-8")
        rc = cmd_land_compile(argparse.Namespace(
            journal_dir=str(paths.root),
            task=task_id,
            draft=str(draft_path),
            transcript=str(transcript_path),
            rounds=1,
        ))
        assert rc == 0
        return task_id, paths

    def _append_bundle(
        self, after_id: str = "02-mitsui-impl",
        new_id: str = "03-mitsui-qa",
    ) -> str:
        """A drafter-format bundle with one new step that the existing
        graph references via on_approve (we'll patch the existing
        on_approve in some tests, but the validator doesn't enforce
        any specific link)."""
        return (
            f"```steps-bundle\n"
            f"## step: {new_id}\n"
            f"---\n"
            f"id: {new_id}\n"
            f"persona: Mitsui\n"
            f"role: qa\n"
            f"on_approve: __done__\n"
            f"on_revise: {new_id}\n"
            f"on_block: __escalate__\n"
            f"max_iters: 5\n"
            f"timeout_sec: 1800\n"
            f"parallel_with: []\n"
            f"---\n"
            f"QA the implementation.\n"
            f"```\n"
        )

    def test_happy_path_appends_new_step(
        self, team_root, journal_dir, tmp_path, capsys,
    ):
        from tigerharness.journal.compile_cli import cmd_append_steps
        task_id, paths = self._landed(team_root, journal_dir)
        bundle_path = tmp_path / "append.md"
        bundle_path.write_text(self._append_bundle())
        ns = argparse.Namespace(
            journal_dir=str(paths.root),
            task=task_id,
            new_bundle=str(bundle_path),
        )
        rc = cmd_append_steps(ns)
        assert rc == 0, capsys.readouterr().err
        # orchestration.json extended.
        orch = json.loads(
            (paths.task_dir(task_id) / "orchestration.json").read_text(),
        )
        assert orch["steps"] == [
            "01-anzai-plan", "02-mitsui-impl", "03-mitsui-qa",
        ]
        assert "03-mitsui-qa" in orch["edges"]
        # New step file written.
        assert (paths.task_dir(task_id) / "steps" / "03-mitsui-qa.md").is_file()
        # Old step files untouched.
        assert (paths.task_dir(task_id) / "steps" / "01-anzai-plan.md").is_file()
        assert (paths.task_dir(task_id) / "steps" / "02-mitsui-impl.md").is_file()
        out = capsys.readouterr().out
        assert "appended: 1 step(s)" in out
        assert "03-mitsui-qa" in out

    def test_appends_multiple_steps(
        self, team_root, journal_dir, tmp_path,
    ):
        from tigerharness.journal.compile_cli import cmd_append_steps
        task_id, paths = self._landed(team_root, journal_dir)
        bundle = (
            "```steps-bundle\n"
            "## step: 03-mitsui-qa\n"
            "---\n"
            "id: 03-mitsui-qa\n"
            "persona: Mitsui\n"
            "role: qa\n"
            "on_approve: 04-mitsui-ship\n"
            "on_revise: 03-mitsui-qa\n"
            "on_block: __escalate__\n"
            "max_iters: 5\n"
            "timeout_sec: 1800\n"
            "parallel_with: []\n"
            "---\n"
            "QA.\n"
            "## step: 04-mitsui-ship\n"
            "---\n"
            "id: 04-mitsui-ship\n"
            "persona: Mitsui\n"
            "role: shipper\n"
            "on_approve: __done__\n"
            "on_revise: 04-mitsui-ship\n"
            "on_block: __escalate__\n"
            "max_iters: 5\n"
            "timeout_sec: 1800\n"
            "parallel_with: []\n"
            "---\n"
            "Ship.\n"
            "```\n"
        )
        bundle_path = tmp_path / "append.md"
        bundle_path.write_text(bundle)
        rc = cmd_append_steps(argparse.Namespace(
            journal_dir=str(paths.root),
            task=task_id,
            new_bundle=str(bundle_path),
        ))
        assert rc == 0
        orch = json.loads(
            (paths.task_dir(task_id) / "orchestration.json").read_text(),
        )
        assert orch["steps"][-2:] == ["03-mitsui-qa", "04-mitsui-ship"]

    def test_refuses_on_non_complete_phase(
        self, team_root, journal_dir, tmp_path, capsys,
    ):
        """A task in compile_phase=pending (graph not landed yet) can't
        be append-stepped -- there's no graph to extend."""
        from tigerharness.journal.scaffold import new_workflow_task
        from tigerharness.journal.compile_cli import cmd_append_steps
        paths = JournalPaths(root=journal_dir)
        r = new_workflow_task(
            brief_text="b\n", playbook_text="p\n",
            playbook_name="default", team_root=team_root, paths=paths,
        )
        bundle_path = tmp_path / "append.md"
        bundle_path.write_text(self._append_bundle())
        rc = cmd_append_steps(argparse.Namespace(
            journal_dir=str(paths.root),
            task=r.task_id, new_bundle=str(bundle_path),
        ))
        assert rc == 2
        assert "only operates on a completed compile" in \
            capsys.readouterr().err

    def test_rejects_id_collision_with_existing_step(
        self, team_root, journal_dir, tmp_path, capsys,
    ):
        from tigerharness.journal.compile_cli import cmd_append_steps
        task_id, paths = self._landed(team_root, journal_dir)
        # Bundle uses the SAME id as an existing step.
        bundle_path = tmp_path / "append.md"
        bundle_path.write_text(
            self._append_bundle(new_id="02-mitsui-impl"),
        )
        rc = cmd_append_steps(argparse.Namespace(
            journal_dir=str(paths.root),
            task=task_id, new_bundle=str(bundle_path),
        ))
        assert rc == 1
        err = capsys.readouterr().err
        assert "collide" in err
        assert "02-mitsui-impl" in err
        # Orchestration NOT modified.
        orch = json.loads(
            (paths.task_dir(task_id) / "orchestration.json").read_text(),
        )
        assert orch["steps"] == ["01-anzai-plan", "02-mitsui-impl"]

    def test_unparseable_bundle_returns_json_envelope(
        self, team_root, journal_dir, tmp_path, capsys,
    ):
        from tigerharness.journal.compile_cli import cmd_append_steps
        task_id, paths = self._landed(team_root, journal_dir)
        capsys.readouterr()  # drain fixture's "landed:" stdout
        bundle_path = tmp_path / "append.md"
        bundle_path.write_text("no fence here\n")
        rc = cmd_append_steps(argparse.Namespace(
            journal_dir=str(paths.root),
            task=task_id, new_bundle=str(bundle_path),
        ))
        assert rc == 1
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["ok"] is False
        assert envelope["errors"][0]["validator"] == "parse"

    def test_validator_failure_leaves_orchestration_untouched(
        self, team_root, journal_dir, tmp_path, capsys,
    ):
        """A new step that names a non-roster persona fails the
        roster validator. The orchestration MUST be left unchanged."""
        from tigerharness.journal.compile_cli import cmd_append_steps
        task_id, paths = self._landed(team_root, journal_dir)
        capsys.readouterr()  # drain fixture's "landed:" stdout
        bad_bundle = self._append_bundle().replace(
            "persona: Mitsui", "persona: NotInRoster",
        )
        bundle_path = tmp_path / "append.md"
        bundle_path.write_text(bad_bundle)
        rc = cmd_append_steps(argparse.Namespace(
            journal_dir=str(paths.root),
            task=task_id, new_bundle=str(bundle_path),
        ))
        assert rc == 1
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["ok"] is False
        # Orchestration NOT modified.
        orch = json.loads(
            (paths.task_dir(task_id) / "orchestration.json").read_text(),
        )
        assert orch["steps"] == ["01-anzai-plan", "02-mitsui-impl"]
        # New step file NOT written.
        assert not (paths.task_dir(task_id) / "steps"
                    / "03-mitsui-qa.md").exists()

    def test_unreadable_bundle_returns_2(
        self, team_root, journal_dir, capsys,
    ):
        from tigerharness.journal.compile_cli import cmd_append_steps
        task_id, paths = self._landed(team_root, journal_dir)
        rc = cmd_append_steps(argparse.Namespace(
            journal_dir=str(paths.root),
            task=task_id,
            new_bundle="/no/such/file.md",
        ))
        assert rc == 2
        assert "cannot read new-bundle" in capsys.readouterr().err

    def test_zero_steps_returns_2(
        self, team_root, journal_dir, tmp_path, capsys, monkeypatch,
    ):
        """Defense-in-depth: if the bundle parser somehow returns zero
        steps (it currently raises instead, but the parser API could
        change), cmd_append_steps surfaces a clear error. We mock the
        parser to return an empty list to exercise this branch."""
        from tigerharness.journal.compile_cli import cmd_append_steps
        task_id, paths = self._landed(team_root, journal_dir)
        capsys.readouterr()
        bundle_path = tmp_path / "bundle.md"
        bundle_path.write_text("```steps-bundle\n```\n")
        import tigerharness.workflow_runner.compile.drafter as drafter_mod
        monkeypatch.setattr(drafter_mod, "_parse_response", lambda _: [])
        rc = cmd_append_steps(argparse.Namespace(
            journal_dir=str(paths.root),
            task=task_id, new_bundle=str(bundle_path),
        ))
        assert rc == 2
        assert "zero steps" in capsys.readouterr().err

    def test_unknown_task_returns_2(self, journal_dir, tmp_path, capsys):
        from tigerharness.journal.compile_cli import cmd_append_steps
        bundle_path = tmp_path / "append.md"
        bundle_path.write_text(self._append_bundle())
        rc = cmd_append_steps(argparse.Namespace(
            journal_dir=str(journal_dir),
            task="2026-05-30-nope-mitsui-abc12",
            new_bundle=str(bundle_path),
        ))
        assert rc == 2
        assert "no active workflow task" in capsys.readouterr().err

    def test_orphan_on_second_step_rolls_back_first(
        self, team_root, journal_dir, tmp_path, capsys,
    ):
        """If the orphan-block check fires after one or more step
        files have already been written, the rollback undoes the
        earlier writes."""
        from tigerharness.journal.compile_cli import cmd_append_steps
        task_id, paths = self._landed(team_root, journal_dir)
        capsys.readouterr()
        # Pre-seed an orphan for the SECOND step in the bundle.
        orphan = paths.task_dir(task_id) / "steps" / "04-b.md"
        orphan.write_text("stray\n")
        bundle = (
            "```steps-bundle\n"
            "## step: 03-a\n"
            "---\n"
            "id: 03-a\npersona: Mitsui\nrole: a\n"
            "on_approve: __done__\non_revise: 03-a\non_block: __escalate__\n"
            "max_iters: 5\ntimeout_sec: 1800\nparallel_with: []\n"
            "---\nstep a\n"
            "## step: 04-b\n"
            "---\n"
            "id: 04-b\npersona: Mitsui\nrole: b\n"
            "on_approve: __done__\non_revise: 04-b\non_block: __escalate__\n"
            "max_iters: 5\ntimeout_sec: 1800\nparallel_with: []\n"
            "---\nstep b\n"
            "```\n"
        )
        bundle_path = tmp_path / "append.md"
        bundle_path.write_text(bundle)
        rc = cmd_append_steps(argparse.Namespace(
            journal_dir=str(paths.root),
            task=task_id, new_bundle=str(bundle_path),
        ))
        assert rc == 1
        # The first new step's file got rolled back. The orphan stays
        # (we only delete what THIS invocation wrote).
        td = paths.task_dir(task_id)
        assert not (td / "steps" / "03-a.md").exists()
        assert orphan.read_text() == "stray\n"

    def test_rollback_unlink_failure_is_swallowed(
        self, team_root, journal_dir, tmp_path, monkeypatch, capsys,
    ):
        """The rollback cleanup is best-effort. If unlink itself fails
        (e.g. perm error), we still surface the original error and
        return 1, without crashing on the cleanup."""
        from tigerharness.journal.compile_cli import cmd_append_steps
        task_id, paths = self._landed(team_root, journal_dir)
        capsys.readouterr()
        bundle = (
            "```steps-bundle\n"
            "## step: 03-a\n"
            "---\n"
            "id: 03-a\npersona: Mitsui\nrole: a\n"
            "on_approve: __done__\non_revise: 03-a\non_block: __escalate__\n"
            "max_iters: 5\ntimeout_sec: 1800\nparallel_with: []\n"
            "---\nstep a\n"
            "## step: 04-b\n"
            "---\n"
            "id: 04-b\npersona: Mitsui\nrole: b\n"
            "on_approve: __done__\non_revise: 04-b\non_block: __escalate__\n"
            "max_iters: 5\ntimeout_sec: 1800\nparallel_with: []\n"
            "---\nstep b\n"
            "```\n"
        )
        bundle_path = tmp_path / "append.md"
        bundle_path.write_text(bundle)
        from pathlib import Path as _Path
        real_write = _Path.write_text
        counter = {"n": 0}

        def flaky_write(self, *args, **kwargs):
            if self.name.endswith(".md") and "steps" in self.parts:
                counter["n"] += 1
                if counter["n"] == 2:
                    raise OSError("simulated disk full")
            return real_write(self, *args, **kwargs)

        real_unlink = _Path.unlink

        def flaky_unlink(self, *args, **kwargs):
            # First step's file (03-a.md) is what rollback tries to unlink.
            # Force unlink to fail to exercise the swallow branch.
            if self.name == "03-a.md":
                raise OSError("simulated unlink failure")
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(_Path, "write_text", flaky_write)
        monkeypatch.setattr(_Path, "unlink", flaky_unlink)
        rc = cmd_append_steps(argparse.Namespace(
            journal_dir=str(paths.root),
            task=task_id, new_bundle=str(bundle_path),
        ))
        assert rc == 1
        assert "failed writing new step files" in capsys.readouterr().err

    def test_orphan_rollback_unlink_failure_is_swallowed(
        self, team_root, journal_dir, tmp_path, monkeypatch, capsys,
    ):
        """Same as above, but trigger via the orphan-found branch."""
        from tigerharness.journal.compile_cli import cmd_append_steps
        task_id, paths = self._landed(team_root, journal_dir)
        capsys.readouterr()
        orphan = paths.task_dir(task_id) / "steps" / "04-b.md"
        orphan.write_text("stray\n")
        bundle = (
            "```steps-bundle\n"
            "## step: 03-a\n"
            "---\n"
            "id: 03-a\npersona: Mitsui\nrole: a\n"
            "on_approve: __done__\non_revise: 03-a\non_block: __escalate__\n"
            "max_iters: 5\ntimeout_sec: 1800\nparallel_with: []\n"
            "---\nstep a\n"
            "## step: 04-b\n"
            "---\n"
            "id: 04-b\npersona: Mitsui\nrole: b\n"
            "on_approve: __done__\non_revise: 04-b\non_block: __escalate__\n"
            "max_iters: 5\ntimeout_sec: 1800\nparallel_with: []\n"
            "---\nstep b\n"
            "```\n"
        )
        bundle_path = tmp_path / "append.md"
        bundle_path.write_text(bundle)
        from pathlib import Path as _Path
        real_unlink = _Path.unlink

        def flaky_unlink(self, *args, **kwargs):
            if self.name == "03-a.md":
                raise OSError("simulated unlink failure")
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(_Path, "unlink", flaky_unlink)
        rc = cmd_append_steps(argparse.Namespace(
            journal_dir=str(paths.root),
            task=task_id, new_bundle=str(bundle_path),
        ))
        assert rc == 1
        assert "refusing to overwrite" in capsys.readouterr().err

    def test_disk_full_mid_write_rolls_back_partial_files(
        self, team_root, journal_dir, tmp_path, monkeypatch, capsys,
    ):
        """If write_text fails partway through the new-step loop, the
        cleanup undoes any earlier writes from THIS invocation so a
        retry isn't blocked by stale stray files."""
        from tigerharness.journal.compile_cli import cmd_append_steps
        task_id, paths = self._landed(team_root, journal_dir)
        capsys.readouterr()
        # Bundle with two new steps.
        bundle_path = tmp_path / "append.md"
        bundle_path.write_text(
            "```steps-bundle\n"
            "## step: 03-a\n"
            "---\n"
            "id: 03-a\npersona: Mitsui\nrole: a\n"
            "on_approve: __done__\non_revise: 03-a\non_block: __escalate__\n"
            "max_iters: 5\ntimeout_sec: 1800\nparallel_with: []\n"
            "---\n"
            "step a\n"
            "## step: 04-b\n"
            "---\n"
            "id: 04-b\npersona: Mitsui\nrole: b\n"
            "on_approve: __done__\non_revise: 04-b\non_block: __escalate__\n"
            "max_iters: 5\ntimeout_sec: 1800\nparallel_with: []\n"
            "---\n"
            "step b\n"
            "```\n"
        )
        # Fail on the SECOND write_text via a counter-based patch.
        from pathlib import Path as _Path
        real_write = _Path.write_text
        counter = {"n": 0}

        def flaky_write(self, *args, **kwargs):
            if self.name.endswith(".md") and "steps" in self.parts:
                counter["n"] += 1
                if counter["n"] == 2:
                    raise OSError("simulated disk full")
            return real_write(self, *args, **kwargs)

        monkeypatch.setattr(_Path, "write_text", flaky_write)
        rc = cmd_append_steps(argparse.Namespace(
            journal_dir=str(paths.root),
            task=task_id, new_bundle=str(bundle_path),
        ))
        assert rc == 1
        assert "failed writing new step files" in capsys.readouterr().err
        # The first step's file was rolled back, so retry isn't blocked.
        td = paths.task_dir(task_id)
        assert not (td / "steps" / "03-a.md").exists()
        assert not (td / "steps" / "04-b.md").exists()
        # orchestration.json NOT modified.
        orch = json.loads((td / "orchestration.json").read_text())
        assert orch["steps"] == ["01-anzai-plan", "02-mitsui-impl"]

    def test_stray_orphan_step_file_blocks_overwrite(
        self, team_root, journal_dir, tmp_path, capsys,
    ):
        """If a stray steps/<new-id>.md exists on disk that the
        orchestration doesn't reference (e.g. a manual edit), refuse
        to clobber it -- the existing-id collision check only screens
        ids in orchestration.json, so the file-overwrite check is the
        defense-in-depth."""
        from tigerharness.journal.compile_cli import cmd_append_steps
        task_id, paths = self._landed(team_root, journal_dir)
        # Plant an orphan that orchestration.json does NOT reference.
        orphan = paths.task_dir(task_id) / "steps" / "03-mitsui-qa.md"
        orphan.write_text("stray content\n")
        bundle_path = tmp_path / "append.md"
        bundle_path.write_text(self._append_bundle())
        rc = cmd_append_steps(argparse.Namespace(
            journal_dir=str(paths.root),
            task=task_id, new_bundle=str(bundle_path),
        ))
        assert rc == 1
        assert "refusing to overwrite" in capsys.readouterr().err
        # Orphan preserved (no destruction).
        assert orphan.read_text() == "stray content\n"
        # Orchestration NOT extended.
        orch = json.loads(
            (paths.task_dir(task_id) / "orchestration.json").read_text(),
        )
        assert "03-mitsui-qa" not in orch["steps"]

    def test_refreshes_heartbeat(
        self, team_root, journal_dir, tmp_path,
    ):
        """append-steps bumps updated_at so the sweep doesn't reclassify
        the task as stale while the operator works it."""
        from tigerharness.journal.compile_cli import cmd_append_steps
        task_id, paths = self._landed(team_root, journal_dir)
        before = Status.from_json(paths.status_json(task_id).read_text())
        before.updated_at = "2020-01-01T00:00:00Z"
        paths.status_json(task_id).write_text(before.to_json())
        bundle_path = tmp_path / "append.md"
        bundle_path.write_text(self._append_bundle())
        cmd_append_steps(argparse.Namespace(
            journal_dir=str(paths.root),
            task=task_id, new_bundle=str(bundle_path),
        ))
        after = Status.from_json(paths.status_json(task_id).read_text())
        assert after.updated_at > before.updated_at

    def test_corrupt_orchestration_returns_2(
        self, team_root, journal_dir, tmp_path, capsys,
    ):
        from tigerharness.journal.compile_cli import cmd_append_steps
        task_id, paths = self._landed(team_root, journal_dir)
        (paths.task_dir(task_id) / "orchestration.json").write_text("{not json")
        bundle_path = tmp_path / "append.md"
        bundle_path.write_text(self._append_bundle())
        rc = cmd_append_steps(argparse.Namespace(
            journal_dir=str(paths.root),
            task=task_id, new_bundle=str(bundle_path),
        ))
        assert rc == 2
        assert "cannot read orchestration.json" in capsys.readouterr().err

    def test_missing_step_file_returns_2(
        self, team_root, journal_dir, tmp_path, capsys,
    ):
        """If a step file referenced by orchestration.json is missing
        (e.g. someone deleted it), rehydration fails and append-steps
        refuses to operate."""
        from tigerharness.journal.compile_cli import cmd_append_steps
        task_id, paths = self._landed(team_root, journal_dir)
        (paths.task_dir(task_id) / "steps" / "01-anzai-plan.md").unlink()
        bundle_path = tmp_path / "append.md"
        bundle_path.write_text(self._append_bundle())
        rc = cmd_append_steps(argparse.Namespace(
            journal_dir=str(paths.root),
            task=task_id, new_bundle=str(bundle_path),
        ))
        assert rc == 2
        assert "rehydrate existing steps" in capsys.readouterr().err


class TestReadExistingStep:
    """The frontmatter rehydrator helper."""

    def test_round_trips_a_rendered_step(self, tmp_path):
        from tigerharness.journal.compile_cli import (
            _read_existing_step, _render_frontmatter,
        )
        from tigerharness.workflow_runner.models import StepFrontmatter

        s = StepFrontmatter(
            id="x-1", persona="Anzai", role="planner",
            on_approve="x-2", on_revise="x-1", on_block="__escalate__",
            max_iters=5, timeout_sec=1800, parallel_with=[],
        )
        td = tmp_path / "task"
        (td / "steps").mkdir(parents=True)
        (td / "steps" / "x-1.md").write_text(
            f"---\n{_render_frontmatter(s)}---\n",
        )
        loaded = _read_existing_step(td, "x-1")
        assert loaded.id == s.id
        assert loaded.persona == s.persona
        assert loaded.on_approve == s.on_approve

    def test_missing_leading_delimiter_raises(self, tmp_path):
        from tigerharness.journal.compile_cli import _read_existing_step
        td = tmp_path / "task"
        (td / "steps").mkdir(parents=True)
        (td / "steps" / "x-1.md").write_text("no delimiter here\n")
        with pytest.raises(ValueError) as exc:
            _read_existing_step(td, "x-1")
        assert "no leading --- delimiter" in str(exc.value)

    def test_missing_trailing_delimiter_raises(self, tmp_path):
        from tigerharness.journal.compile_cli import _read_existing_step
        td = tmp_path / "task"
        (td / "steps").mkdir(parents=True)
        (td / "steps" / "x-1.md").write_text("---\nid: x\n")
        with pytest.raises(ValueError) as exc:
            _read_existing_step(td, "x-1")
        assert "no trailing --- delimiter" in str(exc.value)


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

    def test_complete_phase_rejected(self, scaffolded, capsys):
        """Final-review fix: compile-fail must not clobber a successfully
        landed graph. A task in compile_phase=complete is terminal for
        the compile sub-machine; the operator should use `abort` to
        archive it instead."""
        from tigerharness.journal.compile_cli import cmd_compile_fail
        task_id, paths = scaffolded
        s = Status.from_json(paths.status_json(task_id).read_text())
        s.compile_phase = CompilePhase.COMPLETE
        s.compile_pending = False
        paths.status_json(task_id).write_text(s.to_json())
        rc = cmd_compile_fail(argparse.Namespace(
            journal_dir=str(paths.root), task_id=task_id,
            reason="someone fat-fingered this",
        ))
        assert rc == 1
        err = capsys.readouterr().err
        assert "compile_phase=complete" in err
        assert "clobber" in err
        # Status NOT modified.
        s2 = Status.from_json(paths.status_json(task_id).read_text())
        assert s2.compile_phase == CompilePhase.COMPLETE
        assert s2.state == State.PENDING

    def test_failed_phase_rejected(self, scaffolded, capsys):
        """A task already in compile_phase=failed should not be
        re-marked -- doing so overwrites the original postmortem in
        next_action without an audit trail."""
        from tigerharness.journal.compile_cli import cmd_compile_fail
        task_id, paths = scaffolded
        # First put the task into FAILED state.
        first = cmd_compile_fail(argparse.Namespace(
            journal_dir=str(paths.root), task_id=task_id,
            reason="original postmortem -- Akagi BLOCK",
        ))
        assert first == 0
        # Now try to re-mark it.
        rc = cmd_compile_fail(argparse.Namespace(
            journal_dir=str(paths.root), task_id=task_id,
            reason="second postmortem -- different reason",
        ))
        assert rc == 1
        assert "already compile_phase=failed" in capsys.readouterr().err
        # The original postmortem survives.
        s = Status.from_json(paths.status_json(task_id).read_text())
        assert "original postmortem -- Akagi BLOCK" in s.next_action

    def test_refreshes_heartbeat(self, scaffolded):
        """compile-fail must refresh updated_at on the same atomic
        write that flips the state, matching the sibling CLIs."""
        from tigerharness.journal.compile_cli import cmd_compile_fail
        task_id, paths = scaffolded
        before = Status.from_json(paths.status_json(task_id).read_text())
        before.updated_at = "2020-01-01T00:00:00Z"
        paths.status_json(task_id).write_text(before.to_json())
        cmd_compile_fail(argparse.Namespace(
            journal_dir=str(paths.root), task_id=task_id, reason="r",
        ))
        after = Status.from_json(paths.status_json(task_id).read_text())
        assert after.updated_at > before.updated_at


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
        # The error block annotates which role mapped to the missing
        # persona so the operator sees the structural cause.
        assert "akagi: Akagi" in err and "MISSING" in err

    def test_happy_prints_role_mapping(self, team_root, capsys):
        ns = argparse.Namespace(team="Shohoku")
        rc = cmd_validate_personas(ns)
        assert rc == 0
        out = capsys.readouterr().out
        # Default mapping surfaced under "ok:".
        assert "drafter: Anzai" in out
        assert "akagi: Akagi" in out
        assert "ayako: Ayako" in out

    def test_overridden_personas_validated(
        self, tmp_path, monkeypatch, capsys,
    ):
        """Phase 2: when workflow.yaml overrides the compile-time
        personas, validate-personas checks the OVERRIDDEN names exist
        on disk, NOT the Phase 1.5 defaults."""
        team = _make_team(
            tmp_path,
            personas=["Sakuragi", "Rukawa", "Mitsui"],
        )
        (team / "configs" / "workflow.yaml").write_text(
            "compile_personas:\n"
            "  drafter: Sakuragi\n"
            "  akagi: Rukawa\n"
            "  ayako: Mitsui\n"
        )
        monkeypatch.chdir(team)
        ns = argparse.Namespace(team="Shohoku")
        rc = cmd_validate_personas(ns)
        assert rc == 0
        out = capsys.readouterr().out
        assert "drafter: Sakuragi" in out
        assert "akagi: Rukawa" in out
        assert "ayako: Mitsui" in out

    def test_overridden_persona_missing_fails_with_clear_mapping(
        self, tmp_path, monkeypatch, capsys,
    ):
        """If the team configures an override pointing at a persona
        that doesn't exist on disk, the failure message names both the
        missing persona AND the role it's mapped to."""
        team = _make_team(
            tmp_path,
            personas=["Anzai", "Akagi", "Ayako"],  # default-named
        )
        (team / "configs" / "workflow.yaml").write_text(
            "compile_personas:\n"
            "  drafter: Sakuragi\n"  # points at non-existent persona
        )
        monkeypatch.chdir(team)
        ns = argparse.Namespace(team="Shohoku")
        rc = cmd_validate_personas(ns)
        assert rc == 1
        err = capsys.readouterr().err
        assert "missing prompt.md" in err
        assert "Sakuragi" in err
        assert "drafter: Sakuragi" in err and "MISSING" in err


# ---------------------------------------------------------------------------
# build_subparsers wiring
# ---------------------------------------------------------------------------

class TestBuildSubparsers:
    def test_registers_all_nine(self):
        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest="cmd")
        build_subparsers(sub)
        names = sorted(sub.choices.keys())
        assert names == sorted([
            "compile-context", "compile-prompts", "validate-graph",
            "land-compile", "compile-fail", "compile-retry",
            "append-steps", "abort", "validate-personas",
        ])

    def test_each_sets_func(self):
        from tigerharness.journal.compile_cli import (
            cmd_append_steps, cmd_compile_fail, cmd_compile_retry,
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
            ("append-steps", cmd_append_steps),
            ("abort", cmd_abort),
            ("validate-personas", cmd_validate_personas),
        ]:
            assert sub.choices[name].get_default("func") is expected

"""Phase 2 compile-mode tests for ``workflow start``.

These exercise the new ``--playbook`` / ``--task-brief`` / ``--brief-file``
/ ``--thread`` flags and the compile-mode flow in
:func:`cli._cmd_start_compile`.

The compile pipeline (Sakuragi) lands in a parallel Phase 2 worktree and
may not exist on this branch yet. Per the documented integration seam
(``docs/workflow-runner-phase2.md`` Public API), we patch the late-bound
module global ``cli.compile_playbook`` so these tests never depend on the
real implementation. The fakes mimic the pipeline's side of the contract:
they accept the documented signature
(``playbook_path`` / ``task_brief`` / ``team_root`` / ``task_paths`` /
``session_manager``), write ``orchestration.json`` + ``steps/``, and return
an object exposing ``.steps`` / ``.critique_iters`` / ``.orchestration``.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from tigerharness.workflow_runner import cli, read_events
from tigerharness.journal.wfcore.errors import (
    CompileTier1Error,
    CompileTier2Error,
)
from tigerharness.workflow_runner.executor import ExecutionOutcome
from tigerharness.journal.wfcore.models import (
    Orchestration,
    WorkflowConfig,
    now_iso,
)


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


@pytest.fixture()
def journal_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI's journal root at a throwaway tmp dir."""
    root = tmp_path / "journal"
    root.mkdir()
    monkeypatch.setenv("TIGERHARNESS_WORKFLOW_JOURNAL", str(root))
    return root


@pytest.fixture()
def team_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create ``teams/Shohoku/workflow/default.md`` and point the CLI at it.

    ``_resolve_team_root`` reads ``$TIGERHARNESS_TEAMS_DIR`` first, so we
    set it to the parent ``teams`` dir; the resolver appends the team name.
    """
    teams = tmp_path / "teams"
    root = teams / "Shohoku"
    (root / "workflow").mkdir(parents=True)
    (root / "workflow" / "default.md").write_text(
        "# Default playbook\n\nDo the work, then review it.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TIGERHARNESS_TEAMS_DIR", str(teams))
    return root


def _make_fake_compile(
    *,
    critique_iters: int = 3,
    human_gate: bool = False,
    approvers: list[str] | None = None,
    steps: list[str] | None = None,
):
    """Build a stand-in for ``compile_playbook``.

    Mimics the pipeline's writes (orchestration.json + step files) and
    returns an object shaped like the real ``CompileResult`` the CLI
    consumes (``.steps`` / ``.critique_iters`` / ``.orchestration``).
    """
    step_ids = list(steps or ["01-plan", "02-review"])

    def _compile(
        *,
        playbook_path: Path,
        task_brief: str,
        team_root: Path,
        task_paths,
        session_manager,
        max_compile_iters: int = 8,
    ):
        wf = WorkflowConfig(
            human_gate=human_gate,
            human_gate_approvers=(
                approvers
                if approvers is not None
                else (["@coach-anzai"] if human_gate else [])
            ),
        )
        orch = Orchestration(
            task_id=task_paths.task_id,
            team="Shohoku",
            playbook=playbook_path.stem,
            playbook_sha256="deadbeefcafe",
            steps=step_ids,
            entrypoint=step_ids[0],
            compiled_at=now_iso(),
            compiled_by="pipeline",
            edges={},
            workflow_config=wf,
            compile_critique_iters=critique_iters,
        )
        # The pipeline owns the step .md *bodies* (CompileResult.steps is
        # frontmatter-only, so the CLI cannot reconstruct them). It does
        # NOT write orchestration.json here -- the CLI persists that (and
        # the trace/transcript) from the returned result via
        # _write_compile_artifacts.
        for sid in step_ids:
            task_paths.step_file(sid).write_text(
                f"# {sid}\n", encoding="utf-8"
            )
        return types.SimpleNamespace(
            steps=list(step_ids),
            critique_iters=critique_iters,
            orchestration=orch,
            trace="happy path: 01-plan -> 02-review -> __done__\n",
            transcript="# Critique transcript\nround 1: APPROVE / APPROVE\n",
        )

    return _compile


@pytest.fixture()
def patch_compile(monkeypatch: pytest.MonkeyPatch):
    """Patch the late-bound ``compile_playbook`` global.

    ``_resolve_compile_entrypoint`` lazily imports the real
    ``compile.pipeline`` module only when the global is ``None``, and that
    module does not exist on this branch -- so the patch keeps the import
    from ever firing.
    """

    def _apply(compile_fn) -> None:
        monkeypatch.setattr(cli, "compile_playbook", compile_fn)

    return _apply


def _patch_executor(
    monkeypatch: pytest.MonkeyPatch, *, outcome=None, must_not_run=False
) -> None:
    """Replace ``cli.WorkflowExecutor`` so no real ``claude`` spawns."""

    class _FakeExecutor:
        def __init__(self, paths, **kwargs):
            if must_not_run:
                raise AssertionError(
                    "executor must not be constructed (--no-run set)"
                )
            self.paths = paths

        def run(self):
            return outcome

    monkeypatch.setattr(cli, "WorkflowExecutor", _FakeExecutor)


def _kinds(events_p: Path) -> list[str]:
    return [e.kind for e in read_events(events_p)]


def _only(events_p: Path, kind: str):
    matches = [e for e in read_events(events_p) if e.kind == kind]
    assert len(matches) == 1, f"expected exactly one {kind}, got {len(matches)}"
    return matches[0]


def _make_step_dir(base: Path) -> Path:
    """A minimal pre-compiled steps dir for the escape-hatch test."""
    d = base / "steps_src"
    d.mkdir(parents=True, exist_ok=True)
    (d / "01-plan.md").write_text(
        "---\n"
        "id: 01-plan\n"
        "persona: anzai\n"
        "role: planner\n"
        "on_approve: __done__\n"
        "on_revise: 01-plan\n"
        "on_block: __escalate__\n"
        "max_iters: 3\n"
        "timeout_sec: 600\n"
        "parallel_with: []\n"
        "---\n\nBody.\n",
        encoding="utf-8",
    )
    return d


def _task_dir(journal_root: Path) -> Path:
    dirs = [p for p in journal_root.iterdir() if p.is_dir()]
    assert len(dirs) == 1, f"expected one task dir, got {dirs}"
    return dirs[0]


# --------------------------------------------------------------------------- #
# 1. happy path: compile then run
# --------------------------------------------------------------------------- #


def test_happy_path_playbook_compiles_and_runs(
    journal_root: Path,
    team_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    patch_compile,
    capsys: pytest.CaptureFixture[str],
) -> None:
    patch_compile(_make_fake_compile(critique_iters=3))
    _patch_executor(
        monkeypatch,
        outcome=ExecutionOutcome(
            final_phase="done", reason="all approved", total_cost_usd=2.5
        ),
    )

    rc = cli.main(
        [
            "start",
            "--team",
            "Shohoku",
            "--playbook",
            "default",
            "--task-brief",
            "Ship the thing safely.",
        ]
    )
    assert rc == 0

    out = capsys.readouterr().out
    assert "Task initialised (compiled):" in out
    assert "playbook:       default" in out
    assert "critique_iters: 3" in out
    assert "Task done:" in out

    events_p = _task_dir(journal_root) / "events.jsonl"
    kinds = _kinds(events_p)
    assert kinds == ["compile_started", "compile_completed", "task_started"]

    started = _only(events_p, "compile_started")
    assert started.extra["playbook"] == "default"
    # sha256 of the brief, lowercase hex, 64 chars.
    sha = started.extra["task_brief_sha256"]
    assert len(sha) == 64 and all(c in "0123456789abcdef" for c in sha)

    completed = _only(events_p, "compile_completed")
    assert completed.extra == {"steps": 2, "critique_iters": 3}

    # The CLI -- not the fake pipeline -- persists the compiled plan and
    # diagnostics from the returned CompileResult. The fake deliberately
    # does NOT write orchestration.json/trace/transcript, so these prove
    # _write_compile_artifacts fired before compile_completed.
    task_dir = _task_dir(journal_root)
    orch = json.loads((task_dir / "orchestration.json").read_text())
    assert orch["steps"] == ["01-plan", "02-review"]
    assert (task_dir / "compile_trace.txt").read_text() == (
        "happy path: 01-plan -> 02-review -> __done__\n"
    )
    assert (task_dir / "compile_critique.md").read_text() == (
        "# Critique transcript\nround 1: APPROVE / APPROVE\n"
    )


# --------------------------------------------------------------------------- #
# 2. --no-run skips executor dispatch
# --------------------------------------------------------------------------- #


def test_no_run_skips_dispatch(
    journal_root: Path,
    team_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    patch_compile,
    capsys: pytest.CaptureFixture[str],
) -> None:
    patch_compile(_make_fake_compile())
    # Executor must never be constructed when --no-run is set.
    _patch_executor(monkeypatch, must_not_run=True)

    rc = cli.main(
        [
            "start",
            "--team",
            "Shohoku",
            "--playbook",
            "default",
            "--task-brief",
            "Just compile, do not run.",
            "--no-run",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Task initialised (compiled):" in out
    assert "--no-run set" in out

    # Compile happened; executor did not (no task_* run events beyond start).
    events_p = _task_dir(journal_root) / "events.jsonl"
    assert _kinds(events_p) == [
        "compile_started",
        "compile_completed",
        "task_started",
    ]


# --------------------------------------------------------------------------- #
# 3. --playbook requires a brief
# --------------------------------------------------------------------------- #


def test_playbook_requires_brief(
    journal_root: Path,
    team_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cli.main(["start", "--team", "Shohoku", "--playbook", "default"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "requires a brief" in err
    assert "--task-brief" in err and "--brief-file" in err
    # Nothing should have been written.
    assert not any(journal_root.iterdir())


# --------------------------------------------------------------------------- #
# 4. --steps is mutually exclusive with compile-mode flags
# --------------------------------------------------------------------------- #


def test_steps_incompatible_with_playbook(
    journal_root: Path,
    team_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    steps_dir = _make_step_dir(tmp_path)
    rc = cli.main(
        [
            "start",
            "--team",
            "Shohoku",
            "--steps",
            str(steps_dir),
            "--playbook",
            "default",
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "mutually exclusive" in err
    assert "--playbook" in err
    assert not any(journal_root.iterdir())


# --------------------------------------------------------------------------- #
# 5. --task-brief and --brief-file are mutually exclusive
# --------------------------------------------------------------------------- #


def test_brief_text_and_file_mutually_exclusive(
    journal_root: Path,
    team_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    brief_file = tmp_path / "brief.md"
    brief_file.write_text("from a file\n", encoding="utf-8")
    rc = cli.main(
        [
            "start",
            "--team",
            "Shohoku",
            "--task-brief",
            "inline",
            "--brief-file",
            str(brief_file),
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "--task-brief and --brief-file are mutually exclusive" in err
    assert not any(journal_root.iterdir())


# --------------------------------------------------------------------------- #
# 6. --steps escape hatch still works (legacy Phase 1 path)
# --------------------------------------------------------------------------- #


def test_steps_escape_hatch_still_works(
    journal_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    steps_dir = _make_step_dir(tmp_path)
    rc = cli.main(
        [
            "start",
            "--team",
            "Shohoku",
            "--steps",
            str(steps_dir),
            "--no-run",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    # The precompiled path prints the plain "Task initialised:" header,
    # NOT the compiled variant.
    assert "Task initialised:" in out
    assert "(compiled)" not in out

    events_p = _task_dir(journal_root) / "events.jsonl"
    # Escape hatch emits only task_started -- no compile_* events.
    assert _kinds(events_p) == ["task_started"]


# --------------------------------------------------------------------------- #
# 7. Tier 1 compile failure emits compile_failed{tier:1}
# --------------------------------------------------------------------------- #


def test_compile_tier1_failure_emits_failure_event(
    journal_root: Path,
    team_root: Path,
    patch_compile,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tier1_errors = [
        {"validator": "ref", "step": "02-review", "msg": "dangling edge"},
    ]

    def _boom(**kwargs):
        raise CompileTier1Error(errors=tier1_errors)

    patch_compile(_boom)

    rc = cli.main(
        [
            "start",
            "--team",
            "Shohoku",
            "--playbook",
            "default",
            "--task-brief",
            "will fail tier 1",
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "tier 1" in err

    events_p = _task_dir(journal_root) / "events.jsonl"
    kinds = _kinds(events_p)
    assert kinds == ["compile_started", "compile_failed"]
    failed = _only(events_p, "compile_failed")
    assert failed.extra["tier"] == 1
    assert failed.extra["errors"] == tier1_errors


# --------------------------------------------------------------------------- #
# 8. Tier 2 compile failure emits compile_failed{tier:2}
# --------------------------------------------------------------------------- #


def test_compile_tier2_failure_emits_failure_event(
    journal_root: Path,
    team_root: Path,
    patch_compile,
    capsys: pytest.CaptureFixture[str],
) -> None:
    last_verdicts = [
        {"critic": "akagi", "verdict": "REVISE", "reason": "too vague"},
        {"critic": "rukawa", "verdict": "APPROVE", "reason": "ok"},
    ]

    def _boom(**kwargs):
        raise CompileTier2Error(last_verdicts=last_verdicts)

    patch_compile(_boom)

    rc = cli.main(
        [
            "start",
            "--team",
            "Shohoku",
            "--playbook",
            "default",
            "--task-brief",
            "will not converge",
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "tier 2" in err

    events_p = _task_dir(journal_root) / "events.jsonl"
    assert _kinds(events_p) == ["compile_started", "compile_failed"]
    failed = _only(events_p, "compile_failed")
    assert failed.extra["tier"] == 2
    assert failed.extra["last_verdicts"] == last_verdicts


# --------------------------------------------------------------------------- #
# 9. --thread persists to status.json (phase_state)
# --------------------------------------------------------------------------- #


def test_thread_flag_persists_to_status_json(
    journal_root: Path,
    team_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    patch_compile,
) -> None:
    patch_compile(_make_fake_compile())
    _patch_executor(monkeypatch, must_not_run=True)

    rc = cli.main(
        [
            "start",
            "--team",
            "Shohoku",
            "--playbook",
            "default",
            "--task-brief",
            "route the gate to slack",
            "--thread",
            "1780389332.940649",
            "--no-run",
        ]
    )
    assert rc == 0

    status = json.loads(
        (_task_dir(journal_root) / "status.json").read_text()
    )
    assert status["phase_state"]["slack_thread_ts"] == "1780389332.940649"


# --------------------------------------------------------------------------- #
# 10. missing playbook -> clear error naming the path
# --------------------------------------------------------------------------- #


def test_playbook_file_missing_clear_error(
    journal_root: Path,
    team_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cli.main(
        [
            "start",
            "--team",
            "Shohoku",
            "--playbook",
            "does-not-exist",
            "--task-brief",
            "x",
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "not found" in err
    assert "does-not-exist.md" in err
    # Failure was pre-mint: no task folder created.
    assert not any(journal_root.iterdir())


# --------------------------------------------------------------------------- #
# 11. missing brief file -> clear error naming the path
# --------------------------------------------------------------------------- #


def test_brief_file_missing_clear_error(
    journal_root: Path,
    team_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "nope" / "brief.md"
    rc = cli.main(
        [
            "start",
            "--team",
            "Shohoku",
            "--playbook",
            "default",
            "--brief-file",
            str(missing),
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "cannot read brief file" in err
    assert str(missing) in err
    assert not any(journal_root.iterdir())


# --------------------------------------------------------------------------- #
# Coverage: brief-file success path
# --------------------------------------------------------------------------- #


def test_brief_file_is_read_and_snapshotted(
    journal_root: Path,
    team_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    patch_compile,
) -> None:
    brief_file = team_root / "brief.md"
    brief_file.write_text("Compile me from a file.\n", encoding="utf-8")

    captured: dict[str, str] = {}

    def _compile(**kwargs):
        captured["task_brief"] = kwargs["task_brief"]
        return _make_fake_compile()(**kwargs)

    patch_compile(_compile)
    _patch_executor(monkeypatch, must_not_run=True)

    rc = cli.main(
        [
            "start",
            "--team",
            "Shohoku",
            "--playbook",
            "default",
            "--brief-file",
            str(brief_file),
            "--no-run",
        ]
    )
    assert rc == 0
    # The file content reached the pipeline verbatim...
    assert captured["task_brief"] == "Compile me from a file.\n"
    # ...and was snapshotted into the journal.
    snap = (_task_dir(journal_root) / "task_brief.md").read_text()
    assert snap == "Compile me from a file.\n"


# --------------------------------------------------------------------------- #
# Coverage: human gate enabled -> human_gate_requested
# --------------------------------------------------------------------------- #


def test_human_gate_requested_when_config_enables_gate(
    journal_root: Path,
    team_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    patch_compile,
) -> None:
    patch_compile(
        _make_fake_compile(human_gate=True, approvers=["@coach-anzai"])
    )
    _patch_executor(monkeypatch, must_not_run=True)

    rc = cli.main(
        [
            "start",
            "--team",
            "Shohoku",
            "--playbook",
            "default",
            "--task-brief",
            "needs a human gate",
            "--thread",
            "1780389332.940649",
            "--no-run",
        ]
    )
    assert rc == 0

    events_p = _task_dir(journal_root) / "events.jsonl"
    kinds = _kinds(events_p)
    assert kinds == [
        "compile_started",
        "compile_completed",
        "human_gate_requested",
        "task_started",
    ]
    gate = _only(events_p, "human_gate_requested")
    assert gate.extra["approvers"] == ["@coach-anzai"]
    assert gate.extra["slack_thread_ts"] == "1780389332.940649"


# --------------------------------------------------------------------------- #
# Coverage: compile mode rejects a malformed --task-id (mint guard)
# --------------------------------------------------------------------------- #


def test_compile_rejects_bad_task_id(
    journal_root: Path,
    team_root: Path,
    patch_compile,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # compile_playbook must never be reached: minting fails first.
    def _never(**kwargs):
        raise AssertionError("compile_playbook must not be called")

    patch_compile(_never)

    rc = cli.main(
        [
            "start",
            "--team",
            "Shohoku",
            "--playbook",
            "default",
            "--task-brief",
            "x",
            "--task-id",
            "bad id with spaces",
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "task-id" in err
    assert not any(journal_root.iterdir())


# --------------------------------------------------------------------------- #
# Coverage: --playbook rejects path-traversal / unsafe names
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad_name",
    ["../evil", "sub/dir", "..", ".hidden", "a\\b"],
)
def test_playbook_name_rejects_path_traversal(
    journal_root: Path,
    team_root: Path,
    patch_compile,
    capsys: pytest.CaptureFixture[str],
    bad_name: str,
) -> None:
    # compile must never be reached: the name guard fails first.
    def _never(**kwargs):
        raise AssertionError("compile_playbook must not be called")

    patch_compile(_never)

    rc = cli.main(
        [
            "start",
            "--team",
            "Shohoku",
            "--playbook",
            bad_name,
            "--task-brief",
            "x",
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "bare name" in err
    # Pre-mint failure: no task folder, no events.
    assert not any(journal_root.iterdir())


# --------------------------------------------------------------------------- #
# Coverage: default playbook name when --playbook omitted
# --------------------------------------------------------------------------- #


def test_playbook_defaults_to_default_name(
    journal_root: Path,
    team_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    patch_compile,
) -> None:
    seen: dict[str, Path] = {}

    def _compile(**kwargs):
        seen["playbook_path"] = kwargs["playbook_path"]
        return _make_fake_compile()(**kwargs)

    patch_compile(_compile)
    _patch_executor(monkeypatch, must_not_run=True)

    # No --playbook: should resolve to default.md.
    rc = cli.main(
        [
            "start",
            "--team",
            "Shohoku",
            "--task-brief",
            "use the default playbook",
            "--no-run",
        ]
    )
    assert rc == 0
    assert seen["playbook_path"].name == "default.md"

    events_p = _task_dir(journal_root) / "events.jsonl"
    started = _only(events_p, "compile_started")
    assert started.extra["playbook"] == "default"


# --------------------------------------------------------------------------- #
# Coverage: _resolve_team_root resolution order (3 branches)
# --------------------------------------------------------------------------- #


def test_resolve_team_root_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TIGERHARNESS_TEAMS_DIR", str(tmp_path))
    assert cli._resolve_team_root("Shohoku") == tmp_path / "Shohoku"


def test_resolve_team_root_cwd_is_team(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TIGERHARNESS_TEAMS_DIR", raising=False)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "personas.yaml").write_text("x: 1\n")
    monkeypatch.chdir(tmp_path)
    # cwd is itself a team root -> return it as-is.
    assert cli._resolve_team_root("Shohoku") == tmp_path


def test_resolve_team_root_default_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TIGERHARNESS_TEAMS_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    # cwd is not a team root, no env override -> teams/<Team>.
    assert cli._resolve_team_root("Shohoku") == tmp_path / "teams" / "Shohoku"

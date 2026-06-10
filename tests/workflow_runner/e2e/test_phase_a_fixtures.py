"""Phase A smoke tests -- exercise the e2e fixtures themselves.

These tests sit one layer below the Phase B scenarios. Their job is
to verify that the fixtures we lean on for Phase B are well-behaved:

* the canonical 3-step playbook validates against the live
  ``StepFrontmatter`` contract, so the executor is guaranteed a
  sane plan to walk;
* ``cli.main(["start", ...])`` against the canonical playbook
  initialises a complete journal tree under the test's private
  ``TIGERHARNESS_WORKFLOW_JOURNAL`` root;
* the scripted fake-claude binary, invoked directly, advances its
  counter, emits the right trailer, echoes ``--resume <sid>``, and
  gracefully degrades on over-run;
* the ``e2e_driver`` fixture's ``run_executor`` closure actually
  drives :class:`WorkflowExecutor.run` to a terminal phase.

If any of these tests fail, every Phase B scenario built on top of
them would be untrustworthy -- so we catch the regression here at
the seam rather than later in a tangled e2e assertion.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tigerharness.workflow_runner import cli as wf_cli
from tigerharness.workflow_runner.executor import ExecutionOutcome
from tigerharness.journal.wfcore.models import StepFrontmatter
from tigerharness.journal.wfcore.trailer import parse_trailer


# --------------------------------------------------------------------------- #
# Step playbook validates
# --------------------------------------------------------------------------- #


def _read_frontmatter(path: Path) -> dict:
    """Re-use the CLI's frontmatter parser so we stay in lockstep."""
    return wf_cli._parse_frontmatter(path.read_text(encoding="utf-8"))


def test_canonical_playbook_validates_against_step_frontmatter(
    e2e_steps_dir: Path,
) -> None:
    """Every step file must parse cleanly into a :class:`StepFrontmatter`.

    Catches drift between the playbook and the live model -- e.g. a
    renamed required field would land the executor with a partial
    plan and a cryptic ``KeyError`` mid-iteration.
    """
    step_files = sorted(e2e_steps_dir.glob("*.md"))
    assert [p.name for p in step_files] == [
        "01-plan.md",
        "02-build.md",
        "03-review.md",
    ], "Phase B scenarios assume exactly these three ids in this order"

    parsed = [StepFrontmatter.from_dict(_read_frontmatter(p)) for p in step_files]

    plan, build, review = parsed
    # Sanity: the routing graph matches the brief.
    assert plan.id == "01-plan"
    assert plan.on_approve == "02-build"
    assert plan.on_revise == "01-plan"  # self-loop -- drives the max-iters scenario
    assert plan.on_block == "__escalate__"

    assert build.id == "02-build"
    assert build.on_approve == "03-review"
    assert build.on_revise == "01-plan"  # REVISE rewinds to plan
    assert build.on_block == "__escalate__"

    assert review.id == "03-review"
    assert review.on_approve == "__done__"
    assert review.on_revise == "02-build"
    assert review.on_block == "__escalate__"

    # Per-step caps are uniform per the brief.
    for sf in parsed:
        assert sf.max_iters == 3
        assert sf.timeout_sec == 30


# --------------------------------------------------------------------------- #
# `cli start` initialises a complete journal tree
# --------------------------------------------------------------------------- #


def test_cli_start_initialises_journal(
    e2e_steps_dir: Path,
    e2e_journal_root: Path,
    e2e_personas_dir: Path,  # noqa: ARG001 -- ensures envs are wired
) -> None:
    """``workflow start`` lays out the journal tree per the spec.

    We verify the externally-observable contract -- file existence,
    JSON shape, recorded ``task_started`` event -- rather than
    re-asserting CLI internals. That way the test still passes if
    the CLI internals refactor as long as the on-disk contract holds.
    """
    rc = wf_cli.main(
        ["start", "--team", "ShohokuE2E", "--steps", str(e2e_steps_dir),
         "--no-run"]
    )
    assert rc == 0

    # Exactly one task dir under the journal root.
    task_dirs = [p for p in e2e_journal_root.iterdir() if p.is_dir()]
    assert len(task_dirs) == 1
    task_dir = task_dirs[0]

    # Required files per the spec's "Folder layout" section.
    assert (task_dir / "status.json").exists()
    assert (task_dir / "orchestration.json").exists()
    assert (task_dir / "sessions.json").exists()
    assert (task_dir / "events.jsonl").exists()
    assert (task_dir / "steps").is_dir()
    assert (task_dir / "logs").is_dir()

    # The compiled steps were copied in by id.
    step_files = sorted(p.name for p in (task_dir / "steps").iterdir())
    assert step_files == ["01-plan.md", "02-build.md", "03-review.md"]

    # status.json: phase=execute, pointer at the entrypoint, no work done yet.
    status = json.loads((task_dir / "status.json").read_text())
    assert status["phase"] == "execute"
    assert status["current_step"] == "01-plan"
    assert status["current_iter"] == 0
    assert status["step_history"] == []

    # orchestration.json: edges captured for every step.
    orch = json.loads((task_dir / "orchestration.json").read_text())
    assert orch["team"] == "ShohokuE2E"
    assert orch["entrypoint"] == "01-plan"
    assert set(orch["edges"].keys()) == {"01-plan", "02-build", "03-review"}

    # sessions.json: empty until the executor invokes a persona.
    assert json.loads((task_dir / "sessions.json").read_text()) == {}

    # events.jsonl: a single task_started record, fields per the spec.
    lines = (task_dir / "events.jsonl").read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["kind"] == "task_started"
    assert rec["team"] == "ShohokuE2E"
    assert rec["entrypoint"] == "01-plan"
    assert rec["steps"] == 3


# --------------------------------------------------------------------------- #
# Driver harness
# --------------------------------------------------------------------------- #


def test_e2e_driver_factory_returns_bundle(e2e_driver) -> None:
    """The driver factory packages the task state for a scenario.

    A Phase B test will rely on the returned bundle exposing the
    task-id, paths, fake-claude handle, and (eventually) a working
    ``run_executor``; this test pins that surface.
    """
    bundle = e2e_driver(team="ShohokuDriverTest")
    assert bundle.team == "ShohokuDriverTest"
    assert bundle.task_id, (
        "factory must mint a non-empty task-id when --task-id is omitted"
    )
    assert bundle.paths.task_dir.is_dir()
    assert bundle.read_status()["phase"] == "execute"

    events = bundle.read_events()
    assert len(events) == 1 and events[0]["kind"] == "task_started"


def test_e2e_driver_run_executor_returns_outcome(e2e_driver) -> None:
    """``run_executor`` drives the real :class:`WorkflowExecutor`.

    Pins the wire-up contract: ``run_executor()`` returns an
    :class:`ExecutionOutcome` with a terminal phase in
    ``{"done", "escalated", "cancelled"}`` and a non-negative cost.

    Driver-level smoke test only -- the per-scenario assertions live
    in ``test_phase_b_scenarios.py``. Here we just confirm the wire
    is intact end-to-end.
    """
    bundle = e2e_driver(team="ShohokuWireSmoke")
    bundle.fake_claude.set_script([
        {"trailer": "WORKFLOW: APPROVE", "cost_usd": 0.01},
        {"trailer": "WORKFLOW: APPROVE", "cost_usd": 0.01},
        {"trailer": "WORKFLOW: APPROVE", "cost_usd": 0.01},
    ])
    outcome = bundle.run_executor()
    assert isinstance(outcome, ExecutionOutcome)
    assert outcome.final_phase in {"done", "escalated", "cancelled"}
    assert outcome.total_cost_usd >= 0.0
    # The status on disk must agree with the returned outcome --
    # this catches a regression where _write_status is skipped.
    assert bundle.read_status()["phase"] == outcome.final_phase


# --------------------------------------------------------------------------- #
# Scripted fake-claude binary
# --------------------------------------------------------------------------- #


def _run_fake(
    binary: Path,
    *,
    env_extras: dict[str, str] | None = None,
    stdin: str = "",
    extra_argv: list[str] | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if env_extras:
        env.update(env_extras)
    argv = [str(binary), "-p", "--output-format", "json"]
    if extra_argv:
        argv.extend(extra_argv)
    return subprocess.run(
        argv,
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def test_scripted_fake_emits_trailers_in_order(
    e2e_fake_claude,
) -> None:
    """Each invocation pops the next response; the parser sees the trailer."""
    script_path = e2e_fake_claude.set_script([
        {"trailer": "WORKFLOW: APPROVE", "cost_usd": 0.10, "persona": "anzai"},
        {
            "trailer": "WORKFLOW: REVISE: scope too big",
            "cost_usd": 0.05,
            "persona": "akagi",
        },
        {"trailer": "WORKFLOW: BLOCK: missing creds", "persona": "rukawa"},
    ])
    env = {"FAKE_CLAUDE_SCRIPT": str(script_path)}

    verdict_kinds: list[str] = []
    costs: list[float] = []
    for _ in range(3):
        proc = _run_fake(e2e_fake_claude.binary, env_extras=env, stdin="hi")
        assert proc.returncode == 0, proc.stderr
        envelope = json.loads(proc.stdout)
        verdict = parse_trailer(envelope["result"])
        verdict_kinds.append(verdict.kind)
        costs.append(float(envelope.get("total_cost_usd", 0.0)))

    assert verdict_kinds == ["APPROVE", "REVISE", "BLOCK"]
    assert costs == pytest.approx([0.10, 0.05, 0.0])
    assert e2e_fake_claude.counter() == 3


def test_scripted_fake_echoes_resume_sid(e2e_fake_claude) -> None:
    """When --resume <sid> is on argv, the fake echoes that sid back.

    This is what lets the SessionManager's "session rotation" code
    path stay stable across iterations without forcing the test
    author to hard-code rotating sids in the script.
    """
    script = e2e_fake_claude.set_script([{"trailer": "WORKFLOW: APPROVE"}])

    env = {"FAKE_CLAUDE_SCRIPT": str(script)}
    proc = _run_fake(
        e2e_fake_claude.binary,
        env_extras=env,
        extra_argv=["--resume", "carry-this-sid"],
    )
    envelope = json.loads(proc.stdout)
    assert envelope["session_id"] == "carry-this-sid"


def test_scripted_fake_mints_fresh_sid_without_resume(e2e_fake_claude) -> None:
    """On first-call (no --resume), a fresh sid is minted and stable per index."""
    script = e2e_fake_claude.set_script([
        {"trailer": "WORKFLOW: APPROVE"},
        {"trailer": "WORKFLOW: APPROVE"},
    ])
    env = {"FAKE_CLAUDE_SCRIPT": str(script)}

    first = json.loads(_run_fake(e2e_fake_claude.binary, env_extras=env).stdout)
    second = json.loads(_run_fake(e2e_fake_claude.binary, env_extras=env).stdout)
    assert first["session_id"] == "sid-fresh-1"
    assert second["session_id"] == "sid-fresh-2"


def test_scripted_fake_overrun_emits_block(e2e_fake_claude) -> None:
    """Past the end of the script, the fake emits a clearly-tagged BLOCK.

    Hangs are the worst e2e failure mode -- they eat the CI minute
    budget. Forcing a visible BLOCK at end-of-script means a test
    that under-counts its responses fails fast with a useful message.
    """
    script = e2e_fake_claude.set_script([{"trailer": "WORKFLOW: APPROVE"}])
    env = {"FAKE_CLAUDE_SCRIPT": str(script)}

    # Consume the only response.
    _run_fake(e2e_fake_claude.binary, env_extras=env)
    # Second call runs past the end.
    proc = _run_fake(e2e_fake_claude.binary, env_extras=env)
    envelope = json.loads(proc.stdout)
    verdict = parse_trailer(envelope["result"])
    assert verdict.kind == "BLOCK"
    # Block verdicts carry the reason in ``summary``.
    assert "ran off" in verdict.summary.lower()


def test_scripted_fake_session_id_override(e2e_fake_claude) -> None:
    """An entry's ``session_id`` field wins over the resume-echo default."""
    script = e2e_fake_claude.set_script([
        {"trailer": "WORKFLOW: APPROVE", "session_id": "explicit-sid"},
    ])
    env = {"FAKE_CLAUDE_SCRIPT": str(script)}
    proc = _run_fake(
        e2e_fake_claude.binary,
        env_extras=env,
        extra_argv=["--resume", "resumed-sid-would-have-won"],
    )
    envelope = json.loads(proc.stdout)
    assert envelope["session_id"] == "explicit-sid"


def test_scripted_fake_unset_falls_back_to_legacy_env_vars(
    e2e_fake_claude,
) -> None:
    """Without ``FAKE_CLAUDE_SCRIPT`` the legacy env-var path applies.

    Critical: Rukawa's session tests rely on ``FAKE_RESULT_TEXT`` /
    ``FAKE_SESSION_ID`` / ``FAKE_COST_USD``. The additive scripted
    branch must not steal control unless explicitly engaged.
    """
    proc = _run_fake(
        e2e_fake_claude.binary,
        env_extras={
            "FAKE_RESULT_TEXT": "legacy-path-text",
            "FAKE_SESSION_ID": "legacy-sid",
            "FAKE_COST_USD": "0.99",
        },
    )
    envelope = json.loads(proc.stdout)
    assert envelope["result"] == "legacy-path-text"
    assert envelope["session_id"] == "legacy-sid"
    assert envelope["total_cost_usd"] == pytest.approx(0.99)

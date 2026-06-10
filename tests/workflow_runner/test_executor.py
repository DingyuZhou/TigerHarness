"""Unit tests for ``tigerharness.workflow_runner.executor``.

Strategy: build a tiny in-memory ``FakeSessionManager`` that scripts
a sequence of ``InvocationResult``s. The executor consumes that fake
through its constructor seam, so no ``claude`` subprocess ever runs
and the tests are fully deterministic.

Scenarios covered (matches the brief's required matrix):

* Linear three-step path to ``__done__``.
* REVISE rewind (same step re-runs with feedback prologue).
* REVISE with explicit ``target=`` jumps back two steps.
* BLOCK exit -> ``phase=escalated`` + ``escalation=block:...``.
* ``max_loop_iters`` breach -> escalation.
* ``max_cost_usd`` breach -> escalation.
* ``max_task_wall_sec`` breach -> escalation.
* ``.cancel`` flag honored at iteration boundary.
* Resume from ``status.phase=cancelling`` finalises as ``cancelled``.
* ParseError x2 retries same step; 3rd ParseError -> escalation.
* Resume after kill: status reflects pre-call pointer / iter_counts.
* Two-process lock contention (subprocess pattern from test_locks.py).
* status.last_heartbeat and .pid heartbeat move together (Akagi's
  load-bearing contract).
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable, Optional

import pytest

from tigerharness.workflow_runner import executor as executor_mod
from tigerharness.workflow_runner.atomic import read_json, write_json_atomic
from tigerharness.workflow_runner.events import read_events
from tigerharness.workflow_runner.executor import (
    ExecutionOutcome,
    ExecutorError,
    MAX_PARSE_FAILURES,
    WorkflowExecutor,
    _extract_body,
    _iso_to_epoch,
    _parse_frontmatter,
)
from tigerharness.workflow_runner.locks import read_pid_info
from tigerharness.journal.wfcore.models import (
    Orchestration,
    SessionMap,
    Status,
    StepEdges,
    StepFrontmatter,
    WorkflowConfig,
    now_iso,
)
from tigerharness.workflow_runner.paths import TaskPaths
from tigerharness.workflow_runner.sessions import InvocationResult


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _make_step_md(
    *,
    step_id: str,
    persona: str = "anzai",
    role: str = "planner",
    on_approve: str = "__done__",
    on_revise: Optional[str] = None,
    on_block: str = "__escalate__",
    max_iters: int = 5,
    timeout_sec: int = 60,
    body: str = "Do the thing.\n",
) -> str:
    """Render a step .md file with valid frontmatter + body."""
    on_revise = on_revise if on_revise is not None else step_id
    fm_lines = [
        "---",
        f"id: {step_id}",
        f"persona: {persona}",
        f"role: {role}",
        f"on_approve: {on_approve}",
        f"on_revise: {on_revise}",
        f"on_block: {on_block}",
        f"max_iters: {max_iters}",
        f"timeout_sec: {timeout_sec}",
        "parallel_with: []",
        "---",
        "",
    ]
    return "\n".join(fm_lines) + body


@pytest.fixture
def task_paths(tmp_path: Path) -> TaskPaths:
    paths = TaskPaths(root=tmp_path, task_id="t-test").ensure()
    return paths


def _seed_task(
    paths: TaskPaths,
    *,
    steps: list[dict],
    workflow_config: Optional[dict] = None,
) -> None:
    """Materialise step files + orchestration.json + status.json + sessions.json.

    Each ``steps`` entry is a kwargs dict passed to :func:`_make_step_md`.
    ``workflow_config`` overrides the WorkflowConfig defaults.
    """
    fms: list[StepFrontmatter] = []
    for s in steps:
        text = _make_step_md(**s)
        sid = s["step_id"]
        (paths.steps_dir / f"{sid}.md").write_text(text, encoding="utf-8")
        fms.append(StepFrontmatter.from_dict(_parse_frontmatter(text)))

    cfg_kwargs: dict[str, Any] = {
        "human_gate": False,
        "human_gate_approvers": [],
    }
    if workflow_config:
        cfg_kwargs.update(workflow_config)
    cfg = WorkflowConfig(**cfg_kwargs)

    orch = Orchestration(
        task_id=paths.task_id,
        team="Shohoku",
        playbook="precompiled",
        playbook_sha256="0" * 64,
        steps=[fm.id for fm in fms],
        entrypoint=fms[0].id,
        compiled_at=now_iso(),
        compiled_by="test",
        edges={fm.id: fm.edges for fm in fms},
        workflow_config=cfg,
    )
    write_json_atomic(paths.orchestration_json, orch.to_dict())

    status = Status(
        task_id=paths.task_id,
        phase="execute",
        started_at=now_iso(),
        current_step=fms[0].id,
        current_iter=0,
    )
    write_json_atomic(paths.status_json, status.to_dict())
    write_json_atomic(paths.sessions_json, {})


# --------------------------------------------------------------------------- #
# Fake SessionManager
# --------------------------------------------------------------------------- #


class FakeSessionManager:
    """Scripts a queue of ``InvocationResult``s per call.

    ``script`` is a list of dicts describing each invocation's outcome:
        {"stdout": "...WORKFLOW: APPROVE\\n", "cost_usd": 0.10}
    or
        {"trailer": "APPROVE", "cost_usd": 0.10}    # convenience
    or
        {"trailer": "REVISE", "summary": "fix X", "cost_usd": 0.05}
    or
        {"trailer": "REVISE_TARGET", "target": "s1",
         "summary": "back to s1", "cost_usd": 0.05}
    or
        {"trailer": "BLOCK", "summary": "stuck", "cost_usd": 0.05}
    or
        {"trailer": "PARSE_ERROR", "cost_usd": 0.05}   # produces stdout
                                                       # without trailer
    """

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    def invoke(
        self,
        persona: str,
        prompt: str,
        *,
        timeout_sec: int,
        log_dir: Optional[Path] = None,
    ) -> InvocationResult:
        if not self._script:
            raise AssertionError(
                f"FakeSessionManager out of scripted calls "
                f"(called persona={persona!r})"
            )
        spec = self._script.pop(0)
        self.calls.append(
            {
                "persona": persona,
                "prompt": prompt,
                "timeout_sec": timeout_sec,
                "log_dir": log_dir,
            }
        )
        stdout = spec.get("stdout")
        if stdout is None:
            stdout = _render_stdout(spec)
        # Mimic the real SessionManager: write prompt + envelope + stdout
        # + stderr to log_dir so executor tests can assert on the
        # captured artifacts.
        if log_dir is not None:
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
            (log_dir / "stdout.txt").write_text(stdout, encoding="utf-8")
            (log_dir / "stderr.txt").write_text("", encoding="utf-8")
            (log_dir / "envelope.json").write_text(
                json.dumps({"result": stdout}), encoding="utf-8"
            )
        return InvocationResult(
            stdout=stdout,
            session_id=spec.get("session_id", f"sid-{persona}"),
            cost_usd=float(spec.get("cost_usd", 0.0)),
            exit_code=int(spec.get("exit_code", 0)),
            error=spec.get("error"),
            raw_envelope=spec.get("envelope", {}),
        )


def _render_stdout(spec: dict[str, Any]) -> str:
    """Render a stdout payload whose last line is the desired trailer."""
    tag = spec.get("trailer", "APPROVE")
    body = spec.get("body", "ok\n")
    if tag == "APPROVE":
        return body + "WORKFLOW: APPROVE\n"
    if tag == "REVISE":
        return body + f"WORKFLOW: REVISE: {spec['summary']}\n"
    if tag == "REVISE_TARGET":
        return (
            body
            + f"WORKFLOW: REVISE: target={spec['target']}: {spec['summary']}\n"
        )
    if tag == "BLOCK":
        return body + f"WORKFLOW: BLOCK: {spec['summary']}\n"
    if tag == "PARSE_ERROR":
        return body + "no trailer here\n"
    raise AssertionError(f"unknown trailer tag: {tag!r}")


# --------------------------------------------------------------------------- #
# Frontmatter / body helpers
# --------------------------------------------------------------------------- #


def test_parse_frontmatter_extracts_yaml_mapping():
    text = "---\nid: s1\npersona: p\n---\nbody here\n"
    assert _parse_frontmatter(text) == {"id": "s1", "persona": "p"}


def test_parse_frontmatter_returns_empty_on_no_fence():
    assert _parse_frontmatter("no fence here\n") == {}


def test_parse_frontmatter_returns_empty_on_unclosed_fence():
    assert _parse_frontmatter("---\nid: s1\nno closing\n") == {}


def test_parse_frontmatter_returns_empty_on_yaml_error():
    assert _parse_frontmatter("---\n: bad\n  yaml: [\n---\nbody") == {}


def test_parse_frontmatter_returns_empty_on_non_mapping():
    assert _parse_frontmatter("---\n- a\n- b\n---\nbody") == {}


def test_parse_frontmatter_returns_empty_on_empty_file():
    assert _parse_frontmatter("") == {}


def test_extract_body_strips_frontmatter_and_leading_blank_lines():
    text = "---\nid: s1\n---\n\n\nactual body\n"
    assert _extract_body(text) == "actual body\n"


def test_extract_body_returns_text_unchanged_without_frontmatter():
    assert _extract_body("plain body\n") == "plain body\n"


def test_extract_body_returns_text_when_fence_unclosed():
    text = "---\nid: s1\nno closing\n"
    assert _extract_body(text) == text


def test_extract_body_empty_file():
    assert _extract_body("") == ""


def test_iso_to_epoch_round_trips_utc():
    # Round-trip via datetime so the literal stays self-checking
    # regardless of platform timezone weirdness.
    import datetime as _dt
    expected = _dt.datetime(2026, 5, 28, tzinfo=_dt.timezone.utc).timestamp()
    assert _iso_to_epoch("2026-05-28T00:00:00Z") == pytest.approx(expected, abs=1.0)


def test_iso_to_epoch_handles_naive_iso():
    assert _iso_to_epoch("2026-05-28T00:00:00") is not None


def test_iso_to_epoch_none_on_bad_input():
    assert _iso_to_epoch("garbage") is None
    assert _iso_to_epoch(None) is None
    assert _iso_to_epoch("") is None


# --------------------------------------------------------------------------- #
# Happy path: three-step linear approve to __done__
# --------------------------------------------------------------------------- #


def test_linear_path_to_done(task_paths: TaskPaths) -> None:
    _seed_task(
        task_paths,
        steps=[
            {"step_id": "s1", "on_approve": "s2"},
            {"step_id": "s2", "on_approve": "s3"},
            {"step_id": "s3", "on_approve": "__done__"},
        ],
    )
    fake = FakeSessionManager(
        [
            {"trailer": "APPROVE", "cost_usd": 0.1},
            {"trailer": "APPROVE", "cost_usd": 0.2},
            {"trailer": "APPROVE", "cost_usd": 0.3},
        ]
    )
    ex = WorkflowExecutor(task_paths, session_manager=fake)
    outcome = ex.run()

    assert outcome.final_phase == "done"
    assert outcome.total_cost_usd == pytest.approx(0.6)
    status = Status.from_dict(read_json(task_paths.status_json))
    assert status.phase == "done"
    assert status.iter_counts == {"s1": 1, "s2": 1, "s3": 1}
    assert status.cost_usd_per_step == {
        "s1": pytest.approx(0.1),
        "s2": pytest.approx(0.2),
        "s3": pytest.approx(0.3),
    }
    # History records each verdict + persona attribution.
    assert [h.verdict for h in status.step_history] == [
        "APPROVE", "APPROVE", "APPROVE",
    ]
    assert [h.step for h in status.step_history] == ["s1", "s2", "s3"]

    # Events: task_started was written by the seed (no, by CLI -- not seed).
    # Our seed doesn't emit task_started; the executor emits step_started /
    # step_completed / task_completed. Confirm those.
    kinds = [e.kind for e in read_events(task_paths.events_jsonl)]
    assert kinds == [
        "step_started", "step_completed",
        "step_started", "step_completed",
        "step_started", "step_completed",
        "task_completed",
    ]


# --------------------------------------------------------------------------- #
# REVISE rewind to same step with feedback prologue
# --------------------------------------------------------------------------- #


def test_revise_rewind_same_step_with_feedback_prologue(
    task_paths: TaskPaths,
) -> None:
    _seed_task(
        task_paths,
        steps=[
            {"step_id": "s1", "on_approve": "__done__", "max_iters": 3},
        ],
    )
    fake = FakeSessionManager(
        [
            {"trailer": "REVISE", "summary": "needs more detail",
             "cost_usd": 0.1},
            {"trailer": "APPROVE", "cost_usd": 0.1},
        ]
    )
    ex = WorkflowExecutor(task_paths, session_manager=fake)
    outcome = ex.run()

    assert outcome.final_phase == "done"
    # Second invoke's prompt must contain the feedback prologue with
    # the prior REVISE reason.
    assert "needs more detail" in fake.calls[1]["prompt"]
    assert fake.calls[0]["prompt"] != fake.calls[1]["prompt"]
    # Iter 1 prompt has no prologue.
    assert "previous attempts" not in fake.calls[0]["prompt"]
    # Iter 2 dispatched into a different log dir (iter-01 vs iter-02).
    assert fake.calls[0]["log_dir"].name == "iter-01"
    assert fake.calls[1]["log_dir"].name == "iter-02"


# --------------------------------------------------------------------------- #
# REVISE with explicit target= jumps to named step
# --------------------------------------------------------------------------- #


def test_revise_target_jumps_two_steps_back(
    task_paths: TaskPaths,
) -> None:
    _seed_task(
        task_paths,
        steps=[
            {"step_id": "s1", "on_approve": "s2"},
            {"step_id": "s2", "on_approve": "s3"},
            {"step_id": "s3", "on_approve": "__done__"},
        ],
    )
    fake = FakeSessionManager(
        [
            {"trailer": "APPROVE", "cost_usd": 0.1},   # s1 iter 1
            {"trailer": "APPROVE", "cost_usd": 0.1},   # s2 iter 1
            # s3 iter 1: revise back to s1 directly
            {"trailer": "REVISE_TARGET", "target": "s1",
             "summary": "redo from scratch", "cost_usd": 0.1},
            {"trailer": "APPROVE", "cost_usd": 0.1},   # s1 iter 2
            {"trailer": "APPROVE", "cost_usd": 0.1},   # s2 iter 2
            {"trailer": "APPROVE", "cost_usd": 0.1},   # s3 iter 2
        ]
    )
    ex = WorkflowExecutor(task_paths, session_manager=fake)
    outcome = ex.run()

    assert outcome.final_phase == "done"
    status = Status.from_dict(read_json(task_paths.status_json))
    assert status.iter_counts == {"s1": 2, "s2": 2, "s3": 2}
    # Step history reads cleanly in order of dispatch.
    sequence = [(h.step, h.verdict) for h in status.step_history]
    assert sequence == [
        ("s1", "APPROVE"),
        ("s2", "APPROVE"),
        ("s3", "REVISE"),
        ("s1", "APPROVE"),
        ("s2", "APPROVE"),
        ("s3", "APPROVE"),
    ]
    # The s1 iter 2 prompt carries the REVISE reason as feedback.
    s1_iter2_prompt = fake.calls[3]["prompt"]
    assert "redo from scratch" in s1_iter2_prompt
    # The s2 iter 2 prompt does NOT inherit it (chain broken by s1's
    # APPROVE).
    s2_iter2_prompt = fake.calls[4]["prompt"]
    assert "redo from scratch" not in s2_iter2_prompt


# --------------------------------------------------------------------------- #
# BLOCK exits with escalated phase
# --------------------------------------------------------------------------- #


def test_block_verdict_escalates(task_paths: TaskPaths) -> None:
    _seed_task(
        task_paths,
        steps=[{"step_id": "s1", "on_approve": "__done__"}],
    )
    fake = FakeSessionManager(
        [{"trailer": "BLOCK", "summary": "missing input", "cost_usd": 0.05}]
    )
    outcome = WorkflowExecutor(task_paths, session_manager=fake).run()

    assert outcome.final_phase == "escalated"
    assert outcome.reason == "block:missing input"
    status = Status.from_dict(read_json(task_paths.status_json))
    assert status.phase == "escalated"
    assert status.escalation == "block:missing input"
    kinds = [e.kind for e in read_events(task_paths.events_jsonl)]
    assert "step_completed" in kinds
    assert "block" in kinds


# --------------------------------------------------------------------------- #
# Constraint breach: max_loop_iters
# --------------------------------------------------------------------------- #


def test_max_loop_iters_breach_escalates(task_paths: TaskPaths) -> None:
    _seed_task(
        task_paths,
        steps=[
            {"step_id": "s1", "on_approve": "__done__", "max_iters": 2},
        ],
    )
    # Two REVISEs back to s1, then we hit the cap before iter 3 starts.
    fake = FakeSessionManager(
        [
            {"trailer": "REVISE", "summary": "again", "cost_usd": 0.1},
            {"trailer": "REVISE", "summary": "again", "cost_usd": 0.1},
        ]
    )
    outcome = WorkflowExecutor(task_paths, session_manager=fake).run()

    assert outcome.final_phase == "escalated"
    assert outcome.reason.startswith("max_loop_iters:s1:2")
    status = Status.from_dict(read_json(task_paths.status_json))
    assert status.iter_counts == {"s1": 2}
    # Cap-breach event was emitted as a constraint_breached kind.
    kinds = [e.kind for e in read_events(task_paths.events_jsonl)]
    assert "constraint_breached" in kinds


# --------------------------------------------------------------------------- #
# Constraint breach: max_cost_usd
# --------------------------------------------------------------------------- #


def test_max_cost_usd_breach_escalates(task_paths: TaskPaths) -> None:
    """Cost check fires at the top of the loop iteration. A step that
    pushes us over the ceiling escalates BEFORE the next dispatch (so a
    looping graph eventually hits the cap)."""
    _seed_task(
        task_paths,
        steps=[
            {"step_id": "s1", "on_approve": "s2"},
            {"step_id": "s2", "on_approve": "s1", "max_iters": 5},
        ],
        workflow_config={"max_cost_usd": 0.50},
    )
    fake = FakeSessionManager(
        [
            {"trailer": "APPROVE", "cost_usd": 0.30},  # s1 -> total 0.30
            {"trailer": "APPROVE", "cost_usd": 0.25},  # s2 -> total 0.55
            # Loop would continue to s1 iter 2; cost check kills it.
        ]
    )
    outcome = WorkflowExecutor(task_paths, session_manager=fake).run()

    assert outcome.final_phase == "escalated"
    assert outcome.reason.startswith("max_cost_usd:")
    status = Status.from_dict(read_json(task_paths.status_json))
    assert status.cost_usd_total == pytest.approx(0.55)
    assert len(fake.calls) == 2


def test_max_cost_usd_breach_at_loop_top(task_paths: TaskPaths) -> None:
    """Cost check fires *before* a step dispatch when prior iters
    already pushed cumulative cost past the ceiling."""
    _seed_task(
        task_paths,
        steps=[{"step_id": "s1", "on_approve": "s1", "max_iters": 5}],
        workflow_config={"max_cost_usd": 0.20},
    )
    fake = FakeSessionManager(
        [
            {"trailer": "REVISE", "summary": "x", "cost_usd": 0.25},
        ]
    )
    outcome = WorkflowExecutor(task_paths, session_manager=fake).run()
    assert outcome.final_phase == "escalated"
    assert outcome.reason.startswith("max_cost_usd:")
    # Only one dispatch occurred.
    assert len(fake.calls) == 1


# --------------------------------------------------------------------------- #
# Constraint breach: max_task_wall_sec
# --------------------------------------------------------------------------- #


def test_max_task_wall_sec_breach_escalates(task_paths: TaskPaths) -> None:
    _seed_task(
        task_paths,
        steps=[{"step_id": "s1", "on_approve": "__done__"}],
        workflow_config={"max_task_wall_sec": 1},  # 1 second cap
    )
    fake = FakeSessionManager(
        [{"trailer": "APPROVE", "cost_usd": 0.01}]
    )
    # Advance epoch_now well past status.started_at + 1s.
    base = time.time()
    clock = {"t": base + 9999}
    ex = WorkflowExecutor(
        task_paths,
        session_manager=fake,
        now_epoch_fn=lambda: clock["t"],
    )
    outcome = ex.run()
    assert outcome.final_phase == "escalated"
    assert outcome.reason.startswith("max_task_wall_sec:")
    assert fake.calls == []  # never dispatched


# --------------------------------------------------------------------------- #
# Cancel handling
# --------------------------------------------------------------------------- #


def test_cancel_flag_honoured_at_loop_top(task_paths: TaskPaths) -> None:
    _seed_task(
        task_paths,
        steps=[{"step_id": "s1", "on_approve": "__done__"}],
    )
    (task_paths.task_dir / ".cancel").touch()
    fake = FakeSessionManager([])
    outcome = WorkflowExecutor(task_paths, session_manager=fake).run()
    assert outcome.final_phase == "cancelled"
    status = Status.from_dict(read_json(task_paths.status_json))
    assert status.phase == "cancelled"
    kinds = [e.kind for e in read_events(task_paths.events_jsonl)]
    assert "cancel_complete" in kinds
    # No dispatch occurred.
    assert fake.calls == []


def test_resume_in_cancelling_phase_finalises_as_cancelled(
    task_paths: TaskPaths,
) -> None:
    _seed_task(
        task_paths,
        steps=[{"step_id": "s1", "on_approve": "__done__"}],
    )
    # Simulate the CLI's `cancel` action: phase=cancelling without a
    # .cancel sentinel.
    raw = read_json(task_paths.status_json)
    raw["phase"] = "cancelling"
    write_json_atomic(task_paths.status_json, raw)

    fake = FakeSessionManager([])
    outcome = WorkflowExecutor(task_paths, session_manager=fake).run()
    assert outcome.final_phase == "cancelled"


def test_cancel_mid_loop_after_first_iter(task_paths: TaskPaths) -> None:
    """First iter runs; .cancel set externally; second loop turn cancels."""
    _seed_task(
        task_paths,
        steps=[{"step_id": "s1", "on_approve": "s1", "max_iters": 5}],
    )

    class CancelAfterFirst(FakeSessionManager):
        def __init__(self, paths, script):
            super().__init__(script)
            self._paths = paths

        def invoke(self, *args, **kwargs):  # type: ignore[override]
            res = super().invoke(*args, **kwargs)
            # Drop the cancel flag after the first call returns.
            if len(self.calls) == 1:
                (self._paths.task_dir / ".cancel").touch()
            return res

    fake = CancelAfterFirst(
        task_paths,
        [
            {"trailer": "REVISE", "summary": "loop", "cost_usd": 0.05},
        ],
    )
    outcome = WorkflowExecutor(task_paths, session_manager=fake).run()
    assert outcome.final_phase == "cancelled"
    # One dispatch executed; the second loop iteration tripped cancel.
    assert len(fake.calls) == 1


# --------------------------------------------------------------------------- #
# ParseError handling
# --------------------------------------------------------------------------- #


def test_parse_error_retries_same_step_then_succeeds(
    task_paths: TaskPaths,
) -> None:
    """Per spec: re-prompt exactly ONCE on parse failure. If the
    second attempt produces a clean trailer, the loop continues.
    """
    _seed_task(
        task_paths,
        steps=[{"step_id": "s1", "on_approve": "__done__", "max_iters": 5}],
    )
    fake = FakeSessionManager(
        [
            {"trailer": "PARSE_ERROR", "cost_usd": 0.05},
            {"trailer": "APPROVE", "cost_usd": 0.05},
        ]
    )
    outcome = WorkflowExecutor(task_paths, session_manager=fake).run()
    assert outcome.final_phase == "done"
    status = Status.from_dict(read_json(task_paths.status_json))
    # parse_failure_count reset on successful verdict.
    assert "parse_failure_count" not in status.phase_state
    # Two dispatches, all on s1.
    assert [c["persona"] for c in fake.calls] == ["anzai"] * 2
    # iter_counts shows two dispatches (parse failures count too --
    # otherwise the per-step cap couldn't bound a parse-failure loop).
    assert status.iter_counts == {"s1": 2}
    # History only has the one successful APPROVE.
    assert len(status.step_history) == 1
    assert status.step_history[0].verdict == "APPROVE"


def test_parse_error_second_consecutive_escalates(
    task_paths: TaskPaths,
) -> None:
    """Per spec: original shot + exactly one re-prompt. If the second
    attempt still fails to produce a trailer, escalate.
    """
    _seed_task(
        task_paths,
        steps=[{"step_id": "s1", "on_approve": "__done__", "max_iters": 5}],
    )
    fake = FakeSessionManager(
        [
            {"trailer": "PARSE_ERROR", "cost_usd": 0.05},
            {"trailer": "PARSE_ERROR", "cost_usd": 0.05},
        ]
    )
    outcome = WorkflowExecutor(task_paths, session_manager=fake).run()
    assert outcome.final_phase == "escalated"
    assert outcome.reason == "parse_failure_loop"
    status = Status.from_dict(read_json(task_paths.status_json))
    assert status.escalation == "parse_failure_loop"
    assert len(fake.calls) == MAX_PARSE_FAILURES
    assert MAX_PARSE_FAILURES == 2  # spec contract


def test_parse_failure_reprompt_text_injected(
    task_paths: TaskPaths,
) -> None:
    """Per spec: the second dispatch (after a parse failure) must be
    prefixed with the canonical re-prompt message so the persona knows
    why it's being asked again.
    """
    _seed_task(
        task_paths,
        steps=[{"step_id": "s1", "on_approve": "__done__", "max_iters": 5}],
    )
    fake = FakeSessionManager(
        [
            {"trailer": "PARSE_ERROR", "cost_usd": 0.05},
            {"trailer": "APPROVE", "cost_usd": 0.05},
        ]
    )
    WorkflowExecutor(task_paths, session_manager=fake).run()
    # First dispatch: no re-prompt prefix.
    assert "I couldn't find your WORKFLOW: trailer" not in fake.calls[0]["prompt"]
    # Second dispatch: spec-mandated re-prompt prefix.
    assert "I couldn't find your WORKFLOW: trailer" in fake.calls[1]["prompt"]
    assert "WORKFLOW: APPROVE / REVISE / BLOCK" in fake.calls[1]["prompt"]


# --------------------------------------------------------------------------- #
# Resume after kill: pre-call state durable
# --------------------------------------------------------------------------- #


def test_resume_after_kill_repeats_in_progress_iter(
    task_paths: TaskPaths,
) -> None:
    """Two runs of the same task. The first 'kills' itself after iter 1
    (we simulate by raising); the second continues. Status reflects
    only what completed before the kill, so iter 2 re-dispatches from
    the same pointer."""
    _seed_task(
        task_paths,
        steps=[
            {"step_id": "s1", "on_approve": "s2"},
            {"step_id": "s2", "on_approve": "__done__"},
        ],
    )

    class KillAfterOne(FakeSessionManager):
        def invoke(self, *args, **kwargs):  # type: ignore[override]
            if len(self.calls) == 1:
                raise KeyboardInterrupt("simulated kill")
            return super().invoke(*args, **kwargs)

    fake1 = KillAfterOne(
        [
            {"trailer": "APPROVE", "cost_usd": 0.10},  # s1 iter 1 ok
            {"trailer": "APPROVE", "cost_usd": 0.10},  # s2 iter 1 -- killed
        ]
    )
    with pytest.raises(KeyboardInterrupt):
        WorkflowExecutor(task_paths, session_manager=fake1).run()

    # After the kill, status should show: s1 completed iter 1, pointer
    # advanced to s2, current_iter on s2 is 1 (we entered iter 1 of s2
    # before the kill -- but iter_counts is bumped post-dispatch, not
    # pre; since invoke raised before we even called it the second
    # time, iter_counts has no s2 entry).
    status_mid = Status.from_dict(read_json(task_paths.status_json))
    assert status_mid.iter_counts == {"s1": 1}
    assert status_mid.current_step == "s2"
    # current_iter reflects the in-progress dispatch attempt (1)
    # because we wrote step_started_at + bumped current_iter before
    # invoke. iter_counts is still empty for s2 -- exactly what
    # 'resume re-runs from scratch' expects.
    assert status_mid.current_iter == 1
    assert status_mid.cost_usd_total == pytest.approx(0.10)

    # Second run completes the task.
    fake2 = FakeSessionManager(
        [{"trailer": "APPROVE", "cost_usd": 0.20}]
    )
    outcome = WorkflowExecutor(task_paths, session_manager=fake2).run()
    assert outcome.final_phase == "done"
    status_final = Status.from_dict(read_json(task_paths.status_json))
    assert status_final.iter_counts == {"s1": 1, "s2": 1}
    assert status_final.cost_usd_total == pytest.approx(0.30)


# --------------------------------------------------------------------------- #
# Two-process lock contention
# --------------------------------------------------------------------------- #


def test_two_process_lock_contention_refuses(tmp_path: Path) -> None:
    """A second WorkflowExecutor.run on the same task folder while the
    first holds the lock must raise ExecutorError immediately."""
    paths = TaskPaths(root=tmp_path, task_id="t-locked").ensure()
    _seed_task(
        paths,
        steps=[{"step_id": "s1", "on_approve": "__done__"}],
    )

    holder = textwrap.dedent(f"""
        import sys, time
        from tigerharness.workflow_runner.locks import acquire_task_lock
        with acquire_task_lock({str(paths.task_dir)!r}, blocking=False):
            sys.stdout.write("HELD\\n")
            sys.stdout.flush()
            time.sleep(2.0)
    """)
    proc = subprocess.Popen(
        [sys.executable, "-c", holder],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert "HELD" in proc.stdout.readline()

        fake = FakeSessionManager([])
        ex = WorkflowExecutor(paths, session_manager=fake)
        with pytest.raises(ExecutorError) as excinfo:
            ex.run()
        assert "already running" in str(excinfo.value)
    finally:
        proc.terminate()
        proc.wait(timeout=5)


# --------------------------------------------------------------------------- #
# Heartbeat sync (Akagi's load-bearing contract)
# --------------------------------------------------------------------------- #


def test_status_and_pid_heartbeat_move_together(
    task_paths: TaskPaths,
) -> None:
    _seed_task(
        task_paths,
        steps=[
            {"step_id": "s1", "on_approve": "s2"},
            {"step_id": "s2", "on_approve": "__done__"},
        ],
    )
    # Inject a deterministic clock so the two heartbeat fields don't
    # drift by a wall-clock second across the comparison.
    times = iter([f"2026-05-28T12:00:{n:02d}Z" for n in range(60)])

    def fake_now():
        return next(times)

    fake = FakeSessionManager(
        [
            {"trailer": "APPROVE", "cost_usd": 0.1},
            {"trailer": "APPROVE", "cost_usd": 0.1},
        ]
    )
    ex = WorkflowExecutor(
        task_paths,
        session_manager=fake,
        now_iso_fn=fake_now,
    )
    outcome = ex.run()
    assert outcome.final_phase == "done"
    status = Status.from_dict(read_json(task_paths.status_json))
    pid_info = read_pid_info(task_paths.task_dir)
    assert pid_info is not None
    assert status.last_heartbeat == pid_info.last_heartbeat


# --------------------------------------------------------------------------- #
# Error paths
# --------------------------------------------------------------------------- #


def test_missing_orchestration_raises_executor_error(
    task_paths: TaskPaths,
) -> None:
    # status only, no orchestration.
    write_json_atomic(
        task_paths.status_json,
        Status(
            task_id="t-test",
            phase="execute",
            started_at=now_iso(),
            current_step="s1",
        ).to_dict(),
    )
    with pytest.raises(ExecutorError, match="orchestration.json"):
        WorkflowExecutor(
            task_paths, session_manager=FakeSessionManager([])
        ).run()


def test_missing_status_raises_executor_error(task_paths: TaskPaths) -> None:
    # orchestration only, no status.
    cfg = WorkflowConfig(human_gate=False, human_gate_approvers=[])
    edges = {"s1": StepEdges(
        on_approve="__done__", on_revise="s1", on_block="__escalate__"
    )}
    orch = Orchestration(
        task_id="t-test",
        team="Shohoku",
        playbook="precompiled",
        playbook_sha256="0" * 64,
        steps=["s1"],
        entrypoint="s1",
        compiled_at=now_iso(),
        compiled_by="test",
        edges=edges,
        workflow_config=cfg,
    )
    write_json_atomic(task_paths.orchestration_json, orch.to_dict())
    with pytest.raises(ExecutorError, match="status.json"):
        WorkflowExecutor(
            task_paths, session_manager=FakeSessionManager([])
        ).run()


def test_malformed_orchestration_raises_executor_error(
    task_paths: TaskPaths,
) -> None:
    write_json_atomic(task_paths.orchestration_json, {"task_id": "x"})
    write_json_atomic(
        task_paths.status_json,
        Status(
            task_id="t-test",
            phase="execute",
            started_at=now_iso(),
        ).to_dict(),
    )
    with pytest.raises(ExecutorError, match="orchestration.json invalid"):
        WorkflowExecutor(
            task_paths, session_manager=FakeSessionManager([])
        ).run()


def test_malformed_status_raises_executor_error(
    task_paths: TaskPaths,
) -> None:
    cfg = WorkflowConfig(human_gate=False, human_gate_approvers=[])
    edges = {"s1": StepEdges(
        on_approve="__done__", on_revise="s1", on_block="__escalate__"
    )}
    orch = Orchestration(
        task_id="t-test",
        team="Shohoku",
        playbook="precompiled",
        playbook_sha256="0" * 64,
        steps=["s1"],
        entrypoint="s1",
        compiled_at=now_iso(),
        compiled_by="test",
        edges=edges,
        workflow_config=cfg,
    )
    write_json_atomic(task_paths.orchestration_json, orch.to_dict())
    write_json_atomic(task_paths.status_json, {"task_id": "x"})
    with pytest.raises(ExecutorError, match="status.json invalid"):
        WorkflowExecutor(
            task_paths, session_manager=FakeSessionManager([])
        ).run()


def test_current_step_missing_escalates(task_paths: TaskPaths) -> None:
    _seed_task(
        task_paths,
        steps=[{"step_id": "s1", "on_approve": "__done__"}],
    )
    raw = read_json(task_paths.status_json)
    raw["current_step"] = None
    write_json_atomic(task_paths.status_json, raw)
    outcome = WorkflowExecutor(
        task_paths, session_manager=FakeSessionManager([])
    ).run()
    assert outcome.final_phase == "escalated"
    assert outcome.reason == "current_step_missing"


def test_step_file_missing_escalates(task_paths: TaskPaths) -> None:
    _seed_task(
        task_paths,
        steps=[{"step_id": "s1", "on_approve": "__done__"}],
    )
    (task_paths.steps_dir / "s1.md").unlink()
    outcome = WorkflowExecutor(
        task_paths, session_manager=FakeSessionManager([])
    ).run()
    assert outcome.final_phase == "escalated"
    assert outcome.reason.startswith("step_load_failed:s1")


def test_step_file_without_frontmatter_escalates(
    task_paths: TaskPaths,
) -> None:
    _seed_task(
        task_paths,
        steps=[{"step_id": "s1", "on_approve": "__done__"}],
    )
    (task_paths.steps_dir / "s1.md").write_text(
        "no frontmatter here\n", encoding="utf-8"
    )
    outcome = WorkflowExecutor(
        task_paths, session_manager=FakeSessionManager([])
    ).run()
    assert outcome.final_phase == "escalated"
    assert outcome.reason.startswith("step_load_failed:s1")


# --------------------------------------------------------------------------- #
# Approve / Revise to sentinel routing edges
# --------------------------------------------------------------------------- #


def test_approve_to_escalate_sentinel_escalates(task_paths: TaskPaths) -> None:
    _seed_task(
        task_paths,
        steps=[{"step_id": "s1", "on_approve": "__escalate__"}],
    )
    fake = FakeSessionManager(
        [{"trailer": "APPROVE", "cost_usd": 0.05}]
    )
    outcome = WorkflowExecutor(task_paths, session_manager=fake).run()
    assert outcome.final_phase == "escalated"
    assert outcome.reason == "approve_to_escalate:s1"


def test_revise_to_done_sentinel_escalates(task_paths: TaskPaths) -> None:
    _seed_task(
        task_paths,
        steps=[
            {
                "step_id": "s1",
                "on_approve": "__done__",
                "on_revise": "__done__",
            }
        ],
    )
    fake = FakeSessionManager(
        [{"trailer": "REVISE", "summary": "x", "cost_usd": 0.05}]
    )
    outcome = WorkflowExecutor(task_paths, session_manager=fake).run()
    assert outcome.final_phase == "escalated"
    assert outcome.reason == "revise_to_done:s1"


def test_revise_to_escalate_sentinel_escalates(task_paths: TaskPaths) -> None:
    _seed_task(
        task_paths,
        steps=[
            {
                "step_id": "s1",
                "on_approve": "__done__",
                "on_revise": "__escalate__",
            }
        ],
    )
    fake = FakeSessionManager(
        [{"trailer": "REVISE", "summary": "x", "cost_usd": 0.05}]
    )
    outcome = WorkflowExecutor(task_paths, session_manager=fake).run()
    assert outcome.final_phase == "escalated"
    assert outcome.reason == "revise_to_escalate:s1"


# --------------------------------------------------------------------------- #
# Log dir capture
# --------------------------------------------------------------------------- #


def test_iter_log_dirs_are_created_per_iter(task_paths: TaskPaths) -> None:
    _seed_task(
        task_paths,
        steps=[{"step_id": "s1", "on_approve": "__done__", "max_iters": 3}],
    )
    fake = FakeSessionManager(
        [
            {"trailer": "REVISE", "summary": "again", "cost_usd": 0.05},
            {"trailer": "APPROVE", "cost_usd": 0.05},
        ]
    )
    WorkflowExecutor(task_paths, session_manager=fake).run()
    s1_logs = task_paths.logs_dir / "s1"
    assert (s1_logs / "iter-01" / "prompt.txt").exists()
    assert (s1_logs / "iter-02" / "prompt.txt").exists()


# --------------------------------------------------------------------------- #
# Default SessionManager wiring
# --------------------------------------------------------------------------- #


def test_default_session_manager_built_when_none_passed(
    task_paths: TaskPaths,
) -> None:
    """Without an explicit session_manager, the executor builds a real
    :class:`SessionManager` rooted at the task dir. We don't run it
    (would spawn claude); just check the construction path."""
    ex = WorkflowExecutor(task_paths)
    assert ex._sessions is not None
    # Internals are intentional public-by-convention here -- this is a
    # cheap proof the test-seam default doesn't drift.
    assert getattr(ex._sessions, "_task_dir") == task_paths.task_dir


# --------------------------------------------------------------------------- #
# Feedback prologue edge cases
# --------------------------------------------------------------------------- #


def test_feedback_prologue_empty_on_iter_one(task_paths: TaskPaths) -> None:
    """Iter 1 of any step gets no prologue, even if history is non-empty
    from prior steps that didn't end in REVISE."""
    _seed_task(
        task_paths,
        steps=[
            {"step_id": "s1", "on_approve": "s2"},
            {"step_id": "s2", "on_approve": "__done__"},
        ],
    )
    fake = FakeSessionManager(
        [
            {"trailer": "APPROVE", "cost_usd": 0.05},
            {"trailer": "APPROVE", "cost_usd": 0.05},
        ]
    )
    WorkflowExecutor(task_paths, session_manager=fake).run()
    assert "previous attempts" not in fake.calls[1]["prompt"]


def test_feedback_prologue_breaks_on_non_revise_in_history(
    task_paths: TaskPaths,
) -> None:
    """If the most recent history entry isn't a REVISE, no prologue is
    injected even when iter > 1."""
    _seed_task(
        task_paths,
        steps=[
            {"step_id": "s1", "on_approve": "s2"},
            {"step_id": "s2", "on_approve": "s1", "max_iters": 3},
        ],
    )
    # s1 -> APPROVE -> s2 -> APPROVE-routed-to-s1 (via on_approve back
    # to s1). Then iter 2 of s1 has history tail = APPROVE, not REVISE.
    fake = FakeSessionManager(
        [
            {"trailer": "APPROVE", "cost_usd": 0.05},  # s1 iter 1
            {"trailer": "APPROVE", "cost_usd": 0.05},  # s2 iter 1
            {"trailer": "REVISE_TARGET", "target": "__done__",
             "summary": "skip", "cost_usd": 0.05},     # s1 iter 2
        ]
    )
    # Adjust: s2's on_approve goes back to s1 to force iter 2 of s1.
    # s1 iter 2's history tail is APPROVE (from s2). No prologue.
    # Then s1 iter 2 REVISE_TARGET to __done__ which escalates.
    outcome = WorkflowExecutor(task_paths, session_manager=fake).run()
    # s1 iter 2's prompt has no prologue because the most recent entry
    # was an APPROVE (s2's), not a REVISE.
    s1_iter2_call = fake.calls[2]
    assert "previous attempts" not in s1_iter2_call["prompt"]
    # And the REVISE-to-done escalates.
    assert outcome.final_phase == "escalated"

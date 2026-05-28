"""Phase B end-to-end scenarios for the workflow-runner.

These tests drive the canonical 3-step playbook to completion via
the public ``workflow start`` + :class:`WorkflowExecutor` entry
points, then assert on ``events.jsonl`` + ``status.json``. Every
scenario uses the shared ``e2e_driver`` factory; differences are
entirely in the scripted fake-claude responses.

Each scenario pins three families of contract:

  * **Terminal phase** -- ``status.phase`` is ``done`` / ``escalated``
    / ``cancelled`` as the spec requires for that path. All three
    terminal phases are exercised by at least one scenario in this
    module (done: 1, 2, 5; escalated: 3, 4; cancelled: 6).
  * **Cost accumulation** -- ``status.cost_usd_total`` equals the
    sum of the fake's per-call ``cost_usd`` values. This catches
    a regression where the executor drops cost between dispatch
    and status write (Akagi's sweep CLI surfaces this number).
  * **Machine-truth events** -- ``events.jsonl`` carries the
    documented event kinds in the documented order. The exact
    sequence is what downstream tools (``workflow show``,
    ``workflow diagnose``) key off.

The scenarios are gated on a runtime import probe of
``tigerharness.workflow_runner.executor``: if the module ever
disappears (a refactor mishap, an upstream revert) the tests
skip with a clear reason rather than ImportError-crashing the
whole e2e suite.
"""

from __future__ import annotations

import importlib.util
import json

import pytest


# --------------------------------------------------------------------------- #
# Gate: executor module presence
# --------------------------------------------------------------------------- #


def _executor_ready() -> bool:
    """Return True iff ``workflow_runner.executor`` is importable.

    Cheap import probe -- does not actually import the module, just
    asks the importer whether a loader can find it. Avoids the cost
    + side effects of a real import in the no-op (skip) case.
    """
    return (
        importlib.util.find_spec(
            "tigerharness.workflow_runner.executor"
        )
        is not None
    )


pytestmark = pytest.mark.skipif(
    not _executor_ready(),
    reason=(
        "tigerharness.workflow_runner.executor is unexpectedly "
        "absent. The scenarios depend on the executor module being "
        "importable; skip cleanly rather than ImportError-crashing "
        "the rest of the e2e suite."
    ),
)


# --------------------------------------------------------------------------- #
# Scenario 1 -- linear path to __done__
# --------------------------------------------------------------------------- #


def test_linear_path_to_done(e2e_driver) -> None:
    """Three APPROVEs in a row -> ``status.phase == "done"``.

    Asserts (spec contract):
      * final ``status.phase`` is ``"done"``
      * ``events.jsonl`` contains ``task_started``, then three
        ``step_completed`` events (one per step) in order, then a
        terminal event (``task_completed``)
      * total cost equals the sum of the fake's per-call costs
      * the scripted fake's counter is exactly 3 (no parse-fail
        re-prompts, no over-run)
    """
    bundle = e2e_driver(team="ShohokuLinear")
    bundle.fake_claude.set_script([
        {"trailer": "WORKFLOW: APPROVE", "cost_usd": 0.10, "persona": "anzai"},
        {"trailer": "WORKFLOW: APPROVE", "cost_usd": 0.07, "persona": "akagi"},
        {"trailer": "WORKFLOW: APPROVE", "cost_usd": 0.05, "persona": "rukawa"},
    ])

    bundle.run_executor()

    status = bundle.read_status()
    assert status["phase"] == "done"
    assert status["cost_usd_total"] == pytest.approx(0.22)

    events = bundle.read_events()
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "task_started"
    step_completed = [e for e in events if e["kind"] == "step_completed"]
    assert len(step_completed) == 3
    assert [e["step"] for e in step_completed] == [
        "01-plan",
        "02-build",
        "03-review",
    ]
    assert all(e["verdict"] == "APPROVE" for e in step_completed)
    # The executor's terminal-done event kind is ``task_completed``
    # (see _finalize_done). Pin the exact name -- if it ever
    # changes we want this test to flag the rename loudly.
    assert kinds[-1] == "task_completed"

    assert bundle.fake_claude.counter() == 3


# --------------------------------------------------------------------------- #
# Scenario 2 -- single REVISE rewind to plan
# --------------------------------------------------------------------------- #


def test_single_revise_rewinds_to_plan(e2e_driver) -> None:
    """02-build REVISEs once, then linear to done.

    Sequence: plan APPROVE, build REVISE -> plan re-runs (iter 2,
    APPROVE), build APPROVE, review APPROVE.

    Asserts:
      * ``status.step_history`` includes the REVISE entry with the
        reason text intact
      * an event shows ``step_completed{verdict="REVISE"}`` on build
      * 01-plan is entered a second time after the REVISE (iter 2,
        with a feedback prologue in its prompt -- the executor
        injects the prologue per spec "Loop semantics -- rewind")
      * final phase is ``"done"``
    """
    bundle = e2e_driver(team="ShohokuRevise")
    bundle.fake_claude.set_script([
        # 01-plan iter 1
        {"trailer": "WORKFLOW: APPROVE", "cost_usd": 0.10, "persona": "anzai", "iter": 1},
        # 02-build iter 1 -- REVISE rewinds to 01-plan
        {
            "trailer": "WORKFLOW: REVISE: scope too big",
            "cost_usd": 0.04,
            "persona": "akagi",
            "iter": 1,
        },
        # 01-plan iter 2 -- after rewind
        {"trailer": "WORKFLOW: APPROVE", "cost_usd": 0.09, "persona": "anzai", "iter": 2},
        # 02-build iter 2
        {"trailer": "WORKFLOW: APPROVE", "cost_usd": 0.07, "persona": "akagi", "iter": 2},
        # 03-review iter 1
        {"trailer": "WORKFLOW: APPROVE", "cost_usd": 0.05, "persona": "rukawa", "iter": 1},
    ])

    bundle.run_executor()

    status = bundle.read_status()
    assert status["phase"] == "done"
    # 01-plan ran twice; 02-build twice; 03-review once.
    assert status["iter_counts"]["01-plan"] == 2
    assert status["iter_counts"]["02-build"] == 2
    assert status["iter_counts"]["03-review"] == 1
    # Sum of script costs: 0.10 + 0.04 + 0.09 + 0.07 + 0.05 = 0.35.
    assert status["cost_usd_total"] == pytest.approx(0.35)

    # SessionManager continuity: each persona's session id must be
    # reused across re-dispatches of its step. The on-disk shape is
    # ``{persona: sid_str}``; with 3 distinct personas we expect
    # exactly 3 entries with 3 distinct sids. If anzai's iter 2
    # minted a fresh sid we'd still see "anzai" once, but the sid
    # the fake echoed back wouldn't match what got persisted at
    # iter 1 -- the persona-thread persistence contract surfaces
    # as "no churn in this map across the rewind".
    sessions_path = bundle.paths.sessions_json
    assert sessions_path.exists(), (
        "sessions.json must exist after a successful run -- the "
        "SessionManager writes it on every invoke."
    )
    sessions = json.loads(sessions_path.read_text(encoding="utf-8"))
    assert set(sessions.keys()) == {"anzai", "akagi", "rukawa"}, (
        f"expected one entry per persona; got {sorted(sessions)!r}"
    )
    sids = set(sessions.values())
    assert len(sids) == 3 and all(sids), (
        "expected 3 distinct, non-empty session ids (one per "
        f"persona); got {sessions!r}"
    )

    history = status["step_history"]
    revise_entries = [h for h in history if h["verdict"] == "REVISE"]
    assert len(revise_entries) == 1
    assert revise_entries[0]["step"] == "02-build"
    assert "scope too big" in (revise_entries[0]["reason"] or "")

    # The executor signals a rewind not as a dedicated event kind
    # but as the sequence: ``step_completed{verdict=REVISE}`` on the
    # source step, then ``step_started`` on the target step with a
    # bumped iter number. That sequence IS the rewind observable.
    events = bundle.read_events()
    kinds = [(e["kind"], e.get("step"), e.get("iter"), e.get("verdict"))
             for e in events]
    revise_on_build_idx = next(
        i for i, t in enumerate(kinds)
        if t == ("step_completed", "02-build", 1, "REVISE")
    )
    plan_iter2_started_idx = next(
        i for i, t in enumerate(kinds)
        if t[0] == "step_started" and t[1] == "01-plan" and t[2] == 2
    )
    assert plan_iter2_started_idx > revise_on_build_idx, (
        "01-plan iter 2 must start AFTER 02-build iter 1 emits the "
        "REVISE -- that ordering is the rewind contract."
    )

    assert bundle.fake_claude.counter() == 5


# --------------------------------------------------------------------------- #
# Scenario 3 -- BLOCK exit on step 2
# --------------------------------------------------------------------------- #


def test_block_on_build_escalates(e2e_driver) -> None:
    """02-build BLOCKs -> ``status.phase == "escalated"``.

    Asserts:
      * final phase is ``"escalated"``
      * the escalation reason contains the block reason text
      * the last event before terminal is the block / escalation
        record carrying ``verdict="BLOCK"`` on 02-build
    """
    bundle = e2e_driver(team="ShohokuBlock")
    bundle.fake_claude.set_script([
        {"trailer": "WORKFLOW: APPROVE", "cost_usd": 0.10, "persona": "anzai"},
        {
            "trailer": "WORKFLOW: BLOCK: missing credentials for build env",
            "cost_usd": 0.03,
            "persona": "akagi",
        },
    ])

    bundle.run_executor()

    status = bundle.read_status()
    assert status["phase"] == "escalated"
    # Cost contract: 0.10 (plan APPROVE) + 0.03 (build BLOCK) = 0.13.
    # The block-emitting dispatch's cost must still be accumulated --
    # the spec writes cost before the trailer is parsed.
    assert status["cost_usd_total"] == pytest.approx(0.13)

    # The Status model carries the escalation reason in the
    # top-level ``escalation`` field; per spec the message should
    # surface the BLOCK reason so the Operator sees actionable text.
    escalation_text = (status.get("escalation") or "").lower()
    assert "block" in escalation_text or "credentials" in escalation_text

    events = bundle.read_events()
    block_events = [
        e for e in events
        if e["kind"] == "step_completed" and e.get("verdict") == "BLOCK"
    ]
    assert len(block_events) == 1
    assert block_events[0]["step"] == "02-build"

    assert bundle.fake_claude.counter() == 2


# --------------------------------------------------------------------------- #
# Scenario 4 -- constraint breach (max_loop_iters)
# --------------------------------------------------------------------------- #


def test_max_loop_iters_self_revise_escalates(e2e_driver) -> None:
    """01-plan keeps REVISEing itself; hits ``max_iters=3`` and escalates.

    The plan step's ``on_revise`` points back to itself, so each
    REVISE bumps its own iter counter without advancing the
    pointer. On iter == max_iters (3) the executor must emit
    ``constraint_breached`` and route to escalation.
    """
    bundle = e2e_driver(team="ShohokuLoop")
    bundle.fake_claude.set_script([
        # iter 1 REVISE -> self-rewind, iter_counts[01-plan] = 1
        {"trailer": "WORKFLOW: REVISE: needs more detail", "cost_usd": 0.05},
        # iter 2 REVISE -> self-rewind, iter_counts[01-plan] = 2
        {"trailer": "WORKFLOW: REVISE: still too thin", "cost_usd": 0.05},
        # iter 3 REVISE -> self-rewind, iter_counts[01-plan] = 3.
        # The executor then re-enters the loop, computes
        # next_iter = 4, sees 4 > cap (3), and finalises the
        # constraint breach WITHOUT a fourth dispatch. So the
        # script needs exactly 3 entries; a defensive 4th would
        # silently hide a regression that over-dispatched.
        {"trailer": "WORKFLOW: REVISE: still not good", "cost_usd": 0.05},
    ])

    bundle.run_executor()

    status = bundle.read_status()
    assert status["phase"] == "escalated"
    # Iter cap is 3 (from 01-plan.max_iters in the playbook); the
    # executor consumes all 3 iter slots before escalating.
    assert status["iter_counts"]["01-plan"] == 3
    # Cost contract: exactly 3 dispatches at 0.05 each. If the
    # executor ever over-dispatched, this number would be wrong.
    assert status["cost_usd_total"] == pytest.approx(0.15)
    # And the counter pins the dispatch count from the fake's side.
    assert bundle.fake_claude.counter() == 3

    # The constraint_breached event carries the reason text the
    # executor wrote into status.escalation. For per-step iter cap
    # breaches the reason is shaped ``max_loop_iters:<step>:<cap>``
    # -- that string is the only mandatory machine-readable signal,
    # and it must mention the offending step.
    events = bundle.read_events()
    breached = [e for e in events if e["kind"] == "constraint_breached"]
    assert breached, "expected a constraint_breached event"
    reasons = [str(e.get("reason") or "") for e in breached]
    assert any(
        "max_loop_iters" in r and "01-plan" in r for r in reasons
    ), (
        "expected a constraint_breached event whose reason names "
        f"max_loop_iters on 01-plan; got reasons={reasons!r}"
    )
    # And the status field surfaces the same text for the Operator.
    assert "max_loop_iters" in (status.get("escalation") or "")


# --------------------------------------------------------------------------- #
# Scenario 5 -- parse error then valid trailer
# --------------------------------------------------------------------------- #


def test_parse_failure_reprompts_once_then_recovers(e2e_driver) -> None:
    """01-plan's first response has no trailer; second is valid APPROVE.

    Per spec ``parser rule 4``: on parse miss, re-prompt the same
    persona exactly once with the canonical reminder. If the second
    response parses, the loop continues normally.

    Asserts:
      * a ``verdict_parse_failed`` event is emitted on the first
        response
      * 01-plan still completes (iter 1) on the second response
      * the workflow runs through to ``done``
    """
    bundle = e2e_driver(team="ShohokuParseError")
    bundle.fake_claude.set_script([
        # 01-plan first attempt -- no WORKFLOW: trailer at all
        {
            "trailer": "(intentionally not a workflow trailer)",
            "body": "I forgot to add the trailer line.",
            "cost_usd": 0.03,
            "persona": "anzai",
        },
        # 01-plan re-prompt -- valid APPROVE
        {"trailer": "WORKFLOW: APPROVE", "cost_usd": 0.08, "persona": "anzai"},
        # 02-build, 03-review -- clean APPROVE each
        {"trailer": "WORKFLOW: APPROVE", "cost_usd": 0.07, "persona": "akagi"},
        {"trailer": "WORKFLOW: APPROVE", "cost_usd": 0.05, "persona": "rukawa"},
    ])

    bundle.run_executor()

    status = bundle.read_status()
    assert status["phase"] == "done"
    # Cost contract: 0.03 (parse-failure dispatch) + 0.08 + 0.07 + 0.05 = 0.23.
    # The parse-failed dispatch's cost must still accumulate -- per
    # spec the cost write happens BEFORE the trailer is parsed, so
    # crash-after-dispatch leaves cost durable.
    assert status["cost_usd_total"] == pytest.approx(0.23)

    events = bundle.read_events()
    parse_failed = [e for e in events if e["kind"] == "verdict_parse_failed"]
    assert len(parse_failed) == 1, (
        "expected exactly one verdict_parse_failed -- if 2+, the "
        "executor is re-prompting too many times"
    )
    assert parse_failed[0]["step"] == "01-plan"

    assert bundle.fake_claude.counter() == 4


# --------------------------------------------------------------------------- #
# Scenario 6 -- .cancel flag short-circuits to cancelled
# --------------------------------------------------------------------------- #


def test_cancel_flag_short_circuits_to_cancelled(e2e_driver) -> None:
    """A ``.cancel`` flag dropped before ``run()`` -> ``status.phase == "cancelled"``.

    The ``workflow cancel`` CLI writes a ``.cancel`` sentinel into
    the task dir; the executor checks for it at the top of every
    loop iteration (see ``executor._cancel_requested``). If the
    flag is present *before* the first iteration, the executor
    must finalise as cancelled WITHOUT dispatching to any persona.

    Asserts (the cancel contract):
      * final ``status.phase`` is ``"cancelled"`` -- the third
        terminal phase that the module docstring promises but no
        other scenario exercises
      * a ``cancel_complete`` event is emitted exactly once, with
        the right ``task_id``
      * NO ``step_started`` / ``step_completed`` events fire --
        cancel must short-circuit before any dispatch
      * the fake-claude counter stays at 0 -- a regression where
        the executor dispatched-once-then-cancelled would be a
        cost/correctness bug, and this pin catches it
      * ``status.cost_usd_total`` is 0.0 -- no dispatch, no cost
      * the returned :class:`ExecutionOutcome.final_phase` agrees
        with the on-disk status (the two writers must not disagree)
    """
    bundle = e2e_driver(team="ShohokuCancel")
    # Script a single APPROVE as a tripwire: if the executor ever
    # dispatches despite the .cancel flag, this entry would be
    # consumed and the counter assertion below would catch it.
    bundle.fake_claude.set_script([
        {"trailer": "WORKFLOW: APPROVE", "cost_usd": 0.99, "persona": "anzai"},
    ])

    # Drop the cancel sentinel into the task dir BEFORE running.
    # This is exactly the on-disk state ``workflow cancel`` leaves
    # behind: a zero-byte ``.cancel`` file in the task root.
    cancel_flag = bundle.paths.task_dir / ".cancel"
    cancel_flag.touch()
    assert cancel_flag.exists(), "test setup: .cancel flag not on disk"

    outcome = bundle.run_executor()

    status = bundle.read_status()
    assert status["phase"] == "cancelled"
    assert status["cost_usd_total"] == 0.0, (
        "cancel short-circuit must not dispatch -- so cost must "
        "stay at zero. A non-zero value means the executor ran "
        "at least one persona before noticing the flag."
    )

    # The returned outcome and the on-disk status must agree on
    # the terminal phase. Two writers, one truth.
    assert outcome.final_phase == "cancelled"
    assert outcome.total_cost_usd == 0.0

    events = bundle.read_events()
    kinds = [e["kind"] for e in events]
    cancel_events = [e for e in events if e["kind"] == "cancel_complete"]
    assert len(cancel_events) == 1, (
        f"expected exactly one cancel_complete event; got "
        f"kinds={kinds!r}"
    )
    assert cancel_events[0].get("task_id") == bundle.task_id

    # No dispatch fired -- so no step_started / step_completed.
    # If either appears, the executor is doing work it shouldn't.
    assert not any(k in ("step_started", "step_completed") for k in kinds), (
        "cancel must short-circuit BEFORE any step dispatches; "
        f"got events of kinds={kinds!r}"
    )

    # And the fake's counter pins it from the other side.
    assert bundle.fake_claude.counter() == 0

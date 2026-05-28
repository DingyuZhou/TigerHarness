"""Phase B end-to-end scenarios (blocked on Rukawa's executor).

These tests drive the canonical 3-step playbook to completion via
the public ``workflow start`` + executor entry points, then assert
on ``events.jsonl`` + ``status.json``. Every scenario uses the
shared ``e2e_driver`` factory; differences are entirely in the
scripted fake-claude responses.

**State today:** every test in this module is gated on a runtime
import probe of ``tigerharness.workflow_runner.executor``. Until
that module lands, each test is marked ``pytest.skip(...)`` with a
clear message rather than failing — keeps CI green during Wave 3,
and the moment the executor lands, the skips drop and the
scenarios run for real.

When the executor lands, the wire-up changes in exactly two places:
  1. ``_executor_ready()`` in this file returns True.
  2. ``_executor_not_yet_landed()`` in ``conftest.py`` is replaced
     by a thin wrapper around ``executor.run(task_id=...)`` (or
     whatever the executor's public entry point ends up being).

The asserts in each scenario are written to the **spec contract**,
not to executor internals. If the executor honours
``docs/workflow-runner.md`` faithfully, every assertion holds.
"""

from __future__ import annotations

import importlib.util

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
        "tigerharness.workflow_runner.executor is not present yet "
        "(Wave 3 / Rukawa's #4). Phase B scenarios will run once "
        "the executor module lands and conftest's run_executor() "
        "placeholder is replaced with the real wire-up."
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
    assert kinds[-1] in {"task_completed", "task_done"}

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

    history = status["step_history"]
    revise_entries = [h for h in history if h["verdict"] == "REVISE"]
    assert len(revise_entries) == 1
    assert revise_entries[0]["step"] == "02-build"
    assert "scope too big" in (revise_entries[0]["reason"] or "")

    events = bundle.read_events()
    rewind_events = [e for e in events if e["kind"] == "step_rewind"]
    assert any(
        e.get("from") == "02-build" and e.get("to") == "01-plan"
        for e in rewind_events
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
        # iter 1 REVISE -> self-rewind
        {"trailer": "WORKFLOW: REVISE: needs more detail", "cost_usd": 0.05},
        # iter 2 REVISE -> self-rewind
        {"trailer": "WORKFLOW: REVISE: still too thin", "cost_usd": 0.05},
        # iter 3 REVISE -> would breach max_iters
        {"trailer": "WORKFLOW: REVISE: still not good", "cost_usd": 0.05},
        # Defensive: one extra in case the executor counts inclusive
        # vs exclusive differently. If the assert below shows counter=3
        # the executor stopped after iter 3; if counter=4 it ran one
        # more then stopped -- either is plausibly spec-correct;
        # update the assertion when we settle on a convention.
        {"trailer": "WORKFLOW: REVISE: defensive extra", "cost_usd": 0.05},
    ])

    bundle.run_executor()

    status = bundle.read_status()
    assert status["phase"] == "escalated"
    assert status["iter_counts"]["01-plan"] >= 3

    events = bundle.read_events()
    breached = [e for e in events if e["kind"] == "constraint_breached"]
    assert breached, "expected a constraint_breached event"
    assert any(
        "max_loop_iters" in str(e.get("kind_detail") or e.get("detail") or "")
        or e.get("step") == "01-plan"
        for e in breached
    )


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

    events = bundle.read_events()
    parse_failed = [e for e in events if e["kind"] == "verdict_parse_failed"]
    assert len(parse_failed) == 1, (
        "expected exactly one verdict_parse_failed -- if 2+, the "
        "executor is re-prompting too many times"
    )
    assert parse_failed[0]["step"] == "01-plan"

    assert bundle.fake_claude.counter() == 4

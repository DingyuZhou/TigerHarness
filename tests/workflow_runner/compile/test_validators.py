"""Unit tests for ``tigerharness.workflow_runner.compile.validators``.

Each Tier 1 validator gets a happy path, a focused failure path, and
an edge case. The dry-run trace has a verbatim regression pin so any
future change to the trace format trips the suite. Coverage target is
100% line + branch on the validators module (the repo gate enforces
it).
"""

from __future__ import annotations

import pytest

from tigerharness.workflow_runner.compile.validators import (
    SENTINELS,
    ValidationError,
    ValidationResult,
    build_dry_run_trace,
    validate_compile_output,
    validate_cycles,
    validate_refs,
    validate_roster,
    validate_schema,
)
from tigerharness.workflow_runner.models import StepFrontmatter


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def _step(
    step_id: str,
    *,
    persona: str = "anzai",
    role: str = "planner",
    on_approve: str = "__done__",
    on_revise: str = "__escalate__",
    on_block: str = "__escalate__",
    max_iters: int = 5,
    timeout_sec: int = 1800,
) -> StepFrontmatter:
    return StepFrontmatter(
        id=step_id,
        persona=persona,
        role=role,
        on_approve=on_approve,
        on_revise=on_revise,
        on_block=on_block,
        max_iters=max_iters,
        timeout_sec=timeout_sec,
    )


def _canonical() -> list[StepFrontmatter]:
    """A valid 3-step graph: plan -> critique -> doc -> __done__.

    Each step rewinds to the planner on REVISE (loops back to
    ``01-plan``) and escalates on BLOCK. ``01-plan`` also self-loops on
    REVISE. Used by the happy-path tests and the trace regression pin.
    """
    return [
        _step(
            "01-plan",
            persona="anzai",
            role="planner",
            on_approve="02-critique",
            on_revise="01-plan",
            on_block="__escalate__",
            max_iters=5,
        ),
        _step(
            "02-critique",
            persona="akagi",
            role="exec_critic",
            on_approve="03-doc",
            on_revise="01-plan",
            on_block="__escalate__",
            max_iters=5,
        ),
        _step(
            "03-doc",
            persona="ayako",
            role="doc_writer",
            on_approve="__done__",
            on_revise="01-plan",
            on_block="__escalate__",
            max_iters=3,
        ),
    ]


CANON_ROSTER = ["anzai", "akagi", "ayako"]


# --------------------------------------------------------------------------- #
# schema
# --------------------------------------------------------------------------- #


def test_schema_happy():
    assert validate_schema(_canonical()) == []


def test_schema_rejects_duplicate_ids():
    steps = [_step("01-dup"), _step("01-dup")]
    errors = validate_schema(steps)
    assert len(errors) == 1
    err = errors[0]
    assert err.validator == "schema"
    assert err.step_id == "01-dup"
    assert "duplicate" in err.message


def test_schema_catches_post_construction_corruption():
    # Defensive round-trip: a cap mutated below the model floor is
    # caught here even though __post_init__ accepted the original value.
    step = _step("01-corrupt")
    step.max_iters = 0
    errors = validate_schema([step])
    assert len(errors) == 1
    assert errors[0].validator == "schema"
    assert errors[0].step_id == "01-corrupt"
    assert "fails schema" in errors[0].message


def test_schema_handles_non_string_id():
    # A non-str id can only arise from corruption; step_id is reported
    # as None because there is no usable id to name the offender.
    step = _step("01-x")
    step.id = 123  # type: ignore[assignment]
    errors = validate_schema([step])
    assert len(errors) == 1
    assert errors[0].validator == "schema"
    assert errors[0].step_id is None


# --------------------------------------------------------------------------- #
# ref
# --------------------------------------------------------------------------- #


def test_refs_happy():
    assert validate_refs(_canonical(), entrypoint="01-plan") == []


def test_refs_happy_without_entrypoint():
    # entrypoint=None skips the entrypoint resolution check entirely.
    assert validate_refs(_canonical()) == []


def test_refs_rejects_unknown_target():
    steps = [
        _step(
            "01-a",
            on_approve="ghost-step",
            on_revise="01-a",
            on_block="__escalate__",
        )
    ]
    errors = validate_refs(steps)
    assert len(errors) == 1
    assert errors[0].validator == "ref"
    assert errors[0].step_id == "01-a"
    assert "on_approve" in errors[0].message
    assert "ghost-step" in errors[0].message


def test_refs_rejects_unknown_entrypoint():
    errors = validate_refs(_canonical(), entrypoint="not-a-step")
    assert len(errors) == 1
    assert errors[0].validator == "ref"
    assert errors[0].step_id is None
    assert "entrypoint" in errors[0].message


# --------------------------------------------------------------------------- #
# roster
# --------------------------------------------------------------------------- #


def test_roster_happy():
    assert validate_roster(_canonical(), roster=CANON_ROSTER) == []


def test_roster_rejects_unknown_persona():
    steps = [_step("01-a", persona="mitsui")]
    errors = validate_roster(steps, roster=["anzai", "akagi"])
    assert len(errors) == 1
    assert errors[0].validator == "roster"
    assert errors[0].step_id == "01-a"
    assert "mitsui" in errors[0].message


def test_roster_is_case_sensitive():
    # "Anzai" != "anzai"; the roster check mirrors configs/personas.yaml.
    steps = [_step("01-a", persona="Anzai")]
    errors = validate_roster(steps, roster=["anzai"])
    assert len(errors) == 1
    assert errors[0].validator == "roster"


# --------------------------------------------------------------------------- #
# cycle
# --------------------------------------------------------------------------- #


def test_cycles_happy_bounded_loops_pass():
    # The canonical graph is full of REVISE loops, all bounded by
    # max_iters -- so the validator reports no errors.
    assert validate_cycles(_canonical()) == []


def test_cycles_acyclic_graph_passes():
    steps = [
        _step("01-a", on_approve="02-b", on_revise="__escalate__"),
        _step("02-b", on_approve="__done__", on_revise="__escalate__"),
    ]
    assert validate_cycles(steps) == []


def test_cycles_rejects_unbounded_cycle():
    # Defensive: a self-loop whose only member lost its finite cap is
    # an unbounded cycle. (Reachable only via post-construction
    # corruption in the current model, but the check guards a future
    # model where caps may be optional.)
    step = _step("01-loop", on_approve="__done__", on_revise="01-loop")
    step.max_iters = 0
    errors = validate_cycles([step])
    assert len(errors) == 1
    assert errors[0].validator == "cycle"
    assert errors[0].step_id is None
    assert "unbounded" in errors[0].message
    assert "01-loop" in errors[0].message


# --------------------------------------------------------------------------- #
# dry-run trace
# --------------------------------------------------------------------------- #


# Verbatim regression pin for the canonical 3-step graph. Any change to
# the trace format must update this string deliberately.
EXPECTED_CANON_TRACE = "\n".join(
    [
        "workflow dry-run trace",
        "======================",
        "entrypoint: 01-plan",
        "steps (3): 01-plan, 02-critique, 03-doc",
        "",
        "happy path (all APPROVE):",
        "  01-plan -> 02-critique -> 03-doc -> __done__",
        "",
        "routing detail:",
        "  01-plan [anzai/planner] max_iters=5:",
        "    on_approve -> 02-critique",
        "    on_revise  -> 01-plan (self-loop; at most 5 re-entries)",
        "    on_block   -> __escalate__ (escalate)",
        "  02-critique [akagi/exec_critic] max_iters=5:",
        "    on_approve -> 03-doc",
        "    on_revise  -> 01-plan (loop back; at most 5 re-entries)",
        "    on_block   -> __escalate__ (escalate)",
        "  03-doc [ayako/doc_writer] max_iters=3:",
        "    on_approve -> __done__ (done)",
        "    on_revise  -> 01-plan (loop back; at most 5 re-entries)",
        "    on_block   -> __escalate__ (escalate)",
        "",
        "loops:",
        "  {01-plan, 02-critique, 03-doc} -- bounded",
        "  self-loop on 01-plan -- bounded",
    ]
)


def test_trace_canonical_verbatim():
    assert build_dry_run_trace(_canonical()) == EXPECTED_CANON_TRACE


def test_trace_empty_graph():
    assert build_dry_run_trace([]) == (
        "workflow dry-run trace\n"
        "======================\n"
        "(no steps to trace)"
    )


def test_trace_explicit_entrypoint():
    trace = build_dry_run_trace(_canonical(), entrypoint="02-critique")
    assert "entrypoint: 02-critique" in trace
    # From 02-critique the whole graph is still reachable (REVISE edges
    # rewind to 01-plan), so there is no unreachable section.
    assert "unreachable" not in trace


def test_trace_unknown_target_and_unreachable_steps():
    steps = [
        _step(
            "01-a",
            on_approve="ghost",  # dangling forward edge
            on_revise="__escalate__",
            on_block="__escalate__",
        ),
        _step(
            "02-orphan",  # never referenced -> unreachable
            on_approve="__done__",
            on_revise="__escalate__",
            on_block="__escalate__",
        ),
    ]
    trace = build_dry_run_trace(steps)
    assert "01-a -> ghost (unknown)" in trace
    assert "on_approve -> ghost (unknown target)" in trace
    assert "unreachable from entrypoint:\n  02-orphan" in trace
    assert "loops:\n  (none)" in trace


def test_trace_happy_path_self_loop_on_approve():
    # on_approve pointing back at the step itself makes the happy path
    # loop; the walk annotates and breaks instead of spinning.
    steps = [
        _step(
            "01-spin",
            on_approve="01-spin",
            on_revise="__escalate__",
            on_block="__escalate__",
        )
    ]
    trace = build_dry_run_trace(steps)
    assert "01-spin -> 01-spin (loops back)" in trace
    assert "self-loop on 01-spin -- bounded" in trace


def test_trace_bad_entrypoint_marks_all_unreachable():
    trace = build_dry_run_trace(_canonical(), entrypoint="zzz-missing")
    assert "happy path (all APPROVE):\n  zzz-missing (unknown)" in trace
    # Nothing is reachable from a non-existent entrypoint.
    assert "unreachable from entrypoint:" in trace
    for sid in ("01-plan", "02-critique", "03-doc"):
        assert sid in trace.split("unreachable from entrypoint:")[1]


def test_trace_unbounded_loop_flagged():
    step = _step("01-loop", on_approve="__done__", on_revise="01-loop")
    step.max_iters = 0
    trace = build_dry_run_trace([step])
    assert "self-loop on 01-loop -- UNBOUNDED" in trace


def test_trace_fan_in_to_earlier_terminal_is_a_known_soft_label():
    # Characterization pin for the documented `_edge_line` limitation.
    # Diamond graph, NO cycle:  01-a -> {02-b, 04-d};  02-b -> 03-c;
    # 03-c -> 04-d;  04-d -> __done__. BFS discovers 04-d (via 01-a's
    # on_revise) before 03-c, so the fan-in edge 03-c -> 04-d points at
    # an earlier-discovered node. The BFS-order heuristic therefore
    # annotates it "loop back" even though 04-d is a terminal that can
    # never re-reach 03-c. The authoritative `loops:` section correctly
    # reports none. This asserts BOTH, documenting that the inline
    # annotation is a readability hint, not a cycle decision. See
    # `_edge_line.__doc__` for why reachability is NOT the fix; if a
    # DFS active-stack back-edge classifier ever lands, update this pin.
    steps = [
        _step("01-a", on_approve="02-b", on_revise="04-d", on_block="__escalate__"),
        _step("02-b", on_approve="03-c", on_revise="__escalate__", on_block="__escalate__"),
        _step("03-c", on_approve="04-d", on_revise="__escalate__", on_block="__escalate__"),
        _step("04-d", on_approve="__done__", on_revise="__escalate__", on_block="__escalate__"),
    ]
    trace = build_dry_run_trace(steps)
    # The heuristic mislabels the acyclic fan-in edge:
    assert "-> 04-d (loop back; at most 5 re-entries)" in trace
    # ...but the rigorous cycle pass sees no loop at all:
    assert "loops:\n  (none)" in trace


# --------------------------------------------------------------------------- #
# validate_compile_output (orchestrator)
# --------------------------------------------------------------------------- #


def test_compile_output_happy():
    result = validate_compile_output(_canonical(), roster=CANON_ROSTER)
    assert isinstance(result, ValidationResult)
    assert result.ok is True
    assert result.errors == []
    assert "happy path (all APPROVE):" in result.trace


def test_compile_output_explicit_entrypoint():
    result = validate_compile_output(
        _canonical(), roster=CANON_ROSTER, entrypoint="01-plan"
    )
    assert result.ok is True
    assert "entrypoint: 01-plan" in result.trace


def test_compile_output_empty_graph():
    result = validate_compile_output([], roster=[])
    assert result.ok is False
    assert any(
        e.validator == "schema" and "no steps" in e.message
        for e in result.errors
    )
    assert result.trace.endswith("(no steps to trace)")


def test_compile_output_collects_errors_across_validators():
    # One step with both a bad ref target and an off-roster persona:
    # the orchestrator must report BOTH, not stop at the first.
    steps = [
        _step(
            "01-x",
            persona="ghost",
            on_approve="nowhere",
            on_revise="01-x",
            on_block="__escalate__",
        )
    ]
    result = validate_compile_output(steps, roster=["anzai"])
    assert result.ok is False
    validators = {e.validator for e in result.errors}
    assert "ref" in validators
    assert "roster" in validators
    # The trace is always built, even on failure.
    assert "routing detail:" in result.trace


def test_validation_error_is_frozen():
    err = ValidationError("schema", "01-x", "boom")
    with pytest.raises(Exception):
        err.message = "changed"  # type: ignore[misc]


def test_sentinels_constant():
    assert SENTINELS == frozenset({"__done__", "__escalate__"})

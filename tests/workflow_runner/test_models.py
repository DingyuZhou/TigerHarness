"""Unit tests for ``tigerharness.workflow_runner.models``."""

from __future__ import annotations

import datetime as dt

import pytest

from tigerharness.workflow_runner import models as models_mod
from tigerharness.workflow_runner.models import (
    Event,
    Orchestration,
    SessionMap,
    Status,
    StepEdges,
    StepFrontmatter,
    StepHistoryEntry,
    WorkflowConfig,
    WorkflowModelError,
    now_iso,
)


# Convenient sample data ----------------------------------------------------

GOOD_TS = "2026-05-28T14:00:00Z"


def _good_frontmatter(**overrides):
    base = {
        "id": "01-plan",
        "persona": "anzai",
        "role": "planner",
        "on_approve": "02-critique",
        "on_revise": "01-plan",
        "on_block": "__escalate__",
        "max_iters": 5,
        "timeout_sec": 1800,
        "parallel_with": [],
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# now_iso
# --------------------------------------------------------------------------- #


def test_now_iso_shape():
    out = now_iso()
    assert out.endswith("Z")
    # parseable
    dt.datetime.fromisoformat(out.replace("Z", "+00:00"))


# --------------------------------------------------------------------------- #
# StepFrontmatter
# --------------------------------------------------------------------------- #


def test_step_frontmatter_round_trip():
    raw = _good_frontmatter()
    fm = StepFrontmatter.from_dict(raw)
    assert fm.to_dict() == raw
    # Edges accessor mirrors fields
    assert fm.edges == StepEdges(
        on_approve="02-critique",
        on_revise="01-plan",
        on_block="__escalate__",
    )


@pytest.mark.parametrize(
    "field, bad",
    [
        ("id", ""),
        ("persona", 42),
        ("role", None),
        ("on_approve", ""),
        ("on_revise", ""),
        ("on_block", ""),
    ],
)
def test_step_frontmatter_rejects_bad_strings(field, bad):
    raw = _good_frontmatter(**{field: bad})
    with pytest.raises(WorkflowModelError):
        StepFrontmatter.from_dict(raw)


@pytest.mark.parametrize(
    "field, bad",
    [
        ("max_iters", 0),
        ("max_iters", -1),
        ("max_iters", "five"),
        ("max_iters", True),  # bool must not pass as int
        ("timeout_sec", 0),
        ("timeout_sec", -10),
    ],
)
def test_step_frontmatter_rejects_bad_ints(field, bad):
    raw = _good_frontmatter(**{field: bad})
    with pytest.raises(WorkflowModelError):
        StepFrontmatter.from_dict(raw)


def test_step_frontmatter_rejects_missing_keys():
    raw = _good_frontmatter()
    raw.pop("on_block")
    with pytest.raises(WorkflowModelError) as exc:
        StepFrontmatter.from_dict(raw)
    assert "on_block" in str(exc.value)


def test_step_frontmatter_rejects_non_dict():
    with pytest.raises(WorkflowModelError):
        StepFrontmatter.from_dict("not a dict")  # type: ignore[arg-type]


def test_step_frontmatter_parallel_with_validation():
    raw = _good_frontmatter(parallel_with=["sib-a", "sib-b"])
    fm = StepFrontmatter.from_dict(raw)
    assert fm.parallel_with == ["sib-a", "sib-b"]
    bad = _good_frontmatter(parallel_with=["", "x"])
    with pytest.raises(WorkflowModelError):
        StepFrontmatter.from_dict(bad)


def test_step_frontmatter_parallel_with_must_be_list():
    raw = _good_frontmatter(parallel_with="not-a-list")
    with pytest.raises(WorkflowModelError):
        StepFrontmatter.from_dict(raw)


def test_step_frontmatter_parallel_with_default_when_omitted():
    raw = _good_frontmatter()
    raw.pop("parallel_with")
    fm = StepFrontmatter.from_dict(raw)
    assert fm.parallel_with == []


def test_step_frontmatter_parallel_with_explicit_none():
    """An explicit ``None`` (e.g. from YAML's ``parallel_with: ``) is
    treated the same as a missing key."""
    raw = _good_frontmatter(parallel_with=None)
    fm = StepFrontmatter.from_dict(raw)
    assert fm.parallel_with == []


# --------------------------------------------------------------------------- #
# StepEdges
# --------------------------------------------------------------------------- #


def test_step_edges_round_trip():
    raw = {"on_approve": "a", "on_revise": "b", "on_block": "c"}
    edges = StepEdges.from_dict(raw)
    assert edges.to_dict() == raw


def test_step_edges_rejects_non_dict():
    with pytest.raises(WorkflowModelError):
        StepEdges.from_dict([])  # type: ignore[arg-type]


def test_step_edges_rejects_missing():
    with pytest.raises(WorkflowModelError) as exc:
        StepEdges.from_dict({"on_approve": "a", "on_revise": "b"})
    assert "on_block" in str(exc.value)


def test_step_edges_rejects_bad_string():
    with pytest.raises(WorkflowModelError):
        StepEdges(on_approve="", on_revise="b", on_block="c")


# --------------------------------------------------------------------------- #
# WorkflowConfig
# --------------------------------------------------------------------------- #


def test_workflow_config_defaults():
    wc = WorkflowConfig()
    assert wc.human_gate is True
    assert wc.max_compile_iters == 8
    assert wc.max_cost_usd == 10.0
    assert wc.max_loop_iters == 5
    assert wc.step_timeout_sec == 1800
    assert wc.max_task_wall_sec == 86400
    assert wc.allow_parallel is False
    assert wc.human_gate_approvers == []


def test_workflow_config_from_dict_none_yields_defaults():
    assert WorkflowConfig.from_dict(None).to_dict() == WorkflowConfig().to_dict()


def test_workflow_config_round_trip():
    raw = {
        "human_gate": False,
        "max_compile_iters": 3,
        "max_cost_usd": 1.0,
        "max_loop_iters": 2,
        "step_timeout_sec": 30,
        "max_task_wall_sec": 60,
        "allow_parallel": True,
        "human_gate_approvers": ["U1", "U2"],
    }
    wc = WorkflowConfig.from_dict(raw)
    assert wc.to_dict() == raw


def test_workflow_config_partial_overrides_keep_defaults():
    wc = WorkflowConfig.from_dict({"max_loop_iters": 9})
    assert wc.max_loop_iters == 9
    assert wc.step_timeout_sec == 1800  # default preserved


def test_workflow_config_rejects_non_dict():
    with pytest.raises(WorkflowModelError):
        WorkflowConfig.from_dict("hi")  # type: ignore[arg-type]


def test_workflow_config_rejects_bad_human_gate():
    with pytest.raises(WorkflowModelError):
        WorkflowConfig(human_gate="yes")  # type: ignore[arg-type]


def test_workflow_config_rejects_bad_allow_parallel():
    with pytest.raises(WorkflowModelError):
        WorkflowConfig(allow_parallel=1)  # type: ignore[arg-type]


def test_workflow_config_rejects_bad_ints():
    with pytest.raises(WorkflowModelError):
        WorkflowConfig(max_compile_iters=0)
    with pytest.raises(WorkflowModelError):
        WorkflowConfig(max_loop_iters=-1)
    with pytest.raises(WorkflowModelError):
        WorkflowConfig(step_timeout_sec=0)
    with pytest.raises(WorkflowModelError):
        WorkflowConfig(max_task_wall_sec=-5)


def test_workflow_config_rejects_negative_cost():
    with pytest.raises(WorkflowModelError):
        WorkflowConfig(max_cost_usd=-0.01)


def test_workflow_config_rejects_non_number_cost():
    with pytest.raises(WorkflowModelError):
        WorkflowConfig(max_cost_usd="lots")  # type: ignore[arg-type]


def test_workflow_config_rejects_bad_approvers():
    with pytest.raises(WorkflowModelError):
        WorkflowConfig(human_gate_approvers=[""])
    with pytest.raises(WorkflowModelError):
        WorkflowConfig(human_gate_approvers="U1")  # type: ignore[arg-type]


def test_workflow_config_rejects_bool_as_int():
    """bool is an int subclass in Python; we reject it explicitly so that
    True doesn't sneak in as a count."""
    with pytest.raises(WorkflowModelError):
        WorkflowConfig(max_compile_iters=True)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def _good_orchestration(**overrides):
    base = {
        "task_id": "20260528-foo-aaaaaaaa",
        "team": "Shohoku",
        "playbook": "default",
        "playbook_sha256": "deadbeef",
        "steps": ["01-plan", "02-critique"],
        "entrypoint": "01-plan",
        "compiled_at": GOOD_TS,
        "compiled_by": "anzai",
        "edges": {
            "01-plan": {
                "on_approve": "02-critique",
                "on_revise": "01-plan",
                "on_block": "__escalate__",
            },
            "02-critique": {
                "on_approve": "__done__",
                "on_revise": "01-plan",
                "on_block": "__escalate__",
            },
        },
        "workflow_config": None,
        "compile_critique_iters": 3,
    }
    base.update(overrides)
    return base


def test_orchestration_round_trip():
    raw = _good_orchestration()
    o = Orchestration.from_dict(raw)
    redumped = o.to_dict()
    assert redumped["task_id"] == raw["task_id"]
    assert redumped["steps"] == raw["steps"]
    assert redumped["edges"]["01-plan"] == raw["edges"]["01-plan"]
    # workflow_config default expands to a full dict
    assert redumped["workflow_config"]["human_gate"] is True


def test_orchestration_rejects_non_dict():
    with pytest.raises(WorkflowModelError):
        Orchestration.from_dict([])  # type: ignore[arg-type]


def test_orchestration_rejects_bad_edges_container():
    bad = _good_orchestration(edges=["not", "a", "dict"])
    with pytest.raises(WorkflowModelError):
        Orchestration.from_dict(bad)


def test_orchestration_rejects_dup_steps():
    bad = _good_orchestration(
        steps=["01-plan", "01-plan"],
        edges={"01-plan": {
            "on_approve": "01-plan", "on_revise": "01-plan",
            "on_block": "__escalate__",
        }},
    )
    with pytest.raises(WorkflowModelError) as exc:
        Orchestration.from_dict(bad)
    assert "duplicate" in str(exc.value).lower()


def test_orchestration_entrypoint_must_be_in_steps():
    bad = _good_orchestration(entrypoint="99-missing")
    with pytest.raises(WorkflowModelError):
        Orchestration.from_dict(bad)


def test_orchestration_edges_must_reference_known_steps():
    bad = _good_orchestration(
        edges={
            "01-plan": {
                "on_approve": "02-critique",
                "on_revise": "01-plan",
                "on_block": "__escalate__",
            },
            "ghost": {
                "on_approve": "01-plan",
                "on_revise": "01-plan",
                "on_block": "__escalate__",
            },
            "02-critique": {
                "on_approve": "__done__",
                "on_revise": "01-plan",
                "on_block": "__escalate__",
            },
        }
    )
    with pytest.raises(WorkflowModelError) as exc:
        Orchestration.from_dict(bad)
    assert "ghost" in str(exc.value)


def test_orchestration_rejects_bad_compiled_at():
    bad = _good_orchestration(compiled_at="not-iso")
    with pytest.raises(WorkflowModelError):
        Orchestration.from_dict(bad)


def test_orchestration_rejects_bad_critique_iters():
    bad = _good_orchestration(compile_critique_iters=-1)
    with pytest.raises(WorkflowModelError):
        Orchestration.from_dict(bad)


def test_orchestration_edges_entry_must_be_stepedges():
    # Construct directly to exercise the type-check path in __post_init__.
    base = _good_orchestration()
    o = Orchestration.from_dict(base)
    with pytest.raises(WorkflowModelError):
        Orchestration(
            task_id=o.task_id,
            team=o.team,
            playbook=o.playbook,
            playbook_sha256=o.playbook_sha256,
            steps=o.steps,
            entrypoint=o.entrypoint,
            compiled_at=o.compiled_at,
            compiled_by=o.compiled_by,
            edges={"01-plan": "not-a-stepedges"},  # type: ignore[dict-item]
            workflow_config=o.workflow_config,
        )


def test_orchestration_edges_dict_must_be_dict_direct_ctor():
    base = _good_orchestration()
    o = Orchestration.from_dict(base)
    with pytest.raises(WorkflowModelError):
        Orchestration(
            task_id=o.task_id,
            team=o.team,
            playbook=o.playbook,
            playbook_sha256=o.playbook_sha256,
            steps=o.steps,
            entrypoint=o.entrypoint,
            compiled_at=o.compiled_at,
            compiled_by=o.compiled_by,
            edges="nope",  # type: ignore[arg-type]
        )


# --------------------------------------------------------------------------- #
# StepHistoryEntry
# --------------------------------------------------------------------------- #


def _good_history(**overrides):
    base = {
        "step": "01-plan",
        "iter": 1,
        "persona": "anzai",
        "started_at": GOOD_TS,
        "ended_at": GOOD_TS,
        "verdict": "APPROVE",
        "reason": None,
        "cost_usd": 0.5,
    }
    base.update(overrides)
    return base


def test_step_history_entry_round_trip():
    raw = _good_history()
    e = StepHistoryEntry.from_dict(raw)
    assert e.to_dict() == raw


def test_step_history_entry_rejects_non_dict():
    with pytest.raises(WorkflowModelError):
        StepHistoryEntry.from_dict("not a dict")  # type: ignore[arg-type]


def test_step_history_entry_optional_ended_at_and_verdict():
    raw = _good_history(ended_at=None, verdict=None)
    e = StepHistoryEntry.from_dict(raw)
    assert e.ended_at is None
    assert e.verdict is None


def test_step_history_entry_rejects_bad_verdict():
    raw = _good_history(verdict="MAYBE")
    with pytest.raises(WorkflowModelError):
        StepHistoryEntry.from_dict(raw)


def test_step_history_entry_rejects_bad_reason_type():
    with pytest.raises(WorkflowModelError):
        StepHistoryEntry(
            step="x", iter=1, persona="p",
            started_at=GOOD_TS,
            reason=123,  # type: ignore[arg-type]
        )


def test_step_history_entry_rejects_bad_iter():
    raw = _good_history(iter=0)
    with pytest.raises(WorkflowModelError):
        StepHistoryEntry.from_dict(raw)


def test_step_history_entry_rejects_bad_started_at():
    raw = _good_history(started_at="nope")
    with pytest.raises(WorkflowModelError):
        StepHistoryEntry.from_dict(raw)


def test_step_history_entry_rejects_bad_ended_at():
    raw = _good_history(ended_at="nope")
    with pytest.raises(WorkflowModelError):
        StepHistoryEntry.from_dict(raw)


def test_step_history_entry_rejects_negative_cost():
    raw = _good_history(cost_usd=-0.01)
    with pytest.raises(WorkflowModelError):
        StepHistoryEntry.from_dict(raw)


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #


def _good_status(**overrides):
    base = {
        "task_id": "t1",
        "phase": "execute",
        "started_at": GOOD_TS,
        "current_step": "01-plan",
        "current_iter": 1,
        "step_started_at": GOOD_TS,
        "iter_counts": {"01-plan": 1},
        "cost_usd_total": 0.5,
        "cost_usd_per_step": {"01-plan": 0.5},
        "step_history": [],
        "phase_state": {"compile_passed_tier1": True},
        "last_heartbeat": GOOD_TS,
        "escalation": None,
    }
    base.update(overrides)
    return base


def test_status_round_trip_minimal():
    raw = {
        "task_id": "t1",
        "phase": "compile",
        "started_at": GOOD_TS,
    }
    s = Status.from_dict(raw)
    redumped = s.to_dict()
    assert redumped["task_id"] == "t1"
    assert redumped["phase"] == "compile"
    assert redumped["current_step"] is None
    assert redumped["current_iter"] == 0
    assert redumped["iter_counts"] == {}
    assert redumped["step_history"] == []


def test_status_round_trip_with_history():
    raw = _good_status(
        step_history=[_good_history()],
    )
    s = Status.from_dict(raw)
    assert len(s.step_history) == 1
    assert isinstance(s.step_history[0], StepHistoryEntry)
    redumped = s.to_dict()
    assert redumped["step_history"][0]["verdict"] == "APPROVE"


def test_status_rejects_bad_phase():
    raw = _good_status(phase="exploding")
    with pytest.raises(WorkflowModelError):
        Status.from_dict(raw)


def test_status_rejects_non_dict():
    with pytest.raises(WorkflowModelError):
        Status.from_dict("hi")  # type: ignore[arg-type]


def test_status_rejects_bad_started_at():
    raw = _good_status(started_at="ages-ago")
    with pytest.raises(WorkflowModelError):
        Status.from_dict(raw)


def test_status_rejects_bad_step_started_at():
    raw = _good_status(step_started_at="ages-ago")
    with pytest.raises(WorkflowModelError):
        Status.from_dict(raw)


def test_status_rejects_bad_last_heartbeat():
    raw = _good_status(last_heartbeat="never")
    with pytest.raises(WorkflowModelError):
        Status.from_dict(raw)


def test_status_rejects_bad_escalation_type():
    with pytest.raises(WorkflowModelError):
        Status(
            task_id="t1", phase="execute", started_at=GOOD_TS,
            escalation=42,  # type: ignore[arg-type]
        )


def test_status_rejects_negative_iter():
    raw = _good_status(current_iter=-1)
    with pytest.raises(WorkflowModelError):
        Status.from_dict(raw)


def test_status_rejects_bad_current_step_type():
    with pytest.raises(WorkflowModelError):
        Status(
            task_id="t1", phase="execute", started_at=GOOD_TS,
            current_step=42,  # type: ignore[arg-type]
        )


def test_status_rejects_bad_iter_counts_container():
    with pytest.raises(WorkflowModelError):
        Status(
            task_id="t1", phase="execute", started_at=GOOD_TS,
            iter_counts="nope",  # type: ignore[arg-type]
        )


def test_status_rejects_bad_iter_count_key():
    with pytest.raises(WorkflowModelError):
        Status(
            task_id="t1", phase="execute", started_at=GOOD_TS,
            iter_counts={"": 1},
        )


def test_status_rejects_bad_iter_count_value():
    with pytest.raises(WorkflowModelError):
        Status(
            task_id="t1", phase="execute", started_at=GOOD_TS,
            iter_counts={"x": -1},
        )


def test_status_rejects_bad_cost_total():
    with pytest.raises(WorkflowModelError):
        Status(
            task_id="t1", phase="execute", started_at=GOOD_TS,
            cost_usd_total=-1.0,
        )


def test_status_rejects_bad_cost_per_step_container():
    with pytest.raises(WorkflowModelError):
        Status(
            task_id="t1", phase="execute", started_at=GOOD_TS,
            cost_usd_per_step="nope",  # type: ignore[arg-type]
        )


def test_status_rejects_bad_cost_per_step_key():
    with pytest.raises(WorkflowModelError):
        Status(
            task_id="t1", phase="execute", started_at=GOOD_TS,
            cost_usd_per_step={"": 1.0},
        )


def test_status_rejects_bad_cost_per_step_value():
    with pytest.raises(WorkflowModelError):
        Status(
            task_id="t1", phase="execute", started_at=GOOD_TS,
            cost_usd_per_step={"x": -1.0},
        )


def test_status_rejects_bad_step_history_container():
    with pytest.raises(WorkflowModelError):
        Status(
            task_id="t1", phase="execute", started_at=GOOD_TS,
            step_history="not-a-list",  # type: ignore[arg-type]
        )


def test_status_rejects_bad_step_history_member():
    with pytest.raises(WorkflowModelError):
        Status(
            task_id="t1", phase="execute", started_at=GOOD_TS,
            step_history=["not-an-entry"],  # type: ignore[list-item]
        )


def test_status_rejects_bad_phase_state_container():
    with pytest.raises(WorkflowModelError):
        Status(
            task_id="t1", phase="execute", started_at=GOOD_TS,
            phase_state="bad",  # type: ignore[arg-type]
        )


# --------------------------------------------------------------------------- #
# SessionMap
# --------------------------------------------------------------------------- #


def test_session_map_round_trip():
    raw = {"anzai": "sess-abc", "ayako": "sess-xyz"}
    sm = SessionMap.from_dict(raw)
    assert sm.to_dict() == raw
    assert "anzai" in sm
    assert sm.get("anzai") == "sess-abc"
    assert sm.get("missing") is None
    assert len(sm) == 2


def test_session_map_set():
    sm = SessionMap()
    sm.set("anzai", "sess-1")
    assert sm.to_dict() == {"anzai": "sess-1"}


def test_session_map_set_rejects_bad_values():
    sm = SessionMap()
    with pytest.raises(WorkflowModelError):
        sm.set("", "sess-1")
    with pytest.raises(WorkflowModelError):
        sm.set("anzai", "")


def test_session_map_from_dict_none():
    sm = SessionMap.from_dict(None)
    assert sm.to_dict() == {}


def test_session_map_rejects_non_dict():
    with pytest.raises(WorkflowModelError):
        SessionMap.from_dict("nope")  # type: ignore[arg-type]


def test_session_map_rejects_bad_values_at_init():
    with pytest.raises(WorkflowModelError):
        SessionMap(sessions={"persona": 42})  # type: ignore[dict-item]


def test_session_map_rejects_non_dict_sessions_field():
    with pytest.raises(WorkflowModelError):
        SessionMap(sessions=[])  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Event
# --------------------------------------------------------------------------- #


def test_event_round_trip_flat():
    raw = {"ts": GOOD_TS, "kind": "step_started", "step": "01", "iter": 1}
    e = Event.from_dict(raw)
    assert e.ts == GOOD_TS
    assert e.kind == "step_started"
    assert e.extra == {"step": "01", "iter": 1}
    assert e.to_dict() == raw


def test_event_rejects_non_dict():
    with pytest.raises(WorkflowModelError):
        Event.from_dict("hi")  # type: ignore[arg-type]


def test_event_requires_ts_and_kind():
    with pytest.raises(WorkflowModelError):
        Event.from_dict({"kind": "x"})
    with pytest.raises(WorkflowModelError):
        Event.from_dict({"ts": GOOD_TS})


def test_event_rejects_reserved_keys_in_extra():
    with pytest.raises(WorkflowModelError):
        Event(ts=GOOD_TS, kind="step_started", extra={"ts": "shadow"})


def test_event_rejects_non_dict_extra():
    with pytest.raises(WorkflowModelError):
        Event(ts=GOOD_TS, kind="x", extra=[])  # type: ignore[arg-type]


def test_event_rejects_bad_ts():
    with pytest.raises(WorkflowModelError):
        Event(ts="never", kind="x")


# --------------------------------------------------------------------------- #
# Private helpers (cover edge branches that aren't reachable via public API
# parameterisation).
# --------------------------------------------------------------------------- #


def test_require_str_rejects_non_string():
    with pytest.raises(WorkflowModelError):
        models_mod._require_str(123, "field")


def test_require_str_allows_empty_when_flagged():
    assert models_mod._require_str("", "field", allow_empty=True) == ""


def test_require_positive_int_rejects_float():
    with pytest.raises(WorkflowModelError):
        models_mod._require_positive_int(1.5, "field")  # type: ignore[arg-type]


def test_require_non_negative_int_rejects_bool():
    with pytest.raises(WorkflowModelError):
        models_mod._require_non_negative_int(True, "field")  # type: ignore[arg-type]


def test_require_non_negative_number_rejects_bool():
    with pytest.raises(WorkflowModelError):
        models_mod._require_non_negative_number(True, "field")  # type: ignore[arg-type]


def test_require_list_of_str_rejects_non_list():
    with pytest.raises(WorkflowModelError):
        models_mod._require_list_of_str("a-string", "field")

"""Phase A integration: scripted fake claude driven by SessionManager.

These tests close the last gap in Phase A confidence.
``test_phase_a_fixtures.py`` proves the scripted fake binary works
when invoked directly as a subprocess. But the executor (Rukawa's
#4) will not invoke ``claude`` directly -- it will go through
:class:`tigerharness.workflow_runner.sessions.SessionManager`. So
the *real* "ready for executor" claim is: the scripted fake works
when driven by the SessionManager.

If these tests pass, the only Phase 1 piece that still needs to land
before Phase B can run end-to-end is the executor's loop logic
itself. Every other moving part -- session id rotation, cost
extraction, trailer-shaped envelope, ``sessions.json`` persistence,
``--resume`` continuity -- is already wired and verified here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tigerharness.workflow_runner.sessions import SessionManager
from tigerharness.journal.wfcore.trailer import parse_trailer


@pytest.fixture
def task_dir(tmp_path: Path) -> Path:
    """Per-test task directory hosting ``sessions.json``."""
    d = tmp_path / "task"
    d.mkdir()
    return d


def test_session_manager_drives_scripted_fake_three_calls(
    e2e_fake_claude, e2e_personas_dir, task_dir: Path
) -> None:
    """Three persona invocations against a 3-response script.

    Mirrors what the executor will do once #4 lands: invoke persona,
    parse trailer, route, invoke next persona. We don't route here
    (that's the executor's job), but we do prove that every other
    seam holds.
    """
    e2e_fake_claude.set_script([
        {
            "trailer": "WORKFLOW: APPROVE",
            "body": "anzai approves the plan.",
            "cost_usd": 0.11,
            "session_id": "sid-anzai-1",
            "persona": "anzai",
            "iter": 1,
        },
        {
            "trailer": "WORKFLOW: APPROVE",
            "body": "akagi reports the build is clean.",
            "cost_usd": 0.07,
            "session_id": "sid-akagi-1",
            "persona": "akagi",
            "iter": 1,
        },
        {
            "trailer": "WORKFLOW: APPROVE",
            "body": "rukawa signs off on the review.",
            "cost_usd": 0.05,
            "session_id": "sid-rukawa-1",
            "persona": "rukawa",
            "iter": 1,
        },
    ])

    mgr = SessionManager(task_dir)

    # First persona: fresh session, no --resume; SessionManager will
    # add the persona system prompt to argv on this first call.
    r1 = mgr.invoke("anzai", "draft a plan", timeout_sec=10)
    assert r1.exit_code == 0
    assert r1.session_id == "sid-anzai-1"
    assert r1.cost_usd == pytest.approx(0.11)
    v1 = parse_trailer(r1.stdout)
    assert v1.kind == "APPROVE"

    r2 = mgr.invoke("akagi", "build per the plan", timeout_sec=10)
    assert r2.session_id == "sid-akagi-1"
    assert r2.cost_usd == pytest.approx(0.07)
    assert parse_trailer(r2.stdout).kind == "APPROVE"

    r3 = mgr.invoke("rukawa", "review the build", timeout_sec=10)
    assert r3.session_id == "sid-rukawa-1"
    assert r3.cost_usd == pytest.approx(0.05)
    assert parse_trailer(r3.stdout).kind == "APPROVE"

    # sessions.json now carries all three per-persona ids -- this is
    # exactly the resume material the executor needs on iteration N+1.
    saved = json.loads((task_dir / "sessions.json").read_text())
    assert saved == {
        "anzai": "sid-anzai-1",
        "akagi": "sid-akagi-1",
        "rukawa": "sid-rukawa-1",
    }

    # All three script entries consumed; no over-run.
    assert e2e_fake_claude.counter() == 3


def test_session_manager_resume_path_echoes_existing_sid(
    e2e_fake_claude, e2e_personas_dir, task_dir: Path
) -> None:
    """Second call for the same persona resumes the stored sid.

    Critical for the rewind scenario: when step 02-build REVISEs to
    step 01-plan, the executor will re-invoke ``anzai`` -- and that
    invocation must carry ``--resume <stored-sid>`` so anzai sees
    iter-2 in the same session memory as iter-1. This test pins the
    SessionManager half of that contract end-to-end.
    """
    # Script entries omit ``session_id`` so the fake echoes whatever
    # ``--resume`` carries -- modelling a real claude rotation that
    # keeps the same sid.
    e2e_fake_claude.set_script([
        {"trailer": "WORKFLOW: REVISE: needs more detail", "cost_usd": 0.10},
        {"trailer": "WORKFLOW: APPROVE", "cost_usd": 0.08},
    ])

    mgr = SessionManager(task_dir)

    # First call mints a fresh sid (fake's "sid-fresh-1").
    r1 = mgr.invoke("anzai", "draft the plan", timeout_sec=10)
    assert r1.session_id == "sid-fresh-1"
    v1 = parse_trailer(r1.stdout)
    assert v1.kind == "REVISE"
    assert "more detail" in v1.summary

    # Second call: SessionManager should pass --resume sid-fresh-1
    # on argv; the scripted fake echoes it back; SessionManager keeps
    # sessions.json pointed at the same id (no rotation).
    r2 = mgr.invoke("anzai", "iter 2 with feedback prologue", timeout_sec=10)
    assert r2.session_id == "sid-fresh-1", (
        "fake should have echoed the resumed sid, proving SessionManager "
        "passed --resume on the second invocation"
    )
    assert parse_trailer(r2.stdout).kind == "APPROVE"

    saved = json.loads((task_dir / "sessions.json").read_text())
    assert saved == {"anzai": "sid-fresh-1"}, (
        "no rotation expected when fake echoes the resumed sid"
    )


def test_session_manager_captures_total_cost(
    e2e_fake_claude, e2e_personas_dir, task_dir: Path
) -> None:
    """SessionManager hands the executor a cost it can sum into status.

    The brief's linear-path scenario asserts "cost matches sum of
    fake costs". That's only meaningful if SessionManager actually
    extracts ``total_cost_usd`` from each envelope and surfaces it
    on the result. Pin that here.
    """
    e2e_fake_claude.set_script([
        {"trailer": "WORKFLOW: APPROVE", "cost_usd": 0.42},
    ])
    mgr = SessionManager(task_dir)
    result = mgr.invoke("anzai", "go", timeout_sec=10)
    assert result.cost_usd == pytest.approx(0.42)

"""B3 team-sweep gating (sweep.py) — staleness floor + soft-lease claim."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from tigerharness.tiger_memory import sweep
from tigerharness.tiger_memory.sweep import (
    read_sweep_state,
    sweep_due,
    sweep_state_path,
    try_claim_sweep,
    mark_sweep_complete,
    write_sweep_state,
)

NOW = datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)


def _iso(delta_hours: float) -> str:
    return (NOW - timedelta(hours=delta_hours)).isoformat()


# ----- state IO ------------------------------------------------------------


def test_read_missing_state_is_empty(tmp_path: Path) -> None:
    assert read_sweep_state(tmp_path) == {}


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    team = tmp_path / "memories"  # does not exist yet -> mkdir
    write_sweep_state(team, {"last_sweep_at": "x", "k": 1})
    assert sweep_state_path(team).exists()
    assert read_sweep_state(team) == {"last_sweep_at": "x", "k": 1}


def test_read_malformed_json_is_empty(tmp_path: Path) -> None:
    sweep_state_path(tmp_path).write_text("{not json", encoding="utf-8")
    assert read_sweep_state(tmp_path) == {}


def test_read_non_dict_json_is_empty(tmp_path: Path) -> None:
    sweep_state_path(tmp_path).write_text("[1, 2, 3]", encoding="utf-8")
    assert read_sweep_state(tmp_path) == {}


# ----- staleness floor -----------------------------------------------------


def test_sweep_due_never_swept() -> None:
    assert sweep_due(None, NOW) is True


def test_sweep_due_unparseable_watermark() -> None:
    assert sweep_due("not-a-timestamp", NOW) is True


def test_sweep_due_elapsed_past_floor() -> None:
    assert sweep_due(_iso(25), NOW, floor_hours=24) is True
    assert sweep_due(_iso(24), NOW, floor_hours=24) is True  # boundary


def test_sweep_not_due_within_floor() -> None:
    assert sweep_due(_iso(1), NOW, floor_hours=24) is False


# ----- soft-lease claim ----------------------------------------------------


def test_claim_succeeds_on_fresh_team(tmp_path: Path) -> None:
    res = try_claim_sweep(tmp_path, now=NOW, token="sess-A")
    assert res.claimed is True and res.reason == "claimed"
    state = read_sweep_state(tmp_path)
    assert state["claim_token"] == "sess-A"
    assert state["claim_at"] == NOW.isoformat()


def test_claim_not_due_within_floor(tmp_path: Path) -> None:
    write_sweep_state(tmp_path, {"last_sweep_at": _iso(1)})
    res = try_claim_sweep(tmp_path, now=NOW, token="sess-A")
    assert res.claimed is False and res.reason == "not_due"


def test_claim_busy_when_other_holds_fresh_claim(tmp_path: Path) -> None:
    write_sweep_state(tmp_path, {"claim_token": "sess-A", "claim_at": _iso(0.1)})
    res = try_claim_sweep(tmp_path, now=NOW, token="sess-B")
    assert res.claimed is False and res.reason == "busy"


def test_claim_steals_stale_claim(tmp_path: Path) -> None:
    # Owner crashed ~1h ago; lease is 1800s -> stale, stealable.
    write_sweep_state(tmp_path, {"claim_token": "sess-A", "claim_at": _iso(1)})
    res = try_claim_sweep(tmp_path, now=NOW, token="sess-B")
    assert res.claimed is True and res.reason == "claimed"
    assert read_sweep_state(tmp_path)["claim_token"] == "sess-B"


def test_claim_reentrant_same_token(tmp_path: Path) -> None:
    write_sweep_state(tmp_path, {"claim_token": "sess-A", "claim_at": _iso(0.1)})
    res = try_claim_sweep(tmp_path, now=NOW, token="sess-A")
    assert res.claimed is True and res.reason == "claimed"


# ----- completion ----------------------------------------------------------


def test_mark_complete_advances_watermark_and_clears_claim(tmp_path: Path) -> None:
    try_claim_sweep(tmp_path, now=NOW, token="sess-A")
    later = NOW + timedelta(minutes=5)
    mark_sweep_complete(tmp_path, later)
    state = read_sweep_state(tmp_path)
    assert state["last_sweep_at"] == later.isoformat()
    assert "claim_token" not in state and "claim_at" not in state
    # The freshly-bumped watermark gates the next trigger.
    assert try_claim_sweep(tmp_path, now=later, token="sess-B").reason == "not_due"


def test_module_exposes_defaults() -> None:
    assert sweep.DEFAULT_STALENESS_FLOOR_HOURS == 24.0
    assert sweep.DEFAULT_LEASE_SECONDS == 1800.0

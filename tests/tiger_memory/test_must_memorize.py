"""Tests for must_memorize scoring + decay + persistence."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from tigerharness.tiger_memory import must_memorize as mm
from tigerharness.tiger_memory.must_memorize import (
    KIND_OWNER_EXPLICIT,
    KIND_DECISION,
    KIND_INCIDENT,
    KIND_PREFERENCE,
    Row,
    decay_all,
    load,
    merge_candidates,
    parse_extractor_output,
    save,
)
from tigerharness.tiger_memory.store import Store


# ----- parse extractor output ----------------------------------------------


def test_parse_none() -> None:
    assert parse_extractor_output("NONE") == []
    assert parse_extractor_output("") == []


def test_parse_single_block() -> None:
    out = parse_extractor_output(
        "KIND: owner_explicit\n"
        "MEMO: Never push --force to main.\n"
    )
    assert len(out) == 1
    assert out[0].kind == KIND_OWNER_EXPLICIT
    assert out[0].locked is True
    assert "Never push --force" in out[0].memo


def test_parse_multiple_blocks() -> None:
    out = parse_extractor_output(
        "KIND: incident\nMEMO: 64 KB buffer overflow.\n\n"
        "KIND: preference\nMEMO: Form-encoding for Slack uploads.\n"
    )
    assert len(out) == 2
    assert out[0].kind == KIND_INCIDENT
    assert out[1].kind == KIND_PREFERENCE
    assert out[1].locked is False


# ----- merge / repeat-detection --------------------------------------------


def test_merge_new_candidate_added() -> None:
    rows: list[Row] = []
    cand = Row(kind=KIND_INCIDENT, memo="64 KB buffer overflow on bridge.")
    kept, demoted = merge_candidates(
        rows, [cand], today="2026-05-14",
        similarity_threshold=0.7, max_rows=60,
    )
    assert len(kept) == 1
    assert kept[0].last_bump == "2026-05-14"
    assert demoted == []


def test_merge_near_duplicate_bumps_existing() -> None:
    rows = [
        Row(kind=KIND_INCIDENT, memo="64 KB buffer overflow on bridge.",
            score=5, last_bump="2026-05-10", last_decay="2026-05-10"),
    ]
    cand = Row(kind=KIND_INCIDENT, memo="64 KB buffer overflow on bridge!")
    kept, _ = merge_candidates(
        rows, [cand], today="2026-05-14",
        similarity_threshold=0.7, max_rows=60,
    )
    assert len(kept) == 1
    assert kept[0].score == 6
    assert kept[0].last_bump == "2026-05-14"


def test_merge_promotes_to_owner_explicit() -> None:
    rows = [Row(kind=KIND_PREFERENCE, memo="Pre-commit hooks are mandatory.")]
    cand = Row(kind=KIND_OWNER_EXPLICIT, memo="Pre-commit hooks are mandatory!",
               locked=True)
    kept, _ = merge_candidates(
        rows, [cand], today="2026-05-14",
        similarity_threshold=0.7, max_rows=60,
    )
    assert kept[0].kind == KIND_OWNER_EXPLICIT
    assert kept[0].locked is True


def test_merge_caps_at_max_rows() -> None:
    rows = [
        Row(kind=KIND_PREFERENCE, memo=f"pref {i}", score=2)
        for i in range(5)
    ]
    cand = Row(kind=KIND_OWNER_EXPLICIT, memo="locked top", locked=True)
    kept, demoted = merge_candidates(
        rows, [cand], today="2026-05-14",
        similarity_threshold=0.7, max_rows=3,
    )
    assert len(kept) == 3
    assert len(demoted) == 3
    # owner_explicit always survives the cap
    assert any(r.kind == KIND_OWNER_EXPLICIT for r in kept)


# ----- decay ---------------------------------------------------------------


def test_decay_skips_locked() -> None:
    rows = [Row(kind=KIND_OWNER_EXPLICIT, memo="lock", locked=True, score=5,
                last_bump="2025-01-01", last_decay="2025-01-01")]
    out = decay_all(rows, today="2026-05-14",
                    days_per_point={"preference": 7, "decision": 14,
                                    "incident": 28})
    assert out[0].score == 5  # unchanged


def test_decay_preference_seven_days_per_point() -> None:
    # 14 days elapsed → -2 points
    rows = [Row(kind=KIND_PREFERENCE, memo="m", score=5,
                last_bump="2026-05-01", last_decay="2026-05-01")]
    out = decay_all(rows, today="2026-05-15",
                    days_per_point={"preference": 7, "decision": 14,
                                    "incident": 28})
    assert out[0].score == 3


def test_decay_incident_twenty_eight_days() -> None:
    # 28 days elapsed → -1 point for incident
    rows = [Row(kind=KIND_INCIDENT, memo="m", score=5,
                last_bump="2026-04-16", last_decay="2026-04-16")]
    out = decay_all(rows, today="2026-05-14",
                    days_per_point={"preference": 7, "decision": 14,
                                    "incident": 28})
    assert out[0].score == 4


def test_decay_removes_zero_or_negative() -> None:
    rows = [Row(kind=KIND_PREFERENCE, memo="m", score=2,
                last_bump="2026-01-01", last_decay="2026-01-01")]
    out = decay_all(rows, today="2026-05-14",
                    days_per_point={"preference": 7, "decision": 14,
                                    "incident": 28})
    assert out == []


def test_decay_idempotent_across_back_to_back_rebuilds() -> None:
    rows = [Row(kind=KIND_PREFERENCE, memo="m", score=10,
                last_bump="2026-05-01", last_decay="2026-05-01")]
    today = "2026-05-08"  # 7 days → 1 point
    decay_all(rows, today=today,
              days_per_point={"preference": 7, "decision": 14, "incident": 28})
    score_after_first = rows[0].score
    # Re-run on same day — no further decay
    decay_all(rows, today=today,
              days_per_point={"preference": 7, "decision": 14, "incident": 28})
    assert rows[0].score == score_after_first


# ----- persistence roundtrip ----------------------------------------------


def test_save_load_roundtrip(tmp_path: Path) -> None:
    store = Store(tmp_path / "mem")
    store.init_layout()
    rows = [
        Row(kind=KIND_OWNER_EXPLICIT, memo="Never --force.", locked=True,
            score=5, last_bump="2026-05-14", source="CEO"),
        Row(kind=KIND_INCIDENT, memo="Buffer overflow.", score=5,
            last_bump="2026-05-13", source="extract"),
    ]
    save(store, rows)
    loaded = load(store)
    assert len(loaded) == 2
    # Sort puts locked first.
    assert loaded[0].locked is True
    assert "Never --force" in loaded[0].memo


def test_load_returns_empty_when_no_file(tmp_path: Path) -> None:
    store = Store(tmp_path / "mem")
    store.init_layout()
    assert load(store) == []

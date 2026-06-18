"""Tests for the shared recency/date-math helpers (ranking.py; plan §2 dev-2)."""
from __future__ import annotations

from tigerharness.tiger_memory.ranking import (
    _parse_iso,
    days_between,
    recency_score,
)


def test_parse_iso_with_trailing_z() -> None:
    dt = _parse_iso("2026-06-17T00:00:00Z")
    assert dt is not None and dt.year == 2026


def test_parse_iso_without_z_assumes_utc() -> None:
    dt = _parse_iso("2026-06-17T00:00:00")
    assert dt is not None and dt.tzinfo is not None


def test_parse_iso_with_explicit_offset() -> None:
    dt = _parse_iso("2026-06-17T00:00:00+00:00")
    assert dt is not None and dt.tzinfo is not None


def test_parse_iso_empty_is_none() -> None:
    assert _parse_iso("") is None


def test_parse_iso_garbage_is_none() -> None:
    assert _parse_iso("not-a-date") is None


def test_days_between_simple() -> None:
    assert days_between("2026-06-07T00:00:00Z", "2026-06-17T00:00:00Z") == 10.0


def test_days_between_fractional() -> None:
    got = days_between("2026-06-17T00:00:00Z", "2026-06-17T12:00:00Z")
    assert got == 0.5


def test_days_between_negative_span_floored() -> None:
    # end precedes start -> 0 (decay never grows a magnitude).
    assert days_between("2026-06-17T00:00:00Z", "2026-06-07T00:00:00Z") == 0.0


def test_days_between_equal_is_zero() -> None:
    assert days_between("2026-06-17T00:00:00Z", "2026-06-17T00:00:00Z") == 0.0


def test_days_between_unparseable_is_zero() -> None:
    assert days_between("", "2026-06-17T00:00:00Z") == 0.0
    assert days_between("2026-06-17T00:00:00Z", "bad") == 0.0


def test_recency_score_fresher_is_higher() -> None:
    now = "2026-06-17T00:00:00Z"
    fresh = recency_score(now, now)
    old = recency_score("2026-01-01T00:00:00Z", now)
    assert fresh > old


def test_recency_score_unparseable_is_neg_inf() -> None:
    assert recency_score("garbage", "2026-06-17T00:00:00Z") == float("-inf")

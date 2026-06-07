"""Unit tests for the thin rebuild metrics hook."""
from __future__ import annotations

from tigerharness.tiger_memory.metrics import RebuildMetrics


def test_empty_metrics_reduction_is_zero() -> None:
    m = RebuildMetrics()
    assert m.chars_saved == 0
    assert m.reduction_pct == 0.0  # raw <= 0 guard
    assert m.as_dict() == {
        "sessions_processed": 0,
        "summarize_calls": 0,
        "content_chars_raw": 0,
        "content_chars_filtered": 0,
        "content_chars_saved": 0,
        "prefilter_reduction_pct": 0.0,
    }


def test_record_session_accumulates() -> None:
    m = RebuildMetrics()
    m.record_session(chars_raw=1000, chars_filtered=600, calls=3)
    m.record_session(chars_raw=500, chars_filtered=500, calls=2)
    assert m.sessions_processed == 2
    assert m.summarize_calls == 5
    assert m.content_chars_raw == 1500
    assert m.content_chars_filtered == 1100
    assert m.chars_saved == 400


def test_reduction_pct_rounds_to_one_decimal() -> None:
    m = RebuildMetrics()
    m.record_session(chars_raw=1000, chars_filtered=667, calls=3)
    # saved 333 / 1000 = 33.3%
    assert m.reduction_pct == 33.3
    d = m.as_dict()
    assert d["content_chars_saved"] == 333
    assert d["prefilter_reduction_pct"] == 33.3

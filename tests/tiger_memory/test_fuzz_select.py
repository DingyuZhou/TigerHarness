"""Tests for diary fuzz-candidate selection (4-store model, b1-dev-2/Rukawa).

Covers the fresh-window guard, the under-bound no-op, and the over-bound route-
lowest-aged-to-fuzz behaviour (no hard drop) to 100% branch coverage.
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from tigerharness.tiger_memory import fuzz_select as fs
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.entries import DiaryEntry

NOW = "2026-06-19T00:00:00Z"


def _cfg(tmp_path: Path, *, max_length: int = 120, fresh_days: int = 7):
    p = tmp_path / "cfg.yaml"
    p.write_text(dedent(f"""\
        agent: {{name: Anzai, role: r}}
        store: {{root: {tmp_path}/m}}
        sources: [{{kind: claude_code, project_path: {tmp_path}/p/}}]
        summarizer: {{backend: anthropic, model: m, prompts: default/v1}}
        memory:
          diary: {{max_length: {max_length}, overflow_limit: {max_length + 2000}, fresh_days: {fresh_days}}}
    """))
    return load_config(p)


def _d(id_, day, weight, text="padded note here for length padding padding"):
    ts = f"2026-06-{day:02d}T00:00:00Z"
    return DiaryEntry(id=id_, text=text, created_at=ts, last_used=ts,
                      source="diary", weight=weight)


def test_under_bound_keeps_all(tmp_path: Path):
    cfg = _cfg(tmp_path, max_length=4000)
    entries = [_d("a", 1, 1.0), _d("b", 19, 2.0)]
    kept, fuzzed = fs.select_diary_fuzz(entries, NOW, cfg)
    assert kept == entries and fuzzed == []


def test_all_fresh_never_fuzzed_even_over_bound(tmp_path: Path):
    cfg = _cfg(tmp_path, max_length=50, fresh_days=7)
    # both dated within 7 days of 06-19 → fresh → protected even over the 50-char bound.
    entries = [_d("a", 18, 0.0), _d("b", 19, 1.0)]
    kept, fuzzed = fs.select_diary_fuzz(entries, NOW, cfg)
    assert {e.id for e in kept} == {"a", "b"} and fuzzed == []


def test_over_bound_routes_lowest_aged_to_fuzz(tmp_path: Path):
    cfg = _cfg(tmp_path, max_length=80, fresh_days=7)
    # three AGED items (all dated 06-01..06-03, > 7 days before 06-19), total over 80.
    entries = [
        _d("weak", 1, 1.0),
        _d("mid", 2, 5.0),
        _d("strong", 3, 9.0),
    ]
    kept, fuzzed = fs.select_diary_fuzz(entries, NOW, cfg)
    # lowest |weight| fuzzed first; strongest survives; result fits the bound.
    assert "strong" in {e.id for e in kept}
    assert "weak" in {e.id for e in fuzzed}
    assert fs._diary_len(kept) <= 80
    # no hard drop: every input is either kept or fuzzed.
    assert {e.id for e in kept} | {e.id for e in fuzzed} == {"weak", "mid", "strong"}


def test_fresh_protected_aged_fuzzed(tmp_path: Path):
    cfg = _cfg(tmp_path, max_length=80, fresh_days=7)
    # one fresh low-weight (06-19) + two aged; over bound -> aged fuzz first, fresh stays.
    entries = [_d("fresh", 19, 0.0), _d("aged1", 1, 1.0), _d("aged2", 2, 2.0)]
    kept, fuzzed = fs.select_diary_fuzz(entries, NOW, cfg)
    assert "fresh" in {e.id for e in kept}
    assert {e.id for e in fuzzed} and all(i.startswith("aged") for i in {e.id for e in fuzzed})

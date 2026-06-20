"""Tests for diary fuzz-candidate selection (4-store model, b1-dev-2/Rukawa).

Covers the fresh-window guard, the under-bound no-op, and the over-bound route-
lowest-aged-to-fuzz behaviour (no hard drop) to 100% branch coverage.
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from tigerharness.tiger_memory import fuzz_select as fs
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.entries import DiaryEntry, MustRememberEntry

NOW = "2026-06-19T00:00:00Z"


def _cfg(tmp_path: Path, *, max_length: int = 120, fresh_days: int = 7,
         mr_max: int = 8000):
    p = tmp_path / "cfg.yaml"
    p.write_text(dedent(f"""\
        agent: {{name: Anzai, role: r}}
        store: {{root: {tmp_path}/m}}
        sources: [{{kind: claude_code, project_path: {tmp_path}/p/}}]
        summarizer: {{backend: anthropic, model: m, prompts: default/v1}}
        memory:
          diary: {{max_length: {max_length}, overflow_limit: {max_length + 2000}, fresh_days: {fresh_days}}}
          must_remember: {{max_length: {mr_max}, overflow_limit: {mr_max + 2000}}}
    """))
    return load_config(p)


def _mr(id_, day, importance, repeat_count=1, text="a must-remember fact padded padded"):
    ts = f"2026-06-{day:02d}T00:00:00Z"
    return MustRememberEntry(id=id_, text=text, created_at=ts, last_used=ts,
                             source="s", kind="preference",
                             importance=importance, repeat_count=repeat_count)


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


# ----- must_remember fuzz-selection (4-store model) ------------------------

def test_mr_downgraded_always_fuzzed(tmp_path: Path):
    cfg = _cfg(tmp_path, mr_max=8000)  # well under bound
    entries = [_mr("a", 1, 5.0), _mr("b", 2, 3.0)]
    kept, fuzzed = fs.select_mr_fuzz(entries, {"b"}, NOW, cfg)
    assert {e.id for e in fuzzed} == {"b"}  # downgraded fuzzed even under bound
    assert {e.id for e in kept} == {"a"}


def test_mr_under_bound_no_extra_fuzz(tmp_path: Path):
    cfg = _cfg(tmp_path, mr_max=8000)
    entries = [_mr("a", 1, 5.0), _mr("b", 2, 3.0)]
    kept, fuzzed = fs.select_mr_fuzz(entries, set(), NOW, cfg)
    assert fuzzed == [] and {e.id for e in kept} == {"a", "b"}


def test_mr_over_bound_routes_lowest_repeat_count(tmp_path: Path):
    cfg = _cfg(tmp_path, mr_max=80)  # small -> over bound
    entries = [
        _mr("rare", 1, 1.0, repeat_count=1),
        _mr("some", 2, 3.0, repeat_count=3),
        _mr("often", 3, 9.0, repeat_count=9),
    ]
    kept, fuzzed = fs.select_mr_fuzz(entries, set(), NOW, cfg)
    assert "often" in {e.id for e in kept}      # most-reinforced survives
    assert "rare" in {e.id for e in fuzzed}     # least-reinforced fuzzed first
    assert fs._mr_len(kept) <= 80
    assert {e.id for e in kept} | {e.id for e in fuzzed} == {"rare", "some", "often"}


def test_mr_downgraded_plus_over_bound(tmp_path: Path):
    cfg = _cfg(tmp_path, mr_max=80)
    entries = [_mr("dn", 5, 5.0), _mr("a", 1, 1.0), _mr("b", 2, 9.0, repeat_count=9)]
    kept, fuzzed = fs.select_mr_fuzz(entries, {"dn"}, NOW, cfg)
    assert "dn" in {e.id for e in fuzzed}       # downgraded always fuzzed
    assert "b" in {e.id for e in kept}          # strongest survives


def test_mr_all_fuzzed_when_far_over_bound(tmp_path: Path):
    # bound so tiny even one item exceeds it -> every item routes to fuzz (the
    # loop exhausts without an early break); nothing is dropped.
    cfg = _cfg(tmp_path, mr_max=10)
    entries = [_mr("a", 1, 1.0), _mr("b", 2, 2.0)]
    kept, fuzzed = fs.select_mr_fuzz(entries, set(), NOW, cfg)
    assert kept == [] and {e.id for e in fuzzed} == {"a", "b"}

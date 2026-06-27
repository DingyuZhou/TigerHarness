"""Tests for the 4-store persona meditation orchestrator (b1-dev-1 integration).

The centerpiece is the no-hard-drop invariant (plan §7): meditation never
deletes — every sharp-store item ends up kept OR routed into fuzzy.md. Also
covers the fresh-window guard, fuzzy convergence across repeated cycles, and
lock back-off. Uses a mock summarizer (no live model).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from textwrap import dedent

from tigerharness.tiger_memory import fuzzy_store
from tigerharness.tiger_memory.bounded_store import BoundedStore
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.entries import (
    STORE_DIARY,
    STORE_MUST_REMEMBER,
    DiaryEntry,
    MustRememberEntry,
)
from tigerharness.tiger_memory.meditation import meditate_persona
from tigerharness.tiger_memory.store import Store
from tigerharness.tiger_memory.summarizers.base import Summarizer

MISSION = "ship the 4-store memory model"


class MockSummarizer(Summarizer):
    """No merge / no downgrade; entry-compaction never shrinks (force the fuzz
    path); re-compaction returns a fixed coarse blob."""
    name = "mock"
    version = "v1"

    def summarize(self, *, prompt: str, max_words: int) -> str:
        if "YES" in prompt and "NO" in prompt:        # a judgment prompt
            return "NO"
        if "Compact the older memory" in prompt:       # the re-compaction call
            return "## Fuzzy\n- coarse fuzzy gist of older memory\n"
        return "X" * 2000                              # entry-compaction: no shrink


def _store(tmp_path: Path, *, dmax=120, mmax=120, fresh=7):
    p = tmp_path / "cfg.yaml"
    p.write_text(dedent(f"""\
        agent: {{name: Anzai, role: r}}
        store: {{root: {tmp_path}/m}}
        sources: [{{kind: claude_code, project_path: {tmp_path}/p/}}]
        summarizer: {{backend: anthropic, model: m, prompts: default/v1}}
        memory:
          diary: {{max_length: {dmax}, overflow_limit: {dmax + 2000}, fresh_days: {fresh}}}
          must_remember: {{max_length: {mmax}, overflow_limit: {mmax + 2000}}}
          fuzzy: {{max_length: 4000, overflow_limit: 6000}}
    """))
    cfg = load_config(p)
    store = Store(cfg.store.root)
    store.init_layout()
    return cfg, store, BoundedStore(cfg, store)


def _d(id_, day, weight, text):
    ts = f"2026-06-{day:02d}T00:00:00Z"
    return DiaryEntry(id=id_, text=text, created_at=ts, last_used=ts,
                      source="diary", weight=weight)


def _m(id_, day, text):
    ts = f"2026-06-{day:02d}T00:00:00Z"
    return MustRememberEntry(id=id_, text=text, created_at=ts, last_used=ts,
                             source="s", kind="preference")


def test_no_hard_drop_invariant(tmp_path: Path):
    cfg, store, bs = _store(tmp_path, dmax=90, mmax=90)
    diary = [_d("d1", 1, 1.0, "alpha aged diary note padding padding"),
             _d("d2", 2, 5.0, "bravo aged diary note padding padding"),
             _d("d3", 3, 9.0, "charlie strong aged note padding padding")]
    mr = [_m("m1", 1, "delta aged fact padding padding padding"),
          _m("m2", 2, "echo aged fact padding padding padding")]
    bs.save_atomic(STORE_DIARY, diary)
    bs.save_atomic(STORE_MUST_REMEMBER, mr)

    res = meditate_persona("ctx", MISSION, MockSummarizer(), cfg, bs)
    assert not res.skipped_locked

    # Content-based (the compact diary format regenerates ids on load; text is
    # the stable key). THE INVARIANT: every input is kept sharp OR routed to
    # fuzzy — nothing deleted.
    kept_diary = {e.text for e in bs.load(STORE_DIARY)}
    kept_mr = {e.text for e in bs.load(STORE_MUST_REMEMBER)}
    diary_texts = {e.text for e in diary}
    mr_texts = {e.text for e in mr}
    assert kept_diary | set(res.fuzzed_diary) == diary_texts
    assert kept_mr | set(res.fuzzed_mr) == mr_texts
    assert not (kept_diary & set(res.fuzzed_diary))   # kept xor fuzzed
    assert not (kept_mr & set(res.fuzzed_mr))
    assert res.fuzzed_diary or res.fuzzed_mr          # something aged out (over bound)
    assert "coarse fuzzy gist" in fuzzy_store.load_fuzzy(store)
    assert bs.length_chars(bs.load(STORE_DIARY)) <= 90


def test_fresh_window_kept_sharp(tmp_path: Path):
    cfg, store, bs = _store(tmp_path, dmax=60, fresh=7)
    # Dates are anchored RELATIVE to now: the fresh-window guard compares
    # last_used against the real wall clock (fuzz_select.days_between(.., now)),
    # so hard-coded calendar dates silently age out of the window as time
    # passes. Both entries sit 1-2 days back -> always fresh -> never fuzzed
    # even though the store is over the 60-char bound.
    now = datetime.now(timezone.utc)

    def _fresh(id_, days_ago, weight, text):
        ts = (now - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return DiaryEntry(id=id_, text=text, created_at=ts, last_used=ts,
                          source="diary", weight=weight)

    bs.save_atomic(STORE_DIARY, [
        _fresh("f1", 2, 0.0, "fresh zero-weight note padding padding"),
        _fresh("f2", 1, 1.0, "fresh recent note padding padding"),
    ])
    res = meditate_persona("ctx", MISSION, MockSummarizer(), cfg, bs)
    kept = {e.text for e in bs.load(STORE_DIARY)}
    assert kept == {"fresh zero-weight note padding padding",
                    "fresh recent note padding padding"}
    assert res.fuzzed_diary == []


def test_fuzzy_converges_over_repeated_cycles(tmp_path: Path):
    cfg, store, bs = _store(tmp_path, dmax=90)
    for r in range(3):
        bs.save_atomic(STORE_DIARY, [
            _d(f"r{r}a", 1, 1.0, "aged note A padding padding padding"),
            _d(f"r{r}b", 2, 2.0, "aged note B padding padding padding"),
            _d(f"r{r}c", 3, 3.0, "aged note C padding padding padding"),
        ])
        meditate_persona("ctx", MISSION, MockSummarizer(), cfg, bs)
        assert len(fuzzy_store.load_fuzzy(store)) <= cfg.memory.fuzzy.max_length


def test_skips_when_store_locked(tmp_path: Path):
    cfg, store, bs = _store(tmp_path)
    bs.save_atomic(STORE_DIARY, [_d("d1", 1, 1.0, "note")])
    with bs.store_lock(STORE_DIARY):           # a live session holds the diary lock
        res = meditate_persona("ctx", MISSION, MockSummarizer(), cfg, bs)
    assert res.skipped_locked is True

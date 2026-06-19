"""B2 QA-defense for the 4-store meditation (Sakuragi).

Attacks the assumed-away edges of meditate_persona that the dev tests don't yet
exercise: the FULL pipeline running merge + relevance-downgrade + fuzz together,
a downgraded operator directive routing to fuzzy (not dropped), idempotent
re-run stability, and unicode/multibyte length in the fuzzy bound (CHARACTERS,
not bytes). All under a scripted mock summarizer — zero live-model calls.
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from tigerharness.tiger_memory import fuzzy_store
from tigerharness.tiger_memory.bounded_store import BoundedStore
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.entries import (
    KIND_OPERATOR_EXPLICIT,
    KIND_PREFERENCE,
    DiaryEntry,
    MustRememberEntry,
)
from tigerharness.tiger_memory.meditation import meditate_persona
from tigerharness.tiger_memory.store import Store
from tigerharness.tiger_memory.summarizers.base import Summarizer

MISSION = "ship the 4-store memory model"


class B2Summarizer(Summarizer):
    """Handles all four meditate_persona prompt types: similarity, stale,
    entry-compaction (no shrink), and fuzzy re-compaction (fixed blob)."""
    name = "b2"
    version = "v1"

    def __init__(self, *, similar_pairs=(), stale_texts=()):
        super().__init__()
        self.similar_pairs = {frozenset(p) for p in similar_pairs}
        self.stale_texts = set(stale_texts)

    def summarize(self, *, prompt: str, max_words: int) -> str:
        if prompt.startswith("Are these two memory entries"):
            a = prompt.split("ENTRY A:\n", 1)[1].split("\n\nENTRY B:", 1)[0]
            b = prompt.split("ENTRY B:\n", 1)[1].rstrip("\n")
            return "YES" if frozenset({a, b}) in self.similar_pairs else "NO"
        if "STALE" in prompt:
            d = prompt.split("DIRECTIVE:\n", 1)[1].rstrip("\n")
            return "YES" if d in self.stale_texts else "NO"
        if "Compact the older memory" in prompt:
            return "## Fuzzy\n- coarse gist of older memory\n"
        # entry-compaction: return body unchanged (no shrink -> force fuzz path).
        return prompt.split("Return ONLY the rewritten text.\n\n", 1)[1].rstrip("\n")


def _store(tmp_path: Path, *, dmax=90, mmax=90, fresh=7, fuzzy_max=4000):
    p = tmp_path / "cfg.yaml"
    p.write_text(dedent(f"""\
        agent: {{name: Sakuragi, role: qa}}
        store: {{root: {tmp_path}/m}}
        sources: [{{kind: claude_code, project_path: {tmp_path}/p/}}]
        summarizer: {{backend: anthropic, model: m, prompts: default/v1}}
        memory:
          diary: {{max_length: {dmax}, overflow_limit: {dmax + 2000}, fresh_days: {fresh}}}
          must_remember: {{max_length: {mmax}, overflow_limit: {mmax + 2000}}}
          fuzzy: {{max_length: {fuzzy_max}, overflow_limit: {fuzzy_max + 2000}}}
    """))
    cfg = load_config(p)
    store = Store(cfg.store.root)
    store.init_layout()
    return cfg, store, BoundedStore(cfg, store)


def _d(id_, day, w, text):
    ts = f"2026-06-{day:02d}T00:00:00Z"
    return DiaryEntry(id=id_, text=text, created_at=ts, last_used=ts, source="diary", weight=w)


def _m(id_, day, text, kind=KIND_PREFERENCE):
    ts = f"2026-06-{day:02d}T00:00:00Z"
    return MustRememberEntry(id=id_, text=text, created_at=ts, last_used=ts,
                             source="s", kind=kind)


def test_full_pipeline_merge_downgrade_fuzz(tmp_path: Path):
    cfg, store, bs = _store(tmp_path, mmax=60)
    stale = "old goal directive padding padding padding"
    bs.save_atomic("must_remember", [
        _m("p1", 1, "use tabs not spaces here padding"),
        _m("p2", 2, "use tabs not spaces here padding"),   # dup of p1 -> merge
        _m("op", 3, stale, kind=KIND_OPERATOR_EXPLICIT),    # stale -> downgrade
    ])
    bs.save_atomic("diary", [_d("d1", 1, 1.0, "aged diary note padding padding")])
    summ = B2Summarizer(similar_pairs=[("use tabs not spaces here padding",
                                        "use tabs not spaces here padding")],
                        stale_texts=[stale])
    res = meditate_persona("ctx", MISSION, summ, cfg, bs)
    # merge happened (p1/p2 folded), downgrade happened (op), and the downgraded
    # directive routed to fuzzy (not deleted).
    assert res.must_remember.merged
    assert res.must_remember.downgraded
    assert stale in res.fuzzed_mr
    assert "coarse gist" in fuzzy_store.load_fuzzy(store)
    # the downgraded directive is no longer in the sharp store but is captured.
    assert stale not in {e.text for e in bs.load("must_remember")}


def test_downgraded_directive_not_hard_dropped(tmp_path: Path):
    cfg, store, bs = _store(tmp_path, mmax=8000)  # under bound: only downgrade drives fuzz
    stale = "directive tied to a shipped goal"
    bs.save_atomic("must_remember", [
        _m("op", 1, stale, kind=KIND_OPERATOR_EXPLICIT),
        _m("keep", 2, "still relevant preference"),
    ])
    res = meditate_persona("ctx", MISSION, B2Summarizer(stale_texts=[stale]), cfg, bs)
    kept = {e.text for e in bs.load("must_remember")}
    assert stale in res.fuzzed_mr and stale not in kept   # fuzzed, not deleted
    assert "still relevant preference" in kept             # relevant one stays


def test_meditate_persona_idempotent_rerun(tmp_path: Path):
    cfg, store, bs = _store(tmp_path)
    bs.save_atomic("diary", [
        _d("a", 1, 1.0, "aged note A padding padding padding"),
        _d("b", 2, 2.0, "aged note B padding padding padding"),
        _d("c", 3, 9.0, "aged note C padding padding padding"),
    ])
    r1 = meditate_persona("ctx", MISSION, B2Summarizer(), cfg, bs)
    r2 = meditate_persona("ctx", MISSION, B2Summarizer(), cfg, bs)
    assert not r1.skipped_locked and not r2.skipped_locked
    # stable: fuzzy stays bounded, diary stays bounded, no crash.
    assert len(fuzzy_store.load_fuzzy(store)) <= cfg.memory.fuzzy.max_length
    assert bs.length_chars(bs.load("diary")) <= cfg.memory.diary.max_length


def test_fuzzy_bound_counts_characters_not_bytes(tmp_path: Path):
    # multibyte chars: 'é' is 2 bytes but 1 char; the bound is CHARACTERS.
    cfg, store, bs = _store(tmp_path, fuzzy_max=10)
    fuzzy_store.save_fuzzy(cfg, store, "é" * 50)  # 50 chars, 100 bytes
    text = fuzzy_store.load_fuzzy(store)
    assert len(text) <= 10           # characters, not bytes
    assert all(ch == "é" for ch in text)

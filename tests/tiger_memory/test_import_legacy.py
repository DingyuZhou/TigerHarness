"""Tests for the legacy-import seed-write + idempotency foundation (b1-dev-1).

Covers ``import_legacy.py``: the seed-writer (append, no re-refresh/re-stamp,
provenance assertion, no-double-seed), the two idempotency guards (the
``.state.json`` marker AND detect-existing-seed), and the read-before-drop
ordering helpers. Mock-free — pure store I/O.
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from tigerharness.tiger_memory.bounded_store import BoundedStore
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.entries import (
    EmotionalEntry,
    MustRememberEntry,
    SkillEntry,
)
from tigerharness.tiger_memory.import_legacy import (
    IMPORT_SOURCE,
    STATE_KEY,
    DoubleSeedError,
    already_imported,
    assert_seed_inputs_snapshotted,
    has_seeded_entries,
    mark_imported,
    seed_entries,
    seeds_perform_no_deletion,
)
from tigerharness.tiger_memory.lifecycle import Candidates
from tigerharness.tiger_memory.store import Store

SRC = "2025-01-01T00:00:00Z"  # a backdated source date


def _make_store(tmp_path: Path) -> tuple[Store, BoundedStore]:
    p = tmp_path / "cfg.yaml"
    p.write_text(
        dedent(
            f"""\
            agent:
              name: Mitsui
              role: data-integrity
            store:
              root: {tmp_path}/memory
            sources:
              - kind: claude_code
                project_path: {tmp_path}/p/
            summarizer:
              backend: anthropic
              model: m
              prompts: default/v1
            memory:
              skills:
                max_count: 40
                overflow_limit: 50
              must_remember:
                max_length: 8000
                overflow_limit: 10000
              emotional_log:
                max_length: 12000
                overflow_limit: 15000
                weight_cap: 10
                decay:
                  magnitude_per_day: 0.1
            """
        )
    )
    cfg = load_config(p)
    store = Store(cfg.store.root)
    store.init_layout()
    return store, BoundedStore(cfg, store)


def _skill(src=IMPORT_SOURCE, name="run the suite"):
    return SkillEntry(
        text="always run the full test suite before pushing",
        created_at=SRC, last_used=SRC, source=src,
        name=name, trigger="before push", procedure="uv run pytest", usage_count=1,
    )


def _mr(src=IMPORT_SOURCE):
    return MustRememberEntry(
        text="never push without approval", created_at=SRC, last_used=SRC,
        source=src, kind="owner_explicit", importance=5.0,
    )


def _emo(src=IMPORT_SOURCE, weight=3.0):
    return EmotionalEntry(
        text="shipping the revamp felt great", created_at=SRC, last_used=SRC,
        source=src, weight=weight, reaction="proud",
    )


def _cands(skills=(), must=(), emo=()):
    return Candidates(
        skills=list(skills), must_remember=list(must), emotional=list(emo)
    )


# ----- seed_entries ---------------------------------------------------------


def test_seed_entries_empty_is_noop(tmp_path: Path) -> None:
    _store, bstore = _make_store(tmp_path)
    added = seed_entries(bstore, _cands(), now="2026-06-18T00:00:00Z")
    assert added == {"skills": 0, "must_remember": 0, "emotional": 0}


def test_seed_entries_appends_and_preserves_backdating(tmp_path: Path) -> None:
    _store, bstore = _make_store(tmp_path)
    # pre-existing non-import entry in skills -> append must not clobber it.
    bstore.save_atomic("skills", [_skill(src="extract", name="old skill")])
    added = seed_entries(
        bstore, _cands(skills=[_skill()], must=[_mr()], emo=[_emo()])
    )
    assert added == {"skills": 1, "must_remember": 1, "emotional": 1}
    skills = bstore.load("skills")
    assert {e.name for e in skills} == {"old skill", "run the suite"}
    # backdating survives byte-for-byte: no re-stamp.
    seeded = [e for e in skills if e.source == IMPORT_SOURCE][0]
    assert seeded.last_used == SRC and seeded.created_at == SRC
    emo = bstore.load("emotional")[0]
    assert emo.weight == 3.0  # not re-derived


def test_seed_entries_skips_empty_bucket(tmp_path: Path) -> None:
    _store, bstore = _make_store(tmp_path)
    added = seed_entries(bstore, _cands(skills=[_skill()]))  # must/emo empty
    assert added == {"skills": 1, "must_remember": 0, "emotional": 0}


def test_seed_entries_rejects_wrong_source(tmp_path: Path) -> None:
    _store, bstore = _make_store(tmp_path)
    with pytest.raises(DoubleSeedError, match="not 'import-legacy'"):
        seed_entries(bstore, _cands(skills=[_skill(src="extract")]))


def test_seed_entries_refuses_double_seed(tmp_path: Path) -> None:
    _store, bstore = _make_store(tmp_path)
    seed_entries(bstore, _cands(skills=[_skill()]))
    with pytest.raises(DoubleSeedError, match="already holds"):
        seed_entries(bstore, _cands(skills=[_skill(name="another")]))


# ----- idempotency guards ---------------------------------------------------


def test_has_seeded_entries(tmp_path: Path) -> None:
    _store, bstore = _make_store(tmp_path)
    # a pre-existing NON-import entry: scan must skip it (if-False branch) and
    # keep looking, not mistake it for a seed.
    bstore.save_atomic("skills", [_skill(src="extract", name="old skill")])
    assert has_seeded_entries(bstore) is False
    seed_entries(bstore, _cands(emo=[_emo()]))
    # now the scan walks the non-import skill (continue) then finds the
    # import-legacy emotional entry -> True.
    assert has_seeded_entries(bstore) is True


def test_already_imported_via_marker(tmp_path: Path) -> None:
    store, bstore = _make_store(tmp_path)
    assert already_imported(store, bstore) is False
    mark_imported(store, counts={"skills": 1})
    assert already_imported(store, bstore) is True
    # marker preserved alongside other state keys.
    assert store.read_state()[STATE_KEY]["done"] is True
    assert store.read_state()[STATE_KEY]["seeded"]["skills"] == 1


def test_already_imported_fallback_when_marker_deleted(tmp_path: Path) -> None:
    store, bstore = _make_store(tmp_path)
    seed_entries(bstore, _cands(must=[_mr()]))
    # no marker written (hand-deleted scenario) -> detect-existing-seed blocks.
    assert STATE_KEY not in (store.read_state() or {})
    assert already_imported(store, bstore) is True


def test_mark_imported_preserves_existing_state(tmp_path: Path) -> None:
    store, bstore = _make_store(tmp_path)
    state = store.read_state() or {}
    state["sentinel"] = "keep me"
    store.write_state(state)
    mark_imported(store, counts={"emotional": 2})
    after = store.read_state()
    assert after["sentinel"] == "keep me"
    assert after[STATE_KEY]["seeded"]["emotional"] == 2


# ----- read-before-drop -----------------------------------------------------


def test_assert_seed_inputs_snapshotted_ok(tmp_path: Path) -> None:
    assert_seed_inputs_snapshotted(_cands(skills=[_skill()]))  # lists -> no raise


def test_assert_seed_inputs_snapshotted_rejects_lazy(tmp_path: Path) -> None:
    lazy = Candidates(skills=(e for e in []), must_remember=[], emotional=[])
    with pytest.raises(TypeError, match="read-before-drop"):
        assert_seed_inputs_snapshotted(lazy)


def test_seeds_perform_no_deletion(tmp_path: Path) -> None:
    assert seeds_perform_no_deletion({"a.md", "b.md"}, {"a.md", "b.md", "c.md"})
    assert not seeds_perform_no_deletion({"a.md", "gone.md"}, {"a.md"})

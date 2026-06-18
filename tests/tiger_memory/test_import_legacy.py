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
    SEED_SKILL_USAGE_CAP,
    STATE_KEY,
    DoubleSeedError,
    already_imported,
    assert_seed_inputs_snapshotted,
    has_seeded_entries,
    mark_imported,
    score_seed_candidates,
    seed_entries,
    seeds_perform_no_deletion,
)
from tigerharness.tiger_memory.lifecycle import Candidates
from tigerharness.tiger_memory.store import Store

SRC = "2025-01-01T00:00:00Z"  # a backdated source date


def _load_cfg(tmp_path: Path):
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
    return load_config(p)


def _make_store(tmp_path: Path) -> tuple[Store, BoundedStore]:
    cfg = _load_cfg(tmp_path)
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


# ----- score_seed_candidates (b1-dev-2, Rukawa) -----------------------------
#
# The seeding scorer turns the re-authored RAW candidates (whose source date
# lives in ``created_at``, set by Miyagi's reader) into FINAL backdated entries
# tagged source="import-legacy", ready for seed_entries to write AS-IS.

NOW = "2026-06-17T00:00:00Z"  # ~530 days after SRC (2025-01-01)


def _raw_skill(src=SRC, usage=5, name="run the suite"):
    # An UNSCORED input skill: source != IMPORT_SOURCE, source date in created_at.
    return SkillEntry(
        text="always run the full test suite before pushing",
        created_at=src, last_used=src, source="reauthor",
        name=name, trigger="before push", procedure="uv run pytest",
        usage_count=usage, importance=99.0,
    )


def _raw_mr(src=SRC, kind="owner_explicit", importance=5.0):
    return MustRememberEntry(
        text="never push without approval", created_at=src, last_used=src,
        source="reauthor", kind=kind, importance=importance,
    )


def _raw_emo(src=SRC, weight=8.0):
    return EmotionalEntry(
        text="shipping the revamp felt great", created_at=src, last_used=src,
        source="reauthor", weight=weight, reaction="proud",
    )


def _raw(skills=(), must=(), emo=()):
    return Candidates(
        skills=list(skills), must_remember=list(must), emotional=list(emo)
    )


def test_score_empty_returns_empty(tmp_path: Path) -> None:
    cfg = _load_cfg(tmp_path)
    out = score_seed_candidates(_raw(), cfg=cfg, now=NOW)
    assert out.is_empty()


def test_score_tags_every_entry_import_source(tmp_path: Path) -> None:
    cfg = _load_cfg(tmp_path)
    out = score_seed_candidates(
        _raw(skills=[_raw_skill()], must=[_raw_mr()], emo=[_raw_emo()]),
        cfg=cfg, now=NOW,
    )
    every = out.skills + out.must_remember + out.emotional
    assert every and all(e.source == IMPORT_SOURCE for e in every)


def test_score_emotional_backdated_decay_preserves_sign(tmp_path: Path) -> None:
    cfg = _load_cfg(tmp_path)
    # An OLD high-magnitude experience (source ~530d back) enters PARTLY decayed:
    # 530 * 0.1 = 53 of shrink fully erases an 8.0 magnitude -> pinned to 0.
    out = score_seed_candidates(_raw(emo=[_raw_emo(weight=8.0)]), cfg=cfg, now=NOW)
    e = out.emotional[0]
    assert e.created_at == SRC and e.last_used == SRC  # backdated, not now()
    assert e.weight == 0.0
    # A recent positive experience (~1 day back) is only slightly decayed and
    # keeps its sign + most of its magnitude.
    recent = "2026-06-16T00:00:00Z"
    out2 = score_seed_candidates(
        _raw(emo=[_raw_emo(src=recent, weight=8.0)]), cfg=cfg, now=NOW
    )
    w = out2.emotional[0].weight
    assert 0 < w < 8.0 and abs(w - 7.9) < 1e-9
    # A negative experience keeps its (negative) sign while decaying toward 0.
    out3 = score_seed_candidates(
        _raw(emo=[_raw_emo(src=recent, weight=-8.0)]), cfg=cfg, now=NOW
    )
    assert out3.emotional[0].weight < 0


def test_score_emotional_clamps_beyond_cap(tmp_path: Path) -> None:
    cfg = _load_cfg(tmp_path)
    # A same-day, beyond-cap re-authored weight clamps to +weight_cap (10): no
    # decay (0 days), so only the clamp acts.
    out = score_seed_candidates(
        _raw(emo=[_raw_emo(src=NOW, weight=999.0)]), cfg=cfg, now=NOW
    )
    assert out.emotional[0].weight == 10.0
    out_neg = score_seed_candidates(
        _raw(emo=[_raw_emo(src=NOW, weight=-999.0)]), cfg=cfg, now=NOW
    )
    assert out_neg.emotional[0].weight == -10.0


def test_score_skill_backdated_lastused_and_low_usage(tmp_path: Path) -> None:
    cfg = _load_cfg(tmp_path)
    out = score_seed_candidates(_raw(skills=[_raw_skill(usage=5)]), cfg=cfg, now=NOW)
    s = out.skills[0]
    assert s.last_used == SRC and s.created_at == SRC  # backdated, not now()
    assert s.usage_count == SEED_SKILL_USAGE_CAP  # 5 capped to 1 (low)
    assert s.importance == 0.0  # NOT refreshed — derived at meditation
    # a re-author that judged the skill never exercised seeds usage_count 0.
    out0 = score_seed_candidates(_raw(skills=[_raw_skill(usage=0)]), cfg=cfg, now=NOW)
    assert out0.skills[0].usage_count == 0


def test_score_must_remember_preserves_kind_and_importance(tmp_path: Path) -> None:
    cfg = _load_cfg(tmp_path)
    out = score_seed_candidates(
        _raw(must=[_raw_mr(kind="owner_explicit", importance=5.0)]),
        cfg=cfg, now=NOW,
    )
    m = out.must_remember[0]
    assert m.kind == "owner_explicit"  # preserved straight from the table
    assert m.importance == 5.0  # elevated, mapped as-is
    assert m.created_at == SRC and m.last_used == SRC
    # a non-elevated kind maps its (lower) score straight through too.
    out2 = score_seed_candidates(
        _raw(must=[_raw_mr(kind="decision", importance=1.0)]), cfg=cfg, now=NOW
    )
    assert out2.must_remember[0].kind == "decision"
    assert out2.must_remember[0].importance == 1.0


def test_score_source_dates_override_map(tmp_path: Path) -> None:
    cfg = _load_cfg(tmp_path)
    # A must_memorize row whose date the reader resolves separately: created_at
    # is empty on the raw input, the override map supplies the source date.
    raw_mr = MustRememberEntry(
        text="owner said X", created_at="", last_used="", source="reauthor",
        kind="owner_explicit", importance=5.0, id="row-1",
    )
    out = score_seed_candidates(
        _raw(must=[raw_mr]), cfg=cfg, now=NOW, source_dates={"row-1": SRC}
    )
    assert out.must_remember[0].created_at == SRC


def test_score_source_dates_map_without_matching_id(tmp_path: Path) -> None:
    cfg = _load_cfg(tmp_path)
    # A source_dates map is supplied but holds no entry for this id: .get falls
    # through to the entry's own created_at (the source date the reader stamped).
    out = score_seed_candidates(
        _raw(must=[_raw_mr(src=SRC)]),
        cfg=cfg, now=NOW, source_dates={"some-other-id": "2024-01-01T00:00:00Z"},
    )
    assert out.must_remember[0].created_at == SRC


def test_score_falls_back_to_now_when_no_source_date(tmp_path: Path) -> None:
    cfg = _load_cfg(tmp_path)
    # No created_at, no override -> conservative same-day seed at now (never
    # an empty timestamp, which would fail validation).
    raw_emo = EmotionalEntry(
        text="neutral note", created_at="", last_used="", source="reauthor",
        weight=2.0, reaction="meh",
    )
    out = score_seed_candidates(_raw(emo=[raw_emo]), cfg=cfg, now=NOW)
    e = out.emotional[0]
    assert e.created_at == NOW and e.last_used == NOW
    assert e.weight == 2.0  # 0 days elapsed -> no decay


def test_score_now_defaults_to_iso_now(tmp_path: Path) -> None:
    cfg = _load_cfg(tmp_path)
    # now omitted -> defaults to iso_now(); the entry is still produced and the
    # old source weight decays toward 0 over the elapsed span.
    out = score_seed_candidates(_raw(emo=[_raw_emo(weight=8.0)]), cfg=cfg)
    assert len(out.emotional) == 1
    assert out.emotional[0].created_at == SRC


def test_score_output_writes_through_seed_entries(tmp_path: Path) -> None:
    # End-to-end with Mitsui's seed-writer: scored output is accepted AS-IS.
    _store, bstore = _make_store(tmp_path)
    cfg = _load_cfg(tmp_path)
    scored = score_seed_candidates(
        _raw(skills=[_raw_skill()], must=[_raw_mr()], emo=[_raw_emo(src=NOW)]),
        cfg=cfg, now=NOW,
    )
    added = seed_entries(bstore, scored, now=NOW)
    assert added == {"skills": 1, "must_remember": 1, "emotional": 1}

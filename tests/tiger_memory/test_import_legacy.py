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
from tigerharness.tiger_memory.diary import (
    clamp_weight,
    decay_entry,
    diary_keep_rank,
)
from tigerharness.tiger_memory.entries import (
    DiaryEntry,
    MustRememberEntry,
    SkillEntry,
)
from tigerharness.tiger_memory.import_legacy import (
    IMPORT_SOURCE,
    SEED_SKILL_USAGE_CAP,
    STATE_KEY,
    DoubleSeedError,
    LegacyRollup,
    _mtime_iso,
    already_imported,
    assert_seed_inputs_snapshotted,
    has_seeded_entries,
    import_legacy_run,
    mark_imported,
    read_legacy,
    reauthor,
    score_seed_candidates,
    seed_entries,
    seeds_perform_no_deletion,
)
from tigerharness.tiger_memory.lifecycle import Candidates
from tigerharness.tiger_memory.store import Store
from tigerharness.tiger_memory.summarizers import MockSummarizer, Summarizer

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
              diary:
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
    return DiaryEntry(
        text="shipping the revamp felt great", created_at=SRC, last_used=SRC,
        source=src, weight=weight,
    )


def _cands(skills=(), must=(), emo=()):
    return Candidates(
        skills=list(skills), must_remember=list(must), diary=list(emo)
    )


# ----- seed_entries ---------------------------------------------------------


def test_seed_entries_empty_is_noop(tmp_path: Path) -> None:
    _store, bstore = _make_store(tmp_path)
    added = seed_entries(bstore, _cands(), now="2026-06-18T00:00:00Z")
    assert added == {"skills": 0, "must_remember": 0, "diary": 0}


def test_seed_entries_appends_and_preserves_backdating(tmp_path: Path) -> None:
    _store, bstore = _make_store(tmp_path)
    # pre-existing non-import entry in skills -> append must not clobber it.
    bstore.save_atomic("skills", [_skill(src="extract", name="old skill")])
    added = seed_entries(
        bstore, _cands(skills=[_skill()], must=[_mr()], emo=[_emo()])
    )
    assert added == {"skills": 1, "must_remember": 1, "diary": 1}
    skills = bstore.load("skills")
    assert {e.name for e in skills} == {"old skill", "run the suite"}
    # backdating survives byte-for-byte: no re-stamp.
    seeded = [e for e in skills if e.source == IMPORT_SOURCE][0]
    assert seeded.last_used == SRC and seeded.created_at == SRC
    emo = bstore.load("diary")[0]
    assert emo.weight == 3.0  # not re-derived


def test_seed_entries_skips_empty_bucket(tmp_path: Path) -> None:
    _store, bstore = _make_store(tmp_path)
    added = seed_entries(bstore, _cands(skills=[_skill()]))  # must/emo empty
    assert added == {"skills": 1, "must_remember": 0, "diary": 0}


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
    seed_entries(bstore, _cands(must=[_mr()]))
    # now the scan walks the non-import skill (continue) then finds the
    # import-legacy must_remember entry -> True. (Diary is source-less and not
    # content-scanned; it is covered by the .state.json marker.)
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
    mark_imported(store, counts={"diary": 2})
    after = store.read_state()
    assert after["sentinel"] == "keep me"
    assert after[STATE_KEY]["seeded"]["diary"] == 2


# ----- read-before-drop -----------------------------------------------------


def test_assert_seed_inputs_snapshotted_ok(tmp_path: Path) -> None:
    assert_seed_inputs_snapshotted(_cands(skills=[_skill()]))  # lists -> no raise


def test_assert_seed_inputs_snapshotted_rejects_lazy(tmp_path: Path) -> None:
    lazy = Candidates(skills=(e for e in []), must_remember=[], diary=[])
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
    return DiaryEntry(
        text="shipping the revamp felt great", created_at=src, last_used=src,
        source="reauthor", weight=weight,
    )


def _raw(skills=(), must=(), emo=()):
    return Candidates(
        skills=list(skills), must_remember=list(must), diary=list(emo)
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
    every = out.skills + out.must_remember + out.diary
    assert every and all(e.source == IMPORT_SOURCE for e in every)


def test_score_emotional_stores_raw_clamped_weight_backdated(tmp_path: Path) -> None:
    # GAP-5 (final convergence): a seed stores the CLAMPED RAW weight — NOT a
    # pre-decayed one — backdated to its source date. The "partly decayed" effect
    # happens ONCE at rank time (via last_used), so a seed is never double-decayed.
    cfg = _load_cfg(tmp_path)
    out = score_seed_candidates(_raw(emo=[_raw_emo(weight=8.0)]), cfg=cfg, now=NOW)
    e = out.diary[0]
    assert e.created_at == SRC and e.last_used == SRC  # backdated, not now()
    assert e.weight == 8.0  # raw clamped, NOT decayed (was wrongly 0.0 pre-fix)
    # sign preserved; a beyond-cap raw still clamps (no pre-decay involved).
    neg = score_seed_candidates(
        _raw(emo=[_raw_emo(weight=-8.0)]), cfg=cfg, now=NOW
    ).diary[0]
    assert neg.weight == -8.0


def test_seed_emotional_not_double_decayed_matches_organic(tmp_path: Path) -> None:
    # GAP-5 regression (Kogure r1): a seeded emotional entry and an ORGANIC entry
    # of the SAME age + raw weight must rank IDENTICALLY — the seed is decayed
    # exactly once, at rank time, like any organic entry. (Pre-fix the seed was
    # double-decayed: a 30-day +8 ranked 2.0 instead of the intended 5.0.)
    cfg = _load_cfg(tmp_path)
    old = "2026-05-18T00:00:00Z"  # 30 days before NOW (2026-06-17)
    seeded = score_seed_candidates(
        _raw(emo=[_raw_emo(src=old, weight=8.0)]), cfg=cfg, now=NOW
    ).diary[0]
    assert seeded.weight == clamp_weight(8.0, cfg)  # stored raw, not pre-decayed
    organic = DiaryEntry(
        text="x", created_at=old, last_used=old, source="extract",
        weight=8.0,
    )
    # single rank-time decay is identical for both -> intended 5.0, not 2.0.
    assert decay_entry(seeded, NOW, cfg) == decay_entry(organic, NOW, cfg) == 5.0
    assert diary_keep_rank(seeded, NOW, cfg) == diary_keep_rank(
        organic, NOW, cfg
    )


def test_score_emotional_clamps_beyond_cap(tmp_path: Path) -> None:
    cfg = _load_cfg(tmp_path)
    # A same-day, beyond-cap re-authored weight clamps to +weight_cap (10): no
    # decay (0 days), so only the clamp acts.
    out = score_seed_candidates(
        _raw(emo=[_raw_emo(src=NOW, weight=999.0)]), cfg=cfg, now=NOW
    )
    assert out.diary[0].weight == 10.0
    out_neg = score_seed_candidates(
        _raw(emo=[_raw_emo(src=NOW, weight=-999.0)]), cfg=cfg, now=NOW
    )
    assert out_neg.diary[0].weight == -10.0


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
    raw_emo = DiaryEntry(
        text="neutral note", created_at="", last_used="", source="reauthor",
        weight=2.0,
    )
    out = score_seed_candidates(_raw(emo=[raw_emo]), cfg=cfg, now=NOW)
    e = out.diary[0]
    assert e.created_at == NOW and e.last_used == NOW
    assert e.weight == 2.0  # 0 days elapsed -> no decay


def test_score_now_defaults_to_iso_now(tmp_path: Path) -> None:
    cfg = _load_cfg(tmp_path)
    # now omitted -> defaults to iso_now(); the entry is still produced and the
    # old source weight decays toward 0 over the elapsed span.
    out = score_seed_candidates(_raw(emo=[_raw_emo(weight=8.0)]), cfg=cfg)
    assert len(out.diary) == 1
    assert out.diary[0].created_at == SRC


def test_score_output_writes_through_seed_entries(tmp_path: Path) -> None:
    # End-to-end with Mitsui's seed-writer: scored output is accepted AS-IS.
    _store, bstore = _make_store(tmp_path)
    cfg = _load_cfg(tmp_path)
    scored = score_seed_candidates(
        _raw(skills=[_raw_skill()], must=[_raw_mr()], emo=[_raw_emo(src=NOW)]),
        cfg=cfg, now=NOW,
    )
    added = seed_entries(bstore, scored, now=NOW)
    assert added == {"skills": 1, "must_remember": 1, "diary": 1}


# ===== legacy reader + re-author + orchestrator + CLI (b1-dev-3, Miyagi) =====


class _BundleSummarizer(Summarizer):
    """A scripted summarizer emitting one valid re-author bundle per call.

    The deterministic ``MockSummarizer`` returns prose bullets that do NOT
    satisfy the strict ``@@SKILLS@@/.../@@DIARY@@`` marker contract (so the
    re-author of a rollup yields no skill/emotional seeds under ``--mock`` — only
    the mechanical pins seed). This stand-in returns a full, parseable bundle so
    the end-to-end "seeds all three stores" path is exercised.
    """

    name = "bundle"
    version = "v1"

    def summarize(self, *, prompt: str, max_words: int) -> str:  # noqa: D401
        return (
            "@@SKILLS@@\n"
            "NAME: bound a markdown store\n"
            "TRIGGER: when a store can grow unbounded\n"
            "PROCEDURE: cap by count or chars; meditate on overflow\n"
            "\n"
            "@@MUST_REMEMBER@@\n"
            "KIND: decision\n"
            "MEMO: migration is a fresh start with a one-off import seed\n"
            "\n"
            "@@DIARY@@\n"
            "WEIGHT: 6\n"
            "REACTION: proud\n"
            "TEXT: shipping the bounded-store revamp felt great\n"
        )


def _write_must_memorize(store: Store) -> None:
    (store.paths.journal / "must_memorize.md").write_text(
        dedent(
            """\
            ---
            type: must_memorize
            updated_at: '2026-06-16T04:20:52Z'
            ---
            # Must memorize

            | Score | Kind | Last bump | Source | Memo |
            |------:|------|-----------|--------|------|
            |     5 | owner_explicit | 2026-06-16 | extract | Always run uv run pytest --cov. |
            |     1 | decision | 2026-06-11 | extract | Lean is the goal; bare is the failure mode. |
            | bad   | decision | 2026-06-11 | extract | Unparseable score row -> skipped. |
            |     2 | bogus_kind | 2026-06-11 | extract | Unknown kind row -> skipped. |
            |     3 | preference | | extract | No last-bump date -> mtime fallback. |
            |     4 | incident | 2026-06-10T08:00:00Z | extract | Full ISO last-bump passes through. |
            | only | two |
            """
        ),
        encoding="utf-8",
    )


def _write_rollups(store: Store) -> None:
    j = store.paths.journal
    # daily rollup (period = YYYY-MM-DD).
    (j / "20260611-daily-aaaaaaaa-0000-0000-0000-000000000000.md").write_text(
        "---\ntype: daily_rollup\nperiod: '2026-06-11'\n---\n"
        "- Shipped the journal scheduler with exactly-once materialization.\n",
        encoding="utf-8",
    )
    # weekly rollup (period = YYYY-MM-DD).
    (j / "20260608-week-bbbbbbbb-0000-0000-0000-000000000000.md").write_text(
        "---\ntype: weekly_rollup\nperiod: '2026-06-08'\n---\n"
        "## Shipped\n- Multi-operator memory segmentation landed.\n",
        encoding="utf-8",
    )
    # monthly rollup (period = YYYY-MM).
    (j / "202606-month-cccccccc-0000-0000-0000-000000000000.md").write_text(
        "---\ntype: monthly_rollup\nperiod: 2026-06\n---\n"
        "## Themes\nHardening the scheduler stack and excising legacy infra.\n",
        encoding="utf-8",
    )
    # a per-session SHORT transcript -> MUST be skipped (not a rollup).
    (j / "20260610-080144-dddddddd-0000-0000-0000-000000000000.md").write_text(
        "---\ntype: short\n---\nverbose per-session detail, do not import\n",
        encoding="utf-8",
    )
    # an empty-body rollup -> skipped (no prose to mine).
    (j / "20260607-daily-eeeeeeee-0000-0000-0000-000000000000.md").write_text(
        "---\ntype: daily_rollup\nperiod: '2026-06-07'\n---\n\n",
        encoding="utf-8",
    )


# ----- read_legacy ----------------------------------------------------------


def test_read_legacy_absent_journal_is_empty(tmp_path: Path) -> None:
    cfg = _load_cfg(tmp_path)
    store = Store(cfg.store.root)  # never init_layout -> no journal dir
    src = read_legacy(store)
    assert src.is_empty()


def test_read_legacy_parses_pins(tmp_path: Path) -> None:
    store, _bstore = _make_store(tmp_path)
    _write_must_memorize(store)
    src = read_legacy(store)
    # 4 valid rows kept (owner_explicit, decision, preference, incident); the
    # unparseable score, the bogus kind, the short ``| only | two |`` row, and
    # the header/separator rows are all dropped.
    assert len(src.pins) == 4
    by_kind = {p.kind: p for p in src.pins}
    assert by_kind["owner_explicit"].score == 5.0
    assert by_kind["owner_explicit"].source_date == "2026-06-16T00:00:00Z"
    assert by_kind["decision"].memo.startswith("Lean is the goal")
    # the date-less preference row falls back to the file mtime (a full ISO ts).
    assert by_kind["preference"].source_date.endswith("Z")
    assert by_kind["preference"].source_date != ""
    # a row whose ``Last bump`` is already a full ISO timestamp passes through.
    assert by_kind["incident"].source_date == "2026-06-10T08:00:00Z"


def test_read_legacy_parses_rollups_and_skips_shorts(tmp_path: Path) -> None:
    store, _bstore = _make_store(tmp_path)
    _write_rollups(store)
    src = read_legacy(store)
    # 3 non-empty rollups (daily/weekly/monthly); the SHORT + the empty-body
    # daily are skipped.
    kinds = sorted(r.kind for r in src.rollups)
    assert kinds == ["daily", "monthly", "weekly"]
    by_kind = {r.kind: r for r in src.rollups}
    assert by_kind["daily"].source_date == "2026-06-11T00:00:00Z"
    assert by_kind["monthly"].source_date == "2026-06-01T00:00:00Z"  # YYYY-MM
    assert "exactly-once" in by_kind["daily"].body


def test_read_legacy_does_not_unlink_any_file(tmp_path: Path) -> None:
    # read-before-drop: the reader SNAPSHOTS, it never deletes a legacy file.
    store, _bstore = _make_store(tmp_path)
    _write_must_memorize(store)
    _write_rollups(store)
    before = {p.name for p in store.paths.journal.glob("*.md")}
    read_legacy(store)
    after = {p.name for p in store.paths.journal.glob("*.md")}
    assert seeds_perform_no_deletion(before, after)
    assert before == after


# ----- reauthor -------------------------------------------------------------


def test_reauthor_pins_pass_through_mechanically(tmp_path: Path) -> None:
    cfg = _load_cfg(tmp_path)
    store, _bstore = _make_store(tmp_path)
    _write_must_memorize(store)
    src = read_legacy(store)
    # MockSummarizer body is NOT a valid bundle -> rollups mine nothing, but the
    # mechanical pins still pass through unchanged.
    out = reauthor(cfg, MockSummarizer(), src)
    assert len(out.must_remember) == len(src.pins) == 4
    assert {m.kind for m in out.must_remember} == {
        "owner_explicit", "decision", "preference", "incident"
    }
    # each pin carries its source date in created_at for the scorer to backdate.
    oe = next(m for m in out.must_remember if m.kind == "owner_explicit")
    assert oe.created_at == "2026-06-16T00:00:00Z"


def test_reauthor_mock_yields_no_rollup_seeds(tmp_path: Path) -> None:
    cfg = _load_cfg(tmp_path)
    store, _bstore = _make_store(tmp_path)
    _write_rollups(store)  # rollups only, no pins
    src = read_legacy(store)
    out = reauthor(cfg, MockSummarizer(), src)
    # the mock bundle fails the marker contract -> swallowed to empty per rollup.
    assert not out.skills and not out.diary and not out.must_remember


def test_reauthor_scripted_bundle_mines_all_stores(tmp_path: Path) -> None:
    cfg = _load_cfg(tmp_path)
    store, _bstore = _make_store(tmp_path)
    _write_rollups(store)
    src = read_legacy(store)
    out = reauthor(cfg, _BundleSummarizer(), src)
    # one bundle per non-empty rollup (3) -> 3 skills, 3 emotional, 3 mr.
    assert len(out.skills) == 3
    assert len(out.diary) == 3
    assert len(out.must_remember) == 3
    # each re-authored entry carries its rollup's source date in created_at.
    assert {s.created_at for s in out.skills} == {
        "2026-06-11T00:00:00Z", "2026-06-08T00:00:00Z", "2026-06-01T00:00:00Z"
    }


# ----- import_legacy_run (the orchestrator) ---------------------------------


def _full_legacy_store(tmp_path: Path) -> tuple[Store, object]:
    store, bstore = _make_store(tmp_path)
    _write_must_memorize(store)
    _write_rollups(store)
    return store, bstore


def test_import_legacy_run_end_to_end_seeds_all_stores(tmp_path: Path) -> None:
    cfg = _load_cfg(tmp_path)
    store, bstore = _full_legacy_store(tmp_path)
    result = import_legacy_run(
        cfg, store, summarizer=_BundleSummarizer(), now=NOW
    )
    assert result["skipped"] is None
    # 3 rollups * 1 skill each = 3 skills; 3 rollup-mr + 4 pins = 7 mr; 3 emo.
    assert result == {
        "skipped": None, "skills": 3, "must_remember": 7, "diary": 3
    }
    # the durable marker is written.
    assert already_imported(store, bstore) is True
    assert store.read_state()[STATE_KEY]["seeded"]["skills"] == 3
    # every seeded entry carries the import provenance + is backdated (not NOW
    # for the rollup/pin items).
    for name in ("skills", "must_remember"):
        seeded = bstore.load(name)
        assert seeded and all(e.source == IMPORT_SOURCE for e in seeded)
    # old emotional seeds entered already-decayed (source < now).
    emo = bstore.load("diary")[0]
    assert emo.created_at != NOW


def test_import_legacy_run_idempotent_no_double_seed(tmp_path: Path) -> None:
    cfg = _load_cfg(tmp_path)
    store, bstore = _full_legacy_store(tmp_path)
    first = import_legacy_run(cfg, store, summarizer=_BundleSummarizer(), now=NOW)
    counts_before = {n: len(bstore.load(n)) for n in ("skills", "must_remember", "diary")}
    # second run gates on the marker -> a no-op; stores unchanged.
    second = import_legacy_run(cfg, store, summarizer=_BundleSummarizer(), now=NOW)
    assert first["skipped"] is None
    assert second == {"skipped": "already"}
    counts_after = {n: len(bstore.load(n)) for n in ("skills", "must_remember", "diary")}
    assert counts_before == counts_after


def test_import_legacy_run_marker_deleted_still_blocks(tmp_path: Path) -> None:
    cfg = _load_cfg(tmp_path)
    store, bstore = _full_legacy_store(tmp_path)
    import_legacy_run(cfg, store, summarizer=_BundleSummarizer(), now=NOW)
    # hand-delete the marker; the detect-existing-seed fallback still blocks.
    state = store.read_state()
    del state[STATE_KEY]
    store.write_state(state)
    second = import_legacy_run(cfg, store, summarizer=_BundleSummarizer(), now=NOW)
    assert second == {"skipped": "already"}


def test_import_legacy_run_force_re_seeds(tmp_path: Path) -> None:
    cfg = _load_cfg(tmp_path)
    store, bstore = _full_legacy_store(tmp_path)
    import_legacy_run(cfg, store, summarizer=_BundleSummarizer(), now=NOW)
    before = len(bstore.load("must_remember"))
    # force drops the prior import-legacy entries, then re-seeds the same set ->
    # counts are stable (REPLACE, not duplicate).
    forced = import_legacy_run(
        cfg, store, summarizer=_BundleSummarizer(), now=NOW, force=True
    )
    assert forced["skipped"] is None
    assert len(bstore.load("must_remember")) == before  # replaced, not doubled


def test_import_legacy_run_force_preserves_live_memory(tmp_path: Path) -> None:
    cfg = _load_cfg(tmp_path)
    store, bstore = _full_legacy_store(tmp_path)
    import_legacy_run(cfg, store, summarizer=_BundleSummarizer(), now=NOW)
    # a live (non-import) pin must survive a forced re-seed purge.
    live = MustRememberEntry(
        text="live pin keep me", created_at=NOW, last_used=NOW, source="pin",
        kind="owner_explicit", importance=5.0,
    )
    bstore.save_atomic("must_remember", bstore.load("must_remember") + [live])
    import_legacy_run(cfg, store, summarizer=_BundleSummarizer(), now=NOW, force=True)
    texts = {m.text for m in bstore.load("must_remember")}
    assert "live pin keep me" in texts


def test_import_legacy_run_empty_store_seeds_nothing(tmp_path: Path) -> None:
    cfg = _load_cfg(tmp_path)
    store, bstore = _make_store(tmp_path)  # no legacy files at all
    result = import_legacy_run(cfg, store, summarizer=_BundleSummarizer(), now=NOW)
    assert result == {"skipped": None, "skills": 0, "must_remember": 0, "diary": 0}
    # marker still written (a one-off "we imported, there was nothing").
    assert already_imported(store, bstore) is True


def test_import_legacy_run_does_not_unlink_legacy_files(tmp_path: Path) -> None:
    # read-before-drop ordering: the import never deletes a legacy file (rebuild
    # is the only step that drops; the orchestrator never calls it).
    cfg = _load_cfg(tmp_path)
    store, _bstore = _full_legacy_store(tmp_path)
    before = {p.name for p in store.paths.journal.glob("*.md")}
    import_legacy_run(cfg, store, summarizer=_BundleSummarizer(), now=NOW)
    after = {p.name for p in store.paths.journal.glob("*.md")}
    # every legacy file present before the import is still present (plus the new
    # store files skills.md/must_remember.md/diary.md).
    assert before <= after
    assert "must_memorize.md" in after
    assert "20260610-080144-dddddddd-0000-0000-0000-000000000000.md" in after  # short kept


def test_import_legacy_run_default_summarizer(tmp_path: Path, monkeypatch) -> None:
    # summarizer omitted -> the orchestrator builds one from cfg. Stub the
    # builder so no live model is constructed; assert it is used.
    cfg = _load_cfg(tmp_path)
    store, bstore = _full_legacy_store(tmp_path)
    used = {}

    def _fake_build(_cfg, *, mock=False):
        used["built"] = True
        return _BundleSummarizer()

    monkeypatch.setattr(
        "tigerharness.tiger_memory.import_legacy._build_summarizer", _fake_build
    )
    result = import_legacy_run(cfg, store, now=NOW)  # no summarizer arg
    assert used.get("built") is True
    assert result["skills"] == 3


# ----- CLI verb -------------------------------------------------------------


def _write_cli_config(tmp_path: Path) -> Path:
    p = tmp_path / "cfg.yaml"
    _load_cfg(tmp_path)  # writes cfg.yaml; reuse the same on-disk file.
    return p


def test_cli_import_legacy_mock_seeds_pins(tmp_path: Path, capsys) -> None:
    from tigerharness.tiger_memory.cli import main
    cfg = _load_cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    _write_must_memorize(store)
    _write_rollups(store)
    rc = main(["--config", str(tmp_path / "cfg.yaml"), "import-legacy", "--mock"])
    assert rc == 0
    out = capsys.readouterr().out
    # under --mock the rollups mine nothing; the 4 mechanical pins seed mr.
    assert "import-legacy seeded:" in out
    assert "4 must_remember" in out
    assert "0 skills" in out
    bstore = BoundedStore(cfg, store)
    assert len(bstore.load("must_remember")) == 4
    assert already_imported(store, bstore) is True


def test_cli_import_legacy_idempotent_then_force(tmp_path: Path, capsys) -> None:
    from tigerharness.tiger_memory.cli import main
    cfg = _load_cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    _write_must_memorize(store)
    cfgpath = str(tmp_path / "cfg.yaml")
    assert main(["--config", cfgpath, "import-legacy", "--mock"]) == 0
    capsys.readouterr()
    # a second --mock run is gated -> the "already imported" message + rc 0.
    assert main(["--config", cfgpath, "import-legacy", "--mock"]) == 0
    assert "already imported" in capsys.readouterr().out
    # --force re-seeds (drops + re-adds), rc 0, prints the seeded counts again.
    assert main(["--config", cfgpath, "import-legacy", "--mock", "--force"]) == 0
    assert "import-legacy seeded:" in capsys.readouterr().out


def test_reauthor_one_swallows_backend_error(tmp_path: Path) -> None:
    # a re-author backend that raises must not abort the import — the bad rollup
    # yields empty candidates and the rest proceed.
    cfg = _load_cfg(tmp_path)
    store, _bstore = _make_store(tmp_path)

    class _Boom(Summarizer):
        name = "boom"
        version = "v1"

        def summarize(self, *, prompt: str, max_words: int) -> str:
            raise RuntimeError("backend down")

    rollup = LegacyRollup(kind="daily", source_date=SRC, body="some prose")
    from tigerharness.tiger_memory.import_legacy import reauthor as _ra
    src = read_legacy(store)
    src.rollups.append(rollup)
    out = _ra(cfg, _Boom(), src)
    assert out.is_empty()


# ===== b2-qa-sakuragi — adversarial QA defense ==============================
#
# ATTACK what the build assumed away: malformed/edge legacy input, source-date
# fallbacks, idempotency under stress, read-before-drop on the error paths, and
# re-author parse failures. A real break is captured as an xfail(strict) test +
# precise repro (the dev fixes it on rewind); everything that HOLDS stays as
# hardening. MOCK/scripted summarizer only — no live model.


class _JunkSummarizer(Summarizer):
    """Returns text with NO section markers → ``ExtractionParseError``."""

    name = "junk"
    version = "v1"

    def summarize(self, *, prompt: str, max_words: int) -> str:
        return "total garbage prose with no @@markers@@ at all"


class _PartialBundleSummarizer(Summarizer):
    """Returns ONLY the ``@@SKILLS@@`` marker → missing-markers parse error."""

    name = "partial"
    version = "v1"

    def summarize(self, *, prompt: str, max_words: int) -> str:
        return "@@SKILLS@@\nNAME: x\nTRIGGER: y\nPROCEDURE: z\n"


class _EmptyBundleSummarizer(Summarizer):
    """Returns the empty string → ``empty extraction output`` parse error."""

    name = "empty"
    version = "v1"

    def summarize(self, *, prompt: str, max_words: int) -> str:
        return ""


# ----- 1. malformed / edge legacy input to read_legacy ----------------------


def test_qa_corrupt_empty_must_memorize_no_table(tmp_path: Path) -> None:
    # HOLDS: a header-only must_memorize.md with no markdown table degrades to
    # zero pins (not a crash); the import completes + writes the marker.
    cfg = _load_cfg(tmp_path)
    store, bstore = _make_store(tmp_path)
    (store.paths.journal / "must_memorize.md").write_text(
        "---\ntype: must_memorize\n---\n# Must memorize\n\nNo table here at all.\n",
        encoding="utf-8",
    )
    assert read_legacy(store).pins == []
    result = import_legacy_run(cfg, store, summarizer=MockSummarizer(), now=NOW)
    assert result == {"skipped": None, "skills": 0, "must_remember": 0, "diary": 0}
    assert already_imported(store, bstore) is True


def test_qa_totally_empty_must_memorize_file(tmp_path: Path) -> None:
    # HOLDS: a zero-byte must_memorize.md → no frontmatter, no body, zero pins.
    store, _bstore = _make_store(tmp_path)
    (store.paths.journal / "must_memorize.md").write_text("", encoding="utf-8")
    assert read_legacy(store).pins == []


def test_qa_ragged_and_extra_column_rows_skipped(tmp_path: Path) -> None:
    # HOLDS: short (< 5 cell) rows are skipped; an extra-column row keeps only
    # its first 5 cells (cells[:5]) so a 6-column row still parses its memo.
    store, _bstore = _make_store(tmp_path)
    (store.paths.journal / "must_memorize.md").write_text(
        dedent(
            """\
            | Score | Kind | Last bump | Source | Memo |
            |------:|------|-----------|--------|------|
            | 3 | decision | 2026-06-11 |
            | 4 | incident | 2026-06-10 | extract | Good row. | extra | columns |
            """
        ),
        encoding="utf-8",
    )
    pins = read_legacy(store).pins
    # the 3-cell ragged row drops; the 7-cell extra-column row keeps first 5.
    assert len(pins) == 1
    assert pins[0].kind == "incident"
    assert pins[0].memo == "Good row."


def test_qa_whitespace_only_memo_dropped_not_crashing(tmp_path: Path) -> None:
    # HOLDS: a valid-kind row whose Memo cell is whitespace-only is DROPPED at
    # parse (cell.strip() → "" → falsy `not memo`), so a blank-text entry never
    # reaches MustRememberEntry.validate (which would EntryError-abort the whole
    # atomic seed). The good row alongside it still seeds.
    cfg = _load_cfg(tmp_path)
    store, bstore = _make_store(tmp_path)
    (store.paths.journal / "must_memorize.md").write_text(
        dedent(
            """\
            | Score | Kind | Last bump | Source | Memo |
            |------:|------|-----------|--------|------|
            | 5 | owner_explicit | 2026-06-16 | extract |    |
            | 1 | decision | 2026-06-11 | extract | Good memo survives. |
            """
        ),
        encoding="utf-8",
    )
    assert len(read_legacy(store).pins) == 1
    result = import_legacy_run(cfg, store, summarizer=MockSummarizer(), now=NOW)
    assert result["must_remember"] == 1
    assert bstore.load("must_remember")[0].text == "Good memo survives."


def test_qa_kind_not_in_valid_kinds_dropped(tmp_path: Path) -> None:
    # HOLDS: a row whose Kind is not in VALID_KINDS is dropped at parse (it would
    # else fail MustRememberEntry.validate and abort the seed).
    store, _bstore = _make_store(tmp_path)
    (store.paths.journal / "must_memorize.md").write_text(
        dedent(
            """\
            | Score | Kind | Last bump | Source | Memo |
            |------:|------|-----------|--------|------|
            | 2 | not_a_real_kind | 2026-06-11 | extract | Dropped. |
            | 1 | decision | 2026-06-11 | extract | Kept. |
            """
        ),
        encoding="utf-8",
    )
    pins = read_legacy(store).pins
    assert [p.kind for p in pins] == ["decision"]


def test_qa_rollup_missing_or_malformed_frontmatter(tmp_path: Path) -> None:
    # HOLDS: a rollup with NO frontmatter (or frontmatter lacking `period`) does
    # not crash — the source date falls back to the file mtime (a full ISO ts).
    store, _bstore = _make_store(tmp_path)
    j = store.paths.journal
    (j / "20260611-daily-aaaaaaaa-0000-0000-0000-000000000000.md").write_text(
        "no frontmatter at all, just prose about shipping\n", encoding="utf-8"
    )
    (j / "20260612-daily-bbbbbbbb-0000-0000-0000-000000000000.md").write_text(
        "---\ntype: daily_rollup\n---\nperiod key missing, prose here\n",
        encoding="utf-8",
    )
    rollups = read_legacy(store).rollups
    assert len(rollups) == 2
    for r in rollups:
        assert r.source_date.endswith("Z") and r.source_date  # mtime fallback


def test_qa_persona_with_only_rollups_no_pins(tmp_path: Path) -> None:
    # HOLDS: a persona with rollups but no must_memorize.md still imports; the
    # mechanical-pin half is simply empty.
    cfg = _load_cfg(tmp_path)
    store, bstore = _make_store(tmp_path)
    _write_rollups(store)
    assert read_legacy(store).pins == []
    result = import_legacy_run(cfg, store, summarizer=_BundleSummarizer(), now=NOW)
    assert result["must_remember"] == 3  # only the 3 rollup-mined mr, no pins
    assert already_imported(store, bstore) is True


def test_qa_persona_with_nothing_at_all(tmp_path: Path) -> None:
    # HOLDS: an empty store (no pins, no rollups) imports as an all-zero seed and
    # still writes the marker (the "we imported, there was nothing" record).
    cfg = _load_cfg(tmp_path)
    store, bstore = _make_store(tmp_path)
    result = import_legacy_run(cfg, store, summarizer=_BundleSummarizer(), now=NOW)
    assert result == {"skipped": None, "skills": 0, "must_remember": 0, "diary": 0}
    assert already_imported(store, bstore) is True


def test_qa_non_utf8_byte_in_must_memorize_degrades_gracefully(tmp_path: Path) -> None:
    # FIXED (D1, b2 REVISE): read_legacy now decodes tolerantly (errors='replace'
    # via _read_text_lenient), so a stray non-UTF8 byte no longer crashes the
    # whole persona import — the bad byte is replaced and the rest still seeds.
    cfg = _load_cfg(tmp_path)
    store, bstore = _make_store(tmp_path)
    # a stray 0xFF byte in the pin file; a perfectly good rollup also present.
    (store.paths.journal / "must_memorize.md").write_bytes(
        b"---\ntype: must_memorize\n---\n"
        b"| 5 | decision | 2026-06-16 | x | bad byte \xff here |\n"
    )
    (store.paths.journal / "20260611-daily-aaaaaaaa-0000-0000-0000-000000000000.md").write_text(
        "---\ntype: daily_rollup\nperiod: '2026-06-11'\n---\ngood prose to mine\n",
        encoding="utf-8",
    )
    # EXPECTED (post-fix): the import does NOT crash; the undecodable file is
    # skipped/replaced and the valid rollup still seeds. Currently raises
    # UnicodeDecodeError → this assertion is never reached (strict xfail).
    result = import_legacy_run(cfg, store, summarizer=_BundleSummarizer(), now=NOW)
    assert result["skipped"] is None
    assert already_imported(store, bstore) is True


def test_qa_non_utf8_only_file_does_not_crash_and_completes(tmp_path: Path) -> None:
    # FIXED (D1, b2 REVISE): a non-UTF8 byte in the ONLY legacy file no longer
    # raises out of import_legacy_run — the byte is replaced and the run reaches
    # mark_imported normally (no crash, no abort, no lost import).
    cfg = _load_cfg(tmp_path)
    store, bstore = _make_store(tmp_path)
    (store.paths.journal / "20260611-daily-aaaaaaaa-0000-0000-0000-000000000000.md").write_bytes(
        b"---\ntype: daily_rollup\nperiod: '2026-06-11'\n---\nlatin1 \xff byte\n"
    )
    result = import_legacy_run(cfg, store, summarizer=_BundleSummarizer(), now=NOW)
    assert result["skipped"] is None
    assert already_imported(store, bstore) is True


# ----- 2. source-date fallback ----------------------------------------------


def test_qa_dateless_must_memorize_row_falls_back_to_mtime(tmp_path: Path) -> None:
    # HOLDS: a row with a blank `Last bump` backdates to the file mtime — a real,
    # non-empty ISO timestamp — not now() and not an empty (invalid) timestamp.
    store, _bstore = _make_store(tmp_path)
    mm = store.paths.journal / "must_memorize.md"
    mm.write_text(
        dedent(
            """\
            | Score | Kind | Last bump | Source | Memo |
            |------:|------|-----------|--------|------|
            | 3 | preference | | extract | No date → mtime fallback. |
            """
        ),
        encoding="utf-8",
    )
    pin = read_legacy(store).pins[0]
    assert pin.source_date.endswith("Z") and pin.source_date  # mtime, real ts
    # and it backdates to roughly the file mtime, not the literal string "now".
    assert pin.source_date == _mtime_iso(mm)


def test_qa_dated_row_does_not_silently_use_now(tmp_path: Path) -> None:
    # HOLDS: when a real date IS present, the fallback does NOT override it with
    # now() — the explicit Last bump is honored.
    store, _bstore = _make_store(tmp_path)
    (store.paths.journal / "must_memorize.md").write_text(
        dedent(
            """\
            | Score | Kind | Last bump | Source | Memo |
            |------:|------|-----------|--------|------|
            | 5 | owner_explicit | 2025-03-04 | extract | Real date honored. |
            """
        ),
        encoding="utf-8",
    )
    assert read_legacy(store).pins[0].source_date == "2025-03-04T00:00:00Z"


def test_qa_garbage_nonISO_date_falls_back_not_passthrough(tmp_path: Path) -> None:
    # FIXED (D2, b2 REVISE): _normalise_source_date now validates a non-empty
    # value parses as ISO before passing it through; a garbage 'Last bump' like
    # 'yesterday' falls back to the mtime sentinel instead of poisoning the date.
    store, _bstore = _make_store(tmp_path)
    mm = store.paths.journal / "must_memorize.md"
    mm.write_text(
        dedent(
            """\
            | Score | Kind | Last bump | Source | Memo |
            |------:|------|-----------|--------|------|
            | 5 | owner_explicit | yesterday | extract | Garbage date. |
            """
        ),
        encoding="utf-8",
    )
    pin = read_legacy(store).pins[0]
    # EXPECTED (post-fix): an unparseable date falls back to the mtime sentinel
    # (a real ISO ts), never the literal garbage string. Currently 'yesterday'
    # passes straight through → this assertion fails (strict xfail).
    assert pin.source_date != "yesterday"
    assert pin.source_date == _mtime_iso(mm)


# ----- 3. idempotency under stress ------------------------------------------


def test_qa_partial_import_marker_unwritten_still_blocks(tmp_path: Path) -> None:
    # HOLDS: simulate a crash AFTER seed_entries but BEFORE mark_imported — the
    # store holds import-legacy entries but no marker. The detect-existing-seed
    # fallback still reports "imported" and a re-run no-ops (no double-seed).
    cfg = _load_cfg(tmp_path)
    store, bstore = _make_store(tmp_path)
    seeded = score_seed_candidates(
        _raw(must=[_raw_mr(kind="decision", importance=1.0)]), cfg=cfg, now=NOW
    )
    seed_entries(bstore, seeded)
    assert STATE_KEY not in (store.read_state() or {})  # marker never written
    assert already_imported(store, bstore) is True  # fallback guard catches it
    result = import_legacy_run(cfg, store, summarizer=_BundleSummarizer(), now=NOW)
    assert result == {"skipped": "already"}
    assert len(bstore.load("must_remember")) == 1  # no double-seed


def test_qa_double_invocation_back_to_back(tmp_path: Path) -> None:
    # HOLDS: two import_legacy_run calls in a row (sequential double-invoke) →
    # the first seeds, the second is gated; counts are stable.
    cfg = _load_cfg(tmp_path)
    store, bstore = _full_legacy_store(tmp_path)
    first = import_legacy_run(cfg, store, summarizer=_BundleSummarizer(), now=NOW)
    second = import_legacy_run(cfg, store, summarizer=_BundleSummarizer(), now=NOW)
    assert first["skipped"] is None
    assert second == {"skipped": "already"}
    counts = {n: len(bstore.load(n)) for n in ("skills", "must_remember", "diary")}
    assert counts == {"skills": 3, "must_remember": 7, "diary": 3}


def test_qa_force_purges_only_import_legacy_entries(tmp_path: Path) -> None:
    # HOLDS: --force purges ONLY import-legacy entries across ALL three stores,
    # leaving live extract/pin memory of every store untouched, then re-seeds.
    cfg = _load_cfg(tmp_path)
    store, bstore = _full_legacy_store(tmp_path)
    import_legacy_run(cfg, store, summarizer=_BundleSummarizer(), now=NOW)
    # seed a live (non-import) entry into EACH store.
    bstore.save_atomic("skills", bstore.load("skills") + [_skill(src="extract", name="live skill")])
    bstore.save_atomic(
        "must_remember",
        bstore.load("must_remember")
        + [MustRememberEntry(text="live directive", created_at=NOW, last_used=NOW,
                             source="pin", kind="owner_explicit", importance=5.0)],
    )
    import_legacy_run(cfg, store, summarizer=_BundleSummarizer(), now=NOW, force=True)
    # the live entry in every store survived; import-legacy entries were replaced.
    assert "live skill" in {e.name for e in bstore.load("skills")}
    assert "live directive" in {m.text for m in bstore.load("must_remember")}
    # Diary is source-less (compact format): a force-reimport resets + reseeds
    # it rather than selectively purging, so it holds exactly the fresh seed.
    assert bstore.load("diary"), "diary reseeded after force"
    # and the import-legacy entries are present exactly once (replace, not double).
    assert sum(1 for e in bstore.load("skills") if e.source == IMPORT_SOURCE) == 3


def test_qa_force_on_never_imported_store_is_clean_seed(tmp_path: Path) -> None:
    # HOLDS: --force on a store that was NEVER imported (purge is a no-op) still
    # seeds correctly and writes the marker.
    cfg = _load_cfg(tmp_path)
    store, bstore = _full_legacy_store(tmp_path)
    result = import_legacy_run(
        cfg, store, summarizer=_BundleSummarizer(), now=NOW, force=True
    )
    assert result == {"skipped": None, "skills": 3, "must_remember": 7, "diary": 3}
    assert already_imported(store, bstore) is True


# ----- 4. read-before-drop on the error paths -------------------------------


def test_qa_import_unlinks_nothing_even_with_bad_rollup(tmp_path: Path) -> None:
    # HOLDS: even when a rollup re-author FAILS (parse error swallowed), the
    # import unlinks no legacy file and never calls rebuild — read-before-drop
    # holds on the error path too.
    cfg = _load_cfg(tmp_path)
    store, _bstore = _full_legacy_store(tmp_path)
    before = {p.name for p in store.paths.journal.glob("*.md")}
    # _JunkSummarizer makes every rollup re-author fail (swallowed to empty).
    import_legacy_run(cfg, store, summarizer=_JunkSummarizer(), now=NOW)
    after = {p.name for p in store.paths.journal.glob("*.md")}
    assert before <= after  # nothing dropped
    assert "must_memorize.md" in after


def test_qa_import_never_imports_rebuild(tmp_path: Path, monkeypatch) -> None:
    # HOLDS: the orchestrator never invokes rebuild (the only legacy-dropping
    # step). Poison the symbol so any accidental call would raise.
    import tigerharness.tiger_memory.import_legacy as il

    def _boom(*a, **k):  # pragma: no cover - asserted never called
        raise AssertionError("import_legacy_run must NEVER call rebuild")

    # rebuild is not imported into import_legacy's namespace, so guard the source
    # module too: confirm the name simply isn't referenced in the run path.
    monkeypatch.setattr("tigerharness.tiger_memory.lifecycle.rebuild", _boom, raising=False)
    cfg = _load_cfg(tmp_path)
    store, _bstore = _full_legacy_store(tmp_path)
    result = import_legacy_run(cfg, store, summarizer=_BundleSummarizer(), now=NOW)
    assert result["skipped"] is None  # completed without touching rebuild


# ----- 5. re-author parse failures degrade safely ---------------------------


def test_qa_reauthor_junk_bundle_seeds_nothing(tmp_path: Path) -> None:
    # HOLDS: a summarizer returning marker-less junk → ExtractionParseError,
    # swallowed per rollup → empty candidates. The rollup half mines nothing; the
    # mechanical pins still seed (so a junk model never loses the pins).
    cfg = _load_cfg(tmp_path)
    store, bstore = _full_legacy_store(tmp_path)
    result = import_legacy_run(cfg, store, summarizer=_JunkSummarizer(), now=NOW)
    assert result["skills"] == 0 and result["diary"] == 0
    assert result["must_remember"] == 4  # only the 4 mechanical pins
    assert already_imported(store, bstore) is True


def test_qa_reauthor_partial_bundle_seeds_nothing(tmp_path: Path) -> None:
    # HOLDS: a bundle with only one of the three required markers →
    # missing-markers ExtractionParseError → swallowed to empty (no garbage seed).
    cfg = _load_cfg(tmp_path)
    store, _bstore = _make_store(tmp_path)
    _write_rollups(store)
    src = read_legacy(store)
    out = reauthor(cfg, _PartialBundleSummarizer(), src)
    assert out.is_empty()


def test_qa_reauthor_empty_bundle_seeds_nothing(tmp_path: Path) -> None:
    # HOLDS: an empty-string summarizer output → "empty extraction output" parse
    # error → swallowed to empty candidates (no crash, no garbage).
    cfg = _load_cfg(tmp_path)
    store, _bstore = _make_store(tmp_path)
    _write_rollups(store)
    src = read_legacy(store)
    out = reauthor(cfg, _EmptyBundleSummarizer(), src)
    assert out.is_empty()

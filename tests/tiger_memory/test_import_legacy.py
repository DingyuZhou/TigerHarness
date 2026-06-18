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
    LegacyRollup,
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


# ===== legacy reader + re-author + orchestrator + CLI (b1-dev-3, Miyagi) =====


class _BundleSummarizer(Summarizer):
    """A scripted summarizer emitting one valid re-author bundle per call.

    The deterministic ``MockSummarizer`` returns prose bullets that do NOT
    satisfy the strict ``@@SKILLS@@/.../@@EMOTIONAL@@`` marker contract (so the
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
            "@@EMOTIONAL@@\n"
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
    assert not out.skills and not out.emotional and not out.must_remember


def test_reauthor_scripted_bundle_mines_all_stores(tmp_path: Path) -> None:
    cfg = _load_cfg(tmp_path)
    store, _bstore = _make_store(tmp_path)
    _write_rollups(store)
    src = read_legacy(store)
    out = reauthor(cfg, _BundleSummarizer(), src)
    # one bundle per non-empty rollup (3) -> 3 skills, 3 emotional, 3 mr.
    assert len(out.skills) == 3
    assert len(out.emotional) == 3
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
        "skipped": None, "skills": 3, "must_remember": 7, "emotional": 3
    }
    # the durable marker is written.
    assert already_imported(store, bstore) is True
    assert store.read_state()[STATE_KEY]["seeded"]["skills"] == 3
    # every seeded entry carries the import provenance + is backdated (not NOW
    # for the rollup/pin items).
    for name in ("skills", "must_remember", "emotional"):
        seeded = bstore.load(name)
        assert seeded and all(e.source == IMPORT_SOURCE for e in seeded)
    # old emotional seeds entered already-decayed (source < now).
    emo = bstore.load("emotional")[0]
    assert emo.created_at != NOW


def test_import_legacy_run_idempotent_no_double_seed(tmp_path: Path) -> None:
    cfg = _load_cfg(tmp_path)
    store, bstore = _full_legacy_store(tmp_path)
    first = import_legacy_run(cfg, store, summarizer=_BundleSummarizer(), now=NOW)
    counts_before = {n: len(bstore.load(n)) for n in ("skills", "must_remember", "emotional")}
    # second run gates on the marker -> a no-op; stores unchanged.
    second = import_legacy_run(cfg, store, summarizer=_BundleSummarizer(), now=NOW)
    assert first["skipped"] is None
    assert second == {"skipped": "already"}
    counts_after = {n: len(bstore.load(n)) for n in ("skills", "must_remember", "emotional")}
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
    assert result == {"skipped": None, "skills": 0, "must_remember": 0, "emotional": 0}
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
    # store files skills.md/must_remember.md/emotional.md).
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

"""Tests for staged compaction (compaction.py, ADR 0007).

Covers ``compact_plan`` (deterministic stale-topic forget + one staged
prompt per over-bound surface), ``compact_apply`` (card contracts per
target kind, malformed-card handling, deterministic convergence trims,
protected-content still_over reporting), and the small pure helpers
(``_split_dated_sections``, ``_section_after_marker``, ``_is_fresh`` /
``_is_stale`` boundaries). Pure Python, no model calls.
"""
from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path

import pytest
import yaml

from tigerharness.tiger_memory import compaction as cp
from tigerharness.tiger_memory import indexes
from tigerharness.tiger_memory.bounded_store import BoundedStore
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.entries import (
    STORE_MUST_REMEMBER,
    STORE_SKILLS,
    STORE_TOPICS,
    MustRememberEntry,
    SkillEntry,
    TopicEntry,
)
from tigerharness.tiger_memory.store import Store

NOW = "2026-07-23T12:00:00Z"
FRESH = "2026-07-22T00:00:00Z"          # 1.5 days ago
NOT_FRESH = "2026-06-20T00:00:00Z"      # ~33 days: not fresh, not stale
STALE_1 = "2026-01-05T00:00:00Z"        # stale (oldest)
STALE_2 = "2026-02-05T00:00:00Z"        # stale (newer)


# ----- fixtures / helpers -----------------------------------------------------


def make_env(tmp_path: Path, memory: dict | None = None):
    """A loaded config + initialized store, with optional ``memory:`` bounds."""
    raw = {
        "agent": {"name": "Aya", "role": "r"},
        "store": {"root": str(tmp_path / "memory")},
        "sources": [{"kind": "claude_code", "project_path": f"{tmp_path}/p/"}],
        "summarizer": {"backend": "anthropic", "model": "m", "prompts": "default/v1"},
        "rebuild": {"lock_path": str(tmp_path / "test.lock")},
    }
    if memory is not None:
        raw["memory"] = memory
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(yaml.safe_dump(raw))
    cfg = load_config(cfg_path)
    store = Store(cfg.store.root)
    store.init_layout()
    return cfg, store, BoundedStore(cfg, store)


def _skill(
    name: str = "Skill",
    trigger: str = "t",
    proc: str = "p",
    *,
    usage: int = 0,
    importance: float = 0.0,
    last: str = NOW,
    created: str = NOW,
) -> SkillEntry:
    return SkillEntry(
        text=proc, created_at=created, last_used=last, source="test",
        name=name, trigger=trigger, procedure=proc,
        usage_count=usage, importance=importance,
    )


def _topic(
    name: str,
    *,
    last: str = NOW,
    summary: str = "sum",
    text: str = "## 2026-07-01\n- a",
    touch: int = 1,
) -> TopicEntry:
    return TopicEntry(
        text=text, created_at="2026-01-01T00:00:00Z", last_used=last,
        source="test", name=name, summary=summary, touch_count=touch,
    )


def _memo(
    text: str = "memo",
    *,
    kind: str = "preference",
    repeats: int = 1,
    last: str = NOW,
) -> MustRememberEntry:
    return MustRememberEntry(
        text=text, created_at="2026-01-01T00:00:00Z", last_used=last,
        source="test", kind=kind, repeat_count=repeats,
    )


def _stage(store: Store, targets: list[dict]) -> Path:
    """Hand-build a staging dir + manifest (apply is plan-independent)."""
    staging = store.root / cp.STAGING_DIR_NAME
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "manifest.json").write_text(
        json.dumps({
            "generated_at": NOW, "dropped_stale_topics": [], "targets": targets,
        }),
        encoding="utf-8",
    )
    return staging


def _target(staging: Path, kind: str, key: str, **extra) -> dict:
    t = {
        "kind": kind,
        "key": key,
        "prompt_path": str(staging / f"{key}.prompt.md"),
        "card_path": str(staging / f"{key}.card.md"),
    }
    t.update(extra)
    return t


def _card(staging: Path, key: str, text: str) -> Path:
    p = staging / f"{key}.card.md"
    p.write_text(text, encoding="utf-8")
    return p


def _roster_line(prompt: str, slug: str) -> str:
    """The one roster line for *slug* in a staged topic_roster prompt."""
    return next(
        line for line in prompt.splitlines() if line.startswith(f"- `{slug}`")
    )


# ----- freshness / staleness boundaries ---------------------------------------


def test_is_fresh_boundary_inclusive():
    assert cp._is_fresh(_topic("T", last="2026-07-16T12:00:00Z"), NOW, 7)
    assert not cp._is_fresh(_topic("T", last="2026-07-16T11:59:59Z"), NOW, 7)


def test_is_stale_boundary_exclusive():
    assert not cp._is_stale(_topic("T", last="2026-05-24T12:00:00Z"), NOW, 60)
    assert cp._is_stale(_topic("T", last="2026-05-24T11:59:59Z"), NOW, 60)


def test_unparseable_last_used_is_infinitely_old():
    # Hardening pass: a corrupt freshness anchor is INFINITELY OLD (never
    # fresh, always stale) — the old 0.0-age reading made the entry
    # eternally fresh, i.e. immune to forget/merge/trim, a permanent
    # still_over poison pill. `check --fix` repairs the anchor; until it
    # does, the entry must not block convergence.
    t = _topic("T", last="not-a-date")
    assert not cp._is_fresh(t, NOW, 7)
    assert cp._is_stale(t, NOW, 60)
    assert cp._age_days("not-a-date", NOW) == float("inf")
    assert cp._age_days(None, NOW) == float("inf")


# ----- _split_dated_sections ---------------------------------------------------


def test_split_dated_sections_no_heading():
    body = "just prose\n- bullet\n"
    assert cp._split_dated_sections(body) == [body]


def test_split_dated_sections_single_heading():
    body = "## 2026-07-01\n- a\n"
    assert cp._split_dated_sections(body) == [body]


def test_split_dated_sections_multi_lossless():
    s1 = "## 2026-01-01\n- a\n\n"
    s2 = "## 2026-02-02   \n- b\n\n"  # trailing spaces on the heading still match
    s3 = "## 2026-03-03\n- c"
    parts = cp._split_dated_sections(s1 + s2 + s3)
    assert parts == [s1, s2, s3]
    assert "".join(parts) == s1 + s2 + s3


def test_split_dated_sections_preamble_stays_with_first():
    s1 = "## 2026-01-01\n- a\n\n"
    s2 = "## 2026-02-02\n- b"
    body = "(earlier digest)\n\n" + s1 + s2
    parts = cp._split_dated_sections(body)
    assert parts == ["(earlier digest)\n\n" + s1, s2]


# ----- _section_after_marker ----------------------------------------------------


def test_section_after_marker_empty_card():
    with pytest.raises(cp.CompactionParseError, match="empty compaction card"):
        cp._section_after_marker("   \n ", cp.MARK_SKILLS)


def test_section_after_marker_missing_marker():
    with pytest.raises(cp.CompactionParseError, match="missing marker"):
        cp._section_after_marker("no marker here\n", cp.MARK_SKILLS)


def test_section_after_marker_whole_line_only():
    # An inline echo of the marker is not a match; a padded whole line is.
    text = "quote: @@SKILLS@@ inline\n  @@SKILLS@@  \nbody line\n"
    assert cp._section_after_marker(text, cp.MARK_SKILLS) == "body line"


# ----- compact_plan -------------------------------------------------------------


def test_plan_nothing_over_stages_no_targets(tmp_path):
    cfg, store, bstore = make_env(tmp_path)
    bstore.save_atomic(STORE_SKILLS, [_skill("Small")])
    bstore.save_atomic(STORE_MUST_REMEMBER, [_memo("tiny")])
    bstore.save_atomic(STORE_TOPICS, [_topic("Small Topic")])
    # Pre-existing staging junk is wiped by a re-plan.
    staging = store.root / cp.STAGING_DIR_NAME
    staging.mkdir(parents=True)
    (staging / "junk.txt").write_text("old")
    manifest = cp.compact_plan(cfg, store)  # default now= branch
    assert manifest["targets"] == []
    assert manifest["dropped_stale_topics"] == []
    assert manifest["generated_at"]
    assert not (staging / "junk.txt").exists()
    on_disk = json.loads((staging / "manifest.json").read_text())
    assert on_disk == manifest


def test_plan_stale_forget_stops_once_under_max(tmp_path):
    old1 = _topic("Old One", last=STALE_1)
    old2 = _topic("Old Two", last=STALE_2)
    mid = _topic("Mid Topic", last=NOT_FRESH)   # inside window: never dropped
    fresh = _topic("Fresh Topic", last=FRESH)
    full = len(indexes.render_topic_index([old1, old2, mid, fresh]))
    after1 = len(indexes.render_topic_index([old2, mid, fresh]))
    cfg, store, bstore = make_env(tmp_path, memory={"topics": {
        "index_max_length": after1, "index_overflow_limit": full,
    }})
    bstore.save_atomic(STORE_TOPICS, [old1, old2, mid, fresh])
    manifest = cp.compact_plan(cfg, store, now=NOW)
    # Only the oldest stale topic goes: dropping it reached max, so the
    # (also stale) old-two survives; the non-stale mid was never eligible.
    assert manifest["dropped_stale_topics"] == ["old-one"]
    assert manifest["targets"] == []
    assert manifest["generated_at"] == NOW
    slugs = {t.slug for t in bstore.load(STORE_TOPICS)}
    assert slugs == {"old-two", "mid-topic", "fresh-topic"}


def test_plan_stale_forget_exhausts_then_stages_roster(tmp_path):
    s1 = _topic("Gone One", last=STALE_1)
    s2 = _topic("Gone Two", last=STALE_2)
    fresh = _topic("Fresh Big", last=FRESH, summary="S" * 400)
    assert len(indexes.render_topic_index([fresh])) >= 250  # still over after
    cfg, store, bstore = make_env(tmp_path, memory={"topics": {
        "index_max_length": 200, "index_overflow_limit": 250,
    }})
    bstore.save_atomic(STORE_TOPICS, [s1, s2, fresh])
    manifest = cp.compact_plan(cfg, store, now=NOW)
    # Deterministic forget order: oldest first, all stale exhausted.
    assert manifest["dropped_stale_topics"] == ["gone-one", "gone-two"]
    assert [t["kind"] for t in manifest["targets"]] == ["topic_roster"]
    assert [t.slug for t in bstore.load(STORE_TOPICS)] == ["fresh-big"]
    prompt = Path(manifest["targets"][0]["prompt_path"]).read_text()
    assert "[fresh]" in _roster_line(prompt, "fresh-big")


def test_plan_over_without_stale_marks_fresh_only(tmp_path):
    fresh = _topic("Fresh One", last=FRESH, summary="S" * 150)
    older = _topic("Older One", last=NOT_FRESH, summary="T" * 150)
    cfg, store, bstore = make_env(tmp_path, memory={"topics": {
        "index_max_length": 200, "index_overflow_limit": 250,
    }})
    bstore.save_atomic(STORE_TOPICS, [fresh, older])
    manifest = cp.compact_plan(cfg, store, now=NOW)
    assert manifest["dropped_stale_topics"] == []
    [target] = manifest["targets"]
    assert target["kind"] == "topic_roster"
    assert len(bstore.load(STORE_TOPICS)) == 2  # nothing force-dropped
    prompt = Path(target["prompt_path"]).read_text()
    assert "[fresh]" in _roster_line(prompt, "fresh-one")
    assert "[fresh]" not in _roster_line(prompt, "older-one")
    # Roster is freshest-first: the fresh topic's line comes first.
    assert prompt.index("`fresh-one`") < prompt.index("`older-one`")


def test_plan_stages_every_target_kind(tmp_path):
    cfg, store, bstore = make_env(tmp_path, memory={
        "skills": {
            "index_max_length": 200, "index_overflow_limit": 220,
            "detail_max_length": 200, "detail_overflow_limit": 260,
        },
        "must_remember": {"max_length": 30, "overflow_limit": 40},
        "topics": {
            "index_max_length": 200, "index_overflow_limit": 220,
            "detail_max_length": 200, "detail_overflow_limit": 260,
        },
    })
    # Team mission for the must_remember prompt.
    team_root = cfg.store.root.parent.parent
    (team_root / "charter").mkdir(parents=True, exist_ok=True)
    (team_root / "charter" / "README.md").write_text("MISSION SENTINEL ALPHA\n")

    t_long = _topic(
        "Long Topic", last=FRESH, summary="S" * 120,
        text="## 2026-07-01\n- " + "D" * 300,
    )
    t_other = _topic("Other Topic", last=NOT_FRESH, summary="S" * 120)
    bstore.save_atomic(STORE_TOPICS, [t_long, t_other])

    s_hot = _skill("Hot Skill", trigger="T" * 120, proc="p", usage=9, last=FRESH)
    s_big = _skill("Big Skill", trigger="t", proc="P" * 300)
    bstore.save_atomic(STORE_SKILLS, [s_hot, s_big])

    op = _memo("OP-KEEP!!", kind="operator_explicit")
    pref = _memo("x" * 35)
    bstore.save_atomic(STORE_MUST_REMEMBER, [op, pref])

    manifest = cp.compact_plan(cfg, store, now=NOW)
    assert manifest["dropped_stale_topics"] == []  # over-bound but nothing stale
    targets = {t["kind"]: t for t in manifest["targets"]}
    # Detail targets are DEFERRED whenever their index-level target is also
    # staged (a roster merge / index replacement would clobber or dangle a
    # same-run detail rewrite) — so this plan stages the three index-level
    # kinds only; the oversized details re-stage next sweep.
    assert set(targets) == {"topic_roster", "skills", "must_remember"}
    assert len(manifest["targets"]) == 3

    # Manifest shape: keys, staged paths, snapshot ids on replacement kinds.
    staging = store.root / cp.STAGING_DIR_NAME
    for t in manifest["targets"]:
        assert Path(t["prompt_path"]).parent == staging
        assert Path(t["prompt_path"]).exists()
        assert t["card_path"] == str(staging / f"{t['key']}{cp.CARD_SUFFIX}")
    assert set(targets["skills"]["snapshot_ids"]) == {s_hot.id, s_big.id}
    assert set(targets["must_remember"]["snapshot_ids"]) == {op.id, pref.id}
    on_disk = json.loads((staging / "manifest.json").read_text())
    assert on_disk == manifest

    # Roster prompt: only the fresh topic carries the [fresh] mark.
    roster_prompt = Path(targets["topic_roster"]["prompt_path"]).read_text()
    assert "[fresh]" in _roster_line(roster_prompt, "long-topic")
    assert "[fresh]" not in _roster_line(roster_prompt, "other-topic")

    # Skills prompt is keep-rank ordered, most important first.
    skills_prompt = Path(targets["skills"]["prompt_path"]).read_text()
    assert skills_prompt.index("Hot Skill") < skills_prompt.index("Big Skill")


    # must_remember prompt: mission text, protected block, remaining budget
    # (max_length 30 minus the 9-char protected memo = 21, floored at the
    # hardening pass's 200-char minimum so the card author never sees a
    # nonsense near-zero target).
    mr_prompt = Path(targets["must_remember"]["prompt_path"]).read_text()
    assert "MISSION SENTINEL ALPHA" in mr_prompt
    assert "MEMO: OP-KEEP!!" in mr_prompt
    assert "x" * 35 in mr_prompt
    assert "at most 200" in mr_prompt


def test_plan_stages_detail_kinds_when_indexes_fit(tmp_path):
    """Detail targets stage (with slug/entry_id addressing and full embedded
    bodies) when the indexes themselves are under bound — no deferral."""
    cfg, store, bstore = make_env(tmp_path, memory={
        "skills": {
            "index_max_length": 4000, "index_overflow_limit": 6000,
            "detail_max_length": 200, "detail_overflow_limit": 260,
        },
        "topics": {
            "index_max_length": 4000, "index_overflow_limit": 6000,
            "detail_max_length": 200, "detail_overflow_limit": 260,
        },
    })
    t_long = _topic(
        "Long Topic", last=FRESH,
        text="## 2026-07-01\n- " + "D" * 300,
    )
    bstore.save_atomic(STORE_TOPICS, [t_long])
    s_big = _skill("Big Skill", trigger="t", proc="P" * 300)
    bstore.save_atomic(STORE_SKILLS, [s_big])

    manifest = cp.compact_plan(cfg, store, now=NOW)
    targets = {t["kind"]: t for t in manifest["targets"]}
    assert set(targets) == {"topic_detail", "skill_detail"}
    assert targets["topic_detail"]["slug"] == "long-topic"
    assert targets["topic_detail"]["key"] == "topic_detail.long-topic"
    assert targets["skill_detail"]["entry_id"] == s_big.id
    assert targets["skill_detail"]["key"] == f"skill_detail.{s_big.id}"
    assert "D" * 300 in Path(targets["topic_detail"]["prompt_path"]).read_text()
    assert "P" * 300 in Path(targets["skill_detail"]["prompt_path"]).read_text()


def test_plan_must_remember_no_charter_no_protected(tmp_path):
    cfg, store, bstore = make_env(tmp_path, memory={
        "must_remember": {"max_length": 30, "overflow_limit": 40},
    })
    bstore.save_atomic(STORE_MUST_REMEMBER, [_memo("x" * 45)])
    manifest = cp.compact_plan(cfg, store, now=NOW)
    [target] = manifest["targets"]
    assert target["kind"] == "must_remember"
    prompt = Path(target["prompt_path"]).read_text()
    assert "(no charter mission found)" in prompt   # missing charter fallback
    assert "(none)" in prompt                       # no protected entries
    # Hardening pass: the advertised budget is floored at 200 so a card
    # author is never told "compact to ≤ tiny/zero chars" (the tiny test
    # bound of 30 lands on the floor).
    assert "at most 200" in prompt


# ----- compact_apply: manifest / dispatch --------------------------------------


def test_apply_without_manifest_raises(tmp_path):
    cfg, store, _ = make_env(tmp_path)
    with pytest.raises(FileNotFoundError, match="compact-plan"):
        cp.compact_apply(cfg, store, now=NOW)


def test_apply_missing_card_is_skipped(tmp_path):
    cfg, store, _ = make_env(tmp_path)
    staging = _stage(store, [])
    _stage(store, [_target(staging, cp.KIND_MUST_REMEMBER, "must_remember")])
    report = cp.compact_apply(cfg, store)  # default now= branch
    assert report.skipped_no_card == ["must_remember"]
    assert report.applied == [] and report.malformed == []


def test_apply_unknown_target_kind_is_malformed(tmp_path):
    cfg, store, _ = make_env(tmp_path)
    staging = store.root / cp.STAGING_DIR_NAME
    staging.mkdir(parents=True)
    t = _target(staging, "wat", "wat")
    _stage(store, [t])
    _card(staging, "wat", "@@ANYTHING@@\nstuff\n")
    report = cp.compact_apply(cfg, store, now=NOW)
    assert report.applied == []
    [bad] = report.malformed
    assert bad["key"] == "wat" and "unknown target kind 'wat'" in bad["error"]
    assert Path(t["card_path"]).exists()  # malformed cards are kept


def test_apply_empty_and_markerless_cards_are_malformed(tmp_path):
    cfg, store, bstore = make_env(tmp_path)
    bstore.save_atomic(STORE_MUST_REMEMBER, [_memo("keep me around")])
    staging = store.root / cp.STAGING_DIR_NAME
    staging.mkdir(parents=True)
    targets = [
        _target(staging, cp.KIND_MUST_REMEMBER, "empty"),
        _target(staging, cp.KIND_MUST_REMEMBER, "markerless"),
    ]
    _stage(store, targets)
    _card(staging, "empty", "")
    _card(staging, "markerless", "just prose, no marker\n")
    report = cp.compact_apply(cfg, store, now=NOW)
    errors = {m["key"]: m["error"] for m in report.malformed}
    assert "empty compaction card" in errors["empty"]
    assert "missing marker" in errors["markerless"]
    # Store untouched by malformed cards.
    assert [e.text for e in bstore.load(STORE_MUST_REMEMBER)] == ["keep me around"]


def test_apply_mixed_deletes_only_applied_staging_files(tmp_path):
    cfg, store, bstore = make_env(tmp_path)
    bstore.save_atomic(STORE_MUST_REMEMBER, [_memo("junk", kind="decision")])
    staging = store.root / cp.STAGING_DIR_NAME
    staging.mkdir(parents=True)
    targets = [
        _target(staging, cp.KIND_MUST_REMEMBER, "good"),
        _target(staging, cp.KIND_MUST_REMEMBER, "nocard"),
        _target(staging, cp.KIND_MUST_REMEMBER, "bad"),
    ]
    _stage(store, targets)
    for key in ("good", "nocard", "bad"):
        (staging / f"{key}.prompt.md").write_text("prompt")
    _card(staging, "good", "@@MUST_REMEMBER@@\nKIND: preference\nMEMO: compact memo\n")
    _card(staging, "bad", "no marker\n")
    report = cp.compact_apply(cfg, store, now=NOW)
    assert report.to_dict() == {
        "applied": ["good"],
        "skipped_no_card": ["nocard"],
        "malformed": [{"key": "bad", "error": "missing marker @@MUST_REMEMBER@@"}],
        "forced_trims": [],
        "still_over": [],
        "locked": [],
    }
    # Applied target's staging files are gone; the others stay for a re-run.
    assert not (staging / "good.prompt.md").exists()
    assert not (staging / "good.card.md").exists()
    assert (staging / "nocard.prompt.md").exists()
    assert (staging / "bad.prompt.md").exists()
    assert (staging / "bad.card.md").exists()
    # Hardening pass: the manifest survives while a malformed target still
    # needs a targeted retry (only a fully-clean apply consumes it).
    assert (staging / "manifest.json").exists()


# ----- compact_apply: must_remember ---------------------------------------------


def _mr_env(tmp_path, memory=None):
    cfg, store, bstore = make_env(tmp_path, memory)
    staging = store.root / cp.STAGING_DIR_NAME
    staging.mkdir(parents=True)
    t = _target(staging, cp.KIND_MUST_REMEMBER, "must_remember")
    _stage(store, [t])
    return cfg, store, bstore, staging, t


def test_apply_must_remember_replaces_and_protects(tmp_path):
    cfg, store, bstore, staging, t = _mr_env(tmp_path)
    op = _memo("OP DIRECTIVE", kind="operator_explicit")
    bstore.save_atomic(
        STORE_MUST_REMEMBER, [op, _memo("old junk"), _memo("more junk", kind="decision")]
    )
    _card(staging, "must_remember", (
        "@@MUST_REMEMBER@@\n"
        "KIND: operator_explicit\nMEMO: sneaky new directive\n"
        "\n"
        "KIND: preference\nMEMO: compacted memo\n"
    ))
    report = cp.compact_apply(cfg, store, now=NOW)
    assert report.applied == ["must_remember"]
    assert report.forced_trims == [] and report.still_over == []
    entries = bstore.load(STORE_MUST_REMEMBER)
    # Protected entry carried over verbatim (same id), card's operator block
    # ignored, all non-protected content replaced wholesale.
    assert [e.text for e in entries] == ["OP DIRECTIVE", "compacted memo"]
    assert entries[0].id == op.id
    assert entries[1].kind == "preference"
    assert entries[1].source == "compact"
    assert entries[1].created_at == NOW
    assert not Path(t["card_path"]).exists()


def test_apply_must_remember_forced_trim_drops_lowest_first(tmp_path):
    cfg, store, bstore, staging, _ = _mr_env(tmp_path, memory={
        "must_remember": {"max_length": 40, "overflow_limit": 50},
    })
    bstore.save_atomic(
        STORE_MUST_REMEMBER,
        [_memo("P" * 12, kind="operator_explicit"), _memo("J" * 30)],
    )
    _card(staging, "must_remember", (
        "@@MUST_REMEMBER@@\n"
        f"KIND: preference\nMEMO: {'a' * 20}\n"
        "\n"
        f"KIND: decision\nMEMO: {'b' * 20}\n"
    ))
    report = cp.compact_apply(cfg, store, now=NOW)
    # 12 + 20 + 20 = 52 > 40: equal recurrence/recency → first-listed drops
    # first; one drop reaches 32 ≤ 40.
    assert report.forced_trims == [STORE_MUST_REMEMBER]
    assert report.still_over == []
    assert [e.text for e in bstore.load(STORE_MUST_REMEMBER)] == ["P" * 12, "b" * 20]


def test_apply_must_remember_still_over_when_protected_exceed_max(tmp_path):
    cfg, store, bstore, staging, _ = _mr_env(tmp_path, memory={
        "must_remember": {"max_length": 20, "overflow_limit": 30},
    })
    op = _memo("P" * 40, kind="operator_explicit")
    bstore.save_atomic(STORE_MUST_REMEMBER, [op])
    _card(staging, "must_remember", "@@MUST_REMEMBER@@\nNONE\n")
    report = cp.compact_apply(cfg, store, now=NOW)
    assert report.forced_trims == [STORE_MUST_REMEMBER]
    assert report.still_over == [STORE_MUST_REMEMBER]
    # Protected content is never force-dropped.
    assert [e.text for e in bstore.load(STORE_MUST_REMEMBER)] == ["P" * 40]


def test_apply_must_remember_bad_block_is_malformed(tmp_path):
    cfg, store, bstore, staging, _ = _mr_env(tmp_path)
    bstore.save_atomic(STORE_MUST_REMEMBER, [_memo("keep")])
    _card(staging, "must_remember", "@@MUST_REMEMBER@@\nKIND: bogus\nMEMO: m\n")
    report = cp.compact_apply(cfg, store, now=NOW)
    [bad] = report.malformed
    assert "bad must_remember block" in bad["error"]
    assert [e.text for e in bstore.load(STORE_MUST_REMEMBER)] == ["keep"]


# ----- compact_apply: skills -----------------------------------------------------


def _skills_env(tmp_path, memory=None):
    cfg, store, bstore = make_env(tmp_path, memory)
    staging = store.root / cp.STAGING_DIR_NAME
    staging.mkdir(parents=True)
    t = _target(staging, cp.KIND_SKILLS, "skills")
    _stage(store, [t])
    return cfg, store, bstore, staging, t


def test_apply_skills_carries_over_usage_by_name_case_insensitive(tmp_path):
    cfg, store, bstore, staging, _ = _skills_env(tmp_path)
    bstore.save_atomic(STORE_SKILLS, [_skill(
        "Deploy Fix", usage=5,
        created="2026-01-01T00:00:00Z", last="2026-05-01T00:00:00Z",
    )])
    _card(staging, "skills", (
        "@@SKILLS@@\n"
        "NAME: deploy fix\nTRIGGER: new trigger\nPROCEDURE: new proc\n"
        "\n"
        "NAME: Brand New\nTRIGGER: bt\nPROCEDURE: bp\n"
    ))
    report = cp.compact_apply(cfg, store, now=NOW)
    assert report.applied == ["skills"]
    assert report.forced_trims == [] and report.still_over == []
    by_name = {e.name: e for e in bstore.load(STORE_SKILLS)}
    kept = by_name["deploy fix"]
    assert kept.usage_count == 5
    assert kept.created_at == "2026-01-01T00:00:00Z"
    assert kept.last_used == "2026-05-01T00:00:00Z"
    assert kept.importance == pytest.approx(math.log1p(5))
    assert kept.trigger == "new trigger" and kept.procedure == "new proc"
    new = by_name["Brand New"]
    assert new.usage_count == 0 and new.importance == 0.0
    assert new.created_at == NOW and new.source == "compact"


def test_apply_skills_forced_trim_drops_lowest_keep_rank(tmp_path):
    keep_dummy = _skill("Keep Me", trigger="kt", proc="kp")
    drop_dummy = _skill("Drop Me", trigger="dt", proc="dp")
    keep_len = len(indexes.render_skill_index([keep_dummy]))
    full_len = len(indexes.render_skill_index([keep_dummy, drop_dummy]))
    cfg, store, bstore, staging, _ = _skills_env(tmp_path, memory={
        "skills": {"index_max_length": keep_len, "index_overflow_limit": full_len},
    })
    bstore.save_atomic(STORE_SKILLS, [_skill(
        "Keep Me", usage=9,
        created="2026-01-01T00:00:00Z", last="2026-05-01T00:00:00Z",
    )])
    _card(staging, "skills", (
        "@@SKILLS@@\n"
        "NAME: Keep Me\nTRIGGER: kt\nPROCEDURE: kp\n"
        "\n"
        "NAME: Drop Me\nTRIGGER: dt\nPROCEDURE: dp\n"
    ))
    report = cp.compact_apply(cfg, store, now=NOW)
    assert report.forced_trims == [STORE_SKILLS]
    assert report.still_over == []
    [survivor] = bstore.load(STORE_SKILLS)
    # The never-used skill went first despite being fresher; the carried-over
    # usage kept "Keep Me" on top of the keep-rank.
    assert survivor.name == "Keep Me"
    assert survivor.usage_count == 9
    assert survivor.last_used == "2026-05-01T00:00:00Z"


def test_apply_skills_still_over_when_even_empty_index_exceeds_max(tmp_path):
    cfg, store, bstore, staging, _ = _skills_env(tmp_path, memory={
        "skills": {"index_max_length": 10, "index_overflow_limit": 20},
    })
    bstore.save_atomic(STORE_SKILLS, [_skill("Existing")])
    _card(staging, "skills", "@@SKILLS@@\nNONE\n")
    report = cp.compact_apply(cfg, store, now=NOW)
    assert report.forced_trims == [STORE_SKILLS]
    assert report.still_over == [STORE_SKILLS]
    assert bstore.load(STORE_SKILLS) == []  # full replacement roster wins


def test_apply_skills_bad_block_is_malformed(tmp_path):
    cfg, store, bstore, staging, _ = _skills_env(tmp_path)
    bstore.save_atomic(STORE_SKILLS, [_skill("Keep")])
    _card(staging, "skills", "@@SKILLS@@\nNAME: X\nPROCEDURE: p\n")  # no TRIGGER
    report = cp.compact_apply(cfg, store, now=NOW)
    [bad] = report.malformed
    assert "bad skill block" in bad["error"]
    assert [e.name for e in bstore.load(STORE_SKILLS)] == ["Keep"]


# ----- compact_apply: topic_roster ------------------------------------------------


def _roster_env(tmp_path, memory=None):
    cfg, store, bstore = make_env(tmp_path, memory)
    staging = store.root / cp.STAGING_DIR_NAME
    staging.mkdir(parents=True)
    t = _target(staging, cp.KIND_TOPIC_ROSTER, "topic_roster")
    _stage(store, [t])
    return cfg, store, bstore, staging, t


def test_apply_roster_forget_merge_summary(tmp_path, caplog):
    cfg, store, bstore, staging, _ = _roster_env(tmp_path)
    alpha = _topic("Alpha", last="2026-07-01T00:00:00Z",
                   text="## 2026-06-01\n- a", touch=2)
    beta = _topic("Beta", last="2026-06-15T00:00:00Z",
                  text="## 2026-05-01\n- b", touch=3)
    gamma = _topic("Gamma", last=FRESH)
    delta = _topic("Delta", last=NOT_FRESH)
    eps = _topic("Eps", last="2026-07-21T00:00:00Z")
    zeta = _topic("Zeta", last=NOT_FRESH)
    bstore.save_atomic(STORE_TOPICS, [alpha, beta, gamma, delta, eps, zeta])
    _card(staging, "topic_roster", (
        "@@TOPIC_ROSTER@@\n"
        "ACTION: forget\nTOPIC: delta\n"
        "\n"
        "ACTION: forget\nTOPIC: eps\n"          # fresh → refused
        "\n"
        "ACTION: forget\nTOPIC: nope\n"          # unknown slug → ignored
        "\n"
        "ACTION: merge\nINTO: alpha\nFROM: beta gamma missing alpha\n"
        "SUMMARY: merged summary\n"
        "\n"
        "ACTION: merge\nINTO: absent\nFROM: zeta\n"   # missing INTO → ignored
        "\n"
        "ACTION: merge\nINTO: zeta\nFROM: missing2\n"  # no SUMMARY, no sources
        "\n"
        "ACTION: summary\nTOPIC: zeta\nSUMMARY: zeta refreshed\n"
        "\n"
        "ACTION: summary\nTOPIC: alpha\nSUMMARY:\n"    # empty summary → no-op
        "\n"
        "ACTION: summary\nTOPIC: nothere\nSUMMARY: xx\n"
    ))
    with caplog.at_level(logging.WARNING, "tigerharness.tiger_memory.compaction"):
        report = cp.compact_apply(cfg, store, now=NOW)
    assert report.applied == ["topic_roster"]
    assert report.forced_trims == [] and report.still_over == []
    assert "refusing to forget fresh topic" in caplog.text
    assert "refusing to merge away fresh" in caplog.text
    by_slug = {t.slug: t for t in bstore.load(STORE_TOPICS)}
    assert set(by_slug) == {"alpha", "gamma", "eps", "zeta"}
    merged = by_slug["alpha"]
    assert merged.text == "## 2026-06-01\n- a\n\n## 2026-05-01\n- b"
    assert merged.touch_count == 5                      # 2 + beta's 3
    assert merged.last_used == "2026-07-01T00:00:00Z"   # max(alpha, beta)
    assert merged.summary == "merged summary"           # empty-SUMMARY no-op held
    assert by_slug["zeta"].summary == "zeta refreshed"
    assert by_slug["gamma"].touch_count == 1            # fresh merge-source kept


def test_apply_roster_unknown_action_is_malformed(tmp_path):
    cfg, store, bstore, staging, _ = _roster_env(tmp_path)
    bstore.save_atomic(STORE_TOPICS, [_topic("Alpha")])
    _card(staging, "topic_roster", "@@TOPIC_ROSTER@@\nACTION: obliterate\nTOPIC: alpha\n")
    report = cp.compact_apply(cfg, store, now=NOW)
    [bad] = report.malformed
    assert "bad roster directive" in bad["error"]
    assert [t.slug for t in bstore.load(STORE_TOPICS)] == ["alpha"]


def test_apply_roster_post_trim_forgets_oldest_until_under_max(tmp_path):
    old1 = _topic("Trim One", last="2026-03-01T00:00:00Z")
    old2 = _topic("Trim Two", last="2026-04-01T00:00:00Z")
    keep_len = len(indexes.render_topic_index([old2]))
    full_len = len(indexes.render_topic_index([old1, old2]))
    cfg, store, bstore, staging, _ = _roster_env(tmp_path, memory={
        "topics": {"index_max_length": keep_len, "index_overflow_limit": full_len},
    })
    bstore.save_atomic(STORE_TOPICS, [old1, old2])
    _card(staging, "topic_roster", "@@TOPIC_ROSTER@@\nNONE\n")
    report = cp.compact_apply(cfg, store, now=NOW)
    assert report.forced_trims == [STORE_TOPICS]
    assert report.still_over == []
    # Oldest-touched went first; the trim stopped as soon as it was under max.
    assert [t.slug for t in bstore.load(STORE_TOPICS)] == ["trim-two"]


def test_apply_roster_post_trim_never_drops_fresh(tmp_path):
    fresh = _topic("Fresh Keep", last=FRESH, summary="S" * 100)
    old = _topic("Old Drop", last=NOT_FRESH)
    cfg, store, bstore, staging, _ = _roster_env(tmp_path, memory={
        "topics": {"index_max_length": 10, "index_overflow_limit": 20},
    })
    bstore.save_atomic(STORE_TOPICS, [fresh, old])
    _card(staging, "topic_roster", "@@TOPIC_ROSTER@@\nNONE\n")
    report = cp.compact_apply(cfg, store, now=NOW)
    assert report.forced_trims == [STORE_TOPICS]
    assert report.still_over == [STORE_TOPICS]
    assert [t.slug for t in bstore.load(STORE_TOPICS)] == ["fresh-keep"]


# ----- compact_apply: topic_detail --------------------------------------------------


def _topic_detail_env(tmp_path, slug: str, memory=None):
    cfg, store, bstore = make_env(tmp_path, memory)
    staging = store.root / cp.STAGING_DIR_NAME
    staging.mkdir(parents=True)
    t = _target(staging, cp.KIND_TOPIC_DETAIL, f"topic_detail.{slug}", slug=slug)
    _stage(store, [t])
    return cfg, store, bstore, staging, t


def test_apply_topic_detail_replaces_body(tmp_path):
    cfg, store, bstore, staging, _ = _topic_detail_env(tmp_path, "alpha")
    bstore.save_atomic(STORE_TOPICS, [_topic("Alpha", text="## 2026-06-01\n- old")])
    _card(staging, "topic_detail.alpha", "@@TOPIC_DETAIL@@\n## 2026-07-23\n- new fact\n")
    report = cp.compact_apply(cfg, store, now=NOW)
    assert report.applied == ["topic_detail.alpha"]
    assert report.forced_trims == [] and report.still_over == []
    [t] = bstore.load(STORE_TOPICS)
    assert t.text == "## 2026-07-23\n- new fact"


def test_apply_topic_detail_vanished_topic_is_noop(tmp_path):
    cfg, store, bstore, staging, t = _topic_detail_env(tmp_path, "ghost")
    bstore.save_atomic(STORE_TOPICS, [_topic("Alpha", text="## 2026-06-01\n- keep")])
    _card(staging, "topic_detail.ghost", "@@TOPIC_DETAIL@@\n## 2026-07-23\n- x\n")
    report = cp.compact_apply(cfg, store, now=NOW)
    # Vanished between plan and apply (merged/forgotten): applied, no change.
    assert report.applied == ["topic_detail.ghost"]
    [alpha] = bstore.load(STORE_TOPICS)
    assert alpha.text == "## 2026-06-01\n- keep"
    assert not Path(t["card_path"]).exists()


def test_apply_topic_detail_empty_body_is_malformed(tmp_path):
    cfg, store, bstore, staging, _ = _topic_detail_env(tmp_path, "alpha")
    bstore.save_atomic(STORE_TOPICS, [_topic("Alpha")])
    _card(staging, "topic_detail.alpha", "@@TOPIC_DETAIL@@\n\n")
    report = cp.compact_apply(cfg, store, now=NOW)
    [bad] = report.malformed
    assert "empty topic detail body" in bad["error"]


def test_apply_topic_detail_trims_oldest_sections(tmp_path):
    s1 = "## 2026-05-01\n- " + "a" * 80 + "\n\n"
    s2 = "## 2026-06-01\n- " + "b" * 80 + "\n\n"
    s3 = "## 2026-07-01\n- " + "c" * 80
    clone = _topic("Alpha", last=FRESH, text=s2 + s3)
    max_len = len(indexes.render_topic_detail(clone))
    cfg, store, bstore, staging, _ = _topic_detail_env(tmp_path, "alpha", memory={
        "topics": {"detail_max_length": max_len, "detail_overflow_limit": max_len + 100},
    })
    bstore.save_atomic(STORE_TOPICS, [_topic("Alpha", last=FRESH)])
    _card(staging, "topic_detail.alpha", "@@TOPIC_DETAIL@@\n" + s1 + s2 + s3)
    report = cp.compact_apply(cfg, store, now=NOW)
    assert report.forced_trims == ["topic_detail.alpha"]
    assert report.still_over == []
    [t] = bstore.load(STORE_TOPICS)
    # Oldest dated section dropped; trim stopped as soon as the detail fit.
    assert t.text == s2 + s3


def test_apply_topic_detail_single_oversized_section_hard_trims(tmp_path):
    """Convergence must not depend on the card's formatting discipline: a
    body with no droppable dated sections still gets line-dropped and, at
    the very end, hard-truncated (newest tail kept) — never accepted
    oversized forever."""
    body = "## 2026-07-01\n- " + "x" * 300
    cfg, store, bstore, staging, _ = _topic_detail_env(tmp_path, "alpha", memory={
        "topics": {"detail_max_length": 100, "detail_overflow_limit": 150},
    })
    bstore.save_atomic(STORE_TOPICS, [_topic("Alpha")])
    _card(staging, "topic_detail.alpha", "@@TOPIC_DETAIL@@\n" + body)
    report = cp.compact_apply(cfg, store, now=NOW)
    assert report.forced_trims == ["topic_detail.alpha"]
    assert report.still_over == []
    [t] = bstore.load(STORE_TOPICS)
    assert bstore.detail_chars(t) <= 100
    assert t.text.startswith("…")
    assert t.text.endswith("x")          # the newest tail survives


def test_apply_topic_detail_trim_exhausts_sections_then_lines(tmp_path):
    s1 = "## 2026-05-01\n- " + "a" * 200 + "\n\n"
    s2 = "## 2026-07-01\n- " + "b" * 200
    cfg, store, bstore, staging, _ = _topic_detail_env(tmp_path, "alpha", memory={
        "topics": {"detail_max_length": 100, "detail_overflow_limit": 150},
    })
    bstore.save_atomic(STORE_TOPICS, [_topic("Alpha")])
    _card(staging, "topic_detail.alpha", "@@TOPIC_DETAIL@@\n" + s1 + s2)
    report = cp.compact_apply(cfg, store, now=NOW)
    assert report.still_over == []
    [t] = bstore.load(STORE_TOPICS)
    assert bstore.detail_chars(t) <= 100
    assert "a" not in t.text             # oldest section dropped first
    assert t.text.endswith("b")          # newest content survives the trim


def test_apply_topic_detail_bullets_only_body_converges(tmp_path):
    """A headings-free card (bullets only) used to wedge over-max forever —
    the line-drop fallback now converges it."""
    body = "\n".join(f"- fact {i} " + "z" * 40 for i in range(10))
    cfg, store, bstore, staging, _ = _topic_detail_env(tmp_path, "alpha", memory={
        "topics": {"detail_max_length": 150, "detail_overflow_limit": 200},
    })
    bstore.save_atomic(STORE_TOPICS, [_topic("Alpha")])
    _card(staging, "topic_detail.alpha", "@@TOPIC_DETAIL@@\n" + body)
    report = cp.compact_apply(cfg, store, now=NOW)
    assert report.still_over == []
    [t] = bstore.load(STORE_TOPICS)
    assert bstore.detail_chars(t) <= 150
    assert "fact 9" in t.text            # latest bullets survive


def test_apply_topic_detail_none_card_is_malformed(tmp_path):
    """`NONE` is a valid sibling-card verdict but would erase a topic's whole
    history here — rejected as malformed, store untouched."""
    cfg, store, bstore, staging, _ = _topic_detail_env(tmp_path, "alpha", memory={
        "topics": {"detail_max_length": 100, "detail_overflow_limit": 150},
    })
    original = _topic("Alpha")
    bstore.save_atomic(STORE_TOPICS, [original])
    _card(staging, "topic_detail.alpha", "@@TOPIC_DETAIL@@\nNONE\n")
    report = cp.compact_apply(cfg, store, now=NOW)
    assert [m["key"] for m in report.malformed] == ["topic_detail.alpha"]
    [t] = bstore.load(STORE_TOPICS)
    assert t.text == original.text


# ----- compact_apply: skill_detail ---------------------------------------------------


def _skill_detail_env(tmp_path, entry_id: str, memory=None):
    cfg, store, bstore = make_env(tmp_path, memory)
    staging = store.root / cp.STAGING_DIR_NAME
    staging.mkdir(parents=True)
    key = f"skill_detail.{entry_id}"
    t = _target(staging, cp.KIND_SKILL_DETAIL, key, entry_id=entry_id)
    _stage(store, [t])
    return cfg, store, bstore, staging, t


def test_apply_skill_detail_replaces_fields(tmp_path):
    s = _skill("Old Name", trigger="ot", proc="op", usage=4)
    cfg, store, bstore, staging, _ = _skill_detail_env(tmp_path, s.id)
    bstore.save_atomic(STORE_SKILLS, [s])
    _card(staging, f"skill_detail.{s.id}",
          "@@SKILLS@@\nNAME: New Name\nTRIGGER: nt\nPROCEDURE: np\n")
    report = cp.compact_apply(cfg, store, now=NOW)
    assert report.applied == [f"skill_detail.{s.id}"]
    assert report.forced_trims == [] and report.still_over == []
    [e] = bstore.load(STORE_SKILLS)
    assert (e.name, e.trigger, e.procedure, e.text) == ("New Name", "nt", "np", "np")
    assert e.usage_count == 4  # untouched by a detail rewrite


def test_apply_skill_detail_vanished_skill_is_noop(tmp_path):
    cfg, store, bstore, staging, _ = _skill_detail_env(tmp_path, "missing12345")
    keep = _skill("Keep", proc="kp")
    bstore.save_atomic(STORE_SKILLS, [keep])
    _card(staging, "skill_detail.missing12345",
          "@@SKILLS@@\nNAME: N\nTRIGGER: T\nPROCEDURE: P\n")
    report = cp.compact_apply(cfg, store, now=NOW)
    assert report.applied == ["skill_detail.missing12345"]
    [e] = bstore.load(STORE_SKILLS)
    assert e.name == "Keep" and e.procedure == "kp"


def test_apply_skill_detail_requires_exactly_one_block(tmp_path):
    s = _skill("S")
    cfg, store, bstore, staging, _ = _skill_detail_env(tmp_path, s.id)
    bstore.save_atomic(STORE_SKILLS, [s])
    _card(staging, f"skill_detail.{s.id}", (
        "@@SKILLS@@\n"
        "NAME: A\nTRIGGER: t\nPROCEDURE: p\n"
        "\n"
        "NAME: B\nTRIGGER: t\nPROCEDURE: p\n"
    ))
    report = cp.compact_apply(cfg, store, now=NOW)
    [bad] = report.malformed
    assert "exactly one block; got 2" in bad["error"]
    # And zero blocks (NONE) is just as malformed for a detail card.
    _card(staging, f"skill_detail.{s.id}", "@@SKILLS@@\nNONE\n")
    report = cp.compact_apply(cfg, store, now=NOW)
    [bad] = report.malformed
    assert "exactly one block; got 0" in bad["error"]


def test_apply_skill_detail_bad_block_is_malformed(tmp_path):
    s = _skill("S")
    cfg, store, bstore, staging, _ = _skill_detail_env(tmp_path, s.id)
    bstore.save_atomic(STORE_SKILLS, [s])
    _card(staging, f"skill_detail.{s.id}", "@@SKILLS@@\nNAME: A\nTRIGGER: t\n")
    report = cp.compact_apply(cfg, store, now=NOW)
    [bad] = report.malformed
    assert "bad skill block" in bad["error"]


def test_apply_skill_detail_hard_truncates_with_ellipsis(tmp_path):
    s = _skill("Long Proc")
    cfg, store, bstore, staging, _ = _skill_detail_env(tmp_path, s.id, memory={
        "skills": {"detail_max_length": 200, "detail_overflow_limit": 300},
    })
    bstore.save_atomic(STORE_SKILLS, [s])
    _card(staging, f"skill_detail.{s.id}",
          f"@@SKILLS@@\nNAME: Long Proc\nTRIGGER: t\nPROCEDURE: {'z' * 400}\n")
    report = cp.compact_apply(cfg, store, now=NOW)
    assert report.forced_trims == [f"skill_detail.{s.id}"]
    assert report.still_over == []
    [e] = bstore.load(STORE_SKILLS)
    assert e.procedure.endswith("…")
    assert set(e.procedure[:-1]) == {"z"}
    assert bstore.detail_chars(e) <= 200
    assert e.text == e.procedure


def test_apply_skill_detail_still_over_when_header_alone_exceeds_max(tmp_path):
    s = _skill("Tiny Bound")
    cfg, store, bstore, staging, _ = _skill_detail_env(tmp_path, s.id, memory={
        "skills": {"detail_max_length": 3, "detail_overflow_limit": 10},
    })
    bstore.save_atomic(STORE_SKILLS, [s])
    _card(staging, f"skill_detail.{s.id}",
          f"@@SKILLS@@\nNAME: Tiny Bound\nTRIGGER: t\nPROCEDURE: {'z' * 50}\n")
    report = cp.compact_apply(cfg, store, now=NOW)
    assert report.forced_trims == [f"skill_detail.{s.id}"]
    assert report.still_over == [f"skill_detail.{s.id}"]
    [e] = bstore.load(STORE_SKILLS)
    assert e.procedure == "z…"  # keep floor of one character + ellipsis


# ----- plan → apply round-trip ----------------------------------------------------


def test_plan_then_apply_roundtrip_must_remember(tmp_path):
    cfg, store, bstore = make_env(tmp_path, memory={
        "must_remember": {"max_length": 60, "overflow_limit": 80},
    })
    op = _memo("KEEP OP", kind="operator_explicit")
    bstore.save_atomic(STORE_MUST_REMEMBER, [op, _memo("j" * 40), _memo("k" * 40)])
    manifest = cp.compact_plan(cfg, store, now=NOW)
    [target] = manifest["targets"]
    Path(target["card_path"]).write_text(
        "@@MUST_REMEMBER@@\nKIND: decision\nMEMO: the compacted memo\n",
        encoding="utf-8",
    )
    report = cp.compact_apply(cfg, store, now=NOW)
    assert report.applied == ["must_remember"]
    assert report.forced_trims == [] and report.still_over == []
    assert [e.text for e in bstore.load(STORE_MUST_REMEMBER)] == [
        "KEEP OP", "the compacted memo",
    ]
    assert not Path(target["prompt_path"]).exists()
    assert not Path(target["card_path"]).exists()
    # Hardening pass: a fully-clean apply consumes the manifest so a
    # blind re-apply gets the loud exit-2, not a silent "clean" no-op.
    assert not (store.root / cp.STAGING_DIR_NAME / "manifest.json").exists()


# ----- exact-duplicate dedup pre-pass (plan) -----------------------------------


def test_dedup_must_remember_merges_exact_normalized_dupes():
    a = _memo("Never  bill the API rail.", kind="decision", repeats=2,
              last="2026-01-02T00:00:00Z")
    b = _memo("never bill the API   rail.", kind="decision", repeats=3,
              last="2026-03-02T00:00:00Z")
    c = _memo("Never bill the API rail.", kind="incident")  # different kind
    survivors, dropped = cp._dedup_must_remember([a, b, c])
    assert survivors == [a, c] and dropped == 1
    assert a.repeat_count == 5  # reinforcement counts sum on merge
    assert a.last_used == "2026-03-02T00:00:00Z"


def test_dedup_skills_merges_exact_normalized_dupes():
    a = _skill("Bound a store", "growth", "compact it", usage=2,
               last="2026-01-02T00:00:00Z")
    b = _skill("bound A  store", "GROWTH", "Compact   it", usage=3,
               last="2026-04-02T00:00:00Z")
    c = _skill("Bound a store", "growth", "different procedure")
    survivors, dropped = cp._dedup_skills([a, b, c])
    assert survivors == [a, c] and dropped == 1
    assert a.usage_count == 5
    assert a.last_used == "2026-04-02T00:00:00Z"


def test_plan_dedup_persists_and_reports_counts(tmp_path):
    cfg, store, bstore = make_env(tmp_path, memory={
        "skills": {"index_max_length": 4000, "index_overflow_limit": 6000},
        "must_remember": {"max_length": 4000, "overflow_limit": 6000},
    })
    bstore.save_atomic(STORE_SKILLS, [
        _skill("Dup", "t", "p"), _skill("dup", "T", "P"), _skill("Other", "t", "p"),
    ])
    bstore.save_atomic(STORE_MUST_REMEMBER, [
        _memo("same memo"), _memo("same  MEMO"), _memo("unique"),
    ])
    manifest = cp.compact_plan(cfg, store, now=NOW)
    assert manifest["deduped_skills"] == 1
    assert manifest["deduped_must_remember"] == 1
    assert manifest["targets"] == []  # generous bounds: dedup alone suffices
    assert len(bstore.load(STORE_SKILLS)) == 2
    assert len(bstore.load(STORE_MUST_REMEMBER)) == 2


def test_plan_dedup_can_bring_store_back_under_overflow(tmp_path):
    cfg, store, bstore = make_env(tmp_path, memory={
        "must_remember": {"max_length": 60, "overflow_limit": 90},
    })
    # Two copies push past overflow (2 * 50 = 100 >= 90); one copy fits.
    memo = "x" * 50
    bstore.save_atomic(STORE_MUST_REMEMBER, [_memo(memo), _memo(memo)])
    manifest = cp.compact_plan(cfg, store, now=NOW)
    assert manifest["deduped_must_remember"] == 1
    assert not any(
        t["kind"] == cp.KIND_MUST_REMEMBER for t in manifest["targets"]
    )


def test_plan_protected_memos_rendered_with_ids(tmp_path):
    cfg, store, bstore = make_env(tmp_path, memory={
        "must_remember": {"max_length": 20, "overflow_limit": 30},
    })
    op = _memo("keep me forever, operator said so", kind="operator_explicit")
    bstore.save_atomic(STORE_MUST_REMEMBER, [op])
    manifest = cp.compact_plan(cfg, store, now=NOW)
    (target,) = [
        t for t in manifest["targets"] if t["kind"] == cp.KIND_MUST_REMEMBER
    ]
    prompt = Path(target["prompt_path"]).read_text(encoding="utf-8")
    assert f"ID: {op.id}" in prompt
    assert "Relevance check" in prompt


# ----- STALE relevance-downgrade (apply) ---------------------------------------


def test_apply_stale_downgrades_protected_to_decision(tmp_path):
    cfg, store, bstore = make_env(tmp_path, memory={
        "must_remember": {"max_length": 2000, "overflow_limit": 3000},
    })
    # Hardening pass: only a directive untouched past forget_days may be
    # relevance-downgraded — the STALE'd entry must actually BE stale.
    op_stale = _memo("stale directive", kind="operator_explicit",
                     last="2026-01-02T00:00:00Z")
    op_live = _memo("live directive", kind="operator_explicit")
    bstore.save_atomic(STORE_MUST_REMEMBER, [op_stale, op_live])
    staging = _stage(store, [
        _target(store.root / cp.STAGING_DIR_NAME, cp.KIND_MUST_REMEMBER,
                "must_remember"),
    ])
    _card(staging, "must_remember", (
        "@@MUST_REMEMBER@@\n"
        f"STALE: {op_stale.id}\n"
        "\n"
        "STALE: not-a-real-id\n"       # unknown id: ignored
        "\n"
        "KIND: preference\n"
        "MEMO: tidy survivor\n"
    ))
    report = cp.compact_apply(cfg, store, now=NOW)
    assert report.applied == ["must_remember"]
    entries = {
        e.text: e for e in bstore.load(STORE_MUST_REMEMBER)
    }
    assert entries["stale directive"].kind == "decision"      # downgraded
    assert entries["stale directive"].id == op_stale.id        # same entry
    assert entries["live directive"].kind == "operator_explicit"
    assert entries["tidy survivor"].kind == "preference"


def test_apply_stale_downgraded_entry_is_trimmable(tmp_path):
    cfg, store, bstore = make_env(tmp_path, memory={
        # Tight bound: only the live directive fits.
        "must_remember": {"max_length": 20, "overflow_limit": 30},
    })
    op_stale = _memo("this old directive is very long indeed",
                     kind="operator_explicit", last="2026-01-02T00:00:00Z")
    op_live = _memo("keep me", kind="operator_explicit")
    bstore.save_atomic(STORE_MUST_REMEMBER, [op_stale, op_live])
    staging = _stage(store, [
        _target(store.root / cp.STAGING_DIR_NAME, cp.KIND_MUST_REMEMBER,
                "must_remember"),
    ])
    _card(staging, "must_remember", (
        "@@MUST_REMEMBER@@\n"
        f"STALE: {op_stale.id}\n"
    ))
    report = cp.compact_apply(cfg, store, now=NOW)
    assert report.applied == ["must_remember"]
    assert cp.STORE_MUST_REMEMBER not in report.still_over
    texts = [e.text for e in bstore.load(STORE_MUST_REMEMBER)]
    assert texts == ["keep me"]        # downgraded entry trimmed to converge
    assert report.forced_trims == [cp.STORE_MUST_REMEMBER]


# ----- must_remember freshness: AGE annotation + stale-first drop order --------


def test_render_mr_blocks_marks_forget_eligible():
    fresh = _memo("fresh memo", last=NOW)
    stale = _memo("stale memo", last="2026-05-01T00:00:00Z")  # ~83 days
    text = cp._render_mr_blocks([fresh, stale], now=NOW, forget_days=30)
    assert "AGE: untouched" not in text.split("stale memo")[0]
    assert "[forget-eligible]" in text
    # Without the window args, no annotation at all.
    assert "forget-eligible" not in cp._render_mr_blocks([stale])


def test_plan_mr_prompt_carries_forget_eligible_annotation(tmp_path):
    cfg, store, bstore = make_env(tmp_path, memory={
        "must_remember": {"max_length": 20, "overflow_limit": 30,
                          "forget_days": 30},
    })
    stale = _memo("an old untouched memo, long enough to overflow the bound",
                  last="2026-01-01T00:00:00Z")
    bstore.save_atomic(STORE_MUST_REMEMBER, [stale])
    manifest = cp.compact_plan(cfg, store, now=NOW)
    (target,) = [
        t for t in manifest["targets"] if t["kind"] == cp.KIND_MUST_REMEMBER
    ]
    prompt = Path(target["prompt_path"]).read_text(encoding="utf-8")
    assert "[forget-eligible]" in prompt
    assert "untouched for over\n{forget_days}" not in prompt  # placeholder filled


def test_mr_drop_order_stale_normal_then_fresh_then_stale_protected():
    stale_old = _memo("stale old", last="2026-01-01T00:00:00Z")
    stale_new = _memo("stale newer", last="2026-03-01T00:00:00Z")
    fresh_low = _memo("fresh low", repeats=1, last=NOW)
    fresh_high = _memo("fresh high", repeats=9, last=NOW)
    op_stale = _memo("op stale", kind="operator_explicit",
                     last="2026-02-01T00:00:00Z")
    op_fresh = _memo("op fresh", kind="operator_explicit", last=NOW)
    order = cp._mr_drop_order(
        [op_fresh, fresh_high, stale_new, op_stale, stale_old, fresh_low],
        NOW, 30,
    )
    assert [e.text for e in order] == [
        "stale old", "stale newer",      # stale normal, oldest first
        "fresh low", "fresh high",       # fresh normal, keep-rank order
        "op stale",                      # stale protected: last resort
    ]
    assert op_fresh not in order          # fresh protected never droppable


def test_apply_mr_trim_drops_stale_downgraded_before_fresh_memo(tmp_path):
    """The stale-first drop order, end to end: a STALE-downgraded directive
    keeps its old last_used, so when the bound forces a trim it drops
    BEFORE the fresh card-minted memo — even though the downgraded entry's
    repeat_count (5) is higher than the fresh memo's (1)."""
    cfg, store, bstore = make_env(tmp_path, memory={
        "must_remember": {"max_length": 30, "overflow_limit": 40,
                          "forget_days": 30},
    })
    op_stale = _memo("very old big directive text here",
                     kind="operator_explicit", repeats=5,
                     last="2026-01-01T00:00:00Z")
    bstore.save_atomic(STORE_MUST_REMEMBER, [op_stale])
    staging = _stage(store, [
        _target(store.root / cp.STAGING_DIR_NAME, cp.KIND_MUST_REMEMBER,
                "must_remember"),
    ])
    _card(staging, "must_remember", (
        "@@MUST_REMEMBER@@\n"
        f"STALE: {op_stale.id}\n"
        "\n"
        "KIND: preference\nMEMO: fresh survivor\n"
    ))
    report = cp.compact_apply(cfg, store, now=NOW)
    assert report.applied == ["must_remember"]
    texts = [e.text for e in bstore.load(STORE_MUST_REMEMBER)]
    assert texts == ["fresh survivor"]
    assert report.forced_trims == [cp.STORE_MUST_REMEMBER]


def test_apply_mr_stale_protected_dropped_as_last_resort(tmp_path, caplog):
    cfg, store, bstore = make_env(tmp_path, memory={
        "must_remember": {"max_length": 20, "overflow_limit": 30,
                          "forget_days": 30},
    })
    op_stale = _memo("very old operator directive nobody touched",
                     kind="operator_explicit", last="2026-01-01T00:00:00Z")
    op_fresh = _memo("live rule", kind="operator_explicit", last=NOW)
    bstore.save_atomic(STORE_MUST_REMEMBER, [op_stale, op_fresh])
    staging = _stage(store, [
        _target(store.root / cp.STAGING_DIR_NAME, cp.KIND_MUST_REMEMBER,
                "must_remember"),
    ])
    _card(staging, "must_remember", "@@MUST_REMEMBER@@\nNONE\n")
    with caplog.at_level(logging.WARNING):
        report = cp.compact_apply(cfg, store, now=NOW)
    assert report.applied == ["must_remember"]
    texts = [e.text for e in bstore.load(STORE_MUST_REMEMBER)]
    assert texts == ["live rule"]         # stale protected dropped, fresh kept
    assert cp.STORE_MUST_REMEMBER not in report.still_over
    assert "last resort" in caplog.text


def test_apply_stale_backticked_id_still_downgrades(tmp_path):
    """The prompt displays ids backticked; a card echoing the displayed
    form must still resolve — a silently-missed STALE would leave the
    directive protected and hand the decision to the deterministic trim."""
    cfg, store, bstore = make_env(tmp_path, memory={
        "must_remember": {"max_length": 2000, "overflow_limit": 3000},
    })
    op_stale = _memo("stale directive", kind="operator_explicit",
                     last="2026-01-02T00:00:00Z")
    bstore.save_atomic(STORE_MUST_REMEMBER, [op_stale])
    staging = _stage(store, [
        _target(store.root / cp.STAGING_DIR_NAME, cp.KIND_MUST_REMEMBER,
                "must_remember"),
    ])
    _card(staging, "must_remember", (
        "@@MUST_REMEMBER@@\n"
        f"STALE: `{op_stale.id}`\n"
    ))
    report = cp.compact_apply(cfg, store, now=NOW)
    assert report.applied == ["must_remember"]
    [e] = bstore.load(STORE_MUST_REMEMBER)
    assert e.kind == "decision" and e.id == op_stale.id


def test_apply_roster_backticked_refs_resolve(tmp_path):
    """Backtick-echoed TOPIC/INTO/FROM addresses still resolve — forget,
    merge, and summary all land instead of degrading to no-ops."""
    cfg, store, bstore, staging, _ = _roster_env(tmp_path)
    alpha = _topic("Alpha", last="2026-07-01T00:00:00Z",
                   text="## 2026-06-01\n- a", touch=2)
    beta = _topic("Beta", last=NOT_FRESH, text="## 2026-05-01\n- b", touch=3)
    delta = _topic("Delta", last=NOT_FRESH)
    zeta = _topic("Zeta", last=NOT_FRESH)
    bstore.save_atomic(STORE_TOPICS, [alpha, beta, delta, zeta])
    _card(staging, "topic_roster", (
        "@@TOPIC_ROSTER@@\n"
        "ACTION: forget\nTOPIC: `delta`\n"
        "\n"
        "ACTION: merge\nINTO: `alpha`\nFROM: `beta`\n"
        "\n"
        "ACTION: summary\nTOPIC: `zeta`\nSUMMARY: zeta refreshed\n"
    ))
    report = cp.compact_apply(cfg, store, now=NOW)
    assert report.applied == ["topic_roster"]
    by_slug = {t.slug: t for t in bstore.load(STORE_TOPICS)}
    assert set(by_slug) == {"alpha", "zeta"}       # delta forgotten, beta merged
    assert "- b" in by_slug["alpha"].text
    assert by_slug["alpha"].touch_count == 5       # 2 + beta's 3
    assert by_slug["zeta"].summary == "zeta refreshed"


def test_apply_mr_fresh_protected_never_dropped_still_over(tmp_path):
    cfg, store, bstore = make_env(tmp_path, memory={
        "must_remember": {"max_length": 10, "overflow_limit": 15,
                          "forget_days": 30},
    })
    op1 = _memo("fresh directive one", kind="operator_explicit", last=NOW)
    op2 = _memo("fresh directive two", kind="operator_explicit", last=NOW)
    bstore.save_atomic(STORE_MUST_REMEMBER, [op1, op2])
    staging = _stage(store, [
        _target(store.root / cp.STAGING_DIR_NAME, cp.KIND_MUST_REMEMBER,
                "must_remember"),
    ])
    _card(staging, "must_remember", "@@MUST_REMEMBER@@\nNONE\n")
    report = cp.compact_apply(cfg, store, now=NOW)
    assert cp.STORE_MUST_REMEMBER in report.still_over
    assert len(bstore.load(STORE_MUST_REMEMBER)) == 2   # both survive


# ----- plan-time snapshot: post-plan writes survive the replacement card -------


def test_apply_mr_post_plan_pin_survives_replacement(tmp_path):
    """The card replaces only what its prompt SAW (the plan-time snapshot):
    a pin landed between plan and apply is NOT in snapshot_ids and must
    survive the wholesale replacement."""
    cfg, store, bstore = make_env(tmp_path)
    old = _memo("old junk seen by the plan")
    pin = _memo("pinned after the plan", kind="decision")
    bstore.save_atomic(STORE_MUST_REMEMBER, [old, pin])
    staging = store.root / cp.STAGING_DIR_NAME
    staging.mkdir(parents=True)
    t = _target(staging, cp.KIND_MUST_REMEMBER, "must_remember",
                snapshot_ids=[old.id])
    _stage(store, [t])
    _card(staging, "must_remember",
          "@@MUST_REMEMBER@@\nKIND: preference\nMEMO: compacted memo\n")
    report = cp.compact_apply(cfg, store, now=NOW)
    assert report.applied == ["must_remember"]
    entries = bstore.load(STORE_MUST_REMEMBER)
    assert [e.text for e in entries] == ["compacted memo", "pinned after the plan"]
    survivor = entries[1]
    assert survivor.id == pin.id                    # untouched, not re-minted


def test_apply_skills_post_plan_write_survives_and_kept_id_carries(tmp_path):
    """Same snapshot rule for skills — plus id carry-over: a kept skill
    (name match within the snapshot) keeps its OLD id, because detail
    filenames and future detail targets are id-addressed."""
    cfg, store, bstore = make_env(tmp_path)
    kept = _skill("Deploy Fix", usage=5,
                  created="2026-01-01T00:00:00Z", last="2026-05-01T00:00:00Z")
    post = _skill("Post Plan Skill", trigger="pt", proc="pp")
    bstore.save_atomic(STORE_SKILLS, [kept, post])
    staging = store.root / cp.STAGING_DIR_NAME
    staging.mkdir(parents=True)
    t = _target(staging, cp.KIND_SKILLS, "skills", snapshot_ids=[kept.id])
    _stage(store, [t])
    _card(staging, "skills",
          "@@SKILLS@@\nNAME: Deploy Fix\nTRIGGER: nt\nPROCEDURE: np\n")
    report = cp.compact_apply(cfg, store, now=NOW)
    assert report.applied == ["skills"]
    by_name = {e.name: e for e in bstore.load(STORE_SKILLS)}
    assert set(by_name) == {"Deploy Fix", "Post Plan Skill"}
    assert by_name["Deploy Fix"].id == kept.id      # OLD id kept (id-addressed)
    assert by_name["Deploy Fix"].usage_count == 5
    assert by_name["Deploy Fix"].procedure == "np"
    assert by_name["Post Plan Skill"].id == post.id  # post-plan write survives


# ----- must_remember freshness carry-over (kept memos keep the TOUCH clock) ----


def test_apply_mr_kept_memo_inherits_identity_and_freshness(tmp_path):
    """A card memo matching a snapshot entry (kind + normalized text)
    inherits id / created_at / last_used / repeat_count — compaction must
    not reset the TOUCH clock of a surviving memo."""
    cfg, store, bstore = make_env(tmp_path)
    kept = _memo("Targeted git add, never -A", repeats=7,
                 last="2026-05-01T00:00:00Z")
    dropped = _memo("obsolete memo", kind="decision")
    bstore.save_atomic(STORE_MUST_REMEMBER, [kept, dropped])
    staging = store.root / cp.STAGING_DIR_NAME
    staging.mkdir(parents=True)
    t = _target(staging, cp.KIND_MUST_REMEMBER, "must_remember",
                snapshot_ids=[kept.id, dropped.id])
    _stage(store, [t])
    # Whitespace/case differences still match the normalized key.
    _card(staging, "must_remember",
          "@@MUST_REMEMBER@@\nKIND: preference\nMEMO: targeted GIT add,  never -A\n")
    report = cp.compact_apply(cfg, store, now=NOW)
    assert report.applied == ["must_remember"]
    [e] = bstore.load(STORE_MUST_REMEMBER)
    assert e.id == kept.id
    assert e.created_at == "2026-01-01T00:00:00Z"   # _memo helper's created_at
    assert e.last_used == "2026-05-01T00:00:00Z"    # TOUCH clock NOT reset
    assert e.repeat_count == 7
    assert e.text == "targeted GIT add,  never -A"  # card's wording wins


def test_apply_mr_duplicate_card_memos_inherit_only_once(tmp_path):
    """The by_key map pops on inherit: two identical card memos cannot BOTH
    claim the snapshot entry's identity — the second mints fresh."""
    cfg, store, bstore = make_env(tmp_path)
    kept = _memo("same memo", repeats=4, last="2026-05-01T00:00:00Z")
    bstore.save_atomic(STORE_MUST_REMEMBER, [kept])
    staging = store.root / cp.STAGING_DIR_NAME
    staging.mkdir(parents=True)
    t = _target(staging, cp.KIND_MUST_REMEMBER, "must_remember",
                snapshot_ids=[kept.id])
    _stage(store, [t])
    _card(staging, "must_remember", (
        "@@MUST_REMEMBER@@\n"
        "KIND: preference\nMEMO: same memo\n"
        "\n"
        "KIND: preference\nMEMO: same memo\n"
    ))
    report = cp.compact_apply(cfg, store, now=NOW)
    assert report.applied == ["must_remember"]
    first, second = bstore.load(STORE_MUST_REMEMBER)
    assert first.id == kept.id and first.repeat_count == 4      # inherited once
    assert second.id != kept.id and second.repeat_count == 1    # minted fresh
    assert second.created_at == NOW and second.last_used == NOW


# ----- topic_detail convergence: header overhead alone can defeat the trim -----


def test_apply_topic_detail_still_over_when_header_alone_exceeds_max(tmp_path):
    """When the rendered header (name + summary line) alone exceeds the
    detail max, even the hard truncate cannot converge — the surface is
    reported still_over rather than silently accepted."""
    cfg, store, bstore, staging, _ = _topic_detail_env(tmp_path, "alpha", memory={
        "topics": {"detail_max_length": 100, "detail_overflow_limit": 150},
    })
    bstore.save_atomic(STORE_TOPICS, [_topic("Alpha", summary="S" * 200)])
    _card(staging, "topic_detail.alpha", "@@TOPIC_DETAIL@@\n- tiny body\n")
    report = cp.compact_apply(cfg, store, now=NOW)
    assert report.forced_trims == ["topic_detail.alpha"]
    assert report.still_over == ["topic_detail.alpha"]
    [t] = bstore.load(STORE_TOPICS)
    assert t.text.startswith("…")            # truncate ran; keep floor of 1 char


# ----- plan pre-passes back off when a live session holds the store lock -------


def _live_lock(store, store_name: str) -> Path:
    """Simulate a live holder (our own PID) on one store's lock."""
    lock = store.paths.journal / f".{store_name}.lock"
    lock.write_text(f"{os.getpid()} 0")
    return lock


def test_plan_stale_forget_skipped_when_topics_locked(tmp_path, caplog):
    old1 = _topic("Old One", last=STALE_1)
    old2 = _topic("Old Two", last=STALE_2)
    fresh = _topic("Fresh Topic", last=FRESH)
    full = len(indexes.render_topic_index([old1, old2, fresh]))
    cfg, store, bstore = make_env(tmp_path, memory={"topics": {
        "index_max_length": full - 1, "index_overflow_limit": full,
    }})
    bstore.save_atomic(STORE_TOPICS, [old1, old2, fresh])
    before = (store.paths.journal / "topics.md").read_text()
    _live_lock(store, STORE_TOPICS)
    with caplog.at_level(logging.WARNING, "tigerharness.tiger_memory.compaction"):
        manifest = cp.compact_plan(cfg, store, now=NOW)
    assert "locked by a live session" in caplog.text
    assert manifest["dropped_stale_topics"] == []            # pre-pass skipped
    assert (store.paths.journal / "topics.md").read_text() == before
    # Still over: the roster target stages against the unmutated store.
    assert [t["kind"] for t in manifest["targets"]] == ["topic_roster"]


def test_plan_skills_dedup_skipped_when_skills_locked(tmp_path, caplog):
    cfg, store, bstore = make_env(tmp_path, memory={
        "skills": {"index_max_length": 4000, "index_overflow_limit": 6000},
    })
    bstore.save_atomic(STORE_SKILLS, [
        _skill("Dup", "t", "p"), _skill("dup", "T", "P"),
    ])
    before = (store.paths.journal / "skills.md").read_text()
    _live_lock(store, STORE_SKILLS)
    with caplog.at_level(logging.WARNING, "tigerharness.tiger_memory.compaction"):
        manifest = cp.compact_plan(cfg, store, now=NOW)
    assert "locked by a live session" in caplog.text
    assert manifest["deduped_skills"] == 0                   # reverted count
    assert manifest["targets"] == []
    assert (store.paths.journal / "skills.md").read_text() == before
    assert len(bstore.load(STORE_SKILLS)) == 2               # nothing merged


def test_plan_mr_dedup_skipped_when_must_remember_locked(tmp_path, caplog):
    cfg, store, bstore = make_env(tmp_path, memory={
        "must_remember": {"max_length": 4000, "overflow_limit": 6000},
    })
    bstore.save_atomic(STORE_MUST_REMEMBER, [
        _memo("same memo"), _memo("same  MEMO"),
    ])
    before = (store.paths.journal / "must_remember.md").read_text()
    _live_lock(store, STORE_MUST_REMEMBER)
    with caplog.at_level(logging.WARNING, "tigerharness.tiger_memory.compaction"):
        manifest = cp.compact_plan(cfg, store, now=NOW)
    assert "locked by a live session" in caplog.text
    assert manifest["deduped_must_remember"] == 0            # reverted count
    assert manifest["targets"] == []
    assert (store.paths.journal / "must_remember.md").read_text() == before
    assert len(bstore.load(STORE_MUST_REMEMBER)) == 2


# ----- deferral: index staged with NO oversized details defers nothing ---------


def test_plan_skills_index_staged_without_oversized_details(tmp_path, caplog):
    cfg, store, bstore = make_env(tmp_path, memory={
        "skills": {
            "index_max_length": 100, "index_overflow_limit": 120,
            "detail_max_length": 4000, "detail_overflow_limit": 6000,
        },
    })
    bstore.save_atomic(STORE_SKILLS, [
        _skill("Skill One", trigger="T" * 80), _skill("Skill Two"),
    ])
    with caplog.at_level(logging.INFO, "tigerharness.tiger_memory.compaction"):
        manifest = cp.compact_plan(cfg, store, now=NOW)
    assert [t["kind"] for t in manifest["targets"]] == ["skills"]
    assert "deferring" not in caplog.text        # no detail was over-bound

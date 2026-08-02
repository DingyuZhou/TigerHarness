"""Tests for the bounded-store substrate (design §4, §5; ADR 0007).

Covers persistence (load/save_atomic + crash-safety) for the three stores
(skills / must_remember / topics), the ADR-0007 measurement API
(index_chars / detail_chars / is_over_overflow hysteresis / max bounds),
the per-store lock, and the forget-guard (the no-safety-net correctness
anchor)."""
from __future__ import annotations

import os
from pathlib import Path
from textwrap import dedent

import pytest

from tigerharness.tiger_memory import indexes
from tigerharness.tiger_memory.bounded_store import (
    BoundedStore,
    ForgetGuardError,
    StoreLockHeld,
    _split_blocks,
)
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.entries import (
    EntryError,
    MustRememberEntry,
    SkillEntry,
    TopicEntry,
)
from tigerharness.tiger_memory.store import Store

NOW = "2026-06-17T00:00:00Z"

# Fixture bounds (deliberately small + distinct per store so every bound
# assertion below is unambiguous about which knob it read).
SKILLS_INDEX_MAX, SKILLS_INDEX_OVERFLOW = 200, 300
SKILLS_DETAIL_MAX, SKILLS_DETAIL_OVERFLOW = 300, 450
MR_MAX, MR_OVERFLOW = 40, 60
TOPICS_INDEX_MAX, TOPICS_INDEX_OVERFLOW = 220, 320
TOPICS_DETAIL_MAX, TOPICS_DETAIL_OVERFLOW = 350, 500


@pytest.fixture
def bounded(tmp_path: Path) -> BoundedStore:
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(
        dedent(
            f"""\
            agent:
              name: Mitsui
              role: r
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
                index_max_length: {SKILLS_INDEX_MAX}
                index_overflow_limit: {SKILLS_INDEX_OVERFLOW}
                detail_max_length: {SKILLS_DETAIL_MAX}
                detail_overflow_limit: {SKILLS_DETAIL_OVERFLOW}
              must_remember:
                max_length: {MR_MAX}
                overflow_limit: {MR_OVERFLOW}
              topics:
                index_max_length: {TOPICS_INDEX_MAX}
                index_overflow_limit: {TOPICS_INDEX_OVERFLOW}
                detail_max_length: {TOPICS_DETAIL_MAX}
                detail_overflow_limit: {TOPICS_DETAIL_OVERFLOW}
                fresh_days: 7
                forget_days: 60
            """
        )
    )
    cfg = load_config(cfg_path)
    store = Store(cfg.store.root)
    store.init_layout()
    return BoundedStore(cfg, store)


def _skill(name: str = "N", text: str = "body text") -> SkillEntry:
    return SkillEntry(
        text=text, created_at=NOW, last_used=NOW, source="extract",
        name=name, trigger=f"when {name}", procedure=f"do {name}",
    )


def _mr(kind: str = "preference", text: str = "memo") -> MustRememberEntry:
    return MustRememberEntry(
        text=text, created_at=NOW, last_used=NOW, source="pin", kind=kind,
    )


def _topic(
    name: str = "Topic Store Revamp",
    summary: str = "the revamp plan",
    text: str = "## 2026-06-17\n- did x",
) -> TopicEntry:
    return TopicEntry(
        text=text, created_at=NOW, last_used=NOW, source="extract",
        name=name, summary=summary,
    )


# -- padding helpers: build entries whose RENDERED surface hits an exact
# -- character target, so bound-edge assertions are byte-precise.


def _skills_at_index_chars(bounded: BoundedStore, target: int) -> list[SkillEntry]:
    s = _skill("Pad")
    base = bounded.index_chars("skills", [s])
    assert base < target, "fixture bound too small for the padding helper"
    s.trigger += "x" * (target - base)
    assert bounded.index_chars("skills", [s]) == target
    return [s]


def _topics_at_index_chars(bounded: BoundedStore, target: int) -> list[TopicEntry]:
    t = _topic("T", summary="s")
    base = bounded.index_chars("topics", [t])
    assert base < target, "fixture bound too small for the padding helper"
    t.summary += "x" * (target - base)
    assert bounded.index_chars("topics", [t]) == target
    return [t]


def _skill_at_detail_chars(bounded: BoundedStore, target: int) -> SkillEntry:
    s = _skill("Pad")
    base = bounded.detail_chars(s)
    assert base < target, "fixture bound too small for the padding helper"
    s.procedure += "x" * (target - base)
    assert bounded.detail_chars(s) == target
    return s


def _topic_at_detail_chars(bounded: BoundedStore, target: int) -> TopicEntry:
    t = _topic("T", summary="s")
    base = bounded.detail_chars(t)
    assert base < target, "fixture bound too small for the padding helper"
    t.text += "x" * (target - base)
    assert bounded.detail_chars(t) == target
    return t


# ----- load / save roundtrip ----------------------------------------------


def test_load_absent_store_is_empty(bounded: BoundedStore) -> None:
    assert bounded.load("skills") == []
    assert bounded.load("topics") == []


def test_save_load_roundtrip_all_stores(bounded: BoundedStore) -> None:
    s = _skill("Alpha")
    bounded.save_atomic("skills", [s])
    got = bounded.load("skills")
    assert len(got) == 1 and got[0].name == "Alpha" and got[0].id == s.id

    m1, m2 = _mr("operator_explicit", "ship friday"), _mr("preference", "tabs")
    bounded.save_atomic("must_remember", [m1, m2])
    gm = bounded.load("must_remember")
    assert [e.kind for e in gm] == ["operator_explicit", "preference"]
    assert [e.id for e in gm] == [m1.id, m2.id]

    t = _topic()
    bounded.save_atomic("topics", [t])
    gt = bounded.load("topics")
    assert len(gt) == 1
    assert gt[0].id == t.id
    assert gt[0].name == "Topic Store Revamp"
    assert gt[0].slug == "topic-store-revamp"  # auto-derived, persisted
    assert gt[0].summary == "the revamp plan"
    assert gt[0].touch_count == 1
    assert gt[0].text == "## 2026-06-17\n- did x"


def test_save_empty_then_load(bounded: BoundedStore) -> None:
    bounded.save_atomic("topics", [_topic()])
    bounded.save_atomic("topics", [])  # clear
    assert bounded.load("topics") == []


def test_save_validates_entries(bounded: BoundedStore) -> None:
    bad = _skill()
    bad.name = "  "  # invalid
    with pytest.raises(EntryError, match="name"):
        bounded.save_atomic("skills", [bad])
    # No partial file written.
    assert not (bounded.store.paths.journal / "skills.md").exists()


def test_save_validates_topic_entries(bounded: BoundedStore) -> None:
    bad = _topic()
    bad.summary = "  "  # invalid
    with pytest.raises(EntryError, match="summary"):
        bounded.save_atomic("topics", [bad])
    assert not (bounded.store.paths.journal / "topics.md").exists()
    # A hand-set malformed slug is also refused at save time.
    bad2 = _topic()
    bad2.slug = "Not A Slug"
    with pytest.raises(EntryError, match="slug"):
        bounded.save_atomic("topics", [bad2])


def test_save_rejects_cross_store_entry(bounded: BoundedStore) -> None:
    with pytest.raises(EntryError, match="belongs to store"):
        bounded.save_atomic("skills", [_mr()])
    with pytest.raises(EntryError, match="belongs to store"):
        bounded.save_atomic("topics", [_skill()])


def test_unknown_store_name_paths_raise(bounded: BoundedStore) -> None:
    with pytest.raises(EntryError, match="unknown store_name"):
        bounded.load("nope")
    with pytest.raises(EntryError, match="unknown store_name"):
        bounded.save_atomic("diary", [])  # retired store is unknown now


def test_load_skips_unparseable_blocks(bounded: BoundedStore) -> None:
    """A block with no frontmatter is skipped (lenient read)."""
    path = bounded.store.paths.journal / "skills.md"
    good = _skill("Good")
    bounded.save_atomic("skills", [good])
    # Append a junk block with no frontmatter.
    text = path.read_text()
    path.write_text(text + "\n<!-- tiger-memory-entry -->\njunk no frontmatter\n")
    got = bounded.load("skills")
    assert len(got) == 1 and got[0].name == "Good"


def test_load_replaces_non_utf8_bytes(
    bounded: BoundedStore, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-UTF8 byte degrades to a replaced (then skipped) block with a
    warning — it never raises and never denies the good sibling entries."""
    good = _skill("Good")
    bounded.save_atomic("skills", [good])
    path = bounded.store.paths.journal / "skills.md"
    path.write_bytes(
        path.read_bytes() + b"\n<!-- tiger-memory-entry -->\n\xff\xfe junk\n"
    )
    with caplog.at_level("WARNING", "tigerharness.tiger_memory.bounded_store"):
        got = bounded.load("skills")
    assert len(got) == 1 and got[0].name == "Good"
    assert any("non-UTF8" in r.message for r in caplog.records)


def test_load_skips_corrupt_field_block(bounded: BoundedStore) -> None:
    """A topic block whose touch_count is non-numeric is skipped; the good
    sibling still loads (lenient read, no-safety-net store)."""
    a, b = _topic("Keep Me", "kept"), _topic("Corrupt Me", "corrupted")
    bounded.save_atomic("topics", [a, b])
    path = bounded.store.paths.journal / "topics.md"
    text = path.read_text()
    assert text.count("touch_count: 1") == 2
    # Corrupt only the SECOND block's numeric field.
    head, _, tail = text.rpartition("touch_count: 1")
    path.write_text(head + "touch_count: not-an-int" + tail)
    got = bounded.load("topics")
    assert [e.name for e in got] == ["Keep Me"]


def test_load_skips_schema_invalid_block(bounded: BoundedStore) -> None:
    """A block that parses but fails validate() (empty summary) is dropped at
    load time — symmetric with save_atomic's validation (QI-1)."""
    t = _topic("Solo", "real summary")
    bounded.save_atomic("topics", [t])
    path = bounded.store.paths.journal / "topics.md"
    path.write_text(path.read_text().replace("summary: real summary", "summary: ''"))
    assert bounded.load("topics") == []


# ----- crash-safety (atomic write) ----------------------------------------


def test_atomic_write_no_tmp_leftover(bounded: BoundedStore) -> None:
    bounded.save_atomic("skills", [_skill()])
    path = bounded.store.paths.journal / "skills.md"
    assert path.exists()
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_save_overwrites_atomically(bounded: BoundedStore) -> None:
    bounded.save_atomic("topics", [_topic("First")])
    bounded.save_atomic("topics", [_topic("Second")])
    got = bounded.load("topics")
    assert len(got) == 1 and got[0].name == "Second"


# ----- measurement: _entry_chars / length_chars / count ---------------------


def test_length_chars_skill_counts_prose_fields(bounded: BoundedStore) -> None:
    s = _skill("Z", text="body")
    expected = len("body") + len("Z") + len("when Z") + len("do Z")
    assert bounded.length_chars([s]) == expected


def test_length_chars_topic_counts_summary_not_slug(bounded: BoundedStore) -> None:
    """_entry_chars counts a topic's text + name + summary; the slug is an
    address, not prose, and is NOT counted."""
    t = _topic("My Name", summary="short summary", text="## d\n- body")
    expected = len("## d\n- body") + len("My Name") + len("short summary")
    assert bounded.length_chars([t]) == expected


def test_length_chars_must_remember_text_only(bounded: BoundedStore) -> None:
    m = _mr("preference", "x" * 17)
    assert bounded.length_chars([m]) == 17


def test_length_chars_sums_entries(bounded: BoundedStore) -> None:
    ms = [_mr("preference", "x" * 10), _mr("decision", "y" * 5)]
    assert bounded.length_chars(ms) == 15
    assert bounded.length_chars([]) == 0


def test_count(bounded: BoundedStore) -> None:
    assert bounded.count([_skill("a"), _skill("b")]) == 2
    assert bounded.count([]) == 0


# ----- measurement: index_chars ---------------------------------------------


def test_index_chars_skills_is_rendered_index_length(bounded: BoundedStore) -> None:
    skills = [_skill("Alpha"), _skill("Beta")]
    assert bounded.index_chars("skills", skills) == len(
        indexes.render_skill_index(skills)
    )
    # The empty index is the rendered placeholder, not zero.
    assert bounded.index_chars("skills", []) == len(indexes.render_skill_index([]))
    assert bounded.index_chars("skills", []) > 0


def test_index_chars_topics_is_rendered_index_length(bounded: BoundedStore) -> None:
    topics = [_topic("One", "first"), _topic("Two", "second")]
    assert bounded.index_chars("topics", topics) == len(
        indexes.render_topic_index(topics)
    )
    assert bounded.index_chars("topics", []) == len(indexes.render_topic_index([]))
    assert bounded.index_chars("topics", []) > 0


def test_index_chars_must_remember_raises(bounded: BoundedStore) -> None:
    with pytest.raises(EntryError, match="no rendered index"):
        bounded.index_chars("must_remember", [_mr()])


# ----- measurement: is_over_overflow (hysteresis edges) ---------------------


def test_is_over_overflow_skills_index_edges(bounded: BoundedStore) -> None:
    just_under = _skills_at_index_chars(bounded, SKILLS_INDEX_OVERFLOW - 1)
    assert bounded.is_over_overflow("skills", just_under) is False
    at_limit = _skills_at_index_chars(bounded, SKILLS_INDEX_OVERFLOW)
    assert bounded.is_over_overflow("skills", at_limit) is True


def test_is_over_overflow_topics_index_edges(bounded: BoundedStore) -> None:
    just_under = _topics_at_index_chars(bounded, TOPICS_INDEX_OVERFLOW - 1)
    assert bounded.is_over_overflow("topics", just_under) is False
    at_limit = _topics_at_index_chars(bounded, TOPICS_INDEX_OVERFLOW)
    assert bounded.is_over_overflow("topics", at_limit) is True


def test_is_over_overflow_must_remember_edges(bounded: BoundedStore) -> None:
    just_under = [_mr("preference", "x" * (MR_OVERFLOW - 1))]
    assert bounded.is_over_overflow("must_remember", just_under) is False
    at_limit = [_mr("preference", "x" * MR_OVERFLOW)]
    assert bounded.is_over_overflow("must_remember", at_limit) is True


def test_hysteresis_band_does_not_overflow(bounded: BoundedStore) -> None:
    """In the max<=n<overflow band, is_over_overflow stays False (no thrash)
    — for all three stores."""
    skills = _skills_at_index_chars(bounded, 250)  # 200 <= 250 < 300
    assert bounded.index_chars("skills", skills) >= bounded.max_bound("skills")
    assert bounded.is_over_overflow("skills", skills) is False

    topics = _topics_at_index_chars(bounded, 270)  # 220 <= 270 < 320
    assert bounded.index_chars("topics", topics) >= bounded.max_bound("topics")
    assert bounded.is_over_overflow("topics", topics) is False

    mid = [_mr("preference", "x" * 50)]  # 40 <= 50 < 60
    assert bounded.length_chars(mid) >= bounded.max_bound("must_remember")
    assert bounded.is_over_overflow("must_remember", mid) is False


def test_max_bound(bounded: BoundedStore) -> None:
    assert bounded.max_bound("skills") == SKILLS_INDEX_MAX
    assert bounded.max_bound("topics") == TOPICS_INDEX_MAX
    assert bounded.max_bound("must_remember") == MR_MAX


# ----- measurement: per-entry detail bounds ---------------------------------


def test_detail_chars_is_rendered_detail_length(bounded: BoundedStore) -> None:
    s = _skill("Alpha")
    assert bounded.detail_chars(s) == len(indexes.render_skill_detail(s))
    t = _topic()
    assert bounded.detail_chars(t) == len(indexes.render_topic_detail(t))


def test_detail_chars_must_remember_raises(bounded: BoundedStore) -> None:
    with pytest.raises(EntryError, match="no detail file"):
        bounded.detail_chars(_mr())


def test_is_detail_over_overflow_skill_edges(bounded: BoundedStore) -> None:
    just_under = _skill_at_detail_chars(bounded, SKILLS_DETAIL_OVERFLOW - 1)
    assert bounded.is_detail_over_overflow(just_under) is False
    at_limit = _skill_at_detail_chars(bounded, SKILLS_DETAIL_OVERFLOW)
    assert bounded.is_detail_over_overflow(at_limit) is True


def test_is_detail_over_overflow_topic_edges(bounded: BoundedStore) -> None:
    just_under = _topic_at_detail_chars(bounded, TOPICS_DETAIL_OVERFLOW - 1)
    assert bounded.is_detail_over_overflow(just_under) is False
    at_limit = _topic_at_detail_chars(bounded, TOPICS_DETAIL_OVERFLOW)
    assert bounded.is_detail_over_overflow(at_limit) is True


def test_is_detail_over_overflow_must_remember_raises(bounded: BoundedStore) -> None:
    """A must_remember entry has no detail file — the check raises rather
    than silently measuring against the wrong store's bound."""
    with pytest.raises(EntryError, match="no detail file"):
        bounded.is_detail_over_overflow(_mr())


def test_detail_max_bound(bounded: BoundedStore) -> None:
    assert bounded.detail_max_bound(_skill()) == SKILLS_DETAIL_MAX
    assert bounded.detail_max_bound(_topic()) == TOPICS_DETAIL_MAX


def test_detail_max_bound_must_remember_raises(bounded: BoundedStore) -> None:
    """Symmetric with detail_chars / is_detail_over_overflow: a
    must_remember entry has no detail file, so asking for its detail bound
    is a caller bug, not a silent topics-bound fallback."""
    with pytest.raises(EntryError, match="no detail file"):
        bounded.detail_max_bound(_mr())


# ----- store_lock ----------------------------------------------------------


def test_store_lock_acquire_release(bounded: BoundedStore) -> None:
    lock_file = bounded.store.paths.journal / ".skills.lock"
    with bounded.store_lock("skills"):
        assert lock_file.exists()
    assert not lock_file.exists()


def test_store_lock_blocks_second_live_holder(bounded: BoundedStore) -> None:
    lock_file = bounded.store.paths.journal / ".skills.lock"
    # Simulate a live holder (our own PID).
    lock_file.write_text(f"{os.getpid()} 0")
    with pytest.raises(StoreLockHeld, match="locked by another live session"):
        with bounded.store_lock("skills"):
            pass
    # The foreign lock is untouched (we never owned it).
    assert lock_file.exists()
    lock_file.unlink()


def test_store_lock_reclaims_dead_holder(bounded: BoundedStore) -> None:
    lock_file = bounded.store.paths.journal / ".skills.lock"
    lock_file.write_text("999999 0")  # PID almost certainly dead
    with bounded.store_lock("skills"):
        assert lock_file.read_text().split()[0] == str(os.getpid())
    assert not lock_file.exists()


def test_store_lock_reclaims_garbage_holder(bounded: BoundedStore) -> None:
    lock_file = bounded.store.paths.journal / ".skills.lock"
    lock_file.write_text("not-a-pid")  # unparseable -> treated as reclaimable
    with bounded.store_lock("skills"):
        assert lock_file.exists()
    assert not lock_file.exists()


def test_store_lock_unknown_store(bounded: BoundedStore) -> None:
    with pytest.raises(EntryError, match="unknown store_name"):
        with bounded.store_lock("nope"):
            pass


def test_store_lock_reraises_non_eexist_oserror(
    bounded: BoundedStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-EEXIST OSError on lock create propagates (e.g. permission denied)."""
    import errno as _errno

    from tigerharness.tiger_memory import bounded_store as bs_mod

    def boom(*a, **k):
        raise OSError(_errno.EACCES, "denied")

    monkeypatch.setattr(bs_mod.os, "open", boom)
    with pytest.raises(OSError, match="denied"):
        with bounded.store_lock("skills"):
            pass


def test_store_lock_independent_per_store(bounded: BoundedStore) -> None:
    """Locking skills does not block topics (per-store granularity)."""
    with bounded.store_lock("skills"):
        with bounded.store_lock("topics"):
            assert (bounded.store.paths.journal / ".skills.lock").exists()
            assert (bounded.store.paths.journal / ".topics.lock").exists()


# ----- forget-guard (the no-safety-net anchor) -----------------------------


def test_forget_drops_normal_entries(bounded: BoundedStore) -> None:
    a, b = _mr("preference", "a"), _mr("decision", "b")
    survivors = bounded.forget("must_remember", [a, b], [a.id])
    assert [e.id for e in survivors] == [b.id]


def test_forget_guard_blocks_unchecked_owner_directive(
    bounded: BoundedStore,
) -> None:
    owner = _mr("operator_explicit", "ship friday")
    with pytest.raises(ForgetGuardError, match="relevance-check"):
        bounded.forget("must_remember", [owner], [owner.id])


def test_forget_allows_owner_directive_after_relevance_check(
    bounded: BoundedStore,
) -> None:
    owner = _mr("operator_explicit", "ship friday")
    survivors = bounded.forget(
        "must_remember", [owner], [owner.id],
        relevance_checked_ids=[owner.id],
    )
    assert survivors == []


def test_forget_ignores_unknown_drop_ids(bounded: BoundedStore) -> None:
    a = _mr("preference", "a")
    survivors = bounded.forget("must_remember", [a], ["does-not-exist"])
    assert [e.id for e in survivors] == [a.id]


def test_forget_guard_only_applies_to_must_remember(
    bounded: BoundedStore,
) -> None:
    """Skills and topics are freely forgettable — the guard is must_remember-only."""
    s = _skill("Sk")
    assert bounded.forget("skills", [s], [s.id]) == []
    t = _topic()
    assert bounded.forget("topics", [t], [t.id]) == []


def test_forget_preserves_owner_when_other_dropped(
    bounded: BoundedStore,
) -> None:
    owner = _mr("operator_explicit", "keep me")
    pref = _mr("preference", "drop me")
    survivors = bounded.forget("must_remember", [owner, pref], [pref.id])
    assert [e.id for e in survivors] == [owner.id]


# ----- serialization internals --------------------------------------------


def test_split_blocks_empty() -> None:
    assert _split_blocks("") == []
    assert _split_blocks("   \n  ") == []

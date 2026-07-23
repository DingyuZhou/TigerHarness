"""Tests for the three store entry schemas (ADR 0007; skills/must_remember/topics)."""
from __future__ import annotations

import inspect

import pytest

from tigerharness.tiger_memory import entries as E
from tigerharness.tiger_memory.entries import (
    EntryError,
    MustRememberEntry,
    SkillEntry,
    TopicEntry,
    entry_class_for,
    entry_from_frontmatter,
    new_id,
    topic_slug,
)

NOW = "2026-06-17T00:00:00Z"


def _base(**kw):
    d = dict(text="some text", created_at=NOW, last_used=NOW, source="extract")
    d.update(kw)
    return d


# ----- ids -----------------------------------------------------------------


def test_new_id_unique_and_nonempty() -> None:
    a, b = new_id(), new_id()
    assert a and b and a != b


# ----- store roster (ADR 0007) ----------------------------------------------


def test_store_roster_is_exactly_three() -> None:
    assert E.STORE_NAMES == ("skills", "must_remember", "topics")
    assert E.STORE_TOPICS == "topics"
    # Diary and fuzzy are retired — no free-text store remains.
    for gone in ("DiaryEntry", "STORE_DIARY", "STORE_FUZZY", "ALL_STORE_NAMES"):
        assert not hasattr(E, gone)


@pytest.mark.parametrize("cls", [SkillEntry, MustRememberEntry, TopicEntry])
def test_validate_takes_no_extra_args(cls: type) -> None:
    # The old weight_cap kwarg is gone everywhere (ADR 0007).
    assert list(inspect.signature(cls.validate).parameters) == ["self"]


# ----- skill ---------------------------------------------------------------


def test_skill_valid() -> None:
    s = SkillEntry(name="N", trigger="when N", procedure="do N", **_base())
    s.validate()
    assert s.store_name == E.STORE_SKILLS
    fm = s.frontmatter()
    assert fm["name"] == "N" and fm["store"] == "skills"
    assert fm["importance"] == 0.0 and fm["usage_count"] == 0


def test_skill_to_dict_carries_store_name() -> None:
    s = SkillEntry(name="N", trigger="t", procedure="p", **_base())
    d = s.to_dict()
    assert d["store_name"] == "skills" and d["name"] == "N"


@pytest.mark.parametrize(
    "field,bad",
    [
        ("name", "  "),
        ("trigger", ""),
        ("procedure", "   "),
    ],
)
def test_skill_rejects_blank_text_fields(field: str, bad: str) -> None:
    kw = dict(name="N", trigger="t", procedure="p")
    kw[field] = bad
    s = SkillEntry(**_base(), **kw)
    with pytest.raises(EntryError, match=field):
        s.validate()


def test_skill_rejects_negative_usage() -> None:
    s = SkillEntry(name="N", trigger="t", procedure="p", usage_count=-1, **_base())
    with pytest.raises(EntryError, match="usage_count"):
        s.validate()


def test_skill_rejects_nonnumber_importance() -> None:
    s = SkillEntry(
        name="N", trigger="t", procedure="p", importance="high", **_base()
    )
    with pytest.raises(EntryError, match="importance"):
        s.validate()


# ----- must_remember -------------------------------------------------------


def test_must_remember_valid_all_kinds() -> None:
    for kind in E.VALID_KINDS:
        m = MustRememberEntry(kind=kind, importance=3, **_base())
        m.validate()
        assert m.frontmatter()["kind"] == kind
    assert m.store_name == E.STORE_MUST_REMEMBER


def test_must_remember_rejects_bad_kind() -> None:
    m = MustRememberEntry(kind="nonsense", **_base())
    with pytest.raises(EntryError, match="kind"):
        m.validate()


def test_must_remember_rejects_bool_importance() -> None:
    m = MustRememberEntry(kind="decision", importance=True, **_base())
    with pytest.raises(EntryError, match="importance"):
        m.validate()


def test_must_remember_repeat_count_default_and_frontmatter() -> None:
    m = MustRememberEntry(kind="preference", **_base())
    m.validate()  # default repeat_count=1 is valid
    assert m.repeat_count == 1
    assert m.frontmatter()["repeat_count"] == 1


def test_must_remember_rejects_bad_repeat_count() -> None:
    for bad in (0, -1, True, 1.5):
        m = MustRememberEntry(kind="preference", repeat_count=bad, **_base())
        with pytest.raises(EntryError, match="repeat_count"):
            m.validate()


# ----- topic_slug (ADR 0007) -------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Topic Store Revamp!", "topic-store-revamp"),
        ("  A/B_c  9 ", "a-b-c-9"),
        ("already-good", "already-good"),
        ("ADR 0007", "adr-0007"),
        ("--edge--", "edge"),
    ],
)
def test_topic_slug_normalizes(name: str, expected: str) -> None:
    assert topic_slug(name) == expected


@pytest.mark.parametrize("name", ["", "!!!", "---", "  ", "🙂🙂"])
def test_topic_slug_symbol_only_raises(name: str) -> None:
    with pytest.raises(EntryError, match="empty slug"):
        topic_slug(name)


# ----- topics (ADR 0007) ------------------------------------------------------


def _topic(**kw):
    d = dict(name="Topic Store Revamp", summary="What changed and why.")
    d.update(kw)
    return TopicEntry(**_base(), **d)


def test_topic_valid_and_frontmatter() -> None:
    t = _topic(slug="topic-store-revamp", touch_count=3)
    t.validate()
    assert t.store_name == E.STORE_TOPICS
    fm = t.frontmatter()
    assert fm["store"] == "topics"
    assert fm["name"] == "Topic Store Revamp"
    assert fm["slug"] == "topic-store-revamp"
    assert fm["summary"] == "What changed and why."
    assert fm["touch_count"] == 3


def test_topic_slug_auto_derives_from_name() -> None:
    t = _topic()  # no slug given
    assert t.slug == "topic-store-revamp"
    t.validate()


def test_topic_explicit_slug_is_kept() -> None:
    t = _topic(slug="custom-address")
    assert t.slug == "custom-address"
    t.validate()


def test_topic_symbol_only_name_raises_at_construction() -> None:
    # Auto-derivation runs in __post_init__, so an unaddressable name fails fast.
    with pytest.raises(EntryError, match="empty slug"):
        _topic(name="!!!")


def test_topic_blank_name_skips_derivation_and_fails_validate() -> None:
    t = _topic(name="  ")  # no derivation attempted; slug stays empty
    assert t.slug == ""
    with pytest.raises(EntryError, match="name"):
        t.validate()


@pytest.mark.parametrize(
    "bad_slug", ["Bad-Slug", "-lead", "trail-", "a--b", "a b", "a_b"]
)
def test_topic_rejects_malformed_slug(bad_slug: str) -> None:
    t = _topic(slug=bad_slug)
    with pytest.raises(EntryError, match="slug"):
        t.validate()


def test_topic_rejects_empty_slug() -> None:
    t = _topic()
    t.slug = ""  # simulate a corrupt write
    with pytest.raises(EntryError, match="slug"):
        t.validate()


def test_topic_rejects_blank_summary() -> None:
    t = _topic(summary="   ")
    with pytest.raises(EntryError, match="summary"):
        t.validate()


def test_topic_rejects_bad_touch_count() -> None:
    for bad in (0, -2, True, 2.5):
        t = _topic(touch_count=bad)
        with pytest.raises(EntryError, match="touch_count"):
            t.validate()


def test_topic_touch_count_default_is_one() -> None:
    t = _topic()
    t.validate()
    assert t.touch_count == 1


def test_topic_to_dict_carries_store_name() -> None:
    d = _topic().to_dict()
    assert d["store_name"] == "topics" and d["slug"] == "topic-store-revamp"


# ----- base-field validation (shared) --------------------------------------


@pytest.mark.parametrize(
    "field",
    ["text", "created_at", "last_used", "source"],
)
def test_base_rejects_blank_required_field(field: str) -> None:
    kw = _base(name="N", trigger="t", procedure="p")
    kw[field] = ""
    s = SkillEntry(**kw)
    with pytest.raises(EntryError, match=field):
        s.validate()


def test_base_rejects_blank_id() -> None:
    s = SkillEntry(name="N", trigger="t", procedure="p", id="", **_base())
    with pytest.raises(EntryError, match="id"):
        s.validate()


# ----- dispatch + reconstruction -------------------------------------------


def test_entry_class_for_known() -> None:
    assert entry_class_for("skills") is SkillEntry
    assert entry_class_for("must_remember") is MustRememberEntry
    assert entry_class_for("topics") is TopicEntry


def test_entry_class_for_unknown_raises() -> None:
    with pytest.raises(EntryError, match="unknown store_name"):
        entry_class_for("diary")


def test_from_frontmatter_skill_roundtrip() -> None:
    s = SkillEntry(
        name="N", trigger="t", procedure="p", usage_count=4, importance=2.5,
        **_base(),
    )
    rebuilt = entry_from_frontmatter("skills", s.frontmatter(), s.text)
    assert isinstance(rebuilt, SkillEntry)
    assert rebuilt.name == "N" and rebuilt.usage_count == 4
    assert rebuilt.importance == 2.5 and rebuilt.id == s.id


def test_from_frontmatter_must_remember_roundtrip() -> None:
    m = MustRememberEntry(kind="incident", importance=1, repeat_count=3, **_base())
    rebuilt = entry_from_frontmatter("must_remember", m.frontmatter(), m.text)
    assert isinstance(rebuilt, MustRememberEntry)
    assert rebuilt.kind == "incident"
    assert rebuilt.repeat_count == 3  # the reinforcement count round-trips


def test_from_frontmatter_topic_roundtrip() -> None:
    t = _topic(slug="topic-store-revamp", touch_count=5)
    rebuilt = entry_from_frontmatter("topics", t.frontmatter(), t.text)
    assert isinstance(rebuilt, TopicEntry)
    assert rebuilt.name == t.name and rebuilt.slug == "topic-store-revamp"
    assert rebuilt.summary == t.summary and rebuilt.touch_count == 5
    assert rebuilt.id == t.id and rebuilt.last_used == NOW
    rebuilt.validate()


def test_from_frontmatter_missing_id_gets_fresh() -> None:
    fm = {"created_at": NOW, "last_used": NOW, "source": "s"}
    rebuilt = entry_from_frontmatter("topics", fm, "body")
    assert rebuilt.id  # a fresh id was minted
    assert rebuilt.touch_count == 1  # dataclass default


def test_from_frontmatter_topic_numeric_string_coerces() -> None:
    fm = _topic().frontmatter()
    fm["touch_count"] = "7"
    rebuilt = entry_from_frontmatter("topics", fm, "body")
    assert rebuilt.touch_count == 7


def test_from_frontmatter_topic_bad_touch_count_raises() -> None:
    fm = _topic().frontmatter()
    fm["touch_count"] = "many"
    with pytest.raises(EntryError, match="touch_count"):
        entry_from_frontmatter("topics", fm, "body")


def test_from_frontmatter_skill_bad_numerics_raise() -> None:
    s = SkillEntry(name="N", trigger="t", procedure="p", **_base())
    fm = s.frontmatter()
    fm["usage_count"] = "lots"
    with pytest.raises(EntryError, match="usage_count"):
        entry_from_frontmatter("skills", fm, s.text)
    fm = s.frontmatter()
    fm["importance"] = "high"
    with pytest.raises(EntryError, match="importance"):
        entry_from_frontmatter("skills", fm, s.text)


def test_from_frontmatter_must_remember_bad_numerics_raise() -> None:
    m = MustRememberEntry(kind="decision", **_base())
    fm = m.frontmatter()
    fm["repeat_count"] = "never"
    with pytest.raises(EntryError, match="repeat_count"):
        entry_from_frontmatter("must_remember", fm, m.text)
    fm = m.frontmatter()
    fm["importance"] = []
    with pytest.raises(EntryError, match="importance"):
        entry_from_frontmatter("must_remember", fm, m.text)


def test_legacy_owner_explicit_kind_normalized_on_read() -> None:
    # A store written before the owner->operator rename carries kind=owner_explicit;
    # it must load as operator_explicit (no silent drop of an elevated directive).
    fm = {"id": "x", "created_at": "t", "last_used": "t", "source": "s",
          "kind": "owner_explicit", "importance": 5}
    rebuilt = entry_from_frontmatter("must_remember", fm, "legacy directive")
    assert isinstance(rebuilt, MustRememberEntry)
    assert rebuilt.kind == "operator_explicit"
    rebuilt.validate()  # the normalized kind is valid
    # a current value passes through unchanged.
    assert E.normalize_kind("operator_explicit") == "operator_explicit"

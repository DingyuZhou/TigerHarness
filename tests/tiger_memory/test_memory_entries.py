"""Tests for the three store entry schemas (design §4; plan §1+§2 dev-1)."""
from __future__ import annotations

import pytest

from tigerharness.tiger_memory import entries as E
from tigerharness.tiger_memory.entries import (
    DiaryEntry,
    EntryError,
    MustRememberEntry,
    SkillEntry,
    entry_class_for,
    entry_from_frontmatter,
    new_id,
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


# ----- emotional -----------------------------------------------------------


def test_emotional_valid() -> None:
    e = DiaryEntry(weight=-7.5, **_base())
    e.validate()
    assert e.store_name == E.STORE_DIARY
    assert e.frontmatter()["weight"] == -7.5


def test_emotional_rejects_over_cap() -> None:
    e = DiaryEntry(weight=11, **_base())
    with pytest.raises(EntryError, match="weight_cap"):
        e.validate()


def test_emotional_custom_cap() -> None:
    e = DiaryEntry(weight=9, **_base())
    e.validate(weight_cap=10)
    with pytest.raises(EntryError, match="weight_cap"):
        e.validate(weight_cap=8)


def test_emotional_rejects_bool_weight() -> None:
    e = DiaryEntry(weight=True, **_base())
    with pytest.raises(EntryError, match="weight"):
        e.validate()


def test_emotional_weight_at_cap_ok() -> None:
    DiaryEntry(weight=10, **_base()).validate()
    DiaryEntry(weight=-10, **_base()).validate()


def test_emotional_rejects_nan_weight() -> None:
    """GAP-3 (schema): a NaN weight is a non-value — ``abs(nan) > cap`` is
    False so it would slip past the cap check and poison the keep-rank
    ordering. ``validate`` must reject it as non-finite."""
    e = DiaryEntry(weight=float("nan"), **_base())
    with pytest.raises(EntryError, match="finite"):
        e.validate()


@pytest.mark.parametrize("weight", [float("inf"), float("-inf")])
def test_emotional_rejects_inf_weight(weight: float) -> None:
    """GAP-3 (schema): ±inf are non-finite and explicitly rejected (the
    finite check subsumes the over-cap path for them)."""
    e = DiaryEntry(weight=weight, **_base())
    with pytest.raises(EntryError, match="finite"):
        e.validate()


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
    assert entry_class_for("diary") is DiaryEntry


def test_entry_class_for_unknown_raises() -> None:
    with pytest.raises(EntryError, match="unknown store_name"):
        entry_class_for("nope")


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


def test_from_frontmatter_emotional_roundtrip() -> None:
    e = DiaryEntry(weight=3.5, **_base())
    rebuilt = entry_from_frontmatter("diary", e.frontmatter(), e.text)
    assert isinstance(rebuilt, DiaryEntry)
    assert rebuilt.weight == 3.5


def test_from_frontmatter_missing_id_gets_fresh() -> None:
    fm = {"created_at": NOW, "last_used": NOW, "source": "s"}
    rebuilt = entry_from_frontmatter("diary", fm, "body")
    assert rebuilt.id  # a fresh id was minted
    assert rebuilt.weight == 0.0

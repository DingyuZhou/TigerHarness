"""Unit tests for the diary dated-bullet format (plan §2 dev-1, b1-dev-1).

Covers serialize / parse / validate and the private helpers to 100% branch
coverage — the single shared format module both validate-on-write and the
`check` verb reuse.
"""
from __future__ import annotations

import pytest

from tigerharness.tiger_memory import diary_format as df
from tigerharness.tiger_memory.diary_format import DiaryEntry, DiaryFormatError


# ----- serialize -----------------------------------------------------------

def test_serialize_empty_is_empty_string():
    assert df.serialize([]) == ""


def test_serialize_single_day_single_bullet():
    out = df.serialize([DiaryEntry("2026-06-17", 7, "did the thing")])
    assert out == "## 2026-06-17\n- (+7) did the thing\n"


def test_serialize_groups_and_sorts_days_ascending():
    entries = [
        DiaryEntry("2026-06-18", 4, "later day"),
        DiaryEntry("2026-06-17", -5, "earlier day"),
        DiaryEntry("2026-06-17", 7, "earlier day two"),
    ]
    out = df.serialize(entries)
    assert out == (
        "## 2026-06-17\n"
        "- (-5) earlier day\n"
        "- (+7) earlier day two\n"
        "\n"
        "## 2026-06-18\n"
        "- (+4) later day\n"
    )


def test_serialize_float_weight_renders_with_decimal():
    out = df.serialize([DiaryEntry("2026-06-17", 7.5, "half")])
    assert "- (+7.5) half" in out


def test_serialize_zero_weight_keeps_sign():
    out = df.serialize([DiaryEntry("2026-06-17", 0, "neutral")])
    assert "- (+0) neutral" in out


# ----- parse ---------------------------------------------------------------

def test_parse_round_trips_canonical_text():
    entries = [
        DiaryEntry("2026-06-17", -5, "a"),
        DiaryEntry("2026-06-18", 7, "b"),
    ]
    assert df.parse(df.serialize(entries)) == entries


def test_parse_skips_blank_lines():
    text = "## 2026-06-17\n\n- (+1) note\n\n"
    assert df.parse(text) == [DiaryEntry("2026-06-17", 1, "note")]


def test_parse_accepts_float_weight():
    assert df.parse("## 2026-06-17\n- (-2.5) x\n") == [
        DiaryEntry("2026-06-17", -2.5, "x")
    ]


def test_parse_bullet_before_header_raises():
    with pytest.raises(DiaryFormatError, match="before any"):
        df.parse("- (+1) orphan\n")


def test_parse_invalid_date_raises():
    with pytest.raises(DiaryFormatError, match="invalid date"):
        df.parse("## 2026-13-01\n- (+1) x\n")


def test_parse_weight_over_cap_raises():
    with pytest.raises(DiaryFormatError, match="exceeds cap"):
        df.parse("## 2026-06-17\n- (+11) x\n")


def test_parse_weight_over_custom_cap_ok_within():
    assert df.parse("## 2026-06-17\n- (+11) x\n", weight_cap=20) == [
        DiaryEntry("2026-06-17", 11, "x")
    ]


def test_parse_empty_note_raises():
    # A bullet whose note is only whitespace -> empty after strip.
    with pytest.raises(DiaryFormatError, match="empty note"):
        df.parse("## 2026-06-17\n- (+1)    \n")


def test_parse_stray_line_raises():
    with pytest.raises(DiaryFormatError, match="stray line"):
        df.parse("## 2026-06-17\nnot a bullet\n")


# ----- validate ------------------------------------------------------------

def test_validate_clean_returns_empty():
    text = df.serialize([DiaryEntry("2026-06-17", 3, "ok")])
    assert df.validate(text) == []


def test_validate_reports_parse_error():
    errs = df.validate("- (+1) orphan\n")
    assert len(errs) == 1 and "before any" in errs[0]


def test_validate_flags_non_canonical_round_trip_day_order():
    # Days in descending order parse fine line-by-line but are not canonical
    # (serialize sorts ascending) -> round-trip mismatch.
    text = "## 2026-06-18\n- (+1) b\n\n## 2026-06-17\n- (+1) a\n"
    errs = df.validate(text)
    assert errs == ["round-trip mismatch: text does not serialize canonically"]


def test_validate_flags_split_same_day_sections():
    # Same day in two non-adjacent sections: parse keeps file order; serialize
    # merges them -> round-trip mismatch.
    text = "## 2026-06-17\n- (+1) a\n\n## 2026-06-18\n- (+2) b\n\n## 2026-06-17\n- (+3) c\n"
    assert df.validate(text) == [
        "round-trip mismatch: text does not serialize canonically"
    ]


# ----- helpers -------------------------------------------------------------

def test_valid_day_rejects_bad_format():
    assert df._valid_day("2026-6-1") is False


def test_valid_day_rejects_bad_month():
    assert df._valid_day("2026-13-01") is False


def test_valid_day_rejects_bad_day():
    assert df._valid_day("2026-01-32") is False


def test_valid_day_accepts_normal_day():
    assert df._valid_day("2026-06-17") is True


@pytest.mark.parametrize(
    "date,ok",
    [
        ("2024-02-29", True),   # leap (div 4, not century)
        ("2023-02-29", False),  # not div 4
        ("2000-02-29", True),   # century leap (div 400)
        ("1900-02-29", False),  # century non-leap (div 100, not 400)
    ],
)
def test_valid_day_leap_year_rules(date, ok):
    assert df._valid_day(date) is ok


def test_format_weight_int_and_float():
    assert df._format_weight(7) == "+7"
    assert df._format_weight(-5.0) == "-5"
    assert df._format_weight(7.5) == "+7.5"

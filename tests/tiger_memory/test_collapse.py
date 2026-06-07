"""Unit tests for the collapsed single-pass summary parser (P1.3)."""
from __future__ import annotations

import pytest

from tigerharness.tiger_memory.collapse import (
    CollapseParseError,
    parse_collapsed,
)


def _valid(must: str = "KIND: decision\nMEMO: store lives in-repo") -> str:
    return (
        "@@SHORT@@\n- decides X\n- ships Y\n"
        "@@DETAILED@@\n## Intent\nThe user wanted X.\n"
        f"@@MUST_MEMORIZE@@\n{must}\n"
    )


def test_parses_well_formed_output() -> None:
    short, detailed, must = parse_collapsed(_valid())
    assert short.startswith("- decides X")
    assert detailed.startswith("## Intent")
    assert "store lives in-repo" in must


def test_none_must_memorize_section_is_allowed() -> None:
    short, detailed, must = parse_collapsed(_valid(must="NONE"))
    assert short and detailed
    assert must == "NONE"


def test_empty_text_raises() -> None:
    with pytest.raises(CollapseParseError, match="empty output"):
        parse_collapsed("")


def test_missing_short_marker_raises() -> None:
    text = "@@DETAILED@@\nstuff\n@@MUST_MEMORIZE@@\nNONE\n"
    with pytest.raises(CollapseParseError, match="missing"):
        parse_collapsed(text)


def test_missing_detailed_marker_raises() -> None:
    text = "@@SHORT@@\n- x\n@@MUST_MEMORIZE@@\nNONE\n"
    with pytest.raises(CollapseParseError, match="missing"):
        parse_collapsed(text)


def test_missing_must_marker_raises() -> None:
    text = "@@SHORT@@\n- x\n@@DETAILED@@\nstuff\n"
    with pytest.raises(CollapseParseError, match="missing"):
        parse_collapsed(text)


def test_markers_out_of_order_raises() -> None:
    text = (
        "@@DETAILED@@\nstuff\n@@SHORT@@\n- x\n@@MUST_MEMORIZE@@\nNONE\n"
    )
    with pytest.raises(CollapseParseError, match="out of order"):
        parse_collapsed(text)


def test_empty_short_section_raises() -> None:
    text = "@@SHORT@@\n   \n@@DETAILED@@\nstuff\n@@MUST_MEMORIZE@@\nNONE\n"
    with pytest.raises(CollapseParseError, match="empty short or detailed"):
        parse_collapsed(text)


def test_empty_detailed_section_raises() -> None:
    text = "@@SHORT@@\n- x\n@@DETAILED@@\n  \n@@MUST_MEMORIZE@@\nNONE\n"
    with pytest.raises(CollapseParseError, match="empty short or detailed"):
        parse_collapsed(text)

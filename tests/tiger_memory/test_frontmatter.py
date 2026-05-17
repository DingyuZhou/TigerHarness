"""Tests for tiger_memory.frontmatter."""
from __future__ import annotations

from tigerharness.tiger_memory.frontmatter import parse, render


def test_roundtrip() -> None:
    fm = {"type": "short_summary", "conversation_uuid": "abc-123", "count": 7}
    body = "# Hello\n\nThis is the body.\n"
    text = render(fm, body)
    parsed_fm, parsed_body = parse(text)
    assert parsed_fm == fm
    assert parsed_body == body


def test_parse_no_frontmatter() -> None:
    fm, body = parse("just plain content\n")
    assert fm == {}
    assert body == "just plain content\n"


def test_parse_malformed_frontmatter() -> None:
    text = "---\nbroken: [unclosed\n---\nbody\n"
    fm, body = parse(text)
    # Falls back to no-frontmatter rather than raising.
    assert fm == {}


def test_parse_unterminated_frontmatter() -> None:
    text = "---\nkey: value\nno-close-marker\n"
    fm, body = parse(text)
    assert fm == {}
    assert body == text

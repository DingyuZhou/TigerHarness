"""Additional frontmatter tests for uncovered branches."""
from __future__ import annotations

from pathlib import Path

from tigerharness.tiger_memory.frontmatter import parse, read_frontmatter, render


class TestParseEdgeCases:
    def test_body_starts_with_newline_stripped(self):
        text = "---\nkey: value\n---\n\nBody here."
        fm, body = parse(text)
        assert fm == {"key": "value"}
        assert body == "Body here."

    def test_body_no_leading_newline(self):
        text = "---\nkey: value\n---\nBody here."
        fm, body = parse(text)
        assert body == "Body here."

    def test_non_dict_frontmatter_returns_empty(self):
        text = "---\n- item1\n- item2\n---\nBody."
        fm, body = parse(text)
        assert fm == {}
        assert body == text

    def test_invalid_yaml_returns_empty(self):
        text = "---\n: bad: yaml: {{{\n---\nBody."
        fm, body = parse(text)
        assert fm == {}

    def test_unclosed_frontmatter(self):
        text = "---\nkey: value\nno closing delimiter"
        fm, body = parse(text)
        assert fm == {}
        assert body == text


class TestRender:
    def test_adds_trailing_newline(self):
        result = render({"key": "val"}, "no trailing newline")
        assert result.endswith("\n")

    def test_body_already_has_newline(self):
        result = render({"key": "val"}, "body\n")
        assert not result.endswith("\n\n")


class TestReadFrontmatter:
    def test_missing_file(self, tmp_path: Path):
        fm = read_frontmatter(tmp_path / "nonexistent.md")
        assert fm == {}

    def test_file_with_frontmatter(self, tmp_path: Path):
        p = tmp_path / "test.md"
        p.write_text("---\ntitle: Hello\n---\nBody.")
        fm = read_frontmatter(p)
        assert fm == {"title": "Hello"}

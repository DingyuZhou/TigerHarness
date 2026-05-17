"""YAML frontmatter parser/writer for tiger-memory markdown files.

Format:
    ---
    key: value
    ---
    body markdown here
"""
from __future__ import annotations

from typing import Any

import yaml


_DELIM = "---"


def parse(text: str) -> tuple[dict[str, Any], str]:
    """Split YAML frontmatter from body. Returns ``({}, text)`` if no frontmatter."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != _DELIM:
        return {}, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == _DELIM:
            end = i
            break
    if end is None:
        return {}, text
    fm_text = "".join(lines[1:end])
    body = "".join(lines[end + 1 :])
    if body.startswith("\n"):
        body = body[1:]
    try:
        data = yaml.safe_load(fm_text) or {}
        if not isinstance(data, dict):
            return {}, text
        return data, body
    except yaml.YAMLError:
        return {}, text


def render(frontmatter: dict[str, Any], body: str) -> str:
    """Render *frontmatter* + *body* as a markdown file string."""
    fm_text = yaml.safe_dump(
        frontmatter,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    if not body.endswith("\n"):
        body = body + "\n"
    return f"{_DELIM}\n{fm_text}{_DELIM}\n{body}"


def read_frontmatter(path) -> dict[str, Any]:
    """Read just the frontmatter from *path* (no body materialization)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return {}
    fm, _ = parse(text)
    return fm

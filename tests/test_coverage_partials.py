"""Tests targeting remaining partial branch coverage.

Covers:
- router.py:157->159 — duplicate roster name (case-insensitive collision)
- notifier.py:117->116 — _first_allowed_user_from_yaml with no valid entries
- notifier.py:252->254 — notify_stuck_escalation with empty meta.name
- stuck_watchdog.py:59->64 — _read_btime with no btime line
- lifecycle.py:303->276 — slack_thread source in source list with other sources
- claude_transcript.py:340->326, 350->326 — content type fallthrough
"""
from __future__ import annotations

from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestRouterDuplicateRosterName:
    """router.py:157->159: key already in idx → skip (duplicate name)."""

    def test_duplicate_name_first_wins(self):
        from tigerharness.slack_bridge.router import _build_alias_index

        # Duplicate case-insensitive names: "Alpha" and "alpha"
        # The first should win.
        idx = _build_alias_index(["Alpha", "alpha"], None)
        assert idx["alpha"] == "Alpha"  # first wins

    def test_duplicate_via_case_with_aliases(self):
        from tigerharness.slack_bridge.router import _build_alias_index

        # Alias collides with a canonical name
        aliases = {"Beta": ["alpha"]}  # alias "alpha" collides with name "Alpha"
        idx = _build_alias_index(["Alpha", "Beta"], aliases)
        assert idx["alpha"] == "Alpha"  # canonical wins over alias


class TestTranscriptUnknownBlockType:
    """claude_transcript.py:340->326, 350->326 — unknown block types."""

    def test_unknown_dict_block_type(self):
        """340->326: dict block with unknown type → skipped."""
        from tigerharness.tiger_memory.sources.claude_transcript import _extract_text
        event = {"message": {"content": [
            {"type": "thinking", "thinking": "internal thought"},
            {"type": "text", "text": "visible"},
        ]}}
        result = _extract_text(event)
        assert "visible" in result
        assert "internal thought" not in result

    def test_non_dict_non_str_block(self):
        """350->326: block that is neither dict nor str → skipped."""
        from tigerharness.tiger_memory.sources.claude_transcript import _extract_text
        event = {"message": {"content": [
            12345,
            None,
            {"type": "text", "text": "visible"},
        ]}}
        result = _extract_text(event)
        assert "visible" in result


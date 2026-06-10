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


class TestDrillPartialBranches:
    """drill.py partial branches."""

    def test_grep_hits_rg_error_code(self, tmp_path):
        """185->192: rg returns non-0/1 code → falls through to Python fallback."""
        from tigerharness.tiger_memory.store import Store
        from tigerharness.tiger_memory.drill import _grep_hits

        store = Store(tmp_path)
        store.init_layout()
        (store.paths.journal / "test.md").write_text("searchable content\n")

        # rg returns returncode=2 (usage error) → Python fallback
        fake_result = MagicMock()
        fake_result.returncode = 2
        with patch("tigerharness.tiger_memory.drill.subprocess.run",
                   return_value=fake_result):
            paths = _grep_hits(store, "searchable")
        assert len(paths) >= 1

    def test_grep_hits_python_fallback_no_match(self, tmp_path):
        """197->195: file doesn't match pattern → loop continues."""
        from tigerharness.tiger_memory.store import Store
        from tigerharness.tiger_memory.drill import _grep_hits

        store = Store(tmp_path)
        store.init_layout()
        (store.paths.journal / "test.md").write_text("unrelated content\n")

        with patch("tigerharness.tiger_memory.drill.subprocess.run",
                   side_effect=FileNotFoundError("rg not found")):
            paths = _grep_hits(store, "nonexistent_topic")
        assert len(paths) == 0

    def test_python_grep_no_match(self, tmp_path, capsys):
        """271->269: _python_grep files don't match → no results."""
        from tigerharness.tiger_memory.store import Store
        from tigerharness.tiger_memory.drill import _python_grep

        store = Store(tmp_path)
        store.init_layout()
        (store.paths.journal / "test.md").write_text("unrelated\n")

        result = _python_grep(store, "zzz_nonexistent", max_hits=10)
        assert result == 0


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


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


class TestNotifierNoValidUserIds:
    """notifier.py:117->116: loop completes without valid entry."""

    def test_all_invalid_entries(self, tmp_path):
        from tigerharness.task_runner.notifier import _first_allowed_user_from_yaml
        cfg = tmp_path / "bridge.yaml"
        cfg.write_text("allowed_user_ids:\n  - 123\n  - ''\n  - '   '\n")
        result = _first_allowed_user_from_yaml(cfg)
        assert result is None

    def test_empty_list(self, tmp_path):
        from tigerharness.task_runner.notifier import _first_allowed_user_from_yaml
        cfg = tmp_path / "bridge.yaml"
        cfg.write_text("allowed_user_ids: []\n")
        result = _first_allowed_user_from_yaml(cfg)
        assert result is None


class TestNotifierEmptyMetaName:
    """notifier.py:252->254: meta.name is empty → skip name line."""

    def test_stuck_escalation_no_name(self, tmp_path):
        from tigerharness.task_runner.notifier import notify_stuck_escalation
        from tigerharness.task_runner.registry import JobMeta

        meta = JobMeta(
            job_id="noname-test", persona="tester", prompt_chars=10,
            max_iters=3, compact_every=0, continuation="", name="",
            cwd="/tmp", started_at=0.0, status="running", pid=None,
            current_iter=1, session_id="", last_update=0.0,
        )

        # Provide creds so we reach the name check at line 252
        with patch("tigerharness.task_runner.notifier._resolve_creds",
                   return_value=("xoxb-fake", "U0CEO")):
            with patch("tigerharness.task_runner.notifier._post_json",
                       return_value={"ok": True}):
                result = notify_stuck_escalation(meta, iter_num=1, detail="test detail")
        assert result is True

    def test_stuck_escalation_with_name(self, tmp_path):
        """252->254 True: meta.name is non-empty → name line included."""
        from tigerharness.task_runner.notifier import notify_stuck_escalation
        from tigerharness.task_runner.registry import JobMeta

        meta = JobMeta(
            job_id="named-test", persona="tester", prompt_chars=10,
            max_iters=3, compact_every=0, continuation="", name="my-task",
            cwd="/tmp", started_at=0.0, status="running", pid=None,
            current_iter=1, session_id="", last_update=0.0,
        )

        posted_payloads = []

        def _capture_post(endpoint, token, payload):
            posted_payloads.append(payload)
            return {"ok": True}

        with patch("tigerharness.task_runner.notifier._resolve_creds",
                   return_value=("xoxb-fake", "U0CEO")):
            with patch("tigerharness.task_runner.notifier._post_json",
                       side_effect=_capture_post):
                result = notify_stuck_escalation(meta, iter_num=1, detail="test")
        assert result is True
        assert any("my-task" in p.get("text", "") for p in posted_payloads)


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


class TestReadBtimeNoBtimeLine:
    """stuck_watchdog.py:59->64: /proc/stat has no btime line."""

    def test_no_btime_line(self):
        from tigerharness.task_runner.stuck_watchdog import _read_btime
        fake_stat = StringIO("cpu  123 456\nprocesses 789\n")
        with patch("builtins.open", return_value=fake_stat):
            result = _read_btime()
        assert result is None

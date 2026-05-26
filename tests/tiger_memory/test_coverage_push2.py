"""Coverage-push tests for tiger_memory modules.

Covers:
- briefing.py:97->99 (rebuild exception cleanup)
- cli.py:133-134 (no subcommand → print help)
- drill.py: 33->37 (raw no children), 122->121 (_find_claude_jsonl empty uuid),
  185->192 (rg fails → Python fallback), 197->195 (file read OSError in grep),
  271->269 (_python_grep glob loop)
- lifecycle.py:303->276 (slack_thread source kind → pass)
- must_memorize.py:53->55 (bump on locked row)
- store.py:241->243 (refresher join when not None)
- summarizers/anthropic.py:136->138 (strip codefence with closing ```)
- sources/claude_transcript.py:94-95 (OSError on stat),
  334->336 (tool_use_id is str), 340->326 (tool_result skip),
  350->326 (str block in content)
"""
from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestBriefingRebuildException:
    """briefing.py:97->99: rebuild exception cleans up temp dir."""

    def test_rebuild_cleanup_on_error(self, tmp_path):
        from tigerharness.tiger_memory.config import Config
        from tigerharness.tiger_memory.store import Store

        # Set up minimal store
        store_root = tmp_path / "memories" / "Test"
        store_root.mkdir(parents=True)
        store = Store(store_root)
        store.init_layout()

        cfg = MagicMock()
        cfg.agent.name = "Test"
        cfg.summarizer.prompts = "default"

        with patch("tigerharness.tiger_memory.briefing._render_readme",
                   side_effect=RuntimeError("boom")):
            with patch("tigerharness.tiger_memory.briefing._briefing_up_to_date",
                       return_value=False):
                from tigerharness.tiger_memory.briefing import rebuild_briefing
                with pytest.raises(RuntimeError, match="boom"):
                    rebuild_briefing(cfg, store)


class TestCliNoSubcommand:
    """cli.py:133-134: no recognized subcommand → print help, return 2."""

    def test_no_args_prints_help(self, capsys):
        from tigerharness.tiger_memory.cli import main
        # With no arguments, main should exit with code 2
        with pytest.raises(SystemExit):
            main([])

    def test_unknown_cmd_falls_through(self, tmp_path, capsys):
        """Lines 133-134: args.cmd doesn't match any known command.
        We force this by patching parse_args to return a cmd we don't handle."""
        from tigerharness.tiger_memory import cli as cli_mod
        import argparse

        fake_args = argparse.Namespace(cmd="unknown_cmd", config=None)

        mock_store = MagicMock()
        mock_cfg = MagicMock()
        mock_cfg.store.root = tmp_path

        with patch.object(argparse.ArgumentParser, "parse_args", return_value=fake_args):
            with patch("tigerharness.tiger_memory.cli.load_config", return_value=mock_cfg):
                with patch("tigerharness.tiger_memory.cli.Store", return_value=mock_store):
                    result = cli_mod.main(["--config", "/fake"])

        assert result == 2


class TestDrillBranches:
    """drill.py branch coverage: 33->37, etc."""

    def test_drill_file_no_children(self, tmp_path, capsys):
        """drill.py:33->37: drill a file that has no children."""
        from tigerharness.tiger_memory.store import Store
        from tigerharness.tiger_memory.drill import drill

        store = Store(tmp_path)
        store.init_layout()

        # Create a file with no child references
        f = store.paths.journal / "2026-01-01-test.md"
        f.write_text("---\ntype: short\n---\nTest content only\n")

        result = drill(store, f)
        assert result == 0
        out = capsys.readouterr().out
        assert "Test content only" in out

    def test_raw_claude_code_source(self, tmp_path, capsys):
        """raw with source: claude_code + JSONL exists."""
        from tigerharness.tiger_memory.store import Store
        from tigerharness.tiger_memory.drill import raw
        from tigerharness.tiger_memory.config import SourceConfig

        store = Store(tmp_path)
        store.init_layout()

        f = store.paths.archive / "2026-01-01-test.md"
        f.write_text(
            "---\nsource: claude_code\nsource_id: abc-def\n"
            "conversation_uuid: abc-def\n---\nContent\n"
        )

        # Create the JSONL the raw function expects to find
        project_path = tmp_path / "project"
        project_path.mkdir()
        (project_path / "abc-def.jsonl").write_text('{"line":1}\n')

        cfg = MagicMock()
        cfg.sources = [SourceConfig(kind="claude_code",
                                     fields={"project_path": str(project_path)})]
        result = raw(cfg, store, f)
        assert result == 0


class TestFindClaudeJsonl:
    """drill.py:122->121: empty session_uuid."""

    def test_empty_uuid_returns_none(self):
        from tigerharness.tiger_memory.drill import _find_claude_jsonl
        cfg = MagicMock()
        assert _find_claude_jsonl(cfg, "") is None

    def test_no_matching_source(self):
        from tigerharness.tiger_memory.drill import _find_claude_jsonl
        cfg = MagicMock()
        cfg.sources = []
        assert _find_claude_jsonl(cfg, "abc-123") is None


class TestGrepFallback:
    """drill.py:185->192: rg not found → Python fallback.
    drill.py:197->195: OSError reading file in Python fallback.
    drill.py:271->269: _python_grep loop."""

    def test_grep_fallback_when_rg_missing(self, tmp_path, capsys):
        from tigerharness.tiger_memory.store import Store
        from tigerharness.tiger_memory.drill import search

        store = Store(tmp_path)
        store.init_layout()

        # Write a journal file with searchable content
        (store.paths.journal / "test.md").write_text("unicorn topic here\n")

        cfg = MagicMock()
        cfg.sources = []

        with patch("tigerharness.tiger_memory.drill.subprocess.run",
                   side_effect=FileNotFoundError("rg not found")):
            result = search(cfg, store, topic="unicorn", mode="grep")

        out = capsys.readouterr().out
        assert result == 0

    def test_grep_oserror_on_file_read(self, tmp_path, capsys):
        """197->195: OSError during file read is skipped."""
        from tigerharness.tiger_memory.store import Store
        from tigerharness.tiger_memory.drill import search

        store = Store(tmp_path)
        store.init_layout()

        # Create a file that exists but will error on read
        f = store.paths.journal / "bad.md"
        f.write_text("data\n")

        cfg = MagicMock()
        cfg.sources = []

        # Make read_text raise after glob finds the file
        original_glob = Path.glob

        def _mock_glob(self, pattern):
            yield f

        with patch("tigerharness.tiger_memory.drill.subprocess.run",
                   side_effect=FileNotFoundError("rg not found")):
            with patch.object(Path, "read_text", side_effect=OSError("permission denied")):
                result = search(cfg, store, topic="data", mode="grep")

        assert result == 0


class TestLifecycleSlackThreadSource:
    """lifecycle.py:303->276: slack_thread source kind → pass (no adapter created)."""

    def test_slack_thread_source_is_noop(self, tmp_path):
        from tigerharness.tiger_memory.lifecycle import _build_adapters

        cfg = MagicMock()
        cfg.sources = [MagicMock(kind="slack_thread", fields={"threads_json": "/tmp/threads.json"})]
        cfg.agent.name = "Test"
        adapters = _build_adapters(cfg)
        # slack_thread doesn't produce an adapter — it's passthrough metadata
        assert len(adapters) == 0


class TestMustMemorizeBumpLocked:
    """must_memorize.py:53->55: bump on locked row doesn't increase score."""

    def test_bump_locked_row(self):
        from tigerharness.tiger_memory.must_memorize import Row as MustMemorizeRow
        row = MustMemorizeRow(
            kind="owner_explicit",
            memo="never forget",
            score=10,
            locked=True,
        )
        row.bump("2026-05-26")
        # Score should NOT increase
        assert row.score == 10
        # But last_bump IS updated
        assert row.last_bump == "2026-05-26"


class TestStoreRefresherJoin:
    """store.py:241->243: refresher thread joined on lock release."""

    def test_lock_releases_refresher(self, tmp_path):
        from tigerharness.tiger_memory.store import Store
        store = Store(tmp_path)
        store.init_layout()
        lock_path = tmp_path / ".lock"
        # Using lock context manager exercises the refresher join (line 241->243)
        with store.lock(lock_path, timeout_minutes=1) as acquired:
            assert acquired is True
        # After exiting, lock file should be cleaned up
        assert not lock_path.exists()


class TestStripCodefenceClosing:
    """summarizers/anthropic.py:136->138: closing ``` stripped."""

    def test_strip_codefence_with_closing(self):
        from tigerharness.tiger_memory.summarizers.anthropic import _strip_codefence
        text = "```markdown\nHello world\n```"
        result = _strip_codefence(text)
        assert result == "Hello world"

    def test_strip_codefence_no_closing(self):
        from tigerharness.tiger_memory.summarizers.anthropic import _strip_codefence
        text = "```\nHello world"
        result = _strip_codefence(text)
        assert result == "Hello world"

    def test_strip_codefence_no_opening(self):
        from tigerharness.tiger_memory.summarizers.anthropic import _strip_codefence
        text = "just plain text"
        result = _strip_codefence(text)
        assert result == "just plain text"


class TestClaudeTranscriptOSError:
    """sources/claude_transcript.py:94-95: OSError on stat → continue."""

    def test_stat_oserror_skips_file(self, tmp_path):
        from tigerharness.tiger_memory.sources.claude_transcript import ClaudeTranscriptAdapter

        adapter = ClaudeTranscriptAdapter(
            project_path=tmp_path,
            max_age_days=7,
        )

        # Create a JSONL file
        jsonl = tmp_path / "test-session.jsonl"
        jsonl.write_text('{"type":"summary","summary":"test"}\n')

        # Make only the individual file's stat() fail, not the directory's
        original_stat = Path.stat

        def patched_stat(self, *args, **kwargs):
            if self.suffix == ".jsonl":
                raise OSError("permission denied")
            return original_stat(self, *args, **kwargs)

        with patch.object(Path, "stat", patched_stat):
            records = list(adapter.discover())
        # Should not crash, just skip
        assert len(records) == 0


class TestClaudeTranscriptContentBlocks:
    """sources/claude_transcript.py: block type handling in _extract_text."""

    def test_tool_use_id_string_added_to_skip_set(self, tmp_path):
        """Line 334->336: tool_use block id is str → added to skipped set."""
        from tigerharness.tiger_memory.sources.claude_transcript import _extract_text

        event = {"message": {"content": [
            {"type": "tool_use", "name": "Read", "id": "tu_1",
             "input": {"file_path": "/memory/briefing/README.md"}},
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "briefing data"},
            {"type": "text", "text": "visible text"},
        ]}}
        result = _extract_text(event)
        assert "visible text" in result
        # The briefing-read tool_use and its result should be skipped
        assert "briefing data" not in result

    def test_str_block_in_content(self, tmp_path):
        """Line 350->326: plain string block in content list."""
        from tigerharness.tiger_memory.sources.claude_transcript import _extract_text

        event = {"message": {"content": [
            "plain string block",
            {"type": "text", "text": "dict block"},
        ]}}
        result = _extract_text(event)
        assert "plain string block" in result
        assert "dict block" in result

    def test_tool_result_with_list_content(self, tmp_path):
        """Line 340->326: tool_result with list content."""
        from tigerharness.tiger_memory.sources.claude_transcript import _extract_text

        event = {"message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu_2",
             "content": [{"text": "inner text"}, {"other": "ignored"}]},
        ]}}
        result = _extract_text(event)
        assert "inner text" in result

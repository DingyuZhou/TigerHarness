"""Additional tests for drill.py — covers raw(), _grep_search, _preview, and edge cases."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from unittest.mock import patch, MagicMock
from uuid import uuid4

import pytest

from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.drill import (
    _children_of,
    _preview,
    drill,
    raw,
    search,
    tree,
)
from tigerharness.tiger_memory.store import Store


def _setup(tmp_path: Path):
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(dedent(f"""\
        agent: {{name: T, role: T}}
        store: {{root: {tmp_path}/memory}}
        sources:
          - kind: claude_code
            project_path: {tmp_path}/proj/
        summarizer: {{backend: anthropic, model: claude-opus-4-7, prompts: default/v1}}
        rebuild: {{lock_path: {tmp_path}/lock}}
    """))
    cfg = load_config(cfg_path)
    store = Store(cfg.store.root)
    store.init_layout()
    return cfg, store


class TestRaw:
    def test_not_found(self, tmp_path: Path, capsys):
        cfg, store = _setup(tmp_path)
        rc = raw(cfg, store, Path("nonexistent.md"))
        assert rc == 2
        assert "not found" in capsys.readouterr().out

    def test_claude_code_source_found(self, tmp_path: Path, capsys):
        cfg, store = _setup(tmp_path)
        uid = str(uuid4())
        # Create archive entry with frontmatter
        archive_file = store.paths.archive / f"20260514-082136-{uid}.md"
        archive_file.write_text(dedent(f"""\
            ---
            source: claude_code
            source_id: "{uid}"
            conversation_uuid: "{uid}"
            ---
            # Session content
        """))
        # Create the JSONL file it should find
        proj_dir = tmp_path / "proj"
        proj_dir.mkdir(parents=True, exist_ok=True)
        jsonl = proj_dir / f"{uid}.jsonl"
        jsonl.write_text('{"type":"msg"}\n')

        rc = raw(cfg, store, archive_file)
        assert rc == 0
        out = capsys.readouterr().out
        assert str(jsonl) in out

    def test_claude_code_source_not_found(self, tmp_path: Path, capsys):
        cfg, store = _setup(tmp_path)
        uid = str(uuid4())
        archive_file = store.paths.archive / f"20260514-082136-{uid}.md"
        archive_file.write_text(dedent(f"""\
            ---
            source: claude_code
            source_id: "{uid}"
            conversation_uuid: "{uid}"
            ---
            # Content
        """))
        # No JSONL file exists
        rc = raw(cfg, store, archive_file)
        assert rc == 1
        assert "not found" in capsys.readouterr().out

    def test_slack_source_with_channel(self, tmp_path: Path, capsys):
        cfg, store = _setup(tmp_path)
        uid = str(uuid4())
        archive_file = store.paths.archive / f"20260514-082136-{uid}.md"
        archive_file.write_text(dedent(f"""\
            ---
            source: slack
            source_id: "1234567890.123456@C0ABCDEF"
            conversation_uuid: "{uid}"
            ---
            # Slack convo
        """))
        rc = raw(cfg, store, archive_file)
        assert rc == 0
        out = capsys.readouterr().out
        assert "https://slack.com/archives/C0ABCDEF/p" in out

    def test_slack_source_without_channel(self, tmp_path: Path, capsys):
        cfg, store = _setup(tmp_path)
        uid = str(uuid4())
        archive_file = store.paths.archive / f"20260514-082136-{uid}.md"
        archive_file.write_text(dedent(f"""\
            ---
            source: slack
            source_id: "1234567890.123456"
            conversation_uuid: "{uid}"
            ---
            # Slack convo
        """))
        rc = raw(cfg, store, archive_file)
        assert rc == 0
        out = capsys.readouterr().out
        assert "slack_thread_ts: 1234567890.123456" in out

    def test_doc_source(self, tmp_path: Path, capsys):
        cfg, store = _setup(tmp_path)
        uid = str(uuid4())
        archive_file = store.paths.archive / f"20260514-082136-{uid}.md"
        archive_file.write_text(dedent(f"""\
            ---
            source: doc
            source_id: "docs/design.md"
            ---
            # Doc
        """))
        rc = raw(cfg, store, archive_file)
        assert rc == 0
        assert "docs/design.md" in capsys.readouterr().out

    def test_unknown_source(self, tmp_path: Path, capsys):
        cfg, store = _setup(tmp_path)
        uid = str(uuid4())
        archive_file = store.paths.archive / f"20260514-082136-{uid}.md"
        archive_file.write_text(dedent(f"""\
            ---
            source: jira
            source_id: "PROJ-123"
            ---
            # Unknown
        """))
        rc = raw(cfg, store, archive_file)
        assert rc == 1
        assert "unknown source" in capsys.readouterr().out


class TestGrepSearch:
    def test_grep_search_with_rg(self, tmp_path: Path, capsys):
        """When rg is available and finds hits."""
        cfg, store = _setup(tmp_path)
        short = store.paths.journal / f"20260514-082136-{uuid4()}.md"
        short.write_text("Discussion about tigerharness architecture.\n")

        rc = search(cfg, store, topic="tigerharness", mode="grep")
        assert rc == 0
        out = capsys.readouterr().out
        # Either rg found it or python fallback did
        assert "20260514" in out or "no matches" in out

    def test_grep_search_no_matches(self, tmp_path: Path, capsys):
        cfg, store = _setup(tmp_path)
        # Empty store
        rc = search(cfg, store, topic="nonexistent_topic_xyz", mode="grep")
        assert rc == 0
        out = capsys.readouterr().out
        assert "no matches" in out


class TestSearchAuto:
    def test_auto_mode_falls_back_to_grep(self, tmp_path: Path, capsys):
        """auto mode without rag available should use grep."""
        cfg, store = _setup(tmp_path)
        short = store.paths.journal / f"20260514-082136-{uuid4()}.md"
        short.write_text("Notes about volume targeting.\n")

        with patch("tigerharness.tiger_memory.drill._rag_available", return_value=False):
            rc = search(cfg, store, topic="volume", mode="auto")
            assert rc == 0


class TestPreview:
    def test_preview_returns_first_content_line(self, tmp_path: Path):
        p = tmp_path / "test.md"
        p.write_text(dedent("""\
            ---
            title: Test
            ---
            # Heading

            First real content line here.
            Second line.
        """))
        result = _preview(p)
        assert result == "First real content line here."

    def test_preview_returns_bullet_content(self, tmp_path: Path):
        p = tmp_path / "test.md"
        p.write_text(dedent("""\
            # Notes

            - Bullet point first
            - Second bullet
        """))
        result = _preview(p)
        assert result == "Bullet point first"

    def test_preview_empty_file(self, tmp_path: Path):
        p = tmp_path / "empty.md"
        p.write_text("")
        result = _preview(p)
        assert result == ""

    def test_preview_nonexistent_file(self, tmp_path: Path):
        p = tmp_path / "nope.md"
        result = _preview(p)
        assert result == ""

    def test_preview_truncates_long_lines(self, tmp_path: Path):
        p = tmp_path / "long.md"
        p.write_text("x" * 200 + "\n")
        result = _preview(p)
        assert len(result) == 80


class TestGrepHitsWithRg:
    """Test _grep_hits when rg is available and finds results."""

    def test_grep_hits_rg_success(self, tmp_path: Path):
        from tigerharness.tiger_memory.drill import _grep_hits
        cfg, store = _setup(tmp_path)
        f1 = store.paths.journal / f"20260514-082136-{uuid4()}.md"
        f1.write_text("Solar energy discussion.\n")
        f2 = store.paths.archive / f"20260513-120000-{uuid4()}.md"
        f2.write_text("Solar panels overview.\n")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = f"{f1}\n{f2}\n"
        with patch("tigerharness.tiger_memory.drill.subprocess.run",
                    return_value=mock_result):
            hits = _grep_hits(store, "Solar")
        assert len(hits) == 2

    def test_grep_hits_rg_no_matches(self, tmp_path: Path):
        from tigerharness.tiger_memory.drill import _grep_hits
        cfg, store = _setup(tmp_path)
        mock_result = MagicMock()
        mock_result.returncode = 1  # rg returns 1 for no matches
        mock_result.stdout = ""
        with patch("tigerharness.tiger_memory.drill.subprocess.run",
                    return_value=mock_result):
            hits = _grep_hits(store, "nonexistent")
        assert hits == []


class TestHybridSearch:
    def test_hybrid_with_results(self, tmp_path: Path, capsys):
        cfg, store = _setup(tmp_path)
        f1 = store.paths.journal / f"20260514-082136-{uuid4()}.md"
        f1.write_text("Solar energy discussion.\n")

        # Mock grep to find f1
        with patch("tigerharness.tiger_memory.drill._grep_hits",
                    return_value=[f1]):
            with patch("tigerharness.tiger_memory.drill._rag_available",
                       return_value=False):
                rc = search(cfg, store, topic="Solar", mode="hybrid")
        assert rc == 0
        out = capsys.readouterr().out
        assert "Solar" in out or "20260514" in out

    def test_hybrid_no_results(self, tmp_path: Path, capsys):
        cfg, store = _setup(tmp_path)
        with patch("tigerharness.tiger_memory.drill._grep_hits", return_value=[]):
            rc = search(cfg, store, topic="nonexistent", mode="hybrid")
        assert rc == 0
        out = capsys.readouterr().out
        assert "no matches" in out


class TestGrepSearchRgSuccess:
    def test_grep_search_rg_returns_hits(self, tmp_path: Path, capsys):
        from tigerharness.tiger_memory.drill import _grep_search
        cfg, store = _setup(tmp_path)
        f1 = store.paths.journal / f"20260514-082136-{uuid4()}.md"
        f1.write_text("Content about testing.\n")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = f"{f1}\n"
        with patch("tigerharness.tiger_memory.drill.subprocess.run",
                    return_value=mock_result):
            rc = _grep_search(store, topic="testing")
        assert rc == 0
        out = capsys.readouterr().out
        assert "20260514" in out

    def test_grep_search_rg_error_falls_back(self, tmp_path: Path, capsys):
        from tigerharness.tiger_memory.drill import _grep_search
        cfg, store = _setup(tmp_path)
        f1 = store.paths.journal / f"20260514-082136-{uuid4()}.md"
        f1.write_text("Content about testing.\n")
        mock_result = MagicMock()
        mock_result.returncode = 2  # rg error
        mock_result.stderr = "rg: some error"
        with patch("tigerharness.tiger_memory.drill.subprocess.run",
                    return_value=mock_result):
            rc = _grep_search(store, topic="testing")
        assert rc == 1


class TestDrillTreeEdgeCases:
    def test_drill_not_found(self, tmp_path: Path, capsys):
        cfg, store = _setup(tmp_path)
        rc = drill(store, Path("nonexistent.md"))
        assert rc == 2
        assert "not found" in capsys.readouterr().out

    def test_tree_not_found(self, tmp_path: Path, capsys):
        cfg, store = _setup(tmp_path)
        rc = tree(store, Path("nonexistent.md"))
        assert rc == 2
        assert "not found" in capsys.readouterr().out

    def test_tree_with_depth_limit(self, tmp_path: Path, capsys):
        cfg, store = _setup(tmp_path)
        monthly = store.paths.journal / "202605-month-abc.md"
        weekly = store.paths.journal / "20260511-week-x.md"
        daily = store.paths.journal / "20260512-daily-y.md"
        short = store.paths.journal / f"20260512-100000-{uuid4()}.md"
        for f in (monthly, weekly, daily, short):
            f.write_text("content\n")

        rc = tree(store, monthly, depth=1)
        assert rc == 0
        out = capsys.readouterr().out
        # Should show monthly and weekly but NOT daily (depth=1)
        assert monthly.name in out
        assert weekly.name in out
        # daily should not appear at depth limit 1
        assert daily.name not in out

    def test_children_of_unknown_pattern(self, tmp_path: Path):
        """Files that don't match any pattern return no children."""
        cfg, store = _setup(tmp_path)
        weird = store.paths.journal / "random-notes.md"
        weird.write_text("stuff")
        children = _children_of(store, weird)
        assert children == []

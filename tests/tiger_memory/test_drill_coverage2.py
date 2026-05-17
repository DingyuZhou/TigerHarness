"""Coverage-push tests for drill.py — targeting remaining lines:
100 (slack source with JSONL), 120 (_find_claude_jsonl empty uuid),
170-171 (_rag_available success), 199-200 (_grep_hits OSError skip),
223-225 (_hybrid_search RAG import error), 228 (_hybrid_search with RAG paths),
273-274 (_python_grep OSError skip), 321 (_resolve basename search),
344 (_children_of weekly match skip).
"""
from __future__ import annotations

import re
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from tigerharness.tiger_memory import frontmatter
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.drill import (
    _find_claude_jsonl,
    _grep_hits,
    _hybrid_search,
    _python_grep,
    _rag_available,
    _resolve,
    raw,
    search,
)
from tigerharness.tiger_memory.store import Store


def _cfg(tmp_path: Path):
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(
        f"agent: {{name: T, role: T}}\n"
        f"store: {{root: {tmp_path}/memory}}\n"
        f"sources:\n"
        f"  - kind: claude_code\n"
        f"    project_path: {tmp_path}/proj/\n"
        f"summarizer: {{backend: anthropic, model: claude-sonnet-4-6, prompts: default/v1}}\n"
        f"rebuild: {{lock_path: {tmp_path}/lock}}\n"
    )
    return load_config(cfg_path)


class TestRawSlackSource:
    """Line 100: raw() with source=slack finds JSONL."""

    def test_slack_source_with_jsonl(self, tmp_path: Path, capsys):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()

        uid = str(uuid4())
        # Create archive with slack source
        archive = store.paths.archive / f"20260514-{uid}.md"
        archive.write_text(frontmatter.render(
            {"type": "archive", "source": "slack",
             "source_id": "1234.5678@C0123456",
             "conversation_uuid": uid},
            "Slack content.\n"
        ))

        # Create JSONL transcript
        proj = tmp_path / "proj"
        proj.mkdir(parents=True, exist_ok=True)
        (proj / f"{uid}.jsonl").write_text('{"test": true}\n')

        ret = raw(cfg, store, archive)
        assert ret == 0
        out = capsys.readouterr().out
        assert uid in out
        assert "slack.com/archives" in out


class TestRawSlackNoChannel:
    """Line 109: raw() with slack source, no channel in source_id."""

    def test_slack_no_channel(self, tmp_path: Path, capsys):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()

        uid = str(uuid4())
        archive = store.paths.archive / f"20260514-{uid}.md"
        archive.write_text(frontmatter.render(
            {"type": "archive", "source": "slack",
             "source_id": "1234.5678",
             "conversation_uuid": uid},
            "Slack content.\n"
        ))

        ret = raw(cfg, store, archive)
        assert ret == 0
        out = capsys.readouterr().out
        assert "slack_thread_ts: 1234.5678" in out


class TestFindClaudeJsonlEmpty:
    """Line 120: empty session_uuid returns None."""

    def test_empty_uuid(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        assert _find_claude_jsonl(cfg, "") is None


class TestRagAvailableSuccess:
    """Lines 170-171: _rag_available True when sqlite_vec + embedder available."""

    def test_rag_available_true(self):
        mock_embedder = MagicMock()
        with patch.dict("sys.modules", {"sqlite_vec": MagicMock()}), \
             patch("tigerharness.tiger_memory.embedders.pick_embedder",
                   return_value=mock_embedder):
            assert _rag_available() is True


class TestGrepHitsOSError:
    """Lines 199-200: OSError reading file in _grep_hits fallback."""

    def test_oserror_during_grep(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()

        # Create files, one readable and one not
        good = store.paths.journal / "good.md"
        good.write_text("topic matches here")
        bad = store.paths.journal / "bad.md"
        bad.write_text("topic also here")

        orig_read = Path.read_text

        def patched_read(self, *a, **kw):
            if self.name == "bad.md" and "journal" in str(self):
                raise OSError("nope")
            return orig_read(self, *a, **kw)

        # Force python fallback by making rg unavailable
        with patch("subprocess.run", side_effect=FileNotFoundError("no rg")), \
             patch.object(Path, "read_text", patched_read):
            hits = _grep_hits(store, "topic")

        assert any("good.md" in str(h) for h in hits)


class TestHybridSearchRagError:
    """Lines 223-225: _hybrid_search when RAG import fails."""

    def test_rag_import_error(self, tmp_path: Path, capsys):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()

        # Create a file that'll match grep
        (store.paths.journal / "hit.md").write_text("test topic here")

        with patch("subprocess.run", side_effect=FileNotFoundError("no rg")), \
             patch("tigerharness.tiger_memory.drill._grep_hits",
                   return_value=[store.paths.journal / "hit.md"]):
            with patch.dict("sys.modules", {"tigerharness.tiger_memory.rag": None}):
                ret = _hybrid_search(cfg, store, topic="test topic")

        assert ret == 0


class TestHybridSearchWithRag:
    """Line 228: _hybrid_search merges RAG paths with grep paths."""

    def test_with_rag_paths(self, tmp_path: Path, capsys):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()

        f1 = store.paths.journal / "file1.md"
        f1.write_text("content 1")
        f2 = store.paths.journal / "file2.md"
        f2.write_text("content 2")

        with patch("tigerharness.tiger_memory.drill._grep_hits",
                   return_value=[f1]), \
             patch("tigerharness.tiger_memory.rag.query_paths",
                   return_value=[f2]):
            ret = _hybrid_search(cfg, store, topic="test")

        assert ret == 0
        out = capsys.readouterr().out
        assert "file1.md" in out
        assert "file2.md" in out


class TestPythonGrepOSError:
    """Lines 273-274: OSError in _python_grep skips file."""

    def test_oserror_skips(self, tmp_path: Path, capsys):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()

        good = store.paths.journal / "good.md"
        good.write_text("searchable")
        bad = store.paths.journal / "bad.md"
        bad.write_text("searchable")

        orig_read = Path.read_text

        def patched_read(self, *a, **kw):
            if self.name == "bad.md":
                raise OSError("denied")
            return orig_read(self, *a, **kw)

        with patch.object(Path, "read_text", patched_read):
            ret = _python_grep(store, "searchable", max_hits=10)

        assert ret == 0
        out = capsys.readouterr().out
        assert "good.md" in out


class TestResolveBasenameSearch:
    """Line 321: _resolve falls back to basename search in journal/archive."""

    def test_basename_resolve(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()

        f = store.paths.journal / "unique-file.md"
        f.write_text("content")

        result = _resolve(store, Path("unique-file.md"))
        assert result is not None
        assert result.name == "unique-file.md"

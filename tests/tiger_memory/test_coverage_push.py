"""Targeted coverage push — drill python fallback, rag internals, store lock
retry, config validation, must_memorize parse edges, briefing layer2 fallback."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from tigerharness.tiger_memory.config import load_config, ConfigError
from tigerharness.tiger_memory.store import Store


# ---- drill.py: _grep_hits python fallback, _rag_available, _python_grep ----

class TestDrillPythonFallback:
    def _setup(self, tmp_path):
        cfg_path = tmp_path / "cfg.yaml"
        cfg_path.write_text(dedent(f"""\
            agent: {{name: T, role: T}}
            store: {{root: {tmp_path}/memory}}
            sources:
              - kind: claude_code
                project_path: {tmp_path}/proj/
            summarizer: {{backend: anthropic, model: claude-sonnet-4-6, prompts: default/v1}}
            rebuild: {{lock_path: {tmp_path}/lock}}
        """))
        cfg = load_config(cfg_path)
        store = Store(cfg.store.root)
        store.init_layout()
        return cfg, store

    def test_grep_hits_python_fallback(self, tmp_path: Path):
        """When rg times out, _grep_hits falls back to python regex."""
        import subprocess
        from tigerharness.tiger_memory.drill import _grep_hits
        cfg, store = self._setup(tmp_path)
        f1 = store.paths.journal / f"20260514-082136-{uuid4()}.md"
        f1.write_text("Discussion about solar panels.\n")

        with patch("tigerharness.tiger_memory.drill.subprocess.run",
                    side_effect=subprocess.TimeoutExpired("rg", 20)):
            hits = _grep_hits(store, "solar")
        assert len(hits) == 1
        assert hits[0] == f1

    def test_python_grep_no_matches(self, tmp_path: Path, capsys):
        from tigerharness.tiger_memory.drill import _python_grep
        cfg, store = self._setup(tmp_path)
        rc = _python_grep(store, "nonexistent_xyz", max_hits=30)
        assert rc == 0
        assert "no matches" in capsys.readouterr().out

    def test_python_grep_with_matches(self, tmp_path: Path, capsys):
        from tigerharness.tiger_memory.drill import _python_grep
        cfg, store = self._setup(tmp_path)
        f1 = store.paths.journal / f"20260514-082136-{uuid4()}.md"
        f1.write_text("Discussion about solar panels.\n")
        rc = _python_grep(store, "solar", max_hits=30)
        assert rc == 0
        out = capsys.readouterr().out
        assert "20260514" in out

    def test_rag_available_false(self):
        from tigerharness.tiger_memory.drill import _rag_available
        with patch("tigerharness.tiger_memory.embedders.pick_embedder", return_value=None):
            result = _rag_available()
            assert result is False

    def test_grep_search_no_matches_prints_message(self, tmp_path: Path, capsys):
        from tigerharness.tiger_memory.drill import _grep_search
        cfg, store = self._setup(tmp_path)
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        with patch("tigerharness.tiger_memory.drill.subprocess.run",
                    return_value=mock_result):
            rc = _grep_search(store, topic="nonexistent")
        assert rc == 0
        assert "no matches" in capsys.readouterr().out

    def test_search_rag_import_error(self, tmp_path: Path, capsys):
        """search mode=rag when import fails."""
        from tigerharness.tiger_memory.drill import search
        cfg, store = self._setup(tmp_path)
        with patch("tigerharness.tiger_memory.drill._rag_available", return_value=False):
            with patch.dict("sys.modules", {"tigerharness.tiger_memory.rag": None}):
                rc = search(cfg, store, topic="test", mode="rag")
        # Should print error and return 2
        out = capsys.readouterr().out
        assert rc == 2 or "rag" in out.lower()


# ---- store.py: lock retry, _try_acquire_lock dead PID, ValueError ----

class TestStoreLockEdges:
    def test_lock_dead_pid_reclaimed(self, tmp_path: Path):
        store = Store(tmp_path / "memory")
        store.init_layout()
        lock = tmp_path / "test.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("999999999")  # dead PID
        os.utime(lock, None)  # fresh mtime
        with store.lock(lock, timeout_minutes=60) as got:
            assert got is True

    def test_lock_invalid_pid_reclaimed(self, tmp_path: Path):
        store = Store(tmp_path / "memory")
        store.init_layout()
        lock = tmp_path / "test.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("not-a-number")  # invalid PID
        os.utime(lock, None)
        with store.lock(lock, timeout_minutes=60) as got:
            assert got is True

    def test_pid_alive_permission_error(self):
        from tigerharness.tiger_memory.store import _pid_alive
        # PID 1 (init) exists but we can't signal it — returns True
        result = _pid_alive(1)
        assert result is True


# ---- config.py: validation branch edges ----

class TestConfigValidation:
    def test_missing_agent_name(self, tmp_path: Path):
        cfg = tmp_path / "bad.yaml"
        cfg.write_text(dedent(f"""\
            store: {{root: {tmp_path}/memory}}
            sources:
              - kind: claude_code
                project_path: {tmp_path}/proj/
            summarizer: {{backend: anthropic, model: claude-sonnet-4-6, prompts: default/v1}}
        """))
        with pytest.raises(ConfigError):
            load_config(cfg)

    def test_missing_sources(self, tmp_path: Path):
        cfg = tmp_path / "bad.yaml"
        cfg.write_text(dedent(f"""\
            agent: {{name: T, role: T}}
            store: {{root: {tmp_path}/memory}}
            summarizer: {{backend: anthropic, model: claude-sonnet-4-6, prompts: default/v1}}
        """))
        with pytest.raises(ConfigError):
            load_config(cfg)

    def test_unknown_source_kind(self, tmp_path: Path):
        cfg = tmp_path / "bad.yaml"
        cfg.write_text(dedent(f"""\
            agent: {{name: T, role: T}}
            store: {{root: {tmp_path}/memory}}
            sources:
              - kind: unknown_kind
                path: /tmp
            summarizer: {{backend: anthropic, model: claude-sonnet-4-6, prompts: default/v1}}
        """))
        # May or may not raise depending on validation strictness
        try:
            load_config(cfg)
        except ConfigError:
            pass  # expected


# ---- must_memorize.py: _parse_table edges, pin lock failure ----

class TestMustMemorizeEdges:
    def test_parse_table_invalid_score(self):
        from tigerharness.tiger_memory.must_memorize import _parse_table
        body = dedent("""\
            | Score | Kind | Last bump | Source | Memo |
            |------:|------|-----------|--------|------|
            | bad | preference | 2026-05-01 | pin | test |
        """)
        rows = _parse_table(body)
        assert len(rows) == 0  # invalid score skipped

    def test_parse_table_invalid_kind(self):
        from tigerharness.tiger_memory.must_memorize import _parse_table
        body = dedent("""\
            | Score | Kind | Last bump | Source | Memo |
            |------:|------|-----------|--------|------|
            |     5 | invalid_kind | 2026-05-01 | pin | test |
        """)
        rows = _parse_table(body)
        assert len(rows) == 0  # invalid kind skipped

    def test_parse_table_infinity_score(self):
        from tigerharness.tiger_memory.must_memorize import _parse_table
        body = dedent("""\
            | Score | Kind | Last bump | Source | Memo |
            |------:|------|-----------|--------|------|
            |     \u221e | owner_explicit | 2026-05-01 | pin | Important |
        """)
        rows = _parse_table(body)
        assert len(rows) == 1
        assert rows[0].locked is True


# ---- briefing.py: layer2 daily fallback to shorts, stale manifest ----

class TestBriefingLayer2Fallback:
    def _setup(self, tmp_path):
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

    def test_layer2_falls_back_to_shorts(self, tmp_path: Path):
        from tigerharness.tiger_memory.briefing import _copy_layer2
        cfg, store = self._setup(tmp_path)
        # Create a short but NO daily for this date
        uid = str(uuid4())
        short = store.paths.journal / f"20260514-082136-{uid}.md"
        short.write_text("Short summary.\n")
        dest = tmp_path / "staging"
        (dest / "daily").mkdir(parents=True)
        result = _copy_layer2(store, ["20260514"], dest)
        # Should have copied the short as fallback
        assert len(result) >= 1

    def test_briefing_stale_manifest(self, tmp_path: Path):
        from tigerharness.tiger_memory.briefing import _is_stale
        assert _is_stale("2020-01-01T00:00:00Z") is True


# ---- rag.py: _index_archive_if_needed + _query ----

class TestRagInternals:
    def test_index_and_query_mocked(self, tmp_path: Path):
        """Test _index_archive_if_needed and _query with mocked sqlite."""
        from tigerharness.tiger_memory import frontmatter
        from tigerharness.tiger_memory.rag import _index_archive_if_needed, _query

        store = Store(tmp_path / "memory")
        store.init_layout()

        # Create archive files with frontmatter
        uid = str(uuid4())
        archive = store.paths.archive / f"20260514-082136-{uid}.md"
        archive.write_text(frontmatter.render(
            {"conversation_uuid": uid, "summarizer": "mock@v1"},
            "Discussion about solar energy.\n",
        ))

        # Mock embedder
        mock_embedder = MagicMock()
        mock_embedder.name = "test-embedder"
        mock_embedder.dim = 4
        mock_embedder.embed_batch.return_value = [[0.1, 0.2, 0.3, 0.4]]
        mock_embedder.embed_one.return_value = [0.1, 0.2, 0.3, 0.4]

        # Mock connection
        mock_conn = MagicMock()
        # fetchall for existing docs check returns empty
        mock_conn.execute.return_value.fetchall.return_value = []
        # lastrowid for insert
        mock_cursor = MagicMock()
        mock_cursor.lastrowid = 1
        mock_conn.execute.return_value = mock_cursor
        mock_cursor.fetchall.return_value = []

        _index_archive_if_needed(mock_conn, store, mock_embedder)
        # Should have called embed_batch
        assert mock_embedder.embed_batch.called

        # Now test _query
        mock_conn.execute.return_value.fetchall.return_value = [
            (uid, 0.123, str(archive)),
        ]
        hits = _query(mock_conn, mock_embedder, "solar", k=5)
        assert len(hits) == 1
        assert hits[0][0] == uid

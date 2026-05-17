"""Tests for tigerharness.tiger_memory.rag module."""
from __future__ import annotations

import sys
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock, patch

import pytest

from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "memory")
    s.init_layout()
    return s


@pytest.fixture
def cfg(minimal_config_yaml: Path):
    return load_config(str(minimal_config_yaml))


class TestSearchNoSqliteVec:
    """When sqlite-vec isn't importable, search returns 2 with install hint."""

    def test_search_missing_sqlite_vec(self, cfg, store, capsys):
        with patch.dict(sys.modules, {"sqlite_vec": None}):
            # Force ImportError by removing module
            with patch("builtins.__import__", side_effect=_import_raiser("sqlite_vec")):
                from tigerharness.tiger_memory import rag
                # Reload to test the import path
                ret = rag.search(cfg, store, topic="test", k=5)
                assert ret == 2
                out = capsys.readouterr().out
                assert "sqlite-vec" in out


class TestSearchNoEmbedder:
    """When sqlite-vec exists but no embedder is available."""

    def test_search_no_embedder(self, cfg, store, capsys):
        with patch("tigerharness.tiger_memory.rag.pick_embedder", return_value=None):
            # Need sqlite_vec to be importable
            mock_sqlite_vec = MagicMock()
            with patch.dict(sys.modules, {"sqlite_vec": mock_sqlite_vec}):
                from tigerharness.tiger_memory.rag import search
                ret = search(cfg, store, topic="test", k=5)
                assert ret == 2
                out = capsys.readouterr().out
                assert "No embedder available" in out


class TestQueryPaths:
    """query_paths returns [] gracefully on failure."""

    def test_returns_empty_when_no_sqlite_vec(self, cfg, store):
        with patch.dict(sys.modules, {"sqlite_vec": None}):
            with patch("builtins.__import__", side_effect=_import_raiser("sqlite_vec")):
                from tigerharness.tiger_memory.rag import query_paths
                result = query_paths(cfg, store, topic="test", k=5)
                assert result == []

    def test_returns_empty_when_no_embedder(self, cfg, store):
        mock_sqlite_vec = MagicMock()
        with patch.dict(sys.modules, {"sqlite_vec": mock_sqlite_vec}):
            with patch("tigerharness.tiger_memory.rag.pick_embedder", return_value=None):
                from tigerharness.tiger_memory.rag import query_paths
                result = query_paths(cfg, store, topic="test", k=5)
                assert result == []

    def test_returns_empty_on_db_error(self, cfg, store, capsys):
        mock_sqlite_vec = MagicMock()
        mock_embedder = MagicMock()
        mock_embedder.name = "test"
        mock_embedder.dim = 128
        with patch.dict(sys.modules, {"sqlite_vec": mock_sqlite_vec}):
            with patch("tigerharness.tiger_memory.rag.pick_embedder", return_value=mock_embedder):
                with patch("tigerharness.tiger_memory.rag._open_db", side_effect=RuntimeError("DB corrupt")):
                    from tigerharness.tiger_memory.rag import query_paths
                    result = query_paths(cfg, store, topic="test", k=5)
                    assert result == []
                    err = capsys.readouterr().err
                    assert "rag query failed" in err


class TestSearchHappyPath:
    """Test the full search pipeline with mocked DB."""

    def test_search_returns_results(self, cfg, store, capsys):
        mock_sqlite_vec = MagicMock()
        mock_embedder = MagicMock()
        mock_embedder.name = "test-embedder"
        mock_embedder.dim = 4
        mock_embedder.embed_one.return_value = [0.1, 0.2, 0.3, 0.4]

        mock_conn = MagicMock()
        # _index_archive_if_needed is a no-op (no pending files)
        # _query returns some hits
        mock_conn.execute.return_value.fetchall.return_value = [
            ("uuid1", 0.123, str(store.paths.archive / "conv_abc.md")),
            ("uuid2", 0.456, str(store.paths.archive / "conv_xyz.md")),
        ]

        with patch.dict(sys.modules, {"sqlite_vec": mock_sqlite_vec}):
            with patch("tigerharness.tiger_memory.rag.pick_embedder", return_value=mock_embedder):
                with patch("tigerharness.tiger_memory.rag._open_db", return_value=mock_conn):
                    with patch("tigerharness.tiger_memory.rag._index_archive_if_needed"):
                        from tigerharness.tiger_memory.rag import search
                        ret = search(cfg, store, topic="bitcoin", k=10)
                        assert ret == 0
                        out = capsys.readouterr().out
                        assert "0.123" in out

    def test_search_no_matches(self, cfg, store, capsys):
        mock_sqlite_vec = MagicMock()
        mock_embedder = MagicMock()
        mock_embedder.name = "test-embedder"
        mock_embedder.dim = 4
        mock_embedder.embed_one.return_value = [0.1, 0.2, 0.3, 0.4]

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []

        with patch.dict(sys.modules, {"sqlite_vec": mock_sqlite_vec}):
            with patch("tigerharness.tiger_memory.rag.pick_embedder", return_value=mock_embedder):
                with patch("tigerharness.tiger_memory.rag._open_db", return_value=mock_conn):
                    with patch("tigerharness.tiger_memory.rag._index_archive_if_needed"):
                        from tigerharness.tiger_memory.rag import search
                        ret = search(cfg, store, topic="nonexistent", k=10)
                        assert ret == 0
                        out = capsys.readouterr().out
                        assert "no matches" in out


class TestQueryPathsHappyPath:
    def test_returns_paths(self, cfg, store):
        mock_sqlite_vec = MagicMock()
        mock_embedder = MagicMock()
        mock_embedder.name = "test-embedder"
        mock_embedder.dim = 4
        mock_embedder.embed_one.return_value = [0.1, 0.2, 0.3, 0.4]

        p1 = str(store.paths.archive / "conv_abc.md")
        p2 = str(store.paths.archive / "conv_xyz.md")
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            ("uuid1", 0.1, p1),
            ("uuid2", 0.2, p2),
        ]

        with patch.dict(sys.modules, {"sqlite_vec": mock_sqlite_vec}):
            with patch("tigerharness.tiger_memory.rag.pick_embedder", return_value=mock_embedder):
                with patch("tigerharness.tiger_memory.rag._open_db", return_value=mock_conn):
                    with patch("tigerharness.tiger_memory.rag._index_archive_if_needed"):
                        from tigerharness.tiger_memory.rag import query_paths
                        result = query_paths(cfg, store, topic="test", k=5)
                        assert len(result) == 2
                        assert result[0] == Path(p1)


# --- Helpers ---

def _import_raiser(blocked_module: str):
    """Create a side_effect for __import__ that blocks one module."""
    real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

    def _fake_import(name, *args, **kwargs):
        if name == blocked_module:
            raise ImportError(f"No module named '{blocked_module}'")
        return real_import(name, *args, **kwargs)

    return _fake_import

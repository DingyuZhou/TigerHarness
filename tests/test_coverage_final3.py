"""Final coverage push — targets remaining reachable lines.

Targets:
- embedders.py:95-96 (OpenAIEmbedder.embed_batch)
- rag.py:154,157 (_index_archive_if_needed skip paths)
- lifecycle.py:92 (auto_memory record append in bootstrap)
- drill.py:170-171 (_rag_available import failure)
- drill.py:321 (_resolve basename search)
- drill.py:344 (_children_of monthly non-match continue)
- store.py:267-269 (lock stat FileNotFoundError → retry)
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------- embedders.py:95-96 — OpenAIEmbedder.embed_batch ----------------


class TestOpenAIEmbedderEmbedBatch:
    def test_embed_batch_delegates_to_client(self, monkeypatch):
        """Cover lines 95-96: real embed_batch call with mocked OpenAI."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")

        fake_data = [
            SimpleNamespace(embedding=[0.1, 0.2, 0.3]),
            SimpleNamespace(embedding=[0.4, 0.5, 0.6]),
        ]
        fake_resp = SimpleNamespace(data=fake_data)

        mock_client = MagicMock()
        mock_client.embeddings.create.return_value = fake_resp

        mock_openai_cls = MagicMock(return_value=mock_client)

        # Patch the import inside OpenAIEmbedder.__init__
        import importlib
        import tigerharness.tiger_memory.embedders as mod

        fake_openai_mod = SimpleNamespace(OpenAI=mock_openai_cls)
        with patch.dict("sys.modules", {"openai": fake_openai_mod}):
            # Re-import to pick up the patched module
            embedder = mod.OpenAIEmbedder.__new__(mod.OpenAIEmbedder)
            embedder.name = "openai/text-embedding-3-small"
            embedder.model = "text-embedding-3-small"
            embedder._client = mock_client
            embedder.dim = 1536

            result = embedder.embed_batch(["hello", "world"])

        assert result == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
        mock_client.embeddings.create.assert_called_once_with(
            model="text-embedding-3-small", input=["hello", "world"]
        )


# ---------- rag.py:154,157 — _index_archive_if_needed skip paths -----------


class TestRagIndexSkipPaths:
    def test_archive_file_without_uid_skipped(self, tmp_path):
        """Cover rag.py:154 — archive file with no conversation_uuid."""
        from tigerharness.tiger_memory import frontmatter

        store = MagicMock()
        archive = tmp_path / "archive"
        archive.mkdir()
        # Create archive file without conversation_uuid
        f = archive / "no-uid.md"
        f.write_text("---\nsummarizer: mock@v1\n---\nContent here\n")
        store.paths.archive = archive

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []

        mock_embedder = MagicMock()
        mock_embedder.name = "test"

        from tigerharness.tiger_memory.rag import _index_archive_if_needed

        _index_archive_if_needed(mock_conn, store, mock_embedder)
        # No pending items → no embed call
        mock_embedder.embed_batch.assert_not_called()

    def test_archive_file_already_indexed_skipped(self, tmp_path):
        """Cover rag.py:157 — archive file with matching (summarizer, embedder)."""
        store = MagicMock()
        archive = tmp_path / "archive"
        archive.mkdir()
        f = archive / "indexed.md"
        f.write_text(
            "---\nconversation_uuid: abc-123\nsummarizer: mock@v1\n---\nContent\n"
        )
        store.paths.archive = archive

        mock_conn = MagicMock()
        # Return existing row that matches
        mock_conn.execute.return_value.fetchall.return_value = [
            ("abc-123", "mock@v1", "test-embedder")
        ]

        mock_embedder = MagicMock()
        mock_embedder.name = "test-embedder"

        from tigerharness.tiger_memory.rag import _index_archive_if_needed

        _index_archive_if_needed(mock_conn, store, mock_embedder)
        mock_embedder.embed_batch.assert_not_called()


# ---------- drill.py:170-171 — _rag_available import failure ----------------


class TestRagAvailableImportFailure:
    def test_rag_available_false_when_sqlite_vec_missing(self):
        """Cover drill.py:170-171 — sqlite_vec not installed → False."""
        import sys

        with patch.dict(sys.modules, {"sqlite_vec": None}):
            # Force ImportError by removing from sys.modules cache
            saved = sys.modules.pop("sqlite_vec", None)
            try:
                import builtins
                real_import = builtins.__import__

                def fake_import(name, *args, **kwargs):
                    if name == "sqlite_vec":
                        raise ImportError("no sqlite_vec")
                    return real_import(name, *args, **kwargs)

                with patch("builtins.__import__", side_effect=fake_import):
                    from tigerharness.tiger_memory.drill import _rag_available

                    result = _rag_available()
                    assert result is False
            finally:
                if saved is not None:
                    sys.modules["sqlite_vec"] = saved


# ---------- drill.py:321 — _resolve basename search -------------------------


class TestResolveBasenameSearch:
    def test_resolve_basename_fallback(self, tmp_path):
        """Cover drill.py:321 — _resolve falls back to basename rglob."""
        from tigerharness.tiger_memory.store import Store

        store = Store(tmp_path / "store")
        store.init_layout()

        # Create file deep inside journal (only findable via rglob)
        subdir = store.paths.journal / "sub"
        subdir.mkdir()
        target = subdir / "unique-file.md"
        target.write_text("hello")

        from tigerharness.tiger_memory.drill import _resolve

        # Pass a path whose basename exists only deep in journal
        result = _resolve(store, Path("/nonexistent/path/unique-file.md"))
        assert result == target


# ---------- lifecycle.py:92 — auto_memory record append in bootstrap --------


class TestBootstrapAutoMemory:
    def test_auto_memory_appended_when_limit_allows(self, tmp_path):
        """Cover lifecycle.py:92 — am record appended when limit not reached."""
        from tigerharness.tiger_memory.store import Store
        from tigerharness.tiger_memory.summarizers.mock import MockSummarizer

        store = Store(tmp_path / "store")
        store.init_layout()

        mock_summarizer = MockSummarizer()

        fake_record = MagicMock()
        fake_record.conversation_uuid = "auto-mem-001"
        fake_record.first_event_at = "2026-01-01T00:00:00Z"

        # We need _decide to receive the list with the auto-memory record
        captured_records = []

        def spy_decide(records, *a, **kw):
            captured_records.extend(records)
            return []

        from contextlib import contextmanager

        @contextmanager
        def fake_lock(*a, **kw):
            yield True

        store.lock = fake_lock

        with patch("tigerharness.tiger_memory.lifecycle._build_adapters", return_value=[]), \
             patch("tigerharness.tiger_memory.lifecycle._auto_memory_record", return_value=fake_record), \
             patch("tigerharness.tiger_memory.lifecycle._decide", side_effect=spy_decide), \
             patch("tigerharness.tiger_memory.lifecycle._process_decisions", return_value=0.0), \
             patch("tigerharness.tiger_memory.lifecycle._cascade_all_rollups"), \
             patch("tigerharness.tiger_memory.lifecycle._refresh_longer_memory"), \
             patch("tigerharness.tiger_memory.lifecycle._apply_decay"), \
             patch("tigerharness.tiger_memory.lifecycle._write_state"), \
             patch("tigerharness.tiger_memory.briefing.rebuild_briefing"):
            from tigerharness.tiger_memory.lifecycle import bootstrap

            cfg = MagicMock()
            cfg.rebuild.lock_path = str(tmp_path / "lock")
            cfg.rebuild.rebuild_timeout_minutes = 1

            result = bootstrap(cfg, store, limit=10, summarizer_override=mock_summarizer)
            assert result == 0
            # The auto-memory record should be in the captured list
            assert fake_record in captured_records


# ---------- store.py:267-269 — lock stat FileNotFoundError → retry ----------


class TestLockStatRace:
    def test_lock_stat_fnfe_triggers_retry(self, tmp_path):
        """Cover store.py:267-269 — stat() races with release → retry."""
        from tigerharness.tiger_memory.store import Store

        store = Store(tmp_path)
        lock_path = tmp_path / "test.lock"

        call_count = 0

        original_stat = Path.stat

        def fake_stat(self, *args, **kwargs):
            nonlocal call_count
            if self == lock_path and call_count == 0:
                call_count += 1
                raise FileNotFoundError("raced with release")
            return original_stat(self, *args, **kwargs)

        # First call: lock file exists → stat raises FNFE → retry
        # Second call: lock file doesn't exist → acquire succeeds
        lock_path.write_text("99999999")  # fake PID

        with patch.object(Path, "stat", fake_stat):
            # The retry should work because after FNFE, it tries to
            # acquire again and the lock file is still there but
            # now stat works
            lock_path.write_text("99999999")
            got = store._try_acquire_lock(lock_path, timeout_minutes=1)
            # Should have retried
            assert call_count == 1


# ---------- lifecycle.py:206 — DocsAdapter skip in resummarize ---------------


class TestResummarizeDocsAdapterSkip:
    def test_docs_adapter_skipped_in_resummarize(self, tmp_path):
        """Cover lifecycle.py:206 — DocsAdapter instances skipped during resummarize."""
        from tigerharness.tiger_memory.store import Store
        from tigerharness.tiger_memory.sources.docs import DocsAdapter

        store = Store(tmp_path / "store")
        store.init_layout()

        from contextlib import contextmanager

        @contextmanager
        def fake_lock(*a, **kw):
            yield True

        store.lock = fake_lock

        # Create a DocsAdapter that would blow up if discover() is called
        docs_adapter = MagicMock(spec=DocsAdapter)
        docs_adapter.discover.side_effect = RuntimeError("should not be called")

        with patch("tigerharness.tiger_memory.lifecycle._build_adapters", return_value=[docs_adapter]), \
             patch("tigerharness.tiger_memory.lifecycle._build_summarizer"), \
             patch("tigerharness.tiger_memory.lifecycle._process_decisions", return_value=0.0), \
             patch("tigerharness.tiger_memory.lifecycle._cascade_all_rollups"), \
             patch("tigerharness.tiger_memory.lifecycle._refresh_longer_memory"), \
             patch("tigerharness.tiger_memory.lifecycle._apply_decay"), \
             patch("tigerharness.tiger_memory.lifecycle._write_state"), \
             patch("tigerharness.tiger_memory.briefing.rebuild_briefing"):
            from tigerharness.tiger_memory.lifecycle import resummarize

            cfg = MagicMock()
            cfg.rebuild.lock_path = str(tmp_path / "lock")
            cfg.rebuild.rebuild_timeout_minutes = 1

            result = resummarize(cfg, store, since="2026-01-01")
            assert result == 0
            # DocsAdapter.discover() should NOT have been called
            docs_adapter.discover.assert_not_called()


# ---------- lifecycle.py:368 — mm.append_dropped when demoted ----------------


class TestProcessDecisionsDemoted:
    def test_append_dropped_called_when_rows_exceed_max(self, tmp_path):
        """Cover lifecycle.py:368 — demoted rows trigger append_dropped."""
        from tigerharness.tiger_memory.store import Store
        from tigerharness.tiger_memory.summarizers.mock import MockSummarizer
        from tigerharness.tiger_memory.lifecycle import (
            _process_decisions, Decision, SUMMARIZE_NEW,
        )
        from tigerharness.tiger_memory.must_memorize import Row

        store = Store(tmp_path / "store")
        store.init_layout()

        # Pre-populate must_memorize with max_rows=1 worth of rows
        mm_path = store.paths.journal / "must_memorize.md"
        mm_path.write_text(
            "---\n---\n"
            "| Score | Kind | Last bump | Last decay | Source | Memo |\n"
            "|------:|------|-----------|------------|--------|------|\n"
            "| 10 | preference | 2026-01-01 | 2026-01-01 | extract | existing memo |\n"
        )

        summarizer = MockSummarizer()

        # Create a fake decision
        fake_record = MagicMock()
        fake_record.conversation_uuid = "test-uuid-001"
        fake_record.content = "some content"

        decision = Decision(record=fake_record, action=SUMMARIZE_NEW)

        # Mock _write_short_and_archive to be a no-op
        # Mock _extract_must_memorize to return a candidate that will overflow
        new_candidate = Row(
            kind="preference", memo="new memo", score=5,
            last_bump="2026-01-15", last_decay="2026-01-15",
            source="extract", locked=False,
        )

        cfg = MagicMock()
        cfg.budgets.repeat_detection_similarity = 0.9
        cfg.budgets.must_memorize_rows = 1  # only 1 allowed → demotes the lower-scored one
        # This test feeds a MagicMock record (not a real SourceRecord), so
        # keep the pre-filter off — it runs dataclasses.replace() on the
        # record, which only works on a real dataclass. Prefilter behavior
        # is covered separately in test_lifecycle_prefilter.py.
        cfg.prefilter.enabled = False

        with patch("tigerharness.tiger_memory.lifecycle._write_short_and_archive"), \
             patch("tigerharness.tiger_memory.lifecycle._extract_must_memorize", return_value=[new_candidate]), \
             patch("tigerharness.tiger_memory.lifecycle._approx_cost", return_value=0.001), \
             patch("tigerharness.tiger_memory.must_memorize.append_dropped") as mock_append:
            _process_decisions([decision], store, cfg, summarizer)
            mock_append.assert_called_once()
            # The demoted row should be the lower-scored one (score=5)
            demoted = mock_append.call_args[0][1]
            assert len(demoted) == 1
            assert demoted[0].score == 5

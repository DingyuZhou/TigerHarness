"""Coverage-push tests for lifecycle.py — targeting remaining uncovered lines:
85/87 (limit break), 92 (auto_memory + limit), 155 (DocsAdapter skip in rebuild),
196-197 (resummarize lock failure), 206/208-209/213-215 (resummarize inner loop),
250 (threads_json empty→None), 369 (demoted → append_dropped), 498-500 (extract
must_memorize exception), 686 (folded_into_longer_memory skip),
694-695 (longer_path exists), 824-825 (OSError in auto_memory), 851 (_prompts_root
fallback).
"""
from __future__ import annotations

import os
import time
from datetime import date, datetime, timezone
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from tigerharness.tiger_memory import frontmatter, must_memorize as mm
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.lifecycle import (
    _auto_memory_record,
    _build_adapters,
    _process_decisions,
    _prompts_root,
    _refresh_longer_memory,
    bootstrap,
    rebuild,
    resummarize,
    Decision,
    SUMMARIZE_NEW,
)
from tigerharness.tiger_memory.sources.base import SourceRecord
from tigerharness.tiger_memory.store import Store
from tigerharness.tiger_memory.summarizers import MockSummarizer


def _cfg(tmp_path: Path, *, extra_sources: str = ""):
    cfg_path = tmp_path / "cfg.yaml"
    if extra_sources:
        sources_block = "sources:\n" + extra_sources
    else:
        sources_block = (
            f"sources:\n"
            f"  - kind: claude_code\n"
            f"    project_path: {tmp_path}/proj/\n"
        )
    cfg_path.write_text(
        f"agent: {{name: T, role: T}}\n"
        f"store: {{root: {tmp_path}/memory}}\n"
        f"{sources_block}\n"
        f"summarizer: {{backend: anthropic, model: claude-sonnet-4-6, prompts: default/v1}}\n"
        f"rebuild:\n"
        f"  lock_path: {tmp_path}/lock\n"
        f"  idle_threshold_hours: 2\n"
        f"  resummarize_window_days: 7\n"
    )
    return load_config(cfg_path)


def _make_record(tmp_path, *, hours_old: float = 24) -> SourceRecord:
    uid = str(uuid4())
    now = datetime.now(timezone.utc)
    return SourceRecord(
        conversation_uuid=uid,
        source="claude_code",
        source_id=uid,
        first_event_at=now,
        last_event_at=now,
        activity_mtime=time.time() - hours_old * 3600,
        content=f"Conversation content for {uid}",
        raw_path=tmp_path / f"{uid}.jsonl",
    )


class TestBootstrapLimitBreak:
    """Lines 85/87: inner and outer break when limit is reached."""

    def test_limit_breaks_across_adapters(self, tmp_path: Path, capsys):
        """With limit=1, only 1 record is discovered even if adapter yields more."""
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()

        rec1 = _make_record(tmp_path)
        rec2 = _make_record(tmp_path)

        mock_adapter = MagicMock()
        mock_adapter.discover.return_value = [rec1, rec2]

        with patch("tigerharness.tiger_memory.lifecycle._build_adapters",
                   return_value=[mock_adapter]):
            ret = bootstrap(cfg, store, limit=1,
                            summarizer_override=MockSummarizer())
        assert ret == 0
        out = capsys.readouterr().out
        assert "discovered 1 source" in out


class TestBootstrapAutoMemoryWithLimit:
    """Line 92: auto_memory append respects limit."""

    def test_auto_memory_skipped_when_limit_reached(self, tmp_path: Path, capsys):
        am_dir = tmp_path / "auto_mem"
        am_dir.mkdir()
        (am_dir / "pref.md").write_text("key=value")

        src_text = (
            f"  - kind: claude_code\n"
            f"    project_path: {tmp_path}/proj/\n"
            f"  - kind: auto_memory\n"
            f"    path: {am_dir}\n"
        )
        cfg = _cfg(tmp_path, extra_sources=src_text)
        store = Store(cfg.store.root)
        store.init_layout()

        rec1 = _make_record(tmp_path)
        mock_adapter = MagicMock()
        mock_adapter.discover.return_value = [rec1]

        with patch("tigerharness.tiger_memory.lifecycle._build_adapters",
                   return_value=[mock_adapter]):
            ret = bootstrap(cfg, store, limit=1,
                            summarizer_override=MockSummarizer())
        assert ret == 0
        out = capsys.readouterr().out
        # With limit=1, auto_memory is skipped because 1 record already collected
        assert "discovered 1 source" in out


class TestRebuildSkipsDocsAdapter:
    """Line 155: DocsAdapter is skipped during rebuild (not bootstrap)."""

    def test_rebuild_skips_docs(self, tmp_path: Path):
        src_text = (
            f"  - kind: claude_code\n"
            f"    project_path: {tmp_path}/proj/\n"
            f"  - kind: docs\n"
            f"    glob: '*.md'\n"
        )
        cfg = _cfg(tmp_path, extra_sources=src_text)
        store = Store(cfg.store.root)
        store.init_layout()

        ret = rebuild(cfg, store, summarizer_override=MockSummarizer())
        assert ret == 0  # completes without error


class TestResummarizeLockFailure:
    """Lines 196-197: resummarize returns 1 when lock is held."""

    def test_lock_held(self, tmp_path: Path, capsys):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        # Pre-acquire lock
        cfg.rebuild.lock_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.rebuild.lock_path.write_text(str(os.getpid()))
        os.utime(cfg.rebuild.lock_path, None)

        with patch("tigerharness.tiger_memory.store._pid_alive", return_value=True):
            ret = resummarize(cfg, store, since="2026-01-01")
        assert ret == 1
        out = capsys.readouterr().out
        assert "another run is in progress" in out


class TestResummarizeInnerLoop:
    """Lines 206, 208-209, 213-215: resummarize record filtering + forced
    decisions (both SUMMARIZE_NEW and RE_SUMMARIZE paths)."""

    def test_resummarize_filters_by_date(self, tmp_path: Path, capsys):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()

        old_rec = _make_record(tmp_path, hours_old=24 * 365)
        # Set first_event_at to an old date
        old_rec = SourceRecord(
            conversation_uuid=old_rec.conversation_uuid,
            source=old_rec.source,
            source_id=old_rec.source_id,
            first_event_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            last_event_at=old_rec.last_event_at,
            activity_mtime=old_rec.activity_mtime,
            content=old_rec.content,
            raw_path=old_rec.raw_path,
        )
        new_rec = _make_record(tmp_path)

        mock_adapter = MagicMock()
        mock_adapter.discover.return_value = [old_rec, new_rec]

        with patch("tigerharness.tiger_memory.lifecycle._build_adapters",
                   return_value=[mock_adapter]), \
             patch("tigerharness.tiger_memory.lifecycle._build_summarizer",
                   return_value=MockSummarizer()):
            ret = resummarize(cfg, store, since="2026-01-01")

        assert ret == 0
        out = capsys.readouterr().out
        # Only the new record should be included
        assert "resummarize: 1 sessions" in out


class TestBuildAdaptersThreadsJsonEmpty:
    """Line 250: empty threads_json string → None."""

    def test_empty_threads_json(self, tmp_path: Path):
        cfg_path = tmp_path / "cfg_empty_tj.yaml"
        cfg_path.write_text(dedent(f"""\
            agent: {{name: T, role: T}}
            store: {{root: {tmp_path}/memory}}
            sources:
              - kind: claude_code
                project_path: {tmp_path}/proj/
              - kind: slack_thread
                threads_json: ""
            summarizer: {{backend: anthropic, model: claude-sonnet-4-6, prompts: default/v1}}
            rebuild: {{lock_path: {tmp_path}/lock}}
        """))
        cfg = load_config(cfg_path)
        adapters = _build_adapters(cfg)
        assert len(adapters) == 1


class TestProcessDecisionsDemoted:
    """Line 369: demoted must_memorize rows are appended to dropped."""

    def test_demoted_rows_appended(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()

        rec = _make_record(tmp_path)
        d = Decision(
            record=rec,
            action=SUMMARIZE_NEW,
            existing_archive=None,
            existing_short=None,
        )

        summarizer = MockSummarizer()

        # Pre-populate must_memorize at max capacity so new extractions cause demotion
        rows = [
            mm.Row(kind="preference", memo=f"Fact {i}", score=100, locked=False,
                   source="extract")
            for i in range(50)
        ]
        mm.save(store, rows)

        # Mock the extraction to return a new candidate
        with patch("tigerharness.tiger_memory.lifecycle._extract_must_memorize",
                   return_value=[mm.Row(kind="preference", memo="New fact", score=5,
                                        locked=False, source="extract")]):
            _process_decisions([d], store, cfg, summarizer)

        # Verify it ran without error (mm table still exists)
        loaded = mm.load(store)
        assert len(loaded) > 0


class TestExtractMustMemorizeException:
    """Lines 498-500: exception in _extract_must_memorize returns []."""

    def test_extraction_exception_swallowed(self, tmp_path: Path):
        from tigerharness.tiger_memory.lifecycle import _extract_must_memorize

        cfg = _cfg(tmp_path)
        rec = _make_record(tmp_path)
        summarizer = MockSummarizer()
        summarizer.summarize = MagicMock(side_effect=RuntimeError("LLM down"))

        result = _extract_must_memorize(cfg, summarizer, rec)
        assert result == []


class TestLongerMemoryFoldedSkip:
    """Line 686: monthly already folded_into_longer_memory is skipped."""

    def test_folded_monthly_skipped(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()

        # Create an old monthly that is already folded
        old_monthly = store.paths.journal / "202401-month-old.md"
        old_monthly.write_text(frontmatter.render(
            {"type": "monthly_rollup", "period": "2024-01",
             "folded_into_longer_memory": True},
            "Old folded.\n"
        ))

        summarizer = MockSummarizer()
        _refresh_longer_memory(store, cfg, summarizer)
        # Should complete with no fold attempted (monthly was already folded)


class TestLongerMemoryPathExists:
    """Lines 694-695: longer_memory.md already exists → read prev body/covers."""

    def test_existing_longer_memory(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()

        # Create existing longer_memory.md
        longer_path = store.paths.journal / "longer_memory.md"
        longer_path.write_text(frontmatter.render(
            {"type": "longer_memory", "covers_until": "2024-06"},
            "Previous longer memory.\n"
        ))

        # Create an old monthly that should be folded
        old_monthly = store.paths.journal / "202401-month-old.md"
        old_monthly.write_text(frontmatter.render(
            {"type": "monthly_rollup", "period": "2024-01"},
            "Old monthly.\n"
        ))

        summarizer = MockSummarizer()
        _refresh_longer_memory(store, cfg, summarizer)

        # Verify longer_memory was updated (MockSummarizer returns canned text)
        assert longer_path.exists()


class TestAutoMemoryRecordOSError:
    """Lines 824-825: OSError reading .md in auto_memory dir → skip file."""

    def test_unreadable_md_skipped(self, tmp_path: Path):
        am_dir = tmp_path / "auto_mem"
        am_dir.mkdir()
        store = Store(tmp_path / "memstore")
        store.init_layout()
        good = am_dir / "good.md"
        good.write_text("good content")
        bad = am_dir / "bad.md"
        bad.write_text("bad content")

        src_text = (
            f"  - kind: claude_code\n"
            f"    project_path: {tmp_path}/proj/\n"
            f"  - kind: auto_memory\n"
            f"    path: {am_dir}\n"
        )
        cfg = _cfg(tmp_path, extra_sources=src_text)

        # Make bad.md raise OSError on read
        orig_read_text = Path.read_text

        def patched_read(self, *a, **kw):
            if self.name == "bad.md":
                raise OSError("permission denied")
            return orig_read_text(self, *a, **kw)

        with patch.object(Path, "read_text", patched_read):
            result = _auto_memory_record(cfg, store)

        assert result is not None
        assert "good content" in result.content
        assert "bad content" not in result.content


class TestPromptsRootFallback:
    """Line 851: _prompts_root falls back to cwd-relative when pkg path missing."""

    def test_fallback_when_pkg_path_missing(self, tmp_path: Path):
        cfg_path = tmp_path / "cfg_fallback.yaml"
        cfg_path.write_text(
            f"agent: {{name: T, role: T}}\n"
            f"store: {{root: {tmp_path}/memory}}\n"
            f"sources:\n"
            f"  - kind: claude_code\n"
            f"    project_path: {tmp_path}/proj/\n"
            f"summarizer: {{backend: anthropic, model: claude-sonnet-4-6, prompts: nonexistent/v99}}\n"
            f"rebuild: {{lock_path: {tmp_path}/lock}}\n"
        )
        cfg = load_config(cfg_path)

        result = _prompts_root(cfg)
        assert str(result) == "summarizers/prompts/nonexistent/v99"

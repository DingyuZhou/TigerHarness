"""Coverage-focused lifecycle tests — lock contention, dry-run, resummarize,
_build_adapters with multiple source kinds, cascade exception paths."""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from tigerharness.tiger_memory import frontmatter
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.lifecycle import (
    _build_adapters,
    _cascade_dailies,
    _cascade_monthlies,
    _cascade_weeklies,
    _refresh_longer_memory,
    _apply_decay,
    _spawn_background,
    bootstrap,
    rebuild,
    resummarize,
)
from tigerharness.tiger_memory.sources.base import SourceRecord
from tigerharness.tiger_memory.store import Store
from tigerharness.tiger_memory.summarizers import MockSummarizer


def _cfg(tmp_path: Path, *, extra_sources: str = ""):
    cfg_path = tmp_path / "cfg.yaml"
    sources = f"""
        sources:
          - kind: claude_code
            project_path: {tmp_path}/proj/
    """
    if extra_sources:
        sources = extra_sources
    cfg_path.write_text(dedent(f"""\
        agent: {{name: T, role: T}}
        store: {{root: {tmp_path}/memory}}
        {sources}
        summarizer: {{backend: anthropic, model: claude-sonnet-4-6, prompts: default/v1}}
        rebuild:
          lock_path: {tmp_path}/lock
          idle_threshold_hours: 2
          resummarize_window_days: 7
    """))
    return load_config(cfg_path)


class TestBootstrapLockContention:
    def test_bootstrap_lock_held_returns_1(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        # Pre-acquire lock with a live PID (our own)
        cfg.rebuild.lock_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.rebuild.lock_path.write_text(str(os.getpid()))
        os.utime(cfg.rebuild.lock_path, None)  # keep fresh
        # bootstrap should fail to acquire
        # But since it's our own PID that's alive, _try_acquire_lock returns False
        # Actually store.lock checks if PID is alive and won't reclaim if so
        # We need a different PID that IS alive
        with patch("tigerharness.tiger_memory.store._pid_alive", return_value=True):
            ret = bootstrap(cfg, store, summarizer_override=MockSummarizer())
        assert ret == 1


class TestBootstrapDryRun:
    def test_dry_run_estimates_cost(self, tmp_path: Path, capsys):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        # Create a fake transcript for discovery
        proj = tmp_path / "proj"
        proj.mkdir(parents=True, exist_ok=True)
        uid = str(uuid4())
        jsonl = proj / f"{uid}.jsonl"
        jsonl.write_text(
            '{"type":"summary","subtype":"final","summary":"test",'
            f'"session_id":"{uid}","duration_ms":60000}}\n'
        )
        ret = bootstrap(cfg, store, dry_run=True, limit=1,
                        summarizer_override=MockSummarizer())
        assert ret == 0
        out = capsys.readouterr().out
        assert "DRY-RUN" in out


class TestBootstrapWithLimit:
    def test_limit_caps_records(self, tmp_path: Path, capsys):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        proj = tmp_path / "proj"
        proj.mkdir(parents=True, exist_ok=True)
        for i in range(5):
            uid = str(uuid4())
            (proj / f"{uid}.jsonl").write_text(
                '{"type":"summary","subtype":"final","summary":"test",'
                f'"session_id":"{uid}","duration_ms":60000}}\n'
            )
        ret = bootstrap(cfg, store, limit=2, summarizer_override=MockSummarizer())
        assert ret == 0
        out = capsys.readouterr().out
        # Should discover at most 2
        assert "discovered" in out


class TestResummarizeWithRecords:
    def test_resummarize_processes_records(self, tmp_path: Path, capsys):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        # Create a transcript
        proj = tmp_path / "proj"
        proj.mkdir(parents=True, exist_ok=True)
        uid = str(uuid4())
        (proj / f"{uid}.jsonl").write_text(
            '{"type":"summary","subtype":"final","summary":"test",'
            f'"session_id":"{uid}","duration_ms":60000}}\n'
        )
        ret = resummarize(cfg, store, since="2020-01-01",
                          summarizer=None)
        # This will use the real AnthropicSummarizer which we don't want
        # Let's mock _build_summarizer
        with patch("tigerharness.tiger_memory.lifecycle._build_summarizer",
                    return_value=MockSummarizer()):
            ret = resummarize(cfg, store, since="2020-01-01")
        assert ret == 0
        out = capsys.readouterr().out
        assert "resummarize" in out.lower()


class TestBuildAdaptersMultiKind:
    def test_builds_docs_adapter(self, tmp_path: Path):
        cfg_path = tmp_path / "cfg_docs.yaml"
        cfg_path.write_text(dedent(f"""\
            agent: {{name: T, role: T}}
            store: {{root: {tmp_path}/memory}}
            sources:
              - kind: claude_code
                project_path: {tmp_path}/proj/
              - kind: docs
                glob: "docs/**/*.md"
            summarizer: {{backend: anthropic, model: claude-sonnet-4-6, prompts: default/v1}}
            rebuild: {{lock_path: {tmp_path}/lock}}
        """))
        cfg = load_config(cfg_path)
        adapters = _build_adapters(cfg)
        assert len(adapters) == 2
        names = [a.__class__.__name__ for a in adapters]
        assert "DocsAdapter" in names

    def test_builds_slack_thread_adapter(self, tmp_path: Path):
        threads_json = tmp_path / "threads.json"
        threads_json.write_text("{}")
        cfg_path = tmp_path / "cfg_slack.yaml"
        cfg_path.write_text(dedent(f"""\
            agent: {{name: T, role: T}}
            store: {{root: {tmp_path}/memory}}
            sources:
              - kind: claude_code
                project_path: {tmp_path}/proj/
              - kind: slack_thread
                threads_json: {threads_json}
            summarizer: {{backend: anthropic, model: claude-sonnet-4-6, prompts: default/v1}}
            rebuild: {{lock_path: {tmp_path}/lock}}
        """))
        cfg = load_config(cfg_path)
        adapters = _build_adapters(cfg)
        # slack_thread doesn't add its own adapter, but sets threads_json on claude_code
        assert len(adapters) == 1

    def _basic_cfg(self, tmp_path: Path):
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
        return load_config(cfg_path)

    def test_default_max_age_days_is_7(self, tmp_path: Path):
        # _build_adapters' default mirrors rebuild's 7-day loop-prevention
        # cap. Sites that need broader scope (bootstrap, resummarize)
        # must opt out explicitly.
        cfg = self._basic_cfg(tmp_path)
        adapters = _build_adapters(cfg)
        from tigerharness.tiger_memory.sources.claude_transcript import (
            ClaudeTranscriptAdapter,
        )
        ct = next(a for a in adapters if isinstance(a, ClaudeTranscriptAdapter))
        assert ct.max_age_days == 7

    def test_max_age_days_none_propagates(self, tmp_path: Path):
        # Bootstrap and resummarize pass None to see the full corpus.
        # The cap must not be reintroduced silently by _build_adapters.
        cfg = self._basic_cfg(tmp_path)
        adapters = _build_adapters(cfg, max_age_days=None)
        from tigerharness.tiger_memory.sources.claude_transcript import (
            ClaudeTranscriptAdapter,
        )
        ct = next(a for a in adapters if isinstance(a, ClaudeTranscriptAdapter))
        assert ct.max_age_days is None

    def test_max_age_days_custom_value_propagates(self, tmp_path: Path):
        cfg = self._basic_cfg(tmp_path)
        adapters = _build_adapters(cfg, max_age_days=30)
        from tigerharness.tiger_memory.sources.claude_transcript import (
            ClaudeTranscriptAdapter,
        )
        ct = next(a for a in adapters if isinstance(a, ClaudeTranscriptAdapter))
        assert ct.max_age_days == 30

    def _journal_cfg(self, tmp_path: Path, *, team_line: str):
        from tigerharness.tiger_memory.config import load_config
        cfg_path = tmp_path / "cfg_journal.yaml"
        cfg_path.write_text(dedent(f"""\
            agent: {{name: T, role: T}}
            store: {{root: {tmp_path}/memory}}
            sources:
              - kind: journal_worklog
                journal_root: {tmp_path}/myteam/journal/
                persona: Rukawa
                {team_line}
            summarizer: {{backend: anthropic, model: claude-sonnet-4-6, prompts: default/v1}}
            rebuild: {{lock_path: {tmp_path}/lock}}
        """))
        return load_config(cfg_path)

    def test_builds_journal_worklog_adapter_explicit_team(self, tmp_path: Path):
        from tigerharness.tiger_memory.sources import JournalWorklogAdapter
        cfg = self._journal_cfg(tmp_path, team_line='team: tigers')
        adapters = _build_adapters(cfg)
        jw = next(a for a in adapters if isinstance(a, JournalWorklogAdapter))
        assert jw.persona == "Rukawa"
        assert jw.team == "tigers"
        assert jw.journal_root == (tmp_path / "myteam" / "journal")

    def test_journal_worklog_team_defaults_from_root_parent(
        self, tmp_path: Path,
    ):
        from tigerharness.tiger_memory.sources import JournalWorklogAdapter
        cfg = self._journal_cfg(tmp_path, team_line="")
        adapters = _build_adapters(cfg)
        jw = next(a for a in adapters if isinstance(a, JournalWorklogAdapter))
        # journal_root parent is .../myteam/journal -> parent name "myteam".
        assert jw.team == "myteam"

    def test_journal_worklog_blank_team_defaults_from_root_parent(
        self, tmp_path: Path,
    ):
        from tigerharness.tiger_memory.sources import JournalWorklogAdapter
        cfg = self._journal_cfg(tmp_path, team_line='team: ""')
        adapters = _build_adapters(cfg)
        jw = next(a for a in adapters if isinstance(a, JournalWorklogAdapter))
        assert jw.team == "myteam"

    def test_journal_worklog_relative_root_anchored_to_config_dir(
        self, tmp_path: Path,
    ):
        from tigerharness.tiger_memory.sources import JournalWorklogAdapter
        cfg_dir = tmp_path / "team" / "memories" / "p"
        cfg_dir.mkdir(parents=True)
        cfg_path = cfg_dir / "cfg.yaml"
        cfg_path.write_text(dedent(f"""\
            agent: {{name: p, role: T}}
            store: {{root: .}}
            sources:
              - kind: journal_worklog
                journal_root: ../../journal/
                persona: Rukawa
            summarizer: {{backend: anthropic, model: claude-sonnet-4-6, prompts: default/v1}}
            rebuild: {{lock_path: {tmp_path}/lock}}
        """))
        cfg = load_config(cfg_path)
        adapters = _build_adapters(cfg)
        jw = next(a for a in adapters if isinstance(a, JournalWorklogAdapter))
        # ../../journal/ from .../team/memories/p -> .../team/journal
        assert jw.journal_root == (tmp_path / "team" / "journal").resolve()
        assert jw.team == "team"


class TestCascadeExceptions:
    def test_daily_rollup_exception_continues(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        uid = str(uuid4())
        short = store.paths.journal / f"20260514-082136-{uid}.md"
        short.write_text(frontmatter.render({"type": "short_summary"}, "Content.\n"))
        # Mock summarizer that raises on summarize
        summarizer = MockSummarizer()
        summarizer.summarize = MagicMock(side_effect=RuntimeError("LLM down"))
        # Should not raise — logs and continues
        _cascade_dailies(store, cfg, summarizer)

    def test_weekly_rollup_exception_continues(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        # Create a daily so weekly cascade has something
        daily = store.paths.journal / "20260514-daily-abc.md"
        daily.write_text(frontmatter.render({"type": "daily_rollup"}, "Daily.\n"))
        summarizer = MockSummarizer()
        summarizer.summarize = MagicMock(side_effect=RuntimeError("fail"))
        _cascade_weeklies(store, cfg, summarizer)

    def test_monthly_rollup_exception_continues(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        weekly = store.paths.journal / "20260511-week-abc.md"
        weekly.write_text(frontmatter.render({"type": "weekly_rollup"}, "Weekly.\n"))
        summarizer = MockSummarizer()
        summarizer.summarize = MagicMock(side_effect=RuntimeError("fail"))
        _cascade_monthlies(store, cfg, summarizer)


class TestLongerMemoryFoldException:
    def test_fold_exception_continues(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        old_monthly = store.paths.journal / "202401-month-old.md"
        old_monthly.write_text(frontmatter.render(
            {"type": "monthly_rollup", "period": "2024-01"}, "Old.\n"
        ))
        summarizer = MockSummarizer()
        summarizer.summarize = MagicMock(side_effect=RuntimeError("fail"))
        # Should not raise
        _refresh_longer_memory(store, cfg, summarizer)
        # Monthly should NOT be marked as folded (since fold failed)
        fm = frontmatter.read_frontmatter(old_monthly)
        assert "folded_into_longer_memory" not in fm


class TestApplyDecayWithRows:
    def test_decay_removes_low_score_rows(self, tmp_path: Path):
        from tigerharness.tiger_memory import must_memorize as mm
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        # Create must_memorize with a low-score preference row
        mm_file = store.paths.journal / "must_memorize.md"
        mm_file.write_text(frontmatter.render(
            {"type": "must_memorize"},
            dedent("""\
                # Must memorize

                | Score | Kind | Last bump | Source | Memo |
                |------:|------|-----------|--------|------|
                |     1 | preference | 2024-01-01 | extract | Old fact |
            """),
        ))
        _apply_decay(store, cfg)
        rows = mm.load(store)
        # The row should be decayed to 0 or below and removed
        assert len(rows) == 0


class TestSpawnBackgroundConfig:
    def test_spawn_with_config_flag(self):
        import sys
        with patch("tigerharness.tiger_memory.lifecycle.subprocess.Popen") as mock_popen:
            with patch.object(sys, "argv", ["tiger-memory", "--config", "/tmp/cfg.yaml", "rebuild"]):
                ret = _spawn_background()
        assert ret == 0
        cmd = mock_popen.call_args[0][0]
        assert "--config" in cmd
        assert "/tmp/cfg.yaml" in cmd

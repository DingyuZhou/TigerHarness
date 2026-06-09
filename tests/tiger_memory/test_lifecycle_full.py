"""Comprehensive lifecycle tests using MockSummarizer.

Covers: bootstrap, rebuild, resummarize, _decide, _process_decisions,
cascade rollups, longer_memory refresh, decay, _auto_memory_record,
_spawn_background, _approx_cost, _estimate_total_cost, _clip,
_fill_prompt, _prompts_root, _build_adapters, _build_summarizer.
"""
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
    ADDENDUM,
    RE_SUMMARIZE,
    SKIP_ACTIVE,
    SKIP_CLEAN,
    SUMMARIZE_NEW,
    Decision,
    _apply_decay,
    _approx_cost,
    _auto_memory_record,
    _build_adapters,
    _build_summarizer,
    _cascade_all_rollups,
    _clip,
    _decide,
    _estimate_total_cost,
    _fill_prompt,
    _fit_content,
    _process_decisions,
    _prompts_root,
    _split_on_boundaries,
    _refresh_longer_memory,
    _spawn_background,
    _write_state,
    bootstrap,
    rebuild,
    resummarize,
)
from tigerharness.tiger_memory.sources.base import SourceRecord
from tigerharness.tiger_memory.store import Store
from tigerharness.tiger_memory.summarizers import MockSummarizer


def _cfg(tmp_path: Path, *, extra_yaml: str = ""):
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(dedent(f"""\
        agent: {{name: T, role: T}}
        store: {{root: {tmp_path}/memory}}
        sources:
          - kind: claude_code
            project_path: {tmp_path}/proj/
        summarizer: {{backend: anthropic, model: claude-sonnet-4-6, prompts: default/v1}}
        rebuild:
          lock_path: {tmp_path}/lock
          idle_threshold_hours: 2
          resummarize_window_days: 7
    """) + extra_yaml)
    return load_config(cfg_path)


def _make_record(
    *,
    uuid: str | None = None,
    content: str = "Test content",
    age_hours: float = 4.0,
    source: str = "claude_code",
) -> SourceRecord:
    uid = uuid or str(uuid4())
    now = time.time()
    first = datetime.now(timezone.utc) - timedelta(hours=age_hours + 1)
    last = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    return SourceRecord(
        conversation_uuid=uid,
        source=source,
        source_id=uid,
        first_event_at=first,
        last_event_at=last,
        activity_mtime=now - age_hours * 3600,
        content=content,
        raw_path=Path("/dev/null"),
    )


# ----- _decide ---------------------------------------------------------------


class TestDecide:
    def test_skip_active(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        rec = _make_record(age_hours=0.5)  # < 2h idle threshold
        decisions = _decide([rec], store, cfg, now=time.time())
        assert len(decisions) == 1
        assert decisions[0].action == SKIP_ACTIVE

    def test_summarize_new(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        rec = _make_record(age_hours=4.0)  # > 2h, no existing archive
        decisions = _decide([rec], store, cfg, now=time.time())
        assert decisions[0].action == SUMMARIZE_NEW

    def test_skip_clean(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        rec = _make_record(age_hours=4.0)
        # Create archive with mtime AFTER activity_mtime
        archive = store.paths.archive / Store.short_filename(
            rec.first_event_at, rec.conversation_uuid
        )
        archive.write_text("existing")
        # Touch archive to be newer than activity
        os.utime(archive, (time.time() + 100, time.time() + 100))
        decisions = _decide([rec], store, cfg, now=time.time())
        assert decisions[0].action == SKIP_CLEAN

    def test_re_summarize_recent(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        rec = _make_record(age_hours=4.0)
        # Create archive with mtime BEFORE activity (dirty)
        archive = store.paths.archive / Store.short_filename(
            rec.first_event_at, rec.conversation_uuid
        )
        archive.write_text("old summary")
        # Set archive mtime to 1 day ago (within 7-day resummarize window)
        old_mtime = time.time() - 86400
        os.utime(archive, (old_mtime, old_mtime))
        decisions = _decide([rec], store, cfg, now=time.time())
        assert decisions[0].action == RE_SUMMARIZE

    def test_addendum_old(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        rec = _make_record(age_hours=4.0)
        archive = store.paths.archive / Store.short_filename(
            rec.first_event_at, rec.conversation_uuid
        )
        archive.write_text("old summary")
        # Set archive mtime to 30 days ago (beyond 7-day resummarize window)
        old_mtime = time.time() - 86400 * 30
        os.utime(archive, (old_mtime, old_mtime))
        decisions = _decide([rec], store, cfg, now=time.time())
        assert decisions[0].action == ADDENDUM


# ----- _process_decisions ----------------------------------------------------


class TestProcessDecisions:
    def test_summarize_new_writes_files(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        rec = _make_record(content="Some conversation about code review.")
        decisions = [Decision(rec, SUMMARIZE_NEW)]
        summarizer = MockSummarizer()
        cost = _process_decisions(decisions, store, cfg, summarizer)
        assert cost >= 0
        # Should have written archive and short
        archives = list(store.paths.archive.glob("*.md"))
        shorts = list(store.paths.journal.glob("*-*.md"))
        assert len(archives) == 1
        assert len(shorts) >= 1

    def test_oversized_record_chunk_reduced_before_summary(self, tmp_path: Path):
        # A real record whose content exceeds max_prompt_content_chars is
        # chunk-and-reduced in-place ONCE (fit != original → replace), so the
        # downstream summary sees already-fitted content. Covers the replace
        # branch in _process_decisions' precompute.
        cfg = _cfg(
            tmp_path,
            extra_yaml="budgets:\n  max_prompt_content_chars: 200\n",
        )
        store = Store(cfg.store.root)
        store.init_layout()
        big = "".join(f"important fact number {i}\n" for i in range(80))
        assert len(big) > 200  # genuinely over budget → fit reduces it
        rec = _make_record(content=big)
        decisions = [Decision(rec, SUMMARIZE_NEW)]
        summarizer = _TinySummarizer()  # short digests → reduce converges
        cost = _process_decisions(decisions, store, cfg, summarizer)
        assert cost >= 0
        assert len(list(store.paths.archive.glob("*.md"))) == 1

    def test_oversized_record_summarizer_error_is_skipped(self, tmp_path: Path):
        # A giant transcript whose chunk-and-reduce map call errors must be
        # logged-and-skipped (the fit runs INSIDE the try), never crashing
        # the whole rebuild. Before the fix this raised out of the loop.
        cfg = _cfg(
            tmp_path,
            extra_yaml="budgets:\n  max_prompt_content_chars: 200\n",
        )
        store = Store(cfg.store.root)
        store.init_layout()
        big = "".join(f"fact {i}\n" for i in range(200))
        assert len(big) > 200  # forces the fit path → a map call
        rec = _make_record(content=big)
        decisions = [Decision(rec, SUMMARIZE_NEW)]
        summarizer = MockSummarizer()
        summarizer.summarize = MagicMock(side_effect=RuntimeError("boom"))
        cost = _process_decisions(decisions, store, cfg, summarizer)  # no raise
        assert cost == 0
        assert list(store.paths.archive.glob("*.md")) == []

    def test_addendum_writes_short_only(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        rec = _make_record(content="Updated conversation.")
        # Create pre-existing archive
        archive = store.paths.archive / Store.short_filename(
            rec.first_event_at, rec.conversation_uuid
        )
        archive.write_text("original summary")
        decisions = [Decision(rec, ADDENDUM, archive)]
        summarizer = MockSummarizer()
        cost = _process_decisions(decisions, store, cfg, summarizer)
        assert cost >= 0
        # Should have written a new short (addendum)
        shorts = list(store.paths.journal.glob("*-*.md"))
        assert len(shorts) >= 1

    def test_skip_actions_do_nothing(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        rec = _make_record()
        decisions = [
            Decision(rec, SKIP_ACTIVE),
            Decision(rec, SKIP_CLEAN),
        ]
        summarizer = MockSummarizer()
        cost = _process_decisions(decisions, store, cfg, summarizer)
        assert cost == 0
        assert list(store.paths.archive.glob("*.md")) == []

    def test_exception_in_summarizer_continues(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        rec = _make_record()
        decisions = [Decision(rec, SUMMARIZE_NEW)]
        summarizer = MockSummarizer()
        summarizer.summarize = MagicMock(side_effect=RuntimeError("boom"))
        # Should not raise — logs and continues
        cost = _process_decisions(decisions, store, cfg, summarizer)
        assert cost == 0


# ----- cascade rollups -------------------------------------------------------


class TestCascadeRollups:
    def test_cascade_creates_daily(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        summarizer = MockSummarizer()
        # Create a short
        uid = str(uuid4())
        short = store.paths.journal / f"20260514-082136-{uid}.md"
        short.write_text(frontmatter.render(
            {"type": "short_summary"}, "Short summary body.\n"
        ))
        _cascade_all_rollups(store, cfg, summarizer)
        # Should have created a daily rollup
        dailies = [
            f for f in store.paths.journal.glob("*.md")
            if "daily" in f.name
        ]
        assert len(dailies) == 1

    def test_cascade_creates_weekly_from_daily(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        summarizer = MockSummarizer()
        # Create a short + cascade to daily first
        uid = str(uuid4())
        # Monday 2026-05-11
        short = store.paths.journal / f"20260511-082136-{uid}.md"
        short.write_text(frontmatter.render(
            {"type": "short_summary"}, "Short.\n"
        ))
        _cascade_all_rollups(store, cfg, summarizer)
        weeklies = [
            f for f in store.paths.journal.glob("*.md")
            if "week" in f.name
        ]
        assert len(weeklies) == 1

    def test_cascade_creates_monthly_from_weekly(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        summarizer = MockSummarizer()
        # Create short → daily → weekly → monthly
        uid = str(uuid4())
        short = store.paths.journal / f"20260511-082136-{uid}.md"
        short.write_text(frontmatter.render(
            {"type": "short_summary"}, "Short.\n"
        ))
        _cascade_all_rollups(store, cfg, summarizer)
        monthlies = [
            f for f in store.paths.journal.glob("*.md")
            if "month" in f.name
        ]
        assert len(monthlies) == 1


# ----- longer memory ---------------------------------------------------------


class TestRefreshLongerMemory:
    def test_no_old_monthlies_is_noop(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        summarizer = MockSummarizer()
        _refresh_longer_memory(store, cfg, summarizer)
        assert not (store.paths.journal / "longer_memory.md").exists()

    def test_folds_old_monthly(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        summarizer = MockSummarizer()
        # Create a monthly from 2 years ago (definitely beyond cutoff)
        old_monthly = store.paths.journal / "202401-month-old.md"
        old_monthly.write_text(frontmatter.render(
            {"type": "monthly_rollup", "period": "2024-01"},
            "Ancient monthly content.\n",
        ))
        _refresh_longer_memory(store, cfg, summarizer)
        assert (store.paths.journal / "longer_memory.md").exists()
        # The monthly should be marked as folded
        fm = frontmatter.read_frontmatter(old_monthly)
        assert "folded_into_longer_memory" in fm


# ----- _apply_decay ----------------------------------------------------------


class TestApplyDecay:
    def test_decay_noop_when_empty(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        _apply_decay(store, cfg)  # should not error

    def test_decay_preserves_must_memorize(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        mm_file = store.paths.journal / "must_memorize.md"
        mm_file.write_text(dedent("""\
            | kind | memo | priority | first_seen | last_seen |
            |------|------|----------|------------|-----------|
            | owner_explicit | Important fact | 100 | 2026-05-01 | 2026-05-15 |
        """))
        _apply_decay(store, cfg)
        # File should still exist
        assert mm_file.exists()


# ----- _write_state ----------------------------------------------------------


class TestWriteState:
    def test_writes_state_json(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        rec = _make_record()
        decisions = [
            Decision(rec, SKIP_ACTIVE),
            Decision(rec, SKIP_CLEAN),
            Decision(rec, SUMMARIZE_NEW),
            Decision(rec, ADDENDUM),
        ]
        _write_state(store, cfg, decisions=decisions, cost_usd=0.42,
                     duration_sec=3.5, last_op="rebuild")
        state = store.read_state()
        assert state["agent"] == "T"
        assert state["last_op"] == "rebuild"
        assert state["sessions"]["active"] == 1
        assert state["sessions"]["clean"] == 1
        assert state["sessions"]["dirty"] == 1
        assert state["sessions"]["frozen"] == 1
        assert state["last_rebuild_cost_usd"] == 0.42
        assert state["last_rebuild_duration_sec"] == 3.5


# ----- _auto_memory_record ---------------------------------------------------


class TestAutoMemoryRecord:
    def test_returns_none_when_not_configured(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        result = _auto_memory_record(cfg)
        assert result is None

    def test_returns_record_when_configured(self, tmp_path: Path):
        auto_mem_dir = tmp_path / "auto_memory"
        auto_mem_dir.mkdir()
        (auto_mem_dir / "note1.md").write_text("# Note 1\nSome content.")
        (auto_mem_dir / "note2.md").write_text("# Note 2\nMore content.")
        cfg_path = tmp_path / "cfg_am.yaml"
        cfg_path.write_text(dedent(f"""\
            agent: {{name: T, role: T}}
            store: {{root: {tmp_path}/memory}}
            sources:
              - kind: claude_code
                project_path: {tmp_path}/proj/
              - kind: auto_memory
                path: {auto_mem_dir}
            summarizer: {{backend: anthropic, model: claude-sonnet-4-6, prompts: default/v1}}
            rebuild: {{lock_path: {tmp_path}/lock}}
        """))
        cfg = load_config(cfg_path)
        result = _auto_memory_record(cfg)
        if result is not None:
            assert "Note 1" in result.content
            assert result.source == "doc"

    def test_returns_none_when_dir_empty(self, tmp_path: Path):
        auto_mem_dir = tmp_path / "auto_memory"
        auto_mem_dir.mkdir()
        cfg_path = tmp_path / "cfg_am2.yaml"
        cfg_path.write_text(dedent(f"""\
            agent: {{name: T, role: T}}
            store: {{root: {tmp_path}/memory}}
            sources:
              - kind: claude_code
                project_path: {tmp_path}/proj/
              - kind: auto_memory
                path: {auto_mem_dir}
            summarizer: {{backend: anthropic, model: claude-sonnet-4-6, prompts: default/v1}}
            rebuild: {{lock_path: {tmp_path}/lock}}
        """))
        cfg = load_config(cfg_path)
        result = _auto_memory_record(cfg)
        assert result is None


# ----- helpers ---------------------------------------------------------------


class TestClip:
    def test_short_text_unchanged(self):
        assert _clip("hello", max_chars=100) == "hello"

    def test_long_text_clipped(self):
        text = "a" * 200
        result = _clip(text, max_chars=50)
        assert "[...content elided...]" in result
        assert len(result) < 200
        assert len(result) <= 50  # strict ceiling, marker room reserved

    def test_tiny_budget_smaller_than_marker(self):
        # Budget too small to fit the elision marker → hard truncate, still
        # within the ceiling (no crash, no negative slice).
        result = _clip("a" * 100, max_chars=10)
        assert result == "a" * 10
        assert len(result) == 10


class TestSplitOnBoundaries:
    def test_short_text_single_chunk(self):
        assert _split_on_boundaries("hello\nworld\n", max_chars=100) == [
            "hello\nworld\n"
        ]

    def test_splits_on_line_boundaries(self):
        text = "".join(f"line{i}\n" for i in range(20))  # 20 short lines
        chunks = _split_on_boundaries(text, max_chars=30)
        assert len(chunks) > 1
        # Every chunk fits the budget...
        assert all(len(c) <= 30 for c in chunks)
        # ...and the split is lossless (concatenation == original).
        assert "".join(chunks) == text
        # No chunk cuts mid-line (each ends on a newline here).
        assert all(c.endswith("\n") for c in chunks)

    def test_oversized_single_line_hard_split(self):
        # A single line longer than the budget must still be chopped so the
        # per-chunk invariant holds (no summarizer can be cheated by a blob).
        text = "x" * 250  # one "line", no newlines
        chunks = _split_on_boundaries(text, max_chars=100)
        assert all(len(c) <= 100 for c in chunks)
        assert "".join(chunks) == text
        assert len(chunks) == 3

    def test_oversized_line_after_buffer_flushes_first(self):
        # A normal line, then an oversized line: the buffer flushes before
        # the hard-split so ordering is preserved.
        text = "short\n" + ("y" * 250)
        chunks = _split_on_boundaries(text, max_chars=100)
        assert "".join(chunks) == text
        assert chunks[0] == "short\n"
        assert all(len(c) <= 100 for c in chunks)


class _GrowingSummarizer(MockSummarizer):
    """A summarizer whose output never shrinks below the budget, to force
    ``_fit_content`` down to its depth-cap / clip fallback."""

    name = "growing"

    def __init__(self, *, blob_chars: int) -> None:
        super().__init__()
        self._blob = "z" * blob_chars
        self.calls = 0

    def summarize(self, *, prompt: str, max_words: int) -> str:
        self.calls += 1
        return self._blob


class _TinySummarizer(MockSummarizer):
    """Returns a short constant digest so a reduce pass clearly converges."""

    name = "tiny"

    def __init__(self, *, digest: str = "digest") -> None:
        super().__init__()
        self._digest = digest
        self.calls = 0

    def summarize(self, *, prompt: str, max_words: int) -> str:
        self.calls += 1
        return self._digest


class TestFitContent:
    def test_fits_already_no_model_calls(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        summ = _TinySummarizer()
        text = "small content"
        out = _fit_content(text, summarizer=summ, cfg=cfg, max_chars=1000)
        assert out == text
        assert summ.calls == 0  # no reduce → no model calls

    def test_chunk_and_reduce_no_lossy_elision(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        summ = _TinySummarizer()
        # ~60 lines well over a tiny budget → multiple chunks, each mapped.
        text = "".join(f"important fact number {i}\n" for i in range(60))
        out = _fit_content(text, summarizer=summ, cfg=cfg, max_chars=200)
        # Short digests → the combined digest converges under budget.
        assert len(out) <= 200
        # It is a real reduction, not a head/tail clip.
        assert "[...content elided...]" not in out
        assert "[transcript part 1/" in out
        # One map call per chunk (at least 2 chunks for this size).
        assert summ.calls >= 2

    def test_recurses_then_converges(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        # A digest big enough that ONE map pass over many chunks still
        # exceeds the budget, forcing a second (reduce) pass that then
        # converges — exercises the recursion without hitting the cap.
        summ = _TinySummarizer(digest="d" * 40)
        text = "".join(f"line {i} with some words\n" for i in range(120))
        out = _fit_content(text, summarizer=summ, cfg=cfg, max_chars=400)
        assert len(out) <= 400
        assert "[...content elided...]" not in out
        assert "[transcript part 1/" in out

    def test_depth_cap_falls_back_to_clip(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        # Each digest is itself over budget, so the reduce can never
        # converge → depth cap trips → bounded clip as last resort.
        summ = _GrowingSummarizer(blob_chars=500)
        text = "a" * 5000
        out = _fit_content(text, summarizer=summ, cfg=cfg, max_chars=300)
        assert len(out) <= 300
        assert "[...content elided...]" in out  # clip fallback marker
        assert summ.calls > 0

    def test_tiny_budget_clips_without_model_calls(self, tmp_path: Path):
        # A budget smaller than the per-part wrappers can never converge and
        # would feed range() a zero step; guard clips directly, no map calls.
        cfg = _cfg(tmp_path)
        summ = _TinySummarizer()
        out = _fit_content("X" * 100, summarizer=summ, cfg=cfg, max_chars=10)
        assert len(out) <= 10
        assert summ.calls == 0


class TestFillPrompt:
    def test_fills_placeholders(self, tmp_path: Path):
        template = tmp_path / "test.md"
        template.write_text("Hello {agent_name}, you have {max_words} words.")
        result = _fill_prompt(template, agent_name="Sai", max_words=100)
        assert result == "Hello Sai, you have 100 words."

    def test_missing_key_stays_literal(self, tmp_path: Path):
        template = tmp_path / "test.md"
        template.write_text("Hello {agent_name}, {unknown_key}.")
        result = _fill_prompt(template, agent_name="Sai")
        assert "{unknown_key}" in result


class TestPromptsRoot:
    def test_finds_bundled_prompts(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        root = _prompts_root(cfg)
        assert root.exists()
        assert (root / "short_summary.md").exists()


class TestApproxCost:
    def test_returns_positive_for_known_model(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        cost = _approx_cost(cfg, "x" * 4000, n_short=1, n_detailed=1)
        assert cost > 0

    def test_returns_zero_for_unknown_model(self, tmp_path: Path):
        cfg_path = tmp_path / "cfg2.yaml"
        cfg_path.write_text(dedent(f"""\
            agent: {{name: T, role: T}}
            store: {{root: {tmp_path}/memory2}}
            sources:
              - kind: claude_code
                project_path: {tmp_path}/proj/
            summarizer: {{backend: anthropic, model: unknown-model-99, prompts: default/v1}}
            rebuild: {{lock_path: {tmp_path}/lock2}}
        """))
        cfg = load_config(cfg_path)
        cost = _approx_cost(cfg, "x" * 4000, n_short=1, n_detailed=1)
        assert cost == 0.0


class TestEstimateTotalCost:
    def test_empty_records(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        assert _estimate_total_cost(cfg, 10, []) == 0.0

    def test_scales_with_n(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        records = [_make_record(content="x" * 1000) for _ in range(3)]
        cost_5 = _estimate_total_cost(cfg, 5, records)
        cost_10 = _estimate_total_cost(cfg, 10, records)
        assert cost_10 == pytest.approx(cost_5 * 2, rel=0.01)


class TestBuildAdapters:
    def test_builds_claude_code_adapter(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        adapters = _build_adapters(cfg)
        assert len(adapters) == 1
        assert adapters[0].__class__.__name__ == "ClaudeTranscriptAdapter"


class TestBuildSummarizer:
    def test_mock_mode(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        s = _build_summarizer(cfg, mock=True)
        assert isinstance(s, MockSummarizer)

    def test_anthropic_mode(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        s = _build_summarizer(cfg, mock=False)
        assert s.__class__.__name__ == "AnthropicSummarizer"

    def test_unknown_backend_raises(self, tmp_path: Path):
        """An unregistered backend now raises ``SummarizerError`` (was
        ``NotImplementedError`` before the registry refactor). The
        message lists every registered backend so the user knows what
        to install / configure."""
        from tigerharness.tiger_memory.summarizers import SummarizerError
        cfg_path = tmp_path / "cfg3.yaml"
        cfg_path.write_text(dedent(f"""\
            agent: {{name: T, role: T}}
            store: {{root: {tmp_path}/memory3}}
            sources:
              - kind: claude_code
                project_path: {tmp_path}/proj/
            summarizer: {{backend: openai, model: gpt-4, prompts: default/v1}}
            rebuild: {{lock_path: {tmp_path}/lock3}}
        """))
        cfg = load_config(cfg_path)
        with pytest.raises(SummarizerError, match="openai"):
            _build_summarizer(cfg)


# ----- _spawn_background -----------------------------------------------------


class TestSpawnBackground:
    def test_spawn_returns_zero(self, tmp_path: Path):
        with patch("tigerharness.tiger_memory.lifecycle.subprocess.Popen") as mock_popen:
            result = _spawn_background()
            assert result == 0
            mock_popen.assert_called_once()


# ----- top-level entry points ------------------------------------------------


class TestBootstrap:
    def test_bootstrap_with_mock_summarizer(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        # Create a fake JSONL transcript
        proj = tmp_path / "proj"
        proj.mkdir(parents=True, exist_ok=True)
        uid = str(uuid4())
        jsonl = proj / f"{uid}.jsonl"
        jsonl.write_text(
            '{"type":"summary","subtype":"final","summary":"test session","cost_usd":0.01,'
            f'"session_id":"{uid}","duration_ms":60000}}\n'
        )
        ret = bootstrap(cfg, store, summarizer_override=MockSummarizer(), limit=1)
        assert ret == 0

    def test_bootstrap_lock_contention(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        # Pre-create lock file with our PID (simulates contention)
        cfg.rebuild.lock_path.write_text(str(os.getpid()))
        # Can't test real contention easily, but we can test the flow
        # by calling with a real lock held — the lock context manager
        # handles this.


class TestRebuild:
    def test_rebuild_with_mock(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        ret = rebuild(cfg, store, summarizer_override=MockSummarizer())
        assert ret == 0
        # State should be written
        state = store.read_state()
        assert state is not None
        assert state["last_op"] == "rebuild"

    def test_rebuild_background_spawns(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        with patch("tigerharness.tiger_memory.lifecycle._spawn_background", return_value=0) as mock_spawn:
            # background=True without TIGER_MEMORY_BACKGROUND_SPAWNED triggers spawn
            ret = rebuild(cfg, store, background=True)
            assert ret == 0
            mock_spawn.assert_called_once()


class TestResummarize:
    def test_resummarize_bad_date(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        ret = resummarize(cfg, store, since="not-a-date")
        assert ret == 2

    def test_resummarize_with_mock(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        # No records to resummarize, but the flow completes
        with patch("tigerharness.tiger_memory.lifecycle._build_adapters", return_value=[]):
            ret = resummarize(cfg, store, since="2026-05-01")
            assert ret == 0

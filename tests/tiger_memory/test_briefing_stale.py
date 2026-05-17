"""Coverage tests for briefing.py — _render_manifest stale path (lines 293-294),
_copy_layer2 shorts fallback (217-218), _copy_layer3/4 matching (237, 258)."""
from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch
from uuid import uuid4

import pytest

from tigerharness.tiger_memory import frontmatter
from tigerharness.tiger_memory.briefing import (
    _copy_layer2,
    _copy_layer3,
    _copy_layer4,
    _render_manifest,
    rebuild_briefing,
)
from tigerharness.tiger_memory.config import load_config
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


class TestRenderManifestStale:
    """Lines 293-294: stale last_rebuild triggers warning in manifest."""

    def test_stale_manifest(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()

        # Set last_rebuild to old timestamp
        store.write_state({"last_rebuild_at": "2020-01-01T00:00:00+00:00"})

        manifest = _render_manifest(
            cfg=cfg,
            store=store,
            layer1=[], layer2=[], layer3=[], layer4=[],
            has_longer=False,
            has_mm=False,
        )
        assert "stale" in manifest.lower()


class TestCopyLayer2ShortsFallback:
    """Lines 217-218: no daily exists → copy shorts instead."""

    def test_shorts_fallback(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()

        uid = str(uuid4())
        short = store.paths.journal / f"20260514-082136-{uid}.md"
        short.write_text(frontmatter.render(
            {"type": "short_summary"}, "Short fallback.\n"
        ))

        dest = tmp_path / "br_tmp"
        dest.mkdir()
        (dest / "daily").mkdir()

        result = _copy_layer2(store, ["20260514"], dest)
        assert len(result) >= 1
        assert any("20260514" in str(p) for p in result)


class TestCopyLayer2DailyExists:
    """Lines 217-218: daily exists → copy it (not shorts)."""

    def test_daily_copied(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()

        uid = str(uuid4())
        daily = store.paths.journal / f"20260514-daily-{uid}.md"
        daily.write_text(frontmatter.render(
            {"type": "daily_rollup"}, "Daily content.\n"
        ))

        dest = tmp_path / "br_tmp2"
        dest.mkdir()
        (dest / "daily").mkdir()

        result = _copy_layer2(store, ["20260514"], dest)
        assert len(result) == 1
        assert "daily" in str(result[0])


class TestCopyLayer3WeeklyMatch:
    """Line 237: _copy_layer3 matches weekly files in date range."""

    def test_weekly_in_range(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()

        uid = str(uuid4())
        weekly = store.paths.journal / f"20260512-week-{uid}.md"
        weekly.write_text(frontmatter.render(
            {"type": "weekly_rollup"}, "Weekly summary.\n"
        ))

        # Also create a non-weekly file so line 237 (continue) fires
        (store.paths.journal / "must_memorize.md").write_text("# MM\n")

        dest = tmp_path / "br_tmp"
        dest.mkdir()
        (dest / "weekly").mkdir()

        # Pass a date range that includes the Monday 20260512
        result = _copy_layer3(store, ["20260510", "20260514"], dest)
        assert len(result) >= 1


class TestCopyLayer4MonthlyMatch:
    """Line 258: _copy_layer4 matches monthly files in date range."""

    def test_monthly_in_range(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()

        uid = str(uuid4())
        monthly = store.paths.journal / f"202605-month-{uid}.md"
        monthly.write_text(frontmatter.render(
            {"type": "monthly_rollup"}, "Monthly summary.\n"
        ))

        # Also create a non-monthly file so line 258 (continue) fires
        (store.paths.journal / "not-a-monthly.md").write_text("nope\n")

        dest = tmp_path / "br_tmp"
        dest.mkdir()
        (dest / "monthly").mkdir()

        result = _copy_layer4(store, ["20260514"], dest)
        assert len(result) >= 1

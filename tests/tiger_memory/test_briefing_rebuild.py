"""Tests for the full briefing rebuild flow — layers 3/4 + manifest."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from uuid import uuid4

import pytest

from tigerharness.tiger_memory.briefing import (
    _briefing_up_to_date,
    _compute_fingerprint,
    _copy_layer1,
    _copy_layer2,
    _copy_layer3,
    _copy_layer4,
    _date_range_from,
    _is_stale,
    _one_line_preview,
    _render_manifest,
    _slice_layers,
    rebuild_briefing,
)
from tigerharness.tiger_memory.config import load_config
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
        briefing:
          walking:
            full_shorts_working_days: 2
            dailies_working_days: 7
            weeklies_working_days: 28
            monthlies_working_days: 2
    """))
    cfg = load_config(cfg_path)
    store = Store(cfg.store.root)
    store.init_layout()
    return cfg, store


class TestCopyLayer3:
    def test_copies_weeklies_in_date_range(self, tmp_path: Path):
        cfg, store = _setup(tmp_path)
        # Create weekly files — 20260504 is in May, 20260601 is June
        w1 = store.paths.journal / "20260504-week-design.md"
        w2 = store.paths.journal / "20260511-week-impl.md"
        w_other = store.paths.journal / "20260601-week-june.md"
        for f in (w1, w2, w_other):
            f.write_text(f"# {f.stem}\n")

        dest = tmp_path / "staging"
        (dest / "weekly").mkdir(parents=True)

        # Date range covering May
        result = _copy_layer3(store, ["20260504", "20260511"], dest)
        assert len(result) == 2
        assert (dest / "weekly" / w1.name).exists()
        assert (dest / "weekly" / w2.name).exists()
        assert not (dest / "weekly" / w_other.name).exists()

    def test_empty_dates(self, tmp_path: Path):
        cfg, store = _setup(tmp_path)
        dest = tmp_path / "staging"
        (dest / "weekly").mkdir(parents=True)
        result = _copy_layer3(store, [], dest)
        assert result == []


class TestCopyLayer4:
    def test_copies_monthlies_in_range(self, tmp_path: Path):
        cfg, store = _setup(tmp_path)
        m1 = store.paths.journal / "202605-month-may.md"
        m2 = store.paths.journal / "202604-month-apr.md"
        m_other = store.paths.journal / "202603-month-mar.md"
        for f in (m1, m2, m_other):
            f.write_text(f"# {f.stem}\n")

        dest = tmp_path / "staging"
        (dest / "monthly").mkdir(parents=True)

        # Dates spanning April-May
        result = _copy_layer4(store, ["20260415", "20260510"], dest)
        assert len(result) == 2
        names = {p.name for p in result}
        assert m1.name in names
        assert m2.name in names


class TestRenderManifest:
    def test_basic_manifest(self, tmp_path: Path):
        cfg, store = _setup(tmp_path)
        # Create some layer files
        layer1 = [tmp_path / "recent" / "short.md"]
        layer2 = [tmp_path / "daily" / "daily.md"]
        layer3 = [tmp_path / "weekly" / "weekly.md"]
        layer4 = [tmp_path / "monthly" / "monthly.md"]
        for p in layer1 + layer2 + layer3 + layer4:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("Content line here.\n")

        text = _render_manifest(
            cfg=cfg, store=store,
            layer1=layer1, layer2=layer2,
            layer3=layer3, layer4=layer4,
            has_longer=True, has_mm=True,
            stats={"total_words": 12, "total_chars": 80, "sections": {}},
            resident_layers=("recent", "daily", "weekly", "monthly"),
        )
        assert "# Briefing manifest" in text
        assert "Briefing size: 12 words / 80 chars" in text
        assert "Drill on demand" not in text  # all layers resident
        assert "Agent: T" in text
        assert "Must memorize" in text
        assert "Longer memory" in text
        assert "Monthly summaries" in text
        assert "Weekly summaries" in text
        assert "Daily summaries" in text
        assert "Recent shorts" in text

    def test_manifest_without_mm_and_longer(self, tmp_path: Path):
        cfg, store = _setup(tmp_path)
        text = _render_manifest(
            cfg=cfg, store=store,
            layer1=[], layer2=[], layer3=[], layer4=[],
            has_longer=False, has_mm=False,
            stats={"total_words": 0, "total_chars": 0, "sections": {}},
            resident_layers=("recent", "daily", "weekly", "monthly"),
        )
        assert "Must memorize" not in text
        assert "Longer memory" not in text
        assert "Read order" in text


class TestIsStale:
    def test_recent_is_not_stale(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert _is_stale(now) is False

    def test_old_is_stale(self):
        assert _is_stale("2020-01-01T00:00:00Z") is True

    def test_invalid_is_stale(self):
        assert _is_stale("not-a-date") is True


class TestOneLinePreview:
    def test_returns_first_content(self, tmp_path: Path):
        p = tmp_path / "test.md"
        p.write_text("---\ntitle: X\n---\n# Heading\n\nActual content.\n")
        assert _one_line_preview(p) == "Actual content."

    def test_returns_bullet(self, tmp_path: Path):
        p = tmp_path / "test.md"
        p.write_text("# H\n\n- Bullet here\n")
        assert _one_line_preview(p) == "Bullet here"

    def test_missing_file(self, tmp_path: Path):
        assert _one_line_preview(tmp_path / "nope.md") == ""


class TestFullRebuild:
    def test_rebuild_creates_briefing(self, tmp_path: Path):
        cfg, store = _setup(tmp_path)
        # Create some journal content
        uid = str(uuid4())
        (store.paths.journal / f"20260514-082136-{uid}.md").write_text(
            "---\nconversation_uuid: x\n---\n# Session notes\n"
        )
        (store.paths.journal / "20260514-daily-recap.md").write_text(
            "# Daily recap\nDid some work.\n"
        )
        (store.paths.journal / "must_memorize.md").write_text(
            "| kind | memo |\n|---|---|\n| pref | likes coffee |\n"
        )
        (store.paths.journal / "longer_memory.md").write_text(
            "---\ncovers_until: 2026-04-01\n---\n# Long memory\n"
        )

        rebuild_briefing(cfg, store)

        assert store.paths.briefing.exists()
        assert (store.paths.briefing / "MANIFEST.md").exists()
        assert (store.paths.briefing / "README.md").exists()
        assert (store.paths.briefing / "must_memorize.md").exists()
        assert (store.paths.briefing / "longer_memory.md").exists()

    def test_rebuild_is_idempotent(self, tmp_path: Path):
        cfg, store = _setup(tmp_path)
        (store.paths.journal / "20260514-daily-x.md").write_text("# X\n")

        rebuild_briefing(cfg, store)
        mtime1 = (store.paths.briefing / "MANIFEST.md").stat().st_mtime

        # Second call should no-op (fingerprint unchanged)
        rebuild_briefing(cfg, store)
        mtime2 = (store.paths.briefing / "MANIFEST.md").stat().st_mtime
        assert mtime1 == mtime2

    def test_rebuild_detects_change(self, tmp_path: Path):
        cfg, store = _setup(tmp_path)
        (store.paths.journal / "20260514-daily-x.md").write_text("# X\n")

        rebuild_briefing(cfg, store)
        fp1 = (store.paths.briefing / ".fingerprint").read_text()

        # Add new file → fingerprint changes → rebuild happens
        (store.paths.journal / "20260515-daily-y.md").write_text("# Y\n")
        rebuild_briefing(cfg, store)
        fp2 = (store.paths.briefing / ".fingerprint").read_text()
        assert fp1 != fp2

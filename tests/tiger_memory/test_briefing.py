"""Tests for briefing walking + manifest."""
from __future__ import annotations

import time
from pathlib import Path
from textwrap import dedent
from uuid import uuid4

import pytest

from tigerharness.tiger_memory.briefing import _slice_layers, rebuild_briefing
from tigerharness.tiger_memory.config import WalkingConfig, load_config
from tigerharness.tiger_memory.store import Store


def _setup(tmp_path: Path):
    cfg_path = tmp_path / "tiger-memory.config.yaml"
    cfg_path.write_text(dedent(f"""\
        agent: {{name: T, role: T}}
        store: {{root: {tmp_path}/memory}}
        sources:
          - kind: claude_code
            project_path: {tmp_path}/proj/
        summarizer: {{backend: anthropic, model: claude-opus-4-7, prompts: default/v1}}
        rebuild:
          lock_path: {tmp_path}/test.lock
    """))
    cfg = load_config(cfg_path)
    store = Store(cfg.store.root)
    store.init_layout()
    return cfg, store


def _make_short(store: Store, date_str: str, hms: str = "082136"):
    uid = str(uuid4())
    f = store.paths.journal / f"{date_str}-{hms}-{uid}.md"
    f.write_text(f"""---
type: short_summary
conversation_uuid: {uid}
---
- bullet for {date_str}
""")
    return f


def test_slice_layers_additive_no_overlap() -> None:
    walking = WalkingConfig(
        full_shorts_working_days=2,
        dailies_working_days=30,
        weeklies_working_days=28,
        monthlies_working_days=90,
    )
    # Fake config wrapper
    class C:
        class B:
            walking_ = walking
        briefing = type("X", (), {"walking": walking})()

    # Generate 200 synthetic working-day strings (newest-first).
    from datetime import date, timedelta
    base = date(2026, 5, 14)
    working_days = [(base - timedelta(days=i)).strftime("%Y%m%d")
                   for i in range(200)]
    l1, l2, l3, l4 = _slice_layers(working_days, C)
    assert len(l1) == 2
    assert len(l2) == 30
    assert len(l3) == 28
    assert len(l4) == 90
    # No overlap (consecutive slices)
    seen = set()
    for d in l1 + l2 + l3 + l4:
        assert d not in seen, f"overlap: {d}"
        seen.add(d)


def test_briefing_rebuild_creates_subfolders(tmp_path: Path) -> None:
    cfg, store = _setup(tmp_path)
    _make_short(store, "20260514")
    rebuild_briefing(cfg, store)
    assert (store.paths.briefing / "MANIFEST.md").exists()
    assert (store.paths.briefing / "recent").is_dir()
    assert (store.paths.briefing / "daily").is_dir()
    assert (store.paths.briefing / "weekly").is_dir()
    assert (store.paths.briefing / "monthly").is_dir()


def test_briefing_no_op_when_unchanged(tmp_path: Path) -> None:
    cfg, store = _setup(tmp_path)
    _make_short(store, "20260514")
    rebuild_briefing(cfg, store)
    manifest = store.paths.briefing / "MANIFEST.md"
    mtime_before = manifest.stat().st_mtime
    # Wait a beat so any rewrite would change mtime.
    time.sleep(0.01)
    rebuild_briefing(cfg, store)
    assert manifest.stat().st_mtime == mtime_before


def test_briefing_full_overwrite_drops_stale_files(tmp_path: Path) -> None:
    cfg, store = _setup(tmp_path)
    _make_short(store, "20260514")
    rebuild_briefing(cfg, store)
    # Plant a stale file in briefing.
    stale = store.paths.briefing / "recent" / "stale.md"
    stale.write_text("zombie")
    assert stale.exists()
    # Force a journal change so the no-op shortcut doesn't fire.
    _make_short(store, "20260513", hms="091200")
    rebuild_briefing(cfg, store)
    assert not stale.exists()


def test_layer2_fallback_to_shorts_when_no_daily(tmp_path: Path) -> None:
    cfg, store = _setup(tmp_path)
    # Create 3 working days: 14, 13, 12. With full=2, layer 2 includes day 12.
    _make_short(store, "20260514")
    _make_short(store, "20260513")
    _make_short(store, "20260512")
    rebuild_briefing(cfg, store)
    # No daily exists → layer 2's "day 12" should appear as shorts in daily/
    daily_dir = store.paths.briefing / "daily"
    files = sorted(daily_dir.glob("*.md"))
    assert any("20260512" in f.name for f in files)

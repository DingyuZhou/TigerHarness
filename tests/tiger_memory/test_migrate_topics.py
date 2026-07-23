"""Tests for the one-off topic-store migration (migrate_topics.py, ADR 0007).

Covers the dry-run default (report only, disk untouched), --apply (retire
diary/fuzzy/emotional files + sidecars, create empty topics.md), idempotent
re-runs, and the partial-rerun collision path (dest exists → ``.again``).
"""
from __future__ import annotations

from pathlib import Path

import yaml

from tigerharness.tiger_memory import migrate_topics as mt
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.store import Store


def _env(tmp_path: Path):
    raw = {
        "agent": {"name": "Aya", "role": "r"},
        "store": {"root": str(tmp_path / "memory")},
        "sources": [{"kind": "claude_code", "project_path": f"{tmp_path}/p/"}],
        "summarizer": {"backend": "anthropic", "model": "m", "prompts": "default/v1"},
    }
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(yaml.safe_dump(raw))
    cfg = load_config(cfg_path)
    store = Store(cfg.store.root)
    store.init_layout()
    return cfg, store


def _seed_legacy(store: Store) -> list[str]:
    for name in mt._RETIRED_FILES:
        (store.paths.journal / name).write_text(f"content of {name}")
    return list(mt._RETIRED_FILES)


def test_dry_run_reports_without_touching_disk(tmp_path):
    cfg, store = _env(tmp_path)
    names = _seed_legacy(store)
    before = sorted(p.name for p in store.paths.journal.iterdir())
    report = mt.migrate_store(cfg, store, apply=False)
    assert report.persona == "Aya"
    assert report.applied is False
    assert report.retired == names
    assert report.topics_created is True
    # Nothing moved, nothing created.
    assert sorted(p.name for p in store.paths.journal.iterdir()) == before
    assert not (store.root / mt.RETIRED_DIR_NAME).exists()
    assert not (store.paths.journal / "topics.md").exists()
    assert report.to_dict() == {
        "persona": "Aya",
        "applied": False,
        "retired": names,
        "topics_created": True,
    }


def test_apply_retires_files_and_creates_topics(tmp_path):
    cfg, store = _env(tmp_path)
    names = _seed_legacy(store)
    report = mt.migrate_store(cfg, store, apply=True)
    assert report.applied is True
    assert report.retired == names
    assert report.topics_created is True
    retired_dir = store.root / mt.RETIRED_DIR_NAME
    for name in names:
        assert not (store.paths.journal / name).exists()
        assert (retired_dir / name).read_text() == f"content of {name}"
    topics = store.paths.journal / "topics.md"
    assert topics.exists()
    assert topics.read_text() == ""  # a valid, empty, zero-block store file


def test_apply_with_nothing_to_retire_only_creates_topics(tmp_path):
    cfg, store = _env(tmp_path)
    report = mt.migrate_store(cfg, store, apply=True)
    assert report.retired == []
    assert report.topics_created is True
    assert (store.paths.journal / "topics.md").exists()
    # No legacy files → the retired/ dir is never created.
    assert not (store.root / mt.RETIRED_DIR_NAME).exists()


def test_apply_rerun_is_noop(tmp_path):
    cfg, store = _env(tmp_path)
    _seed_legacy(store)
    mt.migrate_store(cfg, store, apply=True)
    snapshot = sorted(p.name for p in store.paths.journal.iterdir())
    retired_snapshot = sorted(
        p.name for p in (store.root / mt.RETIRED_DIR_NAME).iterdir()
    )
    report = mt.migrate_store(cfg, store, apply=True)
    assert report.applied is True
    assert report.retired == []
    assert report.topics_created is False
    assert sorted(p.name for p in store.paths.journal.iterdir()) == snapshot
    assert sorted(
        p.name for p in (store.root / mt.RETIRED_DIR_NAME).iterdir()
    ) == retired_snapshot


def test_dry_run_after_apply_is_clean_report(tmp_path):
    cfg, store = _env(tmp_path)
    _seed_legacy(store)
    mt.migrate_store(cfg, store, apply=True)
    report = mt.migrate_store(cfg, store, apply=False)
    assert report.applied is False
    assert report.retired == []
    assert report.topics_created is False


def test_partial_rerun_collision_keeps_both_copies(tmp_path):
    cfg, store = _env(tmp_path)
    (store.paths.journal / "diary.md").write_text("first")
    mt.migrate_store(cfg, store, apply=True)
    # A partial earlier run left (or something recreated) a second diary.md.
    (store.paths.journal / "diary.md").write_text("second")
    report = mt.migrate_store(cfg, store, apply=True)
    assert report.retired == ["diary.md"]
    assert report.topics_created is False
    retired_dir = store.root / mt.RETIRED_DIR_NAME
    assert (retired_dir / "diary.md").read_text() == "first"
    assert (retired_dir / "diary.again.md").read_text() == "second"
    assert not (store.paths.journal / "diary.md").exists()

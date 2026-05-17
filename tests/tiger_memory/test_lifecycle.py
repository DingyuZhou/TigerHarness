"""End-to-end lifecycle tests using MockSummarizer (no model spend)."""
from __future__ import annotations

import json
import time
from pathlib import Path
from textwrap import dedent

import pytest

from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.lifecycle import bootstrap, rebuild
from tigerharness.tiger_memory.store import Store
from tigerharness.tiger_memory.summarizers import MockSummarizer


def _make_jsonl(path: Path, uuid: str, ts: str = "2026-05-14T08:21:36.000Z",
                content: str = "Hello Sai") -> None:
    path.write_text(
        json.dumps({
            "type": "user", "timestamp": ts,
            "message": {"role": "user", "content": content},
        }) + "\n"
        + json.dumps({
            "type": "assistant", "timestamp": ts.replace(":36", ":40"),
            "message": {"role": "assistant", "content": "Got it."},
        }) + "\n"
    )


def _setup(tmp_path: Path):
    project = tmp_path / "claude-project"
    project.mkdir()
    cfg_path = tmp_path / "tiger-memory.config.yaml"
    cfg_path.write_text(dedent(f"""\
        agent:
          name: TestTiger
          role: "Test consumer."
        store:
          root: {tmp_path}/memory
        sources:
          - kind: claude_code
            project_path: {project}
        summarizer:
          backend: anthropic
          model: claude-opus-4-7
          prompts: default/v1
        rebuild:
          lock_path: {tmp_path}/test.lock
          idle_threshold_hours: 0
    """))
    cfg = load_config(cfg_path)
    store = Store(cfg.store.root)
    return cfg, store, project


def _backdate(path: Path, hours: float) -> None:
    new_time = time.time() - hours * 3600
    import os
    os.utime(path, (new_time, new_time))


def test_bootstrap_writes_short_and_archive(tmp_path: Path) -> None:
    cfg, store, project = _setup(tmp_path)
    uid = "3d7a9a41-079b-4d6b-8de1-33ee112bb155"
    _make_jsonl(project / f"{uid}.jsonl", uid)
    _backdate(project / f"{uid}.jsonl", hours=3)  # past idle threshold

    rc = bootstrap(cfg, store, dry_run=False,
                   summarizer_override=MockSummarizer())
    assert rc == 0

    # Archive + short written with matching filenames
    archives = list(store.paths.archive.glob("*.md"))
    shorts = [
        f for f in store.paths.journal.glob("*.md")
        if f.name.startswith("2026")
    ]
    assert len(archives) == 1
    assert any(uid in f.name for f in archives)
    assert any(uid in f.name for f in shorts)


def test_rebuild_idempotent(tmp_path: Path) -> None:
    cfg, store, project = _setup(tmp_path)
    uid = "deadbeef-1234-5678-9012-345678901234"
    _make_jsonl(project / f"{uid}.jsonl", uid)
    _backdate(project / f"{uid}.jsonl", hours=3)

    rebuild(cfg, store, summarizer_override=MockSummarizer())
    archives_first = sorted(store.paths.archive.glob("*.md"))

    rebuild(cfg, store, summarizer_override=MockSummarizer())
    archives_second = sorted(store.paths.archive.glob("*.md"))
    assert [a.name for a in archives_first] == [a.name for a in archives_second]


def test_rebuild_creates_daily_rollup(tmp_path: Path) -> None:
    cfg, store, project = _setup(tmp_path)
    uid = "deadbeef-1234-5678-9012-345678901234"
    _make_jsonl(project / f"{uid}.jsonl", uid)
    _backdate(project / f"{uid}.jsonl", hours=3)

    rebuild(cfg, store, summarizer_override=MockSummarizer())
    daily = store.daily_for_date("20260514")
    assert daily is not None
    assert "daily" in daily.name


def test_rebuild_creates_weekly_and_monthly(tmp_path: Path) -> None:
    cfg, store, project = _setup(tmp_path)
    uid = "deadbeef-1234-5678-9012-345678901234"
    _make_jsonl(project / f"{uid}.jsonl", uid)
    _backdate(project / f"{uid}.jsonl", hours=3)

    rebuild(cfg, store, summarizer_override=MockSummarizer())
    # ISO week for 2026-05-14 is week of 2026-05-11 (Mon)
    weekly = store.weekly_for_monday("20260511")
    assert weekly is not None
    monthly = store.monthly_for_yyyymm("202605")
    assert monthly is not None


def test_state_json_written(tmp_path: Path) -> None:
    cfg, store, project = _setup(tmp_path)
    uid = "deadbeef-1234-5678-9012-345678901234"
    _make_jsonl(project / f"{uid}.jsonl", uid)
    _backdate(project / f"{uid}.jsonl", hours=3)

    rebuild(cfg, store, summarizer_override=MockSummarizer())
    state = store.read_state()
    assert state is not None
    assert state["agent"] == "TestTiger"
    assert state["last_rebuild_at"]


def test_briefing_built(tmp_path: Path) -> None:
    cfg, store, project = _setup(tmp_path)
    uid = "deadbeef-1234-5678-9012-345678901234"
    _make_jsonl(project / f"{uid}.jsonl", uid)
    _backdate(project / f"{uid}.jsonl", hours=3)

    rebuild(cfg, store, summarizer_override=MockSummarizer())
    manifest = store.paths.briefing / "MANIFEST.md"
    assert manifest.exists()
    text = manifest.read_text()
    assert "TestTiger" in text
    assert (store.paths.briefing / "recent").is_dir()
    assert (store.paths.briefing / "daily").is_dir()


def test_active_session_deferred(tmp_path: Path) -> None:
    cfg, store, project = _setup(tmp_path)
    # idle_threshold_hours=0 — but make the file very fresh by overriding
    # the config.
    cfg_path = tmp_path / "fresh.yaml"
    cfg_path.write_text(dedent(f"""\
        agent: {{name: T, role: T}}
        store: {{root: {tmp_path}/memory2}}
        sources:
          - kind: claude_code
            project_path: {project}
        summarizer: {{backend: anthropic, model: claude-opus-4-7, prompts: default/v1}}
        rebuild:
          lock_path: {tmp_path}/test2.lock
          idle_threshold_hours: 2.0
    """))
    cfg2 = load_config(cfg_path)
    store2 = Store(cfg2.store.root)
    uid = "deadbeef-1234-5678-9012-345678901235"
    _make_jsonl(project / f"{uid}.jsonl", uid)
    # File is fresh (mtime ≈ now); should be skipped as active
    rebuild(cfg2, store2, summarizer_override=MockSummarizer())
    archives = list(store2.paths.archive.glob("*.md"))
    assert len(archives) == 0


def test_lock_serializes_concurrent_rebuilds(tmp_path: Path) -> None:
    cfg, store, project = _setup(tmp_path)
    # Pre-hold the lock with our own pid.
    cfg.rebuild.lock_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.rebuild.lock_path.write_text("999999")  # dead PID — should be reclaimed
    rc = rebuild(cfg, store, summarizer_override=MockSummarizer())
    assert rc == 0
    # Now hold with our live PID.
    import os
    cfg.rebuild.lock_path.write_text(str(os.getpid()))
    rc = rebuild(cfg, store, summarizer_override=MockSummarizer())
    assert rc == 0  # graceful no-op
    cfg.rebuild.lock_path.unlink()

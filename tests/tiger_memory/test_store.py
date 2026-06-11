"""Tests for tiger_memory.store."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest

from tigerharness.tiger_memory.store import (
    DAILY_RE,
    MONTHLY_RE,
    SHORT_RE,
    WEEKLY_RE,
    Store,
)


def test_init_layout(tmp_path: Path) -> None:
    store = Store(tmp_path / "mem")
    assert not store.exists()
    store.init_layout()
    assert store.exists()
    assert store.paths.archive.is_dir()
    assert store.paths.journal.is_dir()
    assert store.paths.briefing.is_dir()
    # .gitkeep in archive/ and journal/ (git-trackable), not in briefing/
    assert (store.paths.archive / ".gitkeep").exists()
    assert (store.paths.journal / ".gitkeep").exists()
    assert not (store.paths.briefing / ".gitkeep").exists()


def test_init_layout_gitkeep_idempotent(tmp_path: Path) -> None:
    """Calling init_layout twice doesn't fail or overwrite .gitkeep."""
    store = Store(tmp_path / "mem")
    store.init_layout()
    store.init_layout()  # second call
    assert (store.paths.archive / ".gitkeep").exists()
    assert (store.paths.journal / ".gitkeep").exists()


def test_atomic_write_no_partial_on_crash(tmp_path: Path) -> None:
    store = Store(tmp_path / "mem")
    store.init_layout()
    target = store.paths.journal / "hello.txt"
    store.atomic_write(target, "world\n")
    assert target.read_text() == "world\n"
    # No leftover .tmp file
    assert not target.with_suffix(target.suffix + ".tmp").exists()


def test_atomic_swap_dir(tmp_path: Path) -> None:
    store = Store(tmp_path / "mem")
    store.init_layout()
    target = tmp_path / "target"
    target.mkdir()
    (target / "old.txt").write_text("old")
    new = tmp_path / "newdir"
    new.mkdir()
    (new / "new.txt").write_text("new")
    store.atomic_swap_dir(new, target)
    assert not (target / "old.txt").exists()
    assert (target / "new.txt").read_text() == "new"


def test_filename_patterns_match_design() -> None:
    short = "20260514-082136-abcdef12-3456-7890-1234-567890abcdef.md"
    daily = "20260514-daily-7b9e22.md"
    weekly = "20260511-week-12cd45.md"
    monthly = "202605-month-9f02ab.md"
    assert SHORT_RE.match(short)
    assert DAILY_RE.match(daily)
    assert WEEKLY_RE.match(weekly)
    assert MONTHLY_RE.match(monthly)
    # No cross-matches
    assert not DAILY_RE.match(short)
    assert not SHORT_RE.match(daily)
    assert not WEEKLY_RE.match(monthly)
    assert not MONTHLY_RE.match(weekly)


def test_find_archive_by_uuid(tmp_path: Path) -> None:
    store = Store(tmp_path / "mem")
    store.init_layout()
    uid = "abcdef12-3456-7890-1234-567890abcdef"
    f = store.paths.archive / f"20260514-082136-{uid}.md"
    f.write_text("dummy")
    assert store.find_archive(uid) == f
    assert store.find_archive("nonexistent-uuid") is None


def test_find_short_excludes_rollups(tmp_path: Path) -> None:
    store = Store(tmp_path / "mem")
    store.init_layout()
    uid = "abcdef12-3456-7890-1234-567890abcdef"
    short = store.paths.journal / f"20260514-082136-{uid}.md"
    daily = store.paths.journal / f"20260514-daily-{uid}.md"
    short.write_text("s")
    daily.write_text("d")
    assert store.find_short(uid) == short  # daily excluded


def test_find_short_only_rollup_returns_none(tmp_path: Path) -> None:
    """Only a rollup exists for the uuid -> None.

    Also the deterministic pin for the loop's skip-and-continue arc
    (store.py 146->145): the mixed-file test above only exercises it
    when glob yields the rollup BEFORE the short, and glob order is
    filesystem-dependent -- it did on the dev box (aarch64/NixOS) and
    did not on ubuntu CI, which is exactly how the first CI run landed
    at 99.99%. With ONLY a non-matching file present, the arc executes
    on every platform regardless of directory order."""
    store = Store(tmp_path / "mem")
    store.init_layout()
    uid = "abcdef12-3456-7890-1234-567890abcdef"
    (store.paths.journal / f"20260514-daily-{uid}.md").write_text("d")
    assert store.find_short(uid) is None


def test_filename_derivation() -> None:
    uid = "abcdef12-3456-7890-1234-567890abcdef"
    dt = datetime(2026, 5, 14, 8, 21, 36)
    assert Store.short_filename(dt, uid) == f"20260514-082136-{uid}.md"
    assert Store.daily_filename("20260514", uid) == f"20260514-daily-{uid}.md"
    assert Store.weekly_filename("20260511", uid) == f"20260511-week-{uid}.md"
    assert Store.monthly_filename("202605", uid) == f"202605-month-{uid}.md"


def test_working_days_sorted_desc(tmp_path: Path) -> None:
    store = Store(tmp_path / "mem")
    store.init_layout()
    for date in ["20260514", "20260512", "20260513"]:
        (store.paths.journal / f"{date}-082136-{uuid4()}.md").write_text("s")
    # Add a daily — should not contribute a working day on its own.
    (store.paths.journal / f"20260601-daily-{uuid4()}.md").write_text("d")
    assert store.working_days() == ["20260514", "20260513", "20260512"]


def test_lock_acquire_and_release(tmp_path: Path) -> None:
    store = Store(tmp_path / "mem")
    lock = tmp_path / "lock"
    with store.lock(lock) as acquired:
        assert acquired is True
        assert lock.exists()
    assert not lock.exists()


def test_lock_skips_when_held_by_live_pid(tmp_path: Path) -> None:
    store = Store(tmp_path / "mem")
    lock = tmp_path / "lock"
    # Pretend a live process holds the lock.
    lock.write_text(str(os.getpid()))
    with store.lock(lock) as acquired:
        assert acquired is False
    # We must not have removed the lock — we didn't own it.
    assert lock.exists()
    lock.unlink()


def test_lock_reclaims_dead_pid(tmp_path: Path) -> None:
    store = Store(tmp_path / "mem")
    lock = tmp_path / "lock"
    # PID 999999 almost certainly doesn't exist.
    lock.write_text("999999")
    with store.lock(lock) as acquired:
        assert acquired is True
    assert not lock.exists()


def test_lock_reclaims_after_timeout(tmp_path: Path) -> None:
    store = Store(tmp_path / "mem")
    lock = tmp_path / "lock"
    lock.write_text(str(os.getpid()))
    # Backdate the file to look ancient.
    old = time.time() - 3600
    os.utime(lock, (old, old))
    # timeout_minutes=1 → 60 sec, so file is stale.
    with store.lock(lock, timeout_minutes=1) as acquired:
        assert acquired is True


def test_state_write_read(tmp_path: Path) -> None:
    store = Store(tmp_path / "mem")
    store.init_layout()
    store.write_state({"foo": 1, "bar": "baz"})
    assert store.read_state() == {"foo": 1, "bar": "baz"}


def test_read_state_returns_none_when_missing(tmp_path: Path) -> None:
    store = Store(tmp_path / "mem")
    store.init_layout()
    assert store.read_state() is None

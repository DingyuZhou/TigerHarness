"""Tests for tiger_memory.store."""
from __future__ import annotations

import os
import time
from pathlib import Path

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
    assert store.paths.journal.is_dir()
    assert store.paths.briefing.is_dir()
    # .gitkeep in journal/ (git-trackable), not in briefing/
    assert (store.paths.journal / ".gitkeep").exists()
    assert not (store.paths.briefing / ".gitkeep").exists()


def test_init_layout_does_not_recreate_retired_archive(tmp_path: Path) -> None:
    """GAP-1 regression: the retired ``archive/`` dir must NOT be (re)created.

    §3/§9 retired the detailed-summary archive entirely; the live layout is
    only ``journal/`` + ``briefing/``. ``init_layout`` (called by every
    init/rebuild/pin) must not resurrect ``archive/``, and ``exists()`` must
    not gate on it. This pins the dir shut so it cannot silently return.
    """
    store = Store(tmp_path / "mem")
    store.init_layout()
    store.init_layout()  # idempotent second call must not create it either
    assert not store.paths.archive.exists()
    # store still reports as existing on the journal-only live layout.
    assert store.exists()


def test_init_layout_gitkeep_idempotent(tmp_path: Path) -> None:
    """Calling init_layout twice doesn't fail or overwrite .gitkeep."""
    store = Store(tmp_path / "mem")
    store.init_layout()
    store.init_layout()  # second call
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

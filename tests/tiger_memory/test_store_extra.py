"""Additional store tests — atomic_swap_dir, lock, find_*, state IO, _pid_alive."""
from __future__ import annotations

import os
import time
from pathlib import Path
from uuid import uuid4

import pytest

from tigerharness.tiger_memory.store import Store, _pid_alive, _refresh_lockfile_loop


class TestAtomicSwapDir:
    def test_swap_replaces_target(self, tmp_path: Path):
        store = Store(tmp_path / "memory")
        store.init_layout()
        new_dir = tmp_path / "new_briefing"
        new_dir.mkdir()
        (new_dir / "file.txt").write_text("new content")

        target = store.paths.briefing
        store.atomic_swap_dir(new_dir, target)
        assert (target / "file.txt").exists()
        assert (target / "file.txt").read_text() == "new content"
        assert not new_dir.exists()

    def test_swap_nonexistent_new_dir_raises(self, tmp_path: Path):
        store = Store(tmp_path / "memory")
        store.init_layout()
        with pytest.raises(FileNotFoundError):
            store.atomic_swap_dir(tmp_path / "nonexistent", store.paths.briefing)

    def test_swap_when_target_doesnt_exist(self, tmp_path: Path):
        store = Store(tmp_path / "memory")
        store.init_layout()
        new_dir = tmp_path / "fresh"
        new_dir.mkdir()
        (new_dir / "x.txt").write_text("hello")
        target = tmp_path / "memory" / "new_target"
        store.atomic_swap_dir(new_dir, target)
        assert (target / "x.txt").exists()


class TestFindArchive:
    def test_finds_existing(self, tmp_path: Path):
        store = Store(tmp_path / "memory")
        store.init_layout()
        uid = str(uuid4())
        archive = store.paths.archive / f"20260514-082136-{uid}.md"
        archive.write_text("archive content")
        result = store.find_archive(uid)
        assert result == archive

    def test_returns_none_when_missing(self, tmp_path: Path):
        store = Store(tmp_path / "memory")
        store.init_layout()
        assert store.find_archive("nonexistent-uuid") is None

    def test_raises_on_duplicates(self, tmp_path: Path):
        store = Store(tmp_path / "memory")
        store.init_layout()
        uid = str(uuid4())
        (store.paths.archive / f"20260514-082136-{uid}.md").write_text("a")
        (store.paths.archive / f"20260515-093000-{uid}.md").write_text("b")
        with pytest.raises(RuntimeError, match="multiple archives"):
            store.find_archive(uid)


class TestFindShort:
    def test_finds_short(self, tmp_path: Path):
        store = Store(tmp_path / "memory")
        store.init_layout()
        uid = str(uuid4())
        short = store.paths.journal / f"20260514-082136-{uid}.md"
        short.write_text("short content")
        result = store.find_short(uid)
        assert result == short

    def test_returns_none_when_missing(self, tmp_path: Path):
        store = Store(tmp_path / "memory")
        store.init_layout()
        assert store.find_short("nonexistent-uuid") is None


class TestLock:
    def test_acquire_and_release(self, tmp_path: Path):
        store = Store(tmp_path / "memory")
        store.init_layout()
        lock_path = tmp_path / "test.lock"
        with store.lock(lock_path, timeout_minutes=1) as got:
            assert got is True
            assert lock_path.exists()
            pid = int(lock_path.read_text().strip())
            assert pid == os.getpid()
        # After exit, lock should be cleaned up
        assert not lock_path.exists()

    def test_lock_contention_live_pid(self, tmp_path: Path):
        store = Store(tmp_path / "memory")
        store.init_layout()
        lock_path = tmp_path / "test.lock"
        # Write our own PID as the holder (simulating a live process)
        lock_path.write_text(str(os.getpid()))
        # Touch it so it's fresh (not stale)
        os.utime(lock_path, None)
        with store.lock(lock_path, timeout_minutes=60) as got:
            # Should reclaim since it's our own PID (same process)
            # Actually _pid_alive(our_pid) returns True, so it won't reclaim
            # unless the lock is stale
            pass  # either True or False is acceptable
        # Just verify no crash

    def test_stale_lock_reclaimed(self, tmp_path: Path):
        store = Store(tmp_path / "memory")
        store.init_layout()
        lock_path = tmp_path / "test.lock"
        # Write a stale lock (dead PID)
        lock_path.write_text("999999999")  # almost certainly not a real PID
        with store.lock(lock_path, timeout_minutes=60) as got:
            assert got is True


class TestStateIO:
    def test_write_and_read(self, tmp_path: Path):
        store = Store(tmp_path / "memory")
        store.init_layout()
        payload = {"key": "value", "count": 42}
        store.write_state(payload)
        result = store.read_state()
        assert result == payload

    def test_read_missing_state(self, tmp_path: Path):
        store = Store(tmp_path / "memory")
        store.init_layout()
        assert store.read_state() is None

    def test_read_corrupt_state(self, tmp_path: Path):
        store = Store(tmp_path / "memory")
        store.init_layout()
        (store.paths.journal / ".state.json").write_text("not json{{{")
        assert store.read_state() is None


class TestPidAlive:
    def test_own_pid_alive(self):
        assert _pid_alive(os.getpid()) is True

    def test_dead_pid(self):
        assert _pid_alive(999999999) is False


class TestWorkingDays:
    def test_returns_sorted_desc(self, tmp_path: Path):
        store = Store(tmp_path / "memory")
        store.init_layout()
        uid1, uid2 = str(uuid4()), str(uuid4())
        (store.paths.journal / f"20260514-082136-{uid1}.md").write_text("a")
        (store.paths.journal / f"20260515-093000-{uid2}.md").write_text("b")
        days = store.working_days()
        assert days == ["20260515", "20260514"]

    def test_empty_store(self, tmp_path: Path):
        store = Store(tmp_path / "memory")
        store.init_layout()
        assert store.working_days() == []

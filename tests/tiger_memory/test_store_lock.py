"""Tests for store.py lock internals — refresh loop, stale-by-age, retry race."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from tigerharness.tiger_memory.store import Store, _refresh_lockfile_loop, _pid_alive


class TestRefreshLockfileLoop:
    def test_loop_touches_file(self, tmp_path: Path):
        lock = tmp_path / "test.lock"
        lock.write_text(str(os.getpid()))
        stop = threading.Event()
        mtime_before = lock.stat().st_mtime
        # Run with very short interval
        t = threading.Thread(target=_refresh_lockfile_loop, args=(lock, stop, 0.05))
        t.start()
        time.sleep(0.15)  # let it tick a few times
        stop.set()
        t.join(timeout=1)
        mtime_after = lock.stat().st_mtime
        assert mtime_after >= mtime_before

    def test_loop_exits_when_file_deleted(self, tmp_path: Path):
        lock = tmp_path / "test.lock"
        lock.write_text(str(os.getpid()))
        stop = threading.Event()
        t = threading.Thread(target=_refresh_lockfile_loop, args=(lock, stop, 0.05))
        t.start()
        time.sleep(0.05)
        lock.unlink()  # delete the file
        t.join(timeout=2)
        assert not t.is_alive()

    def test_loop_exits_on_stop(self, tmp_path: Path):
        lock = tmp_path / "test.lock"
        lock.write_text(str(os.getpid()))
        stop = threading.Event()
        t = threading.Thread(target=_refresh_lockfile_loop, args=(lock, stop, 60))
        t.start()
        stop.set()
        t.join(timeout=2)
        assert not t.is_alive()


class TestLockStaleByAge:
    def test_stale_by_age_reclaimed(self, tmp_path: Path):
        store = Store(tmp_path / "memory")
        store.init_layout()
        lock = tmp_path / "test.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        # Create a stale lock with our PID but old mtime
        lock.write_text(str(os.getpid()))
        old_time = time.time() - 7200  # 2 hours ago
        os.utime(lock, (old_time, old_time))
        # timeout_minutes=1 → anything older than 60s is stale
        with store.lock(lock, timeout_minutes=1) as got:
            assert got is True


class TestLockRetryOnRace:
    def test_lock_raced_removal(self, tmp_path: Path):
        """When a lock file disappears between check and reclaim."""
        store = Store(tmp_path / "memory")
        store.init_layout()
        lock = tmp_path / "test.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        # Don't create the lock file — first acquisition should succeed
        with store.lock(lock, timeout_minutes=1) as got:
            assert got is True


class TestLockAtomicSwapRollback:
    def test_swap_dir_rollback_on_rename_failure(self, tmp_path: Path):
        store = Store(tmp_path / "memory")
        store.init_layout()
        target = store.paths.briefing
        new_dir = tmp_path / "new_dir"
        new_dir.mkdir()
        (new_dir / "test.txt").write_text("content")
        # Make os.rename fail on the second call (new_dir → target)
        real_rename = os.rename
        call_count = [0]
        def fake_rename(src, dst):
            call_count[0] += 1
            if call_count[0] == 2:
                raise OSError("rename failed")
            return real_rename(src, dst)
        with patch("tigerharness.tiger_memory.store.os.rename", side_effect=fake_rename):
            with pytest.raises(OSError, match="rename failed"):
                store.atomic_swap_dir(new_dir, target)
        # Target should be restored from backup
        assert target.exists()

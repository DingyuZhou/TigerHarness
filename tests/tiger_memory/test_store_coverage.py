"""Coverage-push tests for store.py — targeting:
98 (backup exists in atomic_swap_dir), 262 (non-EEXIST OSError in lock),
267-269 (FileNotFoundError race → retry), 282-283 (unlink FNFE in reclaim),
316-318 (_refresh_lockfile_loop OSError continue).
"""
from __future__ import annotations

import errno
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from tigerharness.tiger_memory.store import Store, _refresh_lockfile_loop


class TestAtomicSwapDirBackupExists:
    """Line 98: backup from a previous swap exists → rmtree'd first."""

    def test_old_backup_removed(self, tmp_path: Path):
        store = Store(tmp_path / "mem")
        store.init_layout()

        target = tmp_path / "dest"
        target.mkdir()
        (target / "old.txt").write_text("old")

        # Create a stale backup from a prior swap
        backup = tmp_path / "dest.old"
        backup.mkdir()
        (backup / "stale.txt").write_text("stale")

        new_dir = tmp_path / "new"
        new_dir.mkdir()
        (new_dir / "fresh.txt").write_text("fresh")

        store.atomic_swap_dir(new_dir, target)
        assert (target / "fresh.txt").exists()
        assert not backup.exists()


class TestLockNonEExistOSError:
    """Line 262: OSError with errno != EEXIST → re-raised."""

    def test_non_eexist_raises(self, tmp_path: Path):
        store = Store(tmp_path / "mem")
        store.init_layout()
        lock_path = tmp_path / "lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        # Make os.open raise with EACCES (not EEXIST)
        orig_open = os.open

        def bad_open(path, flags, *a, **kw):
            if "lock" in str(path):
                e = OSError()
                e.errno = errno.EACCES
                raise e
            return orig_open(path, flags, *a, **kw)

        with patch("os.open", side_effect=bad_open):
            with pytest.raises(OSError):
                store._try_acquire_lock(lock_path, timeout_minutes=1)


class TestLockStatRace:
    """Lines 267-269: FileNotFoundError on stat → retry."""

    def test_stat_race_retries(self, tmp_path: Path):
        store = Store(tmp_path / "mem")
        store.init_layout()
        lock_path = tmp_path / "lock"

        # Create lock file that will disappear when we stat it
        lock_path.write_text(str(os.getpid()))

        call_count = 0
        orig_stat = Path.stat

        def racing_stat(self, *a, **kw):
            nonlocal call_count
            if self == lock_path and call_count == 0:
                call_count += 1
                # Lock exists for os.open (EEXIST) but then stat fails
                raise FileNotFoundError("race!")
            return orig_stat(self, *a, **kw)

        # First call: os.open → EEXIST, then stat → FNFE, then retry → acquire
        lock_path.unlink()  # Remove so retry can create fresh
        with patch.object(Path, "stat", racing_stat):
            result = store._try_acquire_lock(lock_path, timeout_minutes=1)

        assert result is True


class TestLockUnlinkFNFEDuringReclaim:
    """Lines 282-283: unlink FNFE during stale lock reclaim."""

    def test_unlink_fnfe_swallowed(self, tmp_path: Path):
        store = Store(tmp_path / "mem")
        store.init_layout()
        lock_path = tmp_path / "lock"

        # Create stale lock (dead PID)
        lock_path.write_text("999999")
        old_time = time.time() - 3600
        os.utime(lock_path, (old_time, old_time))

        # Patch unlink to raise FNFE — the code swallows it and retries
        unlink_calls = 0
        orig_unlink = Path.unlink

        def racing_unlink(self, *a, **kw):
            nonlocal unlink_calls
            if str(self) == str(lock_path):
                unlink_calls += 1
                if unlink_calls == 1:
                    # Also actually remove so retry can acquire
                    try:
                        orig_unlink(self, *a, **kw)
                    except FileNotFoundError:
                        pass
                    raise FileNotFoundError("raced")
            return orig_unlink(self, *a, **kw)

        with patch("tigerharness.tiger_memory.store._pid_alive", return_value=False), \
             patch.object(Path, "unlink", racing_unlink):
            result = store._try_acquire_lock(lock_path, timeout_minutes=1)

        assert result is True
        assert unlink_calls >= 1


class TestRefreshLoopOSError:
    """Lines 316-318: _refresh_lockfile_loop OSError on utime → continue."""

    def test_oserror_continues(self, tmp_path: Path):
        lock_path = tmp_path / "lock"
        lock_path.write_text("pid")
        stop = threading.Event()

        call_count = 0
        orig_utime = os.utime

        def flaky_utime(path, times):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("disk hiccup")
            # Second call succeeds, then stop
            stop.set()
            return orig_utime(path, times)

        with patch("os.utime", side_effect=flaky_utime):
            _refresh_lockfile_loop(lock_path, stop, interval_sec=0.01)

        assert call_count >= 2  # first call errored, second succeeded

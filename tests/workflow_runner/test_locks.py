"""Unit tests for ``tigerharness.workflow_runner.locks``."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from tigerharness.workflow_runner import locks as locks_mod
from tigerharness.workflow_runner.atomic import write_json_atomic
from tigerharness.workflow_runner.locks import (
    LockHeldError,
    PidInfo,
    acquire_task_lock,
    heartbeat,
    is_stale,
    read_pid_info,
    write_pid,
)


# --------------------------------------------------------------------------- #
# acquire_task_lock
# --------------------------------------------------------------------------- #


def test_acquire_task_lock_creates_dir_and_lock_file(tmp_path):
    task_dir = tmp_path / "task-abc"
    with acquire_task_lock(task_dir):
        assert task_dir.is_dir()
        assert (task_dir / ".lock").exists()


def test_acquire_task_lock_contended_raises(tmp_path):
    """Second process must get LockHeldError when the first holds."""
    task_dir = tmp_path / "task-xyz"
    task_dir.mkdir()

    holder = textwrap.dedent(f"""
        import sys, time
        from tigerharness.workflow_runner.locks import acquire_task_lock
        with acquire_task_lock({str(task_dir)!r}, blocking=False):
            sys.stdout.write("HELD\\n"); sys.stdout.flush()
            time.sleep(2.0)
    """)
    proc = subprocess.Popen(
        [sys.executable, "-c", holder],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert proc.stdout is not None
        assert "HELD" in proc.stdout.readline()
        with pytest.raises(LockHeldError):
            with acquire_task_lock(task_dir, blocking=False):
                pass  # pragma: no cover
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_acquire_task_lock_releases_after_dead_process(tmp_path):
    """When the prior holder dies, flock is released by the kernel and
    a new acquire succeeds immediately."""
    task_dir = tmp_path / "task-die"
    task_dir.mkdir()

    holder = textwrap.dedent(f"""
        import sys
        from tigerharness.workflow_runner.locks import acquire_task_lock
        with acquire_task_lock({str(task_dir)!r}, blocking=False):
            sys.stdout.write("HELD\\n"); sys.stdout.flush()
            # exit immediately
    """)
    proc = subprocess.run(
        [sys.executable, "-c", holder],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        timeout=5,
    )
    assert "HELD" in proc.stdout
    # The OS has now released the lock; we should be able to acquire.
    with acquire_task_lock(task_dir, blocking=False):
        pass


# --------------------------------------------------------------------------- #
# write_pid / heartbeat / read_pid_info
# --------------------------------------------------------------------------- #


def test_write_pid_writes_atomic_json(tmp_path):
    info = write_pid(tmp_path, pid=999, now="2026-05-28T12:00:00Z")
    assert info.pid == 999
    pid_file = tmp_path / ".pid"
    data = json.loads(pid_file.read_text())
    assert data == {
        "pid": 999,
        "started_at": "2026-05-28T12:00:00Z",
        "last_heartbeat": "2026-05-28T12:00:00Z",
    }


def test_write_pid_default_uses_current_pid(tmp_path):
    info = write_pid(tmp_path)
    assert info.pid == os.getpid()


def test_heartbeat_updates_only_last_heartbeat(tmp_path):
    write_pid(tmp_path, pid=999, now="2026-05-28T12:00:00Z")
    updated = heartbeat(tmp_path, now="2026-05-28T12:05:00Z")
    assert updated.pid == 999
    assert updated.started_at == "2026-05-28T12:00:00Z"
    assert updated.last_heartbeat == "2026-05-28T12:05:00Z"
    on_disk = json.loads((tmp_path / ".pid").read_text())
    assert on_disk["last_heartbeat"] == "2026-05-28T12:05:00Z"
    assert on_disk["started_at"] == "2026-05-28T12:00:00Z"


def test_heartbeat_default_uses_now_iso(tmp_path, monkeypatch):
    write_pid(tmp_path, pid=1, now="2026-05-28T12:00:00Z")
    monkeypatch.setattr(locks_mod, "now_iso", lambda: "2099-01-01T00:00:00Z")
    info = heartbeat(tmp_path)
    assert info.last_heartbeat == "2099-01-01T00:00:00Z"


def test_heartbeat_without_pid_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        heartbeat(tmp_path)


def test_heartbeat_rejects_non_object_pid_file(tmp_path):
    # Write a JSON array where an object is expected.
    write_json_atomic(tmp_path / ".pid", [])
    with pytest.raises(ValueError):
        heartbeat(tmp_path)


def test_read_pid_info_missing(tmp_path):
    assert read_pid_info(tmp_path) is None


def test_read_pid_info_returns_snapshot(tmp_path):
    write_pid(tmp_path, pid=42, now="2026-05-28T12:00:00Z")
    info = read_pid_info(tmp_path)
    assert info is not None
    assert info.pid == 42
    assert info.started_at == "2026-05-28T12:00:00Z"


def test_read_pid_info_corrupt(tmp_path):
    write_json_atomic(tmp_path / ".pid", "junk")
    with pytest.raises(ValueError):
        read_pid_info(tmp_path)


def test_pid_info_equality_and_dict():
    a = PidInfo(pid=1, started_at="t", last_heartbeat="t")
    b = PidInfo(pid=1, started_at="t", last_heartbeat="t")
    assert a == b
    assert a == PidInfo.from_dict(a.to_dict())
    assert a != "not-a-pidinfo"


def test_pid_info_from_dict_rejects_garbage():
    with pytest.raises(ValueError):
        PidInfo.from_dict({"pid": "abc"})
    with pytest.raises(ValueError):
        PidInfo.from_dict({})


# --------------------------------------------------------------------------- #
# is_stale
# --------------------------------------------------------------------------- #


def test_is_stale_no_pid_file(tmp_path):
    assert is_stale(tmp_path, max_silence_sec=60) is False


def test_is_stale_pid_dead_returns_true(tmp_path):
    """Spawn a short-lived helper, then check its (now dead) pid."""
    proc = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        stdout=subprocess.PIPE, text=True, check=True,
    )
    dead_pid = int(proc.stdout.strip())
    # Make sure the pid really is gone.
    time.sleep(0.05)
    # ~ probability of pid reuse this fast is extremely low on Linux.
    write_pid(tmp_path, pid=dead_pid, now="2026-05-28T12:00:00Z")
    assert is_stale(tmp_path, max_silence_sec=999999) is True


def test_is_stale_pid_alive_recent_heartbeat(tmp_path, monkeypatch):
    write_pid(tmp_path, pid=os.getpid(), now="2026-05-28T12:00:00Z")
    when = dt.datetime(
        2026, 5, 28, 12, 0, 5, tzinfo=dt.timezone.utc
    ).timestamp()
    assert is_stale(tmp_path, max_silence_sec=60, now_epoch=when) is False


def test_is_stale_pid_alive_stale_heartbeat(tmp_path):
    write_pid(tmp_path, pid=os.getpid(), now="2026-05-28T12:00:00Z")
    when = dt.datetime(
        2026, 5, 28, 12, 30, 0, tzinfo=dt.timezone.utc
    ).timestamp()
    assert is_stale(tmp_path, max_silence_sec=60, now_epoch=when) is True


def test_is_stale_rejects_non_positive_threshold(tmp_path):
    with pytest.raises(ValueError):
        is_stale(tmp_path, max_silence_sec=0)
    with pytest.raises(ValueError):
        is_stale(tmp_path, max_silence_sec=-1)


def test_is_stale_corrupt_pid_treated_as_stale(tmp_path):
    write_json_atomic(tmp_path / ".pid", {"pid": "not-int"})
    assert is_stale(tmp_path, max_silence_sec=60) is True


def test_is_stale_unparseable_heartbeat_treated_as_stale(tmp_path):
    # Write a manually-corrupted heartbeat field (bypassing write_pid).
    write_json_atomic(tmp_path / ".pid", {
        "pid": os.getpid(),
        "started_at": "2026-05-28T12:00:00Z",
        "last_heartbeat": "never",
    })
    assert is_stale(tmp_path, max_silence_sec=60) is True


def test_iso_to_epoch_handles_z_and_offset():
    assert locks_mod._iso_to_epoch("2026-05-28T12:00:00Z") == \
        dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.timezone.utc).timestamp()
    assert locks_mod._iso_to_epoch("2026-05-28T12:00:00+00:00") == \
        dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.timezone.utc).timestamp()
    # Naive timestamps get assigned UTC.
    naive = locks_mod._iso_to_epoch("2026-05-28T12:00:00")
    assert naive == dt.datetime(
        2026, 5, 28, 12, 0, 0, tzinfo=dt.timezone.utc
    ).timestamp()
    assert locks_mod._iso_to_epoch("nope") is None


def test_pid_alive_zero_pid_false():
    assert locks_mod._pid_alive(0) is False
    assert locks_mod._pid_alive(-1) is False


def test_pid_alive_eperm_treated_as_alive(monkeypatch):
    """Simulate ``EPERM`` (process exists but we can't signal it)."""
    import errno as _errno

    def fake_kill(_pid, _sig):
        raise OSError(_errno.EPERM, "nope")

    monkeypatch.setattr(locks_mod.os, "kill", fake_kill)
    assert locks_mod._pid_alive(12345) is True


def test_pid_alive_esrch_treated_as_dead(monkeypatch):
    import errno as _errno

    def fake_kill(_pid, _sig):
        raise OSError(_errno.ESRCH, "no such")

    monkeypatch.setattr(locks_mod.os, "kill", fake_kill)
    assert locks_mod._pid_alive(12345) is False

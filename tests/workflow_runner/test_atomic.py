"""Unit tests for ``tigerharness.workflow_runner.atomic``."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from tigerharness.workflow_runner.atomic import (
    LockContendedError,
    flocked,
    read_json,
    write_json_atomic,
)


# --------------------------------------------------------------------------- #
# JSON I/O
# --------------------------------------------------------------------------- #


def test_write_then_read_round_trip(tmp_path):
    target = tmp_path / "sub" / "data.json"
    payload = {"a": 1, "b": [1, 2, 3], "c": {"nested": True}}
    write_json_atomic(target, payload)
    assert read_json(target) == payload


def test_write_atomic_replaces_existing(tmp_path):
    target = tmp_path / "data.json"
    write_json_atomic(target, {"v": 1})
    write_json_atomic(target, {"v": 2})
    assert read_json(target) == {"v": 2}


def test_write_atomic_creates_parent_dirs(tmp_path):
    target = tmp_path / "a" / "b" / "c" / "data.json"
    write_json_atomic(target, {"ok": True})
    assert target.exists()
    assert read_json(target) == {"ok": True}


def test_write_atomic_does_not_leak_tmp_files(tmp_path):
    target = tmp_path / "data.json"
    write_json_atomic(target, {"x": 1})
    write_json_atomic(target, {"x": 2})
    # Only the target file should remain in the directory; no leftover
    # ``.data.json.<random>.tmp`` siblings.
    entries = sorted(p.name for p in tmp_path.iterdir())
    assert entries == ["data.json"]


def test_write_atomic_indent_and_sort(tmp_path):
    target = tmp_path / "data.json"
    write_json_atomic(target, {"b": 1, "a": 2}, indent=None, sort_keys=True)
    text = target.read_text()
    assert text.startswith('{"a": 2, "b": 1}')


def test_write_atomic_rejects_unserialisable(tmp_path):
    target = tmp_path / "data.json"
    with pytest.raises(TypeError):
        write_json_atomic(target, {"bad": object()})
    # Crucially the target must not exist -- we serialise before
    # touching the filesystem.
    assert not target.exists()
    # And no orphan tmp file either.
    assert list(tmp_path.iterdir()) == []


def test_read_json_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_json(tmp_path / "nope.json")


def test_write_accepts_str_path(tmp_path):
    target = tmp_path / "data.json"
    write_json_atomic(str(target), {"ok": 1})
    assert json.loads(target.read_text()) == {"ok": 1}


# --------------------------------------------------------------------------- #
# flocked
# --------------------------------------------------------------------------- #


def test_flocked_yields_fd_and_creates_file(tmp_path):
    lock = tmp_path / "x.lock"
    with flocked(lock) as fd:
        assert isinstance(fd, int)
        assert lock.exists()
    # File is left behind (we don't delete locks on release).
    assert lock.exists()


def test_flocked_non_blocking_raises_on_contention(tmp_path):
    """Two processes contending on the same lock: the second must raise.

    We use a subprocess to get genuine cross-process flock semantics
    (flock on Linux is per-open-file, so threads in the same process
    don't contend the way two processes do).
    """
    import subprocess
    import sys
    import textwrap

    lock = tmp_path / "race.lock"
    helper = textwrap.dedent(f"""
        import sys, time
        from tigerharness.workflow_runner.atomic import flocked
        with flocked({str(lock)!r}, blocking=True) as fd:
            sys.stdout.write("HELD\\n"); sys.stdout.flush()
            time.sleep(2.0)
    """)
    proc = subprocess.Popen(
        [sys.executable, "-c", helper],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Wait for the child to actually hold the lock.
        assert proc.stdout is not None
        line = proc.stdout.readline()
        assert "HELD" in line
        with pytest.raises(LockContendedError):
            with flocked(lock, blocking=False):
                pass  # pragma: no cover - should never enter
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_flocked_blocking_serialises_two_processes(tmp_path):
    """Process A holds, B waits, A releases, B proceeds. Verifies
    that ``blocking=True`` actually queues."""
    import subprocess
    import sys
    import textwrap

    lock = tmp_path / "serial.lock"
    log = tmp_path / "ord.log"

    holder = textwrap.dedent(f"""
        import sys, time
        from tigerharness.workflow_runner.atomic import flocked
        with flocked({str(lock)!r}, blocking=True) as fd:
            sys.stdout.write("HELD\\n"); sys.stdout.flush()
            time.sleep(0.6)
            open({str(log)!r}, 'a').write('A\\n')
    """)
    waiter = textwrap.dedent(f"""
        from tigerharness.workflow_runner.atomic import flocked
        with flocked({str(lock)!r}, blocking=True) as fd:
            open({str(log)!r}, 'a').write('B\\n')
    """)

    a = subprocess.Popen(
        [sys.executable, "-c", holder],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert a.stdout is not None
        line = a.stdout.readline()
        assert "HELD" in line
        b = subprocess.Popen(
            [sys.executable, "-c", waiter],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        b.wait(timeout=10)
        a.wait(timeout=10)
    finally:
        for p in (a,):
            if p.poll() is None:
                p.terminate()
                p.wait(timeout=5)

    # A must have written before B.
    assert log.read_text().splitlines() == ["A", "B"]


def test_flocked_in_thread_pair_serialises_writes(tmp_path):
    """Even within one process (where flock is permissive), our
    intra-thread test verifies the *yield-then-release* sequencing.

    We use a separate ``threading.Lock`` for the rendezvous and just
    confirm the context manager doesn't crash with concurrent entry.
    """
    lock = tmp_path / "thread.lock"
    seen: list[int] = []

    def worker(idx: int) -> None:
        with flocked(lock, blocking=True):
            time.sleep(0.05)
            seen.append(idx)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    # We can't enforce order intra-process (flock is permissive within
    # one PID), but we MUST see all four entries (no crashes / lost
    # threads).
    assert sorted(seen) == [0, 1, 2, 3]


def test_flocked_releases_on_exception(tmp_path):
    lock = tmp_path / "ex.lock"

    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        with flocked(lock):
            raise _Boom("inside")

    # Subsequent acquire must succeed (no leaked lock).
    with flocked(lock, blocking=False):
        pass


def test_flocked_create_false_raises_on_missing(tmp_path):
    missing = tmp_path / "no-such.lock"
    with pytest.raises(FileNotFoundError):
        with flocked(missing, create=False):
            pass  # pragma: no cover

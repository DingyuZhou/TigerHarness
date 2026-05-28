"""Atomic JSON I/O + POSIX flock helpers.

Two concerns deliberately kept thin and orthogonal:

* :func:`read_json` / :func:`write_json_atomic` -- crash-safe JSON
  file I/O. Writes go to a sibling tmp file (same directory, same
  filesystem, so rename is atomic), are ``fsync``-flushed, then
  ``os.replace``-renamed over the target. Reads are lock-free since
  ``os.replace`` is atomic at the filesystem layer -- a concurrent
  reader either sees the old file or the new file, never a torn one.

* :func:`flocked` -- a tiny context manager that takes an exclusive
  ``fcntl.flock`` on a file path. Two writers contending on the same
  path serialise cleanly; the kernel releases the lock when the
  context exits or the holding process dies, so an OS-level crash
  does not leak a dead-process lock.

The locking primitives needed by the executor (per-task ``.lock`` +
pid + heartbeat) live in :mod:`tigerharness.workflow_runner.locks`,
which builds on :func:`flocked` here.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

# --------------------------------------------------------------------------- #
# Atomic JSON
# --------------------------------------------------------------------------- #


class AtomicWriteError(OSError):
    """Raised when an atomic write fails part-way through.

    Subclasses :class:`OSError` so existing ``except OSError`` blocks
    in caller code keep working unchanged.
    """


def read_json(path: Path | str) -> Any:
    """Read JSON from ``path``.

    Lock-free. If the file is missing, raises ``FileNotFoundError`` --
    callers that want a default should handle that themselves so the
    behaviour stays explicit.
    """
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8"))


def write_json_atomic(
    path: Path | str,
    data: Any,
    *,
    indent: int | None = 2,
    sort_keys: bool = False,
) -> None:
    """Write ``data`` to ``path`` via tmp-file + fsync + ``os.replace``.

    Steps (the only correct way to do this on POSIX):

    1. Serialise to JSON in memory (rejects unserialisable input
       *before* we touch the filesystem).
    2. Create a uniquely-named tmp file in the same directory as
       ``path`` (same filesystem -> rename is atomic).
    3. Write, flush, ``fsync`` (so data hits the disk's write cache).
    4. ``os.replace`` over the target (atomic at the dirent layer).
    5. On any failure between steps 2-4, unlink the orphan tmp.

    Crash semantics: a concurrent reader sees either the previous
    contents or the new contents -- never a half-written file.

    Parameters
    ----------
    path:
        Target file.
    data:
        Anything ``json.dumps`` accepts.
    indent:
        Forwarded to ``json.dumps``. ``None`` for compact output.
    sort_keys:
        Forwarded to ``json.dumps``. Stable-diff-friendly when ``True``.
    """
    p = Path(path)
    parent = p.parent
    parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=indent, sort_keys=sort_keys) + "\n"

    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(parent),
        prefix=f".{p.name}.",
        suffix=".tmp",
        delete=False,
    )
    tmp_path = Path(tmp.name)
    try:
        try:
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        finally:
            tmp.close()
        os.replace(tmp_path, p)
    except OSError as exc:  # pragma: no cover - defensive
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise AtomicWriteError(
            f"failed to atomically write {p}: {exc}"
        ) from exc


# --------------------------------------------------------------------------- #
# POSIX flock context manager
# --------------------------------------------------------------------------- #


class LockContendedError(OSError):
    """Raised by :func:`flocked` when ``blocking=False`` and the lock
    is held by another process."""


@contextmanager
def flocked(
    path: Path | str,
    *,
    blocking: bool = True,
    create: bool = True,
) -> Iterator[int]:
    """Acquire an exclusive ``fcntl.flock`` on ``path``.

    Yields the underlying file descriptor (callers usually ignore it;
    it's exposed for tests that want to verify lock state). The
    descriptor is closed on context exit, which also releases the
    lock.

    Parameters
    ----------
    path:
        File to lock. Created (mode ``0o644``) if absent and
        ``create=True``.
    blocking:
        When ``False``, raise :class:`LockContendedError` immediately
        if the lock is held. When ``True`` (default), wait until the
        kernel hands it over.
    create:
        Create the lock file if it doesn't already exist. Default
        ``True``; set ``False`` if you want to ensure you only lock
        existing files.

    Notes
    -----
    * ``fcntl.flock`` semantics on Linux: locks are per-open-file
      (per ``open()`` call), not per-fd, and they're advisory. The
      kernel auto-releases them when the holding process dies, which
      gives us free stale-lock cleanup.
    * On Linux, multiple ``flock(LOCK_EX)`` calls from the same
      process on the same path *succeed* (flock is per-open-file-
      description, not per-pid). That means in-process re-entry is
      possible; tests rely on process-level contention to demonstrate
      serialisation.
    """
    p = Path(path)
    open_flags = os.O_RDWR
    if create:
        p.parent.mkdir(parents=True, exist_ok=True)
        open_flags |= os.O_CREAT
    flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if not blocking else 0)
    fd = os.open(str(p), open_flags, 0o644)
    try:
        try:
            fcntl.flock(fd, flags)
        except BlockingIOError as exc:
            raise LockContendedError(
                f"lock on {p} is held by another process"
            ) from exc
        except OSError as exc:  # pragma: no cover - platform-specific
            # EWOULDBLOCK / EAGAIN on some platforms surface as plain
            # OSError rather than BlockingIOError. On Linux + glibc
            # we always get the BlockingIOError subclass, so this
            # fallback is purely defensive for other unixes.
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise LockContendedError(
                    f"lock on {p} is held by another process"
                ) from exc
            raise
        yield fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:  # pragma: no cover - defensive
            pass
        os.close(fd)

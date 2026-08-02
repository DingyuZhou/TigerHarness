"""Filesystem store for tiger-memory: folder layout, atomic writes, lock.

Layout (design doc §4 — the shipped three-store model, ADR 0007):
    <root>/
        journal/    the three bounded stores:
                    ``skills.md`` / ``must_remember.md`` / ``topics.md``
                    plus ``.state.json`` (sweep bookkeeping)
        briefing/   session-load working set (rebuilt atomically)

There is no ``archive/`` dir and no chronological rollups: §3/§9 retired the
detailed-summary archive, the daily/weekly/monthly rollup ladder, and
``longer_memory`` entirely. The migration is a fresh start
(``lifecycle._drop_legacy_surface``) that *deletes* any pre-existing legacy
surface from an old store; ``init_layout`` never (re)creates it.

All writes are write-tmp-then-rename so a crash mid-write never leaves
a partial file. The briefing rebuild is full-folder swap.

The ``*_RE`` filename patterns below describe the *retired* chronological
shapes; they survive only so ``lifecycle._drop_legacy_surface`` can recognise
and delete leftover rollup ``.md`` files on a fresh-start migration.
"""
from __future__ import annotations

import logging

import errno
import json
import os
import re
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

log = logging.getLogger("tigerharness.tiger_memory.store")


def reclaim_lockfile(
    path: Path, *, allow_if_alive_older_than: float | None = None
) -> bool:
    """Atomically retire a stale lockfile; ``True`` iff it was retired.

    The naive reclaim (read pid → ``unlink`` → recreate) has a TOCTOU:
    between the read and the unlink another process may have completed its
    own reclaim AND acquired a fresh lock at the same path — the unlink
    then removes a LIVE lock and two holders enter the critical section
    (audit F4). Here the reclaimer first ``os.rename``s the lockfile to a
    uniquely-named tombstone — atomic, so exactly one reclaimer wins the
    right to judge it — and only then inspects the holder:

    - dead / unreadable holder → drop the tombstone (reclaimed);
    - LIVE holder (we grabbed a fresh lock by mistake) → rename it back.
      When the rename-back loses (a third process already re-created the
      path) the tombstone is dropped; the displaced holder's release then
      no-ops via the owner check in :func:`release_lockfile`, so the
      damage cannot cascade.

    *allow_if_alive_older_than*: when set, a live holder whose lockfile
    is older than this many seconds is still reclaimed (the hung-process
    case age-based locks rely on).
    """
    import uuid as _uuid
    tomb = path.with_name(
        f"{path.name}.stale.{os.getpid()}.{_uuid.uuid4().hex[:8]}"
    )
    try:
        os.rename(path, tomb)
    except OSError:
        return False  # someone else already reclaimed / released it
    try:
        holder_pid = int(tomb.read_text().split()[0])
    except (ValueError, OSError, IndexError):
        holder_pid = -1
    if holder_pid > 0 and _pid_alive(holder_pid):
        age_ok_to_steal = False
        if allow_if_alive_older_than is not None:
            try:
                age = time.time() - tomb.stat().st_mtime
            except OSError:  # pragma: no cover - tombstone vanished
                age = 0.0
            age_ok_to_steal = age > allow_if_alive_older_than
        if not age_ok_to_steal:
            try:
                os.rename(tomb, path)
            except OSError:  # pragma: no cover - path re-created, back off
                tomb.unlink(missing_ok=True)
            return False
    tomb.unlink(missing_ok=True)
    return True


def release_lockfile(path: Path) -> None:
    """Unlink *path* only if THIS process is the recorded holder.

    After a (mis)reclaim, blindly unlinking on release would remove the
    NEW holder's lock and propagate the corruption; the owner check makes
    a displaced holder's release a harmless no-op (audit F4).
    """
    try:
        holder_pid = int(path.read_text().split()[0])
    except (ValueError, OSError, IndexError):
        return
    if holder_pid == os.getpid():
        path.unlink(missing_ok=True)


# Regex patterns for the retired chronological filename shapes. KEPT (not the
# generators/globs, which are gone) because ``lifecycle._drop_legacy_surface``
# still uses these to identify+delete legacy rollup ``.md`` files on migration.
SHORT_RE = re.compile(r"^(\d{8})-(\d{6})-(.+)\.md$")
DAILY_RE = re.compile(r"^(\d{8})-daily-(.+)\.md$")
WEEKLY_RE = re.compile(r"^(\d{8})-week-(.+)\.md$")
MONTHLY_RE = re.compile(r"^(\d{6})-month-(.+)\.md$")


@dataclass(frozen=True)
class Paths:
    root: Path
    journal: Path
    briefing: Path
    # The retired detailed-summary archive dir. NOT part of the live layout —
    # ``init_layout`` never creates it. Resolved only so the fresh-start
    # migration (``lifecycle._drop_legacy_surface``) can delete a pre-existing
    # one left behind by an old store.
    archive: Path

    @classmethod
    def from_root(cls, root: Path) -> "Paths":
        return cls(
            root=root,
            journal=root / "journal",
            briefing=root / "briefing",
            archive=root / "archive",
        )


class Store:
    """Owns the on-disk store: layout, atomic writes, lock primitive."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self.paths = Paths.from_root(self.root)

    # ----- layout -------------------------------------------------------

    def init_layout(self) -> None:
        """Create the live folder layout if missing.

        The live layout is just ``journal/`` + ``briefing/``; the retired
        ``archive/`` dir is NOT (re)created here (see ``Paths.archive``).
        Drops a ``.gitkeep`` in ``journal/`` so the empty directory is
        trackable by git from the first commit (briefing/ is gitignored and
        doesn't need one).
        """
        for d in (self.paths.journal, self.paths.briefing):
            d.mkdir(parents=True, exist_ok=True)
        gitkeep = self.paths.journal / ".gitkeep"
        if not gitkeep.exists():
            gitkeep.write_text("")

    def exists(self) -> bool:
        return self.paths.journal.exists()

    # ----- atomic write -------------------------------------------------

    def atomic_write(self, target: Path, content: str) -> None:
        """Write *content* to *target* via unique-tmp+rename.

        The tmp name is unique per writer: a fixed tmp name lets two
        concurrent writers truncate each other's in-progress bytes and
        publish interleaved content through ``os.replace`` (audit F2).
        """
        import uuid as _uuid
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(
            f"{target.name}.tmp.{os.getpid()}.{_uuid.uuid4().hex[:8]}"
        )
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)

    def atomic_swap_dir(self, new_dir: Path, target: Path) -> None:
        """Atomically replace ``target`` with ``new_dir`` (both must exist)."""
        log.debug("atomic_swap_dir: %s -> %s", new_dir, target)
        if not new_dir.exists():
            raise FileNotFoundError(f"new_dir does not exist: {new_dir}")
        backup = target.with_name(target.name + ".old")
        if backup.exists():
            shutil.rmtree(backup)
        if target.exists():
            os.rename(target, backup)
        try:
            os.rename(new_dir, target)
        except OSError:
            # Rollback
            if target.exists() is False and backup.exists():
                os.rename(backup, target)
            raise
        if backup.exists():
            shutil.rmtree(backup)

    # ----- lock (§7.2) --------------------------------------------------

    @contextmanager
    def lock(
        self,
        lock_path: Path,
        timeout_minutes: int = 60,
    ) -> Iterator[bool]:
        """PID-stamped exclusive lock with periodic mtime refresh.

        Yields ``True`` if the lock was acquired (caller does work); yields
        ``False`` if held by a live PID (caller should exit silently). A
        stale lock (dead PID, or older than *timeout_minutes*) is reclaimed.

        While held, a daemon thread refreshes the lockfile's mtime every
        ``timeout_minutes / 4`` (or 60s, whichever is smaller). This means
        a healthy long-running rebuild stays alive through the timeout
        even if it takes hours, while a *hung* process whose touch thread
        died still gets reclaimed.

        The lock file is removed on exit if we held it.
        """
        import threading
        acquired = self._try_acquire_lock(lock_path, timeout_minutes)
        stop = threading.Event()
        refresher: threading.Thread | None = None
        if acquired:
            interval = max(10.0, min(60.0, timeout_minutes * 60 / 4))
            refresher = threading.Thread(
                target=_refresh_lockfile_loop,
                args=(lock_path, stop, interval),
                daemon=True,
                name="tiger-memory-lock-refresh",
            )
            refresher.start()
        try:
            yield acquired
        finally:
            if acquired:
                stop.set()
                if refresher is not None:  # pragma: no branch  # refresher always started when acquired
                    refresher.join(timeout=2.0)
                # Owner-verified release: after an age-based reclaim our
                # lockfile may already belong to someone else (audit F4).
                release_lockfile(lock_path)

    def _try_acquire_lock(self, lock_path: Path, timeout_minutes: int) -> bool:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic create-and-write (O_EXCL).
        try:
            fd = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            return True
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise

        # Lock exists. Check liveness + age.
        try:
            stat = lock_path.stat()
        except FileNotFoundError:
            # Raced with another process releasing; retry once.
            return self._try_acquire_lock(lock_path, timeout_minutes)
        age_sec = time.time() - stat.st_mtime
        try:
            holder_pid = int(lock_path.read_text().strip())
        except (ValueError, OSError):
            holder_pid = -1

        stale_by_age = age_sec > timeout_minutes * 60
        stale_by_pid = holder_pid > 0 and not _pid_alive(holder_pid)
        if stale_by_age or stale_by_pid or holder_pid <= 0:
            # Rename-based reclaim (TOCTOU-safe, audit F4), then recurse
            # once. A lost reclaim race means someone else owns the call —
            # fall through to "held".
            if reclaim_lockfile(
                lock_path, allow_if_alive_older_than=timeout_minutes * 60
            ):
                return self._try_acquire_lock(lock_path, timeout_minutes)
            return False

        return False  # held by live PID

    # ----- state file ---------------------------------------------------

    def write_state(self, payload: dict) -> None:
        self.atomic_write(
            self.paths.journal / ".state.json",
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
        )

    def read_state(self) -> dict | None:
        p = self.paths.journal / ".state.json"
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            return None


def _refresh_lockfile_loop(lock_path: Path, stop, interval_sec: float) -> None:
    """Periodically `touch` the lockfile so live rebuilds aren't reclaimed.

    Exits when *stop* is set or the lockfile disappears.
    """
    while not stop.wait(interval_sec):
        try:
            os.utime(lock_path, None)
        except FileNotFoundError:
            return
        except OSError:
            # Filesystem hiccup — best-effort; try again next tick.
            continue


def _pid_alive(pid: int) -> bool:
    """Return True iff a process with *pid* exists (POSIX only)."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it — still alive for our purposes.
        return True

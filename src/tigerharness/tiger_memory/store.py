"""Filesystem store for tiger-memory: folder layout, atomic writes, lock.

Layout (design doc §4):
    <root>/
        archive/    detailed summaries (one per conv)
        journal/    shorts + rollups + must_memorize + longer_memory
        briefing/   session-load working set (rebuilt atomically)

All writes are write-tmp-then-rename so a crash mid-write never leaves
a partial file. The briefing rebuild is full-folder swap.

Filename conventions (§4.1):
    Short:   YYYYMMDD-HHmmss-<UUID>.md
    Daily:   YYYYMMDD-daily-<UUID>.md
    Weekly:  YYYYMMDD-week-<UUID>.md   (Monday's date)
    Monthly: YYYYMM-month-<UUID>.md
    Archive: same filename as short, in archive/
"""
from __future__ import annotations

import errno
import json
import os
import re
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator


# Glob/regex patterns per §4.1
SHORT_GLOB = "[0-9]" * 8 + "-[0-9]" * 6 + "-*.md"
DAILY_GLOB_TEMPLATE = "{date}-daily-*.md"
WEEKLY_GLOB_TEMPLATE = "{monday}-week-*.md"
MONTHLY_GLOB_TEMPLATE = "{year_month}-month-*.md"

SHORT_RE = re.compile(r"^(\d{8})-(\d{6})-(.+)\.md$")
DAILY_RE = re.compile(r"^(\d{8})-daily-(.+)\.md$")
WEEKLY_RE = re.compile(r"^(\d{8})-week-(.+)\.md$")
MONTHLY_RE = re.compile(r"^(\d{6})-month-(.+)\.md$")


@dataclass(frozen=True)
class Paths:
    root: Path
    archive: Path
    journal: Path
    briefing: Path

    @classmethod
    def from_root(cls, root: Path) -> "Paths":
        return cls(
            root=root,
            archive=root / "archive",
            journal=root / "journal",
            briefing=root / "briefing",
        )


class Store:
    """Owns the on-disk store: layout, atomic writes, lock primitive."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self.paths = Paths.from_root(self.root)

    # ----- layout -------------------------------------------------------

    def init_layout(self) -> None:
        """Create the folder layout if missing."""
        for d in (self.paths.archive, self.paths.journal, self.paths.briefing):
            d.mkdir(parents=True, exist_ok=True)

    def exists(self) -> bool:
        return self.paths.archive.exists() and self.paths.journal.exists()

    # ----- atomic write -------------------------------------------------

    def atomic_write(self, target: Path, content: str) -> None:
        """Write *content* to *target* via tmp+rename. Parent dirs must exist."""
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)

    def atomic_swap_dir(self, new_dir: Path, target: Path) -> None:
        """Atomically replace ``target`` with ``new_dir`` (both must exist)."""
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

    # ----- session/file resolution (§7.9) -------------------------------

    def find_archive(self, conversation_uuid: str) -> Path | None:
        """Glob ``archive/*-<UUID>.md`` — at most one match."""
        matches = list(self.paths.archive.glob(f"*-{conversation_uuid}.md"))
        if not matches:
            return None
        if len(matches) > 1:
            raise RuntimeError(
                f"multiple archives for uuid {conversation_uuid}: {matches}"
            )
        return matches[0]

    def find_short(self, conversation_uuid: str) -> Path | None:
        """Locate the short summary for *conversation_uuid*.

        Excludes rollups by enforcing the time-bearing filename pattern.
        """
        for f in self.paths.journal.glob(f"*-{conversation_uuid}.md"):
            if SHORT_RE.match(f.name):
                return f
        return None

    # ----- filename derivation ------------------------------------------

    @staticmethod
    def short_filename(first_event_at: datetime, conversation_uuid: str) -> str:
        return (
            f"{first_event_at.strftime('%Y%m%d-%H%M%S')}-"
            f"{conversation_uuid}.md"
        )

    @staticmethod
    def daily_filename(date_str_yyyymmdd: str, rollup_uuid: str) -> str:
        return f"{date_str_yyyymmdd}-daily-{rollup_uuid}.md"

    @staticmethod
    def weekly_filename(monday_str_yyyymmdd: str, rollup_uuid: str) -> str:
        return f"{monday_str_yyyymmdd}-week-{rollup_uuid}.md"

    @staticmethod
    def monthly_filename(year_month_str_yyyymm: str, rollup_uuid: str) -> str:
        return f"{year_month_str_yyyymm}-month-{rollup_uuid}.md"

    # ----- enumerators --------------------------------------------------

    def shorts_for_date(self, date_str: str) -> list[Path]:
        """Return all shorts whose date prefix == *date_str* (YYYYMMDD)."""
        out: list[Path] = []
        for f in self.paths.journal.glob(f"{date_str}-*.md"):
            if SHORT_RE.match(f.name):
                out.append(f)
        return sorted(out)

    def daily_for_date(self, date_str: str) -> Path | None:
        matches = sorted(
            f
            for f in self.paths.journal.glob(f"{date_str}-daily-*.md")
            if DAILY_RE.match(f.name)
        )
        return matches[-1] if matches else None  # if duplicated, pick latest UUID

    def weekly_for_monday(self, monday_str: str) -> Path | None:
        matches = sorted(
            f
            for f in self.paths.journal.glob(f"{monday_str}-week-*.md")
            if WEEKLY_RE.match(f.name)
        )
        return matches[-1] if matches else None

    def monthly_for_yyyymm(self, year_month_str: str) -> Path | None:
        matches = sorted(
            f
            for f in self.paths.journal.glob(f"{year_month_str}-month-*.md")
            if MONTHLY_RE.match(f.name)
        )
        return matches[-1] if matches else None

    def working_days(self) -> list[str]:
        """All distinct YYYYMMDD prefixes that have ≥ 1 short summary.

        Returned newest-first (sorted desc).
        """
        dates: set[str] = set()
        for f in self.paths.journal.glob("*.md"):
            m = SHORT_RE.match(f.name)
            if m:
                dates.add(m.group(1))
        return sorted(dates, reverse=True)

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
                if refresher is not None:
                    refresher.join(timeout=2.0)
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass

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
            # Reclaim. Remove then recurse once.
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            return self._try_acquire_lock(lock_path, timeout_minutes)

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

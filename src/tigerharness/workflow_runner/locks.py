"""Task-level lock + pid + heartbeat primitives.

The executor in Phase 1 sub-step #4 (Rukawa) will use these to
prevent two ``workflow start``/``resume`` invocations from racing on
the same task-id, and to let ``workflow-sweep`` distinguish a
genuinely-running task from a wedged or crashed one.

Two pieces of state per task:

* ``.lock`` -- a POSIX ``fcntl.flock`` target. While the executor
  holds an exclusive lock, no other process can take the lock. The
  kernel releases the lock automatically when the holding process
  dies, so a hard kill never leaks a permanent lock.

* ``.pid`` -- a JSON file written atomically by the executor::

    {
        "pid": 12345,
        "started_at": "2026-05-28T14:42:11Z",
        "last_heartbeat": "2026-05-28T14:55:09Z"
    }

  The pid lets ``workflow-sweep`` print "who's running this task" and
  decide whether to attempt takeover. The ``last_heartbeat`` lets us
  detect "process alive but wedged" cases that flock alone can't
  surface.

Why two files, not one? Because flock + JSON-payload on the same fd
is awkward (the lock fd would have to stay open for the lifetime of
the task while the JSON content gets read/rewritten by other
processes). Two files keep each primitive single-purpose.
"""

from __future__ import annotations

import datetime as _dt
import errno
import os
import time as _time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from tigerharness.workflow_runner.atomic import (
    LockContendedError,
    flocked,
    read_json,
    write_json_atomic,
)
from tigerharness.workflow_runner.models import now_iso


# Re-export under a workflow-runner-flavored name so callers don't
# have to import from the atomic module just for the exception type.
class LockHeldError(LockContendedError):
    """Raised when :func:`acquire_task_lock` is called non-blocking and
    another process already holds the task lock."""


# --------------------------------------------------------------------------- #
# Pid info
# --------------------------------------------------------------------------- #


class PidInfo:
    """Snapshot of the ``.pid`` file. Plain class (not dataclass) so we
    can keep ``read_pid_info`` cheap and avoid validation overhead on
    a hot path used by the sweep CLI."""

    __slots__ = ("pid", "started_at", "last_heartbeat")

    def __init__(
        self,
        pid: int,
        started_at: str,
        last_heartbeat: str,
    ) -> None:
        self.pid = pid
        self.started_at = started_at
        self.last_heartbeat = last_heartbeat

    def to_dict(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "started_at": self.started_at,
            "last_heartbeat": self.last_heartbeat,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "PidInfo":
        try:
            pid = int(raw["pid"])  # type: ignore[arg-type]
            started_at = str(raw["started_at"])
            last_heartbeat = str(raw["last_heartbeat"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid pid file payload: {raw!r}") from exc
        return cls(pid=pid, started_at=started_at,
                   last_heartbeat=last_heartbeat)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"PidInfo(pid={self.pid}, started_at={self.started_at!r}, "
            f"last_heartbeat={self.last_heartbeat!r})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PidInfo):
            return NotImplemented
        return (
            self.pid == other.pid
            and self.started_at == other.started_at
            and self.last_heartbeat == other.last_heartbeat
        )

    def __hash__(self) -> int:  # pragma: no cover - cosmetic
        return hash((self.pid, self.started_at, self.last_heartbeat))


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #


def _lock_path(task_dir: Path | str) -> Path:
    return Path(task_dir) / ".lock"


def _pid_path(task_dir: Path | str) -> Path:
    return Path(task_dir) / ".pid"


# --------------------------------------------------------------------------- #
# Lock
# --------------------------------------------------------------------------- #


@contextmanager
def acquire_task_lock(
    task_dir: Path | str,
    *,
    blocking: bool = False,
) -> Iterator[int]:
    """Acquire an exclusive lock on ``<task_dir>/.lock``.

    Default is non-blocking: a second invocation against the same
    task-id raises :class:`LockHeldError` immediately, matching the
    spec's "second start/resume against the same task-id refuses with
    a clear error".

    Use ``blocking=True`` for cases where the caller is willing to
    queue (e.g., the sweep CLI's ``--auto-resume`` path that wants to
    take over once the prior owner exits).

    The task directory is created if absent.
    """
    p = _lock_path(task_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        with flocked(p, blocking=blocking, create=True) as fd:
            yield fd
    except LockContendedError as exc:
        # Re-raise as our subclass so callers can `except LockHeldError`
        # without importing from the atomic module.
        raise LockHeldError(str(exc)) from exc


# --------------------------------------------------------------------------- #
# Pid + heartbeat
# --------------------------------------------------------------------------- #


def write_pid(
    task_dir: Path | str,
    *,
    pid: int | None = None,
    now: str | None = None,
) -> PidInfo:
    """Atomically write ``<task_dir>/.pid``.

    Sets ``started_at`` and ``last_heartbeat`` to the same timestamp
    (``now_iso()`` by default). Call :func:`heartbeat` periodically
    after this to bump just the ``last_heartbeat`` field.

    Returns the :class:`PidInfo` that was persisted -- useful for
    tests and for the executor to log "I'm task X, pid Y".
    """
    # Cache the timestamp so started_at and last_heartbeat are equal
    # even if the clock ticks across the second boundary between the
    # two reads.
    ts = now if now is not None else now_iso()
    info = PidInfo(
        pid=os.getpid() if pid is None else int(pid),
        started_at=ts,
        last_heartbeat=ts,
    )
    write_json_atomic(_pid_path(task_dir), info.to_dict())
    return info


def heartbeat(
    task_dir: Path | str,
    *,
    now: str | None = None,
) -> PidInfo:
    """Update only ``last_heartbeat`` in ``<task_dir>/.pid``.

    Raises ``FileNotFoundError`` if the pid file doesn't exist yet
    (callers should invoke :func:`write_pid` first). Raises
    ``ValueError`` if the existing pid file is malformed.

    Returns the updated :class:`PidInfo`.
    """
    pid_p = _pid_path(task_dir)
    raw = read_json(pid_p)
    if not isinstance(raw, dict):
        raise ValueError(f"pid file {pid_p} is not a JSON object")
    info = PidInfo.from_dict(raw)
    info.last_heartbeat = now if now is not None else now_iso()
    write_json_atomic(pid_p, info.to_dict())
    return info


def read_pid_info(task_dir: Path | str) -> PidInfo | None:
    """Return the current :class:`PidInfo` or ``None`` if absent.

    Corrupt pid files (missing keys, bad types) raise ``ValueError``
    rather than being silently ignored -- a malformed pid file is a
    bug worth surfacing, not a "no lock holder" situation.
    """
    pid_p = _pid_path(task_dir)
    try:
        raw = read_json(pid_p)
    except FileNotFoundError:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"pid file {pid_p} is not a JSON object")
    return PidInfo.from_dict(raw)


def _pid_alive(pid: int) -> bool:
    """``True`` if a process with this pid exists and is signalable.

    Uses ``os.kill(pid, 0)`` which sends no signal but performs the
    permission + existence checks. We treat ``ESRCH`` (no such
    process) as "not alive"; ``EPERM`` (exists but we can't signal it)
    counts as alive. Other ``OSError``s propagate.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno == errno.EPERM:
            return True
        raise  # pragma: no cover - defensive
    return True


def is_stale(
    task_dir: Path | str,
    *,
    max_silence_sec: float,
    now_epoch: float | None = None,
) -> bool:
    """Return ``True`` if the task's pid file implies a dead/wedged owner.

    "Stale" means **either**:

    * the recorded pid is no longer alive (process gone), **or**
    * the recorded ``last_heartbeat`` is older than ``max_silence_sec``
      seconds (process alive but no longer reporting).

    Returns ``False`` if there is no pid file at all (no one ever
    claimed the lock -- not stale, just absent).

    Returns ``True`` for malformed pid files: an unreadable holder
    record is at least as bad as a stale one and should be cleaned up
    by the takeover path.

    Parameters
    ----------
    task_dir:
        Directory containing the ``.pid`` file.
    max_silence_sec:
        Heartbeat-age threshold. Must be > 0.
    now_epoch:
        Override "current time" for tests. Defaults to ``time.time()``.
    """
    if max_silence_sec <= 0:
        raise ValueError("max_silence_sec must be > 0")

    try:
        info = read_pid_info(task_dir)
    except ValueError:
        # Corrupt pid file. Treat as stale so the next start can take
        # over cleanly.
        return True
    if info is None:
        # No pid file -- no one ever claimed the lock; not stale.
        return False

    if not _pid_alive(info.pid):
        return True

    now = now_epoch if now_epoch is not None else _time.time()
    hb_epoch = _iso_to_epoch(info.last_heartbeat)
    if hb_epoch is None:
        # Unparseable heartbeat -> treat as stale.
        return True
    return (now - hb_epoch) > max_silence_sec


def _iso_to_epoch(ts: str) -> float | None:
    """Parse our canonical ISO timestamps back to epoch seconds.

    Accepts the ``"...Z"`` and ``"...+HH:MM"`` shapes ``now_iso``
    produces. Returns ``None`` on anything else so the caller can
    flag it as stale rather than crashing.
    """
    try:
        # Python's ``fromisoformat`` is forgiving from 3.11 onwards
        # and handles the Z suffix as +00:00.
        normalised = ts.replace("Z", "+00:00")
        dt = _dt.datetime.fromisoformat(normalised)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None

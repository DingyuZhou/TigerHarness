"""The lazy sweep: classify ``active/`` and surface a summary.

Called by the ``drive-journal`` skill at the start of every invocation
and at every cascade boundary. It is **non-AI** -- plain Python -- and
side-effecting only in two precise ways:

1. Archive any task whose ``state`` is ``done`` (move ``active/<id>``
   to ``done/<id>``).
2. Surface a structured summary so the driver can decide what to pick
   up next.

It classifies each ``in_progress`` task as **idle** (detached --
``session_ref`` cleared), **busy** (attached + heartbeat fresh within
the stuck-timeout), or **crashed** (attached + heartbeat stale).
That's it. The sweep does NOT mutate ``status.json``; the
classification is advisory. The driver may then pick up a **crashed**
task as a rescue (and at that point the driver bumps ``sessions`` and
``updated_at`` as it would for any pickup).

The default heartbeat threshold is 1800 seconds (30 min), overridable
via ``TIGERHARNESS_JOURNAL_STUCK_TIMEOUT``.
"""

from __future__ import annotations

import logging

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, field

from tigerharness.journal.models import (
    JournalModelError,
    State,
    Status,
    _utcnow_iso,
)
from tigerharness.journal.paths import JournalPaths

log = logging.getLogger("tigerharness.journal.sweep")


DEFAULT_STUCK_TIMEOUT_SEC = 1800


def stuck_timeout_from_env() -> int:
    """Resolve the heartbeat-stale threshold from the env, falling back
    to ``DEFAULT_STUCK_TIMEOUT_SEC``. Invalid / non-int values raise so
    the operator notices a typo rather than silently getting the default."""
    raw = os.environ.get("TIGERHARNESS_JOURNAL_STUCK_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_STUCK_TIMEOUT_SEC
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"TIGERHARNESS_JOURNAL_STUCK_TIMEOUT must be an integer; "
            f"got {raw!r}"
        ) from exc
    if value < 1:
        raise ValueError(
            f"TIGERHARNESS_JOURNAL_STUCK_TIMEOUT must be >= 1; got {value}"
        )
    return value


@dataclass
class MalformedEntry:
    """A task directory whose ``status.json`` could not be parsed. The
    sweep does not skip these silently -- they're returned in the
    result so the driver can surface them to the human."""

    task_id: str
    error: str


@dataclass
class SweepResult:
    """The classification + actions emitted by one sweep.

    Field meanings (per ``docs/subscription-backend.md`` "The lazy
    sweep" section):

    - ``archived``: task ids moved from ``active/`` to ``done/``.
    - ``pending``: not-yet-started tasks.
    - ``in_progress_idle``: ``in_progress`` with no session attached
      (``session_ref is None``) -- cleanly handed off, resumable
      **immediately** (no heartbeat wait). The instant-resume class.
    - ``in_progress_busy``: ``in_progress`` + a session attached + a
      fresh heartbeat -- a live session owns it; do not touch.
    - ``in_progress_crashed``: ``in_progress`` + a session attached + a
      *stale* heartbeat -- the owner went silent; reclaimable (rescue).
    - ``blocked``: needs human attention.
    - ``needs_input``: parked tasks awaiting an Operator answer. These
      live in the ``needs_input/`` tray (not ``active/``), so they are
      surfaced for visibility but are never actionable -- the Operator
      reopens them with ``journal answer``. See
      ``docs/journal-operator-questions.md``.
    - ``malformed``: task directories whose ``status.json`` failed to
      parse (the sweep does not bail; the driver decides what to do).
    """

    archived: list[str] = field(default_factory=list)
    pending: list[Status] = field(default_factory=list)
    in_progress_idle: list[Status] = field(default_factory=list)
    in_progress_busy: list[Status] = field(default_factory=list)
    in_progress_crashed: list[Status] = field(default_factory=list)
    blocked: list[Status] = field(default_factory=list)
    needs_input: list[Status] = field(default_factory=list)
    malformed: list[MalformedEntry] = field(default_factory=list)
    # T8 scheduler counts (populated before classification).
    schedule_materialized: list[str] = field(default_factory=list)
    schedule_malformed: list[str] = field(default_factory=list)
    # Provenance check (2026-06 multiroot fix): tasks whose recorded
    # ``journal_root`` doesn't match the root they sit in -- a task
    # living in a journal it was never scheduled into. Tuples of
    # (task_id, recorded_root). ``provenance_unknown`` counts tasks
    # predating the field (reported, never guessed at).
    misplaced: list[tuple[str, str]] = field(default_factory=list)
    provenance_unknown: list[str] = field(default_factory=list)

    def actionable(self) -> list[Status]:
        """Tasks the driver may pick at step 2 of OPERATING.md, in
        priority order (**finish before you start**):

        1. **Resume** an in-flight task -- ``in_progress_idle`` (cleanly
           handed off) or ``in_progress_crashed`` (rescue) -- sorted by
           *oldest* heartbeat so the most-abandoned wedge goes first.
        2. Only if nothing is in progress at all, **start** a ``pending``
           task.

        If a task is ``in_progress_busy`` (a live session owns it) and
        nothing is resumable, the result is empty: the driver waits
        rather than starting new work, so a later task cannot begin
        before the in-flight one finishes (it may depend on it)."""
        resumable = sorted(
            self.in_progress_idle + self.in_progress_crashed,
            key=lambda s: s.updated_at,
        )
        if resumable:
            return resumable
        if self.in_progress_busy:
            return []  # a live session owns the in-flight task; wait
        return list(self.pending)

    def has_actionable(self) -> bool:
        return bool(self.actionable())

    def to_summary(self) -> str:
        """Human-readable one-line summary string. Used as the in-session
        line the driver surfaces to the human at the top of each
        invocation and between cascaded tasks."""
        parts = [
            f"{len(self.pending)} pending",
            f"{len(self.in_progress_idle)} resumable",
            f"{len(self.in_progress_busy)} busy",
            f"{len(self.in_progress_crashed)} crashed",
            f"{len(self.blocked)} blocked",
            f"{len(self.needs_input)} needs-input",
        ]
        if self.archived:
            parts.append(f"archived {len(self.archived)} done")
        if self.malformed:
            parts.append(f"{len(self.malformed)} malformed")
        if self.schedule_materialized:
            parts.append(
                f"materialized {len(self.schedule_materialized)} scheduled"
            )
        if self.schedule_malformed:
            parts.append(
                f"{len(self.schedule_malformed)} malformed-definitions"
            )
        return "Journal: " + ", ".join(parts) + "."


def newest_mtime_age_seconds(task_dir: Path, *, now_epoch: float) -> float:
    """Seconds since the most recently modified file under ``task_dir``.

    A second, independent liveness signal. ``status.updated_at`` moves only
    when something remembers to write it; file mtimes move whenever the
    session actually does anything -- a worklog note, a plan revision, an
    artifact. A working session that has not refreshed its heartbeat is
    still visibly working on disk.

    Safe as a liveness signal precisely because :func:`sweep` performs no
    writes into an ``in_progress`` task's directory (see its docstring).
    Were that to change, a dead task would look alive forever and could
    never be reclaimed -- so that "no other writes" guarantee is
    load-bearing here, not incidental.

    Returns ``inf`` when the directory is missing, empty, or unreadable, so
    a task with no evidence degrades to "no liveness signal" and the caller
    falls back to the heartbeat rather than being pinned alive.
    """
    newest = 0.0
    try:
        for p in task_dir.rglob("*"):
            try:
                if p.is_file():
                    newest = max(newest, p.stat().st_mtime)
            except OSError:  # pragma: no cover - racing file removal
                continue
    except OSError:  # pragma: no cover - unreadable task dir
        return float("inf")
    if newest <= 0.0:
        return float("inf")
    return max(0.0, now_epoch - newest)


def _epoch_from_iso(ts: str) -> float:
    """``ts`` as a POSIX timestamp, or the real clock if unparseable.

    Tests inject a frozen ``now`` so heartbeat ages are deterministic; the
    mtime comparison has to share that clock, or a frozen-clock test would
    read every fixture file as wildly stale (or wildly fresh).
    """
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:  # pragma: no cover - Status parsing guards this
        return time.time()
    if parsed.tzinfo is None:  # pragma: no cover - _utcnow_iso is aware
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def sweep(
    paths: JournalPaths,
    *,
    stuck_timeout_sec: int | None = None,
    now: str | None = None,
) -> SweepResult:
    """Run one sweep over ``paths.active`` and return the classification.

    Side effects: archives any ``state=done`` task. **No other writes** --
    and that is a contract now, not a convenience. Crash classification
    consults file mtimes under the task directory as a liveness signal
    (:func:`newest_mtime_age_seconds`), so a sweep that touched an
    ``in_progress`` task would keep refreshing its own evidence and no dead
    task could ever be reclaimed. Do not add writes here.

    ``now`` is injected for tests so heartbeat ages are deterministic;
    in production it defaults to UTC now via ``_utcnow_iso``.
    """
    timeout = stuck_timeout_sec if stuck_timeout_sec is not None \
        else stuck_timeout_from_env()
    ts_now = now or _utcnow_iso()

    result = SweepResult()

    # T8: materialize due schedule definitions FIRST, so a task born
    # from a schedule shows up in this very sweep's pending list (the
    # drive that triggered it can claim it immediately). The
    # materializer never raises -- the sweep is the drive's front door.
    from tigerharness.journal.schedule import materialize_due
    mat = materialize_due(
        paths, now=ts_now, stuck_timeout_sec=timeout,
    )
    result.schedule_materialized = mat.materialized
    result.schedule_malformed = mat.malformed

    for task_id in paths.list_active_ids():
        status_path = paths.status_json(task_id)
        try:
            status = Status.from_json(status_path.read_text())
        except (JournalModelError, OSError) as exc:
            result.malformed.append(
                MalformedEntry(task_id=task_id, error=str(exc))
            )
            continue

        # Provenance check runs for every parseable status,
        # whatever its state -- a misplaced done task matters too.
        if status.journal_root is None:
            result.provenance_unknown.append(task_id)
        elif Path(status.journal_root).resolve() != paths.root.resolve():
            result.misplaced.append((task_id, status.journal_root))
            log.warning(
                "sweep: %s is MISPLACED -- scheduled into %s but "
                "sitting in %s", task_id, status.journal_root,
                paths.root,
            )

        if status.state is State.DONE:
            # Archive: move active/<id>/ -> done/<id>/.
            try:
                paths.archive(task_id)
                result.archived.append(task_id)
                log.info("sweep: archived %s (done -> done/)", task_id)
            except Exception as exc:  # pragma: no cover - defensive
                # Surface as malformed-shaped so the driver still sees it.
                result.malformed.append(MalformedEntry(
                    task_id=task_id,
                    error=f"archive failed: {exc}",
                ))
            continue

        if status.state is State.BLOCKED:
            result.blocked.append(status)
            continue

        if status.state is State.NEEDS_INPUT:
            # Defensive: a parked task normally lives in the needs_input/
            # tray (scanned below), not active/. If one is found here
            # (e.g. a hand-moved dir), surface it as needs_input rather
            # than letting it fall through to the in_progress classifier.
            result.needs_input.append(status)
            continue

        if status.state is State.PENDING:
            result.pending.append(status)
            continue

        # state is IN_PROGRESS by elimination (the enum has 4 values).
        # Classify by the attach signal (session_ref) + heartbeat:
        #   idle    = detached -> resumable now (no heartbeat read)
        #   busy    = attached + fresh -> a live session owns it
        #   crashed = attached + stale -> owner went silent, reclaimable
        # Wrap the heartbeat read because a malformed ``updated_at``
        # should flag just *this* task as malformed -- never abort the
        # classification of the others. (Idle short-circuits before any
        # heartbeat read, so a detached task with a bad timestamp is
        # still resumable.)
        try:
            klass = status.in_progress_class(
                stuck_timeout_sec=timeout, now=ts_now,
            )
        except JournalModelError as exc:
            result.malformed.append(MalformedEntry(
                task_id=task_id,
                error=f"heartbeat unreadable: {exc}",
            ))
            continue
        if klass == "crashed":
            # Second opinion before declaring a death. A long step can run
            # well past the timeout without refreshing the heartbeat, and
            # calling that "crashed" invites a rescue drive to fight the
            # session that is still working -- which is how an overloaded
            # box gets a pile-up. Recent writes in the task dir are proof
            # of life, so treat it as busy and wait one more sweep.
            # Only crashed is re-judged: `idle` means *detached*, which is
            # resumable by design and has nothing to do with liveness.
            age = newest_mtime_age_seconds(
                paths.active / task_id, now_epoch=_epoch_from_iso(ts_now),
            )
            if age <= timeout:
                log.info(
                    "sweep: %s heartbeat stale but files touched %.0fs ago "
                    "-- alive, not crashed", task_id, age,
                )
                klass = "busy"
        log.info("sweep: %s classified %s", task_id, klass)
        if klass == "idle":
            result.in_progress_idle.append(status)
        elif klass == "busy":
            result.in_progress_busy.append(status)
        elif klass == "crashed":
            result.in_progress_crashed.append(status)
        else:  # pragma: no cover -- in_progress_class is exhaustive
            # (idle/busy/crashed); this only fails loud if a future or
            # typo'd value appears, instead of silently mis-bucketing.
            raise JournalModelError(
                f"unexpected in_progress_class {klass!r} for task {task_id!r}"
            )

    # Scan the needs_input/ tray (parked tasks awaiting an Operator
    # answer). They are out of the active queue -- surfaced for
    # visibility, never actionable. A malformed status here is reported
    # like any other malformed entry rather than aborting the sweep.
    for task_id in paths.list_needs_input_ids():
        status_path = paths.needs_input_dir(task_id) / "status.json"
        try:
            status = Status.from_json(status_path.read_text())
        except (JournalModelError, OSError) as exc:
            result.malformed.append(
                MalformedEntry(task_id=task_id, error=str(exc))
            )
            continue
        result.needs_input.append(status)

    return result

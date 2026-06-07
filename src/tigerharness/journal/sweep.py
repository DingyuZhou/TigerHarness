"""The lazy sweep: classify ``active/`` and surface a summary.

Called by the ``drive-journal`` skill at the start of every invocation
and at every cascade boundary. It is **non-AI** -- plain Python -- and
side-effecting only in two precise ways:

1. Archive any task whose ``state`` is ``done`` (move ``active/<id>``
   to ``done/<id>``).
2. Surface a structured summary so the driver can decide what to pick
   up next.

That's it. The sweep does NOT mutate ``status.json`` of stale tasks;
classifying a task as stale is advisory. The driver may then pick up
a stale task as a rescue (and at that point the driver bumps
``sessions`` and ``updated_at`` as it would for any pickup).

The default heartbeat threshold is 1800 seconds (30 min), overridable
via ``TIGERHARNESS_JOURNAL_STUCK_TIMEOUT``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from tigerharness.journal.models import (
    JournalModelError,
    State,
    Status,
    _utcnow_iso,
)
from tigerharness.journal.paths import JournalPaths


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
    - ``malformed``: task directories whose ``status.json`` failed to
      parse (the sweep does not bail; the driver decides what to do).
    """

    archived: list[str] = field(default_factory=list)
    pending: list[Status] = field(default_factory=list)
    in_progress_idle: list[Status] = field(default_factory=list)
    in_progress_busy: list[Status] = field(default_factory=list)
    in_progress_crashed: list[Status] = field(default_factory=list)
    blocked: list[Status] = field(default_factory=list)
    malformed: list[MalformedEntry] = field(default_factory=list)

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
        ]
        if self.archived:
            parts.append(f"archived {len(self.archived)} done")
        if self.malformed:
            parts.append(f"{len(self.malformed)} malformed")
        return "Journal: " + ", ".join(parts) + "."


def sweep(
    paths: JournalPaths,
    *,
    stuck_timeout_sec: int | None = None,
    now: str | None = None,
) -> SweepResult:
    """Run one sweep over ``paths.active`` and return the classification.

    Side effects: archives any ``state=done`` task. No other writes.

    ``now`` is injected for tests so heartbeat ages are deterministic;
    in production it defaults to UTC now via ``_utcnow_iso``.
    """
    timeout = stuck_timeout_sec if stuck_timeout_sec is not None \
        else stuck_timeout_from_env()
    ts_now = now or _utcnow_iso()

    result = SweepResult()
    for task_id in paths.list_active_ids():
        status_path = paths.status_json(task_id)
        try:
            status = Status.from_json(status_path.read_text())
        except (JournalModelError, OSError) as exc:
            result.malformed.append(
                MalformedEntry(task_id=task_id, error=str(exc))
            )
            continue

        if status.state is State.DONE:
            # Archive: move active/<id>/ -> done/<id>/.
            try:
                paths.archive(task_id)
                result.archived.append(task_id)
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

    return result

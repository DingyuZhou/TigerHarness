"""Journal worklog source adapter -- per-persona journal memory.

A persona's worklog entries under a journal task become ONE synthetic
conversation per ``(task, persona)``, so a persona's tiger-memory store
captures the journal work IT personally did during a drive -- not the
driver's. See ``docs/per-persona-journal-memory.md``.

Per design doc section 3 (ingestion path):

    conversation_uuid = uuid5(URL_NS,
                              "journal:" + team + "/" + task_id
                              + "/" + persona)
    source            = "journal"
    source_id         = task_id + "/" + persona
    first_event_at    = earliest entry timestamp (started/ended), else
                        earliest worklog-file mtime
    last_event_at     = latest entry timestamp, else latest file mtime
    activity_mtime    = max worklog-file mtime among that persona's
                        entries (so a NEW entry re-triggers the cascade
                        decision in ``lifecycle._decide``)
    content           = the persona's entries for the task, chronological
    raw_path          = the task's worklog directory

Both ``active/`` and ``done/`` are scanned so a persona keeps memory of
a task after it is archived.

yaml-free underneath
--------------------
``journal.worklog`` parses its frontmatter without PyYAML. This adapter
runs only under the ``[memory]`` extra (the whole tiger-memory package
does), so importing it imposes no new yaml requirement on the *core*
journal write path -- the write side stays installable without extras.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import NAMESPACE_URL, uuid5

from tigerharness.journal import worklog
from tigerharness.journal.ids import is_safe_task_id
from tigerharness.journal.paths import JournalPaths
from tigerharness.journal.worklog import WorklogEntry

from .base import SourceAdapter, SourceRecord


def _parse_dt(ts: str | None) -> datetime | None:
    """Parse an ISO worklog timestamp; ``None`` on missing/garbage. The
    ``Z`` suffix is normalised so ``...T12:00:00Z`` parses on 3.11.

    The result is ALWAYS timezone-aware: a naive timestamp (e.g. a
    hand-edited ``...T12:00:00`` with no offset) is assumed UTC, matching
    the journal's UTC convention (:func:`journal.models._utcnow_iso`).
    Without this, a worklog mixing aware and naive stamps would make the
    ``min()``/``max()`` in :meth:`JournalWorklogAdapter._record_for` raise
    ``TypeError`` -- an error that escapes the per-task guard and aborts
    the persona's whole sweep, violating this adapter's "one bad file
    never aborts discovery" contract."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _format_entry(entry: WorklogEntry) -> str:
    """Render one worklog entry as a chronological transcript block:
    a ``[ts] persona (step=..., role=..., verdict=...)`` header line, an
    optional ``objective:`` line, then the markdown body. Optional fields
    are omitted when absent so a thin driver trace stays terse."""
    ts = entry.ended_at or entry.started_at or ""
    meta = [f"step={entry.step}"]
    if entry.role:
        meta.append(f"role={entry.role}")
    if entry.verdict:
        meta.append(f"verdict={entry.verdict}")
    lines = [f"[{ts}] {entry.persona} ({', '.join(meta)})"]
    if entry.objective:
        lines.append(f"objective: {entry.objective}")
    body = (entry.body or "").strip()
    if body:
        lines.append("")
        lines.append(body)
    return "\n".join(lines)


class JournalWorklogAdapter(SourceAdapter):
    """Discovers ``*/worklog/*.md`` under a journal root, filters to one
    persona, and emits one ``SourceRecord`` per task the persona worked
    on. ``team`` qualifies the ``conversation_uuid`` namespace so two
    teams' identically-named tasks never collide in a shared store."""

    kind = "journal_worklog"

    def __init__(self, *, journal_root: Path, persona: str, team: str):
        self.journal_root = Path(journal_root).expanduser()
        self.persona = persona
        self.team = team

    def discover(self) -> Iterator[SourceRecord]:
        paths = JournalPaths(self.journal_root)
        yield from self._discover_tree(paths, paths.active, archived=False)
        yield from self._discover_tree(paths, paths.done, archived=True)

    def _discover_tree(
        self,
        paths: JournalPaths,
        base_dir: Path,
        *,
        archived: bool,
    ) -> Iterator[SourceRecord]:
        if not base_dir.is_dir():
            return
        for entry in sorted(base_dir.iterdir()):
            if not entry.is_dir():
                continue
            task_id = entry.name
            if not is_safe_task_id(task_id):
                continue
            rec = self._record_for(paths, task_id, archived=archived)
            if rec is not None:
                yield rec

    def _record_for(
        self,
        paths: JournalPaths,
        task_id: str,
        *,
        archived: bool,
    ) -> SourceRecord | None:
        # Read is best-effort: a single corrupt/unreadable worklog file
        # under one task must not abort the whole sweep, so skip the task
        # rather than let the error propagate out of ``discover``. (Our
        # own writer always emits valid UTF-8; this guards externally
        # corrupted state.)
        try:
            all_entries = worklog.list_entries(paths, task_id, archived=archived)
        except (OSError, UnicodeDecodeError):
            return None
        entries = [e for e in all_entries if e.persona == self.persona]
        if not entries:
            return None

        # ``activity_mtime`` drives ``lifecycle._decide``: a worklog file
        # newer than the last summary re-triggers summarisation. Stat is
        # best-effort -- a file that vanished mid-scan is simply dropped,
        # and a task with no statable entry yields no record (never crash
        # discovery over a transient FS error).
        mtimes: list[float] = []
        for e in entries:
            try:
                mtimes.append(e.path.stat().st_mtime)
            except OSError:
                continue
        if not mtimes:
            return None

        stamps: list[datetime] = []
        for e in entries:
            for raw in (e.started_at, e.ended_at):
                dt = _parse_dt(raw)
                if dt is not None:
                    stamps.append(dt)
        if stamps:
            first_at = min(stamps)
            last_at = max(stamps)
        else:
            # No parseable frontmatter timestamps -- fall back to the
            # worklog files' own mtimes so the record still has a sane
            # event window.
            first_at = datetime.fromtimestamp(min(mtimes), tz=timezone.utc)
            last_at = datetime.fromtimestamp(max(mtimes), tz=timezone.utc)

        uid = str(
            uuid5(
                NAMESPACE_URL,
                f"journal:{self.team}/{task_id}/{self.persona}",
            )
        )
        content = "\n\n".join(_format_entry(e) for e in entries) + "\n"
        return SourceRecord(
            conversation_uuid=uid,
            source="journal",
            source_id=f"{task_id}/{self.persona}",
            first_event_at=first_at,
            last_event_at=last_at,
            activity_mtime=max(mtimes),
            content=content,
            raw_path=paths.worklog(task_id, archived=archived),
        )

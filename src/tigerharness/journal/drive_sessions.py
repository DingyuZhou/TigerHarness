"""Drive-session registry: the Slack ``thread_ts`` of journal *drives*.

A journal drive runs entirely inside one Claude session (the driver's),
which adopts personas in-session. tiger-memory's per-persona worklog
adapter (``journal_worklog``) now owns the substantive memory of that
work, sliced per persona, plus a thin "I drove this" trace for the
driver. But the drive session ALSO has a Claude Code transcript, which
the ``claude_transcript`` adapter would otherwise fold -- whole -- into
the driver's store, double-counting the work the worklog already
captured (and defeating the thin-driver-trace goal).

This registry is the suppression hook. At ``journal claim`` a drive
session records its own Slack ``thread_ts`` (learned from the
``[bridge-context]`` block appended to its prompt) here; the
``claude_transcript`` adapter reads the registry and skips any transcript
whose thread_ts is registered. See
``docs/per-persona-journal-memory.md`` (section 4).

yaml-free, tolerant
-------------------
Plain JSON (stdlib ``json``), so the core journal install works without
the ``[memory]`` extra -- mirrors ``journal.walk`` / ``journal.worklog``.
The reader is deliberately *tolerant*: any error (missing file, corrupt
JSON, wrong shape) yields the empty set, i.e. "suppress nothing." That
fails in the safe direction -- a degraded registry causes at worst a
double-counted driver transcript (a quality regression), never lost or
mis-attributed persona memory.

The ``claude_transcript`` adapter keeps its own tiny tolerant reader
(mirroring how it reads the bridge's ``threads.json`` inline rather than
importing the bridge), so it stays independent of the journal package at
import time; ``test_drive_sessions`` cross-checks the two readers agree.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tigerharness.journal.models import _utcnow_iso
from tigerharness.journal.paths import JournalPaths


# How long a drive thread stays in the registry, by ``last_seen_at``.
# Bounds growth across many drives over time. Deliberately far longer
# than any reasonable ``claude_transcript`` ingestion window
# (``max_age_days``, default 7): a pruned entry is one whose drive
# transcript is itself long past ingestion, so its suppression is already
# moot. An active drive re-claimed within the window refreshes
# ``last_seen_at`` and survives indefinitely.
_REGISTRY_TTL_DAYS = 30


def _write_atomic(path: Path, content: str) -> None:
    """Write *content* to *path* via a same-dir temp file + rename, so a
    reader never sees a half-written file even if we are SIGKILLed
    mid-write. Mirrors ``journal.walk._write_atomic`` (the journal package
    keeps a per-module copy rather than a shared util)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, path)
    finally:
        try:
            os.unlink(tmp.name)
        except FileNotFoundError:
            pass  # replaced successfully


def _read(path: Path) -> dict:
    """Tolerant read of the registry file: returns ``{}`` on a missing
    file, unreadable file, corrupt JSON, or non-object top level. Shared
    by :func:`register` (to merge) and :func:`registered_threads`."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_iso(ts: object) -> datetime | None:
    """Parse a stored ISO-8601 UTC stamp (``...Z``) to a tz-aware
    datetime; ``None`` if absent, not a string, malformed, or naive. We
    only trust an explicit-UTC stamp for expiry math -- anything else is
    treated as unknown and kept (the fail-safe direction)."""
    if not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def _prune(data: dict, now: str, *, ttl_days: int = _REGISTRY_TTL_DAYS) -> dict:
    """Return *data* without entries whose ``last_seen_at`` is older than
    *ttl_days* before *now*. Entries with a missing/unparseable
    ``last_seen_at`` (or a non-dict value) are KEPT: we never drop an
    entry we cannot evaluate -- an over-long registry is a bounded
    annoyance, but dropping a live suppression would double-count a
    transcript. If *now* itself is unparseable, nothing is pruned."""
    now_dt = _parse_iso(now)
    if now_dt is None:
        return dict(data)
    cutoff = now_dt - timedelta(days=ttl_days)
    kept: dict = {}
    for thread_ts, entry in data.items():
        seen = _parse_iso(entry.get("last_seen_at")) if isinstance(entry, dict) else None
        if seen is not None and seen < cutoff:
            continue  # expired -- transcript long past ingestion
        kept[thread_ts] = entry
    return kept


def registered_threads(path: Path) -> set[str]:
    """The set of registered drive ``thread_ts`` values (the registry's
    keys). Empty on any error -- the safe direction: suppress nothing.

    Takes a plain ``Path`` (not ``JournalPaths``) so the caller -- the
    ``claude_transcript`` adapter's wiring -- need not depend on the
    journal path layer."""
    return set(_read(path))


def register(
    paths: JournalPaths,
    thread_ts: str,
    *,
    task_id: str,
    driver: str | None = None,
) -> None:
    """Record *thread_ts* as a drive session (idempotent upsert).

    Merges into any existing registry: a thread's first sighting stamps
    ``registered_at``; later claims under the same thread refresh
    ``last_seen_at`` (and the most-recent ``task_id`` / ``driver``) while
    preserving the original ``registered_at``. Writes atomically. Each
    write also prunes entries past ``_REGISTRY_TTL_DAYS`` (by
    ``last_seen_at``), so the registry stays bounded without a separate
    sweep.

    Raises ``OSError`` on a disk failure -- the caller (``cmd_claim``)
    treats registration as best-effort and warns rather than failing an
    otherwise-successful claim.
    """
    now = _utcnow_iso()
    data = _prune(_read(paths.drive_sessions_json), now)
    prior = data.get(thread_ts)
    registered_at = now
    if isinstance(prior, dict) and isinstance(prior.get("registered_at"), str):
        registered_at = prior["registered_at"]
    data[thread_ts] = {
        "task_id": task_id,
        "driver": driver,
        "registered_at": registered_at,
        "last_seen_at": now,
    }
    _write_atomic(
        paths.drive_sessions_json,
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
    )

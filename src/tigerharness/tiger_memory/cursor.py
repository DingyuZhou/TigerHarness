"""Per-session high-water-mark cursors for incremental sweep (ADR 0006 Part 2).

A cursor records, per ``conversation_uuid``, how far a sweep has already
processed that session — so the next sweep stages only the *post-cursor*
slice instead of re-reading the whole transcript. The cursor is

    {conversation_uuid: {last_event_at: ISO, processed_events: int}}

stored in ``.sweep-cursors.json`` in the persona store root (a sibling of
``.state.json`` / ``.sweep-staging/``), kept separate so ``.state.json``'s
schema stays stable and the two never contend on the same write.

``last_event_at`` is the timestamp of the last processed turn (the slice cut);
``processed_events`` is a count guard so a changed event-filter can't silently
re-process or skip (the slice computation discards the cursor and re-runs a
full pass when the recomputed pre-cursor turn count no longer matches).

**Advance is ordering-protected, not transactional.** The cursor advances
ONLY inside :func:`on_slice_ingested`, strictly AFTER the slice's card is
merged into the stores. Because ingest is idempotent (a re-run writes the same
deterministic card), a crash between ingest and advance only causes a harmless
re-process on the next sweep — never a skip, which would be the data-loss
reincarnation ADR 0006 closes.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

from .store import Store

log = logging.getLogger("tigerharness.tiger_memory.cursor")

CURSORS_FILENAME = ".sweep-cursors.json"


@dataclass(frozen=True)
class Cursor:
    """One session's high-water mark."""

    last_event_at: str       # ISO timestamp of the last processed turn
    processed_events: int    # count of turns at/before the boundary (sanity guard)


def _cursors_path(store: Store):
    return store.root / CURSORS_FILENAME


def load_cursors(store: Store) -> dict[str, Cursor]:
    """Load every cursor; a missing/corrupt/ill-typed file is an empty map.

    Tolerance is deliberate: a lost cursor file must degrade to a full first
    pass (re-process, never skip), so any read/parse problem is treated as "no
    cursors yet" rather than an error.
    """
    path = _cursors_path(store)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Cursor] = {}
    for uuid, c in raw.items():
        if not (isinstance(c, dict) and "last_event_at" in c):
            continue
        try:
            out[uuid] = Cursor(
                last_event_at=str(c["last_event_at"]),
                processed_events=int(c.get("processed_events", 0)),
            )
        except (TypeError, ValueError):
            continue
    return out


def load_cursor(store: Store, conversation_uuid: str) -> Cursor | None:
    """The cursor for one session, or ``None`` if none is recorded."""
    return load_cursors(store).get(conversation_uuid)


def save_cursor(store: Store, conversation_uuid: str, cursor: Cursor) -> None:
    """Write/advance one cursor atomically (tmp + ``os.replace``).

    Reads the whole map, replaces this uuid's entry, and rewrites the file in a
    single atomic rename so a concurrent reader never sees a half-written map.
    """
    path = _cursors_path(store)
    cursors = load_cursors(store)
    cursors[conversation_uuid] = cursor
    payload = {
        u: {"last_event_at": c.last_event_at, "processed_events": c.processed_events}
        for u, c in cursors.items()
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def on_slice_ingested(
    store: Store,
    conversation_uuid: str,
    *,
    slice_end_event_at: str,
    processed_events: int,
) -> None:
    """Advance a session's cursor after its slice's card is ingested.

    Called by the ingest path EXACTLY ONCE per uuid, strictly AFTER the card is
    merged. See the module docstring for the ordering-not-transaction invariant
    (safe because ingest is idempotent).
    """
    log.info(
        "cursor advance: %s -> %s (%d events)",
        conversation_uuid, slice_end_event_at, processed_events,
    )
    save_cursor(
        store,
        conversation_uuid,
        Cursor(last_event_at=slice_end_event_at, processed_events=processed_events),
    )

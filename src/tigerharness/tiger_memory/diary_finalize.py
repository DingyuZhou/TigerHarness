"""Shared diary finalize mechanics (plan §1/§6 dev-1/Miyagi).

The bound (forget) pass and the destructive ``snapshot -> write -> mark`` apply
path are needed by BOTH :mod:`migrate_emotional_to_diary` (legacy conversion)
and :mod:`regenerate_diary` (rollup regeneration). Factoring them here gives ONE
implementation of the write path, so the per-store lock window is identical and
minimal for both callers — no copy-paste that could drift (Akagi compile note).

All three names are pure of any summarizer/agent dependency:

- :data:`STATE_KEY` — the ``.state.json`` idempotency marker. Shared, so a store
  finalized by EITHER path is skipped by the other.
- :func:`forget_to_max` — keep the highest-ranked bullets (``|weight|`` then
  date) whose serialized length fits the character bound; report the drop count.
- :func:`finalize_diary` — snapshot ``emotional.md`` -> ``.bak``, write the
  validated ``diary.md``, and stamp the marker, all under the diary store lock so
  the write cannot race a live meditation.
"""
from __future__ import annotations

import logging

from . import diary_format
from .bounded_store import BoundedStore, StoreLockHeld
from .config import Config
from .entries import STORE_DIARY
from .store import Store

log = logging.getLogger("tigerharness.tiger_memory.diary_finalize")

#: ``.state.json`` key marking a completed diary migration/regeneration.
STATE_KEY = "diary_migrated"

#: Skip reason when a live session holds the diary store lock (no write done).
LOCKED_SKIP = "diary store locked by a live session"


def forget_to_max(
    bullets: list[diary_format.DiaryEntry], max_length: int
) -> tuple[list[diary_format.DiaryEntry], int]:
    """Keep the highest-ranked bullets whose serialized length <= *max_length*.

    Ranks by ``|weight|`` then date (newest first) — the retention guarantee:
    strong feelings (for or against) survive while near-neutral / older bullets
    are dropped first. Returns ``(kept, forgotten_count)``. A diary already under
    the bound is returned unchanged with a zero drop count. Length is measured in
    **characters** (vendor-neutral), never tokens.
    """
    if len(diary_format.serialize(bullets)) <= max_length:
        return bullets, 0
    ranked = sorted(bullets, key=lambda b: (abs(b.weight), b.date), reverse=True)
    kept: list[diary_format.DiaryEntry] = []
    for b in ranked:
        if len(diary_format.serialize(kept + [b])) <= max_length:
            kept.append(b)
    return kept, len(bullets) - len(kept)


def finalize_diary(
    cfg: Config, store: Store, serialized: str, *, state_extra: dict
) -> str | None:
    """Write *serialized* diary to the live store under the per-store lock.

    The destructive sequence — snapshot ``emotional.md`` -> ``emotional.md.bak``
    AND any existing ``diary.md`` -> ``diary.md.bak`` (each only if present),
    ``atomic_write`` of the new ``diary.md``, and stamping the :data:`STATE_KEY`
    marker with *state_extra* — runs entirely inside ``store_lock(STORE_DIARY)``
    so it can never race a concurrent meditation (plan §6). Snapshotting an
    existing diary.md is the 4-store correction (brief): a persona that already
    authored a diary must not be overwritten without a backup. Returns ``None``
    on success, or :data:`LOCKED_SKIP` if a live session holds the lock (nothing
    is written). Each caller supplies its own marker payload via *state_extra*.
    """
    emo_path = store.paths.journal / "emotional.md"
    diary_path = store.paths.journal / "diary.md"
    try:
        with BoundedStore(cfg, store).store_lock(STORE_DIARY):
            store.paths.journal.mkdir(parents=True, exist_ok=True)
            if emo_path.exists():
                emo_path.rename(emo_path.with_suffix(".md.bak"))
            if diary_path.exists():
                diary_path.rename(diary_path.with_suffix(".md.bak"))
            store.atomic_write(diary_path, serialized)
            state = store.read_state() or {}
            state[STATE_KEY] = state_extra
            store.write_state(state)
    except StoreLockHeld:
        return LOCKED_SKIP
    return None

"""Fuzz-candidate selection for the 4-store meditation (brief §meditation 4).

Pure, deterministic, no I/O and no summarizer — fully unit-testable. The
meditation pipeline calls these to decide which sharp-store items have aged out
and should be routed to the fuzzy store (re-compacted, never dropped — the
no-hard-drop invariant, plan §7). The fresh-window guard is the load-bearing
rule: a diary item dated within ``memory.diary.fresh_days`` of "now" is ALWAYS
kept verbatim (incl. 0-weight), regardless of bound.
"""
from __future__ import annotations

from . import diary_format
from .config import Config
from .diary import diary_keep_rank
from .entries import DiaryEntry
from .ranking import days_between


def _diary_len(entries: list[DiaryEntry]) -> int:
    """Serialized character length of *entries* (matches the store's measure)."""
    bullets = [
        diary_format.DiaryEntry(
            date=e.last_used[:10], weight=float(e.weight), text=e.text
        )
        for e in entries
    ]
    return len(diary_format.serialize(bullets))


def select_diary_fuzz(
    entries: list[DiaryEntry], now: str, cfg: Config
) -> tuple[list[DiaryEntry], list[DiaryEntry]]:
    """Split diary *entries* into ``(kept, fuzzed)`` for meditation.

    - Items within ``fresh_days`` of *now* are ALWAYS kept (verbatim, any weight
      incl. 0) — never fuzzed.
    - If the whole diary is within ``max_length``, everything is kept (no fuzz).
    - Otherwise the lowest keep-ranked AGED items (decayed ``|weight|`` then
      recency, via :func:`diary_keep_rank`) are routed to ``fuzzed`` one at a
      time until the surviving set fits ``max_length`` — or until no aged item
      remains (the fresh window alone may legitimately exceed the bound; it is
      still kept, per the brief). Fuzzed items are returned for re-compaction
      into the fuzzy store; they are NOT dropped (no silent loss).

    Order of ``kept`` follows the input; ``fuzzed`` is in drop order
    (lowest-value first).
    """
    max_length = cfg.memory.diary.max_length
    fresh_days = cfg.memory.diary.fresh_days
    aged = [e for e in entries if days_between(e.last_used, now) > fresh_days]
    drop_order = sorted(aged, key=lambda e: diary_keep_rank(e, now, cfg))

    keep_ids = {e.id for e in entries}
    fuzzed: list[DiaryEntry] = []
    for e in drop_order:
        if _diary_len([x for x in entries if x.id in keep_ids]) <= max_length:
            break
        keep_ids.discard(e.id)
        fuzzed.append(e)
    kept = [e for e in entries if e.id in keep_ids]
    return kept, fuzzed

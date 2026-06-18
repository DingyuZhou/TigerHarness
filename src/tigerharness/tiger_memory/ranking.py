"""Shared recency / date-math helpers for the keep-ranking (plan §2 dev-2).

The three stores rank entries for keep/forget using a magnitude-or-importance
scalar plus **recency of use**. Recency is derived from each entry's
``last_used`` ISO-8601 UTC timestamp (``state.iso_now()`` style: ends in a
``Z``). These helpers are pure, deterministic, and vendor-neutral — no I/O,
no summarizer, no token math.
"""
from __future__ import annotations

from datetime import datetime, timezone


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO-8601 UTC timestamp (trailing ``Z`` tolerated).

    Returns ``None`` on any malformed / empty input — callers treat an
    unparseable timestamp as "infinitely old" (least recent), so a corrupt
    entry sinks to the bottom of the keep-rank rather than raising.
    """
    if not ts:
        return None
    cleaned = ts[:-1] if ts.endswith("Z") else ts
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def days_between(start: str, end: str) -> float:
    """Whole+fractional days from *start* to *end* (0 if *end* precedes start).

    Either timestamp being unparseable yields ``0.0`` (no measurable elapsed
    time — decay can't act on a date it cannot read). Negative spans (clock
    skew / out-of-order timestamps) are floored at 0 so decay never *grows*
    a magnitude.
    """
    a, b = _parse_iso(start), _parse_iso(end)
    if a is None or b is None:
        return 0.0
    seconds = (b - a).total_seconds()
    if seconds <= 0:
        return 0.0
    return seconds / 86400.0


def recency_score(last_used: str, now: str) -> float:
    """A higher-is-fresher recency score for keep-ranking.

    Defined as the negative age in days, so a more-recent ``last_used`` (small
    age) scores higher than an old one. An unparseable timestamp scores
    ``-inf`` (treated as infinitely old → forgotten first).
    """
    last = _parse_iso(last_used)
    if last is None:
        return float("-inf")
    return -days_between(last_used, now)

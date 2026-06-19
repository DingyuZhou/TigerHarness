"""Signed-weight scoring for the diary store (design §4.3; plan §2 dev-2).

The diary store carries a single signed scalar per entry — a ``weight``
in ``[-weight_cap, +weight_cap]`` (default ±10, design §4.3): positive =
*for* / liked, negative = *against* / disliked, ``0`` = neutral.

Two pure functions live here, with NO I/O and NO summarizer dependency
(deterministic, fully unit-testable):

- :func:`decay_weight` — each day ``|weight|`` shrinks toward 0 by
  ``magnitude_per_day * days``; the **sign is preserved until the magnitude
  reaches exactly 0** (never flips, never overshoots into the opposite sign,
  never produces ``-0.0``).
- :func:`clamp_weight` — a hard clamp to ``[-weight_cap, +weight_cap]``,
  applied on every merge/update so repeated merges can never inflate a single
  memory past the cap (design §4.3).

Forgetting ranks by **magnitude** ``|weight|`` plus recency, so strong
feelings (positive *or* negative) survive while near-neutral / decayed items
are compacted or forgotten first. :func:`diary_keep_rank` produces that
sort key (anchored on each entry's dated-bullet day via ``last_used``); see
:mod:`tigerharness.tiger_memory.meditation` for how it drops the lowest-ranked
entries to keep the whole-loaded diary under its bound.
"""
from __future__ import annotations

import math

from .config import Config
from .entries import DiaryEntry, EntryError
from .ranking import days_between, recency_score


def clamp_weight(weight: float, cfg: Config) -> float:
    """Clamp *weight* to ``[-weight_cap, +weight_cap]`` (design §4.3).

    Applied on every merge/update: merging bumps a survivor's magnitude, and
    this guarantees the bump can never push it past the configured hard cap.
    Returns a plain ``float`` (never ``-0.0`` — see :func:`_zero_safe`).

    **Non-finite defense (GAP-3, defense in depth).** Mitsui's
    :meth:`DiaryEntry.validate` already rejects a non-finite weight at the
    schema/load gate, so a ``NaN``/``±inf`` should never reach here. But the
    scoring math must not *silently* propagate one if it ever does (a ``NaN``
    poisons the keep-rank ``sorted()`` into a non-deterministic order — the
    GAP-3 symptom). So:

    - ``+inf`` clamps to ``+cap`` and ``-inf`` to ``-cap`` (an over-cap value
      is, semantically, exactly what the cap exists to bound — the existing
      ``> cap`` / ``< -cap`` branches already do this; documented here so it
      is intentional, not incidental);
    - ``NaN`` has no defensible clamp target (it is unordered against the cap),
      so it is **rejected** with :class:`EntryError` rather than returned —
      consistent with the schema gate, and it can never silently enter the
      ranking math.
    """
    if math.isnan(weight):
        raise EntryError(
            "diary.weight must be finite (no NaN); cannot clamp NaN."
        )
    cap = cfg.memory.diary.weight_cap
    if weight > cap:
        return cap
    if weight < -cap:
        return -cap
    return _zero_safe(float(weight))


def decay_weight(weight: float, days: float, cfg: Config) -> float:
    """Shrink ``|weight|`` toward 0 by ``magnitude_per_day * days`` (design §4.3).

    The sign is preserved until the magnitude reaches **exactly 0**; the
    result never overshoots into the opposite sign and never yields negative
    zero. ``days <= 0`` (no elapsed time) returns the (clamped) input
    unchanged. A non-positive ``magnitude_per_day`` (decay disabled) likewise
    leaves the magnitude alone. The result is always within the cap.
    """
    clamped = clamp_weight(weight, cfg)
    rate = cfg.memory.diary.decay.magnitude_per_day
    if days <= 0 or rate <= 0 or clamped == 0:
        return clamped
    shrink = rate * days
    magnitude = abs(clamped) - shrink
    if magnitude <= 0:
        # Reached (or passed) 0: pin at 0 — never flip sign, never overshoot.
        return 0.0
    sign = 1.0 if clamped > 0 else -1.0
    return _zero_safe(sign * magnitude)


def decay_entry(entry: DiaryEntry, now: str, cfg: Config) -> float:
    """Decayed weight for *entry* as of *now* (its ``last_used`` is the anchor).

    A convenience over :func:`decay_weight` that derives ``days`` from the
    entry's ``last_used`` timestamp. Used by the keep-rank and by meditation
    to refresh a survivor's effective magnitude before ranking.
    """
    days = days_between(entry.last_used, now)
    return decay_weight(entry.weight, days, cfg)


def diary_keep_rank(
    entry: DiaryEntry, now: str, cfg: Config
) -> tuple[float, float]:
    """Keep-rank for one emotional entry: higher = more worth keeping.

    Ranks by **decayed magnitude** ``|weight|`` first (strong feelings, for or
    against, survive), then by recency of use as a tie-breaker so that, among
    equally-weighted memories, the freshest is kept. Sorting entries by this
    key **ascending** puts the lowest-value (near-neutral / stale) entries
    first — exactly the forget order (design §4.3).
    """
    magnitude = abs(decay_entry(entry, now, cfg))
    return (magnitude, recency_score(entry.last_used, now))


def _zero_safe(value: float) -> float:
    """Return *value*, collapsing ``-0.0`` to ``0.0`` (sign hygiene)."""
    return value + 0.0 if value == 0 else value

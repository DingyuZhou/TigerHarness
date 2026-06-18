"""Skill-importance scoring for the skills store (design §4.1, §10.3).

A skill's ``importance`` **grows with use** — the more often it is invoked,
the more it matters. Crucially there is **NO continuous time-decay** of a
skill's importance (design §10.3): an unused skill does not bleed importance
day by day. Instead, recency of use feeds the *keep-ranking* so an old,
unused skill ranks lower and is forgotten first — the importance scalar
itself stays put until the skill is used again.

:func:`skill_importance` is the pure, deterministic scoring function (no I/O,
no summarizer). It is monotonic non-decreasing in ``usage_count`` and is
**independent of elapsed time** — ``last_used`` and ``now`` are accepted for
signature symmetry with the other stores and to make the no-time-decay
contract explicit/testable, but they never lower the returned importance.

:func:`skills_keep_rank` combines that importance with recency so meditation
can drop the oldest, least-used skills first (design §4.1 overflow rule).
"""
from __future__ import annotations

import math

from .config import Config
from .entries import SkillEntry
from .ranking import recency_score


def skill_importance(
    usage_count: int, last_used: str, now: str, cfg: Config
) -> float:
    """Importance derived from ``usage_count`` — grows with use, no time-decay.

    Uses a diminishing-returns curve (``log1p``): each additional invocation
    raises importance, but the first few uses matter most, so a skill used
    twice is clearly above a never-reused one while a heavily-used skill does
    not run away to unbounded importance. A never-used skill
    (``usage_count == 0``) scores ``0.0``.

    Per design §10.3 there is **no continuous time-decay**: ``last_used`` and
    ``now`` do not lower this value (recency lives in the keep-rank instead).
    A negative ``usage_count`` is treated as 0 (defensive — validation already
    forbids it, but scoring stays total).
    """
    del last_used, now  # intentionally unused: importance never time-decays.
    count = max(0, usage_count)
    return math.log1p(count)


def skills_keep_rank(
    entry: SkillEntry, now: str, cfg: Config
) -> tuple[float, float]:
    """Keep-rank for one skill: higher = more worth keeping.

    Ranks by importance(usage) first, then recency of use as the tie-breaker
    (design §4.1: "an old, unused skill ranks lower and is forgotten first").
    The stored ``entry.importance`` is *recomputed* from the live
    ``usage_count`` here so the rank reflects current usage even if the stored
    scalar is stale. Sorting ascending by this key puts the lowest-value
    (least-used, oldest) skills first — the forget order.
    """
    importance = skill_importance(entry.usage_count, entry.last_used, now, cfg)
    return (importance, recency_score(entry.last_used, now))


def refresh_importance(entry: SkillEntry, now: str, cfg: Config) -> None:
    """Recompute and store *entry*'s ``importance`` from its ``usage_count``.

    Meditation calls this so the persisted scalar (read by the skill index /
    keep-rank without recomputing) reflects current usage. Mutates in place.
    """
    entry.importance = skill_importance(
        entry.usage_count, entry.last_used, now, cfg
    )

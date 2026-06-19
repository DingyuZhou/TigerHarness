"""Regenerate a persona's diary from ALREADY-PRODUCED diary text (plan §6 dev-1).

The diary text is produced upstream on the **subscription rail** by a constrained
sub-agent reading the persona's git-recovered daily rollups (cohort 1), or is the
persona's own authored ``diary.md`` (cohort 2, kept verbatim then bounded). This
module does NOT generate content and NEVER spawns agents — it is pure Python, so
it is fully unit-testable. It validates the text, bounds it via the shared forget
pass, and finalizes it to the live store via the shared locked apply-flow. Both
this and :mod:`migrate_emotional_to_diary` go through :mod:`diary_finalize`, so
there is ONE destructive write path.

The :class:`RegenResult` IS the per-persona accounting row (plan §5): every
generated bullet is either kept or forgotten — no silent loss — and the
``forgotten`` set is itemized.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import diary_format
from .config import Config
from .diary_finalize import STATE_KEY, finalize_diary, forget_to_max
from .store import Store

log = logging.getLogger("tigerharness.tiger_memory.regenerate_diary")


@dataclass
class RegenResult:
    """Outcome of regenerating one persona's diary — the accounting row (§5)."""

    persona: str
    cohort: str = "1"
    source_days: int = 0
    bullets_generated: int = 0
    bullets_kept: int = 0
    bullets_forgotten: int = 0
    header_days: int = 0
    final_chars: int = 0
    applied: bool = False
    skipped_reason: str | None = None
    #: ``(date, weight, text)`` for each bullet the bound pass dropped.
    forgotten_items: list[tuple[str, float, str]] = field(default_factory=list)

    @property
    def no_loss(self) -> bool:
        """Every generated bullet is accounted for (kept or forgotten)."""
        return self.bullets_generated == self.bullets_kept + self.bullets_forgotten


def regenerate_store(
    cfg: Config,
    store: Store,
    diary_text: str,
    *,
    cohort: str = "1",
    apply: bool = False,
) -> RegenResult:
    """Validate -> bound -> finalize a regenerated (or authored) diary.

    Safe + idempotent:

    - a store already carrying the :data:`STATE_KEY` marker is skipped;
    - malformed *diary_text* is refused (skip reason), never written;
    - an EMPTY diary is refused — the non-empty floor / zero-source guard
      (plan §5): a persona with no usable bullets is surfaced as a skip, never
      written as an empty file;
    - ``apply=False`` (default) computes the full accounting but writes nothing.

    With ``apply=True`` the destructive write goes through the shared, lock-guarded
    :func:`finalize_diary`, so it can't race a live meditation.
    """
    res = RegenResult(cfg.agent.name, cohort=cohort)
    if (store.read_state() or {}).get(STATE_KEY):
        res.skipped_reason = "already migrated"
        return res

    errs = diary_format.validate(diary_text, cfg.memory.diary.weight_cap)
    if errs:
        res.skipped_reason = f"invalid diary text: {errs[0]}"
        return res

    bullets = diary_format.parse(diary_text, cfg.memory.diary.weight_cap)
    res.bullets_generated = len(bullets)
    res.source_days = len({b.date for b in bullets})
    if not bullets:
        res.skipped_reason = "empty source: no diary bullets generated"
        return res

    kept, res.bullets_forgotten = forget_to_max(bullets, cfg.memory.diary.max_length)
    res.bullets_kept = len(kept)
    kept_ids = {id(b) for b in kept}
    res.forgotten_items = [
        (b.date, b.weight, b.text) for b in bullets if id(b) not in kept_ids
    ]
    res.header_days = len({b.date for b in kept})

    serialized = diary_format.serialize(kept)
    res.final_chars = len(serialized)
    post = diary_format.validate(serialized, cfg.memory.diary.weight_cap)
    if post:  # pragma: no cover - defensive; kept bullets are valid by construction
        raise ValueError(f"regenerated diary invalid after bound: {post[0]}")

    if apply:
        skip = finalize_diary(
            cfg,
            store,
            serialized,
            state_extra={
                "regenerated": True,
                "cohort": res.cohort,
                "generated": res.bullets_generated,
                "kept": res.bullets_kept,
                "forgotten": res.bullets_forgotten,
            },
        )
        if skip is None:
            res.applied = True
            log.warning(
                "regenerate(%s): APPLIED — %d generated -> %d kept (%d forgotten); "
                "emotional.md backed up to emotional.md.bak",
                res.persona, res.bullets_generated, res.bullets_kept,
                res.bullets_forgotten,
            )
        else:
            res.skipped_reason = skip
            log.warning("regenerate(%s): SKIPPED — %s", res.persona, skip)
    return res

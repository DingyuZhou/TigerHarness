"""One-off migration: legacy ``emotional.md`` -> diary ``diary.md`` (plan §C).

Converts a persona's OLD third-store file — per-entry YAML frontmatter blocks
``{weight, reaction, created_at, last_used, ...}`` + a substantive body — into
the new compact dated-bullet diary format. The map (plan §6):

- ``last_used`` (or ``created_at``) date -> the ``## YYYY-MM-DD`` day header;
- ``weight`` -> the inline ``(±N)`` (clamped to the configured cap);
- the entry **body** -> the bullet note (the substantive content; the old
  ``reaction`` label's valence is carried by the sign of the weight, per the
  redesign — the body is never dropped).

Safety (plan §D / §6):

- ``--dry-run`` is the DEFAULT: it parses + previews, touching nothing.
- ``apply=True`` snapshots ``emotional.md`` -> ``emotional.md.bak``, writes the
  validated ``diary.md``, removes ``emotional.md``, and records a durable
  ``diary_migrated`` marker in ``.state.json`` (idempotent — a second run is a
  no-op, keyed on the marker, NOT on file presence).
- **No silent loss**: every source block is accounted for —
  ``source_blocks == converted`` and ``converted == kept + forgotten``. A
  converted file over ``max_length`` is bounded by a **forget pass** (drop the
  lowest-ranked bullets by ``|weight|`` then date), and the drop count is
  reported (logged), never silent.

The live ``--apply`` over the 9 real persona stores is OPERATOR-GATED: this
module is built + verified on COPIES; the run is the Operator's.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from . import diary_format, frontmatter
from .bounded_store import _split_blocks
from .config import Config
from .diary import clamp_weight
from .store import Store

log = logging.getLogger("tigerharness.tiger_memory.migrate_emotional_to_diary")

#: ``.state.json`` key marking a completed diary migration (idempotency guard).
STATE_KEY = "diary_migrated"


@dataclass
class MigrationResult:
    """Outcome of migrating one persona's emotional store."""

    persona: str
    source_blocks: int = 0
    converted: int = 0
    kept: int = 0
    forgotten: int = 0
    applied: bool = False
    skipped_reason: str | None = None

    @property
    def no_loss(self) -> bool:
        """Every source block is accounted for (converted; kept or forgotten)."""
        return (
            self.source_blocks == self.converted
            and self.converted == self.kept + self.forgotten
        )


def _legacy_bullets(text: str, cfg: Config) -> tuple[int, list[diary_format.DiaryEntry]]:
    """Parse legacy emotional.md text -> (source_block_count, diary bullets)."""
    source_blocks = 0
    bullets: list[diary_format.DiaryEntry] = []
    for block in _split_blocks(text):
        fm, body = frontmatter.parse(block)
        if not fm:
            continue
        source_blocks += 1
        try:
            weight = clamp_weight(float(fm.get("weight", 0.0)), cfg)
        except (TypeError, ValueError):
            log.warning("migrate: skipping block with bad weight %r", fm.get("weight"))
            continue
        date = str(fm.get("last_used") or fm.get("created_at") or "")[:10]
        if not diary_format._valid_day(date):
            log.warning("migrate: skipping block with bad date %r", date)
            continue
        note = body.strip() or str(fm.get("reaction") or "").strip()
        if not note:
            log.warning("migrate: skipping block with empty note (id=%r)", fm.get("id"))
            continue
        bullets.append(diary_format.DiaryEntry(date=date, weight=weight, text=note))
    return source_blocks, bullets


def _forget_to_max(
    bullets: list[diary_format.DiaryEntry], max_length: int
) -> tuple[list[diary_format.DiaryEntry], int]:
    """Keep the highest-ranked bullets whose serialized length <= *max_length*.

    Ranks by ``|weight|`` then date (newest) — the retention guarantee: strong
    feelings survive. Returns ``(kept, forgotten_count)``.
    """
    if len(diary_format.serialize(bullets)) <= max_length:
        return bullets, 0
    ranked = sorted(bullets, key=lambda b: (abs(b.weight), b.date), reverse=True)
    kept: list[diary_format.DiaryEntry] = []
    for b in ranked:
        if len(diary_format.serialize(kept + [b])) <= max_length:
            kept.append(b)
    return kept, len(bullets) - len(kept)


def migrate_store(cfg: Config, store: Store, *, apply: bool = False) -> MigrationResult:
    """Migrate one persona's emotional store to the diary format.

    Idempotent + safe: a no-op if already migrated (marker) or there is no
    ``emotional.md``. With ``apply=False`` (default) nothing is written.
    """
    res = MigrationResult(cfg.agent.name)
    emo_path = store.paths.journal / "emotional.md"
    if (store.read_state() or {}).get(STATE_KEY):
        res.skipped_reason = "already migrated"
        return res
    if not emo_path.exists():
        res.skipped_reason = "no emotional.md"
        return res

    text = emo_path.read_bytes().decode("utf-8", errors="replace")
    res.source_blocks, bullets = _legacy_bullets(text, cfg)
    res.converted = len(bullets)
    kept, res.forgotten = _forget_to_max(bullets, cfg.memory.diary.max_length)
    res.kept = len(kept)

    serialized = diary_format.serialize(kept)
    errs = diary_format.validate(serialized, cfg.memory.diary.weight_cap)
    if errs:  # pragma: no cover - defensive; the bullets are built valid
        raise ValueError(f"migration produced an invalid diary: {errs[0]}")

    if apply:
        store.paths.journal.mkdir(parents=True, exist_ok=True)
        emo_path.rename(emo_path.with_suffix(".md.bak"))
        store.atomic_write(store.paths.journal / "diary.md", serialized)
        state = store.read_state() or {}
        state[STATE_KEY] = {"converted": res.converted, "kept": res.kept,
                            "forgotten": res.forgotten}
        store.write_state(state)
        res.applied = True
        log.warning(
            "migrate(%s): APPLIED — %d source -> %d kept (%d forgotten over max); "
            "emotional.md backed up to emotional.md.bak",
            res.persona, res.source_blocks, res.kept, res.forgotten,
        )
    return res

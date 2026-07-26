"""One-off migration to the topic-store model (ADR 0007).

``tiger-memory migrate-to-topics`` retires a persona store's diary/fuzzy
surface and prepares the topics store:

- ``journal/diary.md`` / ``journal/fuzzy.md`` (and their quarantine
  sidecars, plus a leftover legacy ``emotional.md``) move to
  ``<root>/retired/`` — nothing loads them any more, but the content stays
  on disk (and in git history) rather than being destroyed;
- ``journal/topics.md`` is created empty if absent, so the store layout is
  complete before the first post-migration sweep files topics into it;
- skills and must_remember are left in place — the same entry format is
  still live, and the tightened bounds are enforced by the first
  post-migration compaction, not by the migration.

Idempotent: re-running on a migrated store is a no-op report. Dry-run by
default; ``--apply`` performs it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .bounded_store import BoundedStore
from .config import Config
from .entries import STORE_TOPICS
from .store import Store

log = logging.getLogger("tigerharness.tiger_memory.migrate_topics")

RETIRED_DIR_NAME = "retired"

# Journal files the topic-store model retires. ``emotional.md`` is the
# pre-diary name; a store migrated long ago may still carry sidecars.
_RETIRED_FILES = (
    "diary.md",
    "diary.rejected.md",
    "fuzzy.md",
    "fuzzy.rejected.md",
    "emotional.md",
    "emotional.rejected.md",
)


@dataclass
class MigrationReport:
    persona: str
    applied: bool
    retired: list[str] = field(default_factory=list)
    topics_created: bool = False

    def to_dict(self) -> dict:
        return {
            "persona": self.persona,
            "applied": self.applied,
            "retired": self.retired,
            "topics_created": self.topics_created,
        }


def migrate_store(cfg: Config, store: Store, *, apply: bool) -> MigrationReport:
    """Retire diary/fuzzy files and create the topics store (dry-run default)."""
    store.init_layout()
    journal = store.paths.journal
    report = MigrationReport(persona=cfg.agent.name, applied=apply)

    to_retire = [name for name in _RETIRED_FILES if (journal / name).exists()]
    report.retired = to_retire
    topics_path = journal / "topics.md"
    report.topics_created = not topics_path.exists()

    if not apply:
        return report

    if to_retire:
        retired_dir = store.root / RETIRED_DIR_NAME
        retired_dir.mkdir(parents=True, exist_ok=True)
        for name in to_retire:
            src = journal / name
            dest = retired_dir / name
            n = 1
            while dest.exists():
                # A previous partial run already moved one; keep EVERY copy —
                # rename() would silently replace an existing destination.
                dest = retired_dir / f"{src.stem}.again{n}{src.suffix}"
                n += 1
            src.rename(dest)
            log.info("migrate-to-topics: retired %s -> %s", src, dest)

    if report.topics_created:
        BoundedStore(cfg, store).save_atomic(STORE_TOPICS, [])

    return report

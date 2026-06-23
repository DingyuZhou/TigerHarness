"""In-session sub-agent write-back: extraction bundle → bounded stores.

The subscription-safe sweep runs extraction inside an isolated, in-persona
Task sub-agent (design §2): it reads one staged transcript prompt, emits the
``@@SKILLS@@ / @@MUST_REMEMBER@@ / @@DIARY@@`` bundle, and turns it into
stored entries through THIS entry point — typically via a ``tiger-memory``
CLI that wraps it, so the bulky bundle never transits the driver's context.

Parsing + validation stay in Python (robust); the sub-agent only produces the
text. A malformed bundle raises before any write, so the store is left intact.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .bounded_store import BoundedStore
from .config import Config
from .lifecycle import ingest_candidates, parse_extraction
from .state import iso_now
from .store import Store
from .summarizers.base import Summarizer

log = logging.getLogger("tigerharness.tiger_memory.executor")


@dataclass(frozen=True)
class IngestResult:
    conversation_uuid: str
    skills_added: int
    must_remember_added: int
    diary_added: int

    @property
    def total_added(self) -> int:
        return self.skills_added + self.must_remember_added + self.diary_added


def ingest_extraction(
    store: Store,
    cfg: Config,
    *,
    conversation_uuid: str,
    source: str,
    bundle_text: str,
    now: str | None = None,
    summarizer: Summarizer | None = None,
) -> IngestResult:
    """Parse an extraction bundle and merge its candidates into the stores.

    Raises :class:`~tigerharness.tiger_memory.lifecycle.ExtractionParseError`
    on a malformed bundle — parsing happens BEFORE any write, so the store is
    left untouched and the caller can re-ask the sub-agent.

    **Concurrency — serialize per persona.** The merge is a read-modify-write
    on each per-persona store file; calls for the SAME persona must be
    serialized (run a persona's transcripts serially, or defer to a single
    finalize step). Different personas are independent (separate stores).
    """
    log.info("ingest-extraction: merging bundle for %s", conversation_uuid)
    now = now or iso_now()
    candidates = parse_extraction(bundle_text, now=now, source=source)
    bstore = BoundedStore(cfg, store)
    added = ingest_candidates(bstore, cfg, candidates, now=now)
    if summarizer is not None and cfg.memory.diary.evocation_enabled:
        from .evocation import evoke_and_reinforce
        evoke_and_reinforce(bstore, cfg, candidates, summarizer, now=now)
    from .entries import STORE_DIARY, STORE_MUST_REMEMBER, STORE_SKILLS
    return IngestResult(
        conversation_uuid=conversation_uuid,
        skills_added=added[STORE_SKILLS],
        must_remember_added=added[STORE_MUST_REMEMBER],
        diary_added=added[STORE_DIARY],
    )

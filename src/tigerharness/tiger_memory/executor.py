"""In-session sub-agent write-back: extraction bundle → bounded stores.

The subscription-safe sweep runs extraction inside an isolated, in-persona
Task sub-agent (design §2): it reads one staged transcript prompt, emits the
``@@SKILLS@@ / @@MUST_REMEMBER@@ / @@TOPICS@@`` bundle, and turns it into
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
from .entries import STORE_MUST_REMEMBER, STORE_SKILLS, STORE_TOPICS
from .lifecycle import ingest_candidates, parse_extraction
from .state import iso_now
from .store import Store

log = logging.getLogger("tigerharness.tiger_memory.executor")


@dataclass(frozen=True)
class IngestResult:
    conversation_uuid: str
    skills_added: int
    must_remember_added: int
    topics_added: int
    #: Existing must-remember items whose freshness this bundle refreshed
    #: (``TOUCH:`` blocks) — not additions, so not part of ``total_added``.
    touched: int = 0
    #: Activity lines appended to the TEAM-wide event log (``EVENT:``
    #: blocks, ADR 0008) — a team-level file, not a persona store, so not
    #: part of ``total_added``.
    team_events_added: int = 0

    @property
    def total_added(self) -> int:
        return self.skills_added + self.must_remember_added + self.topics_added


def _source_stamp(source_ts: str | None, now: str) -> str:
    """The timestamp new entries should carry: the SOURCE's end time.

    Stamping ``now`` at ingest mis-dates every backfilled record — a bulk
    sweep marks weeks-old facts "touched today", which breaks freshest-first
    ranking, blanket-protects genuinely stale topics inside ``fresh_days``,
    and presents retired architecture as current knowledge (practicality
    audit, content findings 1/2/5). Falls back to *now* when the source
    timestamp is missing, unparseable, or in the future.
    """
    if not source_ts:
        return now
    from datetime import datetime

    try:
        src = datetime.fromisoformat(str(source_ts).replace("Z", "+00:00"))
        cur = datetime.fromisoformat(now.replace("Z", "+00:00"))
    except ValueError:
        return now
    return source_ts if src < cur else now


def ingest_extraction(
    store: Store,
    cfg: Config,
    *,
    conversation_uuid: str,
    source: str,
    bundle_text: str,
    now: str | None = None,
    source_last_event_at: str | None = None,
) -> IngestResult:
    """Parse an extraction bundle and merge its candidates into the stores.

    Raises :class:`~tigerharness.tiger_memory.lifecycle.ExtractionParseError`
    on a malformed bundle — parsing happens BEFORE any write, so the store is
    left untouched and the caller can re-ask the sub-agent.

    ``source_last_event_at`` is the session's END timestamp (the plan
    manifest's ``last_event_at``). It — not the ingest wall clock — dates
    everything the bundle produces: entry ``created_at``/``last_used``,
    topic detail section headings, and the team event log day. A backlog
    sweep therefore files history under when the work actually happened.

    **Concurrency — serialize per persona.** The merge is a read-modify-write
    on each per-persona store file; calls for the SAME persona must be
    serialized (run a persona's transcripts serially, or defer to a single
    finalize step). Different personas are independent (separate stores) —
    the team event log append does its own locking.
    """
    log.info("ingest-extraction: merging bundle for %s", conversation_uuid)
    now = now or iso_now()
    stamp = _source_stamp(source_last_event_at, now)
    candidates = parse_extraction(bundle_text, now=stamp, source=source)
    bstore = BoundedStore(cfg, store)
    added = ingest_candidates(bstore, cfg, candidates, now=stamp)
    events_added = 0
    if candidates.team_events:
        from .team_events import append_events
        events_added = append_events(
            cfg, persona=cfg.agent.name, day=stamp[:10],
            events=candidates.team_events, now=now,
        )
    return IngestResult(
        conversation_uuid=conversation_uuid,
        skills_added=added[STORE_SKILLS],
        must_remember_added=added[STORE_MUST_REMEMBER],
        topics_added=added[STORE_TOPICS],
        touched=added["touched"],
        team_events_added=events_added,
    )

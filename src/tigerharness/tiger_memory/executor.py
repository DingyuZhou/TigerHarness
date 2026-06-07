"""B1 stage-2 executor write-back (the in-session sub-agent path).

The subscription-safe rebuild moves summarization into an isolated
Task-tool sub-agent (B1/B8): it reads one flagged transcript, emits the
collapsed ``@@SHORT@@/@@DETAILED@@/@@MUST_MEMORIZE@@`` bundle, and turns it
into stored artifacts through THIS entry point — typically by invoking a
``tiger-memory`` CLI that wraps it, so the bulky bundle never transits the
driver's context.

This is the bundle -> store half of the collapsed pass, factored so the
in-session sub-agent and the legacy in-process collapsed path
(``lifecycle._write_session_collapsed``) write identical artifacts.
Parsing + validation stay in Python (robust); the sub-agent only produces
the text and self-validates before calling in.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import must_memorize as mm
from .collapse import parse_collapsed
from .config import Config
from .lifecycle import _write_short_archive_bodies
from .sources.base import SourceRecord
from .store import Store

SUBAGENT_SUMMARIZER_TAG = "subagent@v1"


@dataclass(frozen=True)
class IngestResult:
    conversation_uuid: str
    must_memorize_added: int


def ingest_collapsed_summary(
    store: Store,
    cfg: Config,
    *,
    conversation_uuid: str,
    source: str,
    source_id: str,
    first_event_at: datetime,
    last_event_at: datetime,
    bundle_text: str,
    raw_path: Path,
    summarizer_tag: str = SUBAGENT_SUMMARIZER_TAG,
    today: str | None = None,
) -> IngestResult:
    """Parse a collapsed summary bundle and write the short + detailed
    archive, then merge its must-memorize candidates into the store.

    Raises ``CollapseParseError`` (from ``parse_collapsed``) on a malformed
    bundle — parsing happens *before* any write, so the store is left
    untouched and the caller can fall back / re-ask the sub-agent.
    """
    short_body, detailed_body, mm_section = parse_collapsed(bundle_text)

    rec = SourceRecord(
        conversation_uuid=conversation_uuid,
        source=source,
        source_id=source_id,
        first_event_at=first_event_at,
        last_event_at=last_event_at,
        activity_mtime=0.0,
        content="",
        raw_path=raw_path,
    )
    _write_short_archive_bodies(
        store, cfg, summarizer_tag, rec, short_body, detailed_body
    )

    candidates = mm.parse_extractor_output(mm_section)
    if today is None:
        today = datetime.now(timezone.utc).date().isoformat()
    rows = mm.load(store)
    rows, demoted = mm.merge_candidates(
        rows,
        candidates,
        today=today,
        similarity_threshold=cfg.budgets.repeat_detection_similarity,
        max_rows=cfg.budgets.must_memorize_rows,
    )
    if demoted:
        mm.append_dropped(store, demoted)
    mm.save(store, rows)
    return IngestResult(
        conversation_uuid=conversation_uuid,
        must_memorize_added=len(candidates),
    )

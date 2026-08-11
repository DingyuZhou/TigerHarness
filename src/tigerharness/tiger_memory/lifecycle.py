"""Session → memory extraction (topic-store revamp, ADR 0007; design §2, §4).

This module turns a *finished* session — discovered via the unchanged
``sources/`` adapters — into candidate entries for the three bounded stores
(``skills`` / ``must_remember`` / ``topics``), in-persona, then ingests
them through :class:`BoundedStore`. It replaces the old
rollup / archive / ``longer_memory`` chronological lifecycle entirely
(design §3 — fully retired, no safety net).

The only model touch point is the extraction *judgement*, which goes through
the pluggable summarizer registry (mock in CI, plan §5b). Discovery, the
idle/clean decision, parsing, merging, and the fresh-start ``rebuild`` are
pure Python.

Two write paths share the same parse + ingest core:

- **in-process** (``extract_and_ingest``): a Python summarizer runs the
  extraction call directly — used by tests under the mock and by any caller
  that already has a :class:`Summarizer`.
- **in-session sub-agent** (``plan_extraction`` → ``executor.ingest_extraction``):
  ``plan_extraction`` stages one prompt per flagged transcript under
  ``.sweep-staging/``; an in-persona Task sub-agent reads the staged prompt,
  emits the bundle, and writes it back via the CLI (so the bulky transcript
  never transits the driver's context). This is the subscription-rail path
  (design §2 — never an inline ``claude -p``).
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable

from .bounded_store import BoundedStore, StoreLockHeld
from .config import Config
from .cursor import load_cursor
from .entries import (
    KIND_OPERATOR_EXPLICIT,
    STORE_MUST_REMEMBER,
    STORE_SKILLS,
    STORE_TOPICS,
    VALID_KINDS,
    BaseEntry,
    EntryError,
    MustRememberEntry,
    SkillEntry,
    TopicEntry,
    topic_slug,
)
from .indexes import (
    render_must_remember_touch_list,
    render_topic_routing_list,
)
from .prefilter import filter_transcript
from .skills import refresh_importance
from .sources import (
    ClaudeTranscriptAdapter,
    DocsAdapter,
    JournalWorklogAdapter,
    SourceAdapter,
    SourceRecord,
)
from .state import iso_now
from .store import Store
from .summarizers import (
    MockSummarizer,
    Summarizer,
    get_summarizer,
)


log = logging.getLogger("tigerharness.tiger_memory.lifecycle")


# ----- session-decision constants ------------------------------------------

SKIP_ACTIVE = "skip_active"        # still-active session — not idle yet
EXTRACT = "extract"                # idle session not yet processed


@dataclass
class Decision:
    record: SourceRecord
    action: str


# ----- extraction candidate parsing -----------------------------------------

# The extraction prompt's strict output contract (see
# ``summarizers/prompts/default/v1/extract_memory.md``): four whole-line
# section markers, in this order (contract v3 — @@TEAM_EVENTS@@ added by
# ADR 0008).
MARK_SKILLS = "@@SKILLS@@"
MARK_MUST_REMEMBER = "@@MUST_REMEMBER@@"
MARK_TOPICS = "@@TOPICS@@"
MARK_TEAM_EVENTS = "@@TEAM_EVENTS@@"
_MARKERS = (MARK_SKILLS, MARK_MUST_REMEMBER, MARK_TOPICS, MARK_TEAM_EVENTS)

# Section key for the parsed team-events list (not a bounded store name —
# the team event log is a team-level file, ADR 0008).
SECTION_TEAM_EVENTS = "team_events"


class ExtractionParseError(ValueError):
    """The extraction output didn't satisfy the marker contract."""


@dataclass
class TopicCandidate:
    """One parsed ``@@TOPICS@@`` block — routing info, not yet an entry.

    ``slug`` is empty for a NEW topic (``name`` then carries the human name
    to mint a slug from); otherwise it addresses an existing topic.
    ``summary`` is empty when the block left the existing summary alone.
    """

    slug: str
    name: str
    summary: str
    detail: str


@dataclass
class Candidates:
    """Parsed extraction candidates for the three stores (typed, unscored).

    ``touches`` carries the ids of existing must-remember items the bundle
    marked as related to this session (``TOUCH:`` blocks) — ingest refreshes
    their ``last_used`` freshness anchor rather than adding anything.
    ``team_events`` carries the session's concise activity lines
    (``EVENT:`` blocks, ADR 0008) — appended to the team-wide event log,
    not to any per-persona store, so they don't count in :meth:`total`.
    """

    skills: list[SkillEntry]
    must_remember: list[MustRememberEntry]
    topics: list[TopicCandidate]
    touches: list[str] = field(default_factory=list)
    team_events: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (
            self.skills or self.must_remember or self.topics or self.touches
            or self.team_events
        )

    def total(self) -> int:
        return len(self.skills) + len(self.must_remember) + len(self.topics)


def _split_sections(text: str) -> dict[str, str]:
    """Split a bundle on its whole-line section markers (inverse of the prompt).

    Markers are matched as whole lines (``line.strip() == marker``) taking the
    first standalone occurrence of each — so a marker token echoed inline in a
    section body (e.g. quoted from an untrusted transcript) does not mis-split
    the bundle. Raises :class:`ExtractionParseError` if any marker is missing
    or the markers are out of order.
    """
    if not text or not text.strip():
        raise ExtractionParseError("empty extraction output")
    lines = text.split("\n")
    pos: dict[str, int] = {}
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped in _MARKERS and stripped not in pos:
            pos[stripped] = i
    if any(m not in pos for m in _MARKERS):
        raise ExtractionParseError("missing one or more section markers")
    counts = {m: 0 for m in _MARKERS}
    for line in lines:
        stripped = line.strip()
        if stripped in _MARKERS:
            counts[stripped] += 1
    if any(c > 1 for c in counts.values()):
        # A standalone duplicate (e.g. the card echoed the prompt's contract
        # sample before its real output) makes the split ambiguous — real
        # content could land in a bogus section and be dropped block-by-block
        # while the cursor advances. Malformed is the safe verdict: the card
        # stays put and is re-asked.
        raise ExtractionParseError(
            "duplicate standalone section marker (ambiguous bundle)"
        )
    i_s, i_m, i_t, i_e = (
        pos[MARK_SKILLS], pos[MARK_MUST_REMEMBER], pos[MARK_TOPICS],
        pos[MARK_TEAM_EVENTS],
    )
    if not (i_s < i_m < i_t < i_e):
        raise ExtractionParseError("section markers out of order")
    return {
        STORE_SKILLS: "\n".join(lines[i_s + 1:i_m]).strip(),
        STORE_MUST_REMEMBER: "\n".join(lines[i_m + 1:i_t]).strip(),
        STORE_TOPICS: "\n".join(lines[i_t + 1:i_e]).strip(),
        SECTION_TEAM_EVENTS: "\n".join(lines[i_e + 1:]).strip(),
    }


def clean_ref(raw: str | None) -> str:
    """Normalize an id/slug reference echoed from a prompt listing.

    Prompt listings display addresses backticked (`` `slug` ``); a card that
    copies the displayed form must still resolve, so surrounding whitespace
    and backticks are stripped.
    """
    return (raw or "").strip().strip("`").strip()


def _section_blocks(section: str) -> list[dict[str, str]]:
    """Parse one store section into a list of ``FIELD: value`` block dicts.

    ``NONE`` (case-insensitive, possibly with trailing prose) ⇒ zero blocks.
    Blocks are separated by blank lines; within a block, each ``KEY: value``
    line contributes a field (keys upper-cased). A value may continue onto
    following unkeyed lines (appended with a space). Lines before the first
    key in a block are ignored. Tolerant of extra whitespace.
    """
    if not section or section.strip().upper().startswith("NONE"):
        return []
    blocks: list[dict[str, str]] = []
    current: dict[str, str] = {}
    last_key: str | None = None
    for raw in section.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            if current:
                blocks.append(current)
                current, last_key = {}, None
            continue
        key, sep, value = line.partition(":")
        key_norm = key.strip().upper()
        if sep and key_norm.isalpha() and " " not in key.strip():
            current[key_norm] = value.strip()
            last_key = key_norm
        elif last_key is not None:
            current[last_key] = (current[last_key] + " " + line.strip()).strip()
    if current:
        blocks.append(current)
    return blocks


def parse_extraction(text: str, *, now: str, source: str) -> Candidates:
    """Parse an extraction bundle into typed (unscored) store candidates.

    The body markers are validated (:class:`ExtractionParseError` on a
    malformed bundle, raised BEFORE any candidate is built); within a section,
    individual malformed blocks are skipped (a missing required field drops
    just that block, never the whole bundle). ``now`` seeds ``created_at`` /
    ``last_used``; ``source`` is the provenance tag.
    """
    sections = _split_sections(text)
    skills: list[SkillEntry] = []
    for b in _section_blocks(sections[STORE_SKILLS]):
        name, trigger, proc = b.get("NAME"), b.get("TRIGGER"), b.get("PROCEDURE")
        if not (name and trigger and proc):
            continue
        try:
            # Same sluggability gate the NEW-topic branch has: a NAME with no
            # ASCII alphanumerics would persist fine but poison every later
            # index/detail render (detail filenames slug the name). Drop the
            # block, never the bundle.
            topic_slug(name)
        except EntryError:
            continue
        skills.append(
            SkillEntry(
                text=proc, created_at=now, last_used=now, source=source,
                name=name, trigger=trigger, procedure=proc,
                usage_count=0, importance=0.0,
            )
        )
    must: list[MustRememberEntry] = []
    touches: list[str] = []
    for b in _section_blocks(sections[STORE_MUST_REMEMBER]):
        if "TOUCH" in b:
            # The prompt displays ids backticked (`abc123`); tolerate a card
            # that echoes the displayed form — a silently-missed touch would
            # let a live item drift to forget-eligible.
            touched_id = clean_ref(b["TOUCH"])
            if touched_id:
                touches.append(touched_id)
            if not (b.get("KIND") or b.get("MEMO")):
                continue
            # A sloppy card merged a memo and a touch into one block (no
            # blank line) — keep BOTH rather than silently dropping the memo.
        kind = (b.get("KIND") or "").lower()
        memo = b.get("MEMO")
        if kind not in VALID_KINDS or not memo:
            continue
        must.append(
            MustRememberEntry(
                text=memo, created_at=now, last_used=now, source=source,
                kind=kind,
            )
        )
    topics: list[TopicCandidate] = []
    for b in _section_blocks(sections[STORE_TOPICS]):
        target = (b.get("TOPIC") or "").strip()
        name = (b.get("NAME") or "").strip()
        summary = (b.get("SUMMARY") or "").strip()
        detail = (b.get("DETAIL") or "").strip()
        if not target or not detail:
            continue
        if target.upper() == "NEW":
            # A NEW topic must be nameable, sluggable, and index-worthy; a
            # block missing any of that is dropped (never the whole bundle).
            # The slug probe here keeps an unsluggable NAME (e.g. all
            # symbols) from blowing up mid-ingest after other stores saved.
            if not name or not summary:
                continue
            try:
                topic_slug(name)
            except EntryError:
                continue
            topics.append(
                TopicCandidate(slug="", name=name, summary=summary, detail=detail)
            )
        else:
            try:
                slug = topic_slug(target)
            except EntryError:
                continue
            topics.append(
                TopicCandidate(slug=slug, name=name, summary=summary, detail=detail)
            )
    # EVENT items are single-field, so they are parsed line-wise, not
    # block-wise — consecutive ``EVENT:`` lines with no blank line between
    # them must not collapse into one block (last-wins silent event loss).
    # An unkeyed line continues the previous event.
    team_events: list[str] = []
    events_section = sections[SECTION_TEAM_EVENTS]
    if events_section and not events_section.strip().upper().startswith("NONE"):
        for raw in events_section.split("\n"):
            line = raw.strip()
            if not line:
                continue
            key, sep, value = line.partition(":")
            if sep and key.strip().upper() == "EVENT":
                if value.strip():
                    team_events.append(value.strip())
            elif team_events:
                team_events[-1] = f"{team_events[-1]} {line}"
    return Candidates(
        skills=skills, must_remember=must, topics=topics, touches=touches,
        team_events=team_events,
    )


# ----- extraction (the model touch point) -----------------------------------


def extract_candidates(
    cfg: Config,
    summarizer: Summarizer,
    rec: SourceRecord,
    *,
    now: str | None = None,
    topic_index: str = "",
    must_remember_index: str = "",
) -> Candidates:
    """Run the extraction prompt over *rec* and parse the typed candidates.

    The single LLM call (mock in CI). A backend error or a malformed bundle is
    logged-and-swallowed into empty candidates so one bad session never aborts
    a sweep. The transcript is pre-filtered (if enabled) and clipped to the
    staged ceiling before the call. *topic_index* is the routing list of the
    persona's existing topics (``render_topic_routing_list``) embedded so the
    summarizer files facts into existing topics instead of minting duplicates.
    """
    now = now or iso_now()
    content = rec.content
    if cfg.prefilter.enabled:
        content = filter_transcript(
            content,
            drop_tool_results=cfg.prefilter.drop_tool_results,
            drop_system_reminders=cfg.prefilter.drop_system_reminders,
        )
    content = _clip(content, cfg.budgets.max_prompt_content_chars)
    prompt = _fill_extract_prompt(
        cfg, _prompts_root(cfg), rec, content, topic_index=topic_index,
        must_remember_index=must_remember_index,
    )
    try:
        raw = summarizer.summarize(
            prompt=prompt, max_words=cfg.memory_extract.max_output_words
        )
        return parse_extraction(raw, now=now, source=rec.source)
    except ExtractionParseError as exc:
        log.warning("extraction parse failed for %s: %s", rec.conversation_uuid, exc)
    except Exception:  # noqa: BLE001 — one bad session must not abort the sweep
        log.exception("extraction call failed for %s", rec.conversation_uuid)
    return Candidates(skills=[], must_remember=[], topics=[])


# ----- ingest (candidates → bounded stores) ---------------------------------


def _latest_ts(a: str, b: str) -> str:
    """The later of two ISO timestamps (parse-based; unparseable loses).

    Source-dated ingest means *now* can be an OLD timestamp (a backfilled
    session). A touch or topic bump from old work must never move an
    already-fresher entry's ``last_used`` backward.
    """
    da, db = _parse_iso(a), _parse_iso(b)
    if da is None:
        return b
    if db is None:
        return a
    return a if da >= db else b


def _append_topic_detail(text: str, date: str, detail: str) -> str:
    """Append one detail bullet to a topic body under its ``## <date>`` section.

    The body is a sequence of dated sections (``## YYYY-MM-DD`` headings, each
    followed by ``- `` bullets, newest section last). If the last section is
    already *date*'s, the bullet joins it; otherwise a new section is opened.
    """
    bullet = f"- {detail}"
    body = text.rstrip()
    if not body:
        return f"## {date}\n{bullet}"
    headers = re.findall(r"^## (\d{4}-\d{2}-\d{2})\s*$", body, flags=re.MULTILINE)
    if headers and headers[-1] == date:
        return f"{body}\n{bullet}"
    return f"{body}\n\n## {date}\n{bullet}"


def _route_topic_candidates(
    existing: list[BaseEntry],
    topics: list[TopicCandidate],
    *,
    now: str,
    source: str,
) -> tuple[list[BaseEntry], int]:
    """Fold topic *candidates* into *existing* topic entries (ADR 0007 routing).

    A candidate addressing a known slug appends its detail (dated), bumps
    freshness (``last_used``) and ``touch_count``, and refreshes the summary
    when one was provided. An unknown/NEW candidate mints a new topic — unless
    its minted slug collides with an existing topic, in which case it merges
    as a touch (no duplicate topics by construction). Returns the merged list
    and how many candidates landed.
    """
    by_slug: dict[str, TopicEntry] = {
        e.slug: e for e in existing if isinstance(e, TopicEntry)
    }
    landed = 0
    day = now[:10]
    for cand in topics:
        slug = cand.slug or topic_slug(cand.name)
        entry = by_slug.get(slug)
        if entry is None:
            if not cand.slug:
                # Genuinely new topic.
                entry = TopicEntry(
                    text=f"## {day}\n- {cand.detail}",
                    created_at=now, last_used=now, source=source,
                    name=cand.name, slug=slug, summary=cand.summary,
                    touch_count=1,
                )
                existing.append(entry)
                by_slug[slug] = entry
                landed += 1
                continue
            # An "existing" slug the store no longer has (e.g. forgotten
            # between plan and ingest). Recover rather than drop: revive it
            # as a new topic named from the slug (or the block's NAME).
            name = cand.name or slug.replace("-", " ")
            summary = cand.summary or cand.detail
            entry = TopicEntry(
                text=f"## {day}\n- {cand.detail}",
                created_at=now, last_used=now, source=source,
                name=name, slug=slug, summary=summary,
                touch_count=1,
            )
            existing.append(entry)
            by_slug[slug] = entry
            landed += 1
            continue
        entry.text = _append_topic_detail(entry.text, day, cand.detail)
        stale_candidate = (
            _latest_ts(entry.last_used, now) == entry.last_used
            and entry.last_used != now
        )
        if cand.summary and (not stale_candidate or not entry.summary):
            # A backfilled OLD session must not clobber a newer summary;
            # a same-or-newer session refreshes it as before.
            entry.summary = cand.summary
        entry.last_used = _latest_ts(entry.last_used, now)
        entry.touch_count += 1
        landed += 1
    return existing, landed


def ingest_candidates(
    bstore: BoundedStore,
    cfg: Config,
    candidates: Candidates,
    *,
    now: str | None = None,
) -> dict[str, int]:
    """Merge *candidates* into the three bounded stores (per-store, atomic).

    Returns a per-store count of entries/details added. Skills and
    must_remember append (skills get their ``importance`` refreshed from
    ``usage_count`` + ``last_used``); topic candidates ROUTE — into an
    existing topic when the slug matches, else as a new topic. Compaction is
    NOT run here — the sweep stages it post-ingest only when a surface is
    over its overflow limit (the hysteresis trigger).
    """
    now = now or iso_now()
    added = {STORE_SKILLS: 0, STORE_MUST_REMEMBER: 0, STORE_TOPICS: 0,
             "touched": 0}
    if candidates.is_empty():
        return added
    # Every ingest write below is a read-modify-write held under the
    # per-store lock (waiting variant): lockless, a compact-apply or a
    # concurrent pin interleaving with the RMW silently loses entries —
    # and the cursor advance downstream makes that loss permanent
    # (audit F2). A StoreLockHeld after the wait propagates: the card
    # stays staged, the cursor stays put, the slice re-ingests next sweep.
    per_store: dict[str, list[BaseEntry]] = {
        STORE_SKILLS: list(candidates.skills),
        STORE_MUST_REMEMBER: list(candidates.must_remember),
    }
    for store_name, new_entries in per_store.items():
        if not new_entries:
            continue
        with bstore.store_lock_wait(store_name):
            existing = bstore.load(store_name)
            for entry in new_entries:
                if isinstance(entry, SkillEntry):
                    refresh_importance(entry, now, cfg)
            merged = existing + new_entries
            bstore.save_atomic(store_name, merged)
        added[store_name] = len(new_entries)
    if candidates.topics:
        with bstore.store_lock_wait(STORE_TOPICS):
            existing = bstore.load(STORE_TOPICS)
            merged, landed = _route_topic_candidates(
                existing, candidates.topics, now=now, source="extract"
            )
            bstore.save_atomic(STORE_TOPICS, merged)
        added[STORE_TOPICS] = landed
    if candidates.touches:
        # Freshness touches: the bundle marked these existing must-remember
        # items as related to this session — refresh their forget-eligibility
        # anchor (and repeat signal). Unknown ids are ignored (the item may
        # have been compacted away between plan and ingest).
        with bstore.store_lock_wait(STORE_MUST_REMEMBER):
            entries = bstore.load(STORE_MUST_REMEMBER)
            by_id = {e.id: e for e in entries}
            touched = 0
            for tid in dict.fromkeys(candidates.touches):
                entry = by_id.get(tid)
                if entry is None:
                    continue
                # Never move freshness backward: a backfilled old session's
                # touch must not un-refresh an item a newer session touched.
                entry.last_used = _latest_ts(entry.last_used, now)
                if isinstance(entry, MustRememberEntry):  # pragma: no branch
                    entry.repeat_count += 1
                touched += 1
            if touched:
                bstore.save_atomic(STORE_MUST_REMEMBER, entries)
        added["touched"] = touched
    log.info(
        "ingest: +%d skills, +%d must_remember (%d touched), +%d topic detail(s)",
        added[STORE_SKILLS], added[STORE_MUST_REMEMBER], added["touched"],
        added[STORE_TOPICS],
    )
    return added


def extract_and_ingest(
    cfg: Config,
    store: Store,
    summarizer: Summarizer,
    rec: SourceRecord,
    *,
    now: str | None = None,
) -> dict[str, int]:
    """In-process extract → ingest for one finished session (mock-backed in CI).

    The current topic routing list is read from the store and embedded in the
    extraction prompt so facts land in existing topics (ADR 0007).
    """
    now = now or iso_now()
    from .executor import _source_stamp
    stamp = _source_stamp(rec.last_event_at.isoformat(), now)
    bstore = BoundedStore(cfg, store)
    candidates = extract_candidates(
        cfg, summarizer, rec, now=stamp,
        topic_index=_topic_routing_index(cfg, store),
        must_remember_index=_mr_touch_index(cfg, store),
    )
    added = ingest_candidates(bstore, cfg, candidates, now=stamp)
    if candidates.team_events:
        from .team_events import append_events
        added["team_events"] = append_events(
            cfg, persona=cfg.agent.name,
            day=stamp[:10],
            events=candidates.team_events, now=now,
        )
    return added


# ----- discovery + idle decision --------------------------------------------


def _build_adapters(cfg: Config, *, max_age_days: int | None = 7) -> list[SourceAdapter]:
    """Build source adapters from config (the unchanged input feed, design §3).

    ``max_age_days`` is forwarded to ``ClaudeTranscriptAdapter`` as the
    discovery cutoff (skip JSONLs older than N days by mtime). ``docs`` sources
    are dropped — the rollup/bootstrap docs path is retired.
    """
    adapters: list[SourceAdapter] = []
    repo_root = cfg.source_path.parent if cfg.source_path else Path.cwd()
    threads_json: Path | None = None
    drive_sessions_json: Path | None = None
    for s in cfg.sources:
        if s.kind == "slack_thread":
            raw = s.fields.get("threads_json", "")
            threads_json = Path(raw).expanduser() if raw else None
        elif s.kind == "journal_worklog":
            drive_sessions_json = (
                _resolve_journal_root(s.fields["journal_root"], repo_root)
                / ".drive-sessions.json"
            )
    for s in cfg.sources:
        if s.kind == "claude_code":
            persona = s.fields.get("persona")
            persona = persona.strip() if isinstance(persona, str) and persona.strip() else None
            team = s.fields.get("team")
            team = team.strip() if isinstance(team, str) and team.strip() else None
            include_unattributed = bool(s.fields.get("include_unattributed", False))
            adapters.append(
                ClaudeTranscriptAdapter(
                    project_path=_resolve_project_path(
                        s.fields["project_path"], repo_root, cfg
                    ),
                    threads_json=threads_json,
                    persona=persona,
                    team=team,
                    include_unattributed=include_unattributed,
                    max_age_days=max_age_days,
                    drive_sessions_json=drive_sessions_json,
                )
            )
        elif s.kind == "journal_worklog":
            journal_root = _resolve_journal_root(s.fields["journal_root"], repo_root)
            team = s.fields.get("team")
            team = (
                team.strip()
                if isinstance(team, str) and team.strip()
                else journal_root.parent.name
            )
            adapters.append(
                JournalWorklogAdapter(
                    journal_root=journal_root,
                    persona=s.fields["persona"],
                    team=team,
                )
            )
        # slack_thread / docs / auto_memory carry no live adapter on this path.
    return adapters


def _resolve_project_path(field_path: object, repo_root: Path, cfg: Config) -> Path:
    """Resolve a ``claude_code`` source's ``project_path`` field.

    ``auto`` derives the Claude Code transcripts dir from the team root
    (standard layout: the config lives at
    ``<team>/memories/<persona>/tiger-memory.config.yaml``, so the team
    root is two levels above the config's directory; a config loaded
    without a file path falls back to the cwd, which persona sessions
    pin to the team root). This keeps the per-persona configs free of
    machine-specific absolute paths. An explicit path is used as-is,
    with ``~`` expanded.
    """
    raw = str(field_path).strip()
    if raw == "auto":
        from tigerharness.init import expected_claude_project_path

        team_root = repo_root.parent.parent if cfg.source_path else Path.cwd()
        return expected_claude_project_path(team_root)
    return Path(raw).expanduser()


def _resolve_journal_root(field_root: str, repo_root: Path) -> Path:
    """Resolve a ``journal_worklog`` source's ``journal_root`` field.

    A relative root is anchored to the config's directory (``repo_root``) so a
    per-persona config can say ``../../journal/`` and a headless sweep still
    finds it regardless of the working directory.
    """
    jr = Path(field_root).expanduser()
    if not jr.is_absolute():
        jr = (repo_root / jr).resolve()
    return jr


def _build_summarizer(cfg: Config, *, mock: bool = False) -> Summarizer:
    if mock:
        return MockSummarizer()
    return get_summarizer(cfg.summarizer.backend, cfg.summarizer)


def _discover(cfg: Config, *, max_age_days: int | None = 7) -> list[SourceRecord]:
    records: list[SourceRecord] = []
    for adapter in _build_adapters(cfg, max_age_days=max_age_days):
        records.extend(adapter.discover())
    return records


def has_pending_source(
    cfg: Config,
    store: Store,
    *,
    exclude_session: str | None = None,
    now: float | None = None,
    max_age_days: int | None = 7,
) -> bool:
    """Does this persona have un-swept source content a sweep would stage?

    The split-gate pending check (no LLM, no staging side-effects):
    True iff some discovered source record — the live session excluded —
    would stage a slice today. Two paths, mirroring staging exactly:

    - **Idle** (``activity_mtime`` older than the config's
      ``rebuild.idle_threshold_hours``, the same "completed" test the
      extraction pipeline applies) AND carrying content past the
      persona's ingest-dedupe cursor (no cursor recorded, or
      ``last_event_at`` newer than the cursor's high-water mark); OR
    - **Still active but over the active-slice threshold** — the exact
      staging predicate (``_compute_incremental_slice`` with
      ``active=True``, ADR 0006 Part 2): its post-cursor completed
      turns (or an oversized live tail) exceed
      ``budgets.active_slice_threshold_chars``. Keeping the gate in
      lockstep with staging means it never claims a sweep that would
      stage nothing, and a persona's own long still-warm session can
      open the floor bypass instead of waiting out the quiet window.

    *exclude_session* is the calling session's ``conversation_uuid`` — the
    hard live-session guarantee on top of the heuristics. Without it the
    check excludes nothing by uuid; a fresh-mtime transcript still stays
    out until its completed turns cross the threshold, and even then the
    active slice holds the live tail turn back (whole-turn boundary).

    A record that grew in place without advancing its timestamp is missed
    here (the full pipeline's count guard catches it at the next team
    sweep) — a false negative only ever delays one persona's sweep to the
    floor window, never ingests early.
    """
    now = time.time() if now is None else now
    idle_sec = cfg.rebuild.idle_threshold_hours * 3600
    for rec in _discover(cfg, max_age_days=max_age_days):
        if exclude_session and rec.conversation_uuid == exclude_session:
            continue
        if (now - rec.activity_mtime) < idle_sec:
            # Still active — pending only when staging's active gate would
            # already extract a slice (post-cursor completed turns over
            # ``active_slice_threshold_chars``; the live tail held back).
            if _compute_incremental_slice(cfg, store, rec, active=True) is not None:
                return True
            continue  # still active and under threshold — not pending
        cursor = load_cursor(store, rec.conversation_uuid)
        if cursor is None:
            return True
        cut = _parse_iso(cursor.last_event_at)
        if cut is None or rec.last_event_at > cut:
            return True
    return False


def _decide(
    records: Iterable[SourceRecord], cfg: Config, *, now: float
) -> list[Decision]:
    """Classify each record: still-active (skip) vs idle (extract).

    The summary trigger is unchanged (design §3): a session is processed only
    once it has gone idle for ``idle_threshold_hours``. There is no
    archive/clean/resummarize ladder any more — extraction is idempotent at the
    sweep level (a re-extracted session re-merges near-duplicate entries, which
    meditation later folds), so this only gates the still-active case.
    """
    out: list[Decision] = []
    idle_threshold_sec = cfg.rebuild.idle_threshold_hours * 3600
    for rec in records:
        if (now - rec.activity_mtime) < idle_threshold_sec:
            out.append(Decision(rec, SKIP_ACTIVE))
        else:
            out.append(Decision(rec, EXTRACT))
    return out


# ----- in-session sub-agent staging (subscription rail) ----------------------


def _sweep_staging_dir(store: Store) -> Path:
    return store.root / ".sweep-staging"


EXTRACTION_CARD_SUFFIX = ".extract.md"


def _sweep_card_path(store: Store, conversation_uuid: str) -> Path:
    """Where a sub-agent drops its extraction bundle for one conversation."""
    return _sweep_staging_dir(store) / f"{conversation_uuid}{EXTRACTION_CARD_SUFFIX}"


def _pack_stacks(
    weighted: list[tuple[str, int]], *, char_budget: int, max_items: int
) -> list[list[str]]:
    """Greedily group ``(uuid, weight)`` pairs — in plan order — into stacks,
    one per extraction sub-agent.

    A stack closes when adding the next transcript would push the summed weight
    over *char_budget*, or once it already holds *max_items*. A transcript
    heavier than the budget lands as a solo stack (never split). Deterministic,
    so a re-plan reproduces the same grouping.
    """
    stacks: list[list[str]] = []
    current: list[str] = []
    running = 0
    for uuid, weight in weighted:
        if current and (running + weight > char_budget or len(current) >= max_items):
            stacks.append(current)
            current, running = [], 0
        current.append(uuid)
        running += weight
    if current:
        stacks.append(current)
    return stacks


def _fill_extract_prompt(
    cfg: Config, prompts_root: Path, rec, content: str, *,
    topic_index: str, must_remember_index: str = "",
) -> str:
    """Fill the 3-store extraction prompt for one transcript (or reduced
    digest concatenation). The ``@@SKILLS@@`` / ``@@MUST_REMEMBER@@`` /
    ``@@TOPICS@@`` contract lives ONLY here, so it stays single-sourced
    whether the input is a whole small transcript or the reduced digests of a
    big one. *topic_index* is the persona's current topic routing list, so
    the summarizer files facts into existing topics (ADR 0007).
    """
    return _fill_prompt(
        prompts_root / "extract_memory.md",
        agent_name=cfg.agent.name,
        source=rec.source,
        source_id=rec.source_id,
        first_event_at=rec.first_event_at.isoformat(),
        last_event_at=rec.last_event_at.isoformat(),
        procedure_max_words=cfg.memory_extract.skill_procedure_words,
        memo_max_words=cfg.memory_extract.memo_words,
        topic_summary_max_words=cfg.memory_extract.topic_summary_words,
        topic_detail_max_words=cfg.memory_extract.topic_detail_words,
        team_event_max_words=cfg.memory_extract.team_event_words,
        topic_index=topic_index,
        must_remember_index=(
            must_remember_index or "(no must-remember items yet)"
        ),
        content=content,
    )


def _topic_routing_index(cfg: Config, store: Store) -> str:
    """The persona's current topic routing list, straight off the store."""
    bstore = BoundedStore(cfg, store)
    entries = [
        e for e in bstore.load(STORE_TOPICS) if isinstance(e, TopicEntry)
    ]
    return render_topic_routing_list(entries)


def _mr_touch_index(cfg: Config, store: Store) -> str:
    """The persona's current must-remember touch list, off the store."""
    bstore = BoundedStore(cfg, store)
    entries = [
        e for e in bstore.load(STORE_MUST_REMEMBER)
        if isinstance(e, MustRememberEntry)
    ]
    return render_must_remember_touch_list(entries)


def _per_chunk_words(cfg: Config, n_chunks: int) -> int:
    """Per-chunk digest word budget for the map step.

    Aim the *concatenated* digests well under the staging ceiling (target half)
    so the reduce is a single pass in the common case, with a 120-word floor so
    a tiny chunk still yields a usable digest. ~6 chars/word.
    """
    target_total = cfg.budgets.max_staged_content_chars // 2
    per_chunk_chars = max(1, target_total // max(1, n_chunks))
    return max(120, per_chunk_chars // 6)


# ----- incremental slicing (ADR 0006 Part 2 — per-session high-water mark) ---


def _prefilter(cfg: Config, content: str) -> str:
    """Apply the configured transcript pre-filter (or pass through when off)."""
    if cfg.prefilter.enabled:
        return filter_transcript(
            content,
            drop_tool_results=cfg.prefilter.drop_tool_results,
            drop_system_reminders=cfg.prefilter.drop_system_reminders,
        )
    return content


# A rendered turn header — ``[<iso-ts>] user:`` / ``[<iso-ts>] assistant:`` —
# the same structural boundary the prefilter recognises. The timestamp group
# may be empty (an event with no timestamp), in which case the turn carries no
# slice boundary and is always treated as post-cursor (re-process, never skip).
_TURN_HEADER_RE = re.compile(r"^\[([^\]]*)\]\s+(?:user|assistant):\s*$")

# Read-only continuity context delimiters (acceptance #2). The overlap window
# is prior, already-extracted turns prepended ONLY for continuity; the prompt
# marks them so the sub-agent extracts solely from the post-cursor slice.
_OVERLAP_OPEN = (
    "[read-only context — earlier turns already summarized; DO NOT re-extract, "
    "shown only for continuity]\n"
)
_OVERLAP_CLOSE = (
    "\n[end read-only context — extract ONLY from the turns that follow]\n\n"
)


@dataclass
class _Turn:
    """One transcript turn: its event timestamp (``None`` if unparseable) and
    the full original block (header line + body), so re-joining turns is
    lossless."""

    event_at: datetime | None
    text: str


@dataclass
class _SliceResult:
    """The post-cursor slice staged for one record: the prompt content (a
    read-only overlap window + the extraction-target slice, pre-filtered) plus
    the new high-water mark to advance the cursor to once the card ingests."""

    prompt_content: str
    cursor_event_at: str
    cursor_events: int


def _parse_iso(raw: str | None) -> datetime | None:
    """Parse an ISO turn timestamp to an aware datetime; ``None`` if empty or
    unparseable. A naive timestamp is assumed UTC so slice comparisons never
    mix naive/aware datetimes."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_turns(content: str) -> list[_Turn]:
    """Split a rendered transcript dump into turns on the ``[<ts>] <role>:``
    header lines. Each turn keeps its original text (header + body) so the
    concatenation of every turn's ``text`` reproduces *content* exactly."""
    turns: list[_Turn] = []
    cur_lines: list[str] = []
    cur_ts: datetime | None = None
    for line in content.splitlines(keepends=True):
        m = _TURN_HEADER_RE.match(line.rstrip("\n"))
        if m is not None:
            if cur_lines:
                turns.append(_Turn(cur_ts, "".join(cur_lines)))
            cur_lines = [line]
            cur_ts = _parse_iso(m.group(1))
        else:
            cur_lines.append(line)
    if cur_lines:
        turns.append(_Turn(cur_ts, "".join(cur_lines)))
    return turns


def _join_turns(turns: list[_Turn]) -> str:
    return "".join(t.text for t in turns)


def _active_slice_turns(cfg: Config, post_turns: list[_Turn]) -> list[_Turn] | None:
    """For a still-active session, choose the completed-turn slice to extract
    now, or ``None`` to keep waiting (the idle pass mops up the tail).

    Holds back the live tail turn and extracts the completed turns once they
    exceed ``active_slice_threshold_chars`` (measured on PREFILTERED content,
    cut at a whole-turn boundary — never mid-turn). The hard case: when the
    current (last) turn ALONE exceeds the threshold there is no completed
    boundary below it, so the whole post-cursor slice is extracted now (Part 1's
    chunker handles the oversized turn) rather than waiting and re-opening the
    leak.
    """
    threshold = cfg.budgets.active_slice_threshold_chars
    completed = post_turns[:-1]
    if completed and len(_prefilter(cfg, _join_turns(completed))) > threshold:
        return completed
    if len(_prefilter(cfg, _join_turns([post_turns[-1]]))) > threshold:
        return post_turns
    return None


def _boundary_marker(turns: list[_Turn], boundary: _Turn, rec) -> tuple[str, int]:
    """The new cursor (last_event_at, processed_events) after a slice ending at
    *boundary*.

    ``processed_events`` is fully POSITIONAL — ALL turns up to and
    including the boundary, timestamped or not. The old timestamped-only
    count could never mark an untimestamped turn processed: it always
    landed back in the post-cursor slice (or desynced the count guard
    when it was the boundary itself), re-staging and re-ingesting the
    same content every sweep forever (audit: pipeline finding 2). The
    positional prefix, checked bidirectionally by the guard in
    ``_compute_incremental_slice``, absorbs untimestamped turns while
    keeping the tied-timestamp hold-back property: a held-back turn
    sharing the boundary's timestamp sits in the post-slice at ≤ cut,
    trips the guard, and forces a safe full re-pass.

    The cursor timestamp is the last parseable timestamp at/before the
    boundary (the record's own end timestamp when there is none).
    """
    idx = turns.index(boundary)
    stamped = [t.event_at for t in turns[:idx + 1] if t.event_at is not None]
    ts = stamped[-1] if stamped else rec.last_event_at
    return ts.isoformat(), idx + 1


def _with_overlap(cfg: Config, overlap_turns: list[_Turn], target: str) -> str:
    """Prepend the bounded read-only overlap window to the extraction target."""
    if not overlap_turns:
        return target
    overlap = _prefilter(cfg, _join_turns(overlap_turns))
    return f"{_OVERLAP_OPEN}{overlap}{_OVERLAP_CLOSE}{target}"


def _compute_incremental_slice(
    cfg: Config, store: Store, rec, *, active: bool
) -> _SliceResult | None:
    """Compute the post-cursor slice to stage for *rec*, or ``None`` to skip.

    Loads the record's cursor, drops it (full pass) if the count guard trips,
    partitions turns into pre/post-cursor, applies the active-session threshold
    gate, then builds the staged prompt (read-only overlap window + the
    extraction target, pre-filtered) and the boundary to advance the cursor to.
    """
    turns = _parse_turns(rec.content)
    cursor = load_cursor(store, rec.conversation_uuid)
    cut = _parse_iso(cursor.last_event_at) if cursor is not None else None
    if cursor is not None and cut is not None:
        # POSITIONAL cursor model (audit: pipeline finding 2): the stored
        # count is a turn-prefix length; the guard verifies it is still
        # consistent with the timestamps on both sides — every timestamped
        # turn inside the prefix must be ≤ cut and every one after it must
        # be > cut. Any drift (prefilter change, tied-timestamp hold-back,
        # a legacy timestamped-only count) trips the guard toward a safe
        # full re-pass; untimestamped turns ride the prefix positionally
        # instead of re-ingesting forever.
        n = cursor.processed_events
        if not (0 <= n <= len(turns)):
            cut = None
        else:
            pre_c, post_c = turns[:n], turns[n:]
            consistent = all(
                t.event_at <= cut for t in pre_c if t.event_at is not None
            ) and all(
                t.event_at > cut for t in post_c if t.event_at is not None
            )
            if not consistent:
                cut = None
            elif not post_c and rec.last_event_at > cut:
                # The record grew IN PLACE past the cursor without adding
                # a turn (e.g. a worklog blob rendered as one unheadered
                # turn) — re-process in full.
                cut = None

    if cut is None:
        pre_turns: list[_Turn] = []
        post_turns = turns
    else:
        pre_turns = turns[:cursor.processed_events]
        post_turns = turns[cursor.processed_events:]

    if not post_turns:
        return None  # nothing new since the cursor

    if active:
        slice_turns = _active_slice_turns(cfg, post_turns)
        if slice_turns is None:
            return None  # SKIP_ACTIVE — under threshold; wait for the idle pass
    else:
        slice_turns = post_turns

    target = _prefilter(cfg, _join_turns(slice_turns))
    overlap = (
        pre_turns[-cfg.budgets.overlap_turns:]
        if cfg.budgets.overlap_turns > 0
        else []
    )
    prompt_content = _with_overlap(cfg, overlap, target)
    cursor_event_at, cursor_events = _boundary_marker(turns, slice_turns[-1], rec)
    return _SliceResult(prompt_content, cursor_event_at, cursor_events)


def _stage_record(
    cfg: Config, staging: Path, prompts_root: Path, rec, content: str,
    *, topic_index: str, must_remember_index: str,
) -> dict:
    """Stage one EXTRACT record and return its manifest item.

    Two shapes, distinguished by ``kind``:

    - ``kind="single"`` (content within the staging ceiling) — one
      ``<uuid>.prompt.md`` extraction prompt, exactly as before (back-compat).
    - ``kind="map_reduce"`` (oversized) — split losslessly on line boundaries
      into ``<uuid>.chunkNN.prompt.md`` condense prompts (the map step). The
      sub-agent writes a ``<uuid>.chunkNN.digest.md`` per chunk, then a reduce
      runs the extraction prompt over the concatenated digests to emit
      ``<uuid>.extract.md``. No lossy middle-elision on this path — that is the
      whole point of ADR 0006 Part 1.
    """
    base = {
        "conversation_uuid": rec.conversation_uuid,
        "source": rec.source,
        "source_id": rec.source_id,
        "first_event_at": rec.first_event_at.isoformat(),
        "last_event_at": rec.last_event_at.isoformat(),
        "raw_path": str(rec.raw_path),
    }
    if len(content) <= cfg.budgets.max_staged_content_chars:
        prompt_path = staging / f"{rec.conversation_uuid}.prompt.md"
        prompt_path.write_text(
            _fill_extract_prompt(
                cfg, prompts_root, rec, content, topic_index=topic_index,
                must_remember_index=must_remember_index,
            ),
            encoding="utf-8",
        )
        base["kind"] = "single"
        base["prompt_path"] = str(prompt_path)
        return base
    chunks = _split_on_boundaries(content, cfg.budgets.chunk_content_chars)
    per_chunk_words = _per_chunk_words(cfg, len(chunks))
    chunk_prompts: list[str] = []
    digest_paths: list[str] = []
    for i, chunk in enumerate(chunks):
        stem = f"{rec.conversation_uuid}.chunk{i + 1:02d}"
        cp_path = staging / f"{stem}.prompt.md"
        cp_path.write_text(
            _fill_prompt(
                prompts_root / "chunk_condense.md",
                agent_name=cfg.agent.name,
                chunk_index=i + 1,
                chunk_total=len(chunks),
                max_words=per_chunk_words,
                content=chunk,
            ),
            encoding="utf-8",
        )
        chunk_prompts.append(str(cp_path))
        digest_paths.append(str(staging / f"{stem}.digest.md"))
    base["kind"] = "map_reduce"
    base["chunk_prompts"] = chunk_prompts
    base["digest_paths"] = digest_paths
    base["reduce_with"] = "extract_memory.md"
    return base


def plan_extraction(
    cfg: Config, store: Store, *, max_sessions: int | None = None
) -> list[dict]:
    """Stage one extraction prompt per idle, unprocessed transcript.

    For each ``EXTRACT`` decision (capped at *max_sessions*), pre-filter +
    clip the transcript, fill the extraction prompt, and write it to
    ``<store>/.sweep-staging/<uuid>.prompt.md``. The in-persona sub-agent later
    reads *that* file (so the bulky transcript never transits the driver's
    context), emits the bundle, and writes it back via
    ``executor.ingest_extraction``. Returns the manifest items and persists
    ``.sweep-staging/manifest.json`` (carrying ``items`` + ``stacks``).
    """
    store.init_layout()
    decisions = _decide(_discover(cfg), cfg, now=time.time())

    staging = _sweep_staging_dir(store)
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    prompts_root = _prompts_root(cfg)
    topic_index = _topic_routing_index(cfg, store)
    must_remember_index = _mr_touch_index(cfg, store)
    items: list[dict] = []
    weighted: list[tuple[str, int]] = []
    processed = 0
    for d in decisions:
        # Both idle (EXTRACT) and still-active (SKIP_ACTIVE) records are now
        # candidates (``_decide`` emits only these two): an active session is
        # extracted incrementally once its post-cursor slice crosses the Q3
        # threshold (ADR 0006 Part 2); an idle one always stages whatever is
        # past its cursor.
        if max_sessions is not None and processed >= max_sessions:
            break
        rec = d.record
        sliced = _compute_incremental_slice(
            cfg, store, rec, active=(d.action == SKIP_ACTIVE)
        )
        if sliced is None:
            continue  # nothing new, or active-but-under-threshold
        content = sliced.prompt_content
        item = _stage_record(
            cfg, staging, prompts_root, rec, content, topic_index=topic_index,
            must_remember_index=must_remember_index,
        )
        # The high-water mark this slice advances the cursor to once its card is
        # ingested (the on_slice_ingested hook reads these off the manifest).
        item["cursor_event_at"] = sliced.cursor_event_at
        item["cursor_events"] = sliced.cursor_events
        items.append(item)
        # One stack slot per conversation (a map_reduce item expands into its
        # own N-map + 1-reduce sub-agent turns inside that slot), weighted by
        # the staged slice length so the packer's budget still reflects size.
        weighted.append((rec.conversation_uuid, len(content)))
        processed += 1

    stacks = _pack_stacks(
        weighted,
        char_budget=cfg.budgets.sweep_stack_content_chars,
        max_items=cfg.budgets.sweep_stack_max_items,
    )
    (staging / "manifest.json").write_text(
        json.dumps({"items": items, "stacks": stacks}, indent=2),
        encoding="utf-8",
    )
    log.info("plan_extraction: staged %d transcript(s) in %d stack(s)",
             len(items), len(stacks))
    return items


# ----- fresh-start rebuild (design §10.6 migration = fresh start) ------------


# The legacy on-disk surface the fresh-start rebuild drops: the retired
# rollup/archive store dir + the old journal markdown files. The three new
# stores (skills.md / must_remember.md / topics.md) live in journal/ and
# are NOT in this set, so a rebuild that runs after extraction keeps them.
_LEGACY_JOURNAL_FILES = (
    "must_memorize.md",
    "longer_memory.md",
    ".dropped_memorize.md",
)


def rebuild(cfg: Config, store: Store) -> int:
    """Fresh-start rebuild (design §10.6): drop the retired surface, then
    regenerate the session-start briefing (skill index + must_remember +
    topic index + detail files + unprocessed notice).

    Migration is a fresh start — there is no one-time converter. The first
    rebuild removes the old rollup ``archive/`` dir and legacy journal files
    (chronological summaries, ``must_memorize.md``, ``longer_memory.md``); the
    three bounded stores are then (re)built incrementally by extraction. The
    briefing rebuild is what regenerates the skill index by Python.
    """
    store.init_layout()
    # Serialize whole rebuilds on the configured rebuild lock — previously
    # defined + reported but never ACQUIRED (audit F5), while the bridge's
    # detached rebuild trigger makes concurrent rebuilds a normal event.
    # Skip-if-held: a rebuild is idempotent maintenance; the next trigger
    # retries.
    with store.lock(
        cfg.rebuild.lock_path,
        timeout_minutes=cfg.rebuild.rebuild_timeout_minutes,
    ) as acquired:
        if not acquired:
            log.info("rebuild: lock held by a live rebuild; skipping")
            return 0
        _drop_legacy_surface(store)
        # Per-persona format gate (plan §2 dev-3): validate + repair the
        # three stores BEFORE the briefing is assembled from them, so
        # malformed memory is mechanically fixed (or quarantined to
        # <store>.rejected.md, no silent loss) rather than persisting past
        # a wrap-up. Runs at the end of every sweep's per-persona rebuild.
        from .check import check_all
        report = check_all(cfg, store, fix=True)
        repaired = [s.store_name for s in report.stores if s.repaired]
        if repaired:
            log.warning(
                "rebuild: format-check repaired store(s): %s",
                ", ".join(repaired),
            )
        from .briefing import rebuild_briefing
        rebuild_briefing(cfg, store)
    log.info("rebuild: dropped legacy surface; format-checked; briefing regenerated")
    return 0


def _drop_legacy_surface(store: Store) -> None:
    """Remove the retired rollup/archive on-disk surface (idempotent)."""
    journal = store.paths.journal
    for name in _LEGACY_JOURNAL_FILES:
        (journal / name).unlink(missing_ok=True)
    # Retired chronological summaries (shorts + daily/weekly/monthly rollups).
    from .store import DAILY_RE, MONTHLY_RE, SHORT_RE, WEEKLY_RE
    for f in journal.glob("*.md"):
        if any(rx.match(f.name) for rx in (SHORT_RE, DAILY_RE, WEEKLY_RE, MONTHLY_RE)):
            f.unlink(missing_ok=True)
    # The retired detailed-summary archive dir.
    if store.paths.archive.exists():
        shutil.rmtree(store.paths.archive, ignore_errors=True)


# ----- pin (reframed: write a must_remember entry) --------------------------


def pin(cfg: Config, store: Store, *, memo: str, kind: str) -> int:
    """``tiger-memory pin`` — write a must_remember entry directly (design §3).

    Reframed from the old must-memorize table: a pin is now one
    :class:`MustRememberEntry`. ``operator_explicit`` pins start elevated (the
    forget-guard protects them until meditation's relevance-check). Pinning
    does not run meditation; the next sweep compacts if the store overflows.
    """
    if kind not in VALID_KINDS:
        print(f"unknown kind: {kind}")
        return 2
    store.init_layout()
    now = iso_now()
    bstore = BoundedStore(cfg, store)
    try:
        # Locked RMW (audit F2): a pin racing a compact-apply's critical
        # section must block briefly, not silently vanish — this may be an
        # operator_explicit directive.
        with bstore.store_lock_wait(STORE_MUST_REMEMBER):
            entries = bstore.load(STORE_MUST_REMEMBER)
            entries.append(
                MustRememberEntry(
                    text=memo, created_at=now, last_used=now, source="pin",
                    kind=kind,
                )
            )
            bstore.save_atomic(STORE_MUST_REMEMBER, entries)
    except StoreLockHeld as exc:
        print(f"pin failed: {exc} — retry in a moment")
        return 1
    print(f"pinned ({kind}): {memo}")
    return 0


# ----- mission text sourcing (plan §1/§2.5 — Miyagi owns the sourcing) -------


# The charter Mission is the live team goal the relevance-check reads (design
# §5; resolved decision §10.5). Sourced relative to the team root, which is
# the grandparent of a persona's store dir (``<team>/memories/<persona>/``).
CHARTER_README_REL = ("charter", "README.md")


def team_mission_text(cfg: Config) -> str:
    """Read the live team mission from ``<team>/charter/README.md`` (design §5).

    Resolves the team root from the persona store layout
    (``<team>/memories/<persona>/`` → ``<team>``) and returns the charter
    README text. Returns ``""`` if the charter is missing/unreadable — the
    relevance-check then keeps every directive (a missing mission must never
    cause a directive to be judged stale-and-dropped).
    """
    # store.root == <team>/memories/<persona>; team root is two parents up.
    team_root = cfg.store.root.parent.parent
    charter = team_root.joinpath(*CHARTER_README_REL)
    try:
        return charter.read_text(encoding="utf-8")
    except OSError:
        log.info("charter mission not found at %s; relevance-check keeps all", charter)
        return ""


# ----- prompt loading + filling ---------------------------------------------


def _prompts_root(cfg: Config) -> Path:
    here = Path(__file__).parent / "summarizers" / "prompts" / cfg.summarizer.prompts
    if here.exists():
        return here
    return Path("summarizers/prompts") / cfg.summarizer.prompts  # pragma: no cover


def _fill_prompt(path: Path, **kwargs) -> str:
    template = path.read_text(encoding="utf-8")
    return template.format_map(_SafeFormatDict(kwargs))


class _SafeFormatDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


_CLIP_MARKER = "\n\n[...content elided...]\n\n"


def _clip(text: str, max_chars: int) -> str:
    """Bounded clip: keep head + tail within *max_chars* (lossy middle elision).

    The extraction sub-agent owns the reduce of an oversized transcript; this
    is only the last-resort ceiling so a pathological transcript never blows
    the context window.
    """
    if len(text) <= max_chars:
        return text
    if max_chars <= len(_CLIP_MARKER):
        return text[:max_chars]
    half = (max_chars - len(_CLIP_MARKER)) // 2
    return text[:half] + _CLIP_MARKER + text[-half:]


def _split_on_boundaries(text: str, max_chars: int) -> list[str]:
    """Split *text* into consecutive chunks each ``<= max_chars``, losslessly.

    Prefers line boundaries so a chunk never cuts mid-line. A single line
    longer than *max_chars* is hard-split (the only case that cuts within a
    line). The concatenation of the returned chunks equals *text* exactly —
    that losslessness is what makes chunk-and-reduce preserve the middle of an
    oversized transcript where ``_clip`` silently dropped it.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    buf: list[str] = []
    running = 0
    for line in text.splitlines(keepends=True):
        if len(line) > max_chars:
            if buf:
                chunks.append("".join(buf))
                buf, running = [], 0
            for i in range(0, len(line), max_chars):
                piece = line[i:i + max_chars]
                if len(piece) == max_chars:
                    chunks.append(piece)
                else:
                    buf.append(piece)
                    running += len(piece)
            continue
        if buf and running + len(line) > max_chars:
            chunks.append("".join(buf))
            buf, running = [], 0
        buf.append(line)
        running += len(line)
    if buf:
        chunks.append("".join(buf))
    return chunks


def _concat_digests(parts: list[str]) -> str:
    """Join mapped chunk digests with explicit part markers (reduce input)."""
    return "\n\n".join(
        f"[transcript part {i + 1}/{len(parts)}]\n{p}"
        for i, p in enumerate(parts)
    )


def _reduce_digests(parts: list[str], cfg: Config, *, condense) -> str:
    """Reduce mapped chunk digests into one under-ceiling block.

    Concatenate *parts*; while the concatenation still exceeds
    ``max_staged_content_chars`` and depth budget remains
    (``max_reduce_depth``), re-condense each part via the injected *condense*
    callable ``(part, index, total) -> str`` — one more map round, keeping the
    part count (and thus the fixed per-part marker overhead) constant so the
    loop actually converges as content shrinks. At the depth cap, fall back to
    the bounded ``_clip`` guard so termination is guaranteed even for a
    pathological (non-shrinking) condenser. This is the lossless main line with
    a bounded last-resort guard, never ``_clip``'s silent middle-elision on the
    normal path.

    *condense* is an injectable seam: in production it is another sub-agent map
    round; tests inject a deterministic callable so every branch here (fits
    immediately / shrinks within budget / hits the depth-cap guard) is
    reachable without a live model.
    """
    ceiling = cfg.budgets.max_staged_content_chars
    text = _concat_digests(parts)
    depth = 0
    while len(text) > ceiling and depth < cfg.budgets.max_reduce_depth:
        parts = [condense(p, i + 1, len(parts)) for i, p in enumerate(parts)]
        text = _concat_digests(parts)
        depth += 1
    if len(text) > ceiling:
        log.warning(
            "reduce-digests: hit depth cap (%d) at %d chars; bounded-clip guard",
            cfg.budgets.max_reduce_depth, len(text),
        )
        return _clip(text, ceiling)
    return text


def build_reduce_prompt(cfg: Config, store: Store, item: dict) -> str | None:
    """Assemble the final extraction prompt for one ``map_reduce`` item from its
    staged chunk digests — the reduce step of ADR 0006 Part 1.

    Returns the written ``<uuid>.prompt.md`` path, or ``None`` if not every
    digest is staged yet (the map phase is still in flight for this uuid — a
    later sweep pass retries). Concatenates the digests with part markers,
    applies the bounded last-resort ``_clip`` guard only if the concatenation
    still exceeds the staging ceiling, then fills the single-sourced
    ``extract_memory.md`` contract over them — yielding the SAME
    ``<uuid>.prompt.md`` filename + shape the single path produces, so the
    downstream card-write + ingest are identical.

    The multi-round sub-agent re-condense lives in the sweep skill (it needs
    sub-agent turns, which only the subscription rail can run); this pure-Python
    step is the guaranteed-terminating guard so the staged reduce prompt never
    exceeds the ceiling even if the skill stops condensing early.
    """
    digest_paths = [Path(p) for p in item["digest_paths"]]
    if not all(p.exists() for p in digest_paths):
        return None
    content = _concat_digests([p.read_text(encoding="utf-8") for p in digest_paths])
    ceiling = cfg.budgets.max_staged_content_chars
    if len(content) > ceiling:
        content = _clip(content, ceiling)
    rec = SimpleNamespace(
        source=item["source"],
        source_id=item["source_id"],
        first_event_at=datetime.fromisoformat(item["first_event_at"]),
        last_event_at=datetime.fromisoformat(item["last_event_at"]),
    )
    prompt_path = _sweep_staging_dir(store) / f"{item['conversation_uuid']}.prompt.md"
    prompt_path.write_text(
        _fill_extract_prompt(
            cfg, _prompts_root(cfg), rec, content,
            topic_index=_topic_routing_index(cfg, store),
            must_remember_index=_mr_touch_index(cfg, store),
        ),
        encoding="utf-8",
    )
    return str(prompt_path)

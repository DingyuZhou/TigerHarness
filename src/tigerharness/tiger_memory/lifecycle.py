"""Session → memory extraction (bounded-store revamp; design §2, §4; plan §2 dev-3).

This module turns a *finished* session — discovered via the unchanged
``sources/`` adapters — into candidate entries for the three bounded stores
(``skills`` / ``must_remember`` / ``emotional``), in-persona, then ingests
them through Mitsui's :class:`BoundedStore`. It replaces the old
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
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .bounded_store import BoundedStore
from .config import Config
from .entries import (
    KIND_OPERATOR_EXPLICIT,
    STORE_DIARY,
    STORE_MUST_REMEMBER,
    STORE_SKILLS,
    VALID_KINDS,
    BaseEntry,
    DiaryEntry,
    MustRememberEntry,
    SkillEntry,
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
# ``summarizers/prompts/default/v1/extract_memory.md``): three whole-line
# section markers, in this order.
MARK_SKILLS = "@@SKILLS@@"
MARK_MUST_REMEMBER = "@@MUST_REMEMBER@@"
MARK_DIARY = "@@DIARY@@"
_MARKERS = (MARK_SKILLS, MARK_MUST_REMEMBER, MARK_DIARY)


class ExtractionParseError(ValueError):
    """The extraction output didn't satisfy the marker contract."""


@dataclass
class Candidates:
    """Parsed extraction candidates for the three stores (typed, unscored)."""

    skills: list[SkillEntry]
    must_remember: list[MustRememberEntry]
    diary: list[DiaryEntry]

    def is_empty(self) -> bool:
        return not (self.skills or self.must_remember or self.diary)

    def total(self) -> int:
        return len(self.skills) + len(self.must_remember) + len(self.diary)


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
    i_s, i_m, i_e = pos[MARK_SKILLS], pos[MARK_MUST_REMEMBER], pos[MARK_DIARY]
    if not (i_s < i_m < i_e):
        raise ExtractionParseError("section markers out of order")
    return {
        STORE_SKILLS: "\n".join(lines[i_s + 1:i_m]).strip(),
        STORE_MUST_REMEMBER: "\n".join(lines[i_m + 1:i_e]).strip(),
        STORE_DIARY: "\n".join(lines[i_e + 1:]).strip(),
    }


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
        skills.append(
            SkillEntry(
                text=proc, created_at=now, last_used=now, source=source,
                name=name, trigger=trigger, procedure=proc,
                usage_count=0, importance=0.0,
            )
        )
    must: list[MustRememberEntry] = []
    for b in _section_blocks(sections[STORE_MUST_REMEMBER]):
        kind = (b.get("KIND") or "").lower()
        memo = b.get("MEMO")
        if kind not in VALID_KINDS or not memo:
            continue
        must.append(
            MustRememberEntry(
                text=memo, created_at=now, last_used=now, source=source,
                kind=kind, importance=1.0,
            )
        )
    emo: list[DiaryEntry] = []
    for b in _section_blocks(sections[STORE_DIARY]):
        body = b.get("TEXT")
        weight = _parse_weight(b.get("WEIGHT"))
        if weight is None or not body:
            continue
        emo.append(
            DiaryEntry(
                text=body, created_at=now, last_used=now, source=source,
                weight=weight,
            )
        )
    return Candidates(skills=skills, must_remember=must, diary=emo)


def _parse_weight(raw: str | None) -> float | None:
    """Parse a signed emotional weight; ``None`` if missing/unparseable."""
    if raw is None:
        return None
    try:
        return float(raw.strip().split()[0]) if raw.strip() else None
    except (ValueError, IndexError):
        return None


# ----- extraction (the model touch point) -----------------------------------


def extract_candidates(
    cfg: Config,
    summarizer: Summarizer,
    rec: SourceRecord,
    *,
    now: str | None = None,
) -> Candidates:
    """Run the extraction prompt over *rec* and parse the typed candidates.

    The single LLM call (mock in CI). A backend error or a malformed bundle is
    logged-and-swallowed into empty candidates so one bad session never aborts
    a sweep. The transcript is pre-filtered (if enabled) and clipped to the
    staged ceiling before the call.
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
    prompt = _fill_prompt(
        _prompts_root(cfg) / "extract_memory.md",
        agent_name=cfg.agent.name,
        source=rec.source,
        source_id=rec.source_id,
        first_event_at=rec.first_event_at.isoformat(),
        last_event_at=rec.last_event_at.isoformat(),
        procedure_max_words=cfg.memory_extract.skill_procedure_words,
        memo_max_words=cfg.memory_extract.memo_words,
        reaction_max_words=cfg.memory_extract.reaction_words,
        weight_cap=int(cfg.memory.diary.weight_cap),
        content=content,
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
    return Candidates(skills=[], must_remember=[], diary=[])


# ----- ingest (candidates → bounded stores) ---------------------------------


def ingest_candidates(
    bstore: BoundedStore,
    cfg: Config,
    candidates: Candidates,
    *,
    now: str | None = None,
) -> dict[str, int]:
    """Merge *candidates* into the three bounded stores (per-store, atomic).

    Returns a per-store count of entries added. Each store is loaded, the new
    candidates appended (skills get their ``importance`` refreshed from
    ``usage_count`` + ``last_used``), and the whole store re-saved atomically.
    Meditation/compaction is NOT run here — the sweep runs it post-ingest only
    when a store is over its overflow limit (the hysteresis trigger).
    """
    now = now or iso_now()
    added = {STORE_SKILLS: 0, STORE_MUST_REMEMBER: 0, STORE_DIARY: 0}
    if candidates.is_empty():
        return added
    per_store: dict[str, list[BaseEntry]] = {
        STORE_SKILLS: list(candidates.skills),
        STORE_MUST_REMEMBER: list(candidates.must_remember),
        STORE_DIARY: list(candidates.diary),
    }
    for store_name, new_entries in per_store.items():
        if not new_entries:
            continue
        existing = bstore.load(store_name)
        for entry in new_entries:
            if isinstance(entry, SkillEntry):
                refresh_importance(entry, now, cfg)
        merged = existing + new_entries
        bstore.save_atomic(store_name, merged)
        added[store_name] = len(new_entries)
    log.info(
        "ingest: +%d skills, +%d must_remember, +%d emotional",
        added[STORE_SKILLS], added[STORE_MUST_REMEMBER], added[STORE_DIARY],
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

    When ``memory.diary.evocation_enabled`` is set, runs the associative-evocation
    pass after ingest (one batched summarizer call): reinforce the old items each
    new diary note recalls and append a concise recall reference. Default off, so
    this is a no-op unless deliberately enabled (the model call is a rail choice).
    """
    now = now or iso_now()
    candidates = extract_candidates(cfg, summarizer, rec, now=now)
    bstore = BoundedStore(cfg, store)
    added = ingest_candidates(bstore, cfg, candidates, now=now)
    if cfg.memory.diary.evocation_enabled:
        from .evocation import evoke_and_reinforce
        evoke_and_reinforce(bstore, cfg, candidates, summarizer, now=now)
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
                    project_path=Path(s.fields["project_path"]),
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
    items: list[dict] = []
    weighted: list[tuple[str, int]] = []
    processed = 0
    for d in decisions:
        if d.action != EXTRACT:
            continue
        if max_sessions is not None and processed >= max_sessions:
            break
        rec = d.record
        content = rec.content
        if cfg.prefilter.enabled:
            content = filter_transcript(
                content,
                drop_tool_results=cfg.prefilter.drop_tool_results,
                drop_system_reminders=cfg.prefilter.drop_system_reminders,
            )
        clipped = _clip(content, cfg.budgets.max_staged_content_chars)
        prompt = _fill_prompt(
            prompts_root / "extract_memory.md",
            agent_name=cfg.agent.name,
            source=rec.source,
            source_id=rec.source_id,
            first_event_at=rec.first_event_at.isoformat(),
            last_event_at=rec.last_event_at.isoformat(),
            procedure_max_words=cfg.memory_extract.skill_procedure_words,
            memo_max_words=cfg.memory_extract.memo_words,
            reaction_max_words=cfg.memory_extract.reaction_words,
            weight_cap=int(cfg.memory.diary.weight_cap),
            content=clipped,
        )
        prompt_path = staging / f"{rec.conversation_uuid}.prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        items.append({
            "conversation_uuid": rec.conversation_uuid,
            "source": rec.source,
            "source_id": rec.source_id,
            "first_event_at": rec.first_event_at.isoformat(),
            "last_event_at": rec.last_event_at.isoformat(),
            "raw_path": str(rec.raw_path),
            "prompt_path": str(prompt_path),
        })
        weighted.append((rec.conversation_uuid, len(clipped)))
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
# stores (skills.md / must_remember.md / diary.md) live in journal/ and
# are NOT in this set, so a rebuild that runs after extraction keeps them.
_LEGACY_JOURNAL_FILES = (
    "must_memorize.md",
    "longer_memory.md",
    ".dropped_memorize.md",
)


def rebuild(cfg: Config, store: Store) -> int:
    """Fresh-start rebuild (design §10.6): drop the retired surface, then
    regenerate the session-start briefing (skill index + must_remember +
    emotional view + unprocessed notice).

    Migration is a fresh start — there is no one-time converter. The first
    rebuild removes the old rollup ``archive/`` dir and legacy journal files
    (chronological summaries, ``must_memorize.md``, ``longer_memory.md``); the
    three bounded stores are then (re)built incrementally by extraction. The
    briefing rebuild is what regenerates the skill index by Python.
    """
    store.init_layout()
    _drop_legacy_surface(store)
    # Per-persona format gate (plan §2 dev-3): validate + repair the three
    # stores BEFORE the briefing is assembled from them, so malformed memory is
    # mechanically fixed (or quarantined to <store>.rejected.md, no silent loss)
    # rather than persisting past a wrap-up. Runs at the end of every sweep's
    # per-persona rebuild.
    from .check import check_all
    report = check_all(cfg, store, fix=True)
    repaired = [s.store_name for s in report.stores if s.repaired]
    if repaired:
        log.warning("rebuild: format-check repaired store(s): %s", ", ".join(repaired))
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
    entries = bstore.load(STORE_MUST_REMEMBER)
    importance = 5.0 if kind == KIND_OPERATOR_EXPLICIT else 1.0
    entries.append(
        MustRememberEntry(
            text=memo, created_at=now, last_used=now, source="pin",
            kind=kind, importance=importance,
        )
    )
    bstore.save_atomic(STORE_MUST_REMEMBER, entries)
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

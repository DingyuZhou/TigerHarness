"""Lazy rebuild engine — bootstrap, rebuild, resummarize.

Implements §7 (algorithm) and §11 (bootstrap) of the design doc.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable
from uuid import NAMESPACE_URL, uuid4, uuid5

from . import frontmatter, must_memorize as mm
from .collapse import CollapseParseError, parse_collapsed
from .config import Config
from .metrics import RebuildMetrics
from .prefilter import filter_transcript
from .sources import (
    ClaudeTranscriptAdapter,
    DocsAdapter,
    SourceAdapter,
    SourceRecord,
)
from .state import iso_now
from .store import (
    DAILY_RE,
    MONTHLY_RE,
    SHORT_RE,
    WEEKLY_RE,
    Store,
)
from .summarizers import (
    AnthropicSummarizer,
    MockSummarizer,
    Summarizer,
    get_summarizer,
)


log = logging.getLogger("tigerharness.tiger_memory.lifecycle")


# ----- session-decision constants ------------------------------------------

SKIP_ACTIVE = "skip_active"
SUMMARIZE_NEW = "summarize_new"
SKIP_CLEAN = "skip_clean"
RE_SUMMARIZE = "re_summarize"
ADDENDUM = "addendum"


@dataclass
class Decision:
    record: SourceRecord
    action: str
    existing_archive: Path | None = None
    existing_short: Path | None = None


# ----- entry points: bootstrap / rebuild / resummarize --------------------


def bootstrap(
    cfg: Config,
    store: Store,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    summarizer_override: Summarizer | None = None,
) -> int:
    """One-shot backfill (§11). Resumable: already-written archives skip."""
    store.init_layout()
    with store.lock(cfg.rebuild.lock_path, cfg.rebuild.rebuild_timeout_minutes) as got:
        if not got:
            print("another tiger-memory run is in progress.")
            return 1

        # Bootstrap is a one-shot backfill: ignore the 7-day rebuild cap.
        # ``--limit`` is the user-visible safety; the discovery cutoff is
        # only there to gate the lazy rebuild path.
        adapters = _build_adapters(cfg, max_age_days=None)
        summarizer = summarizer_override or _build_summarizer(cfg, mock=dry_run)
        if dry_run:
            print(f"DRY-RUN: using {summarizer.tag} (no model spend)")

        records = []
        for adapter in adapters:
            for rec in adapter.discover():
                records.append(rec)
                if limit and len(records) >= limit:
                    break
            if limit and len(records) >= limit:
                break

        # In bootstrap, also process auto-memory directory → synthetic conv.
        am = _auto_memory_record(cfg)
        if am is not None and (not limit or len(records) < limit):
            records.append(am)

        print(f"discovered {len(records)} source records")

        # Treat bootstrap same as rebuild — respect 2h idle rule.
        decisions = _decide(records, store, cfg, now=time.time())
        new_or_resum = [
            d for d in decisions
            if d.action in (SUMMARIZE_NEW, RE_SUMMARIZE, ADDENDUM)
        ]
        print(
            f"to process: {len(new_or_resum)} (new/resummarize/addendum); "
            f"{sum(1 for d in decisions if d.action == SKIP_CLEAN)} clean; "
            f"{sum(1 for d in decisions if d.action == SKIP_ACTIVE)} active"
        )

        if dry_run:
            sample = new_or_resum[:5]
            print(f"DRY-RUN: would process {len(sample)} of {len(new_or_resum)}")
            _process_decisions(sample, store, cfg, summarizer)
            est = _estimate_total_cost(cfg, len(new_or_resum), records)
            print(f"DRY-RUN: estimated total cost: ${est:.2f}")
            return 0

        metrics = RebuildMetrics()
        cost = _process_decisions(new_or_resum, store, cfg, summarizer, metrics)
        _finalize_rebuild(
            store, cfg, summarizer,
            decisions=decisions, cost=cost, metrics=metrics,
            last_op="bootstrap",
        )
        print(f"bootstrap done. cost: ${cost:.2f}")
    return 0


def rebuild(
    cfg: Config,
    store: Store,
    *,
    background: bool = False,
    summarizer_override: Summarizer | None = None,
) -> int:
    """Lazy rebuild (§7.3)."""
    if background and "TIGER_MEMORY_BACKGROUND_SPAWNED" not in os.environ:
        return _spawn_background()
    store.init_layout()
    with store.lock(cfg.rebuild.lock_path, cfg.rebuild.rebuild_timeout_minutes) as got:
        if not got:
            print("another tiger-memory rebuild is in progress; skipping.")
            return 0  # graceful no-op
        start = time.time()
        adapters = _build_adapters(cfg)
        summarizer = summarizer_override or _build_summarizer(cfg)

        records = []
        for adapter in adapters:
            # Skip docs in rebuild (one-shot during bootstrap only).
            if isinstance(adapter, DocsAdapter):
                continue
            records.extend(adapter.discover())

        decisions = _decide(records, store, cfg, now=time.time())
        new_or_resum = [
            d for d in decisions
            if d.action in (SUMMARIZE_NEW, RE_SUMMARIZE, ADDENDUM)
        ]

        metrics = RebuildMetrics()
        cost = _process_decisions(
            new_or_resum, store, cfg, summarizer, metrics,
            max_sessions=cfg.cap.max_sessions_per_rebuild,
            max_usd=cfg.cap.max_usd_per_rebuild,
        )
        duration = time.time() - start
        _finalize_rebuild(
            store, cfg, summarizer,
            decisions=decisions, cost=cost, metrics=metrics,
            last_op="rebuild", duration_sec=duration,
        )
    return 0


def resummarize(
    cfg: Config,
    store: Store,
    *,
    since: str,
    summarizer: str | None = None,
) -> int:
    """Re-summarize shorts whose first_event_at is on/after *since* (YYYY-MM-DD)."""
    store.init_layout()
    try:
        since_date = date.fromisoformat(since)
    except ValueError:
        print(f"--since must be YYYY-MM-DD; got {since!r}")
        return 2
    with store.lock(cfg.rebuild.lock_path, cfg.rebuild.rebuild_timeout_minutes) as got:
        if not got:
            print("another run is in progress.")
            return 1

        # ``--since`` is the user's explicit date range; the discovery
        # cutoff would silently override anything older than 7 days.
        # Disable it here so resummarize honors the requested window.
        adapters = _build_adapters(cfg, max_age_days=None)
        summarizer_obj = _build_summarizer(cfg)

        # Find all sessions whose first_event_at >= since
        records = []
        for adapter in adapters:
            if isinstance(adapter, DocsAdapter):
                continue
            for rec in adapter.discover():
                if rec.first_event_at.date() >= since_date:
                    records.append(rec)

        forced: list[Decision] = []
        for rec in records:
            archive = store.find_archive(rec.conversation_uuid)
            short = store.find_short(rec.conversation_uuid)
            forced.append(
                Decision(
                    record=rec,
                    action=RE_SUMMARIZE if archive else SUMMARIZE_NEW,
                    existing_archive=archive,
                    existing_short=short,
                )
            )
        print(f"resummarize: {len(forced)} sessions since {since}")

        metrics = RebuildMetrics()
        cost = _process_decisions(forced, store, cfg, summarizer_obj, metrics)
        _finalize_rebuild(
            store, cfg, summarizer_obj,
            decisions=forced, cost=cost, metrics=metrics,
            last_op="resummarize",
        )
        print(f"resummarize done. cost: ${cost:.2f}")
    return 0


# ----- finalize stage (shared non-AI tail) ---------------------------------


def _finalize_rebuild(
    store: Store,
    cfg: Config,
    summarizer: Summarizer,
    *,
    decisions: list[Decision],
    cost: float,
    metrics: RebuildMetrics,
    last_op: str,
    duration_sec: float | None = None,
) -> None:
    """The non-AI tail shared by every rebuild entry point
    (bootstrap / rebuild / resummarize): cascade rollups, fold
    longer-memory, decay must-memorize, write state, rebuild the briefing.

    This is the ``finalize`` stage of the P2 plan -> execute -> finalize
    split (see ``docs/tiger-memory-rework.md``, "B1/B8 — implementation
    design"): the in-session summarization path will call this same tail
    once its sub-agents have written the per-session artifacts.
    """
    _cascade_all_rollups(store, cfg, summarizer)
    _refresh_longer_memory(store, cfg, summarizer)
    _apply_decay(store, cfg)
    # Write state BEFORE building the briefing so the manifest's
    # last_rebuild_at reflects THIS run, not the previous one.
    _write_state(
        store, cfg, decisions=decisions, cost_usd=cost,
        duration_sec=duration_sec, last_op=last_op, metrics=metrics,
    )
    from .briefing import rebuild_briefing
    rebuild_briefing(cfg, store)


# ----- B1 stage-2: plan side (in-session sub-agent executor) ----------------


def _sweep_staging_dir(store: Store) -> Path:
    return store.root / ".sweep-staging"


def plan_rebuild(
    cfg: Config,
    store: Store,
    *,
    max_sessions: int | None = None,
) -> list[dict]:
    """B1 stage-2 PLAN (non-AI): stage one collapsed prompt per flagged
    transcript for the in-session sub-agent executor, and return the work
    manifest.

    For each ``SUMMARIZE_NEW`` / ``RE_SUMMARIZE`` decision (capped at
    *max_sessions*), apply the P1.1 pre-filter, fill ``combined_summary.md``
    with the clipped content, and write it to
    ``<store>/.sweep-staging/<uuid>.prompt.md``. The sub-agent later reads
    *that* file (so the bulky transcript never transits the driver's
    context — B8), emits the bundle, and writes it back via
    ``executor.ingest_collapsed_summary``. ADDENDUM is deferred (the
    collapsed pass targets the 3-call new-session cost).

    Returns the manifest items and persists ``.sweep-staging/manifest.json``.
    """
    store.init_layout()
    adapters = _build_adapters(cfg)
    records: list[SourceRecord] = []
    for adapter in adapters:
        if isinstance(adapter, DocsAdapter):
            continue
        records.extend(adapter.discover())
    decisions = _decide(records, store, cfg, now=time.time())

    staging = _sweep_staging_dir(store)
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    prompts_root = _prompts_root(cfg)
    items: list[dict] = []
    processed = 0
    for d in decisions:
        if d.action not in (SUMMARIZE_NEW, RE_SUMMARIZE):
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
        prompt = _fill_prompt(
            prompts_root / "combined_summary.md",
            agent_name=cfg.agent.name,
            short_max_words=cfg.budgets.short_summary_words,
            detailed_max_words=cfg.budgets.detailed_summary_words,
            memo_max_words=cfg.budgets.must_memorize_memo_words,
            source=rec.source,
            source_id=rec.source_id,
            first_event_at=rec.first_event_at.isoformat(),
            last_event_at=rec.last_event_at.isoformat(),
            content=_clip(content, max_chars=cfg.budgets.max_prompt_content_chars),
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
            "action": d.action,
        })
        processed += 1

    (staging / "manifest.json").write_text(
        json.dumps({"items": items}, indent=2), encoding="utf-8"
    )
    return items


# ----- adapter / summarizer factories --------------------------------------


def _build_adapters(
    cfg: Config, *, max_age_days: int | None = 7,
) -> list[SourceAdapter]:
    """Build source adapters from config.

    ``max_age_days`` is forwarded to ``ClaudeTranscriptAdapter`` as the
    discovery cutoff (skip JSONLs older than N days by mtime).
    The default ``7`` is the loop-prevention cap used by ``rebuild``.
    Pass ``None`` from ``bootstrap`` and ``resummarize`` -- both commands
    have their own scoping (``--limit`` and ``--since`` respectively) and
    must see the full corpus to honor it. Duplicates the constructor
    default in ``ClaudeTranscriptAdapter``; keep them in sync.
    """
    adapters: list[SourceAdapter] = []
    # Find slack threads.json (if a slack_thread source is configured)
    threads_json: Path | None = None
    for s in cfg.sources:
        if s.kind == "slack_thread":
            raw = s.fields.get("threads_json", "")
            threads_json = Path(raw).expanduser() if raw else None
    repo_root = (
        cfg.source_path.parent if cfg.source_path else Path.cwd()
    )
    for s in cfg.sources:
        if s.kind == "claude_code":
            # Per-persona filtering (added after multi-bridge introduced
            # N-persona routing): when `persona:` is set on this source,
            # only sessions owned by that persona in threads.json are
            # ingested. `include_unattributed: true` brings in local
            # claude-p sessions (no bridge attribution) as well.
            persona = s.fields.get("persona")
            persona = persona.strip() if isinstance(persona, str) and persona.strip() else None
            # B4 — optional team qualifier; defaults None (name-only match).
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
                )
            )
        elif s.kind == "slack_thread":
            # Note: there's no separate Slack adapter. Slack threads
            # share the Claude transcript JSONL location with regular
            # Claude Code sessions; the slack_thread config kind just
            # supplies threads.json so the claude_code adapter can
            # classify some sessions as `source: slack` via reverse
            # lookup. See HANDOFF.md for the rationale.
            pass
        elif s.kind == "docs":  # pragma: no branch  # only known kinds: claude_code, slack_thread, docs
            adapters.append(
                DocsAdapter(
                    glob_pattern=s.fields["glob"],
                    repo_root=repo_root,
                )
            )
    return adapters


def _build_summarizer(cfg: Config, mock: bool = False) -> Summarizer:
    if mock:
        return MockSummarizer()
    # Look up the backend by name in the summarizer registry. Anthropic
    # is pre-registered; external code can plug in new vendors via
    # ``register_summarizer()`` -- see the summarizers package docstring.
    return get_summarizer(cfg.summarizer.backend, cfg.summarizer)


# ----- decision tree per §7.3 step 1 ---------------------------------------


def _decide(
    records: Iterable[SourceRecord], store: Store, cfg: Config, *, now: float
) -> list[Decision]:
    out: list[Decision] = []
    idle_threshold_sec = cfg.rebuild.idle_threshold_hours * 3600
    resummarize_window_sec = cfg.rebuild.resummarize_window_days * 86400
    for rec in records:
        archive = store.find_archive(rec.conversation_uuid)
        short = store.find_short(rec.conversation_uuid)
        activity_age = now - rec.activity_mtime

        if activity_age < idle_threshold_sec:
            out.append(Decision(rec, SKIP_ACTIVE, archive, short))
            continue
        if archive is None:
            out.append(Decision(rec, SUMMARIZE_NEW, archive, short))
            continue
        archive_mtime = archive.stat().st_mtime
        if rec.activity_mtime <= archive_mtime:
            out.append(Decision(rec, SKIP_CLEAN, archive, short))
            continue
        summary_age = now - archive_mtime
        if summary_age <= resummarize_window_sec:
            out.append(Decision(rec, RE_SUMMARIZE, archive, short))
        else:
            out.append(Decision(rec, ADDENDUM, archive, short))
    return out


# ----- per-session processing ----------------------------------------------


def _cap_reason(
    processed: int,
    spent_usd: float,
    max_sessions: int | None,
    max_usd: float | None,
) -> str | None:
    """Return why a rebuild should stop processing, or ``None`` to continue.

    The cost/scope cap (P1.2 / Lever 1.4): a rebuild processes at most
    ``max_sessions`` sessions, or stops once ``spent_usd`` reaches
    ``max_usd`` — whichever trips first. ``None`` for either bound
    disables it (the default for user-scoped bootstrap / resummarize).
    """
    if max_sessions is not None and processed >= max_sessions:
        return "session_cap"
    if max_usd is not None and spent_usd >= max_usd:
        return "usd_cap"
    return None


def _process_decisions(
    decisions: list[Decision],
    store: Store,
    cfg: Config,
    summarizer: Summarizer,
    metrics: RebuildMetrics | None = None,
    *,
    max_sessions: int | None = None,
    max_usd: float | None = None,
) -> float:
    """Run the summarizer for each decision and write archive/short.

    Returns total cost in USD — from ``summarizer.cost_so_far`` when
    the backend reports real numbers (Anthropic does), or a rough
    char-count heuristic for backends that don't (MockSummarizer reports 0).

    The transcript pre-filter (P1.1) runs once per record here, *before*
    any summarize call, so all of short/detailed/addendum/extractor reuse
    the same de-noised content. When *metrics* is supplied, the raw vs.
    filtered char counts and the per-session call count are accumulated
    into it for the rebuild's ``state.json`` snapshot.

    ``max_sessions`` / ``max_usd`` are the P1.2 cost/scope cap: once
    either trips we stop *before* starting the next session, leaving the
    remainder unprocessed. Those records simply keep no archive, so the
    next rebuild's ``_decide`` re-emits them — resumability with no extra
    state. Both default ``None`` (uncapped) for the user-scoped paths.
    """
    cost_at_start = summarizer.cost_so_far
    heuristic_fallback = 0.0
    rows = mm.load(store)
    today = datetime.now(timezone.utc).date().isoformat()
    processed = 0
    for d in decisions:
        if d.action == SKIP_ACTIVE or d.action == SKIP_CLEAN:
            continue
        reason = _cap_reason(
            processed,
            summarizer.cost_so_far - cost_at_start,
            max_sessions,
            max_usd,
        )
        if reason is not None:
            if metrics is not None:
                metrics.note_capped(reason)
            log.info(
                "tiger-memory rebuild cap hit (%s) after %d session(s); "
                "deferring the rest to the next rebuild",
                reason, processed,
            )
            break
        rec = d.record
        raw_chars = len(rec.content)
        if cfg.prefilter.enabled:
            rec = replace(
                rec,
                content=filter_transcript(
                    rec.content,
                    drop_tool_results=cfg.prefilter.drop_tool_results,
                    drop_system_reminders=cfg.prefilter.drop_system_reminders,
                ),
            )
        filtered_chars = len(rec.content)
        try:
            if d.action == ADDENDUM:
                _write_addendum(store, cfg, summarizer, rec)
                heuristic_fallback += _approx_cost(cfg, rec.content,
                                                   n_short=1, n_detailed=0)
                candidates = _extract_must_memorize(cfg, summarizer, rec)
                calls = 2  # short + extractor
            elif cfg.collapse.enabled:  # SUMMARIZE_NEW / RE_SUMMARIZE, collapsed
                candidates, calls = _write_session_collapsed(
                    store, cfg, summarizer, rec
                )
                heuristic_fallback += _approx_cost(cfg, rec.content,
                                                   n_short=1, n_detailed=1)
            else:  # SUMMARIZE_NEW / RE_SUMMARIZE, legacy 3-call
                _write_short_and_archive(store, cfg, summarizer, rec)
                heuristic_fallback += _approx_cost(cfg, rec.content,
                                                   n_short=1, n_detailed=1)
                candidates = _extract_must_memorize(cfg, summarizer, rec)
                calls = 3  # short + detailed + extractor

            rows, demoted = mm.merge_candidates(
                rows,
                candidates,
                today=today,
                similarity_threshold=cfg.budgets.repeat_detection_similarity,
                max_rows=cfg.budgets.must_memorize_rows,
            )
            if demoted:
                mm.append_dropped(store, demoted)
            processed += 1
            if metrics is not None:
                metrics.record_session(
                    chars_raw=raw_chars,
                    chars_filtered=filtered_chars,
                    calls=calls,
                )
        except Exception:
            log.exception(
                "failed to process %s (%s)", rec.conversation_uuid, d.action
            )
            continue
    mm.save(store, rows)

    real_cost = summarizer.cost_so_far - cost_at_start
    # If the backend reported real cost (>0), trust it. Otherwise fall
    # back to the heuristic — useful in dry-runs with MockSummarizer.
    return real_cost if real_cost > 0 else heuristic_fallback


def _write_short_and_archive(
    store: Store,
    cfg: Config,
    summarizer: Summarizer,
    rec: SourceRecord,
) -> int:
    prompts_root = _prompts_root(cfg)

    # Short summary
    short_prompt = _fill_prompt(
        prompts_root / "short_summary.md",
        agent_name=cfg.agent.name,
        max_words=cfg.budgets.short_summary_words,
        source=rec.source,
        source_id=rec.source_id,
        first_event_at=rec.first_event_at.isoformat(),
        last_event_at=rec.last_event_at.isoformat(),
        content=_clip(rec.content, max_chars=cfg.budgets.max_prompt_content_chars),
    )
    short_body = summarizer.summarize(
        prompt=short_prompt, max_words=cfg.budgets.short_summary_words
    )

    # Detailed summary
    detailed_prompt = _fill_prompt(
        prompts_root / "detailed_summary.md",
        agent_name=cfg.agent.name,
        max_words=cfg.budgets.detailed_summary_words,
        source=rec.source,
        source_id=rec.source_id,
        first_event_at=rec.first_event_at.isoformat(),
        last_event_at=rec.last_event_at.isoformat(),
        content=_clip(rec.content, max_chars=cfg.budgets.max_prompt_content_chars),
    )
    detailed_body = summarizer.summarize(
        prompt=detailed_prompt, max_words=cfg.budgets.detailed_summary_words
    )

    return _write_short_archive_bodies(
        store, cfg, summarizer.tag, rec, short_body, detailed_body
    )


def _write_short_archive_bodies(
    store: Store,
    cfg: Config,
    summarizer_tag: str,
    rec: SourceRecord,
    short_body: str,
    detailed_body: str,
) -> int:
    """Write the short + detailed-archive files for *rec*. Shared by the
    legacy 3-call path, the collapsed single-call path, and the in-session
    sub-agent ingest (B1 stage-2) so all emit identical on-disk artifacts.
    Takes a *summarizer_tag* string (not a Summarizer) so the sub-agent
    path — which has no Python summarizer object — can reuse it."""
    filename = Store.short_filename(rec.first_event_at, rec.conversation_uuid)
    short_path = store.paths.journal / filename
    archive_path = store.paths.archive / filename

    short_fm = {
        "type": "short_summary",
        "conversation_uuid": rec.conversation_uuid,
        "source": rec.source,
        "source_id": rec.source_id,
        "first_event_at": rec.first_event_at.isoformat(),
        "last_event_at": rec.last_event_at.isoformat(),
        "summarizer": summarizer_tag,
    }
    archive_fm = {**short_fm, "type": "detailed_summary"}

    store.atomic_write(short_path, frontmatter.render(short_fm, short_body))
    store.atomic_write(archive_path, frontmatter.render(archive_fm, detailed_body))
    return 2


def _write_session_collapsed(
    store: Store,
    cfg: Config,
    summarizer: Summarizer,
    rec: SourceRecord,
) -> tuple[list[mm.Row], int]:
    """Collapsed single-pass summarize (P1.3): one call emits short +
    detailed + must-memorize. Falls back to the legacy 3-call path on a
    ``CollapseParseError`` so a malformed response never corrupts the
    store. Returns ``(must_memorize_candidates, n_calls)`` — ``n_calls``
    is 1 on success, 4 on fallback (the spent collapse call plus 3).
    """
    prompts_root = _prompts_root(cfg)
    prompt = _fill_prompt(
        prompts_root / "combined_summary.md",
        agent_name=cfg.agent.name,
        short_max_words=cfg.budgets.short_summary_words,
        detailed_max_words=cfg.budgets.detailed_summary_words,
        memo_max_words=cfg.budgets.must_memorize_memo_words,
        source=rec.source,
        source_id=rec.source_id,
        first_event_at=rec.first_event_at.isoformat(),
        last_event_at=rec.last_event_at.isoformat(),
        content=_clip(rec.content, max_chars=cfg.budgets.max_prompt_content_chars),
    )
    # One generous cap covering both bodies; per-section budgets are stated
    # in the template itself.
    max_words = (
        cfg.budgets.short_summary_words + cfg.budgets.detailed_summary_words + 100
    )
    raw = summarizer.summarize(prompt=prompt, max_words=max_words)
    try:
        short_body, detailed_body, mm_section = parse_collapsed(raw)
    except CollapseParseError:
        log.warning(
            "collapsed summary parse failed for %s; falling back to 3-call path",
            rec.conversation_uuid,
        )
        _write_short_and_archive(store, cfg, summarizer, rec)
        return _extract_must_memorize(cfg, summarizer, rec), 4
    _write_short_archive_bodies(
        store, cfg, summarizer.tag, rec, short_body, detailed_body
    )
    return mm.parse_extractor_output(mm_section), 1


def _write_addendum(
    store: Store,
    cfg: Config,
    summarizer: Summarizer,
    rec: SourceRecord,
) -> int:
    """Write a fresh short summary with addendum_of pointing at original."""
    addendum_uuid = str(uuid4())
    today_dt = datetime.now(timezone.utc)
    prompts_root = _prompts_root(cfg)
    short_prompt = _fill_prompt(
        prompts_root / "short_summary.md",
        agent_name=cfg.agent.name,
        max_words=cfg.budgets.short_summary_words,
        source=rec.source,
        source_id=rec.source_id,
        first_event_at=today_dt.isoformat(),
        last_event_at=rec.last_event_at.isoformat(),
        content=(
            f"(Addendum to frozen session {rec.conversation_uuid}.\n"
            f"Only NEW activity since the original summary should be reflected.)\n\n"
            + _clip(rec.content, max_chars=cfg.budgets.max_prompt_content_chars // 2)
        ),
    )
    short_body = summarizer.summarize(
        prompt=short_prompt, max_words=cfg.budgets.short_summary_words
    )
    filename = Store.short_filename(today_dt, addendum_uuid)
    short_path = store.paths.journal / filename
    fm = {
        "type": "short_summary",
        "conversation_uuid": addendum_uuid,
        "source": rec.source,
        "source_id": rec.source_id,
        "first_event_at": today_dt.isoformat(),
        "last_event_at": rec.last_event_at.isoformat(),
        "summarizer": summarizer.tag,
        "addendum_of": rec.conversation_uuid,
    }
    store.atomic_write(short_path, frontmatter.render(fm, short_body))
    return 1


def _extract_must_memorize(
    cfg: Config, summarizer: Summarizer, rec: SourceRecord
) -> list[mm.Row]:
    prompts_root = _prompts_root(cfg)
    prompt = _fill_prompt(
        prompts_root / "must_memorize_extract.md",
        agent_name=cfg.agent.name,
        memo_max_words=cfg.budgets.must_memorize_memo_words,
        content=_clip(rec.content, max_chars=cfg.budgets.max_prompt_content_chars // 2),
    )
    try:
        out = summarizer.summarize(
            prompt=prompt, max_words=200  # extractor outputs short
        )
    except Exception:  # noqa: BLE001
        log.exception("must_memorize extraction failed for %s", rec.conversation_uuid)
        return []
    return mm.parse_extractor_output(out)


# ----- cascade rollups (§7.3 step 3) ---------------------------------------


def _cascade_all_rollups(store: Store, cfg: Config, summarizer: Summarizer) -> None:
    # Build a map: date_str → bool (dirty?)
    _cascade_dailies(store, cfg, summarizer)
    _cascade_weeklies(store, cfg, summarizer)
    _cascade_monthlies(store, cfg, summarizer)


def _cascade_dailies(store: Store, cfg: Config, summarizer: Summarizer) -> None:
    """For each date with shorts, create/refresh daily if dirty."""
    by_date: dict[str, list[Path]] = {}
    for f in store.paths.journal.glob("*.md"):
        m = SHORT_RE.match(f.name)
        if m:
            by_date.setdefault(m.group(1), []).append(f)
    prompts_root = _prompts_root(cfg)
    for date_str, shorts in by_date.items():
        if not _daily_dirty(store, date_str, shorts):
            continue
        content_parts = []
        for s in sorted(shorts):
            content_parts.append(f"\n--- {s.name} ---\n")
            content_parts.append(s.read_text(encoding="utf-8"))
        prompt = _fill_prompt(
            prompts_root / "daily_rollup.md",
            agent_name=cfg.agent.name,
            max_words=cfg.budgets.daily_words,
            period=_format_period_date(date_str),
            n_sources=len(shorts),
            content="".join(content_parts),
        )
        try:
            body = summarizer.summarize(
                prompt=prompt, max_words=cfg.budgets.daily_words
            )
        except Exception:
            log.exception("daily rollup failed for %s", date_str)
            continue
        # Use deterministic per-date uuid5 so re-roll keeps the same filename.
        rollup_uuid = str(uuid5(NAMESPACE_URL, f"daily:{date_str}"))
        target = store.paths.journal / Store.daily_filename(date_str, rollup_uuid)
        fm = {
            "type": "daily_rollup",
            "period": _format_period_date(date_str),
            "summarizer": summarizer.tag,
        }
        store.atomic_write(target, frontmatter.render(fm, body))


def _daily_dirty(store: Store, date_str: str, shorts: list[Path]) -> bool:
    daily = store.daily_for_date(date_str)
    if daily is None:
        return True
    daily_mtime = daily.stat().st_mtime
    return any(s.stat().st_mtime > daily_mtime for s in shorts)


def _cascade_weeklies(store: Store, cfg: Config, summarizer: Summarizer) -> None:
    """For each Mon-week containing a dirty daily, refresh the weekly."""
    by_monday: dict[str, list[Path]] = {}
    for f in store.paths.journal.glob("*.md"):
        m = DAILY_RE.match(f.name)
        if not m:
            continue
        d = date.fromisoformat(_iso_from_yyyymmdd(m.group(1)))
        monday = d - timedelta(days=d.weekday())
        by_monday.setdefault(monday.strftime("%Y%m%d"), []).append(f)
    prompts_root = _prompts_root(cfg)
    for monday_str, dailies in by_monday.items():
        if not _weekly_dirty(store, monday_str, dailies):
            continue
        content_parts = []
        for d in sorted(dailies):
            content_parts.append(f"\n--- {d.name} ---\n")
            content_parts.append(d.read_text(encoding="utf-8"))
        prompt = _fill_prompt(
            prompts_root / "weekly_rollup.md",
            agent_name=cfg.agent.name,
            max_words=cfg.budgets.weekly_words,
            period=_format_period_date(monday_str),
            n_sources=len(dailies),
            content="".join(content_parts),
        )
        try:
            body = summarizer.summarize(
                prompt=prompt, max_words=cfg.budgets.weekly_words
            )
        except Exception:
            log.exception("weekly rollup failed for %s", monday_str)
            continue
        rollup_uuid = str(uuid5(NAMESPACE_URL, f"weekly:{monday_str}"))
        target = store.paths.journal / Store.weekly_filename(monday_str, rollup_uuid)
        fm = {
            "type": "weekly_rollup",
            "period": _format_period_date(monday_str),
            "summarizer": summarizer.tag,
        }
        store.atomic_write(target, frontmatter.render(fm, body))


def _weekly_dirty(store: Store, monday_str: str, dailies: list[Path]) -> bool:
    weekly = store.weekly_for_monday(monday_str)
    if weekly is None:
        return True
    weekly_mtime = weekly.stat().st_mtime
    return any(d.stat().st_mtime > weekly_mtime for d in dailies)


def _cascade_monthlies(store: Store, cfg: Config, summarizer: Summarizer) -> None:
    """For each month containing a dirty weekly, refresh the monthly."""
    by_month: dict[str, list[Path]] = {}
    for f in store.paths.journal.glob("*.md"):
        m = WEEKLY_RE.match(f.name)
        if not m:
            continue
        d = date.fromisoformat(_iso_from_yyyymmdd(m.group(1)))
        key = d.strftime("%Y%m")
        by_month.setdefault(key, []).append(f)
    prompts_root = _prompts_root(cfg)
    for yyyymm, weeklies in by_month.items():
        if not _monthly_dirty(store, yyyymm, weeklies):
            continue
        content_parts = []
        for w in sorted(weeklies):
            content_parts.append(f"\n--- {w.name} ---\n")
            content_parts.append(w.read_text(encoding="utf-8"))
        prompt = _fill_prompt(
            prompts_root / "monthly_rollup.md",
            agent_name=cfg.agent.name,
            max_words=cfg.budgets.monthly_words,
            period=f"{yyyymm[:4]}-{yyyymm[4:]}",
            n_sources=len(weeklies),
            content="".join(content_parts),
        )
        try:
            body = summarizer.summarize(
                prompt=prompt, max_words=cfg.budgets.monthly_words
            )
        except Exception:
            log.exception("monthly rollup failed for %s", yyyymm)
            continue
        rollup_uuid = str(uuid5(NAMESPACE_URL, f"monthly:{yyyymm}"))
        target = store.paths.journal / Store.monthly_filename(yyyymm, rollup_uuid)
        fm = {
            "type": "monthly_rollup",
            "period": f"{yyyymm[:4]}-{yyyymm[4:]}",
            "summarizer": summarizer.tag,
        }
        store.atomic_write(target, frontmatter.render(fm, body))


def _monthly_dirty(store: Store, yyyymm: str, weeklies: list[Path]) -> bool:
    monthly = store.monthly_for_yyyymm(yyyymm)
    if monthly is None:
        return True
    monthly_mtime = monthly.stat().st_mtime
    return any(w.stat().st_mtime > monthly_mtime for w in weeklies)


# ----- longer-memory refresh (§7.3 step 3b) --------------------------------


def _refresh_longer_memory(
    store: Store, cfg: Config, summarizer: Summarizer
) -> None:
    """Fold any monthly older than monthlies window that isn't yet folded."""
    cutoff = date.today() - timedelta(
        days=cfg.briefing.walking.monthlies_working_days
    )
    monthlies_to_fold: list[Path] = []
    for f in sorted(store.paths.journal.glob("*.md")):
        m = MONTHLY_RE.match(f.name)
        if not m:
            continue
        yyyymm = m.group(1)
        period_first = date(int(yyyymm[:4]), int(yyyymm[4:]), 1)
        if period_first >= cutoff:
            continue
        fm = frontmatter.read_frontmatter(f)
        if fm.get("folded_into_longer_memory"):
            continue
        monthlies_to_fold.append(f)
    if not monthlies_to_fold:
        return

    prompts_root = _prompts_root(cfg)
    longer_path = store.paths.journal / "longer_memory.md"
    if longer_path.exists():
        prev_fm, prev_body = frontmatter.parse(longer_path.read_text())
        prev_covers = prev_fm.get("covers_until", "")
    else:
        prev_body = ""
        prev_covers = ""

    for monthly_path in monthlies_to_fold:
        fm_existing = frontmatter.read_frontmatter(monthly_path)
        period = fm_existing.get("period", "")
        prompt = _fill_prompt(
            prompts_root / "longer_memory.md",
            agent_name=cfg.agent.name,
            max_words=cfg.budgets.longer_memory_words,
            previous_covers_until=prev_covers or "(empty)",
            previous_longer_memory=prev_body or "(empty)",
            new_month=period,
            new_monthly=monthly_path.read_text(encoding="utf-8"),
        )
        try:
            body = summarizer.summarize(
                prompt=prompt, max_words=cfg.budgets.longer_memory_words
            )
        except Exception:
            log.exception("longer_memory fold failed for %s", monthly_path.name)
            continue
        new_longer_fm = {
            "type": "longer_memory",
            "covers_until": period,
            "last_refreshed_at": iso_now(),
            "summarizer": summarizer.tag,
        }
        store.atomic_write(
            longer_path, frontmatter.render(new_longer_fm, body)
        )
        # Mark monthly as folded (rewrite frontmatter).
        m_text = monthly_path.read_text(encoding="utf-8")
        m_fm, m_body = frontmatter.parse(m_text)
        m_fm["folded_into_longer_memory"] = iso_now()
        store.atomic_write(monthly_path, frontmatter.render(m_fm, m_body))

        prev_body = body
        prev_covers = period


# ----- decay (§7.3 step 4) -------------------------------------------------


def _apply_decay(store: Store, cfg: Config) -> None:
    rows = mm.load(store)
    if not rows:
        return
    today = datetime.now(timezone.utc).date().isoformat()
    kept = mm.decay_all(
        rows,
        today=today,
        days_per_point={
            mm.KIND_PREFERENCE: cfg.decay.preference_days_per_point,
            mm.KIND_DECISION: cfg.decay.decision_days_per_point,
            mm.KIND_INCIDENT: cfg.decay.incident_days_per_point,
        },
    )
    mm.save(store, kept)


# ----- state writer --------------------------------------------------------


def _write_state(
    store: Store,
    cfg: Config,
    *,
    decisions: list[Decision],
    cost_usd: float,
    duration_sec: float | None = None,
    last_op: str = "rebuild",
    metrics: RebuildMetrics | None = None,
) -> None:
    counts = {"active": 0, "clean": 0, "dirty": 0, "frozen": 0}
    for d in decisions:
        if d.action == SKIP_ACTIVE:
            counts["active"] += 1
        elif d.action == SKIP_CLEAN:
            counts["clean"] += 1
        elif d.action == ADDENDUM:
            counts["frozen"] += 1
        else:
            counts["dirty"] += 1

    # Preserve sticky fields from previous state — e.g., bootstrap cost
    # should survive subsequent rebuilds. Running total accumulates.
    prev = store.read_state() or {}
    bootstrap_cost = (
        cost_usd if last_op == "bootstrap"
        else prev.get("last_bootstrap_cost_usd")
    )
    prev_total = prev.get("total_cost_usd") or 0.0

    payload = {
        "agent": cfg.agent.name,
        "last_rebuild_at": iso_now(),
        "last_op": last_op,
        "last_rebuild_duration_sec": duration_sec,
        "sessions": counts,
        "last_rebuild_cost_usd": cost_usd,           # this op only
        "total_cost_usd": prev_total + cost_usd,     # running total since bootstrap
        "last_bootstrap_cost_usd": bootstrap_cost,
    }
    if metrics is not None:
        payload["metrics"] = metrics.as_dict()
    store.write_state(payload)


# ----- auto-memory synthetic conv (§11 step 3) -----------------------------


def _auto_memory_record(cfg: Config) -> SourceRecord | None:
    """Build a SourceRecord by concatenating the legacy auto-memory dir.

    Only runs if a source of kind ``auto_memory`` is configured with a
    ``path:`` field. Tests pointing at isolated configs naturally skip
    this. Returns None if the directory is missing or empty.
    """
    mem_dir: Path | None = None
    for s in cfg.sources:
        if s.kind == "auto_memory":
            mem_dir = Path(s.fields.get("path", "")).expanduser()
            break
    if mem_dir is None or not mem_dir.exists() or not mem_dir.is_dir():
        return None
    parts: list[str] = []
    for f in sorted(mem_dir.glob("*.md")):
        try:
            parts.append(f"\n--- {f.name} ---\n{f.read_text(encoding='utf-8')}")
        except OSError:
            continue
    if not parts:
        return None
    content = "".join(parts)
    now = datetime.now(timezone.utc)
    uid = str(uuid5(NAMESPACE_URL, f"auto_memory:{cfg.agent.name}"))
    return SourceRecord(
        conversation_uuid=uid,
        source="doc",
        source_id=f"auto_memory:{cfg.agent.name}",
        first_event_at=now,
        last_event_at=now,
        activity_mtime=time.time() - 86400 * 365,  # ancient → idle
        content=content,
        raw_path=mem_dir,
    )


# ----- prompt loading + filling --------------------------------------------


def _prompts_root(cfg: Config) -> Path:
    here = Path(__file__).parent / "summarizers" / "prompts" / cfg.summarizer.prompts
    if here.exists():
        return here
    # Fallback: cwd-relative (allows future overrides).
    return Path("summarizers/prompts") / cfg.summarizer.prompts


def _fill_prompt(path: Path, **kwargs) -> str:
    template = path.read_text(encoding="utf-8")
    # Use simple .format() with a SafeDict so missing keys don't crash.
    return template.format_map(_SafeFormatDict(kwargs))


class _SafeFormatDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + "\n\n[...content elided...]\n\n" + text[-half:]


# ----- cost estimation -----------------------------------------------------


def _approx_cost(
    cfg: Config,
    input_text: str,
    *,
    n_short: int = 1,
    n_detailed: int = 1,
) -> float:
    """Heuristic char→token cost estimate for dry-runs.

    Each summarizer call (short, detailed, extractor) consumes the full
    input text as its prompt context. Output sizes differ by call type:
        short    ≈ short_summary_words * 1.3 tokens
        detailed ≈ detailed_summary_words * 1.3 tokens
        extractor ≈ ~65 tokens (just the KIND/MEMO blocks)

    For SUMMARIZE_NEW / RE_SUMMARIZE: short + detailed + extractor = 3 calls.
    For ADDENDUM: short + extractor = 2 calls.
    """
    input_tokens_per_call = max(1, len(input_text) // 4)
    n_extractor = 1  # always one extractor call per session
    n_calls = n_short + n_detailed + n_extractor

    output_tokens = int(
        n_short * cfg.budgets.short_summary_words * 1.3
        + n_detailed * cfg.budgets.detailed_summary_words * 1.3
        + n_extractor * 65
    )
    summ = AnthropicSummarizer(model=cfg.summarizer.model)
    return summ.cost_estimate_usd(
        prompt_tokens=input_tokens_per_call * n_calls,
        output_tokens=output_tokens,
    )


def _estimate_total_cost(
    cfg: Config, n_to_process: int, sample_records: list[SourceRecord]
) -> float:
    if not sample_records:
        return 0.0
    sample = sample_records[: min(5, len(sample_records))]
    per_session_cost = sum(
        _approx_cost(cfg, r.content, n_short=1, n_detailed=1)
        for r in sample
    ) / len(sample)
    return per_session_cost * n_to_process


# ----- background spawn ----------------------------------------------------


def _spawn_background() -> int:
    """Spawn the current process detached and exit."""
    env = os.environ.copy()
    env["TIGER_MEMORY_BACKGROUND_SPAWNED"] = "1"
    argv = [sys.executable, "-m", "tigerharness.tiger_memory.cli", "rebuild"]
    if "--config" in sys.argv:
        idx = sys.argv.index("--config")
        argv = [sys.executable, "-m", "tigerharness.tiger_memory.cli", "--config",
                sys.argv[idx + 1], "rebuild"]
    log_path = Path("/tmp/tiger-memory.background.log")
    with open(log_path, "ab") as out:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL, stdout=out, stderr=out,
            env=env, start_new_session=True,
        )
    return 0


# ----- date helpers --------------------------------------------------------


def _iso_from_yyyymmdd(s: str) -> str:
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def _format_period_date(yyyymmdd: str) -> str:
    return _iso_from_yyyymmdd(yyyymmdd)

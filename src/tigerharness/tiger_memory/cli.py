"""tiger-memory CLI entrypoint (topic-store revamp, ADR 0007).

Subcommands:

    Writers:
        init          create empty store, validate config
        rebuild       fresh-start: drop the retired surface, regenerate the
                      session-start briefing (indexes + detail files + notice)
        pin           write a must_remember entry directly
        migrate-to-topics  one-off (idempotent): retire diary/fuzzy files to
                      <root>/retired/ and create the topics store

    Readers (lockless):
        state         show JSON state snapshot for the three bounded stores

    In-session sub-agent executor (subscription rail, non-AI glue):
        plan          stage one extraction prompt per idle, unprocessed
                      transcript; print the work manifest
        ingest-extraction   write back one sub-agent's extraction bundle
                            (stdin) for a planned conversation uuid
        build-reduce-prompts  reduce step: assemble <uuid>.prompt.md from a
                            map_reduce item's staged chunk digests (ADR 0006 Pt1)
        ingest-staged       ingest every staged <uuid>.extract.md card in ONE
                            process (per-persona merge serialized by construction)
        compact-plan        stage one compaction prompt per over-bound surface
                            (index / detail / must_remember); print the manifest
        compact-apply       validate + apply every staged compaction card in
                            ONE process (deterministic convergence fallback)
        card-check          measure ONE staged card draft against its
                            apply-time bound (the card author's ruler;
                            covers compaction + team-events fold cards)
        team-events-compact-plan   stage one fold prompt per aged-out
                            team-event-log period (ADR 0008; team-level)
        team-events-compact-apply  validate + apply every staged team-events
                            fold card in ONE process (deterministic trim)

    Team-sweep gating (B3, non-AI -- drive the in-session protocol):
        sweep-plan      claim the team sweep; print targets for this wake
        sweep-done      mark one persona done in the in-flight run
        sweep-complete  advance the watermark + end the run
        sweep-release   drop the claim without advancing the watermark

    Operator read/fix loop (practicality audit; see inspect_tools.py):
        search        substring search across the journal stores (+ team
                      event log); --team widens to every roster persona
        forget        operator-authority removal of one entry (audited to
                      <store>.forgotten.md, never silent)
        doctor        team-wide health table; exit 1 when anything is
                      flagged (cron-alertable)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import Config, ConfigError, load_config
from .store import Store

log = logging.getLogger("tigerharness.tiger_memory.cli")


def main(argv: list[str] | None = None) -> int:
    from tigerharness._logging import configure_cli_logging
    configure_cli_logging()
    parser = argparse.ArgumentParser(
        prog="tiger-memory",
        description="Agent-agnostic conversation memory module.",
    )
    parser.add_argument(
        "--config",
        help="Path to tiger-memory.config.yaml (overrides TIGER_MEMORY_CONFIG).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # ----- writers -----
    sub.add_parser("init", help="Create empty store + validate config.")

    sub.add_parser(
        "rebuild",
        help="Fresh-start: drop the retired surface, regenerate the "
             "session-start briefing (skill index + stores + notice).",
    )

    p_pin = sub.add_parser("pin", help="Write a must_remember entry directly.")
    p_pin.add_argument("memo", help="Memo text.")
    p_pin.add_argument(
        "--kind",
        choices=["operator_explicit", "preference", "decision", "incident"],
        # preference, NOT operator_explicit: the old default made every
        # casual pin a maximally-protected directive that nothing could
        # remove (practicality audit: one live store is 17/17
        # operator_explicit). Protection now requires saying so.
        default="preference",
    )

    p_mig = sub.add_parser(
        "migrate-to-topics",
        help="One-off (idempotent): retire diary/fuzzy journal files to "
             "<root>/retired/ and create the topics store (ADR 0007).",
    )
    p_mig.add_argument(
        "--apply", action="store_true",
        help="Perform it (move files, create topics.md). "
             "Default: dry-run (preview only, nothing written).",
    )

    # ----- readers -----
    sub.add_parser("state", help="Print JSON state snapshot for the 3 stores.")

    # ----- in-session sub-agent executor (plan / ingest) -----
    p_plan = sub.add_parser(
        "plan",
        help="Stage one extraction prompt per idle, unprocessed transcript "
             "and print the work manifest (non-AI).",
    )
    p_plan.add_argument("--max-sessions", type=int, default=None,
                        help="Cap on transcripts staged this plan.")

    p_ing = sub.add_parser(
        "ingest-extraction",
        help="Write back one sub-agent's extraction bundle (stdin) for a "
             "planned conversation uuid.",
    )
    p_ing.add_argument("--uuid", required=True,
                       help="conversation_uuid from the plan manifest.")

    sub.add_parser(
        "build-reduce-prompts",
        help="Reduce step (ADR 0006 Part 1): for every map_reduce item whose "
             "chunk digests are all staged, assemble its <uuid>.prompt.md from "
             "the digests so the normal extract + ingest flow finishes it.",
    )

    sub.add_parser(
        "ingest-staged",
        help="Glue: ingest every staged <uuid>.extract.md card in ONE process "
             "(per-persona merge serialized by construction -- no race).",
    )

    sub.add_parser(
        "compact-plan",
        help="Stage one compaction prompt per over-bound surface (non-AI; "
             "runs the deterministic stale-topic forget first).",
    )

    sub.add_parser(
        "compact-apply",
        help="Validate + apply every staged compaction card in ONE process "
             "(deterministic convergence fallback; no silent oversize).",
    )

    p_cc = sub.add_parser(
        "card-check",
        help="Measure ONE staged card draft against the bound its apply "
             "will enforce (the card author's ruler; read-only, non-AI). "
             "Covers compaction cards AND team-events fold cards. "
             "Exit: 0 fits, 4 over-bound, 1 malformed card, 2 no card / "
             "no manifest / not a staged target.",
    )
    p_cc.add_argument(
        "card",
        help="Path to the card draft — must be the manifest's exact "
             "card_path (write the draft there, then check it).",
    )

    p_tep = sub.add_parser(
        "team-events-compact-plan",
        help="Stage one fold prompt per aged-out team-event period "
             "(non-AI; runs the deterministic size backstop first).",
    )
    p_tep.add_argument("--now", default=None,
                       help="ISO timestamp override (testing/determinism).")

    sub.add_parser(
        "team-events-compact-apply",
        help="Validate + apply every staged team-events fold card in ONE "
             "process (deterministic trim; snapshot-surviving).",
    )

    # ----- B3 team-sweep gating convenience CLIs (non-AI) -----
    p_swp = sub.add_parser(
        "sweep-plan",
        help="Try to claim the team sweep at session bootstrap; print the "
             "decision + roster targets to process this wake (non-AI).",
    )
    p_swp.add_argument("--token", default=None,
                       help="Stable claim token (default: random uuid).")
    p_swp.add_argument("--max-personas", type=int, default=None,
                       help="Per-wake cap on personas (default: the config's "
                            "sweep.max_personas, package default 3).")
    p_swp.add_argument("--floor-hours", type=float, default=None,
                       help="Staleness floor override (default: the config's "
                            "sweep.floor_hours, package default 24).")
    p_swp.add_argument("--lease-seconds", type=float, default=None,
                       help="Soft-lease seconds before a claim is stealable "
                            "(default: the config's sweep.lease_seconds).")
    p_swp.add_argument("--now", default=None,
                       help="ISO timestamp override (testing/determinism).")
    p_swp.add_argument("--own-persona", default=None,
                       help="Split gate: the calling session's persona (must "
                            "be the persona this --config belongs to). Its "
                            "completed-but-un-swept sources bypass the "
                            "staleness floor; other personas keep it.")
    p_swp.add_argument("--exclude-session", default=None,
                       help="The calling session's conversation uuid — "
                            "excluded from the own-persona pending check so "
                            "a live session never counts itself.")

    p_swd = sub.add_parser(
        "sweep-done", help="Mark one persona completed in the in-flight run.")
    p_swd.add_argument("--persona", required=True,
                       help="Persona name that was just processed.")

    p_swc = sub.add_parser(
        "sweep-complete",
        help="Advance the team watermark + end the run (all due personas done).")
    p_swc.add_argument("--now", default=None,
                       help="ISO timestamp override (testing/determinism).")
    p_swc.add_argument("--token", default=None,
                       help="Claim token from sweep-plan; refused (exit 3) if "
                            "another session now owns the claim.")
    p_swc.add_argument("--force", action="store_true",
                       help="Complete even though roster personas were not "
                            "recorded done this run (normally refused, exit 3).")

    p_swr = sub.add_parser(
        "sweep-release",
        help="Drop the claim WITHOUT advancing the watermark (per-wake cap hit).")
    p_swr.add_argument("--token", default=None,
                       help="Claim token from sweep-plan; refused (exit 3) if "
                            "another session now owns the claim.")

    p_chk = sub.add_parser(
        "check",
        help="Validate the 3 stores' format; exit non-zero if any are invalid.")
    p_chk.add_argument(
        "--fix", action="store_true",
        help="Repair mechanical drift + quarantine non-mechanical to "
             "<store>.rejected.md (no silent loss).")

    # ----- operator read/fix loop (practicality audit) -----
    p_srch = sub.add_parser(
        "search",
        help="Case-insensitive substring search across the journal stores "
             "(+ the team event log). Exit 0 even on zero matches.",
    )
    p_srch.add_argument("term", help="Substring to look for.")
    p_srch.add_argument(
        "--team", action="store_true",
        help="Search every roster persona's stores, not just this config's.",
    )
    p_srch.add_argument(
        "--store",
        choices=["skills", "must_remember", "topics", "events"],
        default=None,
        help="Restrict to one store (default: all three + events).",
    )

    p_fgt = sub.add_parser(
        "forget",
        help="Operator-authority removal of ONE entry; the removed entry is "
             "appended to <store>.forgotten.md first (never silent).",
    )
    p_fgt.add_argument(
        "--store", required=True,
        choices=["skills", "must_remember", "topics"],
        help="Which journal store to remove from.",
    )
    grp = p_fgt.add_mutually_exclusive_group(required=True)
    grp.add_argument("--id", help="Entry id (any store).")
    grp.add_argument("--slug", help="Topic slug (topics store only).")

    p_doc = sub.add_parser(
        "doctor",
        help="Team-wide memory health table (bounds, staging, rejects, "
             "freshness, topic-slug collisions); exit 1 if anything is "
             "flagged.",
    )
    p_doc.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Print the full JSON structure instead of the table.",
    )

    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    store = Store(cfg.store.root)

    if args.cmd == "init":
        return _cmd_init(cfg, store)
    if args.cmd == "rebuild":
        from .lifecycle import rebuild
        return rebuild(cfg, store)
    if args.cmd == "pin":
        from .lifecycle import pin as pin_cmd
        return pin_cmd(cfg, store, memo=args.memo, kind=args.kind)
    if args.cmd == "migrate-to-topics":
        return _cmd_migrate_topics(cfg, store, apply=args.apply)
    if args.cmd == "state":
        return _cmd_state(cfg, store)
    if args.cmd == "plan":
        return _cmd_plan(cfg, store, args.max_sessions)
    if args.cmd == "ingest-extraction":
        return _cmd_ingest_extraction(cfg, store, args.uuid)
    if args.cmd == "build-reduce-prompts":
        return _cmd_build_reduce_prompts(cfg, store)
    if args.cmd == "ingest-staged":
        return _cmd_ingest_staged(cfg, store)
    if args.cmd == "compact-plan":
        return _cmd_compact_plan(cfg, store)
    if args.cmd == "compact-apply":
        return _cmd_compact_apply(cfg, store)
    if args.cmd == "card-check":
        return _cmd_card_check(cfg, store, args.card)
    if args.cmd == "team-events-compact-plan":
        return _cmd_team_events_compact_plan(cfg, now=args.now)
    if args.cmd == "team-events-compact-apply":
        return _cmd_team_events_compact_apply(cfg)
    if args.cmd == "sweep-plan":
        return _cmd_sweep_plan(
            cfg, store, token=args.token, max_personas=args.max_personas,
            floor_hours=args.floor_hours, lease_seconds=args.lease_seconds,
            now=args.now, own_persona=args.own_persona,
            exclude_session=args.exclude_session,
        )
    if args.cmd == "sweep-done":
        return _cmd_sweep_done(cfg, args.persona)
    if args.cmd == "sweep-complete":
        return _cmd_sweep_complete(
            cfg, now=args.now, token=args.token, force=args.force
        )
    if args.cmd == "sweep-release":
        return _cmd_sweep_release(cfg, token=args.token)
    if args.cmd == "check":
        return _cmd_check(cfg, store, fix=args.fix)
    if args.cmd == "search":
        return _cmd_search(cfg, args.term, team=args.team,
                           store_filter=args.store)
    if args.cmd == "forget":
        return _cmd_forget(cfg, store, store_name=args.store,
                           entry_id=args.id, slug=args.slug)
    if args.cmd == "doctor":  # pragma: no branch  # exhaustive
        return _cmd_doctor(cfg, as_json=args.as_json)
    return 2  # pragma: no cover  # argparse rejects unknown subcommands first


def _cmd_init(cfg: Config, store: Store) -> int:
    store.init_layout()
    # Render the (empty) briefing immediately: a new persona's prompt says
    # "read briefing/README.md" from its very first session — without this,
    # that instruction points at a missing file until the first team sweep
    # happens to reach them (practicality audit S3).
    from .briefing import rebuild_briefing
    rebuild_briefing(cfg, store)
    print(f"tiger-memory store initialised at: {store.root}")
    print(f"  journal/   {store.paths.journal}")
    print(f"  briefing/  {store.paths.briefing}")
    print(f"Agent: {cfg.agent.name}")
    print(f"Sources: {[s.kind for s in cfg.sources]}")
    print(f"Summarizer: {cfg.summarizer.backend}:{cfg.summarizer.model} "
          f"(prompts {cfg.summarizer.prompts})")
    return 0


def _cmd_state(cfg: Config, store: Store) -> int:
    from .state import compute_state
    print(json.dumps(compute_state(cfg, store), indent=2, sort_keys=True))
    return 0


def _cmd_check(cfg: Config, store: Store, *, fix: bool) -> int:
    """Validate (``--fix``: repair + quarantine) the 3 stores' on-disk format.

    Exit 0 when clean (or after ``--fix`` left every store valid); exit 1 on
    any problem WITHOUT ``--fix`` — so CI / the sweep gate fails on malformed
    memory rather than letting it persist.
    """
    from .check import check_all
    report = check_all(cfg, store, fix=fix)
    print(json.dumps({
        "ok": report.ok,
        "fixed": fix,
        "stores": [
            {
                "store": s.store_name, "valid": s.valid, "problems": s.problems,
                "quarantined": s.quarantined, "repaired": s.repaired,
            }
            for s in report.stores
        ],
    }, indent=2))
    if fix:
        return 0
    return 0 if report.ok else 1


def _cmd_migrate_topics(cfg: Config, store: Store, *, apply: bool) -> int:
    """Retire diary/fuzzy files + create the topics store (dry-run unless
    ``--apply``). Idempotent; exit 0 always (a no-op re-run is success)."""
    from .migrate_topics import migrate_store
    res = migrate_store(cfg, store, apply=apply)
    print(json.dumps(res.to_dict(), indent=2))
    return 0


def _cmd_compact_plan(cfg: Config, store: Store) -> int:
    """Stage compaction prompts for every over-bound surface (non-AI)."""
    from .compaction import compact_plan
    manifest = compact_plan(cfg, store)
    print(json.dumps(manifest, indent=2))
    return 0


def _cmd_compact_apply(cfg: Config, store: Store) -> int:
    """Apply every staged compaction card in one process.

    Exit: 0 = clean (some cards may be skipped-no-card; they simply re-stage
    next sweep); 1 = ≥1 malformed card; 2 = no compaction manifest.
    """
    from .compaction import compact_apply
    try:
        report = compact_apply(cfg, store)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report.to_dict(), indent=2))
    return 1 if report.malformed else 0


def _cmd_card_check(cfg: Config, store: Store, card: str) -> int:
    """Measure one staged card draft against its apply-time bound.

    The card author's ruler (read-only, deterministic). Exit: 0 = the
    card fits its bound untrimmed; 4 = valid card, over-bound (tighten
    and re-check); 1 = malformed card (the same error apply would raise);
    2 = no card file / no manifest / card is not a staged target.
    """
    from .compaction import CompactionParseError, card_check
    from .entries import EntryError
    from .team_events import TeamEventsError
    try:
        result = card_check(cfg, store, Path(card))
    except (FileNotFoundError, LookupError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (CompactionParseError, TeamEventsError, EntryError) as exc:
        print(f"malformed card: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result["fits"] else 4


def _cmd_team_events_compact_plan(cfg: Config, *, now: str | None) -> int:
    """Stage team-event-log fold prompts (ADR 0008, non-AI, team-level)."""
    from .team_events import compact_plan
    manifest = compact_plan(cfg, now=now)
    print(json.dumps(manifest, indent=2))
    return 0


def _cmd_team_events_compact_apply(cfg: Config) -> int:
    """Apply every staged team-events fold card in one process.

    Exit: 0 = clean; 1 = ≥1 malformed card (kept; the fold re-stages next
    sweep); 2 = no team-events manifest.
    """
    from .team_events import compact_apply
    try:
        report = compact_apply(cfg)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 1 if report["malformed"] else 0


def _cmd_plan(cfg: Config, store: Store, max_sessions: int | None) -> int:
    from .lifecycle import _sweep_staging_dir, plan_extraction
    plan_extraction(cfg, store, max_sessions=max_sessions)
    manifest_path = _sweep_staging_dir(store) / "manifest.json"
    print(manifest_path.read_text(encoding="utf-8").rstrip("\n"))
    return 0


def _load_plan_manifest(store: Store) -> dict | None:
    from .lifecycle import _sweep_staging_dir
    manifest_path = _sweep_staging_dir(store) / "manifest.json"
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print(f"no plan manifest at {manifest_path}; run `tiger-memory plan` "
              f"first", file=sys.stderr)
        return None


def _ingest_item_bundle(cfg: Config, store: Store, item: dict, bundle_text: str):
    """Parse + ingest one extraction bundle for a manifest *item*, then drop the
    now-consumed staged prompt. Raises ``ExtractionParseError`` on a malformed
    bundle BEFORE any write, so the store + prompt are left intact."""
    from .executor import ingest_extraction
    result = ingest_extraction(
        store, cfg,
        conversation_uuid=item["conversation_uuid"],
        source=item["source"],
        bundle_text=bundle_text,
        # Source-dated ingest: everything this bundle produces (entry
        # freshness, topic section headings, team-event days) is dated by
        # when the session ENDED, not when the sweep ran — a backlog sweep
        # must not stamp weeks-old work "today".
        source_last_event_at=item.get("last_event_at") or None,
    )
    # ADR 0006 Part 2: advance the session's high-water-mark cursor — but ONLY
    # after the card above ingested cleanly (ingest raises before this on a
    # malformed bundle, leaving the cursor untouched → a re-stage next sweep).
    # Ordering-, not transaction-, protected: a crash here re-processes the same
    # slice next sweep (idempotent), never skips it.
    cursor_event_at = item.get("cursor_event_at")
    if cursor_event_at is not None:
        from .cursor import on_slice_ingested
        on_slice_ingested(
            store, item["conversation_uuid"],
            slice_end_event_at=cursor_event_at,
            processed_events=int(item.get("cursor_events", 0)),
        )
    # The staged prompt(s) embed the (prefiltered) transcript; once ingested
    # they are consumed, so drop them to avoid leaving transcript content at
    # rest. A "single" item has one prompt_path; a "map_reduce" item has chunk
    # prompts + their digests (ADR 0006 Part 1) — plus the reduce-built
    # <uuid>.prompt.md, which the manifest does NOT carry (it is written by
    # build-reduce-prompts after planning), so unlink it by construction or
    # digest-derived content stays at rest until the next plan (audit:
    # pipeline finding 8 / drift finding 8).
    from .lifecycle import _sweep_staging_dir
    reduce_prompt = (
        _sweep_staging_dir(store) / f"{item['conversation_uuid']}.prompt.md"
    )
    for staged in [item.get("prompt_path"), str(reduce_prompt),
                   *item.get("chunk_prompts", []),
                   *item.get("digest_paths", [])]:
        if staged:
            Path(staged).unlink(missing_ok=True)
    return result


def _cmd_ingest_extraction(cfg: Config, store: Store, uuid: str) -> int:
    from .lifecycle import ExtractionParseError
    manifest = _load_plan_manifest(store)
    if manifest is None:
        return 2
    item = next(
        (it for it in manifest.get("items", [])
         if it.get("conversation_uuid") == uuid),
        None,
    )
    if item is None:
        print(f"uuid {uuid} not in plan manifest", file=sys.stderr)
        return 2
    bundle = sys.stdin.read()
    from .bounded_store import StoreLockHeld
    try:
        result = _ingest_item_bundle(cfg, store, item, bundle)
    except ExtractionParseError as exc:
        print(f"malformed extraction bundle for {uuid}: {exc}", file=sys.stderr)
        return 1
    except StoreLockHeld as exc:
        print(f"store locked for {uuid}: {exc} — retry shortly "
              "(cursor untouched)", file=sys.stderr)
        return 1
    print(f"ingested {result.conversation_uuid} (+{result.total_added} entries: "
          f"{result.skills_added} skills, {result.must_remember_added} "
          f"must_remember, {result.topics_added} topic detail(s); "
          f"{result.touched} touched)")
    return 0


def _cmd_build_reduce_prompts(cfg: Config, store: Store) -> int:
    """Reduce step (ADR 0006 Part 1): for every ``map_reduce`` manifest item
    whose chunk digests are all staged, assemble its ``<uuid>.prompt.md`` from
    the digests (the single-sourced extract contract over the concatenated
    digests) so the normal extraction sub-agent + ``ingest-staged`` flow can
    finish it exactly like a single-prompt item. An item still missing a digest
    is reported ``pending`` and retried on a later sweep pass.

    Exit: 0 always (a pending item is not an error); 2 = no plan manifest.
    """
    from .lifecycle import build_reduce_prompt
    manifest = _load_plan_manifest(store)
    if manifest is None:
        return 2
    built: list[str] = []
    pending: list[str] = []
    for item in manifest.get("items", []):
        if item.get("kind") != "map_reduce":
            continue
        uuid = item.get("conversation_uuid")
        if build_reduce_prompt(cfg, store, item) is None:
            pending.append(uuid)
        else:
            built.append(uuid)
    print(json.dumps({"built": built, "pending": pending}, indent=2))
    return 0


def _cmd_ingest_staged(cfg: Config, store: Store) -> int:
    """Ingest every staged ``<uuid>.extract.md`` card in ONE process, so the
    per-persona merge is serialized by construction — no race. A malformed card
    is left in place and reported. An item with no card yet is skipped.

    Exit: 0 = no malformed cards; 1 = ≥1 malformed card; 2 = no plan manifest.
    """
    from .bounded_store import StoreLockHeld
    from .lifecycle import ExtractionParseError, _sweep_card_path
    manifest = _load_plan_manifest(store)
    if manifest is None:
        return 2
    ingested: list[str] = []
    malformed: list[str] = []
    locked: list[str] = []
    skipped_no_card = 0
    for item in manifest.get("items", []):
        uuid = item.get("conversation_uuid")
        card_path = _sweep_card_path(store, uuid)
        try:
            bundle = card_path.read_text(encoding="utf-8")
        except OSError:
            skipped_no_card += 1
            continue
        try:
            result = _ingest_item_bundle(cfg, store, item, bundle)
        except ExtractionParseError as exc:
            print(f"malformed extraction card for {uuid}: {exc}", file=sys.stderr)
            malformed.append(uuid)
            continue
        except StoreLockHeld as exc:
            # A stuck lock holder outlasted the ingest wait: keep the card
            # and the cursor untouched — this uuid re-ingests next sweep
            # (audit F2/F7 shape: never abort the whole glue mid-run).
            print(f"store locked for {uuid}: {exc}", file=sys.stderr)
            locked.append(uuid)
            continue
        card_path.unlink(missing_ok=True)
        ingested.append(result.conversation_uuid)
    if not ingested and (skipped_no_card or malformed or locked):
        # A completed fan-out that ingested NOTHING is an anomaly (misnamed
        # cards, premature glue, stuck locks) — say so loudly instead of
        # exiting 0 in silence (audit: drift finding 5; the Operator's
        # "verify ingested>0" incident).
        print(
            "warning: nothing ingested "
            f"({skipped_no_card} no-card, {len(malformed)} malformed, "
            f"{len(locked)} locked) — check the staging dir before moving on",
            file=sys.stderr,
        )
    summary = {
        "ingested": len(ingested),
        "malformed": malformed,
        "locked": locked,
        "skipped_no_card": skipped_no_card,
    }
    # Persist the outcome for the Operator read/fix loop: `tiger-memory
    # doctor` reports the last ingest's malformed/locked counts without
    # re-deriving them (practicality audit).
    from .inspect_tools import record_sweep_report
    record_sweep_report(store, "ingest", summary)
    print(json.dumps(summary, indent=2))
    return 1 if malformed else 0


# ----- operator read/fix loop (search / forget / doctor) --------------------


def _cmd_search(
    cfg: Config, term: str, *, team: bool, store_filter: str | None
) -> int:
    """Print one line per hit + a trailing count. Exit 0 always — zero
    matches is an answer, not an error."""
    from .inspect_tools import search_memory
    hits = search_memory(cfg, term, team=team, store=store_filter)
    for h in hits:
        print(f"{h.persona}  {h.store}  {h.ref}  {h.line}")
    print(f"{len(hits)} match(es)")
    return 0


def _cmd_forget(
    cfg: Config, store: Store, *, store_name: str,
    entry_id: str | None, slug: str | None,
) -> int:
    """Operator-authority removal of one entry (audited, never silent).

    Exit: 0 = forgotten (prints what); 1 = no matching entry (or a stuck
    store lock); 2 = bad flag combo (``--slug`` outside topics; argparse
    already enforces exactly-one-of ``--id``/``--slug``).
    """
    if slug is not None and store_name != "topics":
        print("--slug only addresses the topics store; use --id",
              file=sys.stderr)
        return 2
    from .bounded_store import StoreLockHeld
    from .inspect_tools import forget_entry
    try:
        removed = forget_entry(
            cfg, store, store_name=store_name, entry_id=entry_id, slug=slug
        )
    except StoreLockHeld as exc:
        print(f"store locked: {exc}", file=sys.stderr)
        return 1
    if removed is None:
        ref = slug if slug is not None else entry_id
        print(f"no {store_name} entry matches {ref!r}", file=sys.stderr)
        return 1
    from .entries import TopicEntry
    ref = removed.slug if isinstance(removed, TopicEntry) else removed.id
    head = " ".join(removed.text.split())
    if len(head) > 80:
        head = head[:79] + "…"
    print(f"forgot {store_name} {ref}: {head}")
    return 0


def _cmd_doctor(cfg: Config, *, as_json: bool) -> int:
    """Team-wide health view. Exit 0 = nothing flagged; 1 = ≥1 flag (so a
    cron wrapper can alert on the exit code alone)."""
    from .inspect_tools import doctor_report, render_doctor
    report = doctor_report(cfg)
    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_doctor(report))
    return 1 if report["flags"] else 0


# ----- B3 team-sweep gating helpers ----------------------------------------


def _team_memories_dir(cfg: Config) -> Path:
    """The team-scoped sweep-state dir = the parent of this persona's store."""
    return cfg.store.root.parent


def _parse_now(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _cmd_sweep_plan(
    cfg: Config, store: Store, *, token: str | None,
    max_personas: int | None, floor_hours: float | None,
    lease_seconds: float | None, now: str | None,
    own_persona: str | None = None, exclude_session: str | None = None,
) -> int:
    import uuid

    from . import sweep

    # Split gate: the own-persona pending check (cheap, no LLM) runs
    # against THIS config's sources + cursors — the config must belong to
    # the named persona. Computed before the claim so a floor-bypassing
    # own-only claim is only taken when there is actually work.
    own = None
    own_pending = False
    if own_persona:
        from .lifecycle import has_pending_source

        own_pending = has_pending_source(
            cfg, store, exclude_session=exclude_session
        )
        own = {"persona": own_persona, "pending": own_pending}

    # Flags win; otherwise the config's sweep: block (tunable per team —
    # previously these were untunable code constants).
    claim_token = token or uuid.uuid4().hex
    decision = sweep.maybe_sweep_roster(
        _team_memories_dir(cfg), now=_parse_now(now), token=claim_token,
        max_personas=(
            max_personas if max_personas is not None else cfg.sweep.max_personas
        ),
        floor_hours=(
            floor_hours if floor_hours is not None else cfg.sweep.floor_hours
        ),
        lease_seconds=(
            lease_seconds if lease_seconds is not None
            else cfg.sweep.lease_seconds
        ),
        own_persona=own_persona, own_pending=own_pending,
    )
    plan = decision.plan
    payload = {
        "ran": decision.ran,
        "reason": decision.reason,
        "token": claim_token,
        "scope": decision.scope,
        "own": own,
        "targets": [
            {"name": t.name, "config_path": str(t.config_path)}
            for t in (plan.targets if plan else [])
        ],
        "remaining": plan.remaining if plan else 0,
        "all_personas": plan.all_personas if plan else 0,
    }
    print(json.dumps(payload, indent=2))
    return 0


def _cmd_sweep_done(cfg: Config, persona: str) -> int:
    from . import sweep
    sweep.record_persona_done(_team_memories_dir(cfg), persona)
    print(f"recorded {persona} done")
    return 0


def _cmd_sweep_complete(
    cfg: Config, *, now: str | None, token: str | None, force: bool = False
) -> int:
    from . import sweep
    # Read the claim's scope BEFORE completing — mark_sweep_complete clears
    # it. The success message must match what actually happened: an
    # own-only close leaves the team watermark untouched, and this CLI
    # line is the surface an AI driver reads (printing "watermark
    # advanced" there sent one auditing its own run to the state file).
    state = sweep.read_sweep_state(_team_memories_dir(cfg))
    scope = state.get("scope") or "team"
    if not sweep.mark_sweep_complete(
        _team_memories_dir(cfg), _parse_now(now), token=token, force=force
    ):
        print("sweep-complete refused: claim token mismatch or personas "
              "still pending (see log; --force overrides the pending check)",
              file=sys.stderr)
        return 3
    if scope == "own-only":
        print("sweep complete; own-only run — team watermark unchanged")
    else:
        print("sweep complete; watermark advanced")
    return 0


def _cmd_sweep_release(cfg: Config, *, token: str | None) -> int:
    from . import sweep
    if not sweep.release_sweep_claim(_team_memories_dir(cfg), token=token):
        print("sweep-release refused: claim token mismatch "
              "(another session owns the sweep)", file=sys.stderr)
        return 3
    print("claim released; watermark unchanged")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

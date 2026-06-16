"""tiger-memory CLI entrypoint.

Subcommands (see design doc §3.3):

    Writers (acquire lock):
        init          create empty store, validate config
        bootstrap     one-shot backfill (§11)
        rebuild       lazy rebuild (session-start hook)
        pin           direct injection into must_memorize
        resummarize   re-summarize within a date range

    Readers (lockless):
        drill         walk children of a path
        tree          recursive hierarchy
        raw           raw-transcript locator
        search        grep (default) or rag mode
        state         show JSON state snapshot

    Team sweep (B3 gating, non-AI -- drive the in-session protocol):
        sweep-plan      claim the team sweep; print targets for this wake
        sweep-done      mark one persona done in the in-flight run
        sweep-complete  advance the watermark + end the run
        sweep-release   drop the claim without advancing the watermark
"""
from __future__ import annotations

import logging

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import Config, ConfigError, load_config
from .store import Store
from .sweep import DEFAULT_MAX_PERSONAS

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

    p_bs = sub.add_parser(
        "bootstrap", help="One-shot backfill over all configured sources."
    )
    p_bs.add_argument("--dry-run", action="store_true",
                     help="Estimate cost on 5 representative transcripts, no model spend on the full set.")
    p_bs.add_argument("--limit", type=int, default=None,
                     help="Optional cap on number of sessions to process (for testing).")

    p_rb = sub.add_parser("rebuild", help="Lazy rebuild (session-start hook).")
    p_rb.add_argument("--background", action="store_true",
                     help="Detach and run in background (used by hooks).")

    p_pin = sub.add_parser("pin", help="Inject a must-memorize row directly.")
    p_pin.add_argument("memo", help="Memo text (≤ 25 words by default).")
    p_pin.add_argument(
        "--kind",
        choices=["owner_explicit", "preference", "decision", "incident"],
        default="owner_explicit",
    )

    p_re = sub.add_parser("resummarize", help="Re-summarize within a date range.")
    p_re.add_argument("--since", required=True, help="ISO date (YYYY-MM-DD).")
    p_re.add_argument("--summarizer", default=None,
                     help="e.g. default@v2 (default: current config).")

    # ----- readers -----
    p_dr = sub.add_parser("drill", help="Open file + list immediate children.")
    p_dr.add_argument("path", help="Path to a summary file.")

    p_tr = sub.add_parser("tree", help="Recursive hierarchy from a path.")
    p_tr.add_argument("path", help="Starting summary file.")
    p_tr.add_argument("--depth", type=int, default=None,
                     help="Max depth (default: unlimited).")

    p_raw = sub.add_parser("raw", help="Raw-transcript locator for an archive entry.")
    p_raw.add_argument("archive_path", help="Path to archive/<file>.md.")

    p_sr = sub.add_parser("search", help="Search journal/ + archive/.")
    p_sr.add_argument("topic", help="Query string.")
    p_sr.add_argument(
        "--mode",
        choices=["auto", "grep", "rag", "hybrid"],
        default="auto",
        help="auto (default; hybrid if RAG available, else grep), "
             "grep (ripgrep), rag (embedding semantic), "
             "hybrid (grep + rag fused via RRF).",
    )

    sub.add_parser("state", help="Print JSON state snapshot.")

    # ----- B1 stage-2: in-session sub-agent executor (plan / ingest) -----
    p_plan = sub.add_parser(
        "plan",
        help="Stage collapsed prompts for the in-session sub-agent and "
             "print the work manifest (non-AI).",
    )
    p_plan.add_argument("--max-sessions", type=int, default=None,
                        help="Cap on transcripts staged this plan.")

    p_ing = sub.add_parser(
        "ingest-summary",
        help="Write back one sub-agent's collapsed summary bundle (read "
             "from stdin) for a planned conversation uuid.",
    )
    p_ing.add_argument("--uuid", required=True,
                       help="conversation_uuid from the plan manifest.")

    sub.add_parser(
        "ingest-staged",
        help="Glue: ingest every staged <uuid>.summary.md card in ONE process "
             "(per-persona merge serialized by construction -- no race). Run "
             "after the summarize sub-agents have written their cards.",
    )

    # ----- B3 team-sweep gating convenience CLIs (non-AI) -----
    # Thin wrappers over tiger_memory.sweep so an interactive persona
    # session drives the gating (see docs/tiger-memory-sweep-protocol.md)
    # without inline Python. team_memories_dir = cfg.store.root.parent.
    p_swp = sub.add_parser(
        "sweep-plan",
        help="Try to claim the team sweep at session bootstrap; print the "
             "decision + roster targets to process this wake (non-AI).",
    )
    p_swp.add_argument("--token", default=None,
                       help="Stable claim token (default: random uuid). Pass "
                            "the interactive session id so a crashed claim "
                            "stays re-stealable across resumes.")
    p_swp.add_argument("--max-personas", type=int, default=DEFAULT_MAX_PERSONAS,
                       help="Per-wake cap on personas processed this trigger "
                            f"(default {DEFAULT_MAX_PERSONAS}; pass a larger "
                            "number to process more per wake).")
    p_swp.add_argument("--floor-hours", type=float, default=None,
                       help="Staleness floor override (default 24h).")
    p_swp.add_argument("--lease-seconds", type=float, default=None,
                       help="Soft-lease seconds before a claim is stealable.")
    p_swp.add_argument("--now", default=None,
                       help="ISO timestamp override (testing/determinism).")

    p_swd = sub.add_parser(
        "sweep-done",
        help="Mark one persona completed in the in-flight sweep run.",
    )
    p_swd.add_argument("--persona", required=True,
                       help="Persona name that was just summarized.")

    p_swc = sub.add_parser(
        "sweep-complete",
        help="Advance the team watermark + end the run (all due personas "
             "processed). Makes the next trigger a cheap no-op.",
    )
    p_swc.add_argument("--now", default=None,
                       help="ISO timestamp override (testing/determinism).")

    sub.add_parser(
        "sweep-release",
        help="Drop the claim WITHOUT advancing the watermark (per-wake cap "
             "hit; the next wake resumes the remaining personas).",
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
    if args.cmd == "bootstrap":
        from .lifecycle import bootstrap
        return bootstrap(cfg, store, dry_run=args.dry_run, limit=args.limit)
    if args.cmd == "rebuild":
        from .lifecycle import rebuild
        return rebuild(cfg, store, background=args.background)
    if args.cmd == "pin":
        from .must_memorize import pin as pin_cmd
        return pin_cmd(cfg, store, memo=args.memo, kind=args.kind)
    if args.cmd == "resummarize":
        from .lifecycle import resummarize
        return resummarize(cfg, store, since=args.since, summarizer=args.summarizer)
    if args.cmd == "drill":
        from .drill import drill as drill_cmd
        return drill_cmd(store, Path(args.path))
    if args.cmd == "tree":
        from .drill import tree as tree_cmd
        return tree_cmd(store, Path(args.path), depth=args.depth)
    if args.cmd == "raw":
        from .drill import raw as raw_cmd
        return raw_cmd(cfg, store, Path(args.archive_path))
    if args.cmd == "search":
        from .drill import search as search_cmd
        return search_cmd(cfg, store, topic=args.topic, mode=args.mode)
    if args.cmd == "state":
        return _cmd_state(cfg, store)
    if args.cmd == "plan":
        return _cmd_plan(cfg, store, args.max_sessions)
    if args.cmd == "ingest-summary":
        return _cmd_ingest_summary(cfg, store, args.uuid)
    if args.cmd == "ingest-staged":
        return _cmd_ingest_staged(cfg, store)
    if args.cmd == "sweep-plan":
        return _cmd_sweep_plan(
            cfg,
            token=args.token,
            max_personas=args.max_personas,
            floor_hours=args.floor_hours,
            lease_seconds=args.lease_seconds,
            now=args.now,
        )
    if args.cmd == "sweep-done":
        return _cmd_sweep_done(cfg, args.persona)
    if args.cmd == "sweep-complete":
        return _cmd_sweep_complete(cfg, now=args.now)
    if args.cmd == "sweep-release":
        return _cmd_sweep_release(cfg)

    parser.print_help()
    return 2


def _cmd_init(cfg: Config, store: Store) -> int:
    store.init_layout()
    print(f"tiger-memory store initialised at: {store.root}")
    print(f"  archive/   {store.paths.archive}")
    print(f"  journal/   {store.paths.journal}")
    print(f"  briefing/  {store.paths.briefing}")
    print(f"Agent: {cfg.agent.name}")
    print(f"Sources: {[s.kind for s in cfg.sources]}")
    print(f"Summarizer: {cfg.summarizer.backend}:{cfg.summarizer.model} "
          f"(prompts {cfg.summarizer.prompts})")
    return 0


def _cmd_state(cfg: Config, store: Store) -> int:
    from .state import compute_state
    payload = compute_state(cfg, store)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _cmd_plan(cfg: Config, store: Store, max_sessions: int | None) -> int:
    from .lifecycle import _sweep_staging_dir, plan_rebuild
    plan_rebuild(cfg, store, max_sessions=max_sessions)
    # Print the persisted manifest verbatim so the CLI output and the file
    # the driver reads are the same single source of truth -- it carries both
    # ``items`` (per-uuid metadata) and ``stacks`` (the sub-agent grouping).
    manifest_path = _sweep_staging_dir(store) / "manifest.json"
    print(manifest_path.read_text(encoding="utf-8").rstrip("\n"))
    return 0


def _load_plan_manifest(store: Store) -> dict | None:
    """Return the plan manifest dict, or ``None`` (after printing why) when it
    is missing/unreadable. Shared by both ingest paths."""
    from .lifecycle import _sweep_staging_dir
    manifest_path = _sweep_staging_dir(store) / "manifest.json"
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print(f"no plan manifest at {manifest_path}; run `tiger-memory plan` "
              f"first", file=sys.stderr)
        return None


def _ingest_item_bundle(cfg: Config, store: Store, item: dict, bundle_text: str):
    """Parse + write back one collapsed bundle for a manifest *item*, then drop
    the now-consumed staged prompt. Raises ``CollapseParseError`` on a
    malformed bundle BEFORE any write, so the store + prompt are left intact
    and the transcript can be re-summarized."""
    from .executor import ingest_collapsed_summary
    result = ingest_collapsed_summary(
        store, cfg,
        conversation_uuid=item["conversation_uuid"],
        source=item["source"],
        source_id=item["source_id"],
        first_event_at=datetime.fromisoformat(item["first_event_at"]),
        last_event_at=datetime.fromisoformat(item["last_event_at"]),
        bundle_text=bundle_text,
        raw_path=Path(item["raw_path"]),
    )
    # The staged prompt embeds the (prefiltered) transcript; once ingested it
    # is consumed, so drop it to avoid leaving transcript content at rest.
    Path(item["prompt_path"]).unlink(missing_ok=True)
    return result


def _cmd_ingest_summary(cfg: Config, store: Store, uuid: str) -> int:
    """Write back one sub-agent's collapsed bundle (stdin) for a planned
    conversation. The metadata comes from the plan manifest so the caller
    only needs the uuid + the bundle."""
    from .collapse import CollapseParseError
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
    try:
        result = _ingest_item_bundle(cfg, store, item, bundle)
    except CollapseParseError as exc:
        print(f"malformed summary bundle for {uuid}: {exc}", file=sys.stderr)
        return 1
    print(f"ingested {result.conversation_uuid} "
          f"(+{result.must_memorize_added} must-memorize)")
    return 0


def _cmd_ingest_staged(cfg: Config, store: Store) -> int:
    """Deferred glue: ingest every staged ``<uuid>.summary.md`` card in ONE
    process, so the per-persona must-memorize merge is serialized by
    construction — no agent-side coordination, no lost-update race. The cards
    are written by the summarize sub-agents; this consumes them.

    Per manifest item that has a card: parse + write it back, then delete the
    card (its prompt is dropped by ``_ingest_item_bundle``). A malformed card
    is left in place and reported. An item with no card yet is skipped — it was
    not summarized this wake and re-stages next wake (the store, not the card,
    is the durable ledger; a re-``plan`` wipes the staging dir).

    Exit: 0 = no malformed cards; 1 = at least one malformed card (re-summarize
    those); 2 = no plan manifest.
    """
    from .collapse import CollapseParseError
    from .lifecycle import _sweep_card_path
    manifest = _load_plan_manifest(store)
    if manifest is None:
        return 2

    ingested: list[str] = []
    malformed: list[str] = []
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
        except CollapseParseError as exc:
            print(f"malformed summary card for {uuid}: {exc}", file=sys.stderr)
            malformed.append(uuid)
            continue
        card_path.unlink(missing_ok=True)
        ingested.append(result.conversation_uuid)

    print(json.dumps({
        "ingested": len(ingested),
        "malformed": malformed,
        "skipped_no_card": skipped_no_card,
    }, indent=2))
    return 1 if malformed else 0


# ----- B3 team-sweep gating helpers ----------------------------------------


def _team_memories_dir(cfg: Config) -> Path:
    """The team-scoped sweep-state dir = the parent of this persona's store
    (``<team>/memories/``). It is the same path for every persona on the
    team, which is what makes the sweep claim team-scoped."""
    return cfg.store.root.parent


def _parse_now(raw: str | None) -> datetime:
    """UTC now, or an ISO override (tolerant of a trailing ``Z``)."""
    if not raw:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _cmd_sweep_plan(
    cfg: Config,
    *,
    token: str | None,
    max_personas: int | None,
    floor_hours: float | None,
    lease_seconds: float | None,
    now: str | None,
) -> int:
    import uuid

    from . import sweep

    kwargs: dict[str, float] = {}
    if floor_hours is not None:
        kwargs["floor_hours"] = floor_hours
    if lease_seconds is not None:
        kwargs["lease_seconds"] = lease_seconds
    claim_token = token or uuid.uuid4().hex
    decision = sweep.maybe_sweep_roster(
        _team_memories_dir(cfg),
        now=_parse_now(now),
        token=claim_token,
        max_personas=max_personas,
        **kwargs,
    )
    plan = decision.plan
    payload = {
        "ran": decision.ran,
        "reason": decision.reason,
        # Echo the token actually used so a session that let us mint a
        # uuid can re-claim its own hold on a later wake within the run.
        "token": claim_token,
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


def _cmd_sweep_complete(cfg: Config, *, now: str | None) -> int:
    from . import sweep

    sweep.mark_sweep_complete(_team_memories_dir(cfg), _parse_now(now))
    print("sweep complete; watermark advanced")
    return 0


def _cmd_sweep_release(cfg: Config) -> int:
    from . import sweep

    sweep.release_sweep_claim(_team_memories_dir(cfg))
    print("claim released; watermark unchanged")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

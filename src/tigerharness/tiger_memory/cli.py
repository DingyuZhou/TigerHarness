"""tiger-memory CLI entrypoint (bounded-store revamp).

Subcommands:

    Writers:
        init          create empty store, validate config
        rebuild       fresh-start: drop the retired surface, regenerate the
                      session-start briefing (skill index + must_remember +
                      emotional view + unprocessed notice)
        pin           write a must_remember entry directly
        import-legacy one-off (idempotent) seed of the three stores from the
                      old must_memorize.md pins + daily/weekly/monthly rollups.
                      MUST run BEFORE the fresh-start `rebuild` (which deletes
                      the legacy files): merge -> migrate configs ->
                      import-legacy -> rebuild -> sweep.

    Readers (lockless):
        state         show JSON state snapshot for the three bounded stores

    In-session sub-agent executor (subscription rail, non-AI glue):
        plan          stage one extraction prompt per idle, unprocessed
                      transcript; print the work manifest
        ingest-extraction   write back one sub-agent's extraction bundle
                            (stdin) for a planned conversation uuid
        ingest-staged       ingest every staged <uuid>.extract.md card in ONE
                            process (per-persona merge serialized by construction)

    Team-sweep gating (B3, non-AI -- drive the in-session protocol):
        sweep-plan      claim the team sweep; print targets for this wake
        sweep-done      mark one persona done in the in-flight run
        sweep-complete  advance the watermark + end the run
        sweep-release   drop the claim without advancing the watermark
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

    sub.add_parser(
        "rebuild",
        help="Fresh-start: drop the retired surface, regenerate the "
             "session-start briefing (skill index + stores + notice).",
    )

    p_pin = sub.add_parser("pin", help="Write a must_remember entry directly.")
    p_pin.add_argument("memo", help="Memo text.")
    p_pin.add_argument(
        "--kind",
        choices=["owner_explicit", "preference", "decision", "incident"],
        default="owner_explicit",
    )

    p_imp = sub.add_parser(
        "import-legacy",
        help="One-off (idempotent) seed of the 3 stores from the old "
             "must_memorize.md pins + daily/weekly/monthly rollups. Run BEFORE "
             "`rebuild` (which deletes the legacy files).",
    )
    p_imp.add_argument(
        "--mock", action="store_true",
        help="Use the deterministic mock summarizer (no live model; CI/dry-run).",
    )
    p_imp.add_argument(
        "--force", action="store_true",
        help="Re-seed even if already imported (drops prior import-legacy "
             "entries first).",
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
        "ingest-staged",
        help="Glue: ingest every staged <uuid>.extract.md card in ONE process "
             "(per-persona merge serialized by construction -- no race).",
    )

    # ----- B3 team-sweep gating convenience CLIs (non-AI) -----
    p_swp = sub.add_parser(
        "sweep-plan",
        help="Try to claim the team sweep at session bootstrap; print the "
             "decision + roster targets to process this wake (non-AI).",
    )
    p_swp.add_argument("--token", default=None,
                       help="Stable claim token (default: random uuid).")
    p_swp.add_argument("--max-personas", type=int, default=DEFAULT_MAX_PERSONAS,
                       help=f"Per-wake cap on personas (default {DEFAULT_MAX_PERSONAS}).")
    p_swp.add_argument("--floor-hours", type=float, default=None,
                       help="Staleness floor override (default 24h).")
    p_swp.add_argument("--lease-seconds", type=float, default=None,
                       help="Soft-lease seconds before a claim is stealable.")
    p_swp.add_argument("--now", default=None,
                       help="ISO timestamp override (testing/determinism).")

    p_swd = sub.add_parser(
        "sweep-done", help="Mark one persona completed in the in-flight run.")
    p_swd.add_argument("--persona", required=True,
                       help="Persona name that was just processed.")

    p_swc = sub.add_parser(
        "sweep-complete",
        help="Advance the team watermark + end the run (all due personas done).")
    p_swc.add_argument("--now", default=None,
                       help="ISO timestamp override (testing/determinism).")

    sub.add_parser(
        "sweep-release",
        help="Drop the claim WITHOUT advancing the watermark (per-wake cap hit).")

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
    if args.cmd == "import-legacy":
        return _cmd_import_legacy(cfg, store, mock=args.mock, force=args.force)
    if args.cmd == "state":
        return _cmd_state(cfg, store)
    if args.cmd == "plan":
        return _cmd_plan(cfg, store, args.max_sessions)
    if args.cmd == "ingest-extraction":
        return _cmd_ingest_extraction(cfg, store, args.uuid)
    if args.cmd == "ingest-staged":
        return _cmd_ingest_staged(cfg, store)
    if args.cmd == "sweep-plan":
        return _cmd_sweep_plan(
            cfg, token=args.token, max_personas=args.max_personas,
            floor_hours=args.floor_hours, lease_seconds=args.lease_seconds,
            now=args.now,
        )
    if args.cmd == "sweep-done":
        return _cmd_sweep_done(cfg, args.persona)
    if args.cmd == "sweep-complete":
        return _cmd_sweep_complete(cfg, now=args.now)
    if args.cmd == "sweep-release":  # pragma: no branch  # exhaustive dispatch
        return _cmd_sweep_release(cfg)
    return 2  # pragma: no cover  # argparse rejects unknown subcommands first


def _cmd_init(cfg: Config, store: Store) -> int:
    store.init_layout()
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


def _cmd_import_legacy(cfg: Config, store: Store, *, mock: bool, force: bool) -> int:
    """One-off legacy import — seed the three stores from the old pins + rollups.

    Idempotent: a no-op once imported (unless ``--force``). ``--mock`` uses the
    deterministic mock summarizer (no live model). NEVER runs ``rebuild`` — the
    caller drops the legacy files AFTER (merge -> migrate configs ->
    import-legacy -> rebuild -> sweep).
    """
    from .import_legacy import import_legacy_run
    from .lifecycle import _build_summarizer
    summarizer = _build_summarizer(cfg, mock=mock)
    result = import_legacy_run(cfg, store, summarizer=summarizer, force=force)
    if result.get("skipped") == "already":
        print("import-legacy: already imported; nothing to do (use --force to re-seed)")
        return 0
    print(f"import-legacy seeded: {result['skills']} skills, "
          f"{result['must_remember']} must_remember, {result['diary']} emotional")
    return 0


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
    )
    # The staged prompt embeds the (prefiltered) transcript; once ingested it
    # is consumed, so drop it to avoid leaving transcript content at rest.
    Path(item["prompt_path"]).unlink(missing_ok=True)
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
    try:
        result = _ingest_item_bundle(cfg, store, item, bundle)
    except ExtractionParseError as exc:
        print(f"malformed extraction bundle for {uuid}: {exc}", file=sys.stderr)
        return 1
    print(f"ingested {result.conversation_uuid} (+{result.total_added} entries: "
          f"{result.skills_added} skills, {result.must_remember_added} "
          f"must_remember, {result.emotional_added} emotional)")
    return 0


def _cmd_ingest_staged(cfg: Config, store: Store) -> int:
    """Ingest every staged ``<uuid>.extract.md`` card in ONE process, so the
    per-persona merge is serialized by construction — no race. A malformed card
    is left in place and reported. An item with no card yet is skipped.

    Exit: 0 = no malformed cards; 1 = ≥1 malformed card; 2 = no plan manifest.
    """
    from .lifecycle import ExtractionParseError, _sweep_card_path
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
        except ExtractionParseError as exc:
            print(f"malformed extraction card for {uuid}: {exc}", file=sys.stderr)
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
    """The team-scoped sweep-state dir = the parent of this persona's store."""
    return cfg.store.root.parent


def _parse_now(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _cmd_sweep_plan(
    cfg: Config, *, token: str | None, max_personas: int | None,
    floor_hours: float | None, lease_seconds: float | None, now: str | None,
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
        _team_memories_dir(cfg), now=_parse_now(now), token=claim_token,
        max_personas=max_personas, **kwargs,
    )
    plan = decision.plan
    payload = {
        "ran": decision.ran,
        "reason": decision.reason,
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

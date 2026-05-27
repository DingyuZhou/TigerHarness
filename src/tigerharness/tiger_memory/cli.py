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
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Config, ConfigError, load_config
from .store import Store


def main(argv: list[str] | None = None) -> int:
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


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

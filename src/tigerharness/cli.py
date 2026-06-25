"""Top-level CLI entry point for tigerharness.

Dispatches to sub-package CLIs:
    tigerharness slack-bridge <subcommand>
    tigerharness tiger-memory <subcommand>
    tigerharness journal <subcommand>
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    from tigerharness._logging import configure_cli_logging
    configure_cli_logging()
    args = argv if argv is not None else sys.argv[1:]

    if not args:
        _usage()
        return 0

    cmd = args[0]
    rest = args[1:]

    if cmd == "init":
        from .init import main as init_main
        return init_main(rest)
    elif cmd == "dismiss":
        from .dismiss import main as dismiss_main
        return dismiss_main(rest)
    elif cmd in ("tiger-memory", "tiger_memory", "tm"):
        from .tiger_memory.cli import main as tm_main
        return tm_main(rest)
    elif cmd in ("journal", "j"):
        from .journal.cli import main as journal_main
        return journal_main(rest)
    elif cmd in ("autodrive", "ad"):
        from .autodrive.cli import main as autodrive_main
        return autodrive_main(rest)
    elif cmd in ("slack-bridge", "slack_bridge", "sb"):
        # Sub-dispatch:
        #   slack-bridge gen-service ...  -> render a systemd user unit
        #   slack-bridge text/file ...    -> forward to the notify CLI
        if rest and rest[0] in ("gen-service", "gen_service"):
            from .slack_bridge.gen_service import main as gs_main
            return gs_main(rest[1:])
        from .slack_bridge.notify import main as notify_main
        return notify_main(rest)
    elif cmd in ("--help", "-h", "help"):
        _usage()
        return 0
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        _usage()
        return 2


def _usage() -> None:
    print("tigerharness -- Claude Code agent harness")
    print()
    print("Sub-commands:")
    print("  init               Scaffold a new project (personas, .env, config)")
    print("  dismiss            Interactively tear down a team or persona")
    print("  tiger-memory (tm)  Persistent memory management")
    print("  slack-bridge (sb)  Slack notify CLI")
    print("  journal (j)        File-based subscription backend (Phase 1)")
    print("  autodrive (ad)     Periodically drive the journal (agent SDK)")
    print()
    print("Usage: tigerharness <sub-command> [args...]")


if __name__ == "__main__":
    sys.exit(main())

"""``tigerharness journal`` CLI: ``new`` / ``list`` / ``status`` / ``sweep``.

Wired into ``tigerharness journal`` (or ``python -m
tigerharness.journal``). The driver skill calls ``journal sweep`` as
its first action -- so a journal-aware non-Claude agent could shell
out to the same command.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from tigerharness.journal.models import JournalModelError, Status
from tigerharness.journal.paths import (
    JournalPathError,
    JournalPaths,
    default_journal_root,
)
from tigerharness.journal.scaffold import JournalScaffoldError, new_task
from tigerharness.journal.sweep import (
    DEFAULT_STUCK_TIMEOUT_SEC,
    stuck_timeout_from_env,
    sweep,
)


def _paths_from_args(args: argparse.Namespace) -> JournalPaths:
    """Resolve the journal root. ``--journal-dir`` wins over the env."""
    if args.journal_dir:
        root = Path(args.journal_dir).expanduser().resolve()
    else:
        root = default_journal_root()
    return JournalPaths(root=root)


# ---------------------------------------------------------------------------
# new
# ---------------------------------------------------------------------------

def cmd_new(args: argparse.Namespace) -> int:
    prd_path = Path(args.prd).expanduser()
    if not prd_path.exists():
        print(f"error: PRD not found: {prd_path}", file=sys.stderr)
        return 2
    prd_text = prd_path.read_text(encoding="utf-8")
    paths = _paths_from_args(args)
    try:
        result = new_task(
            prd_text=prd_text,
            persona=args.persona,
            paths=paths,
            title=args.title,
            kind=args.kind,
            max_sessions=args.max_sessions,
            slug=args.slug,
        )
    except (JournalScaffoldError, JournalModelError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"Scaffolded: {result.task_id}")
    print(f"  title:        {result.status.title}")
    print(f"  persona:      {result.status.persona}")
    print(f"  kind:         {result.status.kind}")
    print(f"  max_sessions: {result.status.max_sessions}")
    print(f"  task_dir:     {result.task_dir}")
    print()
    print(
        "Open Claude Code and invoke the `drive-journal` skill to "
        "start working it."
    )
    return 0


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def _read_status_or_none(paths: JournalPaths, task_id: str) -> Status | None:
    try:
        return Status.from_json(paths.status_json(task_id).read_text())
    except (JournalModelError, OSError, JournalPathError):
        return None


def cmd_list(args: argparse.Namespace) -> int:
    paths = _paths_from_args(args)
    if not paths.active.is_dir():
        if args.format == "json":
            print("[]")
        else:
            print("No active tasks.")
        return 0
    rows: list[Status] = []
    malformed: list[str] = []
    for task_id in paths.list_active_ids():
        status = _read_status_or_none(paths, task_id)
        if status is None:
            malformed.append(task_id)
            continue
        rows.append(status)

    if args.format == "json":
        print(json.dumps(
            {
                "active": [dataclasses.asdict(s) | {"state": s.state.value}
                           for s in rows],
                "malformed": malformed,
            },
            indent=2,
        ))
        return 0

    if not rows and not malformed:
        print("No active tasks.")
        return 0

    print(f"{'ID':40}  {'STATE':12}  {'PERSONA':12}  TITLE")
    print(f"{'-'*40}  {'-'*12}  {'-'*12}  -----")
    for s in rows:
        print(f"{s.id:40}  {s.state.value:12}  {s.persona:12}  {s.title}")
    if malformed:
        print()
        print(f"Malformed (status.json unreadable): {len(malformed)}")
        for tid in malformed:
            print(f"  - {tid}")
    return 0


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> int:
    paths = _paths_from_args(args)
    status = _read_status_or_none(paths, args.task_id)
    if status is None:
        print(
            f"error: no task with id {args.task_id!r} in "
            f"{paths.active}",
            file=sys.stderr,
        )
        return 1
    payload = dataclasses.asdict(status) | {"state": status.state.value}
    print(json.dumps(payload, indent=2))
    return 0


# ---------------------------------------------------------------------------
# sweep
# ---------------------------------------------------------------------------

def cmd_sweep(args: argparse.Namespace) -> int:
    paths = _paths_from_args(args)
    paths.ensure()
    try:
        timeout = (
            args.stuck_timeout
            if args.stuck_timeout is not None
            else stuck_timeout_from_env()
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    result = sweep(paths, stuck_timeout_sec=timeout)

    if args.format == "json":
        payload = {
            "summary": result.to_summary(),
            "archived": result.archived,
            "pending": [s.id for s in result.pending],
            "in_progress_fresh": [s.id for s in result.in_progress_fresh],
            "in_progress_stale": [s.id for s in result.in_progress_stale],
            "blocked": [s.id for s in result.blocked],
            "malformed": [
                {"task_id": m.task_id, "error": m.error}
                for m in result.malformed
            ],
            "actionable": [s.id for s in result.actionable()],
        }
        print(json.dumps(payload, indent=2))
        return 0

    print(result.to_summary())
    if result.archived:
        print()
        print("Archived (moved to done/):")
        for tid in result.archived:
            print(f"  - {tid}")
    if result.actionable():
        print()
        print("Actionable (pick one of these next):")
        for s in result.actionable():
            print(f"  - {s.id}  [{s.state.value}]  {s.title}")
    if result.in_progress_fresh:
        print()
        print("Fresh in_progress (LEAVE ALONE -- another session owns):")
        for s in result.in_progress_fresh:
            print(f"  - {s.id}  {s.title}")
    if result.blocked:
        print()
        print("Blocked (need human attention):")
        for s in result.blocked:
            print(f"  - {s.id}  {s.title}")
    if result.malformed:
        print()
        print("Malformed status.json (sweep skipped these):")
        for m in result.malformed:
            print(f"  - {m.task_id}: {m.error}")
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="journal",
        description=(
            "Journal CLI for the subscription backend (Phase 1). "
            "See docs/subscription-backend.md."
        ),
    )
    p.add_argument(
        "--journal-dir",
        default="",
        help=(
            "Override the journal root. Wins over $TIGERHARNESS_JOURNAL_DIR "
            "and the team-folder convention."
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("new", help="Scaffold a new task from a PRD.")
    n.add_argument(
        "--prd", required=True,
        help="Path to the PRD / brief markdown file.",
    )
    n.add_argument(
        "--persona", required=True,
        help="The persona this task is assigned to.",
    )
    n.add_argument(
        "--title", default="",
        help="Human label. Defaults to the first H1 of the PRD.",
    )
    n.add_argument(
        "--slug", default="",
        help=(
            "Override the slug portion of the task id. Defaults to "
            "slugified title."
        ),
    )
    n.add_argument(
        "--kind", default="task", choices=["task"],
        help="Phase 1 only accepts kind=task.",
    )
    n.add_argument(
        "--max-sessions", type=int, default=5,
        help="Soft ceiling on drive-journal invocations. Default 5.",
    )
    n.set_defaults(func=cmd_new)

    li = sub.add_parser("list", help="List active tasks.")
    li.add_argument(
        "--format", choices=["table", "json"], default="table",
    )
    li.set_defaults(func=cmd_list)

    st = sub.add_parser("status", help="Print one task's status.json.")
    st.add_argument("task_id")
    st.set_defaults(func=cmd_status)

    sw = sub.add_parser(
        "sweep",
        help=(
            "Run the lazy sweep: archive done tasks, classify "
            "in_progress as fresh/stale, summarise. Side-effecting."
        ),
    )
    sw.add_argument(
        "--format", choices=["text", "json"], default="text",
    )
    sw.add_argument(
        "--stuck-timeout", type=int, default=None,
        help=(
            f"Heartbeat age past which an in_progress task is stale. "
            f"Default reads $TIGERHARNESS_JOURNAL_STUCK_TIMEOUT or "
            f"{DEFAULT_STUCK_TIMEOUT_SEC}."
        ),
    )
    sw.set_defaults(func=cmd_sweep)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

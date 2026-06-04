"""``tigerharness journal`` CLI: ``new`` / ``list`` / ``status`` / ``sweep``
plus the Phase 1.5 compile subcommands (``compile-context``,
``compile-prompts``, ``validate-graph``, ``land-compile``, ``abort``,
``validate-personas``).

Wired into ``tigerharness journal`` (or ``python -m
tigerharness.journal``). The driver skill calls ``journal sweep`` as
its first action -- so a journal-aware non-Claude agent could shell
out to the same command. The compile subcommands are invoked from
the in-session compile sub-protocol.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tigerharness.journal.compile_cli import build_subparsers as _build_compile_subparsers
from tigerharness.journal.models import JournalModelError, Status
from tigerharness.journal.paths import (
    JournalPathError,
    JournalPaths,
    default_journal_root,
)
from tigerharness.journal.scaffold import (
    JournalScaffoldError,
    MissingPersonaError,
    new_task,
    new_workflow_task,
    resolve_default_persona,
    resolve_playbook_default_captain,
    resolve_team_root,
)
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
    """Scaffold a task or workflow per ``--kind``. The two paths share
    no logic beyond input reading -- task mode wraps ``new_task``,
    workflow mode wraps ``new_workflow_task`` (which validates the
    team's compile-time personas and then writes the brief + playbook
    snapshot + status.json)."""
    paths = _paths_from_args(args)
    if args.kind == "workflow":
        return _cmd_new_workflow(args, paths)
    return _cmd_new_task(args, paths)


def _cmd_new_task(args: argparse.Namespace, paths: JournalPaths) -> int:
    if not args.prd:
        print(
            "error: --prd is required for --kind task",
            file=sys.stderr,
        )
        return 2
    if args.playbook or args.task_brief or args.brief_file:
        print(
            "error: --playbook / --task-brief / --brief-file are "
            "workflow-only flags; use --kind workflow",
            file=sys.stderr,
        )
        return 2
    persona = args.persona
    cwd = Path.cwd()
    team_root = cwd if (cwd / "configs" / "personas.yaml").is_file() else None
    if not persona:
        # Fall back to the team's default_persona from personas.yaml
        # if available. The team root is the cwd convention (same
        # discovery rule the workflow-mode scaffolder uses below).
        if team_root is not None:
            default = resolve_default_persona(team_root)
            if default:
                persona = default
        if not persona:
            print(
                "error: --persona is required for --kind task (no "
                "default_persona configured in the team's "
                "configs/personas.yaml)",
                file=sys.stderr,
            )
            return 2

    # Symmetric with workflow mode: if we can identify a team root,
    # verify the persona's prompt.md actually exists on disk before
    # writing any artifact. Catches typos in both `--persona Typo`
    # (explicit) and `default_persona: Typo` (from yaml). When cwd is
    # NOT a team root we can't validate, and we accept the value as-is
    # for back-compat (the same posture Phase 1 had).
    if team_root is not None:
        from tigerharness.journal.scaffold import validate_personas
        missing = validate_personas(team_root, {persona})
        if missing:
            print(
                f"error: persona {persona!r} is not on team "
                f"{team_root.name} (no prompt.md under "
                f"{team_root / 'personas' / persona}/). Add the "
                "persona via `tigerharness init --persona <name>` "
                "first, or fix the typo (in --persona or in "
                "personas.yaml's default_persona).",
                file=sys.stderr,
            )
            return 2
    prd_path = Path(args.prd).expanduser()
    if not prd_path.exists():
        print(f"error: PRD not found: {prd_path}", file=sys.stderr)
        return 2
    prd_text = prd_path.read_text(encoding="utf-8")
    try:
        result = new_task(
            prd_text=prd_text,
            persona=persona,
            paths=paths,
            title=args.title,
            kind=args.kind,
            max_sessions=args.max_sessions if args.max_sessions is not None else 5,
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


def _cmd_new_workflow(args: argparse.Namespace, paths: JournalPaths) -> int:
    """Workflow-mode scaffolder. Validates flags, resolves the team
    root + playbook path, reads the brief (inline or from file), runs
    the persona pre-flight, and writes the workflow-mode artifacts."""
    if args.prd:
        print(
            "error: --prd is task-only; for --kind workflow use "
            "--task-brief or --brief-file",
            file=sys.stderr,
        )
        return 2
    if not args.playbook:
        print(
            "error: --playbook is required for --kind workflow",
            file=sys.stderr,
        )
        return 2
    if args.task_brief and args.brief_file:
        print(
            "error: --task-brief and --brief-file are mutually exclusive",
            file=sys.stderr,
        )
        return 2
    if not args.task_brief and not args.brief_file:
        print(
            "error: --task-brief or --brief-file is required for "
            "--kind workflow",
            file=sys.stderr,
        )
        return 2
    if args.persona:
        print(
            "error: --persona is task-only; for --kind workflow use "
            "--captain (the optional accountable owner)",
            file=sys.stderr,
        )
        return 2

    # Read brief.
    if args.task_brief:
        brief_text = args.task_brief
    else:
        brief_path = Path(args.brief_file).expanduser()
        if not brief_path.exists():
            print(
                f"error: brief file not found: {brief_path}",
                file=sys.stderr,
            )
            return 2
        brief_text = brief_path.read_text(encoding="utf-8")

    # Resolve team + playbook.
    team_root = resolve_team_root(args.team)
    playbook_path = team_root / "workflow" / f"{args.playbook}.md"
    if not playbook_path.is_file():
        print(
            f"error: playbook {args.playbook}.md not found under "
            f"{team_root / 'workflow'}/ (looked for {playbook_path})",
            file=sys.stderr,
        )
        return 2
    playbook_text = playbook_path.read_text(encoding="utf-8")

    # If --captain wasn't passed, fall back to the playbook's
    # `default_captain:` (HTML-comment YAML block). Alias-resolved
    # through the team's personas.yaml.
    captain = args.captain
    if captain is None:
        captain = resolve_playbook_default_captain(playbook_text, team_root)

    try:
        result = new_workflow_task(
            brief_text=brief_text,
            playbook_text=playbook_text,
            playbook_name=args.playbook,
            team_root=team_root,
            paths=paths,
            title=args.title,
            captain=captain,
            max_sessions=args.max_sessions if args.max_sessions is not None else 10,
            slug=args.slug,
        )
    except MissingPersonaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (JournalScaffoldError, JournalModelError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"Scaffolded: {result.task_id}")
    print(f"  title:        {result.status.title}")
    print(f"  kind:         {result.status.kind}")
    print(f"  captain:      {result.status.persona or '(none)'}")
    print(f"  playbook:     {args.playbook}")
    print(f"  team_root:    {team_root}")
    print(f"  max_sessions: {result.status.max_sessions}")
    print(f"  task_dir:     {result.task_dir}")
    print()
    print(
        "Open Claude Code and invoke the `drive-journal` skill to "
        "start the compile + walk."
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
        # Use Status.to_dict() (NOT dataclasses.asdict) so the per-kind
        # contract is preserved: task rows must NOT carry
        # compile_pending / compile_phase, and workflow rows must.
        # dataclasses.asdict ignores to_dict and would emit defaults
        # for missing fields, producing JSON that cannot round-trip
        # through Status.from_json.
        print(json.dumps(
            {
                "active": [s.to_dict() for s in rows],
                "malformed": malformed,
            },
            indent=2,
        ))
        return 0

    if not rows and not malformed:
        print("No active tasks.")
        return 0

    print(
        f"{'ID':40}  {'STATE':12}  {'KIND':8}  "
        f"{'PERSONA':12}  TITLE"
    )
    print(
        f"{'-'*40}  {'-'*12}  {'-'*8}  "
        f"{'-'*12}  -----"
    )
    for s in rows:
        state_cell = s.state.value
        # For workflows, surface compile state inline: a workflow that's
        # state=pending but compile_pending=True is meaningfully different
        # from one whose graph is already compiled.
        if s.kind == "workflow" and s.compile_phase is not None:
            state_cell = f"{s.state.value}/{s.compile_phase.value}"
        persona_cell = s.persona if s.persona else "(none)"
        print(
            f"{s.id:40}  {state_cell:12}  {s.kind:8}  "
            f"{persona_cell:12}  {s.title}"
        )
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
    print(json.dumps(status.to_dict(), indent=2))
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
            "Journal CLI for the subscription backend (Phase 1 + "
            "Phase 1.5 + Phase 2 + Phase 3). "
            "kind=task and kind=workflow; in-session compile with "
            "drafter + critic loop; compile-retry + append-steps for "
            "runtime extension. See docs/journal.md."
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

    n = sub.add_parser(
        "new",
        help=(
            "Scaffold a new task or workflow. Use --kind task (default) "
            "with --prd + --persona for task mode, OR --kind workflow "
            "with --playbook + --task-brief/--brief-file for workflow "
            "mode."
        ),
    )
    n.add_argument(
        "--kind", default="task", choices=["task", "workflow"],
        help=(
            "Task mode (single persona, free-form PRD) or workflow mode "
            "(multi-persona, compiled from a team playbook). Default: "
            "task."
        ),
    )
    # Task-mode flags
    n.add_argument(
        "--prd", default="",
        help=(
            "Path to the PRD / brief markdown file. Required for "
            "--kind task; forbidden for --kind workflow."
        ),
    )
    n.add_argument(
        "--persona", default="",
        help=(
            "The persona this task is assigned to. Required for "
            "--kind task; forbidden for --kind workflow (use --captain "
            "instead)."
        ),
    )
    # Workflow-mode flags
    n.add_argument(
        "--playbook", default="",
        help=(
            "Bare playbook name (resolves to "
            "teams/<team>/workflow/<name>.md). Required for "
            "--kind workflow."
        ),
    )
    n.add_argument(
        "--team", default="Shohoku",
        help=(
            "Team whose playbook + persona registry to use. Default: "
            "Shohoku. (Workflow mode only.)"
        ),
    )
    n.add_argument(
        "--task-brief", default="",
        help=(
            "Inline brief text for --kind workflow; mutually exclusive "
            "with --brief-file."
        ),
    )
    n.add_argument(
        "--brief-file", default="",
        help=(
            "Path to a brief markdown file for --kind workflow; "
            "mutually exclusive with --task-brief."
        ),
    )
    n.add_argument(
        "--captain", default=None,
        help=(
            "Optional accountable owner shown in `journal list`. "
            "Workflow mode only -- per-step personas come from the "
            "compiled graph. May be omitted for a no-captain workflow."
        ),
    )
    # Common flags
    n.add_argument(
        "--title", default="",
        help="Human label. Defaults to the first H1 of the brief/PRD.",
    )
    n.add_argument(
        "--slug", default="",
        help=(
            "Override the slug portion of the task id. Defaults to "
            "slugified title."
        ),
    )
    n.add_argument(
        "--max-sessions", type=int, default=None,
        help=(
            "Soft ceiling on drive-journal invocations. Default 5 for "
            "task mode and 10 for workflow mode (kind-specific default "
            "kicks in only when --max-sessions is unset)."
        ),
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

    _build_compile_subparsers(sub)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

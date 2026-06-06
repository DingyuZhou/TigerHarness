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
import os
import secrets
import sys
from pathlib import Path

from tigerharness.journal.compile_cli import build_subparsers as _build_compile_subparsers
from tigerharness.journal.models import (
    JournalModelError,
    State,
    Status,
    _utcnow_iso,
)
from tigerharness.journal.paths import (
    JournalPathError,
    JournalPaths,
    default_journal_root,
)
from tigerharness.journal.scaffold import (
    JournalScaffoldError,
    MissingPersonaError,
    _write_atomic,
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
    persona_came_from_default = False
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
                persona_came_from_default = True
        if not persona:
            print(
                "error: --persona is required for --kind task (no "
                "default_persona configured in the team's "
                "configs/personas.yaml)",
                file=sys.stderr,
            )
            return 2

    # Validate ONLY when persona came from the team's default_persona
    # fallback -- a yaml typo there silently affects every subsequent
    # scaffold and is hard to spot. An explicit `--persona Typo`
    # passes through unchanged (Phase 1 behaviour preserved): the
    # operator sees the late "no prompt.md" error from the task-
    # runner, which they typed seconds ago and can immediately
    # correct. Symmetric default-only validation keeps the safety net
    # without changing the stable CLI surface.
    if team_root is not None and persona_came_from_default:
        from tigerharness.journal.scaffold import validate_personas
        missing = validate_personas(team_root, {persona})
        if missing:
            print(
                f"error: default_persona {persona!r} (from "
                f"{team_root / 'configs' / 'personas.yaml'}) has no "
                f"prompt.md under "
                f"{team_root / 'personas' / persona}/. Fix the typo "
                "in personas.yaml's default_persona, or add the "
                "persona via `tigerharness init --persona <name>`.",
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
            max_sessions=args.max_sessions if args.max_sessions is not None else 3,
            early_exit=args.early_exit,
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
            early_exit=args.early_exit,
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
            "in_progress_idle": [s.id for s in result.in_progress_idle],
            "in_progress_busy": [s.id for s in result.in_progress_busy],
            "in_progress_crashed": [s.id for s in result.in_progress_crashed],
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
    if result.in_progress_busy:
        print()
        print("Busy (LEAVE ALONE -- a live session owns these):")
        for s in result.in_progress_busy:
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
# claim / release  (instant session hand-off; see docs/journal-instant-resume.md)
# ---------------------------------------------------------------------------

def _write_status_atomic(paths: JournalPaths, status: Status) -> None:
    """Write status.json atomically, reusing the scaffolder's canonical
    helper so claim/release share its crash-safe, concurrency-safe write
    path (unique same-dir temp + fsync + os.replace -- no fixed temp name
    that racing writers could clobber)."""
    _write_atomic(paths.status_json(status.id), status.to_json())


def cmd_claim(args: argparse.Namespace) -> int:
    """Atomically claim a task for this session (the pickup).

    Claimable = ``pending`` (start), or ``in_progress`` that is *idle*
    (detached) or *crashed* (attached but heartbeat stale). A *busy*
    task (attached + fresh) is refused -- a live session owns it.

    On success: set ``session_ref`` to a fresh token, flip to
    ``in_progress``, bump ``sessions``, refresh ``updated_at``, write
    atomically, then re-read and confirm our token won (compare-and-set).
    """
    paths = _paths_from_args(args)
    status = _read_status_or_none(paths, args.task_id)
    if status is None:
        print(f"error: no task with id {args.task_id!r} in {paths.active}",
              file=sys.stderr)
        return 1
    try:
        timeout = (args.stuck_timeout if args.stuck_timeout is not None
                   else stuck_timeout_from_env())
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if status.state in (State.DONE, State.BLOCKED):
        print(f"error: task is {status.state.value}; not claimable",
              file=sys.stderr)
        return 1
    if status.state is State.IN_PROGRESS:
        try:
            klass = status.in_progress_class(stuck_timeout_sec=timeout)
        except JournalModelError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if klass == "busy":
            print(
                f"error: task is busy -- a live session owns it "
                f"(session_ref={status.session_ref!r}). Not claimable.",
                file=sys.stderr,
            )
            return 1
        # idle or crashed -> claimable (resume / rescue)

    # Budget guard: a task already at its session cap must not be run
    # past it. A crash/clean-stop that left an at-cap task in_progress
    # (instead of blocked) could otherwise be re-claimed forever, and the
    # driver's `sessions >= max_sessions` stop would overshoot a one-off
    # bump. Self-heal: mark it blocked (so it stops surfacing as
    # actionable) and refuse the claim.
    if status.sessions >= status.max_sessions:
        status.state = State.BLOCKED
        status.session_ref = None
        status.next_action = (
            f"hit session cap ({status.sessions}/{status.max_sessions}); "
            f"re-scaffold with a higher --max-sessions, or `journal abort` "
            f"(workflow) / close the task (there is no raise-cap CLI for an "
            f"existing task)"
        )
        status.updated_at = _utcnow_iso()
        _write_status_atomic(paths, status)
        print(
            f"error: task at session cap "
            f"({status.sessions}/{status.max_sessions}); marked blocked.",
            file=sys.stderr,
        )
        return 1

    token = secrets.token_hex(8)
    status.state = State.IN_PROGRESS
    status.session_ref = token
    status.sessions += 1
    status.updated_at = _utcnow_iso()
    _write_status_atomic(paths, status)

    # Compare-and-set check: re-read and confirm our token is on disk. If
    # a concurrent claim won the last write we see a different token and
    # back off. This is NOT a full mutex -- a tight write/read/write/read
    # interleaving can still let two claims both believe they won. The
    # design accepts that for the low-concurrency interactive model;
    # flock is the upgrade path (see docs/journal-instant-resume.md).
    confirm = _read_status_or_none(paths, args.task_id)
    if confirm is None or confirm.session_ref != token:
        print(
            "error: claim lost -- another session claimed this task "
            "concurrently. Re-sweep and pick again.",
            file=sys.stderr,
        )
        return 1

    if getattr(args, "format", "text") == "json":
        print(json.dumps({
            "task_id": status.id,
            "session_ref": token,
            "sessions": status.sessions,
            "max_sessions": status.max_sessions,
            "kind": status.kind,
        }, indent=2))
    else:
        print(f"Claimed {status.id}")
        print(f"  session_ref: {token}")
        print(f"  sessions:    {status.sessions}/{status.max_sessions}")
        print(f"  kind:        {status.kind}")
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    """Release this session's hold on a task (the hand-off / stop).

    Clears ``session_ref`` (detach) so the next session can resume the
    task **immediately** -- no heartbeat wait. Sets the exit ``--state``
    (default ``in_progress`` = clean stop; ``done`` / ``blocked`` for
    terminal stops) and optional ``--next-action``, then refreshes
    ``updated_at``.

    If ``--session-ref`` is given it must match the current holder, so a
    stray release can't detach someone else's live task.
    """
    paths = _paths_from_args(args)
    status = _read_status_or_none(paths, args.task_id)
    if status is None:
        print(f"error: no task with id {args.task_id!r} in {paths.active}",
              file=sys.stderr)
        return 1
    if status.state is State.DONE:
        # Don't resurrect a terminal task. `release` is the driver's
        # in_progress -> exit transition; a done task is awaiting archive,
        # and flipping it back would re-surface it as actionable.
        print("error: task is done; refusing to release (would resurrect "
              "an archived/terminal task).", file=sys.stderr)
        return 1
    if args.session_ref is not None and status.session_ref != args.session_ref:
        print(
            f"error: --session-ref {args.session_ref!r} does not match the "
            f"current holder {status.session_ref!r}; refusing to release.",
            file=sys.stderr,
        )
        return 1
    try:
        new_state = State(args.state)
    except ValueError:
        print(f"error: invalid --state {args.state!r}", file=sys.stderr)
        return 2

    status.state = new_state
    status.session_ref = None  # detach -> instantly resumable if still in_progress
    if args.next_action is not None:
        status.next_action = args.next_action
    status.updated_at = _utcnow_iso()
    _write_status_atomic(paths, status)

    print(f"Released {status.id} (state={new_state.value}, detached)")
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
            "Soft ceiling on drive-journal invocations. Default 3 for "
            "task mode and 10 for workflow mode (kind-specific default "
            "kicks in only when --max-sessions is unset)."
        ),
    )
    n.add_argument(
        "--early-exit", action="store_true", default=False,
        help=(
            "Allow the driver to stop early once the task is done per "
            "acceptance criteria. Default off -> run the full "
            "max_sessions budget (N iterations = exactly N). Mirrors the "
            "task-runner's --early-exit."
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
            "in_progress as idle/busy/crashed, summarise. Side-effecting."
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

    cl = sub.add_parser(
        "claim",
        help=(
            "Atomically claim a task for this session (the pickup): set "
            "session_ref, flip to in_progress, bump sessions. Resumes an "
            "idle/crashed task or starts a pending one; refuses a busy one."
        ),
    )
    cl.add_argument("task_id")
    cl.add_argument("--format", choices=["text", "json"], default="text")
    cl.add_argument(
        "--stuck-timeout", type=int, default=None,
        help=(
            "Heartbeat age past which an attached task counts as crashed "
            "(and thus claimable). Defaults like sweep's."
        ),
    )
    cl.set_defaults(func=cmd_claim)

    rl = sub.add_parser(
        "release",
        help=(
            "Release this session's hold on a task (the hand-off / stop): "
            "clear session_ref so the next session resumes instantly. Use "
            "--state done/blocked for terminal stops."
        ),
    )
    rl.add_argument("task_id")
    rl.add_argument(
        "--state", choices=["in_progress", "done", "blocked"],
        default="in_progress",
        help="Exit state. Default in_progress (clean stop; resumable now).",
    )
    rl.add_argument("--next-action", default=None)
    rl.add_argument(
        "--session-ref", default=None,
        help="If given, must match the current holder before releasing.",
    )
    rl.set_defaults(func=cmd_release)

    _build_compile_subparsers(sub)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

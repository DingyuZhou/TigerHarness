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

import logging

import argparse
import json
import re
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
from tigerharness.journal import drive_sessions, walk, worklog

log = logging.getLogger("tigerharness.journal.cli")


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
            autonomy=args.autonomy,
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
            autonomy=args.autonomy,
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


def _write_driver_claim_entry(
    paths: JournalPaths, status: Status, reason: str, driver: str,
) -> None:
    """Write the thin DRIVER worklog entry for a real claim (the "I drove
    this" record that lands in the driver persona's memory). Best-effort:
    a disk hiccup here must not fail an otherwise-successful claim, so an
    ``OSError`` is warned-and-swallowed. The substantive per-persona work
    note is written later (release / step-done) and IS hard-gated."""
    try:
        worklog.write_entry(paths, worklog.WorklogEntry(
            task_id=status.id,
            persona=driver,
            step="drive",
            kind=status.kind,
            role="driver",
            objective=status.title,
            reason=reason,
            started_at=_utcnow_iso(),
            body=(
                f"Drove `{status.id}` ({reason}); "
                f"session {status.sessions}/{status.max_sessions}; "
                f"kind={status.kind}."
            ),
        ))
    except OSError as exc:
        print(
            f"warning: could not write driver worklog entry for "
            f"{status.id}: {exc}",
            file=sys.stderr,
        )


def _write_task_work_entry(
    paths: JournalPaths, status: Status, output_path: str | None,
) -> int:
    """Write the assigned persona's kind=task work note from
    ``--output``, returning 0 on success or a non-zero CLI code on
    refusal. Unlike the driver claim trace, this is a HARD gate: a
    missing, unreadable, or empty note refuses the done-transition so a
    task cannot be marked done without leaving the persona's memory
    record. Stamps ``persona`` from ``status.persona`` (the assigned
    persona) so the attribution can't be wrong."""
    if not output_path:
        print(
            "error: --output <file> is required to mark a kind=task done "
            "in a drive (the assigned persona's work note is the ticket to "
            "advance). Refusing to mark done.",
            file=sys.stderr,
        )
        return 1
    try:
        output_text = Path(output_path).read_text(encoding="utf-8")
    except OSError as exc:
        print(
            f"error: cannot read --output {output_path!r}: {exc}. "
            f"Refusing to mark done.",
            file=sys.stderr,
        )
        return 1
    if not output_text.strip():
        log.warning("release done-gate refused: empty note at %s (the note is the ticket)", output_path)
        print(
            f"error: --output {output_path!r} is empty; the work note "
            f"cannot be blank. Refusing to mark done.",
            file=sys.stderr,
        )
        return 1
    # Write the note BEFORE cmd_release mutates state to done. A disk
    # failure here must refuse cleanly (return 1) -- matching the
    # step-done gate -- rather than escape as a traceback out of an
    # un-guarded main(): the task then stays in_progress and resumable,
    # and the driver gets an actionable message instead of a stack trace.
    try:
        worklog.write_entry(paths, worklog.WorklogEntry(
            task_id=status.id,
            persona=status.persona,
            step="task-work",
            kind="task",
            objective=status.title,
            ended_at=_utcnow_iso(),
            body=output_text,
        ))
    except OSError as exc:
        print(
            f"error: could not write the work note for {status.id}: {exc}. "
            "Refusing to mark done (the task stays in_progress and "
            "resumable).",
            file=sys.stderr,
        )
        return 1
    return 0


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
        log.warning("claim refused: %s is %s", args.task_id, status.state.value)
        print(f"error: task is {status.state.value}; not claimable",
              file=sys.stderr)
        return 1
    if status.state is State.IN_PROGRESS:
        try:
            klass = status.in_progress_class(stuck_timeout_sec=timeout)
        except JournalModelError as exc:  # pragma: no cover -- unreachable:
            # in_progress_class only raises for a non-in_progress state,
            # which this branch has already excluded.
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if klass == "busy":
            log.warning("claim refused: %s busy (live session holds the lease)", args.task_id)
            print(
                f"error: task is busy -- a live session owns it "
                f"(session_ref={status.session_ref!r}). Not claimable.",
                file=sys.stderr,
            )
            return 1
        # idle or crashed -> claimable (resume / rescue)

    # Classify the pickup for the driver's thin worklog entry (only used
    # when --driver is given). Captured BEFORE the state mutation below.
    # DONE/BLOCKED already returned, so the state is PENDING or
    # IN_PROGRESS (idle/crashed); the ternary short-circuits so ``klass``
    # is only read when it is defined.
    claim_reason = (
        "new" if status.state is State.PENDING
        else ("resume" if klass == "idle" else "rescue")
    )

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
        log.warning("claim lost: %s taken by a concurrent session", args.task_id)
        print(
            "error: claim lost -- another session claimed this task "
            "concurrently. Re-sweep and pick again.",
            file=sys.stderr,
        )
        return 1

    # Drive context only: leave the thin driver trace. No --driver (the
    # plain subscription backend) -> no worklog side-effect at all.
    if getattr(args, "driver", None):
        _write_driver_claim_entry(paths, status, claim_reason, args.driver)

    # Drive context only: register this drive session's Slack thread so
    # tiger-memory's claude_transcript adapter skips its (fat) transcript
    # -- the per-persona worklog now owns that content (see
    # docs/per-persona-journal-memory.md section 4). Best-effort: a
    # registry write failure must not fail an otherwise-successful claim.
    #
    # Two ways the thread_ts arrives, explicit beating implicit:
    #   1. ``--drive-thread`` -- explicit; honored regardless of --driver
    #      (a caller asking to register means it).
    #   2. ``TIGERHARNESS_SLACK_THREAD_TS`` env var -- the harness-enforced
    #      fallback the slack bridge sets on every turn. Gated on
    #      ``--driver`` so it only fires inside a real drive (where the
    #      per-persona worklog replaces the transcript); a plain
    #      subscription turn that happens to claim without --driver must
    #      NOT suppress its own transcript (there is no worklog to replace
    #      it). The agent no longer needs to copy the thread_ts by hand.
    drive_thread = getattr(args, "drive_thread", None)
    if not drive_thread and getattr(args, "driver", None):
        drive_thread = os.environ.get("TIGERHARNESS_SLACK_THREAD_TS") or None
    if drive_thread:
        try:
            drive_sessions.register(
                paths, drive_thread,
                task_id=status.id, driver=args.driver,
            )
        except OSError as exc:
            print(
                f"warning: could not register drive session "
                f"{drive_thread} for {status.id}: {exc}",
                file=sys.stderr,
            )

    if getattr(args, "format", "text") == "json":
        print(json.dumps({
            "task_id": status.id,
            "session_ref": token,
            "sessions": status.sessions,
            "max_sessions": status.max_sessions,
            "kind": status.kind,
        }, indent=2))
    else:
        log.info("claimed %s session_ref=%s sessions=%d/%d kind=%s",
                 status.id, token, status.sessions, status.max_sessions,
                 status.kind.value if hasattr(status.kind, "value") else status.kind)
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
        log.warning("release refused: %s already done", args.task_id)
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
    except ValueError:  # pragma: no cover -- unreachable: argparse `choices`
        # already restricts --state to valid State values.
        print(f"error: invalid --state {args.state!r}", file=sys.stderr)
        return 2

    # Completion gate (drive context only). Marking a kind=task DONE
    # requires the assigned persona's work note via --output: the note is
    # the ticket to advance. The note is written to that persona's memory
    # as the task-work worklog entry. Refusals happen BEFORE any state
    # mutation, so the task stays in_progress and remains resumable.
    if (getattr(args, "driver", None)
            and status.kind == "task"
            and new_state is State.DONE):
        rc = _write_task_work_entry(paths, status, args.output)
        if rc != 0:
            return rc

    # Completion gate for kind=workflow (drive context only). Marking a
    # workflow DONE requires the graph walk to have reached __done__ --
    # i.e. every step that ran left its persona-attributed worklog entry
    # via `journal step-done`. The per-step notes ARE the gate; --output
    # is task-only. Refusal happens before any state mutation.
    if (getattr(args, "driver", None)
            and status.kind == "workflow"
            and new_state is State.DONE):
        rc = _check_workflow_walk_complete(paths, status)
        if rc != 0:
            return rc

    status.state = new_state
    status.session_ref = None  # detach -> instantly resumable if still in_progress
    if args.next_action is not None:
        status.next_action = args.next_action
    status.updated_at = _utcnow_iso()
    _write_status_atomic(paths, status)

    log.info("released %s state=%s detached", status.id, new_state.value)
    print(f"Released {status.id} (state={new_state.value}, detached)")
    return 0


# ---------------------------------------------------------------------------
# step-done  (kind=workflow graph-walk gate)
# See docs/per-persona-journal-memory.md.
# ---------------------------------------------------------------------------

def _load_orchestration(paths: JournalPaths, task_id: str):
    """Load + parse a workflow's orchestration.json. Returns the
    ``Orchestration`` on success or a human-readable error string. The
    wfcore import is lazy (mirrors the compile CLIs) so the
    pure-task journal paths never pull it in."""
    from tigerharness.journal.wfcore.models import (
        Orchestration,
        WorkflowModelError,
    )

    orch_path = paths.task_dir(task_id) / "orchestration.json"
    try:
        data = json.loads(orch_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return (
            f"no orchestration.json for {task_id!r}; the graph is not "
            "landed (run the compile sub-protocol first)"
        )
    except (OSError, json.JSONDecodeError) as exc:
        return f"cannot read orchestration.json for {task_id!r}: {exc}"
    try:
        return Orchestration.from_dict(data)
    except WorkflowModelError as exc:
        return f"orchestration.json for {task_id!r} is malformed: {exc}"


def _read_step_persona_role(
    paths: JournalPaths, task_id: str, step_id: str,
) -> tuple[str | None, str | None]:
    """Read ``(persona, role)`` from a compiled step file. Returns
    ``(None, None)`` and prints an error on failure. Reuses the compile
    machinery's step-file reader (yaml-backed) -- acceptable because
    step-done is workflow-only, which already requires the compile
    path. The persona/role come from the compiled file, never from a
    flag, so a turn cannot mis-file its own memory."""
    from tigerharness.journal.compile_cli import _read_existing_step

    task_dir = paths.task_dir(task_id)
    try:
        step = _read_existing_step(task_dir, step_id)
    except Exception as exc:  # ValueError / WorkflowModelError / OSError
        print(
            f"error: cannot read step file for {step_id!r}: {exc}",
            file=sys.stderr,
        )
        return None, None
    return step.persona, step.role


def cmd_step_done(args: argparse.Namespace) -> int:
    """Advance a kind=workflow graph walk by one step (the per-step gate).

    The workflow analogue of the kind=task release gate: a step turn
    cannot advance the walk without leaving the acting persona's worklog
    entry. The persona/role are read from the compiled step file
    (``steps/<id>.md``), never typed free-hand, so a turn can't mis-file
    its own memory. The walk cursor in ``walk.json`` is validated
    in-order, so a session can't skip a step or fabricate progress.

    Flow: validate kind=workflow + compile_phase=complete + (optional)
    session-ref; require a non-empty ``--output`` (the note is the
    ticket); load orchestration.json + walk.json (lazy-init the cursor to
    the entrypoint on the first call); verify ``--step`` is the expected
    current step and the walk isn't already terminal; resolve the edge
    target for ``--verdict``; read the step's persona/role; write the
    worklog entry FIRST (so a crash can't advance without a note), then
    advance the cursor.
    """
    paths = _paths_from_args(args)
    status = _read_status_or_none(paths, args.task)
    if status is None:
        print(f"error: no task with id {args.task!r} in {paths.active}",
              file=sys.stderr)
        return 1
    if status.kind != "workflow":
        print(
            f"error: step-done is workflow-only; task {status.id!r} is "
            f"kind={status.kind} (use `release` for a kind=task).",
            file=sys.stderr,
        )
        return 1
    from tigerharness.journal.models import CompilePhase
    if status.compile_phase != CompilePhase.COMPLETE:
        phase = status.compile_phase.value if status.compile_phase else "n/a"
        print(
            f"error: task {status.id!r} is compile_phase={phase}; step-done "
            "needs a landed graph (compile_phase=complete).",
            file=sys.stderr,
        )
        return 1
    if args.session_ref is not None and status.session_ref != args.session_ref:
        print(
            f"error: --session-ref {args.session_ref!r} does not match the "
            f"current holder {status.session_ref!r}; refusing to advance.",
            file=sys.stderr,
        )
        return 1

    # The note is the ticket: require a non-empty --output before any
    # walk mutation. (--output is argparse-required, so it is never None.)
    try:
        output_text = Path(args.output).read_text(encoding="utf-8")
    except OSError as exc:
        print(
            f"error: cannot read --output {args.output!r}: {exc}. Refusing "
            "to advance.",
            file=sys.stderr,
        )
        return 1
    if not output_text.strip():
        log.warning("step-done refused: %s empty note (the note is the ticket)", args.step)
        print(
            f"error: --output {args.output!r} is empty; the step note "
            "cannot be blank. Refusing to advance.",
            file=sys.stderr,
        )
        return 1

    orch_or_err = _load_orchestration(paths, status.id)
    if isinstance(orch_or_err, str):
        print(f"error: {orch_or_err}", file=sys.stderr)
        return 1
    orchestration = orch_or_err
    if args.step not in orchestration.steps:
        print(
            f"error: step {args.step!r} is not in the compiled graph "
            f"({sorted(orchestration.steps)}).",
            file=sys.stderr,
        )
        return 1

    # Load or lazy-init the walk cursor.
    try:
        state = walk.read(paths, status.id)
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"error: cannot read walk.json for {status.id}: {exc}. Refusing "
            "to advance.",
            file=sys.stderr,
        )
        return 1
    if state is None:
        state = walk.initial(status.id, orchestration.entrypoint)

    if state.current in walk.SENTINELS:
        log.warning("step-done refused: walk already terminal at %s", state.current)
        print(
            f"error: walk already terminal (current={state.current!r}); "
            "refusing further step-done.",
            file=sys.stderr,
        )
        return 1
    if args.step != state.current:
        log.warning("step-done refused: out-of-order (walk at %s, got %s)",
                    state.current, args.step)
        print(
            f"error: out-of-order step: the walk is at {state.current!r} but "
            f"--step is {args.step!r}; drive the current step (or check the "
            "graph).",
            file=sys.stderr,
        )
        return 1

    edge = orchestration.edges.get(args.step)
    if edge is None:
        print(
            f"error: the compiled graph has no edges for step {args.step!r}; "
            "orchestration.json may be malformed.",
            file=sys.stderr,
        )
        return 1
    next_step = {
        "APPROVE": edge.on_approve,
        "REVISE": edge.on_revise,
        "BLOCK": edge.on_block,
    }[args.verdict]

    persona, role = _read_step_persona_role(paths, status.id, args.step)
    if persona is None:
        return 1  # _read_step_persona_role printed the error

    # Write the worklog entry FIRST (the note is the ticket); only then
    # advance the cursor. If the worklog write fails we do NOT advance, so
    # the session retries the same step rather than losing the note. The
    # reverse window (note written, advance fails) at worst duplicates a
    # note on retry -- preferable to a missed one.
    try:
        stamped = worklog.write_entry(paths, worklog.WorklogEntry(
            task_id=status.id,
            persona=persona,
            step=args.step,
            kind="workflow",
            role=role,
            objective=status.title,
            verdict=args.verdict,
            ended_at=_utcnow_iso(),
            body=output_text,
        ))
    except OSError as exc:
        print(
            f"error: could not write worklog entry for step {args.step!r}: "
            f"{exc}. Walk not advanced.",
            file=sys.stderr,
        )
        return 1

    new_state = walk.advance(
        state, step=args.step, verdict=args.verdict, next_step=next_step,
    )
    walk.write(paths, new_state)

    terminal = next_step in walk.SENTINELS
    if getattr(args, "format", "text") == "json":
        print(json.dumps({
            "task_id": status.id,
            "step": args.step,
            "verdict": args.verdict,
            "persona": persona,
            "role": role,
            "next": next_step,
            "terminal": terminal,
            "worklog_seq": stamped.seq,
            "worklog_path": str(stamped.path),
        }, indent=2))
    else:
        log.info("step-done: %s verdict=%s persona=%s next=%s",
                 args.step, args.verdict, persona, next_step)
        print(f"step-done: {args.step} ({args.verdict}) by {persona}")
        print(f"  worklog: {stamped.path}")
        if next_step == walk.DONE:
            print("  next: __done__ (walk complete -- release --state done)")
        elif next_step == walk.ESCALATE:
            print(
                "  next: __escalate__ (escalated -- release --state blocked)"
            )
        else:
            print(f"  next: {next_step}")
    return 0


def _check_workflow_walk_complete(
    paths: JournalPaths, status: Status,
) -> int:
    """Backstop for marking a kind=workflow done in a drive: the graph
    walk must have reached ``__done__`` (every step that ran left its
    persona-attributed worklog entry via step-done). Returns 0 to allow,
    non-zero to refuse (with the error already printed) so a workflow
    can't be closed with steps' memory unrecorded."""
    try:
        state = walk.read(paths, status.id)
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"error: cannot read walk.json for {status.id}: {exc}. Refusing "
            "to mark done.",
            file=sys.stderr,
        )
        return 1
    if state is None:
        print(
            "error: workflow has no walk state (no `journal step-done` calls "
            "yet); refusing to mark done. Drive the graph to __done__ first.",
            file=sys.stderr,
        )
        return 1
    if state.current != walk.DONE:
        print(
            f"error: workflow walk is at {state.current!r}, not __done__; "
            "refusing to mark done. Finish the remaining steps via `journal "
            "step-done`, or release --state blocked to escalate.",
            file=sys.stderr,
        )
        return 1
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# T8: schedule verbs (add / list / rm)
# ---------------------------------------------------------------------------

def cmd_schedule_add(args: argparse.Namespace) -> int:
    """Create a recurring definition. The prd/brief file is read NOW
    and inlined, so a definition can never dangle on a moved file."""
    import datetime as _dt

    from tigerharness.journal.schedule import (
        ScheduleDef,
        ScheduleDefError,
        def_path,
        next_occurrence,
        save_def,
    )

    paths = _paths_from_args(args)
    payload: dict = {
        "kind": args.kind,
        "max_sessions": args.max_sessions,
        "early_exit": args.early_exit,
        "autonomy": args.autonomy,
    }
    if args.kind == "task":
        if not args.prd or not args.persona:
            print("error: --kind task needs --prd and --persona",
                  file=sys.stderr)
            return 2
        prd_path = Path(args.prd)
        if not prd_path.exists():
            print(f"error: PRD not found: {prd_path}", file=sys.stderr)
            return 2
        payload["prd_text"] = prd_path.read_text(encoding="utf-8")
        payload["persona"] = args.persona
    else:
        if not args.playbook or not (args.task_brief or args.brief_file):
            print(
                "error: --kind workflow needs --playbook and "
                "--task-brief or --brief-file",
                file=sys.stderr,
            )
            return 2
        if args.task_brief and args.brief_file:
            print("error: --task-brief and --brief-file are mutually "
                  "exclusive", file=sys.stderr)
            return 2
        team_root = resolve_team_root(args.team)
        playbook_path = (
            team_root / "workflow" / f"{args.playbook}.md"
        )
        if not playbook_path.exists():
            print(f"error: playbook not found: {playbook_path}",
                  file=sys.stderr)
            return 2
        if args.brief_file:
            brief_path = Path(args.brief_file)
            if not brief_path.exists():
                print(f"error: brief not found: {brief_path}",
                      file=sys.stderr)
                return 2
            payload["brief_text"] = brief_path.read_text(encoding="utf-8")
        else:
            payload["brief_text"] = args.task_brief
        payload["playbook"] = args.playbook
        payload["playbook_text"] = playbook_path.read_text(encoding="utf-8")
        payload["team_root"] = str(team_root)
        if args.captain:
            payload["captain"] = args.captain

    def_id = re.sub(r"[^A-Za-z0-9_-]+", "-", args.title.strip().lower())
    def_id = re.sub(r"-{2,}", "-", def_id).strip("-") or "schedule"
    if def_path(paths, def_id).exists():
        print(f"error: definition {def_id!r} already exists "
              f"(rm it first)", file=sys.stderr)
        return 1
    now_utc = _dt.datetime.now(_dt.timezone.utc)
    try:
        first_due = next_occurrence(
            now_utc, period=args.period, at=args.at,
        )
        d = ScheduleDef(
            id=def_id,
            title=args.title.strip(),
            period=args.period,
            at=args.at,
            next_due=first_due.replace(microsecond=0)
            .isoformat().replace("+00:00", "Z"),
            payload=payload,
        )
        save_def(paths, d)
    except ScheduleDefError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Scheduled: {def_id}")
    print(f"  title:    {d.title}")
    print(f"  cadence:  {d.period} at {d.at} (local wall clock)")
    print(f"  next_due: {d.next_due}")
    return 0


def cmd_schedule_list(args: argparse.Namespace) -> int:
    from tigerharness.journal.schedule import (
        ScheduleDef,
        ScheduleDefError,
        def_path,
        list_def_ids,
    )

    paths = _paths_from_args(args)
    rows = []
    broken = []
    for def_id in list_def_ids(paths):
        try:
            rows.append(ScheduleDef.from_json(
                def_path(paths, def_id).read_text(encoding="utf-8")
            ))
        except (ScheduleDefError, OSError) as exc:
            broken.append((def_id, str(exc)))
    if args.format == "json":
        print(json.dumps({
            "definitions": [r.to_dict() for r in rows],
            "malformed": [
                {"id": i, "error": e} for (i, e) in broken
            ],
        }, indent=2))
        return 0
    if not rows and not broken:
        print("No schedule definitions.")
        return 0
    for r in rows:
        flag = "" if r.enabled else "  [disabled]"
        print(f"{r.id}  {r.period}@{r.at}  next_due={r.next_due}"
              f"{flag}  {r.title}")
    for def_id, err in broken:
        print(f"{def_id}  [malformed: {err}]")
    return 0


def cmd_schedule_rm(args: argparse.Namespace) -> int:
    import datetime as _dt

    from tigerharness.journal.schedule import (
        ScheduleDef,
        ScheduleDefError,
        _intent_stale,
        def_path,
    )
    from tigerharness.journal.sweep import stuck_timeout_from_env

    paths = _paths_from_args(args)
    p = def_path(paths, args.def_id)
    if not p.exists():
        print(f"error: no definition {args.def_id!r}", file=sys.stderr)
        return 1
    try:
        d = ScheduleDef.from_json(p.read_text(encoding="utf-8"))
        if d.materializing is not None and not _intent_stale(
            d.materializing,
            _dt.datetime.now(_dt.timezone.utc),
            stuck_timeout_from_env(),
        ):
            print(
                f"error: definition {args.def_id!r} is mid-"
                f"materialization (fresh lease); retry shortly",
                file=sys.stderr,
            )
            return 1
    except ScheduleDefError:
        pass  # malformed is still removable -- that's the point of rm
    p.unlink()
    print(f"Removed: {args.def_id}")
    return 0


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
            "retired runner's --early-exit."
        ),
    )
    n.add_argument(
        "--autonomy", choices=("ask", "judgement"), default="ask",
        help=(
            "Detached-run autonomy level. 'ask' (default): the persona "
            "pauses on judgment calls per its prompt/charter rules. "
            "'judgement': the persona may self-resolve yellow-light "
            "calls, logging each as a Decision: entry; red-light rules "
            "are never overridable (see the team charter)."
        ),
    )
    n.set_defaults(func=cmd_new)

    sch = sub.add_parser(
        "schedule",
        help="Recurring task definitions materialized by the sweep.",
    )
    sch_sub = sch.add_subparsers(dest="schedule_cmd", required=True)

    sa = sch_sub.add_parser("add", help="Add a recurring definition.")
    sa.add_argument("--title", required=True)
    sa.add_argument("--period", choices=("daily", "weekly"),
                    default="daily")
    sa.add_argument("--at", required=True,
                    help="HH:MM, local wall clock (DST-safe).")
    sa.add_argument("--kind", choices=("task", "workflow"),
                    default="task")
    sa.add_argument("--prd", help="PRD file (kind=task); inlined now.")
    sa.add_argument("--persona", help="Assignee (kind=task).")
    sa.add_argument("--playbook", help="Bare playbook name (workflow).")
    sa.add_argument("--task-brief", help="Inline brief (workflow).")
    sa.add_argument("--brief-file", help="Brief file (workflow); "
                    "inlined now.")
    sa.add_argument("--captain", help="Accountable owner (workflow).")
    sa.add_argument("--team", default="Shohoku",
                    help="Team for playbook resolution (workflow).")
    sa.add_argument("--max-sessions", type=int, default=None)
    sa.add_argument("--early-exit", action="store_true", default=False)
    sa.add_argument("--autonomy", choices=("ask", "judgement"),
                    default="ask")
    sa.set_defaults(func=cmd_schedule_add)

    sl = sch_sub.add_parser("list", help="List definitions.")
    sl.add_argument("--format", choices=("text", "json"),
                    default="text")
    sl.set_defaults(func=cmd_schedule_list)

    sr = sch_sub.add_parser("rm", help="Remove a definition.")
    sr.add_argument("def_id")
    sr.set_defaults(func=cmd_schedule_rm)

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
    cl.add_argument(
        "--driver", default=None,
        help=(
            "Persona name of the drive-journal session. When given, a "
            "thin driver worklog entry (the 'I drove this' record) is "
            "written to that persona's memory. Omit for the plain "
            "subscription backend (no worklog side-effect)."
        ),
    )
    cl.add_argument(
        "--drive-thread", default=None,
        help=(
            "Slack thread_ts of the drive-journal session. When given, the "
            "thread is recorded to the drive-session registry so "
            "tiger-memory skips this drive's transcript (the per-persona "
            "worklog owns that content). Usually unnecessary under the "
            "slack bridge: with --driver set, the thread_ts is read "
            "automatically from the TIGERHARNESS_SLACK_THREAD_TS env var "
            "the bridge sets. Pass it explicitly only to override that or "
            "outside the bridge."
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
    rl.add_argument(
        "--driver", default=None,
        help=(
            "Persona name of the drive-journal session. Enables the "
            "per-persona memory side-effects: marking a kind=task done "
            "then requires --output (the assigned persona's work note). "
            "Omit for the plain subscription backend."
        ),
    )
    rl.add_argument(
        "--output", default=None,
        help=(
            "Path to the assigned persona's work note (markdown). Required "
            "to mark a kind=task done in a drive; written to that "
            "persona's memory as the task-work worklog entry."
        ),
    )
    rl.set_defaults(func=cmd_release)

    sd = sub.add_parser(
        "step-done",
        help=(
            "Advance a kind=workflow graph walk by one step: write the "
            "acting persona's worklog entry (persona/role read from the "
            "compiled step file) and move the walk cursor to the verdict's "
            "edge target. The per-step counterpart to the kind=task release "
            "completion gate."
        ),
    )
    sd.add_argument("--task", required=True)
    sd.add_argument(
        "--step", required=True,
        help=(
            "The step id just completed. Must be the walk's current step "
            "(the entrypoint on the first call); out-of-order is refused."
        ),
    )
    sd.add_argument(
        "--verdict", required=True, choices=["APPROVE", "REVISE", "BLOCK"],
        help=(
            "Routing verdict; selects the edge "
            "(on_approve / on_revise / on_block) to the next step."
        ),
    )
    sd.add_argument(
        "--output", required=True,
        help=(
            "Path to the acting persona's step note (markdown). Required "
            "and must be non-empty: the note is the ticket to advance. "
            "Written to that persona's memory as the worklog entry."
        ),
    )
    sd.add_argument(
        "--session-ref", default=None,
        help="If given, must match the current holder before advancing.",
    )
    sd.add_argument("--format", choices=["text", "json"], default="text")
    sd.set_defaults(func=cmd_step_done)

    _build_compile_subparsers(sub)

    return p


def main(argv: list[str] | None = None) -> int:
    from tigerharness._logging import configure_cli_logging
    configure_cli_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

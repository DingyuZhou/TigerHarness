"""Compile-side CLI subcommands for the workflow-mode journal (Phase 1.5).

These commands are the Python primitives the interactive
``drive-journal`` session shells out to during an in-session compile.
All are **pure Python** -- no LLM calls, no ``claude -p``. The session
itself does the reasoning; these CLIs just stage inputs, validate
drafts, and atomically promote the final compiled artifacts.

The six subcommands (see :func:`build_subparsers`):

- ``compile-context <task-id>``
- ``compile-prompts --task <id> --kind {drafter|akagi|ayako} ...``
- ``validate-graph --task <id> --draft <path>``
- ``land-compile --task <id> --draft <path> --transcript <path> --rounds <N>``
- ``abort <task-id>``
- ``validate-personas <team>``

The contract for each is fixed by ``OPERATING.md`` so the session can
shell out from a static protocol without reading source.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from tigerharness.journal.models import (
    CompilePhase,
    JournalModelError,
    State,
    Status,
)
from tigerharness.journal.paths import (
    JournalPathError,
    JournalPaths,
    default_journal_root,
)
from tigerharness.journal.scaffold import (
    COMPILE_PERSONAS,
    extract_persona_refs_from_playbook,
    read_team_roster,
    resolve_team_root,
    validate_personas,
)


# ---------------------------------------------------------------------------
# Helpers (shared by every compile subcommand)
# ---------------------------------------------------------------------------

def _paths_from_args(args: argparse.Namespace) -> JournalPaths:
    """Resolve the journal root from ``--journal-dir`` or env default."""
    if getattr(args, "journal_dir", "") and args.journal_dir:
        root = Path(args.journal_dir).expanduser().resolve()
    else:
        root = default_journal_root()
    return JournalPaths(root=root)


def _load_workflow_status(
    paths: JournalPaths, task_id: str,
) -> Status | str:
    """Read + parse a workflow task's status.json. Returns the Status on
    success or a human-readable error string on failure (so the caller
    can print to stderr without raising)."""
    try:
        status = Status.from_json(paths.status_json(task_id).read_text())
    except JournalPathError as exc:
        return f"task id {task_id!r} is not path-safe: {exc}"
    except FileNotFoundError:
        return (
            f"no active workflow task with id {task_id!r} at "
            f"{paths.status_json(task_id)}"
        )
    except JournalModelError as exc:
        return f"status.json for {task_id!r} is malformed: {exc}"
    if status.kind != "workflow":
        return (
            f"task {task_id!r} has kind={status.kind!r}; this command "
            "only operates on kind=workflow tasks"
        )
    return status


def _compile_dir(paths: JournalPaths, task_id: str) -> Path:
    """Return the in-flight ``compile/`` workspace path for a task. The
    directory is created lazily on first write -- this getter is pure."""
    return paths.task_dir(task_id) / "compile"


def _write_atomic(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` via temp + rename -- same primitive
    Phase 1's scaffolder uses. Kept here as a tiny duplicate to keep
    this module self-contained for testing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, path)
    finally:
        try:
            os.unlink(tmp.name)
        except FileNotFoundError:
            pass


def _read_brief_and_playbook(
    paths: JournalPaths, task_id: str,
) -> tuple[str, str]:
    """Read the brief + playbook snapshot the scaffolder wrote. Returns
    ``(brief, playbook)`` text."""
    task_dir = paths.task_dir(task_id)
    brief = (task_dir / "task_brief.md").read_text(encoding="utf-8")
    playbook = (task_dir / "playbook_snapshot.md").read_text(encoding="utf-8")
    return brief, playbook


def _roster_for_task(
    paths: JournalPaths, task_id: str,
) -> list[str]:
    """Best-effort roster for a workflow task. Uses
    :func:`resolve_team_root` to find the team that owns the journal,
    then reads its personas.yaml. Returns an empty list if the team
    folder can't be located (the caller surfaces this).

    The journal currently has no on-disk pointer back to its team
    (it's implied by ``cwd`` or env). We replicate Phase 1's discovery
    convention: prefer ``$TIGERHARNESS_TEAMS_DIR``-scoped lookup, fall
    back to cwd-is-team-root.
    """
    cwd = Path.cwd()
    if (cwd / "configs" / "personas.yaml").is_file():
        roster = read_team_roster(cwd)
        return sorted(roster)
    return []


# ---------------------------------------------------------------------------
# compile-context
# ---------------------------------------------------------------------------

def cmd_compile_context(args: argparse.Namespace) -> int:
    """Print playbook + brief + roster + drafter prompt for the task.

    Single bootstrap call the session makes at the top of a compile
    invocation: gives the session everything it needs to write the first
    drafter turn without further file-fiddling.
    """
    paths = _paths_from_args(args)
    status_or_err = _load_workflow_status(paths, args.task_id)
    if isinstance(status_or_err, str):
        print(f"error: {status_or_err}", file=sys.stderr)
        return 1
    status = status_or_err

    brief, playbook = _read_brief_and_playbook(paths, status.id)
    roster = _roster_for_task(paths, status.id)

    # Lazy import: avoids a hard import-time dep on workflow_runner
    # during journal Phase 1 callsites that never reach compile mode.
    from tigerharness.workflow_runner.compile.drafter import _build_prompt

    drafter_prompt = _build_prompt(
        playbook_text=playbook,
        task_brief=brief,
        roster=roster,
        feedback=None,
    )

    # Plain-text section dump -- the session reads it as one block.
    print("# compile-context for task", status.id)
    print()
    print("## Task")
    print(f"id: {status.id}")
    print(f"title: {status.title}")
    print(f"compile_phase: {status.compile_phase.value}")
    print()
    print("## Roster")
    if roster:
        for name in roster:
            print(f"- {name}")
    else:
        print("(roster unresolved -- cwd is not the team root)")
    print()
    print("## Brief")
    print(brief.rstrip("\n"))
    print()
    print("## Playbook")
    print(playbook.rstrip("\n"))
    print()
    print("## Drafter prompt (round 1, no critic feedback yet)")
    print()
    print(drafter_prompt.rstrip("\n"))
    return 0


# ---------------------------------------------------------------------------
# compile-prompts
# ---------------------------------------------------------------------------

def cmd_compile_prompts(args: argparse.Namespace) -> int:
    """Print the assembled drafter / akagi / ayako prompt for a task.

    For ``--kind drafter`` an optional ``--feedback`` argument is
    interpolated as critic feedback for a re-draft round. For
    ``--kind {akagi,ayako}`` the session must supply ``--draft`` (the
    current step bundle text) and ``--trace`` (the Tier 1 dry-run
    trace) -- both required by the critic prompt template.
    """
    paths = _paths_from_args(args)
    status_or_err = _load_workflow_status(paths, args.task)
    if isinstance(status_or_err, str):
        print(f"error: {status_or_err}", file=sys.stderr)
        return 1
    status = status_or_err

    brief, playbook = _read_brief_and_playbook(paths, status.id)
    roster = _roster_for_task(paths, status.id)

    kind = args.kind
    if kind == "drafter":
        from tigerharness.workflow_runner.compile.drafter import _build_prompt
        prompt = _build_prompt(
            playbook_text=playbook,
            task_brief=brief,
            roster=roster,
            feedback=args.feedback,
        )
    else:
        # akagi / ayako: both critics need a current step set + trace.
        if not args.draft:
            print(
                f"error: --draft is required for --kind {kind}",
                file=sys.stderr,
            )
            return 2
        if not args.trace:
            print(
                f"error: --trace is required for --kind {kind}",
                file=sys.stderr,
            )
            return 2
        draft_path = Path(args.draft).expanduser()
        trace_path = Path(args.trace).expanduser()
        try:
            draft_text = draft_path.read_text(encoding="utf-8")
            trace_text = trace_path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot read draft/trace: {exc}", file=sys.stderr)
            return 1
        # Parse the draft into StepFrontmatter, then re-render for the
        # critic prompt (the critic sees the parsed view, not raw text).
        from tigerharness.workflow_runner.compile.drafter import _parse_response
        try:
            steps = _parse_response(draft_text)
        except Exception as exc:  # DrafterParseError
            print(
                f"error: draft does not parse: {exc}",
                file=sys.stderr,
            )
            return 1
        from tigerharness.workflow_runner.compile.critique import (
            AKAGI_CRITIC_PROMPT_TEMPLATE,
            AYAKO_CRITIC_PROMPT_TEMPLATE,
            _build_critic_prompt,
            _render_steps,
        )
        template = (
            AKAGI_CRITIC_PROMPT_TEMPLATE
            if kind == "akagi"
            else AYAKO_CRITIC_PROMPT_TEMPLATE
        )
        rendered = _render_steps(steps)
        prompt = _build_critic_prompt(
            template,
            playbook_text=playbook,
            task_brief=brief,
            roster=roster,
            rendered_steps=rendered,
            trace=trace_text,
        )

    sys.stdout.write(prompt)
    if not prompt.endswith("\n"):
        sys.stdout.write("\n")
    return 0


# ---------------------------------------------------------------------------
# validate-graph
# ---------------------------------------------------------------------------

def cmd_validate_graph(args: argparse.Namespace) -> int:
    """Tier 1 mechanical validators over a draft. Emits JSON envelope to
    stdout: ``{ok: bool, errors: [...], trace: "..."}``. Exit 0 on ok,
    1 on validation failure.

    The draft is read from ``--draft`` (raw drafter text). We parse it
    via the same ``_parse_response`` the api pipeline uses, then call
    ``validate_compile_output`` from workflow_runner.compile.validators.
    The roster is the team's personas.yaml (best-effort).
    """
    paths = _paths_from_args(args)
    status_or_err = _load_workflow_status(paths, args.task)
    if isinstance(status_or_err, str):
        print(f"error: {status_or_err}", file=sys.stderr)
        return 2

    draft_path = Path(args.draft).expanduser()
    try:
        draft_text = draft_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot read draft {draft_path}: {exc}", file=sys.stderr)
        return 2

    from tigerharness.workflow_runner.compile.drafter import (
        DrafterParseError,
        _parse_response,
    )
    try:
        steps = _parse_response(draft_text)
    except DrafterParseError as exc:
        # Treat a parse failure as a validation failure: ok=False,
        # errors=[parse], trace="" (no trace produceable). Exit 1.
        envelope = {
            "ok": False,
            "errors": [
                {
                    "validator": "parse",
                    "step_id": None,
                    "message": str(exc),
                }
            ],
            "trace": "",
        }
        print(json.dumps(envelope, indent=2))
        return 1

    roster = _roster_for_task(paths, status_or_err.id)

    from tigerharness.workflow_runner.compile.validators import (
        validate_compile_output,
    )
    result = validate_compile_output(steps, roster=roster)

    envelope = {
        "ok": result.ok,
        "errors": [
            {
                "validator": e.validator,
                "step_id": e.step_id,
                "message": e.message,
            }
            for e in result.errors
        ],
        "trace": result.trace,
    }
    print(json.dumps(envelope, indent=2))
    return 0 if result.ok else 1


# ---------------------------------------------------------------------------
# land-compile
# ---------------------------------------------------------------------------

def cmd_land_compile(args: argparse.Namespace) -> int:
    """Atomic compile-landing transaction.

    1. Re-run Tier 1 as defensive validation (catches regression
       between the last successful validate-graph and now).
    2. Build the :class:`Orchestration` from the parsed steps.
    3. Write step files + orchestration.json + compile_critique.md to
       ``compile/final/``.
    4. Atomically promote: rename ``compile/final/orchestration.json``
       -> ``<task-id>/orchestration.json``; rename
       ``compile/final/steps/`` -> ``<task-id>/steps/``; copy the
       transcript to ``<task-id>/compile_critique.md``.
    5. Flip ``compile_pending=false`` + ``compile_phase=complete`` in
       status.json LAST -- it's the visibility gate.
    """
    paths = _paths_from_args(args)
    status_or_err = _load_workflow_status(paths, args.task)
    if isinstance(status_or_err, str):
        print(f"error: {status_or_err}", file=sys.stderr)
        return 1
    status = status_or_err

    draft_path = Path(args.draft).expanduser()
    transcript_path = Path(args.transcript).expanduser()
    try:
        draft_text = draft_path.read_text(encoding="utf-8")
        transcript_text = transcript_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot read draft/transcript: {exc}", file=sys.stderr)
        return 1

    from tigerharness.workflow_runner.compile.drafter import (
        DrafterParseError,
        _parse_response,
    )
    try:
        steps = _parse_response(draft_text)
    except DrafterParseError as exc:
        print(f"error: draft does not parse: {exc}", file=sys.stderr)
        return 1

    # Defensive Tier 1 re-validation.
    roster = _roster_for_task(paths, status.id)
    from tigerharness.workflow_runner.compile.validators import (
        validate_compile_output,
    )
    val = validate_compile_output(steps, roster=roster)
    if not val.ok:
        print(
            "error: post-critique Tier 1 re-validation failed; refusing "
            "to land malformed graph",
            file=sys.stderr,
        )
        for err in val.errors:
            print(
                f"  - [{err.validator}] {err.step_id}: {err.message}",
                file=sys.stderr,
            )
        return 1

    # Build Orchestration. The journal subscription model is itself
    # an implicit human gate (every persona turn happens inside a live
    # interactive session a human is sitting in), so the api-backed
    # Tier 3 human_gate mechanism does not port -- it's permanently
    # disabled here. See docs/journal-workflow-mode.md "Out of scope"
    # for the rationale.
    from tigerharness.workflow_runner.compile.pipeline import _build_orchestration
    from tigerharness.workflow_runner.models import WorkflowConfig
    orchestration = _build_orchestration(
        task_id=status.id,
        team=_guess_team_for_status(),
        # Phase 2: read the truthful playbook_name from status.json
        # rather than hardcoding "default". Schema gate guarantees a
        # non-None value for kind=workflow.
        playbook_name=status.playbook_name,
        playbook_text=_read_brief_and_playbook(paths, status.id)[1],
        final_steps=steps,
        workflow_config=WorkflowConfig(human_gate=False),
        critique_iters=args.rounds,
    )

    # Stage to compile/final/ first, then promote. A prior crashed
    # land-compile (or a tier1_post recovery loop) may have left stale
    # step files under final/steps/; nuking the directory before the
    # mkdir guarantees only the current draft's steps make it through
    # to the canonical steps/ promotion.
    compile_dir = _compile_dir(paths, status.id)
    final_dir = compile_dir / "final"
    final_dir.mkdir(parents=True, exist_ok=True)
    final_steps_dir = final_dir / "steps"
    if final_steps_dir.exists():
        shutil.rmtree(final_steps_dir)
    final_steps_dir.mkdir(parents=True, exist_ok=False)

    # Step bodies: write each step's frontmatter to final/steps/<id>.md.
    # StepFrontmatter carries no body field (the drafter's per-step body
    # text is consumed at parse time and dropped); a Phase 2 enhancement
    # that wires bodies through can extend this.
    for step in steps:
        front = _render_frontmatter(step)
        (final_steps_dir / f"{step.id}.md").write_text(
            f"---\n{front}---\n",
            encoding="utf-8",
        )

    # Write staged orchestration.json + compile_critique.md.
    _write_atomic(
        final_dir / "orchestration.json",
        json.dumps(orchestration.to_dict(), indent=2) + "\n",
    )
    _write_atomic(
        final_dir / "compile_critique.md",
        transcript_text,
    )

    # Promote: rename staged files into their canonical paths under
    # task_dir. ``os.replace`` is atomic on the same filesystem.
    canonical_orchestration = paths.task_dir(status.id) / "orchestration.json"
    canonical_steps = paths.task_dir(status.id) / "steps"
    canonical_critique = paths.task_dir(status.id) / "compile_critique.md"

    if canonical_steps.exists():
        shutil.rmtree(canonical_steps)
    shutil.move(str(final_steps_dir), str(canonical_steps))
    os.replace(
        str(final_dir / "orchestration.json"),
        str(canonical_orchestration),
    )
    os.replace(
        str(final_dir / "compile_critique.md"),
        str(canonical_critique),
    )

    # Per Open Question #3 lean: delete compile/final/ after promotion.
    # Round files + transcript.md in compile/ are preserved for audit.
    try:
        shutil.rmtree(final_dir)
    except OSError:
        pass  # best-effort cleanup

    # Flip status.json last -- this is the visibility gate the
    # graph-walker checks before reading orchestration.json.
    status.compile_pending = False
    status.compile_phase = CompilePhase.COMPLETE
    _write_atomic(paths.status_json(status.id), status.to_json())

    print(f"landed: {status.id}")
    print(f"  steps: {len(steps)}")
    print(f"  orchestration: {canonical_orchestration}")
    return 0


def _render_frontmatter(step) -> str:
    """Render a StepFrontmatter as YAML-ish text for the step file's
    --- block. Plain key: value lines; lists rendered inline."""
    out = []
    for k, v in asdict(step).items():
        if isinstance(v, list):
            out.append(f"{k}: [{', '.join(str(item) for item in v)}]\n")
        else:
            out.append(f"{k}: {v}\n")
    return "".join(out)


def _guess_team_for_status() -> str:
    """Phase 1.5 placeholder: the journal doesn't currently store a
    team pointer on the task. We use the cwd's basename when cwd is a
    team root (configs/personas.yaml present); otherwise an
    explicit ``unknown`` so the field is still populated.

    A Phase 2 nicety: persist the team on the task at scaffold time.
    """
    cwd = Path.cwd()
    if (cwd / "configs" / "personas.yaml").is_file():
        return cwd.name
    return "unknown"


# ---------------------------------------------------------------------------
# compile-retry
# ---------------------------------------------------------------------------

def cmd_compile_retry(args: argparse.Namespace) -> int:
    """Reset a compile-failed workflow task so the next ``drive-journal``
    invocation retries the in-session compile from scratch.

    Allowed on tasks in ``compile_phase=failed`` only. The operator is
    expected to have inspected ``compile/`` first; this CLI WIPES the
    in-flight compile workspace (round-NN-*.md, transcript.md) and
    flips the status back to its scaffold-time shape
    (``state=pending``, ``compile_pending=true``,
    ``compile_phase=pending``, ``sessions=0``). The brief, playbook
    snapshot, and progress.md are preserved -- a retry starts the
    compile sub-protocol over but does not re-scaffold the task.

    If the operator wants to keep forensic artifacts, they should
    copy ``compile/`` out of the task dir BEFORE calling this CLI.
    """
    paths = _paths_from_args(args)
    status_or_err = _load_workflow_status(paths, args.task_id)
    if isinstance(status_or_err, str):
        print(f"error: {status_or_err}", file=sys.stderr)
        return 1
    status = status_or_err

    if status.compile_phase != CompilePhase.FAILED:
        print(
            f"error: task {status.id} is not in compile_phase=failed "
            f"(currently {status.compile_phase.value if status.compile_phase else 'n/a'}); "
            "compile-retry only operates on failed compiles. Use abort "
            "to discard or wait for the in-flight compile to finish.",
            file=sys.stderr,
        )
        return 1

    # Wipe in-flight compile workspace -- if it exists. Missing compile/
    # is not an error (the failure could have happened before any round
    # files were written).
    compile_dir = _compile_dir(paths, status.id)
    if compile_dir.exists():
        shutil.rmtree(compile_dir)

    # Reset the status fields to scaffold-time shape. Sessions counter
    # resets to 0 so the next pickup gets its full budget. updated_at
    # gets a fresh timestamp so the sweep doesn't immediately reclassify
    # this task as stale.
    from tigerharness.journal.models import _utcnow_iso
    status.state = State.PENDING
    status.compile_pending = True
    status.compile_phase = CompilePhase.PENDING
    status.sessions = 0
    status.session_ref = None
    status.next_action = (
        "Compile retry requested via `journal compile-retry`. The "
        "next drive-journal invocation will run the in-session compile "
        "sub-protocol from scratch (compile/ has been wiped)."
    )
    status.updated_at = _utcnow_iso()
    _write_atomic(paths.status_json(status.id), status.to_json())

    print(f"compile-retry: {status.id}")
    print( "  state=pending, compile_pending=true, compile_phase=pending")
    print(f"  next_action: {status.next_action}")
    return 0


# ---------------------------------------------------------------------------
# compile-fail
# ---------------------------------------------------------------------------

def cmd_compile_fail(args: argparse.Namespace) -> int:
    """Soft compile-failure: set ``state=blocked`` and
    ``compile_phase=failed`` on a workflow task whose in-session
    compile loop produced a ``WORKFLOW: BLOCK`` verdict or exhausted
    its round / Tier 1 caps. The task stays in ``active/`` so a human
    can inspect ``compile/`` and decide whether to edit the playbook,
    re-scaffold, or ``journal abort``. This is the protocol-side
    counterpart to ``abort`` (which is the human's final cleanup).
    """
    paths = _paths_from_args(args)
    status_or_err = _load_workflow_status(paths, args.task_id)
    if isinstance(status_or_err, str):
        print(f"error: {status_or_err}", file=sys.stderr)
        return 1
    status = status_or_err

    if status.state == State.DONE:
        print(
            f"task {status.id} is already done; cannot mark compile-failed",
            file=sys.stderr,
        )
        return 1

    status.state = State.BLOCKED
    status.compile_phase = CompilePhase.FAILED
    status.next_action = args.reason
    _write_atomic(paths.status_json(status.id), status.to_json())

    print(f"compile-failed: {status.id}  (state=blocked, compile_phase=failed)")
    print(f"  reason: {args.reason}")
    print(f"  next_action: run `journal abort {status.id}` to archive,")
    print( "               or edit the playbook + re-scaffold a fresh task.")
    return 0


# ---------------------------------------------------------------------------
# abort
# ---------------------------------------------------------------------------

def cmd_abort(args: argparse.Namespace) -> int:
    """Archive a failed workflow task to ``done/`` with a postmortem
    next_action. Preserves ``compile/`` for forensics."""
    paths = _paths_from_args(args)
    status_or_err = _load_workflow_status(paths, args.task_id)
    if isinstance(status_or_err, str):
        print(f"error: {status_or_err}", file=sys.stderr)
        return 1
    status = status_or_err

    if status.state == State.DONE:
        print(
            f"task {status.id} is already done; nothing to abort",
            file=sys.stderr,
        )
        return 1

    # Mark the task done with a postmortem; preserve compile/ for
    # forensic inspection in done/. The pre-flight only loads
    # kind=workflow tasks, whose schema gate guarantees a non-None
    # compile_phase -- so the audit string can dereference it directly.
    prior_state = status.state.value
    prior_phase = status.compile_phase.value
    status.state = State.DONE
    status.next_action = (
        f"Aborted by `tigerharness journal abort`. "
        f"Was state={prior_state} compile_phase={prior_phase}. "
        "compile/ preserved for inspection under done/."
    )
    _write_atomic(paths.status_json(status.id), status.to_json())

    # Now archive via the path layer.
    try:
        new_path = paths.archive(status.id)
    except JournalPathError as exc:
        print(f"error: archive failed: {exc}", file=sys.stderr)
        return 1

    print(f"aborted + archived: {status.id} -> {new_path}")
    return 0


# ---------------------------------------------------------------------------
# validate-personas
# ---------------------------------------------------------------------------

def cmd_validate_personas(args: argparse.Namespace) -> int:
    """Pre-flight check: do Anzai/Akagi/Ayako prompts exist under the
    named team? Exit 0 + "ok" on success; exit 1 + missing list on
    failure.

    Used by the journal-new skill or a CI step to fail FAST before
    scaffolding a workflow that would crash mid-compile.
    """
    team_root = resolve_team_root(args.team)
    if not team_root.is_dir():
        print(
            f"error: team root not found at {team_root}",
            file=sys.stderr,
        )
        return 1
    missing = validate_personas(team_root, set(COMPILE_PERSONAS))
    if missing:
        print(
            f"missing prompt.md for: {sorted(missing)} (under {team_root})",
            file=sys.stderr,
        )
        return 1
    print(f"ok: {team_root} has all of {sorted(COMPILE_PERSONAS)}")
    return 0


# ---------------------------------------------------------------------------
# Subparser wiring
# ---------------------------------------------------------------------------

def build_subparsers(sub: "argparse._SubParsersAction") -> None:
    """Register the six compile subcommands on the journal CLI's
    ``subparsers`` group. Called from ``cli.build_parser``."""
    # compile-context
    cc = sub.add_parser(
        "compile-context",
        help="Print playbook + brief + roster + drafter prompt for a task.",
    )
    cc.add_argument("task_id")
    cc.set_defaults(func=cmd_compile_context)

    # compile-prompts
    cp = sub.add_parser(
        "compile-prompts",
        help="Print the assembled prompt for the drafter / akagi / ayako role.",
    )
    cp.add_argument("--task", required=True)
    cp.add_argument(
        "--kind", required=True,
        choices=["drafter", "akagi", "ayako"],
    )
    cp.add_argument("--feedback", default=None,
                    help="Critic feedback for a drafter re-draft.")
    cp.add_argument("--draft", default=None,
                    help="Path to the current draft text (required for critics).")
    cp.add_argument("--trace", default=None,
                    help="Path to the Tier 1 trace (required for critics).")
    cp.set_defaults(func=cmd_compile_prompts)

    # validate-graph
    vg = sub.add_parser(
        "validate-graph",
        help="Run Tier 1 validators over a draft; emit JSON {ok, errors, trace}.",
    )
    vg.add_argument("--task", required=True)
    vg.add_argument("--draft", required=True)
    vg.set_defaults(func=cmd_validate_graph)

    # land-compile
    lc = sub.add_parser(
        "land-compile",
        help="Atomic landing of a passed compile: build Orchestration + promote.",
    )
    lc.add_argument("--task", required=True)
    lc.add_argument("--draft", required=True)
    lc.add_argument("--transcript", required=True)
    lc.add_argument("--rounds", required=True, type=int)
    lc.set_defaults(func=cmd_land_compile)

    # compile-retry
    cr = sub.add_parser(
        "compile-retry",
        help=(
            "Reset a compile-failed workflow task so the next "
            "drive-journal retries the in-session compile. "
            "Wipes compile/."
        ),
    )
    cr.add_argument("task_id")
    cr.set_defaults(func=cmd_compile_retry)

    # compile-fail
    cf = sub.add_parser(
        "compile-fail",
        help=(
            "Mark a workflow task's compile as failed "
            "(state=blocked, compile_phase=failed). "
            "Does NOT archive."
        ),
    )
    cf.add_argument("task_id")
    cf.add_argument(
        "--reason", required=True,
        help=(
            "Postmortem written to status.next_action. e.g. "
            "'compile failed at critiquing: Akagi BLOCK -- "
            "<one-paragraph rationale>'."
        ),
    )
    cf.set_defaults(func=cmd_compile_fail)

    # abort
    ab = sub.add_parser(
        "abort",
        help="Archive a failed workflow task to done/ with a postmortem.",
    )
    ab.add_argument("task_id")
    ab.set_defaults(func=cmd_abort)

    # validate-personas
    vp = sub.add_parser(
        "validate-personas",
        help="Pre-flight: do the compile-time personas (Anzai/Akagi/Ayako) exist for <team>?",
    )
    vp.add_argument("team")
    vp.set_defaults(func=cmd_validate_personas)

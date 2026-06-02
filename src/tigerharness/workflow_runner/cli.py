"""User-facing CLI: ``start``, ``show``, ``list``, ``tail``, ``cancel``.

Driven via::

    python -m tigerharness.workflow_runner <subcommand> [args]

``start`` has two modes:

* **Compile mode (Phase 2, recommended).** ``--playbook <name>`` +
  ``--task-brief <text>`` / ``--brief-file <path>`` compiles a freestyle
  playbook + brief into validated step files (Tier 1 validators + Tier 2
  critique loop), then runs the result. ``--thread <ts>`` records the
  Slack thread for the Phase 3 human gate.
* **Escape hatch (Phase 1).** ``--steps <dir>`` skips compile and uses
  **pre-compiled** step files as-is: it initialises the task folder, then
  drives :class:`WorkflowExecutor`. ``--steps`` is mutually exclusive with
  the compile-mode flags.

Either mode drives :class:`WorkflowExecutor` to a terminal phase (``done``
/ ``escalated`` / ``cancelled``) and maps that to the process exit code.
Pass ``--no-run`` to initialise the journal without starting the loop
(used by tests and for inspecting a task before running it).

Design picks:

- Argparse, same style as :mod:`tigerharness.task_runner.cli`.
- Task-id prefix lookup (git-short-SHA style) via a single shared
  helper, so ``show`` / ``tail`` / ``cancel`` all accept any
  unambiguous prefix.
- ``cancel`` writes a sentinel ``.cancel`` file the executor will
  poll at each iteration boundary, and sets ``phase=cancelling`` in
  ``status.json``. The latter is written as a plain dict (not via
  :class:`Status`) because the ``"cancelling"`` phase is being added
  in parallel by Miyagi's revise and we must not block on that
  landing.
- ``tail --follow`` polls the file with a short sleep and exits
  cleanly on ``KeyboardInterrupt``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import yaml

from tigerharness.workflow_runner import (
    Orchestration,
    StepEdges,
    StepFrontmatter,
    Status,
    WorkflowConfig,
    WorkflowModelError,
    append_event,
)
from tigerharness.workflow_runner.atomic import (
    read_json,
    write_json_atomic,
)
from tigerharness.workflow_runner.compile.errors import (
    CompileTier1Error,
    CompileTier2Error,
)
from tigerharness.workflow_runner.executor import (
    ExecutionOutcome,
    ExecutorError,
    WorkflowExecutor,
)
from tigerharness.workflow_runner.models import now_iso
from tigerharness.workflow_runner.paths import (
    TaskPaths,
    default_journal_root,
    new_task_id,
)
from tigerharness.workflow_runner.sessions import SessionManager

# --------------------------------------------------------------------------- #
# Phase 2 compile-mode integration seams
# --------------------------------------------------------------------------- #
#
# The compile pipeline (``compile/pipeline.py``, Sakuragi) lands in a
# parallel Phase 2 worktree and may not exist on this branch yet. We keep
# its public entrypoint as a late-bound module global (default ``None``) so:
#
#   * importing ``cli`` never hard-depends on that module existing, and
#   * tests can patch ``cli.compile_playbook`` directly (the documented
#     test seam).
#
# The Tier 2 critique loop (``compile/critique.py``, Rukawa) is *not*
# injected from here: per ``docs/workflow-runner-phase2.md`` (Public API),
# ``compile_playbook`` owns the full compile (Tier 1 + Tier 2) internally
# and exposes only ``session_manager`` / ``max_compile_iters`` as seams.
#
# ``_resolve_compile_entrypoint`` imports the real callable lazily when the
# global is still ``None`` -- the path exercised only post-integration
# (Anzai), hence the ``pragma: no cover`` on that branch.
compile_playbook = None


# Step ids land on disk as ``steps/<id>.md`` -- keep them strictly
# filename-safe. Spec examples use ``[a-z0-9-]``; we additionally
# permit ``_`` so Phase 2's compile phase has wiggle room.
_STEP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]*$")

# ``--playbook`` is a *name*, not a path: it is interpolated directly into
# ``teams/<Team>/workflow/<name>.md``. Reject anything with a path
# separator or a leading ``.`` so a stray ``../`` or hidden-file name can't
# escape the workflow directory. First char alphanumeric; thereafter
# ``[A-Za-z0-9._-]`` so versioned names like ``v1.2`` are fine.
_PLAYBOOK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*$")

# Phases that mean "the task is finished, no more work will happen".
# "cancelling" is intentionally *not* terminal: it's the transitional
# state set by ``cancel`` that the executor reads at the next
# iteration boundary before finalising as "cancelled".
_TERMINAL_PHASES = frozenset({"done", "escalated", "cancelled"})

# Exit code for "cancel called on an already-terminal task".
_EXIT_TERMINAL = 3

# Exit codes for ``start`` once the executor has run to a terminal
# phase. ``done`` is success; ``escalated`` / ``cancelled`` are
# non-zero so the wrapping skill (and shell scripts) can branch on the
# outcome. An :class:`ExecutorError` (lock held / corrupt journal) maps
# to 1.
_RUN_EXIT_CODES = {"done": 0, "escalated": 3, "cancelled": 4}

# Frontmatter fence delimiter.
_FM_DELIM = "---"


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Return the YAML frontmatter dict from a step-file body.

    Mirrors the contract of :func:`tiger_memory.frontmatter.parse` but
    kept local so workflow_runner doesn't import from tiger_memory.
    Returns an empty dict if no fenced frontmatter block is present
    or if the block is not a YAML mapping.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != _FM_DELIM:
        return {}
    end: int | None = None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == _FM_DELIM:
            end = i
            break
    if end is None:
        return {}
    fm_text = "".join(lines[1:end])
    try:
        data = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _validate_step_id(step_id: str) -> str:
    """Sanity-check that ``step_id`` is filename-safe.

    Miyagi's :class:`StepFrontmatter` already requires a non-empty
    string, but it does not enforce filename safety. Since the id is
    embedded directly in ``steps/<id>.md`` and in path joins, a stray
    slash or null would be a real footgun. Belt + suspenders.
    """
    if not _STEP_ID_RE.match(step_id):
        raise WorkflowModelError(
            f"step id {step_id!r} is not filename-safe "
            f"(must match {_STEP_ID_RE.pattern})"
        )
    return step_id


def _resolve_task_id_prefix(root: Path, prefix: str) -> str:
    """Resolve ``prefix`` to a single existing task-id under ``root``.

    Mirrors the prefix-lookup contract from
    :meth:`task_runner.registry.JobStore.resolve_prefix`. Exact match
    wins; otherwise expand the prefix across all task folders. Raises
    :class:`KeyError` on no-match or ambiguous match so callers can
    catch a single exception type for both.
    """
    if not prefix:
        raise KeyError("task-id prefix must be non-empty")
    direct = root / prefix
    if direct.is_dir():
        return prefix
    if not root.exists():
        raise KeyError(f"no task matches prefix {prefix!r} (no journal yet)")
    matches = sorted(
        p.name for p in root.iterdir()
        if p.is_dir() and p.name.startswith(prefix)
    )
    if not matches:
        raise KeyError(f"no task matches prefix {prefix!r}")
    if len(matches) > 1:
        joined = ", ".join(matches[:5])
        extra = "" if len(matches) <= 5 else f" (and {len(matches) - 5} more)"
        raise KeyError(
            f"prefix {prefix!r} is ambiguous: matches {joined}{extra}"
        )
    return matches[0]


def _read_status_dict(paths: TaskPaths) -> dict[str, Any]:
    """Read ``status.json`` as a plain dict.

    We avoid :meth:`Status.from_dict` deliberately in some callers
    (notably ``cancel``) so we can read/write a phase value that may
    not yet be in :data:`models._PHASES` while Miyagi's parallel
    revise lands.
    """
    raw = read_json(paths.status_json)
    if not isinstance(raw, dict):
        raise WorkflowModelError(
            f"status.json at {paths.status_json} is not a JSON object"
        )
    return raw


# --------------------------------------------------------------------------- #
# `start`
# --------------------------------------------------------------------------- #


def _load_step_files(steps_dir: Path) -> list[tuple[Path, StepFrontmatter]]:
    """Find ``*.md`` step files in ``steps_dir`` and parse their YAML.

    Returns ``[(source_path, StepFrontmatter), ...]`` sorted by
    filename (filenames are ``NN-...`` per spec, so this is also the
    execution order). Raises ``FileNotFoundError`` if the directory
    is missing, and :class:`WorkflowModelError` on any per-file
    frontmatter problem.
    """
    if not steps_dir.is_dir():
        raise FileNotFoundError(f"steps directory not found: {steps_dir}")
    md_files = sorted(p for p in steps_dir.iterdir() if p.suffix == ".md")
    if not md_files:
        raise WorkflowModelError(
            f"no .md step files found in {steps_dir}"
        )
    out: list[tuple[Path, StepFrontmatter]] = []
    for p in md_files:
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as exc:
            raise WorkflowModelError(
                f"failed to read step file {p}: {exc}"
            ) from exc
        fm = _parse_frontmatter(text)
        if not fm:
            raise WorkflowModelError(
                f"step file {p.name} has no YAML frontmatter"
            )
        try:
            sf = StepFrontmatter.from_dict(fm)
        except WorkflowModelError as exc:
            raise WorkflowModelError(
                f"step file {p.name}: {exc}"
            ) from exc
        _validate_step_id(sf.id)
        out.append((p, sf))
    return out


def _orchestration_for(
    *,
    task_id: str,
    team: str,
    step_frontmatters: list[StepFrontmatter],
    playbook_sha256: str,
) -> Orchestration:
    edges = {sf.id: sf.edges for sf in step_frontmatters}
    return Orchestration(
        task_id=task_id,
        team=team,
        playbook="precompiled",
        playbook_sha256=playbook_sha256,
        steps=[sf.id for sf in step_frontmatters],
        entrypoint=step_frontmatters[0].id,
        compiled_at=now_iso(),
        compiled_by="cli",
        edges=edges,
        # Phase 1 ships without a human-gate workflow yet; disabling
        # keeps WorkflowConfig validation happy (an empty approvers
        # list is rejected when human_gate=True). Phase 3 wires the
        # real gate in.
        workflow_config=WorkflowConfig(
            human_gate=False, human_gate_approvers=[]
        ),
        compile_critique_iters=0,
    )


def _initial_status(*, task_id: str, entrypoint: str) -> Status:
    """Honest initial status: pointer at entrypoint, nothing yet run.

    Notes:

    * ``current_iter=0`` means "iter 1 has not started"; the executor
      will bump to ``1`` when it dispatches the first iteration.
    * ``step_started_at`` is left ``None`` -- we record the entrypoint
      as the current pointer, but no step has actually started, so
      claiming a timestamp here would mislead the diagnose CLI later.
    * ``iter_counts`` starts empty for the same reason: the executor
      populates per-step counts as it runs them.
    """
    return Status(
        task_id=task_id,
        phase="execute",
        started_at=now_iso(),
        current_step=entrypoint,
        current_iter=0,
        step_started_at=None,
        iter_counts={},
    )


def _playbook_sha256(step_paths: Iterable[Path]) -> str:
    """Deterministic ``playbook_sha256`` for a pre-compiled bundle.

    The :class:`Orchestration` model requires a non-empty string here,
    but in Phase 1 we don't have a real playbook -- the steps *are*
    the source. So we hash the concatenation of step file contents in
    sorted order. Stable and good enough for Phase 1's "did the input
    change" sanity check; Phase 2 replaces it with the playbook hash.
    """
    h = hashlib.sha256()
    for p in step_paths:
        h.update(p.name.encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def _mint_task_paths(
    args: argparse.Namespace,
) -> tuple[TaskPaths | None, str | None]:
    """Resolve + create the per-task journal folder.

    Shared by both ``start`` modes (escape hatch + compile). Returns
    ``(paths, task_id)`` on success, or ``(None, None)`` after printing a
    user-facing error (caller maps that to exit code 2). Centralised so
    the ``--task-id`` charset check and the "folder already exists" guard
    behave identically across modes.
    """
    if args.task_id:
        task_id = args.task_id.strip()
        if not _STEP_ID_RE.match(task_id):
            print(
                f"error: --task-id {task_id!r} must match "
                f"{_STEP_ID_RE.pattern}",
                file=sys.stderr,
            )
            return None, None
    else:
        task_id = new_task_id(slug=args.team)

    root = default_journal_root()
    paths = TaskPaths(root=root, task_id=task_id)
    if paths.task_dir.exists():
        print(
            f"error: task folder already exists: {paths.task_dir}",
            file=sys.stderr,
        )
        return None, None
    paths.ensure()
    return paths, task_id


def _cmd_start_precompiled(args: argparse.Namespace) -> int:
    """Escape hatch: initialise from pre-compiled ``--steps`` files.

    This is the unchanged Phase 1 behaviour; ``cmd_start`` routes here
    whenever ``--steps`` is given.
    """
    steps_src = Path(args.steps).expanduser()
    try:
        loaded = _load_step_files(steps_src)
    except (FileNotFoundError, WorkflowModelError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    src_paths = [p for p, _ in loaded]
    frontmatters = [sf for _, sf in loaded]

    # Reject duplicate step ids before we touch the journal.
    seen: set[str] = set()
    for sf in frontmatters:
        if sf.id in seen:
            print(
                f"error: duplicate step id {sf.id!r} across step files",
                file=sys.stderr,
            )
            return 2
        seen.add(sf.id)

    sha = _playbook_sha256(src_paths)

    paths, task_id = _mint_task_paths(args)
    if paths is None:
        return 2

    # Copy step files verbatim. Each compiled file becomes
    # ``steps/<id>.md`` so the executor can address it by id.
    for src, sf in loaded:
        dst = paths.step_file(sf.id)
        shutil.copyfile(src, dst)

    # Build + persist orchestration.json.
    try:
        orch = _orchestration_for(
            task_id=task_id,
            team=args.team,
            step_frontmatters=frontmatters,
            playbook_sha256=sha,
        )
    except WorkflowModelError as exc:
        print(f"error: orchestration build failed: {exc}", file=sys.stderr)
        return 2
    write_json_atomic(paths.orchestration_json, orch.to_dict())

    # Build + persist status.json.
    status = _initial_status(task_id=task_id, entrypoint=orch.entrypoint)
    write_json_atomic(paths.status_json, status.to_dict())

    # Empty sessions.json so the executor can find it on first
    # iteration without special-casing "missing".
    write_json_atomic(paths.sessions_json, {})

    # Open the machine-truth event log with the spec-mandated
    # ``task_started`` record so the audit trail is honest from
    # the moment the task folder exists -- not just from when
    # the executor (Phase 1 #4) first picks it up. ``append_event``
    # creates ``events.jsonl`` on first write, so no separate touch
    # is needed.
    append_event(
        paths.events_jsonl,
        "task_started",
        task_id=task_id,
        team=args.team,
        steps=len(orch.steps),
        entrypoint=orch.entrypoint,
    )

    print(f"Task initialised: {task_id}")
    print(f"  team:       {args.team}")
    print(f"  steps:      {len(orch.steps)}")
    print(f"  entrypoint: {orch.entrypoint}")
    print(f"  path:       {paths.task_dir}")
    print()

    if args.no_run:
        print("note: --no-run set; task initialized but not started.")
        return 0

    return _run_task(paths, task_id)


# --------------------------------------------------------------------------- #
# `start` -- dispatcher + Phase 2 compile mode
# --------------------------------------------------------------------------- #


def cmd_start(args: argparse.Namespace) -> int:
    """Route ``workflow start`` to compile mode or the escape hatch.

    ``--steps`` selects the Phase 1 pre-compiled path; otherwise we
    compile ``--playbook`` + a brief (Phase 2). Flag-combination
    validation runs first so an incompatible invocation fails fast with
    a clear message (exit 2) rather than half-initialising a task.
    """
    err = _validate_start_args(args)
    if err is not None:
        print(f"error: {err}", file=sys.stderr)
        return 2
    if args.steps:
        return _cmd_start_precompiled(args)
    return _cmd_start_compile(args)


def _validate_start_args(args: argparse.Namespace) -> str | None:
    """Enforce the Phase 2 flag-combination rules (ADR 0002 D7).

    Returns ``None`` when the combination is legal, else a
    human-readable error naming the conflicting flags. The mutual
    exclusion is: ``--steps`` (escape hatch) cannot be combined with any
    compile-mode flag, and the two brief sources are exclusive.
    """
    compile_flags: list[str] = []
    if args.playbook is not None:
        compile_flags.append("--playbook")
    if args.task_brief is not None:
        compile_flags.append("--task-brief")
    if args.brief_file is not None:
        compile_flags.append("--brief-file")

    if args.steps:
        if compile_flags:
            joined = " / ".join(compile_flags)
            return (
                f"--steps is mutually exclusive with {joined} "
                f"(the escape hatch uses pre-compiled files; there is no "
                f"compile phase to consume a brief)"
            )
        return None

    # Compile mode (no --steps).
    if args.task_brief is not None and args.brief_file is not None:
        return "--task-brief and --brief-file are mutually exclusive"
    if args.task_brief is None and args.brief_file is None:
        return (
            "compile mode requires a brief: pass --task-brief or "
            "--brief-file (or --steps <dir> for pre-compiled steps)"
        )
    return None


def _resolve_compile_entrypoint() -> Any:
    """Return the ``compile_playbook`` callable.

    Prefers the module global -- patched by tests, wired by integration
    (Anzai) -- and falls back to importing the real implementation lazily.
    See the integration-seam note at the top of the module.
    """
    compile_fn = compile_playbook
    if compile_fn is None:  # pragma: no cover - integration seam (Sakuragi)
        from tigerharness.workflow_runner.compile.pipeline import (
            compile_playbook as compile_fn,
        )
    return compile_fn


def _write_compile_artifacts(paths: TaskPaths, result: Any) -> None:
    """Persist the compiled plan + diagnostics from a ``CompileResult``.

    ``docs/workflow-runner-phase2.md`` is internally split on who writes
    what: the pipeline "High-level flow" lists persisting steps/ +
    orchestration.json + traces, while the ``cmd_start`` flow has the CLI
    call ``write_artifacts(task_paths, result)`` and describes the returned
    ``Orchestration`` as "ready to persist". We resolve that by having the
    CLI write the artifacts it *can* reconstruct from ``result`` --
    ``orchestration.json`` and the compile ``trace`` / ``transcript``
    diagnostics. The write is idempotent: if the pipeline already wrote
    the same content the CLI simply rewrites it; if it did not, the CLI
    fills the gap so the executor always finds a valid
    ``orchestration.json``.

    Step ``.md`` *bodies* and ``sessions.json`` stay the pipeline's job:
    ``CompileResult.steps`` carries only :class:`StepFrontmatter`, so the
    prompt bodies cannot be reconstructed here.
    """
    write_json_atomic(
        paths.orchestration_json, result.orchestration.to_dict()
    )
    paths.compile_trace.write_text(result.trace, encoding="utf-8")
    paths.compile_critique.write_text(result.transcript, encoding="utf-8")


def _resolve_team_root(team: str) -> Path:
    """Resolve the on-disk team root for ``team``.

    Resolution order:

    1. ``$TIGERHARNESS_TEAMS_DIR/<team>`` if the env var is set -- the
       explicit override (and the test seam).
    2. ``<cwd>`` if cwd is itself a team root (``configs/personas.yaml``
       present): the "run from inside the team folder" convention, the
       same heuristic :func:`paths.default_journal_root` uses.
    3. ``<cwd>/teams/<team>`` otherwise -- the documented layout
       (``teams/<Team>/workflow/<name>.md``).
    """
    override = os.environ.get("TIGERHARNESS_TEAMS_DIR", "").strip()
    if override:
        return Path(override) / team
    cwd = Path.cwd()
    if (cwd / "configs" / "personas.yaml").is_file():
        return cwd
    return cwd / "teams" / team


def _cmd_start_compile(args: argparse.Namespace) -> int:
    """Compile ``--playbook`` + a brief into steps, then (optionally) run.

    Emits the Phase 2 compile events (``compile_started`` ->
    ``compile_completed`` | ``compile_failed``, plus ``human_gate_requested``
    when the compiled config enables the gate) and bootstraps the runtime
    ``status.json`` pointer. Ownership of the on-disk artifacts splits as:
    the CLI persists ``orchestration.json`` + the compile ``trace`` /
    ``transcript`` from the returned ``CompileResult`` (see
    :func:`_write_compile_artifacts`), the verbatim input snapshots
    (``task_brief.md`` / ``playbook_snapshot.md``), and the ``status.json``
    pointer; the pipeline owns the step ``.md`` bodies (``steps/``) and
    ``sessions.json``, which the CLI cannot reconstruct from the result.
    """
    team = args.team
    playbook_name = args.playbook or "default"
    if not _PLAYBOOK_NAME_RE.match(playbook_name):
        print(
            f"error: --playbook {playbook_name!r} must be a bare name "
            f"(start with [A-Za-z0-9], then [A-Za-z0-9._-]); no path "
            f"separators",
            file=sys.stderr,
        )
        return 2
    team_root = _resolve_team_root(team)
    playbook_path = team_root / "workflow" / f"{playbook_name}.md"
    if not playbook_path.is_file():
        print(
            f"error: playbook {playbook_name}.md not found under "
            f"{team_root / 'workflow'}/ (looked for {playbook_path})",
            file=sys.stderr,
        )
        return 2

    # Resolve the brief. ``_validate_start_args`` guarantees exactly one
    # of --task-brief / --brief-file is set in compile mode.
    if args.task_brief is not None:
        brief = args.task_brief
    else:
        brief_path = Path(args.brief_file).expanduser()
        try:
            brief = brief_path.read_text(encoding="utf-8")
        except OSError as exc:
            print(
                f"error: cannot read brief file {brief_path}: {exc}",
                file=sys.stderr,
            )
            return 2

    paths, task_id = _mint_task_paths(args)
    if paths is None:
        return 2

    # Snapshot the verbatim inputs up front so the pipeline can read them
    # off disk and the journal records exactly what was compiled.
    paths.task_brief.write_text(brief, encoding="utf-8")
    paths.playbook_snapshot.write_text(
        playbook_path.read_text(encoding="utf-8"), encoding="utf-8"
    )

    brief_sha = hashlib.sha256(brief.encode("utf-8")).hexdigest()
    append_event(
        paths.events_jsonl,
        "compile_started",
        playbook=playbook_name,
        task_brief_sha256=brief_sha,
    )

    compile_fn = _resolve_compile_entrypoint()
    session_manager = SessionManager(paths.task_dir)
    try:
        # Signature per docs/workflow-runner-phase2.md "Public API".
        # ``max_compile_iters`` is intentionally omitted: the pipeline
        # derives it from the playbook's own workflow_config (the CLI has
        # not parsed that yet at this point), falling back to its default.
        result = compile_fn(
            playbook_path=playbook_path,
            task_brief=brief,
            team_root=team_root,
            task_paths=paths,
            session_manager=session_manager,
        )
    except CompileTier1Error as exc:
        append_event(
            paths.events_jsonl,
            "compile_failed",
            tier=1,
            errors=exc.errors,
        )
        print(f"error: compile failed (tier 1): {exc}", file=sys.stderr)
        return 2
    except CompileTier2Error as exc:
        append_event(
            paths.events_jsonl,
            "compile_failed",
            tier=2,
            last_verdicts=exc.last_verdicts,
        )
        print(f"error: compile failed (tier 2): {exc}", file=sys.stderr)
        return 2

    # Persist the compiled plan before logging success so any reader that
    # sees ``compile_completed`` can trust ``orchestration.json`` exists.
    _write_compile_artifacts(paths, result)

    append_event(
        paths.events_jsonl,
        "compile_completed",
        steps=len(result.steps),
        critique_iters=result.critique_iters,
    )

    orch = result.orchestration
    wf_config = orch.workflow_config
    if wf_config.human_gate:
        # Phase 3 hand-off (ADR 0002 D9): announce the gate request with
        # the would-be approvers + the Slack thread to route the ask to.
        # Phase 2 does NOT wait -- compilation proceeds straight to run.
        append_event(
            paths.events_jsonl,
            "human_gate_requested",
            approvers=list(wf_config.human_gate_approvers),
            slack_thread_ts=args.thread,
        )

    # Runtime bootstrap. orchestration.json is already on disk (the CLI
    # just wrote it via _write_compile_artifacts); the pipeline wrote the
    # step bodies under steps/ (+ sessions.json from the critique
    # personas). Here the CLI writes the initial status pointer. The Slack
    # thread is parked in ``phase_state`` so it survives the executor's
    # later status rewrites for the Phase 3 gate to read back (a bare
    # top-level key would be dropped on the first ``Status.to_dict``
    # round-trip).
    status = _initial_status(task_id=task_id, entrypoint=orch.entrypoint)
    if args.thread:
        status.phase_state["slack_thread_ts"] = args.thread
    write_json_atomic(paths.status_json, status.to_dict())

    append_event(
        paths.events_jsonl,
        "task_started",
        task_id=task_id,
        team=team,
        steps=len(orch.steps),
        entrypoint=orch.entrypoint,
    )

    print(f"Task initialised (compiled): {task_id}")
    print(f"  team:           {team}")
    print(f"  playbook:       {playbook_name}")
    print(f"  steps:          {len(orch.steps)}")
    print(f"  critique_iters: {result.critique_iters}")
    print(f"  entrypoint:     {orch.entrypoint}")
    print(f"  path:           {paths.task_dir}")
    print()

    if args.no_run:
        print("note: --no-run set; task initialized but not started.")
        return 0

    return _run_task(paths, task_id)


def _run_task(paths: TaskPaths, task_id: str) -> int:
    """Drive the executor to a terminal phase and map it to an exit code."""
    try:
        outcome: ExecutionOutcome = WorkflowExecutor(paths).run()
    except ExecutorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Task {outcome.final_phase}: {task_id}")
    print(f"  reason: {outcome.reason}")
    print(f"  cost:   ${outcome.total_cost_usd:.4f}")
    return _RUN_EXIT_CODES.get(outcome.final_phase, 1)


# --------------------------------------------------------------------------- #
# `show`
# --------------------------------------------------------------------------- #


def _format_history_groups(
    history: list[dict[str, Any]],
    *,
    last_n: int = 5,
) -> list[str]:
    """Group history by step, keep last N per step, render as lines."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in history:
        step = entry.get("step", "?")
        groups[step].append(entry)
    out: list[str] = []
    for step, entries in groups.items():
        tail = entries[-last_n:]
        out.append(f"  step {step}:")
        for entry in tail:
            verdict = entry.get("verdict") or "?"
            iter_n = entry.get("iter", "?")
            persona = entry.get("persona", "?")
            reason = entry.get("reason") or ""
            reason_short = (
                f" -- {reason[:60]}" if reason else ""
            )
            out.append(
                f"    iter {iter_n} [{persona}] {verdict}{reason_short}"
            )
    return out


def cmd_show(args: argparse.Namespace) -> int:
    root = default_journal_root()
    try:
        task_id = _resolve_task_id_prefix(root, args.task_id)
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    paths = TaskPaths(root=root, task_id=task_id)
    try:
        raw = _read_status_dict(paths)
    except (FileNotFoundError, WorkflowModelError) as exc:
        print(f"error: cannot read status.json: {exc}", file=sys.stderr)
        return 1

    phase = raw.get("phase", "?")
    cur_step = raw.get("current_step") or "(none)"
    cur_iter = raw.get("current_iter", 0)
    started = raw.get("started_at", "?")
    last_hb = raw.get("last_heartbeat") or "(never)"
    history = raw.get("step_history") or []

    print(f"Task:        {task_id}")
    print(f"  phase:     {phase}")
    print(f"  pointer:   {cur_step} (iter {cur_iter})")
    print(f"  started:   {started}")
    print(f"  heartbeat: {last_hb}")
    if history:
        print()
        print("History (last 5 per step):")
        for line in _format_history_groups(history, last_n=5):
            print(line)
    else:
        print()
        print("History: (none yet)")
    return 0


# --------------------------------------------------------------------------- #
# `list`
# --------------------------------------------------------------------------- #


def _fmt_age(ts_iso: str | None) -> str:
    """Render an ISO timestamp as 'Xm ago' / 'Xh ago' / 'Xd ago'."""
    if not ts_iso:
        return "(never)"
    try:
        import datetime as _dt
        normalised = ts_iso.replace("Z", "+00:00")
        dt = _dt.datetime.fromisoformat(normalised)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        delta = (
            _dt.datetime.now(_dt.timezone.utc) - dt
        ).total_seconds()
    except ValueError:
        return ts_iso
    delta = max(0.0, delta)
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def cmd_list(args: argparse.Namespace) -> int:
    root = default_journal_root()
    if not root.exists():
        print("No tasks. (journal root does not exist yet)")
        return 0
    rows: list[tuple[str, str, str, str, str]] = []
    for task_dir in sorted(root.iterdir()):
        if not task_dir.is_dir():
            continue
        status_p = task_dir / "status.json"
        if not status_p.exists():
            continue
        try:
            raw = read_json(status_p)
            if not isinstance(raw, dict):
                continue
        except (OSError, json.JSONDecodeError):
            continue

        phase = str(raw.get("phase", "?"))
        if not args.all and phase in _TERMINAL_PHASES:
            continue

        if args.team:
            team = _team_for(task_dir)
            if team is None or team.lower() != args.team.lower():
                continue

        cur_step = str(raw.get("current_step") or "(none)")
        cur_iter = raw.get("current_iter", 0)
        started = str(raw.get("started_at") or "?")
        updated = raw.get("last_heartbeat") or raw.get("started_at")
        rows.append(
            (
                task_dir.name,
                phase,
                f"{cur_step} ({cur_iter})",
                started,
                _fmt_age(updated if isinstance(updated, str) else None),
            )
        )

    if not rows:
        if args.all:
            print("No tasks.")
        else:
            print("No active tasks. (use --all to include terminal tasks)")
        return 0

    cols = ["TASK-ID", "phase", "step (iter)", "started", "updated"]
    widths = [
        max(len(c), *(len(r[i]) for r in rows))
        for i, c in enumerate(cols)
    ]
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)))
    return 0


def _team_for(task_dir: Path) -> str | None:
    """Read orchestration.json's ``team`` field; ``None`` on missing/bad."""
    orch_p = task_dir / "orchestration.json"
    if not orch_p.exists():
        return None
    try:
        raw = read_json(orch_p)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    team = raw.get("team")
    return team if isinstance(team, str) else None


# --------------------------------------------------------------------------- #
# `tail`
# --------------------------------------------------------------------------- #


def _format_event_line(raw: dict[str, Any]) -> str:
    ts = raw.get("ts", "?")
    kind = raw.get("kind", "?")
    extras = {k: v for k, v in raw.items() if k not in ("ts", "kind")}
    if not extras:
        return f"{ts}  {kind}"
    payload = " ".join(f"{k}={json.dumps(v, default=str)}" for k, v in extras.items())
    return f"{ts}  {kind}  {payload}"


def _iter_jsonl(path: Path, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    """Read records past ``offset`` bytes. Returns (records, new_offset).

    A trailing partial line (no newline yet) is left unread so the
    next call picks it up once it's flushed.
    """
    if not path.exists():
        return [], offset
    size = path.stat().st_size
    if size < offset:
        # File was truncated/rotated -- restart from 0.
        offset = 0
    if size == offset:
        return [], offset
    with open(path, "r", encoding="utf-8") as fh:
        fh.seek(offset)
        text = fh.read()
    # Only consume whole lines; keep the trailing partial for next read.
    last_nl = text.rfind("\n")
    if last_nl < 0:
        return [], offset
    consumed = text[: last_nl + 1]
    new_offset = offset + len(consumed.encode("utf-8"))
    records: list[dict[str, Any]] = []
    for line in consumed.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            records.append(data)
    return records, new_offset


def cmd_tail(args: argparse.Namespace) -> int:
    root = default_journal_root()
    try:
        task_id = _resolve_task_id_prefix(root, args.task_id)
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    paths = TaskPaths(root=root, task_id=task_id)
    events_p = paths.events_jsonl

    records, offset = _iter_jsonl(events_p, 0)
    for rec in records:
        print(_format_event_line(rec))

    if not args.follow:
        if not records:
            print("(no events yet)")
        return 0

    try:
        while True:
            time.sleep(args.poll_interval)
            records, offset = _iter_jsonl(events_p, offset)
            for rec in records:
                print(_format_event_line(rec))
    except KeyboardInterrupt:
        return 0


# --------------------------------------------------------------------------- #
# `cancel`
# --------------------------------------------------------------------------- #


def cmd_cancel(args: argparse.Namespace) -> int:
    root = default_journal_root()
    try:
        task_id = _resolve_task_id_prefix(root, args.task_id)
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    paths = TaskPaths(root=root, task_id=task_id)
    try:
        raw = _read_status_dict(paths)
    except (FileNotFoundError, WorkflowModelError) as exc:
        print(f"error: cannot read status.json: {exc}", file=sys.stderr)
        return 1

    phase = str(raw.get("phase", "?"))
    if phase in _TERMINAL_PHASES:
        print(
            f"error: task {task_id} is already {phase}; nothing to cancel.",
            file=sys.stderr,
        )
        return _EXIT_TERMINAL
    if phase == "cancelling":
        # Idempotent re-run -- the flag is what matters, but ensure it
        # exists. Don't bump the status; print a friendly note.
        cancel_flag = paths.task_dir / ".cancel"
        if not cancel_flag.exists():
            cancel_flag.touch()
        print(f"Task {task_id} is already cancelling.")
        return 0

    # Write the sentinel flag *before* mutating status.json so the
    # executor (which polls the flag at iteration boundaries) cannot
    # miss the request even if a torn read/write or kill races. The
    # flag is the authoritative signal; the status phase update is
    # a UI/diagnose hint.
    cancel_flag = paths.task_dir / ".cancel"
    cancel_flag.touch()

    raw["phase"] = "cancelling"
    write_json_atomic(paths.status_json, raw)

    # Record the decision in the machine-truth event stream so the
    # diagnose/audit tooling sees *who* requested the cancel and
    # *from what phase* -- info that the bare ``.cancel`` flag can't
    # carry. The flag is still authoritative; this is the audit
    # trail counterpart.
    append_event(
        paths.events_jsonl,
        "cancel_requested",
        task_id=task_id,
        prior_phase=phase,
    )

    print(
        f"Cancel requested for {task_id} "
        f"(was {phase}). Executor will exit at the next iteration boundary."
    )
    return 0


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="workflow",
        description="Workflow orchestration CLI.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # start
    s = sub.add_parser(
        "start",
        help="Compile a playbook + brief into a task (or use --steps).",
        description=(
            "Initialise a new task. Compile mode (--playbook + "
            "--task-brief/--brief-file) compiles a freestyle playbook into "
            "validated steps and runs them. The --steps escape hatch skips "
            "compile and consumes pre-compiled step files as-is. The two "
            "modes are mutually exclusive."
        ),
    )
    s.add_argument("--team", required=True,
                   help="Team name (e.g. Shohoku).")
    s.add_argument("--playbook", default=None,
                   help="Playbook name to compile (resolves to "
                        "teams/<Team>/workflow/<name>.md; default 'default'). "
                        "Requires --task-brief or --brief-file.")
    s.add_argument("--task-brief", default=None,
                   help="Inline task brief text (compile mode). Mutually "
                        "exclusive with --brief-file.")
    s.add_argument("--brief-file", default=None,
                   help="Path to a file containing the task brief (compile "
                        "mode). Mutually exclusive with --task-brief.")
    s.add_argument("--thread", default=None,
                   help="Slack thread ts to route the Phase 3 human gate to; "
                        "persisted to status.json.")
    s.add_argument("--steps", default=None,
                   help="Escape hatch: directory of pre-compiled step .md "
                        "files. Mutually exclusive with the compile-mode "
                        "flags (--playbook/--task-brief/--brief-file).")
    s.add_argument("--no-run", action="store_true",
                   help="initialise the task folder but do not start the "
                        "executor loop")
    s.add_argument("--task-id", default="",
                   help="Optional task-id; minted if omitted.")
    s.set_defaults(func=cmd_start)

    # show
    sh = sub.add_parser(
        "show", help="Show status + recent history for a task.",
    )
    sh.add_argument("task_id")
    sh.set_defaults(func=cmd_show)

    # list
    ls = sub.add_parser(
        "list", help="List tasks under the workflow journal root.",
    )
    ls.add_argument("--team", default="",
                    help="Filter by team (case-insensitive).")
    ls.add_argument("--all", action="store_true",
                    help="Include terminal tasks (done/escalated/cancelled).")
    ls.set_defaults(func=cmd_list)

    # tail
    t = sub.add_parser(
        "tail", help="Pretty-print events.jsonl; --follow to stream.",
    )
    t.add_argument("task_id")
    t.add_argument("--follow", "-f", action="store_true",
                   help="Stream new events until SIGINT.")
    t.add_argument(
        "--poll-interval", type=float, default=0.5,
        help=argparse.SUPPRESS,
    )
    t.set_defaults(func=cmd_tail)

    # cancel
    c = sub.add_parser(
        "cancel", help="Request graceful cancel of a task.",
    )
    c.add_argument("task_id")
    c.set_defaults(func=cmd_cancel)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

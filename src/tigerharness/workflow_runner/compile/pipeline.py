"""Public ``compile_playbook`` entrypoint for the compile phase (Phase 2).

This is the orchestration glue that turns a freestyle markdown playbook
plus a task brief into a validated, ready-to-persist
:class:`~tigerharness.workflow_runner.models.Orchestration`. It stitches
together the three Wave-1/Wave-2 components:

* **drafter** (:func:`compile.drafter.draft_steps`) -- Anzai's initial
  step draft from the playbook + brief.
* **Tier 1 validators** (:func:`compile.validators.validate_compile_output`)
  -- the pure-Python mechanical gate (schema / ref / roster / cycle /
  dry-run trace). Gating: a failure aborts the compile (ADR 0002 D2).
* **Tier 2 critique loop** -- the AI critique pass. Built by Rukawa in
  ``compile.critique`` and **injected** here as ``critique_loop`` so the
  two modules can land in parallel; we never import ``compile.critique``
  directly. See "Cross-module seam" below.

Flow (mirrors ``docs/workflow-runner-phase2.md`` section 1):

1. Read the playbook text + derive the roster from
   ``team_root/configs/personas.yaml``.
2. Draft the initial step list (one drafter invocation).
3. Tier 1 over the draft. ``ok=False`` -> :class:`CompileTier1Error`
   (``stage="pre_critique"``).
4. Run the injected critique loop, handing it the validated steps + the
   Tier-1 trace + a re-draft callable.
5. Tier 1 again over the loop's ``final_steps`` (defense in depth -- a
   critic-driven re-draft may have reintroduced a violation). On
   failure -> :class:`CompileTier1Error` (``stage="post_critique"``).
6. Build the :class:`Orchestration` from ``final_steps`` + the config.
7. Persist the four compile artifacts via :class:`TaskPaths`.
8. Sum the cost (initial draft + critique loop) and return
   :class:`CompileResult`.

What we deliberately do **not** do (out of scope, see the task brief):

* Emit ``events.jsonl`` records -- the CLI / ``cmd_start`` translates our
  exceptions into ``compile_failed{tier:...}`` / ``compile_completed``
  events.
* Persist ``orchestration.json`` / ``steps/*.md`` -- the caller's
  ``write_artifacts`` does that from the returned :class:`CompileResult`.
* Retry the *initial* draft on a parse failure -- a
  :class:`~tigerharness.workflow_runner.compile.drafter.DrafterParseError`
  propagates to the caller untouched.

Cross-module seam
-----------------

``compile.critique`` does not exist on this branch yet (Rukawa builds it
in parallel). We therefore:

* Accept ``critique_loop`` as a **required keyword argument** -- in
  production ``cmd_start`` passes
  ``compile.critique.run_critique_loop``; tests inject a fake.
* Recognise the loop's "exhausted without convergence" abort by the
  exception *class name* ``"CritiqueAbortedError"`` rather than importing
  it, so the modules stay decoupled until Anzai wires them together at
  integration time. See :func:`_is_critique_aborted`.

The :class:`CritiqueResult` shape below mirrors Rukawa's spec
(``rounds`` / ``final_steps`` / ``transcript`` / ``cost_usd``). We only
ever read those attributes off the loop's return value, so a structurally
identical class from ``compile.critique`` is accepted just the same
(duck typing); the local class is the convenient default + the test type.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tigerharness.workflow_runner.compile.drafter import draft_steps
from tigerharness.workflow_runner.compile.validators import (
    ValidationError,
    validate_compile_output,
)
from tigerharness.workflow_runner.models import (
    Orchestration,
    StepFrontmatter,
    WorkflowConfig,
    now_iso,
)
from tigerharness.workflow_runner.paths import TaskPaths
from tigerharness.workflow_runner.sessions import SessionManager

__all__ = [
    "CompileConfigError",
    "CompileResult",
    "CompileTier1Error",
    "CompileTier2Error",
    "CritiqueResult",
    "compile_playbook",
]


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: ``compiled_by`` value written into the orchestration. Matches the
#: drafter persona (``compile.drafter._DRAFTER_PERSONA``); redeclared
#: here rather than reaching across a private boundary.
_COMPILED_BY = "anzai"

#: Hard floor of Tier-2 rounds (ADR 0002 D3: "force critique to look hard
#: 3 times"). Not a ``WorkflowConfig`` knob; passed to the critique loop.
_HARD_FLOOR_ITERS = 3

#: Default human-gate approvers used only when no ``workflow_config`` is
#: supplied. ``WorkflowConfig()`` cannot be defaulted bare -- with
#: ``human_gate=True`` (the spec default) it requires a non-empty
#: approver allowlist or it raises. "operator" matches the
#: ``human_gate_requested`` event example in the Phase 2 spec. The real
#: config should come from the playbook's ``workflow_config`` block once
#: ``cmd_start`` parses it; until then this is the documented placeholder.
_DEFAULT_APPROVERS = ("operator",)

#: Class name of the abort the critique loop raises when it exhausts
#: ``max_compile_iters`` without dual-APPROVE. Matched by name (not
#: import) to keep this module decoupled from ``compile.critique``.
_CRITIQUE_ABORTED_TYPENAME = "CritiqueAbortedError"


# --------------------------------------------------------------------------- #
# Public types
# --------------------------------------------------------------------------- #


class CompileConfigError(Exception):
    """The team's ``personas.yaml`` roster config is malformed.

    A missing file surfaces as the underlying ``FileNotFoundError`` (a
    setup bug that should fail loud, consistent with ``sessions.py``); a
    file that exists but has the wrong shape raises this.
    """


class CompileTier1Error(Exception):
    """Tier 1 validators rejected the step list (pre- or post-critique).

    ``errors`` is the full :class:`ValidationError` list (the caller
    surfaces it into a ``compile_failed{tier:1, errors:[...]}`` event).
    ``stage`` is ``"pre_critique"`` (the initial draft failed) or
    ``"post_critique"`` (the loop's ``final_steps`` reintroduced a
    violation).
    """

    def __init__(self, errors: list[ValidationError], stage: str) -> None:
        self.errors = list(errors)
        self.stage = stage
        detail = "; ".join(
            f"[{e.validator}] {e.message}" for e in self.errors
        )
        super().__init__(
            f"Tier 1 validation failed ({stage}): {detail}"
        )


class CompileTier2Error(Exception):
    """Tier 2 critique loop exhausted without convergence.

    Wraps the abort raised by the injected critique loop (Rukawa's
    ``CritiqueAbortedError``). The original abort is attached as both the
    ``__cause__`` (via ``raise ... from``) and the ``cause`` attribute
    for callers that prefer not to walk the exception chain.
    """

    def __init__(
        self, message: str, *, cause: BaseException | None = None
    ) -> None:
        super().__init__(message)
        self.cause = cause


@dataclass(frozen=True)
class CritiqueResult:
    """Outcome of the Tier-2 critique loop.

    Mirrors the shape Rukawa's ``compile.critique`` produces. The
    pipeline reads these four attributes off whatever the injected loop
    returns, so a structurally identical class is equally accepted.
    """

    rounds: list[Any]  # CritiqueRound objects; we only need the count
    final_steps: list[StepFrontmatter]
    transcript: str
    cost_usd: float


@dataclass(frozen=True)
class CompileResult:
    """Everything the caller needs to persist a compiled task.

    ``orchestration`` is ready to write to ``orchestration.json`` (the
    caller does that). ``cost_usd`` is the initial-draft cost plus the
    critique loop's cost (ADR 0002 D10 -- rolls up into
    ``status.cost_usd_total``).
    """

    steps: list[StepFrontmatter]
    orchestration: Orchestration
    critique_iters: int
    trace: str
    transcript: str
    cost_usd: float


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


def _load_roster(team_root: Path) -> list[str]:
    """Return the persona ``name`` list from ``configs/personas.yaml``.

    Names are returned verbatim and in file order -- the same single
    source of truth the slack-bridge and ``dismiss`` use. Casing is *not*
    normalised here: the Tier-1 ``roster`` validator is case-sensitive,
    so reconciling step personas against these names is a deliberate
    gating check, not the pipeline's job to paper over.

    Raises
    ------
    FileNotFoundError
        If ``personas.yaml`` is absent (a setup bug -- fail loud).
    CompileConfigError
        If the file exists but is structurally invalid.
    """
    yaml_path = team_root / "configs" / "personas.yaml"
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise CompileConfigError(
            f"{yaml_path}: top-level YAML must be a mapping"
        )
    personas = data.get("personas")
    if not isinstance(personas, list) or not personas:
        raise CompileConfigError(
            f"{yaml_path}: missing a non-empty 'personas' list"
        )
    names: list[str] = []
    for entry in personas:
        if not isinstance(entry, dict):
            raise CompileConfigError(
                f"{yaml_path}: each persona entry must be a mapping"
            )
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise CompileConfigError(
                f"{yaml_path}: a persona entry is missing a non-empty 'name'"
            )
        names.append(name)
    return names


def _resolve_config(
    workflow_config: WorkflowConfig | None,
) -> WorkflowConfig:
    """Return the supplied config, or a non-raising default.

    ``WorkflowConfig()`` cannot be constructed bare (its ``human_gate``
    default of ``True`` mandates a non-empty approver list), so the
    ``None`` case substitutes the documented ``_DEFAULT_APPROVERS``.
    """
    if workflow_config is not None:
        return workflow_config
    return WorkflowConfig(human_gate_approvers=list(_DEFAULT_APPROVERS))


def _is_critique_aborted(exc: BaseException) -> bool:
    """True iff ``exc`` is (or subclasses) the loop's abort signal.

    Matched by class name across the MRO so we recognise both the exact
    ``CritiqueAbortedError`` and any subclass without importing it.
    """
    return any(
        cls.__name__ == _CRITIQUE_ABORTED_TYPENAME
        for cls in type(exc).__mro__
    )


def _build_orchestration(
    *,
    task_id: str,
    team: str,
    playbook_name: str,
    playbook_text: str,
    final_steps: list[StepFrontmatter],
    workflow_config: WorkflowConfig,
    critique_iters: int,
) -> Orchestration:
    """Assemble the :class:`Orchestration` from validated steps.

    ``final_steps`` is non-empty and ref-consistent here: it has already
    passed the post-critique Tier-1 gate, so ``final_steps[0]`` and the
    edge map are safe.
    """
    playbook_sha256 = hashlib.sha256(
        playbook_text.encode("utf-8")
    ).hexdigest()
    return Orchestration(
        task_id=task_id,
        team=team,
        playbook=playbook_name,
        playbook_sha256=playbook_sha256,
        steps=[step.id for step in final_steps],
        entrypoint=final_steps[0].id,
        compiled_at=now_iso(),
        compiled_by=_COMPILED_BY,
        edges={step.id: step.edges for step in final_steps},
        workflow_config=workflow_config,
        compile_critique_iters=critique_iters,
    )


def _write_artifacts(
    task_paths: TaskPaths,
    *,
    trace: str,
    transcript: str,
    playbook_text: str,
    task_brief: str,
) -> None:
    """Persist the four compile artifacts under the task directory.

    Written only on a fully successful compile so an aborted run leaves
    no half-state behind. ``task_paths`` is already minted + ensured by
    ``cmd_start`` (the four files live directly under ``task_dir``).
    """
    task_paths.compile_trace.write_text(trace, encoding="utf-8")
    task_paths.compile_critique.write_text(transcript, encoding="utf-8")
    task_paths.playbook_snapshot.write_text(playbook_text, encoding="utf-8")
    task_paths.task_brief.write_text(task_brief, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Public entrypoint
# --------------------------------------------------------------------------- #


def compile_playbook(
    *,
    playbook_path: Path,
    task_brief: str,
    team_root: Path,
    task_paths: TaskPaths,
    session_manager: SessionManager,
    critique_loop: Any,
    workflow_config: WorkflowConfig | None = None,
) -> CompileResult:
    """Compile a playbook + brief into a validated orchestration.

    Parameters
    ----------
    playbook_path:
        The freestyle playbook markdown. Read verbatim; its filename stem
        becomes ``orchestration.playbook``.
    task_brief:
        The task brief, verbatim.
    team_root:
        Team folder; ``configs/personas.yaml`` under it is the roster
        source and ``team_root.name`` becomes ``orchestration.team``.
    task_paths:
        Already-minted per-task paths (artifacts are written here).
    session_manager:
        LLM seam shared by the drafter and the critique loop.
    critique_loop:
        The injected Tier-2 entrypoint (see "Cross-module seam" in the
        module docstring). Called with keyword args ``initial_steps`` /
        ``playbook_text`` / ``task_brief`` / ``roster`` / ``trace`` /
        ``session_manager`` / ``drafter`` / ``max_compile_iters`` /
        ``hard_floor_iters``; must return an object exposing
        ``final_steps`` / ``transcript`` / ``cost_usd`` / ``rounds``.
    workflow_config:
        Optional knobs. ``None`` -> a documented default (see
        :func:`_resolve_config`).

    Returns
    -------
    CompileResult
        Final steps, the ready-to-persist orchestration, the Tier-2 round
        count, the (post-critique) trace, the critique transcript, and
        the summed cost.

    Raises
    ------
    CompileTier1Error
        Tier 1 rejected the draft (``stage="pre_critique"``) or the
        loop's output (``stage="post_critique"``).
    CompileTier2Error
        The critique loop aborted without convergence.
    CompileConfigError / FileNotFoundError
        The roster config is malformed / absent.
    """
    playbook_text = playbook_path.read_text(encoding="utf-8")
    roster = _load_roster(team_root)
    config = _resolve_config(workflow_config)

    # --- 1. Initial draft ------------------------------------------------- #
    initial = draft_steps(
        playbook_text=playbook_text,
        task_brief=task_brief,
        roster=roster,
        session_manager=session_manager,
    )

    # --- 2. Tier 1 (pre-critique, gating) --------------------------------- #
    pre = validate_compile_output(initial.steps, roster=roster)
    if not pre.ok:
        raise CompileTier1Error(pre.errors, "pre_critique")

    # --- 3. Tier 2 critique loop ------------------------------------------ #
    def _redraft(feedback: str) -> list[StepFrontmatter]:
        """Re-draft callable handed to the loop (feedback -> steps)."""
        return draft_steps(
            playbook_text=playbook_text,
            task_brief=task_brief,
            roster=roster,
            session_manager=session_manager,
            feedback=feedback,
        ).steps

    try:
        critique = critique_loop(
            initial_steps=initial.steps,
            playbook_text=playbook_text,
            task_brief=task_brief,
            roster=roster,
            trace=pre.trace,
            session_manager=session_manager,
            drafter=_redraft,
            max_compile_iters=config.max_compile_iters,
            hard_floor_iters=_HARD_FLOOR_ITERS,
        )
    except Exception as exc:
        if _is_critique_aborted(exc):
            raise CompileTier2Error(str(exc), cause=exc) from exc
        raise

    final_steps = list(critique.final_steps)

    # --- 4. Tier 1 (post-critique, defense in depth) ---------------------- #
    post = validate_compile_output(final_steps, roster=roster)
    if not post.ok:
        raise CompileTier1Error(post.errors, "post_critique")

    # --- 5. Orchestration + artifacts ------------------------------------- #
    critique_iters = len(critique.rounds)
    orchestration = _build_orchestration(
        task_id=task_paths.task_id,
        team=team_root.name,
        playbook_name=playbook_path.stem,
        playbook_text=playbook_text,
        final_steps=final_steps,
        workflow_config=config,
        critique_iters=critique_iters,
    )
    _write_artifacts(
        task_paths,
        trace=post.trace,
        transcript=critique.transcript,
        playbook_text=playbook_text,
        task_brief=task_brief,
    )

    return CompileResult(
        steps=final_steps,
        orchestration=orchestration,
        critique_iters=critique_iters,
        trace=post.trace,
        transcript=critique.transcript,
        cost_usd=initial.cost_usd + critique.cost_usd,
    )

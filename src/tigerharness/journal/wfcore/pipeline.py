"""Public ``compile_playbook`` entrypoint for the compile phase (Phase 2).

This is the orchestration glue that turns a freestyle markdown playbook
plus a task brief into a validated, ready-to-persist
:class:`~tigerharness.journal.wfcore.models.Orchestration`. It stitches
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
  :class:`~tigerharness.journal.wfcore.drafter.DrafterParseError`
  propagates to the caller untouched.

Cross-module seam
-----------------

``compile.critique`` was built in parallel by Rukawa. The pipeline:

* Accepts ``critique_loop`` as an **optional keyword argument** and
  default-resolves it (lazy import) to
  ``compile.critique.run_critique_loop`` -- so the CLI does not need to
  pass it explicitly (matches ``docs/workflow-runner-phase2.md`` Public
  API). Tests inject a fake.
* Recognises the loop's "exhausted without convergence" abort by the
  exception *class name* ``"CritiqueAbortedError"`` rather than importing
  it, so the modules stay decoupled even after integration. See
  :func:`_is_critique_aborted`.

The :class:`CritiqueResult` shape below mirrors Rukawa's spec
(``rounds`` / ``final_steps`` / ``transcript`` / ``cost_usd``). We only
ever read those attributes off the loop's return value, so a structurally
identical class from ``compile.critique`` is accepted just the same
(duck typing); the local class is the convenient default + the test type.
"""

from __future__ import annotations

import hashlib

from tigerharness.journal.wfcore.models import (
    Orchestration,
    StepFrontmatter,
    WorkflowConfig,
    now_iso,
)

__all__ = [
    "_build_orchestration",
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
# --------------------------------------------------------------------------- #
# Public entrypoint
# --------------------------------------------------------------------------- #
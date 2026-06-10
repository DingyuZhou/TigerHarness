"""Tier 2 forced AI critique loop for the compile phase (Phase 2, Wave 2).

After the drafter emits a candidate step set and the Tier 1 mechanical
validators pass, the critique loop subjects the plan to two adversarial
LLM critics -- :data:`_AKAGI_PERSONA` (execution lens) and
:data:`_AYAKO_PERSONA` (QA lens) -- each round. Both must APPROVE for a
round to count toward termination (ADR 0002, D4: two parallel critics,
not a panel).

The loop's defining rule is the **hard floor** (ADR 0002, D3): it runs
at least ``hard_floor_iters`` rounds even if both critics APPROVE on
round 1 -- the Operator's "force critique to look hard 3 times"
directive. After the floor it terminates the first round both critics
APPROVE. A REVISE from either critic feeds the combined reasons back to
the injected ``drafter`` callable, which re-emits a fresh step set for
the next round. A hard ceiling (``max_compile_iters``) caps the total
rounds; reaching it without convergence raises
:class:`CritiqueAbortedError`.

Seams and the lines we do **not** cross:

* The LLM seam is :class:`SessionManager.invoke` (tests inject a fake).
  ``SessionManager.invoke`` is synchronous, so the two critics run
  sequentially within a round; their verdicts are independent, so order
  does not affect the outcome. (If the seam were async, the brief's
  contract is ``asyncio.gather``; it is not, so we keep it plain.)
* The re-draft is an **injected callable**, not an import of
  ``compile.drafter`` -- that keeps this module free of a circular
  dependency on the drafter, which in turn would import nothing from
  here. In production the pipeline passes a closure over
  ``drafter.draft_steps`` that forwards playbook + brief + roster.
* Verdict parsing is delegated to
  :func:`tigerharness.journal.wfcore.trailer.parse_trailer` -- the one
  place in the codebase where AI-generated text meets deterministic
  routing. Reusing it keeps a single grammar authority for the
  ``WORKFLOW:`` trailer instead of a second, drifting regex here. A
  critic verdict is APPROVE or REVISE only; a ``BLOCK`` or an
  unparseable trailer is a contract violation surfaced as
  :class:`CritiqueParseError` (the pipeline owns any re-prompt policy).
  A failed *invocation* (``invoke`` returns a result with ``error`` set
  rather than raising, on timeout / non-zero exit) is rejected the same
  way, before parsing -- an errored reply carries no trustworthy verdict.
* Per-invocation ``cost_usd`` is summed into
  :attr:`CritiqueResult.cost_usd` so the pipeline can roll it into
  ``status.cost_usd_total`` (ADR 0002, D10).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

from tigerharness.journal.wfcore.models import StepFrontmatter
from tigerharness.journal.wfcore.trailer import (
    Approve,
    Revise,
    parse_trailer,
)

__all__ = [
    "AKAGI_CRITIC_PROMPT_TEMPLATE",
    "AYAKO_CRITIC_PROMPT_TEMPLATE",
    "CritiqueAbortedError",
    "CritiqueParseError",
    "CritiqueResult",
    "CritiqueRound",
    "CritiqueVerdict",
    "run_critique_loop",
]


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Execution critic -- routing / delegation / dev-QA fan-out coherence.
_AKAGI_PERSONA = "akagi"

#: QA critic -- testing gates / critique-loop sizing / doc-step landing.
_AYAKO_PERSONA = "ayako"

#: Per-invocation wall-clock cap handed to the session manager for each
#: critic turn. Matches the drafter's default; the loop is not in the
#: public signature's parameter list, so it lives here as a constant.
_CRITIC_TIMEOUT_SEC = 600

#: Frontmatter keys rendered (in this order) into the step set shown to
#: the critics. Mirrors the drafter's bundle shape so a critic sees a
#: familiar layout.
_RENDER_KEYS: tuple[str, ...] = (*StepFrontmatter.REQUIRED_KEYS, "parallel_with")


# --------------------------------------------------------------------------- #
# Public types
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CritiqueVerdict:
    """One critic's verdict for one round.

    ``reasons`` is free text from the critic's ``REVISE`` trailer; it is
    the empty string for an ``APPROVE``.
    """

    persona: str
    decision: Literal["APPROVE", "REVISE"]
    reasons: str


@dataclass(frozen=True)
class CritiqueRound:
    """One critique round: both critics' verdicts plus the consensus flag."""

    round_num: int  # 1-indexed
    verdicts: list[CritiqueVerdict]  # always two: akagi + ayako, in that order
    all_approve: bool


@dataclass(frozen=True)
class CritiqueResult:
    """Outcome of a converged :func:`run_critique_loop`.

    ``final_steps`` is the step set as of the converging round.
    ``transcript`` is the rendered ``compile_critique.md`` content the
    pipeline persists. ``cost_usd`` is the summed cost of every critic
    invocation across all rounds.
    """

    rounds: list[CritiqueRound]
    final_steps: list[StepFrontmatter]
    transcript: str
    cost_usd: float


# --------------------------------------------------------------------------- #
# Critic prompts
# --------------------------------------------------------------------------- #
#
# Templates are the *static* persona-lens text only. The dynamic sections
# (playbook / brief / roster / step set / trace) are appended by
# ``_build_critic_prompt`` via plain concatenation -- never ``str.format``
# -- because the playbook and trace are free text that can contain ``{``
# / ``}`` which ``format`` would try to interpret.

AKAGI_CRITIC_PROMPT_TEMPLATE = """\
You are Akagi, the EXECUTION critic for a compiled team workflow.

Review only the execution dimensions of the step set below:
  - routing: are on_approve / on_revise / on_block targets sound, and is
    there a clean path to __done__ with no dead ends or orphan steps?
  - delegation: are responsibilities cleanly split across the assigned
    personas, with no step doing two jobs at once?
  - dev/QA fan-out: is the developer -> QA hand-off coherent (work is
    produced before it is reviewed; review can rewind to the right step)?

Do NOT review wording, testing gates, or doc landing -- Ayako owns the
QA lens. Judge only whether this workflow will *execute* correctly."""


AYAKO_CRITIC_PROMPT_TEMPLATE = """\
You are Ayako, the QA critic for a compiled team workflow.

Review only the QA dimensions of the step set below:
  - testing gates: is there a step that actually verifies the work
    (tests / review) before the workflow reaches __done__?
  - critique-loop sizing: are the max_iters caps realistic -- not 1
    (no room to iterate) and not absurdly high (runaway cost)?
  - doc landing: does a doc / finalize step actually land documentation
    before __done__, rather than assuming it happens implicitly?

Do NOT review routing mechanics or delegation -- Akagi owns the
execution lens. Judge only whether quality is adequately gated."""


_VERDICT_DIRECTIVE = """\
## Your verdict

Put your analysis first, then reply on the LAST line with exactly one of:
    WORKFLOW: APPROVE
    WORKFLOW: REVISE: <one-line reasons>
Reasons are free text, < 200 chars. Use REVISE if any concern in your
lens is unaddressed; use APPROVE only when your lens is fully satisfied."""


def _build_critic_prompt(
    template: str,
    *,
    playbook_text: str,
    task_brief: str,
    roster: list[str],
    rendered_steps: str,
    trace: str,
) -> str:
    """Assemble one critic's full prompt from the static template + inputs.

    Order: persona lens, then the two verbatim inputs (playbook, brief),
    the roster, the current step set, the Tier 1 dry-run trace (the
    critic's execution context), and finally the verdict directive so it
    is the freshest instruction in the model's context.
    """
    roster_block = "\n".join(f"  - {name}" for name in roster)
    sections = [
        template,
        "## Playbook (verbatim)\n\n" + playbook_text,
        "## Task brief (verbatim)\n\n" + task_brief,
        "## Roster\n\n" + roster_block,
        "## Current step set\n\n" + rendered_steps,
        "## Dry-run trace (Tier 1)\n\n" + trace,
        _VERDICT_DIRECTIVE,
    ]
    return "\n\n".join(sections) + "\n"


# --------------------------------------------------------------------------- #
# Step rendering (for the prompt) + transcript helpers
# --------------------------------------------------------------------------- #


def _render_value(value: object) -> str:
    """Render one frontmatter value for the step set shown to critics."""
    if isinstance(value, list):
        return "[" + ", ".join(str(item) for item in value) + "]"
    return str(value)


def _render_steps(steps: list[StepFrontmatter]) -> str:
    """Render the step set in the canonical ``## step:`` + frontmatter form.

    Bodies are not available at this layer (the loop operates on parsed
    :class:`StepFrontmatter`, not raw step files); the critics review
    structure and routing, which live entirely in the frontmatter, with
    the dry-run trace supplying the execution walk.
    """
    blocks: list[str] = []
    for step in steps:
        data = step.to_dict()
        fm = "\n".join(f"{key}: {_render_value(data[key])}" for key in _RENDER_KEYS)
        blocks.append(f"## step: {step.id}\n---\n{fm}\n---")
    return "\n\n".join(blocks)


# --------------------------------------------------------------------------- #
# Verdict parsing
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #
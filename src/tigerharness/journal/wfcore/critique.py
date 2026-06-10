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
from tigerharness.workflow_runner.sessions import InvocationResult, SessionManager
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


class CritiqueParseError(ValueError):
    """A critic's verdict could not be obtained as an APPROVE/REVISE.

    Two distinct failures collapse here because the pipeline handles them
    identically (re-prompt or abort -- a policy that is deliberately *not*
    this module's responsibility):

    * the invocation itself failed (timeout / non-zero exit), so there is
      no trustworthy reply to parse -- ``reason`` carries the underlying
      ``InvocationResult.error``; or
    * the invocation succeeded but its trailer was missing, malformed, or
      a ``BLOCK`` (not a valid *critic* verdict) -- ``reason`` is ``None``.

    Carries the offending ``persona`` and the verbatim ``raw_response``
    (the critic's stdout, possibly partial on a timeout) for the event log.
    """

    def __init__(
        self, *, persona: str, raw_response: str, reason: str | None = None
    ) -> None:
        if reason is None:
            message = (
                f"could not parse a WORKFLOW verdict from {persona!r}'s response"
            )
        else:
            message = f"no verdict from {persona!r}: {reason}"
        super().__init__(message)
        self.persona = persona
        self.raw_response = raw_response
        self.reason = reason


class CritiqueAbortedError(RuntimeError):
    """The loop hit ``max_compile_iters`` without dual-APPROVE convergence.

    ``rounds`` is every :class:`CritiqueRound` run; ``last_verdicts`` is
    the final round's verdicts, so the pipeline can emit a
    ``compile_failed{tier:2, last_verdicts:{...}}`` event.

    The failure path also carries the same artifacts the success path
    returns: ``transcript`` (so ``compile_critique.md`` lands even on a
    tier-2 abort -- the Operator debugging the failure wants the rounds
    the critics kept rejecting) and ``cost_usd`` (the spend is real
    whether or not it converged; the pipeline owns the policy decision
    of whether to count it against the ceiling, but it needs the number
    to make it). Both default so a handler test can still construct the
    error with just ``rounds`` + ``last_verdicts`` per the brief.
    """

    def __init__(
        self,
        *,
        rounds: list[CritiqueRound],
        last_verdicts: list[CritiqueVerdict],
        transcript: str = "",
        cost_usd: float = 0.0,
    ) -> None:
        super().__init__(
            f"compile critique did not converge after {len(rounds)} rounds"
        )
        self.rounds = rounds
        self.last_verdicts = last_verdicts
        self.transcript = transcript
        self.cost_usd = cost_usd


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


def _verdict_line(verdict: CritiqueVerdict) -> str:
    """Render one verdict as a transcript bullet."""
    name = verdict.persona.capitalize()
    if verdict.decision == "APPROVE":
        return f"**{name}:** APPROVE"
    return f"**{name}:** REVISE — {verdict.reasons}"


def _summarize_steps(steps: list[StepFrontmatter]) -> str:
    """One line per re-drafted step: id, persona/role, and approve target."""
    return "\n".join(
        f"- {s.id} [{s.persona}/{s.role}] -> {s.on_approve}" for s in steps
    )


def _combine_feedback(verdicts: list[CritiqueVerdict]) -> str:
    """Join every REVISE verdict's reasons into one feedback block."""
    return "\n".join(
        f"{v.persona.capitalize()} (REVISE): {v.reasons}"
        for v in verdicts
        if v.decision == "REVISE"
    )


# --------------------------------------------------------------------------- #
# Verdict parsing
# --------------------------------------------------------------------------- #


def _parse_verdict(persona: str, result: InvocationResult) -> CritiqueVerdict:
    """Turn one critic's :class:`InvocationResult` into a typed verdict.

    A failed invocation is rejected before parsing: ``invoke`` does not
    raise on timeout/non-zero exit, it returns a result with ``error``
    set and (at best) partial stdout, so an errored reply carries no
    trustworthy verdict -- even if a stray trailer survived in the
    partial output. This mirrors the sibling drafter
    (:func:`compile.drafter.draft_steps`), which checks ``result.error``
    before parsing for the same reason.

    On a clean invocation the grammar is delegated to
    :func:`trailer.parse_trailer` so the ``WORKFLOW:`` trailer has a
    single authority. A critic may only APPROVE or REVISE; a ``BLOCK`` or
    an unparseable trailer violates the critique contract. All three
    failure modes raise :class:`CritiqueParseError`.
    """
    if result.error is not None:
        raise CritiqueParseError(
            persona=persona,
            raw_response=result.stdout,
            reason=f"invocation failed: {result.error}",
        )
    verdict = parse_trailer(result.stdout)
    if isinstance(verdict, Approve):
        return CritiqueVerdict(persona=persona, decision="APPROVE", reasons="")
    if isinstance(verdict, Revise):
        return CritiqueVerdict(
            persona=persona, decision="REVISE", reasons=verdict.summary
        )
    raise CritiqueParseError(persona=persona, raw_response=result.stdout)


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #


def run_critique_loop(
    *,
    initial_steps: list[StepFrontmatter],
    playbook_text: str,
    task_brief: str,
    roster: list[str],
    trace: str,
    session_manager: SessionManager,
    drafter: Callable[[str], list[StepFrontmatter]],
    max_compile_iters: int = 8,
    hard_floor_iters: int = 3,
) -> CritiqueResult:
    """Run the Tier 2 forced critique loop to convergence.

    Parameters
    ----------
    initial_steps:
        The drafter's validated candidate set (round 1's subject).
    playbook_text, task_brief, roster, trace:
        Critic context, all rendered verbatim into both prompts.
    session_manager:
        The LLM seam; ``invoke`` is called once per critic per round.
    drafter:
        Injected re-draft callable: given the combined REVISE feedback,
        returns a fresh step set. Called once after any round that
        contains a REVISE.
    max_compile_iters:
        Hard ceiling on rounds (ADR D3 / spec Tier 2). Reaching it
        without convergence raises :class:`CritiqueAbortedError`.
    hard_floor_iters:
        Minimum rounds run even on early dual-APPROVE (ADR D3).

    Returns
    -------
    CritiqueResult
        On the first round at/after the floor where both critics APPROVE.

    Raises
    ------
    ValueError
        If the iteration bounds are inconsistent (caught up front so a
        misconfiguration fails loud rather than looping to the ceiling).
    CritiqueParseError
        If a critic's invocation failed (timeout / non-zero exit) or its
        reply has no parseable APPROVE/REVISE trailer.
    CritiqueAbortedError
        If the ceiling is reached without dual-APPROVE convergence.
    """
    if hard_floor_iters < 1:
        raise ValueError(
            f"hard_floor_iters must be >= 1, got {hard_floor_iters!r}"
        )
    if max_compile_iters < hard_floor_iters:
        raise ValueError(
            f"max_compile_iters ({max_compile_iters}) must be >= "
            f"hard_floor_iters ({hard_floor_iters})"
        )

    steps = list(initial_steps)
    rounds: list[CritiqueRound] = []
    transcript_lines: list[str] = []
    total_cost = 0.0
    round_num = 0

    while True:
        round_num += 1
        # Render the step set once; both critics see the same set, only
        # their persona lens (the template) differs.
        rendered = _render_steps(steps)
        akagi_prompt = _build_critic_prompt(
            AKAGI_CRITIC_PROMPT_TEMPLATE,
            playbook_text=playbook_text,
            task_brief=task_brief,
            roster=roster,
            rendered_steps=rendered,
            trace=trace,
        )
        ayako_prompt = _build_critic_prompt(
            AYAKO_CRITIC_PROMPT_TEMPLATE,
            playbook_text=playbook_text,
            task_brief=task_brief,
            roster=roster,
            rendered_steps=rendered,
            trace=trace,
        )

        # Two independent lenses; sequential because the seam is sync.
        akagi_res = session_manager.invoke(
            _AKAGI_PERSONA, akagi_prompt, timeout_sec=_CRITIC_TIMEOUT_SEC
        )
        ayako_res = session_manager.invoke(
            _AYAKO_PERSONA, ayako_prompt, timeout_sec=_CRITIC_TIMEOUT_SEC
        )
        total_cost += akagi_res.cost_usd + ayako_res.cost_usd

        akagi_verdict = _parse_verdict(_AKAGI_PERSONA, akagi_res)
        ayako_verdict = _parse_verdict(_AYAKO_PERSONA, ayako_res)
        verdicts = [akagi_verdict, ayako_verdict]
        all_approve = all(v.decision == "APPROVE" for v in verdicts)

        rounds.append(
            CritiqueRound(
                round_num=round_num,
                verdicts=verdicts,
                all_approve=all_approve,
            )
        )

        transcript_lines.append(f"## Round {round_num}")
        transcript_lines.append("")
        transcript_lines.append(_verdict_line(akagi_verdict))
        transcript_lines.append(_verdict_line(ayako_verdict))
        transcript_lines.append("")

        if all_approve and round_num >= hard_floor_iters:
            transcript_lines.append(
                "_Both critics APPROVE and the "
                f"{hard_floor_iters}-round hard floor is met; "
                "compile critique converged._"
            )
            transcript_lines.append("")
            return CritiqueResult(
                rounds=rounds,
                final_steps=steps,
                transcript="\n".join(transcript_lines),
                cost_usd=total_cost,
            )

        if round_num >= max_compile_iters:
            transcript_lines.append(
                f"_Ceiling of {max_compile_iters} rounds reached without "
                "dual-APPROVE convergence; compile critique aborted._"
            )
            transcript_lines.append("")
            raise CritiqueAbortedError(
                rounds=rounds,
                last_verdicts=verdicts,
                transcript="\n".join(transcript_lines),
                cost_usd=total_cost,
            )

        if all_approve:
            # Floor not yet met: force another look, same steps (there is
            # no feedback to integrate when both critics approve).
            transcript_lines.append(
                "_Both critics APPROVE but the "
                f"{hard_floor_iters}-round hard floor is not yet met; "
                "forcing another critique round._"
            )
        else:
            steps = list(drafter(_combine_feedback(verdicts)))
            transcript_lines.append("Anzai responded with:")
            transcript_lines.append("")
            transcript_lines.append(_summarize_steps(steps))
        transcript_lines.append("")

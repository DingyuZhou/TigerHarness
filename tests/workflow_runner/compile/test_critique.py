"""Unit tests for ``tigerharness.workflow_runner.compile.critique``.

The critique loop talks to the LLM only through ``SessionManager.invoke``;
every test injects :class:`ScriptedSessionManager`, which serves a
per-persona queue of scripted :class:`InvocationResult`s so the two
critics can be driven independently across rounds without spawning a
``claude`` subprocess. The re-draft step is an injected
:class:`FakeDrafter`, so no real drafter (or its LLM call) runs either.

Coverage map (the 7 brief-required cases + the structural branches the
100% line+branch floor demands):

* happy_path_after_floor               -> brief #1
* floor_forces_three_rounds            -> brief #2
* revise_then_approve                  -> brief #3
* ceiling_aborts_with_clear_error      -> brief #4
* parser_rejects_malformed_response    -> brief #5
* akagi_revise_ayako_approve_round_2   -> brief #6
* cost_summed_across_rounds            -> brief #7
* parser_rejects_block_trailer         -> the non-APPROVE/REVISE raise branch
* invocation_error_surfaces_real_cause -> the result.error pre-parse branch
* invalid_floor / max_below_floor      -> the input-guard branches
* critic_prompts_contain_context       -> prompt assembly + render branches
* invoke_args                          -> persona order + timeout passthrough
"""

from __future__ import annotations

import pytest

from tigerharness.workflow_runner.compile.critique import (
    AKAGI_CRITIC_PROMPT_TEMPLATE,
    AYAKO_CRITIC_PROMPT_TEMPLATE,
    CritiqueAbortedError,
    CritiqueParseError,
    CritiqueResult,
    CritiqueRound,
    CritiqueVerdict,
    run_critique_loop,
)
from tigerharness.workflow_runner.models import StepFrontmatter
from tigerharness.workflow_runner.sessions import InvocationResult

from tests.workflow_runner.compile.conftest import RecordedCall

ROSTER = ["anzai", "akagi", "ayako", "rukawa"]


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #


class ScriptedSessionManager:
    """Serves a per-persona FIFO of scripted ``InvocationResult``s.

    Keying by persona (rather than by global call order) keeps the
    akagi/ayako interleaving order-independent in the test bodies: each
    ``invoke`` pops the next result for that persona.
    """

    def __init__(
        self,
        *,
        akagi: list[InvocationResult],
        ayako: list[InvocationResult],
    ) -> None:
        self._scripts = {"akagi": list(akagi), "ayako": list(ayako)}
        self.calls: list[RecordedCall] = []

    def invoke(self, persona, prompt, *, timeout_sec, log_dir=None):
        self.calls.append(
            RecordedCall(
                persona=persona,
                prompt=prompt,
                timeout_sec=timeout_sec,
                log_dir=log_dir,
            )
        )
        return self._scripts[persona].pop(0)


class FakeDrafter:
    """Injected re-draft callable; records every feedback string."""

    def __init__(self, outputs: list[list[StepFrontmatter]]) -> None:
        self._outputs = list(outputs)
        self.feedbacks: list[str] = []

    def __call__(self, feedback: str) -> list[StepFrontmatter]:
        self.feedbacks.append(feedback)
        return self._outputs.pop(0)


def _invocation(stdout: str, *, cost: float = 0.0) -> InvocationResult:
    return InvocationResult(
        stdout=stdout,
        session_id="sid",
        cost_usd=cost,
        exit_code=0,
        error=None,
        raw_envelope={},
    )


def _errored(error: str, *, stdout: str = "", cost: float = 0.0) -> InvocationResult:
    """A failed invocation: ``error`` set, stdout possibly partial.

    Mirrors what ``SessionManager.invoke`` returns on timeout / non-zero
    exit -- it does not raise, it reports the failure in ``error``.
    """
    return InvocationResult(
        stdout=stdout,
        session_id="sid",
        cost_usd=cost,
        exit_code=-1001,
        error=error,
        raw_envelope={},
    )


def critic(decision: str, reasons: str = "", *, cost: float = 0.0) -> InvocationResult:
    """Build a critic reply whose last line is the WORKFLOW trailer."""
    if decision == "APPROVE":
        trailer = "WORKFLOW: APPROVE"
    else:
        trailer = f"WORKFLOW: REVISE: {reasons}"
    return _invocation(f"Analysis of the plan.\n\n{trailer}", cost=cost)


def _step(
    sid: str,
    *,
    persona: str = "anzai",
    role: str = "planner",
    on_approve: str = "__done__",
    on_revise: str | None = None,
    on_block: str = "__escalate__",
    max_iters: int = 5,
    timeout_sec: int = 1800,
) -> StepFrontmatter:
    return StepFrontmatter.from_dict(
        {
            "id": sid,
            "persona": persona,
            "role": role,
            "on_approve": on_approve,
            "on_revise": on_revise if on_revise is not None else sid,
            "on_block": on_block,
            "max_iters": max_iters,
            "timeout_sec": timeout_sec,
            "parallel_with": [],
        }
    )


def _plan(label: str) -> list[StepFrontmatter]:
    """A 2-step plan whose ids embed ``label`` so a test can tell which
    draft produced ``final_steps``."""
    return [
        _step(
            f"01-{label}-plan",
            persona="anzai",
            role="planner",
            on_approve=f"02-{label}-qa",
            on_revise=f"01-{label}-plan",
        ),
        _step(
            f"02-{label}-qa",
            persona="ayako",
            role="qa",
            on_approve="__done__",
            on_revise=f"01-{label}-plan",
        ),
    ]


def _run(sm, drafter, **overrides):
    """Invoke the loop with canonical defaults; ``overrides`` win."""
    kwargs = dict(
        initial_steps=_plan("init"),
        playbook_text="PLAYBOOK BODY",
        task_brief="BRIEF BODY",
        roster=ROSTER,
        trace="TRACE BODY",
        session_manager=sm,
        drafter=drafter,
    )
    kwargs.update(overrides)
    return run_critique_loop(**kwargs)


# --------------------------------------------------------------------------- #
# Brief #1 -- happy path: three rounds, all APPROVE, exits at the floor
# --------------------------------------------------------------------------- #


def test_happy_path_after_floor() -> None:
    sm = ScriptedSessionManager(
        akagi=[critic("APPROVE"), critic("APPROVE"), critic("APPROVE")],
        ayako=[critic("APPROVE"), critic("APPROVE"), critic("APPROVE")],
    )
    drafter = FakeDrafter([])

    result = _run(sm, drafter)

    assert isinstance(result, CritiqueResult)
    assert [r.round_num for r in result.rounds] == [1, 2, 3]
    assert all(r.all_approve for r in result.rounds)
    for rnd in result.rounds:
        assert isinstance(rnd, CritiqueRound)
        assert [v.persona for v in rnd.verdicts] == ["akagi", "ayako"]
        assert all(v.decision == "APPROVE" for v in rnd.verdicts)
        assert all(isinstance(v, CritiqueVerdict) for v in rnd.verdicts)
    # All three rounds present in the transcript; converged note at the end.
    for n in (1, 2, 3):
        assert f"## Round {n}" in result.transcript
    assert "compile critique converged" in result.transcript
    # No REVISE -> the drafter was never invoked.
    assert drafter.feedbacks == []
    # Steps unchanged through to the converging round.
    assert result.final_steps == _plan("init")


# --------------------------------------------------------------------------- #
# Brief #2 -- the floor forces 3 rounds even when round 1 dual-APPROVEs
# --------------------------------------------------------------------------- #


def test_floor_forces_three_rounds_even_when_round_1_approves() -> None:
    sm = ScriptedSessionManager(
        akagi=[critic("APPROVE"), critic("APPROVE"), critic("APPROVE")],
        ayako=[critic("APPROVE"), critic("APPROVE"), critic("APPROVE")],
    )
    drafter = FakeDrafter([])

    result = _run(sm, drafter)

    # Did NOT short-circuit on the round-1 dual-APPROVE.
    assert len(result.rounds) == 3
    assert result.rounds[-1].round_num == 3
    # Two critic invocations per round.
    assert len(sm.calls) == 6
    # The forcing note appears for rounds 1 and 2, not the converging one.
    assert result.transcript.count("forcing another critique round") == 2


# --------------------------------------------------------------------------- #
# Brief #3 -- REVISE on round 1, then converge; final steps are the re-draft
# --------------------------------------------------------------------------- #


def test_revise_then_approve() -> None:
    sm = ScriptedSessionManager(
        akagi=[critic("REVISE", "routing skips QA"), critic("APPROVE"), critic("APPROVE")],
        ayako=[critic("APPROVE"), critic("APPROVE"), critic("APPROVE")],
    )
    drafter = FakeDrafter([_plan("redraft")])

    result = _run(sm, drafter)

    # Drafter called exactly once, between rounds 1 and 2.
    assert len(drafter.feedbacks) == 1
    assert "routing skips QA" in drafter.feedbacks[0]
    assert "Akagi (REVISE)" in drafter.feedbacks[0]
    # Final steps come from the round-2 re-draft, not the initial set.
    assert result.final_steps == _plan("redraft")
    assert [s.id for s in result.final_steps] == ["01-redraft-plan", "02-redraft-qa"]
    assert len(result.rounds) == 3
    assert result.rounds[0].all_approve is False
    assert "Anzai responded with:" in result.transcript
    assert "01-redraft-plan" in result.transcript


# --------------------------------------------------------------------------- #
# Brief #4 -- ceiling abort with rounds + last verdicts attached
# --------------------------------------------------------------------------- #


def test_ceiling_aborts_with_clear_error() -> None:
    sm = ScriptedSessionManager(
        akagi=[
            critic("REVISE", "a", cost=0.01),
            critic("REVISE", "b", cost=0.01),
            critic("REVISE", "c", cost=0.01),
        ],
        ayako=[
            critic("REVISE", "x", cost=0.01),
            critic("REVISE", "y", cost=0.01),
            critic("REVISE", "z", cost=0.01),
        ],
    )
    # Round 1 + round 2 re-draft; round 3 aborts before any re-draft.
    drafter = FakeDrafter([_plan("d1"), _plan("d2")])

    with pytest.raises(CritiqueAbortedError) as excinfo:
        _run(sm, drafter, max_compile_iters=3, hard_floor_iters=3)

    err = excinfo.value
    assert len(err.rounds) == 3
    assert all(not r.all_approve for r in err.rounds)
    assert len(err.last_verdicts) == 2
    assert all(v.decision == "REVISE" for v in err.last_verdicts)
    # Re-drafted only after rounds 1 and 2.
    assert len(drafter.feedbacks) == 2
    # Failure path carries the same artifacts the success path returns:
    # the full transcript (so compile_critique.md lands on abort) and the
    # real spend (so the pipeline can decide ceiling accounting).
    assert err.cost_usd == pytest.approx(0.06)
    for n in (1, 2, 3):
        assert f"## Round {n}" in err.transcript
    assert "compile critique aborted" in err.transcript


# --------------------------------------------------------------------------- #
# Brief #5 -- malformed reply (no WORKFLOW trailer) -> CritiqueParseError
# --------------------------------------------------------------------------- #


def test_parser_rejects_malformed_response() -> None:
    sm = ScriptedSessionManager(
        akagi=[_invocation("I think this looks fine?")],
        ayako=[critic("APPROVE")],
    )
    drafter = FakeDrafter([])

    with pytest.raises(CritiqueParseError) as excinfo:
        _run(sm, drafter)

    err = excinfo.value
    assert err.persona == "akagi"
    assert err.raw_response == "I think this looks fine?"


def test_invocation_error_surfaces_real_cause() -> None:
    # A timed-out / crashed critic returns error set + (here) empty stdout.
    # The loop must surface the real cause, not a misleading "could not
    # parse a WORKFLOW verdict" on the empty reply.
    sm = ScriptedSessionManager(
        akagi=[_errored("timed out after 600s", cost=0.02)],
        ayako=[critic("APPROVE", cost=0.01)],
    )
    drafter = FakeDrafter([])

    with pytest.raises(CritiqueParseError) as excinfo:
        _run(sm, drafter)

    err = excinfo.value
    assert err.persona == "akagi"
    assert err.reason == "invocation failed: timed out after 600s"
    assert err.raw_response == ""
    assert "timed out after 600s" in str(err)


def test_parser_rejects_block_trailer() -> None:
    # BLOCK is a valid trailer verb but not a valid *critic* verdict.
    sm = ScriptedSessionManager(
        akagi=[critic("APPROVE")],
        ayako=[_invocation("Analysis.\n\nWORKFLOW: BLOCK: cannot proceed")],
    )
    drafter = FakeDrafter([])

    with pytest.raises(CritiqueParseError) as excinfo:
        _run(sm, drafter)

    assert excinfo.value.persona == "ayako"


# --------------------------------------------------------------------------- #
# Brief #6 -- a single REVISE from either critic continues the loop
# --------------------------------------------------------------------------- #


def test_akagi_revise_ayako_approve_triggers_round_2() -> None:
    # hard_floor_iters=1 means a clean round 1 would terminate immediately;
    # the loop only reaches round 2 because of the single REVISE.
    sm = ScriptedSessionManager(
        akagi=[critic("APPROVE"), critic("APPROVE")],
        ayako=[critic("REVISE", "no test gate"), critic("APPROVE")],
    )
    drafter = FakeDrafter([_plan("redraft")])

    result = _run(sm, drafter, hard_floor_iters=1)

    assert len(result.rounds) == 2
    assert result.rounds[0].all_approve is False
    assert result.rounds[1].all_approve is True
    # The REVISE feedback came from Ayako only (Akagi APPROVE is excluded).
    assert len(drafter.feedbacks) == 1
    assert "Ayako (REVISE): no test gate" in drafter.feedbacks[0]
    assert "Akagi" not in drafter.feedbacks[0]


# --------------------------------------------------------------------------- #
# Brief #7 -- cost summed across every critic invocation
# --------------------------------------------------------------------------- #


def test_cost_summed_across_rounds() -> None:
    sm = ScriptedSessionManager(
        akagi=[
            critic("APPROVE", cost=0.05),
            critic("APPROVE", cost=0.03),
            critic("APPROVE", cost=0.02),
        ],
        ayako=[
            critic("APPROVE", cost=0.05),
            critic("APPROVE", cost=0.04),
            critic("APPROVE", cost=0.04),
        ],
    )
    drafter = FakeDrafter([])

    result = _run(sm, drafter)

    assert result.cost_usd == pytest.approx(0.23)


# --------------------------------------------------------------------------- #
# Input-guard branches
# --------------------------------------------------------------------------- #


def test_invalid_floor_raises() -> None:
    sm = ScriptedSessionManager(akagi=[], ayako=[])
    with pytest.raises(ValueError, match="hard_floor_iters must be >= 1"):
        _run(sm, FakeDrafter([]), hard_floor_iters=0)


def test_max_below_floor_raises() -> None:
    sm = ScriptedSessionManager(akagi=[], ayako=[])
    with pytest.raises(ValueError, match="must be >= "):
        _run(sm, FakeDrafter([]), max_compile_iters=2, hard_floor_iters=3)


# --------------------------------------------------------------------------- #
# Prompt assembly + invocation-arg passthrough
# --------------------------------------------------------------------------- #


def test_critic_prompts_contain_context() -> None:
    sm = ScriptedSessionManager(
        akagi=[critic("APPROVE")] * 3,
        ayako=[critic("APPROVE")] * 3,
    )
    _run(sm, FakeDrafter([]))

    akagi_prompt = sm.calls[0].prompt
    ayako_prompt = sm.calls[1].prompt

    for prompt in (akagi_prompt, ayako_prompt):
        assert "PLAYBOOK BODY" in prompt
        assert "BRIEF BODY" in prompt
        assert "TRACE BODY" in prompt
        assert "- anzai" in prompt  # roster rendered
        assert "## step: 01-init-plan" in prompt  # step set rendered
        assert "parallel_with: []" in prompt  # list value rendered
        assert "WORKFLOW: APPROVE" in prompt  # verdict directive
        assert "WORKFLOW: REVISE: <one-line reasons>" in prompt
    # Each critic gets only its own lens.
    assert AKAGI_CRITIC_PROMPT_TEMPLATE in akagi_prompt
    assert AYAKO_CRITIC_PROMPT_TEMPLATE in ayako_prompt
    assert "EXECUTION critic" in akagi_prompt
    assert "QA critic" in ayako_prompt


def test_invoke_args_persona_order_and_timeout() -> None:
    sm = ScriptedSessionManager(
        akagi=[critic("APPROVE")] * 3,
        ayako=[critic("APPROVE")] * 3,
    )
    _run(sm, FakeDrafter([]))

    assert [c.persona for c in sm.calls[:2]] == ["akagi", "ayako"]
    assert all(c.timeout_sec == 600 for c in sm.calls)
    assert all(c.log_dir is None for c in sm.calls)

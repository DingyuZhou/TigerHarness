"""Unit tests for ``tigerharness.journal.wfcore.drafter``.

The drafter talks to the LLM only through ``SessionManager.invoke``;
every test injects the in-memory :class:`FakeSessionManager` from
``conftest.py``, so no ``claude`` subprocess runs and the suite is
deterministic.

Coverage map (the 7 brief-required cases + the structural branches the
100% line+branch floor demands):

* happy_path_three_step            -> brief #1
* with_feedback_prologue           -> brief #2
* malformed_response_raises        -> brief #3 (missing closing ---)
* unknown_field_raises             -> brief #4
* roster_appears_in_prompt         -> brief #5
* playbook_and_brief_in_prompt     -> brief #6
* cost_roundtrip                   -> brief #7
* no_bundle_fence / unclosed_fence / no_step_headers / missing_open_delim
  / invalid_yaml / non_mapping_frontmatter / from_dict_error
  / invoke_error                   -> remaining parse branches
* blank_lines_after_header_ok      -> the leading-blank-skip branch
* invoke_args_default              -> persona + timeout passthrough
"""

from __future__ import annotations

import pytest

from tigerharness.journal.wfcore.drafter import (
    DrafterParseError,
    DrafterResult,
    draft_steps,
)
from tigerharness.journal.wfcore.models import StepFrontmatter

from tests.journal.wfcore.conftest import (
    FakeSessionManager,
    StepSpec,
    make_response,
    three_step_specs,
)


# --------------------------------------------------------------------------- #
# Brief #1 -- happy path
# --------------------------------------------------------------------------- #


def test_happy_path_three_step() -> None:
    response = make_response(three_step_specs())
    fsm = FakeSessionManager(stdout=response, cost_usd=0.10)

    result = draft_steps(
        playbook_text="PLAYBOOK",
        task_brief="BRIEF",
        roster=["anzai", "akagi"],
        session_manager=fsm,
    )

    assert isinstance(result, DrafterResult)
    assert [s.id for s in result.steps] == [
        "01-anzai-plan",
        "02-akagi-critique",
        "03-anzai-revise",
    ]
    assert [s.persona for s in result.steps] == ["anzai", "akagi", "anzai"]
    assert [s.on_approve for s in result.steps] == [
        "02-akagi-critique",
        "03-anzai-revise",
        "__done__",
    ]
    # All parsed objects are real, validated StepFrontmatter instances.
    assert all(isinstance(s, StepFrontmatter) for s in result.steps)
    # Raw response preserved verbatim for the transcript.
    assert result.raw_response == response


# --------------------------------------------------------------------------- #
# Brief #2 -- feedback prologue reaches the prompt
# --------------------------------------------------------------------------- #


def test_with_feedback_prologue() -> None:
    response = make_response(three_step_specs())
    fsm = FakeSessionManager(stdout=response)

    draft_steps(
        playbook_text="PLAYBOOK",
        task_brief="BRIEF",
        roster=["anzai"],
        session_manager=fsm,
        feedback="X is wrong: the QA step has no test gate",
    )

    prompt = fsm.calls[0].prompt
    assert "## Critic feedback to address" in prompt
    assert "X is wrong: the QA step has no test gate" in prompt


def test_no_feedback_section_when_feedback_none() -> None:
    response = make_response(three_step_specs())
    fsm = FakeSessionManager(stdout=response)

    draft_steps(
        playbook_text="PLAYBOOK",
        task_brief="BRIEF",
        roster=["anzai"],
        session_manager=fsm,
    )

    assert "## Critic feedback to address" not in fsm.calls[0].prompt


# --------------------------------------------------------------------------- #
# Brief #3 -- malformed response (missing closing --- delimiter)
# --------------------------------------------------------------------------- #


def test_malformed_response_raises() -> None:
    bad = StepSpec(
        id="01-anzai-plan",
        persona="anzai",
        role="planner",
        on_approve="__done__",
        on_revise="01-anzai-plan",
        drop_close_delim=True,
    )
    response = make_response([bad])
    fsm = FakeSessionManager(stdout=response)

    with pytest.raises(DrafterParseError) as exc_info:
        draft_steps(
            playbook_text="PB",
            task_brief="TB",
            roster=["anzai"],
            session_manager=fsm,
        )

    assert exc_info.value.raw_response == response
    assert "closing" in str(exc_info.value)


def test_missing_opening_delim_raises() -> None:
    bad = StepSpec(
        id="01-anzai-plan",
        persona="anzai",
        role="planner",
        on_approve="__done__",
        on_revise="01-anzai-plan",
        drop_open_delim=True,
    )
    response = make_response([bad])
    fsm = FakeSessionManager(stdout=response)

    with pytest.raises(DrafterParseError) as exc_info:
        draft_steps(
            playbook_text="PB",
            task_brief="TB",
            roster=["anzai"],
            session_manager=fsm,
        )

    assert "opening" in str(exc_info.value)
    assert exc_info.value.raw_response == response


# --------------------------------------------------------------------------- #
# Brief #4 -- unknown frontmatter field
# --------------------------------------------------------------------------- #


def test_unknown_field_raises() -> None:
    bad = StepSpec(
        id="01-anzai-plan",
        persona="anzai",
        role="planner",
        on_approve="__done__",
        on_revise="01-anzai-plan",
        extra_fm_lines=["bogus_field: foo"],
    )
    response = make_response([bad])
    fsm = FakeSessionManager(stdout=response)

    with pytest.raises(DrafterParseError) as exc_info:
        draft_steps(
            playbook_text="PB",
            task_brief="TB",
            roster=["anzai"],
            session_manager=fsm,
        )

    assert "bogus_field" in str(exc_info.value)
    assert exc_info.value.raw_response == response


# --------------------------------------------------------------------------- #
# Brief #5 -- roster reaches the prompt
# --------------------------------------------------------------------------- #


def test_roster_appears_in_prompt() -> None:
    response = make_response(three_step_specs())
    fsm = FakeSessionManager(stdout=response)
    roster = ["anzai", "akagi", "ayako", "rukawa", "mitsui"]

    draft_steps(
        playbook_text="PB",
        task_brief="TB",
        roster=roster,
        session_manager=fsm,
    )

    prompt = fsm.calls[0].prompt
    for name in roster:
        assert name in prompt


# --------------------------------------------------------------------------- #
# Brief #6 -- playbook + brief reach the prompt verbatim
# --------------------------------------------------------------------------- #


def test_playbook_and_brief_in_prompt() -> None:
    response = make_response(three_step_specs())
    fsm = FakeSessionManager(stdout=response)
    playbook = "## My Playbook\nUnique-playbook-marker-7f2a9c14\n"
    brief = "Add cache eviction. Unique-brief-marker-d8e1abf2."

    draft_steps(
        playbook_text=playbook,
        task_brief=brief,
        roster=["anzai"],
        session_manager=fsm,
    )

    prompt = fsm.calls[0].prompt
    assert playbook in prompt
    assert brief in prompt


def test_frontmatter_contract_and_protocol_reach_prompt() -> None:
    """The prompt must teach Anzai the contract + output format.

    The prompt is the load-bearing artifact of this module: if these
    tokens silently drop out (a careless refactor of the contract or
    protocol constants), the LLM emits an unparseable shape and the whole
    compile pipeline breaks with parse failures. Guard the essential,
    stable tokens so that failure is caught here, not in production.
    """
    response = make_response(three_step_specs())
    fsm = FakeSessionManager(stdout=response)

    draft_steps(
        playbook_text="PB",
        task_brief="TB",
        roster=["anzai"],
        session_manager=fsm,
    )

    prompt = fsm.calls[0].prompt
    # Every required frontmatter field (colon-suffixed -> contract-specific).
    for field_token in (
        "id:",
        "persona:",
        "role:",
        "on_approve:",
        "on_revise:",
        "on_block:",
        "max_iters:",
        "timeout_sec:",
        "parallel_with:",
    ):
        assert field_token in prompt, field_token
    # The two routing sentinels (and nothing teaches others).
    assert "__done__" in prompt
    assert "__escalate__" in prompt
    # Output protocol: the bundle fence + the per-step header sentinel.
    assert "```steps-bundle" in prompt
    assert "## step:" in prompt
    # The trailer convention (brief point 7).
    assert "WORKFLOW: APPROVE" in prompt


# --------------------------------------------------------------------------- #
# Brief #7 -- cost round-trips unchanged
# --------------------------------------------------------------------------- #


def test_cost_roundtrip() -> None:
    response = make_response(three_step_specs())
    fsm = FakeSessionManager(stdout=response, cost_usd=0.42)

    result = draft_steps(
        playbook_text="PB",
        task_brief="TB",
        roster=["anzai"],
        session_manager=fsm,
    )

    assert result.cost_usd == 0.42


# --------------------------------------------------------------------------- #
# Remaining structural parse branches
# --------------------------------------------------------------------------- #


def test_no_bundle_fence_raises() -> None:
    response = make_response(three_step_specs(), open_fence=False)
    fsm = FakeSessionManager(stdout=response)

    with pytest.raises(DrafterParseError) as exc_info:
        draft_steps(
            playbook_text="PB",
            task_brief="TB",
            roster=["anzai"],
            session_manager=fsm,
        )

    assert "fence" in str(exc_info.value)
    assert exc_info.value.raw_response == response


def test_unclosed_bundle_fence_raises() -> None:
    response = make_response(three_step_specs(), close_fence=False)
    fsm = FakeSessionManager(stdout=response)

    with pytest.raises(DrafterParseError) as exc_info:
        draft_steps(
            playbook_text="PB",
            task_brief="TB",
            roster=["anzai"],
            session_manager=fsm,
        )

    assert "never closed" in str(exc_info.value)


def test_no_step_headers_raises() -> None:
    headerless = StepSpec(
        id="01-anzai-plan",
        persona="anzai",
        role="planner",
        on_approve="__done__",
        on_revise="01-anzai-plan",
        drop_header=True,
    )
    response = make_response([headerless])
    fsm = FakeSessionManager(stdout=response)

    with pytest.raises(DrafterParseError) as exc_info:
        draft_steps(
            playbook_text="PB",
            task_brief="TB",
            roster=["anzai"],
            session_manager=fsm,
        )

    assert "## step:" in str(exc_info.value)


def test_invalid_yaml_raises() -> None:
    bad = StepSpec(
        id="01-anzai-plan",
        persona="anzai",
        role="planner",
        on_approve="__done__",
        on_revise="01-anzai-plan",
        parallel_with="[a, b",  # unclosed flow sequence -> YAML parse error
    )
    response = make_response([bad])
    fsm = FakeSessionManager(stdout=response)

    with pytest.raises(DrafterParseError) as exc_info:
        draft_steps(
            playbook_text="PB",
            task_brief="TB",
            roster=["anzai"],
            session_manager=fsm,
        )

    assert "not valid YAML" in str(exc_info.value)


def test_non_mapping_frontmatter_raises() -> None:
    response = (
        "```steps-bundle\n"
        "## step: 01-bad\n"
        "---\n"
        "just a scalar string\n"
        "---\n"
        "body\n"
        "```\n"
        "WORKFLOW: APPROVE\n"
    )
    fsm = FakeSessionManager(stdout=response)

    with pytest.raises(DrafterParseError) as exc_info:
        draft_steps(
            playbook_text="PB",
            task_brief="TB",
            roster=["anzai"],
            session_manager=fsm,
        )

    assert "YAML mapping" in str(exc_info.value)


def test_from_dict_error_raises() -> None:
    # Valid delimiters + only known keys, but max_iters=0 is rejected by
    # StepFrontmatter.__post_init__ (must be > 0) -> wrapped as a parse error.
    bad = StepSpec(
        id="01-anzai-plan",
        persona="anzai",
        role="planner",
        on_approve="__done__",
        on_revise="01-anzai-plan",
        max_iters=0,
    )
    response = make_response([bad])
    fsm = FakeSessionManager(stdout=response)

    with pytest.raises(DrafterParseError) as exc_info:
        draft_steps(
            playbook_text="PB",
            task_brief="TB",
            roster=["anzai"],
            session_manager=fsm,
        )

    assert "max_iters" in str(exc_info.value)
    assert exc_info.value.raw_response == response


def test_invoke_error_raises() -> None:
    fsm = FakeSessionManager(
        stdout="", error="timeout after 600 seconds", exit_code=-1001
    )

    with pytest.raises(DrafterParseError) as exc_info:
        draft_steps(
            playbook_text="PB",
            task_brief="TB",
            roster=["anzai"],
            session_manager=fsm,
        )

    assert "invocation failed" in str(exc_info.value)
    assert "timeout after 600 seconds" in str(exc_info.value)
    assert exc_info.value.raw_response == ""


# --------------------------------------------------------------------------- #
# Lenient parsing + invocation args
# --------------------------------------------------------------------------- #


def test_blank_lines_after_header_ok() -> None:
    specs = three_step_specs()
    specs[0].blank_lines_after_header = 2
    response = make_response(specs)
    fsm = FakeSessionManager(stdout=response)

    result = draft_steps(
        playbook_text="PB",
        task_brief="TB",
        roster=["anzai", "akagi"],
        session_manager=fsm,
    )

    assert [s.id for s in result.steps] == [
        "01-anzai-plan",
        "02-akagi-critique",
        "03-anzai-revise",
    ]


def test_invoke_uses_anzai_persona_and_timeout() -> None:
    response = make_response(three_step_specs())
    fsm = FakeSessionManager(stdout=response)

    draft_steps(
        playbook_text="PB",
        task_brief="TB",
        roster=["anzai"],
        session_manager=fsm,
        timeout_sec=123,
    )

    assert len(fsm.calls) == 1
    assert fsm.calls[0].persona == "anzai"
    assert fsm.calls[0].timeout_sec == 123
    # Default timeout is honoured when the caller omits it.
    fsm2 = FakeSessionManager(stdout=response)
    draft_steps(
        playbook_text="PB",
        task_brief="TB",
        roster=["anzai"],
        session_manager=fsm2,
    )
    assert fsm2.calls[0].timeout_sec == 600

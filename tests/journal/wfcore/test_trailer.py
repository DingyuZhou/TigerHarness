"""Tests for ``tigerharness.journal.wfcore.trailer``.

The persona response trailer is the only place AI-generated text meets
deterministic routing. The grammar is tight on purpose; these tests
exercise every documented happy path plus every adversarial form the
spec writer (and the implementer) thought of -- two rounds of
self-critique on the table before this file was written.
"""

from __future__ import annotations

import pytest

from tigerharness.journal.wfcore.trailer import (
    Approve,
    Block,
    ParseError,
    Revise,
    Verdict,
    parse_trailer,
)


# ---------------------------------------------------------------------------
# ADT shape -- discriminator tags and field defaults
# ---------------------------------------------------------------------------


def test_approve_has_kind_tag() -> None:
    # Uppercase to match the wire protocol verbs and
    # ``models._VERDICTS`` (the StepHistoryEntry.verdict allowlist).
    assert Approve().kind == "APPROVE"


def test_revise_has_kind_tag_and_default_target_none() -> None:
    v = Revise(summary="needs work")
    assert v.kind == "REVISE"
    assert v.summary == "needs work"
    assert v.target is None


def test_revise_carries_explicit_target() -> None:
    v = Revise(summary="fix x", target="03-foo-bar")
    assert v.target == "03-foo-bar"


def test_block_has_kind_tag() -> None:
    v = Block(summary="cannot proceed")
    assert v.kind == "BLOCK"
    assert v.summary == "cannot proceed"


def test_parse_error_has_kind_tag() -> None:
    err = ParseError(reason="why")
    # ``PARSE_ERROR`` mirrors the spec's ``verdict_parse_failed``
    # event terminology, kept uppercase for consistency with the
    # other discriminator tags.
    assert err.kind == "PARSE_ERROR"
    assert err.reason == "why"


def test_verdict_alias_is_usable_for_annotation() -> None:
    # Smoke: the alias resolves and accepts every variant. Mostly a
    # readability assertion for callers that want ``verdict: Verdict``.
    samples: list[Verdict] = [
        Approve(),
        Revise(summary="x"),
        Revise(summary="x", target="01-foo"),
        Block(summary="x"),
        ParseError(reason="x"),
    ]
    assert len(samples) == 5


def test_verdicts_are_frozen() -> None:
    # Frozen dataclasses raise on attribute assignment; callers can rely
    # on verdict instances being safe to share across the orchestrator
    # and the events log.
    v = Approve()
    with pytest.raises(Exception):  # FrozenInstanceError subclass of Exception
        v.kind = "REVISE"  # type: ignore[misc,assignment]


# ---------------------------------------------------------------------------
# Happy paths -- APPROVE / REVISE / BLOCK
# ---------------------------------------------------------------------------


def test_approve_minimal() -> None:
    assert parse_trailer("WORKFLOW: APPROVE") == Approve()


def test_approve_with_preamble() -> None:
    text = (
        "Reviewed the plan; the rollback covers the cache + the DB.\n"
        "No concerns.\n"
        "\n"
        "WORKFLOW: APPROVE"
    )
    assert parse_trailer(text) == Approve()


def test_approve_with_trailing_spaces_and_tabs() -> None:
    assert parse_trailer("WORKFLOW: APPROVE   \t\t  ") == Approve()


def test_approve_with_trailing_newline() -> None:
    assert parse_trailer("WORKFLOW: APPROVE\n") == Approve()


def test_approve_with_crlf_line_endings() -> None:
    assert parse_trailer("body line\r\nWORKFLOW: APPROVE\r\n") == Approve()


def test_approve_with_lone_cr_line_ending() -> None:
    # Old-Mac style; ``splitlines()`` handles \r as a separator.
    assert parse_trailer("body\rWORKFLOW: APPROVE\r") == Approve()


def test_revise_without_target() -> None:
    assert parse_trailer("WORKFLOW: REVISE: missing rollback plan") == Revise(
        summary="missing rollback plan", target=None
    )


def test_revise_with_simple_target() -> None:
    assert parse_trailer(
        "WORKFLOW: REVISE: target=06: add rollback"
    ) == Revise(summary="add rollback", target="06")


def test_revise_with_hyphenated_target() -> None:
    # This is the spec's worked example. The step id contains many hyphens
    # and the reason follows the colon-space delimiter.
    assert parse_trailer(
        "WORKFLOW: REVISE: target=06-3f1a-rukawa-implement-cache: missing rollback"
    ) == Revise(
        summary="missing rollback",
        target="06-3f1a-rukawa-implement-cache",
    )


def test_revise_reason_contains_colons() -> None:
    # The reason text is allowed to contain colons; only the FIRST
    # colon after the verb delimits the reason.
    assert parse_trailer(
        "WORKFLOW: REVISE: error: missing : separator"
    ) == Revise(summary="error: missing : separator", target=None)


def test_revise_target_reason_contains_colons() -> None:
    assert parse_trailer(
        "WORKFLOW: REVISE: target=07-foo: deal with: nested: colons"
    ) == Revise(summary="deal with: nested: colons", target="07-foo")


def test_revise_with_trailing_whitespace() -> None:
    assert parse_trailer("WORKFLOW: REVISE: fix this   \t") == Revise(
        summary="fix this", target=None
    )


def test_revise_with_underscores_in_target() -> None:
    # Allowed by the step-id charset ``[A-Za-z0-9_-]+``.
    assert parse_trailer(
        "WORKFLOW: REVISE: target=step_07_foo: reason"
    ) == Revise(summary="reason", target="step_07_foo")


def test_block_with_reason() -> None:
    assert parse_trailer("WORKFLOW: BLOCK: dependency unreachable") == Block(
        summary="dependency unreachable"
    )


def test_block_reason_contains_colons() -> None:
    assert parse_trailer(
        "WORKFLOW: BLOCK: cannot proceed: upstream down"
    ) == Block(summary="cannot proceed: upstream down")


def test_block_with_trailing_whitespace() -> None:
    assert parse_trailer("WORKFLOW: BLOCK: nope\t  ") == Block(summary="nope")


# ---------------------------------------------------------------------------
# Last-wins semantics
# ---------------------------------------------------------------------------


def test_last_trailer_wins_when_both_valid() -> None:
    text = "WORKFLOW: REVISE: first take\nWORKFLOW: APPROVE"
    assert parse_trailer(text) == Approve()


def test_last_trailer_wins_for_three_lines() -> None:
    text = (
        "WORKFLOW: APPROVE\n"
        "WORKFLOW: BLOCK: actually no\n"
        "WORKFLOW: REVISE: revise it"
    )
    assert parse_trailer(text) == Revise(summary="revise it", target=None)


def test_last_trailer_wins_even_when_last_is_malformed() -> None:
    # Critical: we do NOT silently fall back to an earlier valid line.
    # A malformed final trailer is a parse failure -- the orchestrator
    # will re-prompt the persona exactly once.
    text = "WORKFLOW: APPROVE\nWORKFLOW: revise: bad case"
    result = parse_trailer(text)
    assert isinstance(result, ParseError)


def test_trailer_not_on_last_line_is_still_picked_up() -> None:
    # The contract is "last line that starts with WORKFLOW:", not
    # "last line of the text".
    text = "WORKFLOW: APPROVE\nGoodbye."
    assert parse_trailer(text) == Approve()


def test_trailer_followed_by_explanatory_paragraph() -> None:
    text = (
        "WORKFLOW: REVISE: tighten the failure path\n"
        "(Apologies for the late catch.)"
    )
    assert parse_trailer(text) == Revise(
        summary="tighten the failure path", target=None
    )


# ---------------------------------------------------------------------------
# ParseError -- empty / no trailer / no prefix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "   ",
        "\n",
        "\r\n",
        "\n\n  \t\n",
    ],
    ids=[
        "empty-string",
        "single-space",
        "multi-space",
        "newline-only",
        "crlf-only",
        "mixed-whitespace",
    ],
)
def test_empty_or_whitespace_only_is_parse_error(text: str) -> None:
    result = parse_trailer(text)
    assert isinstance(result, ParseError)
    assert "empty" in result.reason or "whitespace" in result.reason


def test_no_workflow_line_is_parse_error() -> None:
    result = parse_trailer("hello world\nno trailer here, sorry")
    assert isinstance(result, ParseError)
    assert "WORKFLOW:" in result.reason


def test_lowercase_workflow_prefix_is_parse_error() -> None:
    # The line literally does not start with ``WORKFLOW:`` so it is
    # treated as "no trailer found".
    result = parse_trailer("workflow: APPROVE")
    assert isinstance(result, ParseError)


def test_leading_whitespace_on_trailer_line_is_parse_error() -> None:
    # ``startswith("WORKFLOW:")`` is False when there is any prefix.
    # This guards against the persona accidentally indenting the trailer
    # inside a code block or list.
    result = parse_trailer(" WORKFLOW: APPROVE")
    assert isinstance(result, ParseError)


def test_tab_indented_trailer_line_is_parse_error() -> None:
    result = parse_trailer("\tWORKFLOW: APPROVE")
    assert isinstance(result, ParseError)


# ---------------------------------------------------------------------------
# ParseError -- case-sensitivity on verbs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "trailer",
    [
        "WORKFLOW: approve",
        "WORKFLOW: Approve",
        "WORKFLOW: APPROVE_PLEASE",  # close-but-no-cigar; trips strict ^...$
        "WORKFLOW: revise: foo",
        "WORKFLOW: Revise: foo",
        "WORKFLOW: REVise: foo",
        "WORKFLOW: block: foo",
        "WORKFLOW: Block: foo",
        "WORKFLOW: SHRUG: unknown verb",
        "WORKFLOW: APPROV",
    ],
    ids=[
        "approve-lower",
        "approve-title",
        "approve-suffix",
        "revise-lower",
        "revise-title",
        "revise-mixed-case",
        "block-lower",
        "block-title",
        "unknown-verb",
        "verb-truncated",
    ],
)
def test_wrong_verb_case_or_unknown_verb_is_parse_error(trailer: str) -> None:
    result = parse_trailer(trailer)
    assert isinstance(result, ParseError)


# ---------------------------------------------------------------------------
# ParseError -- malformed verb / spacing
# ---------------------------------------------------------------------------


def test_workflow_prefix_only_is_parse_error() -> None:
    result = parse_trailer("WORKFLOW:")
    assert isinstance(result, ParseError)


def test_workflow_prefix_plus_space_only_is_parse_error() -> None:
    result = parse_trailer("WORKFLOW: ")
    assert isinstance(result, ParseError)


def test_workflow_prefix_no_space_before_verb_is_parse_error() -> None:
    # Tight contract: exactly one space between ``WORKFLOW:`` and the
    # verb. ``WORKFLOW:APPROVE`` is rejected.
    result = parse_trailer("WORKFLOW:APPROVE")
    assert isinstance(result, ParseError)


def test_workflow_prefix_double_space_before_verb_is_parse_error() -> None:
    # Mirror of the single-space rule. AI personas that emit two spaces
    # get caught and re-prompted -- better that than silent drift.
    result = parse_trailer("WORKFLOW:  APPROVE")
    assert isinstance(result, ParseError)


def test_approve_with_spurious_arg_is_parse_error() -> None:
    # APPROVE takes no argument. ``WORKFLOW: APPROVE: foo`` is not a
    # generous interpretation -- it's a contract violation.
    result = parse_trailer("WORKFLOW: APPROVE: spurious extra")
    assert isinstance(result, ParseError)


# ---------------------------------------------------------------------------
# ParseError -- REVISE / BLOCK missing reason
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "trailer",
    [
        "WORKFLOW: REVISE",
        "WORKFLOW: REVISE:",
        "WORKFLOW: REVISE: ",
        "WORKFLOW: REVISE:    \t",
    ],
    ids=[
        "no-colon",
        "empty-after-colon",
        "single-space-after-colon",
        "whitespace-after-colon",
    ],
)
def test_revise_with_no_reason_is_parse_error(trailer: str) -> None:
    result = parse_trailer(trailer)
    assert isinstance(result, ParseError)


@pytest.mark.parametrize(
    "trailer",
    [
        "WORKFLOW: BLOCK",
        "WORKFLOW: BLOCK:",
        "WORKFLOW: BLOCK: ",
        "WORKFLOW: BLOCK:    \t",
    ],
    ids=[
        "no-colon",
        "empty-after-colon",
        "single-space-after-colon",
        "whitespace-after-colon",
    ],
)
def test_block_with_no_reason_is_parse_error(trailer: str) -> None:
    result = parse_trailer(trailer)
    assert isinstance(result, ParseError)


# ---------------------------------------------------------------------------
# ParseError -- malformed REVISE target spec
# ---------------------------------------------------------------------------


def test_revise_target_with_empty_id_is_parse_error() -> None:
    # ``target=:`` -- step-id is empty.
    result = parse_trailer("WORKFLOW: REVISE: target=: missing id")
    assert isinstance(result, ParseError)
    assert "target" in result.reason


def test_revise_target_with_no_space_after_colon_is_parse_error() -> None:
    # Spec format requires whitespace between ``target=<id>:`` and the
    # reason text. We refuse to silently absorb the reason.
    result = parse_trailer("WORKFLOW: REVISE: target=foo:reason")
    assert isinstance(result, ParseError)
    assert "target" in result.reason


def test_revise_target_with_empty_reason_is_parse_error() -> None:
    # ``target=foo:`` with nothing after -- the line rstrip drops the
    # trailing space and the target regex no longer matches.
    result = parse_trailer("WORKFLOW: REVISE: target=foo: ")
    assert isinstance(result, ParseError)
    assert "target" in result.reason


def test_revise_target_with_disallowed_id_chars_is_parse_error() -> None:
    # Step ids are restricted to ``[A-Za-z0-9_-]+`` -- characters
    # outside that set are rejected to keep adversarial input away from
    # routing keys. ``target=foo/bar`` could otherwise smuggle path
    # fragments into the rewind target.
    result = parse_trailer("WORKFLOW: REVISE: target=foo/bar: reason")
    assert isinstance(result, ParseError)
    assert "target" in result.reason


def test_revise_target_with_space_in_id_is_parse_error() -> None:
    result = parse_trailer("WORKFLOW: REVISE: target=foo bar: reason")
    assert isinstance(result, ParseError)
    assert "target" in result.reason


def test_revise_with_word_targets_in_reason_is_not_a_target_spec() -> None:
    # Sanity check: a reason that merely mentions the word "target"
    # (not at the very start, not prefixed by ``target=``) is just a
    # plain reason.
    assert parse_trailer(
        "WORKFLOW: REVISE: the target was incorrect"
    ) == Revise(summary="the target was incorrect", target=None)

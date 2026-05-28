"""Unit tests for ``tigerharness.workflow_runner.ids``.

The sanitizer is small but security-load-bearing once Phase 2's
AI-compile lands. Every accept/reject case is explicit and named so a
future reader can tell *why* each pattern is on the wrong side of the
line.
"""

from __future__ import annotations

import pytest

from tigerharness.workflow_runner.ids import (
    STEP_ID_PATTERN,
    validate_step_id,
)
from tigerharness.workflow_runner.models import WorkflowModelError


# --------------------------------------------------------------------------- #
# Acceptance cases
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "good_id",
    [
        # The spec's example shape.
        "01-7f2a-anzai-plan",
        # Common shapes used in the existing test fixtures.
        "01-plan",
        "02-critique",
        # Minimum length (1 char).
        "a",
        "0",
        "Z",
        # Maximum length (64 chars).
        "a" * 64,
        # Mixed-case + digits + both separators.
        "AB-12_cd-34",
        # Underscores allowed in body (just not leading).
        "step_id_with_underscores",
    ],
)
def test_validate_step_id_accepts(good_id):
    # Must not raise.
    validate_step_id(good_id)


# --------------------------------------------------------------------------- #
# Rejection cases
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad_id, reason",
    [
        # Path-traversal shapes -- the headline reason this sanitizer
        # exists.
        ("..", "directory-traversal token"),
        (".", "current-dir token"),
        ("./foo", "dot-slash prefix"),
        ("foo/bar", "embedded slash"),
        ("foo\\bar", "embedded backslash"),
        # Length boundaries.
        ("", "empty string"),
        ("a" * 65, "65 chars (one over limit)"),
        # Leading-hyphen blocks argument smuggling like ``rm -rf``.
        ("-rf", "leading hyphen"),
        ("-", "single hyphen"),
        # Leading underscore reserved for routing sentinels.
        ("_private", "leading underscore"),
        ("__done__", "routing sentinel shape"),
        # Whitespace + invisible separators.
        (" ", "single space"),
        ("foo bar", "embedded space"),
        ("foo\tbar", "embedded tab"),
        ("foo\nbar", "embedded newline"),
        # NUL byte -- belt and braces vs. filesystem confusion.
        ("foo\x00bar", "embedded NUL"),
        # Non-ASCII -- alphanumeric ISO categories are permissive
        # enough to be confusing; restrict to ASCII.
        ("café", "non-ASCII"),
        ("01-日本", "CJK"),
        # Symbols never allowed in path-safe ids.
        ("foo.bar", "embedded dot"),
        ("foo:bar", "embedded colon"),
        ("foo;bar", "embedded semicolon"),
        ("foo$bar", "embedded dollar"),
        ("foo|bar", "embedded pipe"),
        ("foo*bar", "embedded glob"),
    ],
)
def test_validate_step_id_rejects(bad_id, reason):
    with pytest.raises(WorkflowModelError) as exc:
        validate_step_id(bad_id)
    # Error message names the bad value so logs are diagnostic.
    assert repr(bad_id) in str(exc.value), reason


def test_validate_step_id_rejects_non_string():
    for not_a_string in (None, 42, [], {}, b"01-plan"):
        with pytest.raises(WorkflowModelError) as exc:
            validate_step_id(not_a_string)
        assert "must be a string" in str(exc.value)


def test_step_id_pattern_is_a_compileable_regex():
    """Belt-and-braces: the public constant must round-trip through
    ``re.compile`` so downstream callers can reuse it verbatim."""
    import re

    pat = re.compile(STEP_ID_PATTERN)
    assert pat.match("01-plan")
    assert not pat.match("..")

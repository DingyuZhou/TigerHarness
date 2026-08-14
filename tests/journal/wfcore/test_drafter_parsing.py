"""Branch coverage for the drafter's pure parsing core.

The session-driven ``draft_steps`` entrypoint left with the legacy
api runner (ADR 0003); the journal compile drives ``_build_prompt``
and ``_parse_response`` directly (``journal validate-graph`` and the
compile-context/compile-prompts CLIs), so every malformed-bundle
branch here is a live error path a hand-written draft can hit.
"""

from __future__ import annotations

import pytest

from tigerharness.journal.wfcore.drafter import (
    DrafterParseError,
    _build_prompt,
    _parse_response,
)

FENCE = chr(96) * 3

GOOD = (
    "preamble text\n\n"
    + FENCE + "steps-bundle\n"
    "## step: s1\n"
    "---\n"
    "id: s1\n"
    "persona: Anzai\n"
    "role: planner\n"
    "on_approve: __done__\n"
    "on_revise: s1\n"
    "on_block: __escalate__\n"
    "max_iters: 2\n"
    "timeout_sec: 60\n"
    "parallel_with: []\n"
    "---\n"
    "body\n"
    + FENCE + "\n"
)


def test_good_bundle_parses() -> None:
    steps = _parse_response(GOOD)
    assert [s.id for s in steps] == ["s1"]


def test_build_prompt_without_feedback() -> None:
    p = _build_prompt(
        playbook_text="PB", task_brief="TB", roster=["Anzai"], feedback=None
    )
    assert "PB" in p and "TB" in p


def test_build_prompt_with_feedback_prologue() -> None:
    p = _build_prompt(
        playbook_text="PB", task_brief="TB", roster=["Anzai"], feedback="fix the cap"
    )
    assert "fix the cap" in p


def test_no_opening_fence_raises() -> None:
    with pytest.raises(DrafterParseError, match="no opening"):
        _parse_response("no bundle here at all")


def test_unclosed_fence_raises() -> None:
    bad = GOOD.rsplit(FENCE, 1)[0]  # drop the closing fence
    with pytest.raises(DrafterParseError, match="never closed"):
        _parse_response(bad)


def test_no_step_headers_raises() -> None:
    bad = FENCE + "steps-bundle\njust prose\n" + FENCE + "\n"
    with pytest.raises(DrafterParseError, match="no '## step:"):
        _parse_response(bad)


def test_unknown_frontmatter_field_raises() -> None:
    bad = GOOD.replace("parallel_with: []", "parallel_with: []\nbogus_key: 1")
    with pytest.raises(DrafterParseError, match="unknown frontmatter"):
        _parse_response(bad)


def test_model_error_is_wrapped() -> None:
    bad = GOOD.replace("max_iters: 2", "max_iters: -5")
    with pytest.raises(DrafterParseError, match="step 's1'"):
        _parse_response(bad)


def test_missing_opening_frontmatter_delim_raises() -> None:
    bad = (
        FENCE + "steps-bundle\n## step: s1\nid: s1\n" + FENCE + "\n"
    )
    with pytest.raises(DrafterParseError, match="opening '---'"):
        _parse_response(bad)


def test_missing_closing_frontmatter_delim_raises() -> None:
    bad = (
        FENCE + "steps-bundle\n## step: s1\n---\nid: s1\n" + FENCE + "\n"
    )
    with pytest.raises(DrafterParseError, match="closing '---'"):
        _parse_response(bad)


def test_invalid_yaml_frontmatter_raises() -> None:
    bad = (
        FENCE + "steps-bundle\n## step: s1\n---\n[unclosed\n---\n" + FENCE + "\n"
    )
    with pytest.raises(DrafterParseError, match="not valid YAML"):
        _parse_response(bad)


def test_non_mapping_frontmatter_raises() -> None:
    bad = (
        FENCE + "steps-bundle\n## step: s1\n---\n- a list\n---\n" + FENCE + "\n"
    )
    with pytest.raises(DrafterParseError, match="YAML mapping"):
        _parse_response(bad)


def test_blank_lines_before_frontmatter_tolerated() -> None:
    padded = GOOD.replace("## step: s1\n---", "## step: s1\n\n\n---")
    steps = _parse_response(padded)
    assert steps[0].id == "s1"


# ---------------------------------------------------------------------------
# Bodies -- everything after the closing '---' of a chunk
# ---------------------------------------------------------------------------

def test_body_is_captured_from_the_chunk() -> None:
    steps = _parse_response(GOOD)
    assert steps[0].body == "body"


def test_multiline_body_keeps_internal_blank_lines() -> None:
    src = GOOD.replace(
        "---\nbody\n",
        "---\nfirst line\n\nsecond paragraph\n  indented\n",
    )
    steps = _parse_response(src)
    assert steps[0].body == "first line\n\nsecond paragraph\n  indented"


def test_chunk_with_no_body_yields_empty_string() -> None:
    src = GOOD.replace("---\nbody\n", "---\n")
    steps = _parse_response(src)
    assert steps[0].body == ""


def test_whitespace_only_body_normalises_to_empty() -> None:
    """A drafter that leaves a blank line after the delimiter must not
    produce a step that reads as "has instructions" downstream."""
    src = GOOD.replace("---\nbody\n", "---\n\n   \n\n")
    steps = _parse_response(src)
    assert steps[0].body == ""


def test_each_step_gets_only_its_own_body() -> None:
    two = GOOD.replace(
        "---\nbody\n",
        "---\nfirst body\n"
        "## step: s2\n"
        "---\n"
        "id: s2\n"
        "persona: Mitsui\n"
        "role: developer\n"
        "on_approve: __done__\n"
        "on_revise: s2\n"
        "on_block: __escalate__\n"
        "max_iters: 2\n"
        "timeout_sec: 60\n"
        "parallel_with: []\n"
        "---\n"
        "second body\n",
    )
    steps = _parse_response(two)
    assert [(s.id, s.body) for s in steps] == [
        ("s1", "first body"), ("s2", "second body"),
    ]


# ---------------------------------------------------------------------------
# Escaping a literal '## step:' line inside a body
# ---------------------------------------------------------------------------

def test_unescaped_step_header_in_body_splits_the_bundle() -> None:
    """The hazard the escape exists for: a body that quotes this format
    without escaping injects a phantom step and truncates the real one."""
    src = GOOD.replace("---\nbody\n", "---\nbody\n## step: not-a-real-step\n")
    with pytest.raises(DrafterParseError, match="not-a-real-step"):
        _parse_response(src)


def test_escaped_step_header_stays_in_the_body() -> None:
    src = GOOD.replace(
        "---\nbody\n",
        "---\nemit one file per phase:\n\\## step: <id>\nthen frontmatter\n",
    )
    steps = _parse_response(src)
    assert [s.id for s in steps] == ["s1"]
    assert steps[0].body == (
        "emit one file per phase:\n## step: <id>\nthen frontmatter"
    )


def test_escape_only_applies_to_step_header_lines() -> None:
    """A backslash that does not disarm a step header is body text and
    must survive verbatim -- the escape is not a general unquoter."""
    src = GOOD.replace("---\nbody\n", "---\n\\## heading\nC:\\path\n")
    steps = _parse_response(src)
    assert steps[0].body == "\\## heading\nC:\\path"


def test_escaped_header_alone_is_not_a_step() -> None:
    """An escaped header cannot be the bundle's only 'header' -- otherwise
    a doc-writing body could smuggle itself in as the whole graph."""
    bad = (
        FENCE + "steps-bundle\n\\## step: s1\n---\nid: s1\n---\n" + FENCE + "\n"
    )
    with pytest.raises(DrafterParseError, match="no '## step:"):
        _parse_response(bad)


def test_inner_code_fence_truncates_the_bundle() -> None:
    """Why the fix is an escape and not fence-aware splitting: a fenced
    block inside a body cannot survive at all. ``_extract_bundle`` ends
    the bundle at the block's own bare closing fence, so step s2 is gone
    and the quoted header becomes a real -- and headless -- step. Teaching
    ``_split_steps`` about fences would never see this bundle."""
    src = GOOD.replace(
        "---\nbody\n",
        "---\nbody\n" + FENCE + "text\n## step: quoted\n" + FENCE + "\n"
        "## step: s2\n"
        "---\n"
        "id: s2\n"
        "persona: Mitsui\n"
        "role: developer\n"
        "on_approve: __done__\n"
        "on_revise: s2\n"
        "on_block: __escalate__\n"
        "max_iters: 2\n"
        "timeout_sec: 60\n"
        "parallel_with: []\n"
        "---\n"
        "second body\n",
    )
    with pytest.raises(DrafterParseError, match="step 'quoted'"):
        _parse_response(src)


def test_output_protocol_teaches_the_escape() -> None:
    p = _build_prompt(
        playbook_text="PB", task_brief="TB", roster=["Anzai"], feedback=None
    )
    assert "\\## step:" in p


def test_body_as_a_frontmatter_key_is_rejected() -> None:
    """The body goes BELOW the closing '---'. A drafter that writes
    ``body:`` inside the frontmatter gets told, rather than having the
    text silently land as a YAML value."""
    bad = GOOD.replace("parallel_with: []", "parallel_with: []\nbody: inline")
    with pytest.raises(DrafterParseError, match="unknown frontmatter"):
        _parse_response(bad)

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

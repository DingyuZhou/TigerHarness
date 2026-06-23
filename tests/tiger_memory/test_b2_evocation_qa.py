"""B2 QA (Sakuragi defense): attack associative reinforcement at its seams.

The two acceptance-critical probes the plan named: (1) the concise recall
reference must survive the diary format gate's round-trip (else `check` breaks);
(2) repeated evocation of a count store must stay well-defined / bounded (no
runaway crash). Plus malformed-model-output defense.
"""
from __future__ import annotations

from tigerharness.tiger_memory import diary_format
from tigerharness.tiger_memory.entries import (
    MustRememberEntry,
    SkillEntry,
)
from tigerharness.tiger_memory.evocation import _parse_response
from tigerharness.tiger_memory.reinforce import (
    build_recall_reference,
    reinforce_must_remember,
)

TS = "2026-06-10T00:00:00Z"


def _skill(name):
    return SkillEntry(id="s1", text="t", created_at=TS, last_used=TS, source="s",
                      name=name, trigger="x", procedure="y", usage_count=1)


def _mr(text, repeat_count=1):
    e = MustRememberEntry(id="m1", text=text, created_at=TS, last_used=TS,
                          source="s", kind="preference")
    e.repeat_count = repeat_count
    return e


def test_recall_reference_survives_diary_format_round_trip():
    """A diary bullet whose text carries a recall reference must serialize,
    re-parse, and validate clean — otherwise `tiger-memory check` would break."""
    ref = build_recall_reference([_skill("commit via -F"), _mr("use -F not -m")])
    note = diary_format.DiaryEntry(
        date="2026-06-22", weight=3.0, text="shipped evocation" + ref,
    )
    text = diary_format.serialize([note])
    assert diary_format.validate(text) == []           # format gate is clean
    parsed = diary_format.parse(text)
    assert "recalls:" in parsed[0].text                 # the reference survived
    assert 'skill "commit via -F"' in parsed[0].text


def test_recall_reference_with_special_chars_stays_one_line():
    """A target body with newlines / quotes must not produce a multi-line bullet
    (which validate-on-write would refuse)."""
    weird = _mr('line one\nline two "quoted" ; semicolon')
    ref = build_recall_reference([weird])
    note = diary_format.DiaryEntry(date="2026-06-22", weight=1.0,
                                   text="note" + ref)
    text = diary_format.serialize([note])
    assert diary_format.validate(text) == []
    # exactly one bullet line under the day header
    assert text.count("\n- ") == 1


def test_repeated_must_remember_evocation_is_bounded_and_defined():
    """Reinforcing one must_remember many times increments deterministically and
    keeps importance == repeat_count (ranks high — intended — never NaN/overflow)."""
    e = _mr("a frequently recalled fact")
    for _ in range(50):
        reinforce_must_remember(e)
    assert e.repeat_count == 51
    assert e.importance == 51.0
    assert isinstance(e.importance, float)


def test_parse_response_empty_or_garbage_is_safe():
    assert _parse_response("", n_notes=1, n_context=2) == {}
    assert _parse_response("totally unrelated text", n_notes=1, n_context=2) == {}
    # a NOTE line with non-numeric body -> empty list, no crash
    assert _parse_response("NOTE 0: nonsense words", n_notes=1, n_context=2) == {0: []}

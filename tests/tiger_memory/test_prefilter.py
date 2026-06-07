"""Unit tests for the transcript pre-filter (P1.1 / Lever 1.2)."""
from __future__ import annotations

from tigerharness.tiger_memory.prefilter import (
    _is_boundary,
    filter_transcript,
)

_HDR_A = "[2026-05-14T08:21:36.000Z] assistant:"
_HDR_U = "[2026-05-14T08:21:40.000Z] user:"


def test_passthrough_when_no_markers() -> None:
    """Plain prose with no tool_result / system-reminder is unchanged."""
    content = f"{_HDR_U}\nWhat's the plan?\n{_HDR_A}\nHere it is.\n"
    assert filter_transcript(content) == content


def test_keeps_tool_use_intent() -> None:
    """tool_use markers (intents) survive; only tool_result bodies go."""
    content = f"{_HDR_A}\nReading now.\n[tool_use: Read]\n"
    assert filter_transcript(content) == content


def test_elides_tool_result_ended_by_event_header() -> None:
    payload = "X" * 500
    content = (
        f"{_HDR_U}\n[tool_result] {payload}\n{_HDR_A}\nDone.\n"
    )
    out = filter_transcript(content)
    assert "[tool_result elided: 500 chars]" in out
    assert payload not in out
    # Surrounding signal is preserved.
    assert "Done." in out and _HDR_A in out


def test_elides_tool_result_at_eof() -> None:
    """A tool_result that runs to end-of-content (no trailing boundary)."""
    content = f"{_HDR_U}\n[tool_result] {'y' * 30}"
    out = filter_transcript(content)
    assert out.endswith("[tool_result elided: 30 chars]")


def test_elides_multiline_tool_result_payload() -> None:
    payload = "line one\nline two\nline three"
    content = f"{_HDR_U}\n[tool_result] {payload}\n{_HDR_A}\nok\n"
    out = filter_transcript(content)
    # 'line one' + '\n' + 'line two' + '\n' + 'line three' = 28 chars.
    assert f"[tool_result elided: {len(payload)} chars]" in out
    assert "line two" not in out
    assert "ok" in out


def test_consecutive_tool_results_each_elided() -> None:
    content = (
        f"{_HDR_U}\n[tool_result] {'a' * 10}\n[tool_result] {'b' * 20}\n{_HDR_A}\n"
    )
    out = filter_transcript(content)
    assert "[tool_result elided: 10 chars]" in out
    assert "[tool_result elided: 20 chars]" in out
    assert "aaaa" not in out and "bbbb" not in out


def test_tool_use_line_terminates_tool_result() -> None:
    content = f"{_HDR_A}\n[tool_result] {'z' * 12}\n[tool_use: Bash]\n"
    out = filter_transcript(content)
    assert "[tool_result elided: 12 chars]" in out
    # The tool_use intent that bounded the payload is kept.
    assert "[tool_use: Bash]" in out


def test_strips_system_reminder_block() -> None:
    content = (
        f"{_HDR_U}\nHello <system-reminder>do not do X\nmultiline</system-reminder> world\n"
    )
    out = filter_transcript(content)
    assert "system-reminder" not in out
    assert "Hello" in out and "world" in out


def test_flags_off_leave_content_untouched() -> None:
    content = (
        f"{_HDR_U}\n[tool_result] {'q' * 40}\n"
        f"<system-reminder>noise</system-reminder>\n"
    )
    out = filter_transcript(
        content, drop_tool_results=False, drop_system_reminders=False
    )
    assert out == content


def test_is_boundary_classifies_all_kinds() -> None:
    assert _is_boundary(_HDR_A) is True          # event header
    assert _is_boundary(_HDR_U) is True
    assert _is_boundary("[tool_use: Read]") is True
    assert _is_boundary("[tool_result] foo") is True
    assert _is_boundary("just some prose") is False

"""Transcript pre-filter (P1.1 / Lever 1.2).

Strip high-volume, low-signal noise from a rendered transcript *before*
it is clipped and embedded into a summarize prompt. Smaller input = less
quota spent per rebuild, regardless of how the summarizer call is billed,
so this win survives the P2 move to in-session summarization: it lives
*above* the summarizer interface, in lifecycle orchestration.

What it drops (conservative v1):
  - ``[tool_result] <payload>`` bodies -- Read file dumps, Bash stdout,
    Grep output, large diffs. Replaced by a ``[tool_result elided: <N>
    chars]`` marker so the summary still knows a tool produced output.
  - ``<system-reminder>...</system-reminder>`` boilerplate blocks the
    harness injects into user-turn prose.

What it KEEPS: all human + assistant prose, and tool *intents*
(``[tool_use: <name>]`` markers -- already compact). Signal stays; spam
goes.

It operates on the *rendered* content string produced by
``sources/claude_transcript.py`` (``[<ts>] <role>:`` event headers with
``[tool_use: ...]`` / ``[tool_result] ...`` markers inside each turn).
Pure and deterministic -- the same input always yields the same output --
so it is trivially unit-testable and safe to run once per record and
reuse across every summarize call for that record.
"""
from __future__ import annotations

import logging

import re

log = logging.getLogger("tigerharness.tiger_memory.prefilter")

# A rendered event-header line: ``[<iso-ts>] user:`` / ``[<iso-ts>]
# assistant:`` (the timestamp may be empty). These -- plus the tool
# markers below -- are the structural boundaries that terminate a
# tool_result payload.
_EVENT_HEADER_RE = re.compile(r"^\[[^\]]*\]\s+(?:user|assistant):\s*$")

_TOOL_RESULT_PREFIX = "[tool_result]"
_TOOL_USE_PREFIX = "[tool_use: "

# ``<system-reminder>...</system-reminder>`` -- non-greedy, across newlines.
_SYSTEM_REMINDER_RE = re.compile(
    r"<system-reminder>.*?</system-reminder>", re.DOTALL
)


def filter_transcript(
    content: str,
    *,
    drop_tool_results: bool = True,
    drop_system_reminders: bool = True,
) -> str:
    """Return *content* with noise stripped per the flags.

    System-reminder stripping runs first so a reminder embedded *inside*
    a tool_result payload (rare) is gone before the payload is measured.
    """
    if drop_system_reminders:
        content = _strip_system_reminders(content)
    if drop_tool_results:
        content = _elide_tool_results(content)
    return content


def _strip_system_reminders(content: str) -> str:
    return _SYSTEM_REMINDER_RE.sub("", content)


def _is_boundary(line: str) -> bool:
    """True iff *line* ends a tool_result payload: a new event header, a
    tool_use intent, or the start of the next tool_result."""
    return (
        _EVENT_HEADER_RE.match(line) is not None
        or line.startswith(_TOOL_USE_PREFIX)
        or line.startswith(_TOOL_RESULT_PREFIX)
    )


def _elide_tool_results(content: str) -> str:
    """Replace each ``[tool_result] <payload>`` with a char-count marker.

    The payload spans from the marker to the next structural boundary
    (``_is_boundary``). Conservative heuristic: a text block that
    immediately follows a tool_result *within the same turn* would be
    elided too -- but assistant prose always begins a fresh event header,
    so the highest-signal content is protected.
    """
    lines = content.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith(_TOOL_RESULT_PREFIX):
            payload_parts = [line[len(_TOOL_RESULT_PREFIX):].lstrip()]
            j = i + 1
            while j < n and not _is_boundary(lines[j]):
                payload_parts.append(lines[j])
                j += 1
            payload = "\n".join(payload_parts)
            out.append(f"[tool_result elided: {len(payload)} chars]")
            i = j
        else:
            out.append(line)
            i += 1
    return "\n".join(out)

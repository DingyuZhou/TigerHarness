"""Collapsed single-pass summary parser (P1.3 / Lever 1.1).

Today a new session issues three summarize calls (short, detailed,
must-memorize) that each re-send the same transcript. P1.3 collapses them
into ONE structured call whose output carries all three sections behind a
strict delimiter contract. This module parses that output; the lifecycle
caller writes the sections and falls back to the legacy 3-call path on any
``CollapseParseError`` so a single malformed response never corrupts the
store.

The contract — the model emits exactly these three markers, in order:

    @@SHORT@@
    <short summary bullets>
    @@DETAILED@@
    <detailed ## sections>
    @@MUST_MEMORIZE@@
    <KIND/MEMO blocks, or NONE>

Pure and deterministic; trivially unit-testable.
"""
from __future__ import annotations

_SHORT = "@@SHORT@@"
_DETAILED = "@@DETAILED@@"
_MUST = "@@MUST_MEMORIZE@@"


class CollapseParseError(ValueError):
    """The collapsed output didn't satisfy the delimiter contract."""


def parse_collapsed(text: str) -> tuple[str, str, str]:
    """Split a collapsed summary into ``(short, detailed, must_memorize)``.

    Markers are matched as **whole lines** (``line.strip() == marker``),
    taking the first standalone-line occurrence of each — which is exactly
    what the prompt's output contract emits. Matching whole lines (rather
    than any substring) means a marker token *mentioned inline* in a
    section body — e.g. echoed from an untrusted transcript (B7) — does
    NOT split the bundle, so it can't silently mis-split into a garbled
    summary.

    Raises ``CollapseParseError`` if any marker is missing (or only ever
    appears inline), the markers are out of order, or the short / detailed
    sections are empty. The must-memorize section is allowed to be empty
    (it may legitimately be ``NONE`` / zero candidates), so the caller
    validates it separately.
    """
    if not text:
        raise CollapseParseError("empty output")
    lines = text.split("\n")
    pos: dict[str, int] = {}
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped in (_SHORT, _DETAILED, _MUST) and stripped not in pos:
            pos[stripped] = i
    if _SHORT not in pos or _DETAILED not in pos or _MUST not in pos:
        raise CollapseParseError("missing one or more section markers")
    i_s, i_d, i_m = pos[_SHORT], pos[_DETAILED], pos[_MUST]
    if not (i_s < i_d < i_m):
        raise CollapseParseError("section markers out of order")
    short = "\n".join(lines[i_s + 1:i_d]).strip()
    detailed = "\n".join(lines[i_d + 1:i_m]).strip()
    must = "\n".join(lines[i_m + 1:]).strip()
    if not short or not detailed:
        raise CollapseParseError("empty short or detailed section")
    return short, detailed, must

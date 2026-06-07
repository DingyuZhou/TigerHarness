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

    Raises ``CollapseParseError`` if any marker is missing, the markers
    are out of order, or the short / detailed sections are empty. The
    must-memorize section is allowed to be empty (it may legitimately be
    ``NONE`` / zero candidates), so the caller validates it separately.
    """
    if not text:
        raise CollapseParseError("empty output")
    i_s = text.find(_SHORT)
    i_d = text.find(_DETAILED)
    i_m = text.find(_MUST)
    if i_s < 0 or i_d < 0 or i_m < 0:
        raise CollapseParseError("missing one or more section markers")
    if not (i_s < i_d < i_m):
        raise CollapseParseError("section markers out of order")
    short = text[i_s + len(_SHORT):i_d].strip()
    detailed = text[i_d + len(_DETAILED):i_m].strip()
    must = text[i_m + len(_MUST):].strip()
    if not short or not detailed:
        raise CollapseParseError("empty short or detailed section")
    return short, detailed, must

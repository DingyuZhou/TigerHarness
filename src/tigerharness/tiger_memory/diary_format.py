"""Diary store on-disk format: compact dated weighted bullets (plan §2 dev-1).

The diary store (design §4.3, formerly ``emotional``) is a personal work-log:
day-headed sections of short weighted bullets, NOT per-entry YAML frontmatter.
Skills / must_remember keep frontmatter; the diary is deliberately compact and
loaded **whole** each session, with forgetting (not a display cap) keeping it
bounded. On-disk shape::

    ## 2026-06-17
    - (+7) Drove the harness to true 100% — patient thoroughness
    - (-5) Agents declaring success on near-misses bugs me

    ## 2026-06-18
    - (+4) Reframed the diary store with the Operator — simpler

Day sections are headed ``## YYYY-MM-DD`` (ascending / chronological); each
bullet is ``- (±N) <note>`` where ``N`` is the signed inline weight in
``[-cap, +cap]``. The weight's sign carries valence (the old separate
``reaction`` field is folded into the note text + sign).

This module is the **single** serialize / parse / validate implementation:
the store's validate-on-write round-trip AND the ``check`` verb both reuse it
(Akagi compile-critique #1 — no second parser that could drift). All three
functions are pure (no I/O, no summarizer) and fully unit-testable. Length is
measured in **characters**, never tokens (vendor-neutral).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: Default magnitude cap when a caller does not pass one (design §4.3, ±10).
DEFAULT_WEIGHT_CAP = 10

_DAY_RE = re.compile(r"^## (\d{4})-(\d{2})-(\d{2})$")
#: ``- (+7) note`` / ``- (-5) note`` — sign is mandatory; magnitude int or float.
_BULLET_RE = re.compile(r"^- \(([+-]\d+(?:\.\d+)?)\) (.+)$")


class DiaryFormatError(ValueError):
    """A diary store does not conform to the dated-bullet format."""


@dataclass
class DiaryEntry:
    """One diary bullet: a dated, signed-weight, one-line note.

    ``date`` is an ISO ``YYYY-MM-DD`` day (the decay anchor — see plan §6,
    decay is per-entry from this date). ``weight`` is the signed emotional
    charge; ``text`` is the note (the old ``reaction`` folded in).
    """

    date: str
    weight: float
    text: str


def _format_weight(weight: float) -> str:
    """Render *weight* as a signed ``(±N)`` token (int when whole, else float).

    ``7`` -> ``+7``; ``-5.0`` -> ``-5``; ``7.5`` -> ``+7.5``; ``0`` -> ``+0``.
    Always carries an explicit sign so the parser's mandatory-sign rule holds.
    """
    if weight == int(weight):
        return f"{int(weight):+d}"
    return f"{weight:+g}"


def _valid_day(date: str) -> bool:
    """True if *date* is a real ``YYYY-MM-DD`` calendar day."""
    m = _DAY_RE.match(f"## {date}")
    if m is None:
        return False
    year, month, day = (int(g) for g in m.groups())
    if not (1 <= month <= 12):
        return False
    days_in = [31, 29 if _is_leap(year) else 28, 31, 30, 31, 30,
               31, 31, 30, 31, 30, 31]
    return 1 <= day <= days_in[month - 1]


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _flatten(text: str) -> str:
    """Collapse a note's internal whitespace (newlines / runs) to single spaces.

    A bullet is ONE line, so a note containing newlines must flatten — otherwise
    serialize would emit a malformed multi-line bullet that validate-on-write
    refuses and the migration crashes on (b2 Sakuragi finding).
    """
    return " ".join(text.split())


def serialize(entries: list[DiaryEntry]) -> str:
    """Render *entries* to the canonical dated-bullet text.

    Bullets are grouped by ``date`` into ``## YYYY-MM-DD`` sections in
    **ascending** (chronological) order; within a day, original order is
    preserved. A blank line separates days. The output always ends with a
    single trailing newline (empty input -> empty string).
    """
    if not entries:
        return ""
    by_day: dict[str, list[DiaryEntry]] = {}
    for e in entries:
        by_day.setdefault(e.date, []).append(e)
    blocks: list[str] = []
    for day in sorted(by_day):
        lines = [f"## {day}"]
        for e in by_day[day]:
            lines.append(f"- ({_format_weight(e.weight)}) {_flatten(e.text)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def parse(text: str, weight_cap: float = DEFAULT_WEIGHT_CAP) -> list[DiaryEntry]:
    """Parse canonical diary text back to entries (strict; the single parser).

    Accepts blank lines, ``## YYYY-MM-DD`` day headers, and ``- (±N) note``
    bullets under a current header. Raises :class:`DiaryFormatError` on a stray
    line, a bullet before any day header, an invalid calendar date, a weight
    outside ``[-weight_cap, +weight_cap]``, or empty note text.
    """
    entries: list[DiaryEntry] = []
    current_day: str | None = None
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if raw == "":
            continue
        day_m = _DAY_RE.match(raw)
        if day_m is not None:
            day = "-".join(day_m.groups())
            if not _valid_day(day):
                raise DiaryFormatError(f"line {lineno}: invalid date {day!r}")
            current_day = day
            continue
        bullet_m = _BULLET_RE.match(raw)
        if bullet_m is not None:
            if current_day is None:
                raise DiaryFormatError(
                    f"line {lineno}: bullet before any '## YYYY-MM-DD' header"
                )
            weight = float(bullet_m.group(1))
            if abs(weight) > weight_cap:
                raise DiaryFormatError(
                    f"line {lineno}: weight {weight:+g} exceeds cap {weight_cap}"
                )
            note = bullet_m.group(2).strip()
            if not note:
                raise DiaryFormatError(f"line {lineno}: empty note text")
            entries.append(DiaryEntry(date=current_day, weight=weight, text=note))
            continue
        raise DiaryFormatError(f"line {lineno}: stray line {raw!r}")
    return entries


def parse_lenient(
    text: str, weight_cap: float = DEFAULT_WEIGHT_CAP
) -> tuple[list[DiaryEntry], list[str]]:
    """Like :func:`parse` but COLLECTS malformed lines instead of raising.

    Returns ``(entries, rejected)``: the good bullets (under valid day headers)
    and the raw text of every line that could not be parsed (a stray line, a
    bullet before any header, an invalid date, an over-cap weight, or an empty
    note). Used by ``tiger-memory check --fix`` to keep the good content while
    quarantining the bad lines to a ``<store>.rejected.md`` sidecar.
    """
    entries: list[DiaryEntry] = []
    rejected: list[str] = []
    current_day: str | None = None
    for raw in text.splitlines():
        if raw == "":
            continue
        day_m = _DAY_RE.match(raw)
        if day_m is not None:
            day = "-".join(day_m.groups())
            if _valid_day(day):
                current_day = day
            else:
                rejected.append(raw)
            continue
        bullet_m = _BULLET_RE.match(raw)
        if bullet_m is not None and current_day is not None:
            weight = float(bullet_m.group(1))
            note = bullet_m.group(2).strip()
            if abs(weight) > weight_cap or not note:
                rejected.append(raw)
            else:
                entries.append(
                    DiaryEntry(date=current_day, weight=weight, text=note)
                )
            continue
        rejected.append(raw)
    return entries, rejected


def validate(text: str, weight_cap: float = DEFAULT_WEIGHT_CAP) -> list[str]:
    """Return a list of format errors for *text* (empty list = valid).

    Reuses :func:`parse` (single source of truth) and additionally asserts the
    **round-trip** invariant — ``serialize(parse(text))`` re-parses to the same
    entries — so a file the writer could not have produced is flagged even if
    each line is individually well-formed. The ``check`` verb consumes this.
    """
    try:
        entries = parse(text, weight_cap)
    except DiaryFormatError as exc:
        return [str(exc)]
    if parse(serialize(entries), weight_cap) != entries:
        return ["round-trip mismatch: text does not serialize canonically"]
    return []

"""Must-memorize table — scoring, decay, repeat-detection, pin, render.

The on-disk format is a markdown file with a YAML frontmatter and one
markdown table. Persistence stays markdown-readable (so an Operator
can hand-edit if needed) while load/save go through this module.

Schema (one row):
    score: int | "∞"
    kind: owner_explicit | preference | decision | incident
    last_bump: ISO date YYYY-MM-DD
    last_decay: ISO date YYYY-MM-DD (per-row decay clock)
    source: free text ("operator", "repeat", "extract", "pin")
    memo: text ≤ memo_max_words

See design doc §5.5 + §6.
"""
from __future__ import annotations

import logging

import difflib
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from . import frontmatter
from .config import Config
from .state import iso_now
from .store import Store

log = logging.getLogger("tigerharness.tiger_memory.must_memorize")


KIND_OWNER_EXPLICIT = "owner_explicit"
KIND_PREFERENCE = "preference"
KIND_DECISION = "decision"
KIND_INCIDENT = "incident"
VALID_KINDS = (KIND_OWNER_EXPLICIT, KIND_PREFERENCE, KIND_DECISION, KIND_INCIDENT)


# ----- row model ------------------------------------------------------------


@dataclass
class Row:
    kind: str
    memo: str
    score: int = 5            # int; owner_explicit rows are still int but locked
    locked: bool = False
    last_bump: str = ""       # YYYY-MM-DD
    last_decay: str = ""      # YYYY-MM-DD
    source: str = "extract"

    def bump(self, today: str) -> None:
        if not self.locked:
            self.score += 1
        self.last_bump = today

    def decay(self, points: int, today: str) -> None:
        if self.locked or points <= 0:
            return
        self.score -= points
        self.last_decay = today

    def score_display(self) -> str:
        return "∞" if self.locked else str(self.score)


# ----- file IO --------------------------------------------------------------


def load(store: Store) -> list[Row]:
    path = store.paths.journal / "must_memorize.md"
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    _, body = frontmatter.parse(text)
    return _parse_table(body)


def save(store: Store, rows: Sequence[Row]) -> None:
    sorted_rows = _sort_rows(rows)
    body = _render_table(sorted_rows)
    fm = {"type": "must_memorize", "updated_at": iso_now()}
    store.atomic_write(
        store.paths.journal / "must_memorize.md",
        frontmatter.render(fm, body),
    )


# ----- scoring operations --------------------------------------------------


def merge_candidates(
    rows: list[Row],
    candidates: Iterable[Row],
    *,
    today: str,
    similarity_threshold: float,
    max_rows: int,
) -> tuple[list[Row], list[Row]]:
    """Merge new *candidates* into *rows* and apply the cap.

    Returns ``(kept, demoted)`` — *demoted* is what got pushed off
    the bottom of the (locked desc, score desc) cap.
    """
    today = today or _today_iso()
    for cand in candidates:
        existing = _find_similar(rows, cand.memo, similarity_threshold)
        if existing is not None:
            existing.bump(today)
            # If candidate is owner_explicit (highest priority), promote.
            if cand.kind == KIND_OWNER_EXPLICIT and existing.kind != KIND_OWNER_EXPLICIT:
                existing.kind = KIND_OWNER_EXPLICIT
                existing.locked = True
        else:
            new = replace(cand)
            new.last_bump = today
            new.last_decay = today
            if new.kind == KIND_OWNER_EXPLICIT:
                new.locked = True
            rows.append(new)

    rows = _sort_rows(rows)
    if len(rows) > max_rows:
        kept, demoted = rows[:max_rows], rows[max_rows:]
        return kept, demoted
    return rows, []


def decay_all(
    rows: list[Row],
    *,
    today: str | None = None,
    days_per_point: dict[str, int],
) -> list[Row]:
    """Apply decay to non-locked rows; remove any row with score ≤ 0."""
    today = today or _today_iso()
    today_dt = date.fromisoformat(today)
    kept: list[Row] = []
    for row in rows:
        if row.locked:
            kept.append(row)
            continue
        anchor = row.last_decay or row.last_bump
        if not anchor:
            row.last_decay = today
            kept.append(row)
            continue
        anchor_dt = _parse_iso_date(anchor)
        elapsed = (today_dt - anchor_dt).days
        if elapsed < 0:
            kept.append(row)
            continue
        rate = days_per_point.get(row.kind, days_per_point.get(KIND_PREFERENCE, 7))
        points = elapsed // rate if rate > 0 else 0
        if points > 0:
            row.decay(points, today)
            # Bump anchor by exactly `points*rate` days so decay stays
            # idempotent across rapid back-to-back rebuilds.
            new_anchor = anchor_dt.fromordinal(anchor_dt.toordinal() + points * rate)
            row.last_decay = new_anchor.isoformat()
        if row.score > 0:
            kept.append(row)
    return kept


def append_dropped(store: Store, demoted: Iterable[Row]) -> None:
    """Append demoted rows to ``.dropped_memorize.md`` for 30-day audit."""
    rows = list(demoted)
    if not rows:
        return
    path = store.paths.journal / ".dropped_memorize.md"
    existing = ""
    if path.exists():
        existing = path.read_text(encoding="utf-8")
    today = _today_iso()
    new_block = f"\n## Dropped {today}\n\n" + _render_table(_sort_rows(rows))
    store.atomic_write(path, (existing + new_block).lstrip())


# ----- extractor output parsing --------------------------------------------


_BLOCK_RE = re.compile(
    r"KIND:\s*(?P<kind>owner_explicit|preference|decision|incident)\s*\n"
    r"MEMO:\s*(?P<memo>.+?)(?=\n\s*KIND:|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def parse_extractor_output(text: str) -> list[Row]:
    """Parse the LLM extractor's output into Row candidates.

    Recognises ``NONE`` as zero candidates. Tolerates whitespace and
    extra blank lines.
    """
    if not text or text.strip().upper().startswith("NONE"):
        return []
    out: list[Row] = []
    for m in _BLOCK_RE.finditer(text):
        kind = m.group("kind").lower().strip()
        memo = " ".join(m.group("memo").split())  # collapse whitespace
        if not memo:
            continue
        out.append(
            Row(
                kind=kind,
                memo=memo,
                locked=(kind == KIND_OWNER_EXPLICIT),
                source="extract",
            )
        )
    return out


# ----- CLI: pin -------------------------------------------------------------


def pin(cfg: Config, store: Store, *, memo: str, kind: str) -> int:
    """Implementation of ``tiger-memory pin``."""
    if kind not in VALID_KINDS:
        print(f"unknown kind: {kind}")
        return 2
    store.init_layout()
    rows = load(store)
    candidate = Row(
        kind=kind,
        memo=memo,
        locked=(kind == KIND_OWNER_EXPLICIT),
        source="pin",
    )
    today = _today_iso()
    with store.lock(cfg.rebuild.lock_path, cfg.rebuild.rebuild_timeout_minutes) as got:
        if not got:
            print("another tiger-memory run is in progress; try again.")
            return 1
        kept, demoted = merge_candidates(
            rows,
            [candidate],
            today=today,
            similarity_threshold=cfg.budgets.repeat_detection_similarity,
            max_rows=cfg.budgets.must_memorize_rows,
        )
        save(store, kept)
        if demoted:
            append_dropped(store, demoted)
    print(f"pinned ({kind}): {memo}")
    return 0


# ----- helpers --------------------------------------------------------------


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _parse_iso_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        # Older entries may have full ISO timestamps; truncate to date.
        return date.fromisoformat(text[:10])


def _find_similar(
    rows: list[Row], memo: str, threshold: float
) -> Row | None:
    if not rows:
        return None
    memo_n = _normalize(memo)
    best_row: Row | None = None
    best_score = 0.0
    for row in rows:
        sim = difflib.SequenceMatcher(
            a=_normalize(row.memo), b=memo_n
        ).ratio()
        if sim > best_score:
            best_score = sim
            best_row = row
    if best_score >= threshold:
        return best_row
    return None


def _normalize(s: str) -> str:
    return " ".join(s.lower().split())


def _sort_key(row: Row) -> tuple:
    # (locked desc, score desc, kind, last_bump desc)
    return (
        0 if row.locked else 1,
        -row.score,
        row.kind,
        # Reverse-sort last_bump within ties by negating: but we have strings.
        # Use the negative ordinal of the date so newer comes first.
        -_safe_date_ordinal(row.last_bump),
    )


def _safe_date_ordinal(text: str) -> int:
    if not text:
        return 0
    try:
        return _parse_iso_date(text).toordinal()
    except ValueError:
        return 0


def _sort_rows(rows: Iterable[Row]) -> list[Row]:
    return sorted(rows, key=_sort_key)


# ----- markdown table render/parse ----------------------------------------


def _render_table(rows: Sequence[Row]) -> str:
    if not rows:
        return "# Must memorize\n\n_(empty)_\n"
    lines = [
        "# Must memorize",
        "",
        "| Score | Kind | Last bump | Source | Memo |",
        "|------:|------|-----------|--------|------|",
    ]
    for r in rows:
        lines.append(
            f"| {r.score_display():>5} | {r.kind} | {r.last_bump or '-':10} | "
            f"{r.source} | {r.memo} |"
        )
    return "\n".join(lines) + "\n"


_TABLE_ROW_RE = re.compile(
    r"^\|\s*([∞\d-]+)\s*\|\s*(\w+)\s*\|\s*([\d\-]+|-)\s*\|\s*(\S+)\s*\|\s*(.+?)\s*\|\s*$"
)


def _parse_table(body: str) -> list[Row]:
    rows: list[Row] = []
    for line in body.splitlines():
        line = line.rstrip()
        if not line.startswith("|"):
            continue
        stripped = line.strip()
        # Skip separator lines like |---|---| ...
        no_pipes = stripped.replace("|", "").strip()
        if no_pipes and all(c in "-: " for c in no_pipes):
            continue
        m = _TABLE_ROW_RE.match(stripped)
        if not m:
            continue
        score_raw, kind, last_bump, source, memo = m.groups()
        if kind not in VALID_KINDS:
            continue
        locked = score_raw == "∞" or kind == KIND_OWNER_EXPLICIT
        try:
            score = 0 if score_raw == "∞" else int(score_raw)
        except ValueError:
            continue
        rows.append(
            Row(
                kind=kind,
                memo=memo,
                score=score,
                locked=locked,
                last_bump=last_bump if last_bump != "-" else "",
                last_decay=last_bump if last_bump != "-" else "",
                source=source,
            )
        )
    return rows

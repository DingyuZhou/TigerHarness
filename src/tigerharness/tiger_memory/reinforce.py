"""Associative reinforcement: strengthen an EVOKED old memory + build its link.

When a finished session's new **diary** event *evokes* (Chinese 联想 — "calls to
mind") an existing memory, that OLD memory is reinforced so it is less likely to
be forgotten — the way human memory strengthens on associative recall (task
brief; Operator, 2026-06-22). This module is the **pure, per-entry** core:

* ``reinforce_diary`` / ``reinforce_must_remember`` / ``reinforce_skill`` — the
  three reinforcement mutations, one per store, mirroring the survivor-bump
  shapes in :mod:`meditation` (``_absorb_*``) so reinforcement and merge agree.
* ``build_recall_reference`` — the **very concise** recall reference folded into
  the NEW diary note's text (Operator: "a very concise reference for old memory
  items … can work like a recall graph sometimes"). It is a minimal,
  human-findable pointer — a locating token per evoked store — NOT a restatement
  of the target, and NOT a structured graph field (the diary store keeps its
  compact, id-less :mod:`diary_format` shape untouched).

The NEW event is never reinforced (no self-bump): only the 0–2 old items it
evokes are. All functions here are pure (no I/O, no summarizer); the batched
evocation pass (:mod:`sweep`) locates the targets and drives them.
"""
from __future__ import annotations

from .config import Config
from .diary import clamp_weight
from .entries import BaseEntry, DiaryEntry, MustRememberEntry, SkillEntry
from .skills import skill_importance

#: Max characters of an evoked entry's body shown in a recall reference. Kept
#: short so the reference stays a pointer, not a restatement.
_SNIPPET_CHARS = 40


def reinforce_diary(entry: DiaryEntry, now: str, cfg: Config) -> None:
    """Reinforce an evoked diary bullet: weight +1 toward its sign, re-dated.

    Decision 1 (weight + recency). Raise the magnitude by exactly 1 toward the
    bullet's EXISTING sign — a 0-weight bullet bumps positive — then clamp to the
    diary ``weight_cap`` (via :func:`clamp_weight`), so a repeatedly-evoked "hub"
    bullet **saturates at ±cap** rather than growing unbounded. AND reset the
    decay anchor: set ``last_used`` to the evoking event's time, which re-dates
    the bullet on save (the on-disk day is ``last_used[:10]``), restoring its
    recency so it stops fading. Mirrors :func:`meditation._absorb_diary`'s clamp.
    """
    sign = 1.0 if entry.weight >= 0 else -1.0
    entry.weight = clamp_weight(sign * (abs(entry.weight) + 1.0), cfg)
    entry.last_used = now


def reinforce_must_remember(entry: MustRememberEntry) -> None:
    """Reinforce an evoked must_remember entry: ``repeat_count += 1``.

    Decision 2 (count). One more recall is one more recurrence; importance is
    derived from ``repeat_count`` exactly as :func:`meditation._absorb` does for
    a merge, so a fact recalled often ranks above a one-off.
    """
    entry.repeat_count += 1
    entry.importance = float(entry.repeat_count)


def reinforce_skill(entry: SkillEntry, cfg: Config) -> None:
    """Reinforce an evoked skill entry: ``usage_count += 1``, importance re-derived.

    Decision 2 (count). Importance grows with use via the diminishing-returns
    ``log1p`` curve in :func:`skill_importance`, so repeated evocation of one
    skill has bounded effect (no runaway hub). Mirrors
    :func:`meditation._absorb_skill` (minus the absorbed peer's count).
    """
    entry.usage_count += 1
    entry.importance = skill_importance(
        entry.usage_count, entry.last_used, entry.last_used, cfg
    )


def _snippet(text: str) -> str:
    """A short, single-line snippet of *text* for a recall reference."""
    flat = " ".join(text.split())
    if len(flat) <= _SNIPPET_CHARS:
        return flat
    return flat[:_SNIPPET_CHARS].rstrip() + "…"


def _locating_token(entry: BaseEntry) -> str:
    """A concise, human-findable token identifying one evoked memory item.

    A locating token per store: a skill by name, a must_remember by kind +
    snippet, a diary bullet by day + snippet — enough for a human reading
    ``diary.md`` to find the referenced item, never a full restatement.
    """
    if isinstance(entry, SkillEntry):
        return f'skill "{entry.name}"'
    if isinstance(entry, MustRememberEntry):
        return f'must_remember/{entry.kind} "{_snippet(entry.text)}"'
    # DiaryEntry — the only remaining store type.
    return f'diary {entry.last_used[:10]} "{_snippet(entry.text)}"'


def build_recall_reference(targets: list[BaseEntry]) -> str:
    """Build the concise in-note recall reference for the 0–2 evoked old items.

    Returns a single-line suffix to append to the NEW diary note's text, e.g.
    ``  ↪ recalls: skill "commit via -F"; diary 2026-06-19 "tiger-memory…"``.
    Empty string when nothing was evoked, so a 0-evocation note is unchanged. The
    suffix is one line (no newlines) so the appended note still matches the
    ``- (±N) note`` bullet grammar and round-trips through :mod:`diary_format`.
    """
    if not targets:
        return ""
    tokens = "; ".join(_locating_token(t) for t in targets)
    return f" ↪ recalls: {tokens}"

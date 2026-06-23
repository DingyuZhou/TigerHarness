"""Associative-evocation pass: judge what each new diary note recalls, reinforce.

The one model touch point for associative reinforcement (task brief decision 6).
After a session's candidates are ingested, this pass asks the summarizer — in a
SINGLE batched call over all of this ingest's new diary notes — which existing
memory items (0, 1, or at most 2 per note, across diary / must_remember / skills)
each new note *evokes* (Chinese 联想, "calls to mind"). Each evoked OLD item is
reinforced (:mod:`reinforce`) so it is less likely to be forgotten, and a concise
recall reference to those items is appended to the new note's text.

It is SEPARATE from meditation's merge (which collapses near-duplicates on
overflow); evocation keeps both entries and strengthens the old one. No self-bump
(decision 5): the candidate context is OLD items only — this ingest's own new
entries are excluded — so a new note can never evoke or reinforce itself.

Gated by ``cfg.memory.diary.evocation_enabled`` (default off): enabling it adds a
model call at ingest, so it is a deliberate, per-deployment (subscription-rail)
opt-in. The default :class:`MockSummarizer` returns no ``NOTE`` lines, so under
the mock the parse yields zero evocations and the pass is a deterministic no-op.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .bounded_store import BoundedStore
from .config import Config
from .entries import (
    STORE_DIARY,
    STORE_MUST_REMEMBER,
    STORE_SKILLS,
    BaseEntry,
    DiaryEntry,
    MustRememberEntry,
)
from .reinforce import (
    build_recall_reference,
    reinforce_diary,
    reinforce_must_remember,
    reinforce_skill,
)
from .summarizers.base import Summarizer

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .lifecycle import Candidates

log = logging.getLogger("tigerharness.tiger_memory.evocation")

#: At most this many evocations per new note (brief: "0, 1, or at most 2").
_MAX_EVOCATIONS = 2
#: ``NOTE <i>: <indices | NONE>`` — one line per new note in the model reply.
_NOTE_LINE = re.compile(r"^NOTE\s+(\d+)\s*:\s*(.+)$", re.MULTILINE)
#: Chars of an item's body shown in the prompt (a hint, not the whole entry).
_PROMPT_SNIPPET = 100


@dataclass
class EvokeLog:
    """What one evocation pass did (for logging / tests)."""

    notes: int = 0           # new diary notes considered
    referenced: int = 0      # new notes that got a recall reference
    reinforced: list[str] = field(default_factory=list)  # evoked item ids


def _snippet(text: str, limit: int = _PROMPT_SNIPPET) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit].rstrip() + "…"


def _split_new_diary(
    loaded: list[BaseEntry], new_candidates: list[DiaryEntry]
) -> tuple[list[BaseEntry], list[BaseEntry]]:
    """Partition *loaded* diary entries into (this-sweep's new, pre-existing old).

    Diary bullets have no persisted id, so a new candidate is matched to its
    loaded twin by signature ``(text, weight, day)`` (the day is ``last_used[:10]``
    — what the on-disk bullet carries). Each loaded entry is claimed at most once,
    so duplicates collapse safely; an unmatched candidate (should not happen) is
    simply absent from the "new" set. Identity, not a re-matched key, is what the
    caller then mutates — so re-dating a reinforced bullet can't drift the lookup.
    """
    claimed: set[int] = set()
    new_loaded: list[BaseEntry] = []
    for cand in new_candidates:
        sig = (cand.text, float(cand.weight), cand.last_used[:10])
        for j, e in enumerate(loaded):
            if j in claimed:
                continue
            if (e.text, float(e.weight), e.last_used[:10]) == sig:  # type: ignore[attr-defined]
                new_loaded.append(e)
                claimed.add(j)
                break
    old_loaded = [e for j, e in enumerate(loaded) if j not in claimed]
    return new_loaded, old_loaded


def _build_prompt(new_notes: list[BaseEntry], context: list[BaseEntry]) -> str:
    """Render the single batched evocation-judgment prompt."""
    lines = [
        "You judge associative recall (联想) for one person's memory store.",
        "For EACH new diary note, list the existing memory items it evokes — the",
        "ones it genuinely 'calls to mind'. List 0, 1, or AT MOST 2 per note, by",
        "their [index]. Most notes evoke nothing; prefer NONE over a weak guess.",
        "",
        "Reply with exactly one line per note, nothing else:",
        "  NOTE <i>: <comma-separated indices, or NONE>",
        "",
        "Existing memory items:",
    ]
    for k, e in enumerate(context):
        lines.append(f"[{k}] ({e.store_name}) {_snippet(e.text)}")
    lines.append("")
    lines.append("New diary notes:")
    for i, e in enumerate(new_notes):
        lines.append(f"NOTE {i}: {_snippet(e.text)}")
    return "\n".join(lines)


def _parse_response(
    response: str, *, n_notes: int, n_context: int
) -> dict[int, list[int]]:
    """Parse the model reply into ``{note_index: [context_index, ...]}``.

    Defensive by construction (the model is fallible): a note index out of range
    is ignored; a context index out of range or non-numeric token is dropped; a
    body of ``NONE`` (any case) yields no evocations; more than two valid indices
    are clamped to the first two. A reply with no ``NOTE`` lines (e.g. the mock
    summarizer) yields an empty map — zero evocations.
    """
    out: dict[int, list[int]] = {}
    for m in _NOTE_LINE.finditer(response):
        i = int(m.group(1))
        if not (0 <= i < n_notes):
            continue
        body = m.group(2).strip()
        idxs: list[int] = []
        if body.upper() != "NONE":
            for tok in body.replace(",", " ").split():
                if tok.isdigit():
                    k = int(tok)
                    if 0 <= k < n_context and k not in idxs:
                        idxs.append(k)
        out[i] = idxs[:_MAX_EVOCATIONS]
    return out


def _reinforce(target: BaseEntry, now: str, cfg: Config) -> None:
    if isinstance(target, DiaryEntry):
        reinforce_diary(target, now, cfg)
    elif isinstance(target, MustRememberEntry):
        reinforce_must_remember(target)
    else:  # SkillEntry
        reinforce_skill(target, cfg)  # type: ignore[arg-type]


def evoke_and_reinforce(
    bstore: BoundedStore,
    cfg: Config,
    candidates: "Candidates",
    summarizer: Summarizer,
    *,
    now: str,
) -> EvokeLog | None:
    """Run the evocation pass for one ingest's new diary notes. Returns its log,
    or ``None`` when there was nothing to do (no new notes / no candidate context).

    Precondition: *candidates* have already been ingested (written to the stores).
    The pass loads the post-ingest stores, separates this ingest's new items from
    the pre-existing ones (the candidate context), asks the summarizer once, and
    for each evoked old item reinforces it and records a concise reference on the
    evoking new note. Only the stores it actually changed are re-saved.
    """
    new_diary_candidates = list(candidates.diary)
    if not new_diary_candidates:
        return None  # no new diary notes this ingest -> no model call

    diary = bstore.load(STORE_DIARY)
    must = bstore.load(STORE_MUST_REMEMBER)
    skills = bstore.load(STORE_SKILLS)

    new_notes, old_diary = _split_new_diary(diary, new_diary_candidates)
    new_skill_ids = {e.id for e in candidates.skills}
    new_mr_ids = {e.id for e in candidates.must_remember}
    old_must = [e for e in must if e.id not in new_mr_ids]
    old_skills = [e for e in skills if e.id not in new_skill_ids]

    # OLD items only -> a new note can never evoke itself (no self-bump).
    context = old_diary + old_must + old_skills
    if not new_notes or not context:
        return None

    response = summarizer.summarize(
        prompt=_build_prompt(new_notes, context), max_words=120
    )
    per_note = _parse_response(
        response, n_notes=len(new_notes), n_context=len(context)
    )

    result = EvokeLog(notes=len(new_notes))
    changed: set[str] = set()
    for i, note in enumerate(new_notes):
        targets: list[BaseEntry] = []
        for k in per_note.get(i, []):
            target = context[k]
            _reinforce(target, now, cfg)
            targets.append(target)
            result.reinforced.append(target.id)
            changed.add(target.store_name)
        if targets:
            note.text = note.text + build_recall_reference(targets)
            result.referenced += 1
            changed.add(STORE_DIARY)

    if STORE_DIARY in changed:
        bstore.save_atomic(STORE_DIARY, diary)
    if STORE_MUST_REMEMBER in changed:
        bstore.save_atomic(STORE_MUST_REMEMBER, must)
    if STORE_SKILLS in changed:
        bstore.save_atomic(STORE_SKILLS, skills)

    log.info(
        "evocation: %d new notes, %d referenced, %d items reinforced",
        result.notes, result.referenced, len(result.reinforced),
    )
    return result

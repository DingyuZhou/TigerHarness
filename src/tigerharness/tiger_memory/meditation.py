"""Meditation — the bounded-store compaction engine (design §5; plan §2 dev-2).

One engine, run per store, that turns "over the overflow limit" back into
"under max." It implements design §5's **ordered** recipe:

  1. **merge** duplicate / near-duplicate entries — merging RAISES the
     survivor's scalar (importance, or emotional magnitude), clamped;
  2. *(must_remember only)* **relevance-check** each ``owner_explicit``
     directive against the live ``mission_text`` and **downgrade** stale ones
     to a normal kind — this runs BEFORE any forget so Mitsui's forget-guard
     never trips on an un-relevance-checked owner directive;
  3. **compact** verbose survivors (summarizer rewrites the body shorter);
  4. **forget** the lowest-keep-ranked entries until length/count < ``max``,
     via Mitsui's guarded :meth:`BoundedStore.forget`.

Invariants (design §5; plan §4):

- **Ordered.** Steps run 1→4. The relevance-check (step 2) always precedes
  any forget so a still-relevant owner directive is never dropped.
- **Idempotent.** A store already under ``max`` is a no-op (no summarizer
  calls, empty log).
- **Hysteresis.** The *caller* fires meditation only at/above
  ``overflow_limit`` (Mitsui's :meth:`is_over_overflow`); meditation itself
  compacts back below ``max`` and then stops.
- **Terminal "nothing safe to forget"** (plan §4 Kogure R1#1). If, after
  merge + compact, the store is still over ``max`` and the only entries left
  to drop are still-relevant ``owner_explicit`` directives, meditation LOGS
  an over-max warning and LEAVES the store intact — it never force-drops to
  hit the number.

Only the *judgement* (which entries are similar, which directives are stale,
how to compact) goes through the pluggable summarizer registry; all ordering,
ranking, clamping, and bookkeeping is pure Python. CI runs it under the mock
summarizer (plan §5b) — zero live-model calls.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .bounded_store import BoundedStore, ForgetGuardError
from .config import Config
from .emotional import clamp_weight, emotional_keep_rank
from .entries import (
    KIND_DECISION,
    KIND_OWNER_EXPLICIT,
    STORE_MUST_REMEMBER,
    STORE_SKILLS,
    BaseEntry,
    EmotionalEntry,
    MustRememberEntry,
    SkillEntry,
)
from .ranking import recency_score
from .skills import skill_importance, skills_keep_rank
from .state import iso_now
from .summarizers.base import Summarizer

log = logging.getLogger("tigerharness.tiger_memory.meditation")


# ----- meditation log -------------------------------------------------------


@dataclass
class MeditationLog:
    """A record of what one meditation pass did to one store (design §5).

    Every list holds entry ids. ``merged`` maps each *dropped* duplicate's id
    to the survivor it folded into; ``downgraded`` lists owner directives
    relevance-checked-and-downgraded to normal; ``compacted`` lists survivors
    whose body the summarizer shortened; ``forgotten`` lists ids dropped to
    get under ``max``. ``over_max`` is the terminal warning flag (plan §4):
    True iff the pass finished still over ``max`` because the only remaining
    drops would be still-relevant owner directives.
    """

    store_name: str
    merged: dict[str, str] = field(default_factory=dict)
    downgraded: list[str] = field(default_factory=list)
    compacted: list[str] = field(default_factory=list)
    forgotten: list[str] = field(default_factory=list)
    over_max: bool = False
    skipped_no_op: bool = False

    @property
    def changed(self) -> bool:
        """True iff the pass mutated the store (something to persist)."""
        return bool(
            self.merged or self.downgraded or self.compacted or self.forgotten
        )


# ----- the summarizer judgement seam (only LLM call) ------------------------

# Verdict sentinels the summarizer must echo. The judgement prompts ask for a
# single uppercase token on its own; parsing is tolerant (substring match,
# case-insensitive) so a chatty backend that wraps the token in prose still
# parses, while the mock can return the bare token (plan §5b).
_VERDICT_YES = "YES"
_VERDICT_NO = "NO"


def _ask_yes_no(summarizer: Summarizer, prompt: str, *, default: bool) -> bool:
    """Ask a yes/no judgement; parse YES/NO from the body (default on miss).

    Tolerant parse: case-insensitive, first decisive token wins. If the body
    contains neither token (a backend that ignored the instruction), fall back
    to *default* — chosen per call site to be the SAFE answer (e.g. "not
    similar", "still relevant") so an unparseable verdict never destroys data.
    """
    raw = summarizer.summarize(prompt=prompt, max_words=8).strip().upper()
    yes_at = raw.find(_VERDICT_YES)
    no_at = raw.find(_VERDICT_NO)
    if yes_at < 0 and no_at < 0:
        return default
    if yes_at < 0:
        return False
    if no_at < 0:
        return True
    return yes_at <= no_at


def _judge_similar(summarizer: Summarizer, a: BaseEntry, b: BaseEntry) -> bool:
    """Does the summarizer judge *a* and *b* near-duplicate? (default: NO)."""
    prompt = (
        "Are these two memory entries duplicates or near-duplicates of the "
        "same fact/lesson/feeling? Answer with a single word: YES or NO.\n\n"
        f"ENTRY A:\n{a.text}\n\nENTRY B:\n{b.text}\n"
    )
    return _ask_yes_no(summarizer, prompt, default=False)


def _judge_stale(
    summarizer: Summarizer, entry: MustRememberEntry, mission_text: str
) -> bool:
    """Is this owner directive stale vs the live mission? (default: NO).

    Default NO = "still relevant": an unparseable judgement keeps the
    directive elevated rather than downgrading-then-forgetting it.
    """
    prompt = (
        "An owner directive is STALE if it was tied to a feature/goal the "
        "team no longer pursues under the current mission. Is this directive "
        "stale? Answer with a single word: YES (stale) or NO (still "
        "relevant).\n\n"
        f"CURRENT MISSION:\n{mission_text}\n\nDIRECTIVE:\n{entry.text}\n"
    )
    return _ask_yes_no(summarizer, prompt, default=False)


def _compact_text(
    summarizer: Summarizer, entry: BaseEntry, max_words: int
) -> str:
    """Rewrite *entry*'s body shorter; keep the original if it does not shrink.

    The summarizer returns the compacted body. We only accept it if it is
    non-empty AND strictly shorter (in characters) than the original — a
    backend that returns boilerplate or a longer rewrite must not *grow* the
    store we are trying to shrink.
    """
    prompt = (
        f"Rewrite this memory entry more concisely (<= {max_words} words), "
        "preserving every concrete fact. Return ONLY the rewritten text.\n\n"
        f"{entry.text}\n"
    )
    out = summarizer.summarize(prompt=prompt, max_words=max_words).strip()
    if out and len(out) < len(entry.text):
        return out
    return entry.text


# ----- per-store keep-rank dispatch -----------------------------------------


def keep_rank(entry: BaseEntry, now: str, cfg: Config) -> tuple[float, float]:
    """Sortable keep-rank for *entry* (higher = keep; ascending = forget order).

    Dispatches by store: must_remember by ``importance`` + recency, emotional
    by decayed ``|weight|`` + recency, skills by ``importance``(usage) +
    recency. The single entry point meditation sorts by to choose drop order.
    """
    if isinstance(entry, EmotionalEntry):
        return emotional_keep_rank(entry, now, cfg)
    if isinstance(entry, SkillEntry):
        return skills_keep_rank(entry, now, cfg)
    # MustRememberEntry: keep by importance, recency tie-break.
    return (float(entry.importance), recency_score(entry.last_used, now))


# ----- the engine -----------------------------------------------------------


def meditate(
    store_name: str,
    persona_ctx: str,
    mission_text: str,
    summarizer: Summarizer,
    cfg: Config,
    store: BoundedStore,
) -> MeditationLog:
    """Compact *store_name* from over-overflow back under ``max`` (design §5).

    Runs the ordered recipe (merge → relevance-check/downgrade → compact →
    forget) under Mitsui's per-store lock. Returns a :class:`MeditationLog`.
    A store already under ``max`` is a no-op (``skipped_no_op``); the terminal
    "nothing safe to forget" case leaves the store intact and sets
    ``over_max`` (plan §4).

    ``persona_ctx`` is threaded into the judgement prompts' context by the
    caller-supplied summarizer wiring; it is accepted here to keep the seam
    end-to-end even though the ranking/ordering logic does not branch on it.
    ``mission_text`` is the live team goal (Miyagi sources it from
    ``charter/README.md``) against which owner directives are relevance-checked.
    """
    del persona_ctx  # context is carried by the summarizer backend wiring.
    now = iso_now()
    log_rec = MeditationLog(store_name=store_name)

    with store.store_lock(store_name):
        entries = list(store.load(store_name))

        # Idempotent no-op: already under max -> nothing to do (no LLM calls).
        if not _over_max(store, store_name, entries):
            log_rec.skipped_no_op = True
            return log_rec

        # 1. merge near-duplicates (raises survivor scalar, clamped).
        entries = _merge_pass(entries, summarizer, cfg, log_rec)

        # 2. must_remember only: relevance-check + downgrade BEFORE any forget.
        relevance_checked: set[str] = set()
        if store_name == STORE_MUST_REMEMBER:
            entries = _relevance_pass(
                entries, summarizer, mission_text, log_rec, relevance_checked
            )

        # 3. compact verbose survivors.
        entries = _compact_pass(entries, store, store_name, summarizer, log_rec)

        # 4. forget the lowest-ranked until under max (guarded).
        entries = _forget_pass(
            entries, store, store_name, now, cfg, relevance_checked, log_rec
        )

        if log_rec.changed:
            store.save_atomic(store_name, entries)

    return log_rec


def _over_max(store: BoundedStore, store_name: str, entries) -> bool:
    """True iff *entries* exceed the store's ``max`` bound (compact target)."""
    bound = store.max_bound(store_name)
    if store_name == STORE_SKILLS:
        return store.count(entries) > bound
    return store.length_chars(entries) > bound


# ----- step 1: merge --------------------------------------------------------


def _merge_pass(
    entries: list[BaseEntry],
    summarizer: Summarizer,
    cfg: Config,
    log_rec: MeditationLog,
) -> list[BaseEntry]:
    """Fold near-duplicates into the earlier survivor, raising its scalar.

    Single forward pass: each entry is compared against the already-kept
    survivors; the first survivor the summarizer judges near-duplicate absorbs
    it. Merging RAISES the survivor's scalar (clamped) so a fact repeated
    across sessions ranks higher, not lower. The dropped id -> survivor id is
    recorded in the log.
    """
    survivors: list[BaseEntry] = []
    for entry in entries:
        target = None
        for kept in survivors:
            if _judge_similar(summarizer, kept, entry):
                target = kept
                break
        if target is None:
            survivors.append(entry)
            continue
        _absorb(target, entry, cfg)
        log_rec.merged[entry.id] = target.id
    return survivors


def _absorb(target: BaseEntry, dropped: BaseEntry, cfg: Config) -> None:
    """Fold *dropped* into *target*, raising the survivor's scalar (clamped).

    Merge only ever compares two entries from the *same* store, so ``target``
    and ``dropped`` are always the same concrete type — dispatch on the
    survivor's type alone.
    """
    if isinstance(target, EmotionalEntry):
        _absorb_emotional(target, dropped, cfg)
    elif isinstance(target, SkillEntry):
        _absorb_skill(target, dropped, cfg)
    else:  # MustRememberEntry
        # A repeated directive matters more: bump importance by 1.0.
        target.importance = float(target.importance) + 1.0


def _absorb_emotional(
    target: EmotionalEntry, dropped: BaseEntry, cfg: Config
) -> None:
    """Magnitude grows toward the stronger feeling; clamp at the cap."""
    other = dropped.weight  # type: ignore[attr-defined]
    merged = target.weight + other
    if abs(target.weight) >= abs(other):
        sign = 1.0 if target.weight >= 0 else -1.0
    else:
        sign = 1.0 if other >= 0 else -1.0
    magnitude = max(abs(target.weight), abs(other), abs(merged))
    target.weight = clamp_weight(sign * magnitude, cfg)


def _absorb_skill(target: SkillEntry, dropped: BaseEntry, cfg: Config) -> None:
    """Usage accrues; importance re-derived from the summed usage."""
    target.usage_count += max(0, dropped.usage_count) + 1  # type: ignore[attr-defined]
    target.importance = skill_importance(
        target.usage_count, target.last_used, target.last_used, cfg
    )


# ----- step 2: relevance-check (must_remember only) -------------------------


def _relevance_pass(
    entries: list[BaseEntry],
    summarizer: Summarizer,
    mission_text: str,
    log_rec: MeditationLog,
    relevance_checked: set[str],
) -> list[BaseEntry]:
    """Relevance-check every owner directive; downgrade stale ones to normal.

    A directive judged **stale** vs the mission is downgraded to ``decision``
    (a normal kind) and its id added to *relevance_checked* — after which it
    rejoins the ordinary forget pool and the forget-guard will permit dropping
    it (design §4.2). A directive judged **still relevant** is left
    ``owner_explicit`` and deliberately NOT added: the forget-guard then
    refuses to drop it, which is exactly the terminal "never drop a
    still-relevant owner directive" invariant (design §5; plan §4). So
    *relevance_checked* ends up holding precisely the downgraded ids — the
    only owner directives this cycle licensed for forgetting.
    """
    for entry in entries:
        if (
            isinstance(entry, MustRememberEntry)
            and entry.kind == KIND_OWNER_EXPLICIT
            and _judge_stale(summarizer, entry, mission_text)
        ):
            entry.kind = KIND_DECISION
            relevance_checked.add(entry.id)
            log_rec.downgraded.append(entry.id)
    return entries


# ----- step 3: compact ------------------------------------------------------


def _compact_pass(
    entries: list[BaseEntry],
    store: BoundedStore,
    store_name: str,
    summarizer: Summarizer,
    log_rec: MeditationLog,
) -> list[BaseEntry]:
    """Shorten verbose survivors (skills store excepted — count-bounded).

    The skills store is count-bounded, so compacting a body never helps it get
    under ``max`` — skip the LLM calls there. For the length-bounded stores,
    compact only while still over ``max``, longest-body-first, until under
    (or nothing shrinks further).
    """
    if store_name == STORE_SKILLS:
        return entries
    max_words = max(1, store.max_bound(store_name) // 50)
    # Work over indices longest-first so the biggest wins land first.
    order = sorted(
        range(len(entries)), key=lambda i: len(entries[i].text), reverse=True
    )
    for i in order:
        if not _over_max(store, store_name, entries):
            break
        entry = entries[i]
        shorter = _compact_text(summarizer, entry, max_words)
        if shorter != entry.text:
            entry.text = shorter
            log_rec.compacted.append(entry.id)
    return entries


# ----- step 4: forget -------------------------------------------------------


def _forget_pass(
    entries: list[BaseEntry],
    store: BoundedStore,
    store_name: str,
    now: str,
    cfg: Config,
    relevance_checked: set[str],
    log_rec: MeditationLog,
) -> list[BaseEntry]:
    """Drop the lowest-keep-ranked entries until under ``max`` (guarded).

    Forget order = keep-rank ascending (lowest value first). A candidate that
    Mitsui's forget-guard refuses (a still-relevant owner directive not in
    *relevance_checked*) is SKIPPED, not forced — we move to the next-lowest.
    If the store cannot get under ``max`` without dropping a protected
    directive, we stop and set the terminal ``over_max`` warning (plan §4):
    the store is left intact rather than force-dropped.
    """
    if not _over_max(store, store_name, entries):
        return entries

    ranked = sorted(entries, key=lambda e: keep_rank(e, now, cfg))
    for candidate in ranked:
        if not _over_max(store, store_name, entries):
            break
        try:
            survivors = store.forget(
                store_name,
                entries,
                [candidate.id],
                relevance_checked_ids=relevance_checked,
            )
        except ForgetGuardError:
            # Protected owner directive (not relevance-checked this cycle):
            # skip it, do not force-drop, move to the next-lowest candidate.
            continue
        entries = survivors
        log_rec.forgotten.append(candidate.id)

    if _over_max(store, store_name, entries):
        # Terminal: still over max, but the only remaining drops are protected
        # owner directives. Leave the store intact, warn, do not force-drop.
        log_rec.over_max = True
        log.warning(
            "meditation: store %r still over max after merge+compact "
            "(%d entries remain, all still-relevant owner directives); "
            "leaving intact (design §5 nothing-safe-to-forget).",
            store_name,
            len(entries),
        )
    return entries

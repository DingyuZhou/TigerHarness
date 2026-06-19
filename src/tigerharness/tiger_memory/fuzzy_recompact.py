"""Fuzzy re-compaction — the meditation AI step that folds aging memory into
the fuzzy store (brief §meditation 5).

Takes the items selected to age out of the sharp stores this meditation (diary +
must_remember) plus the EXISTING fuzzy.md, and asks the summarizer to compact
them into one coarse, grouped blob — most-important-first. Because the same aged
content is re-summarised every meditation it coarsens progressively, and the
caller's :func:`fuzzy_store.save_fuzzy` hard-bounds the result, so the fuzzy
store CONVERGES rather than grows (the no-grow guarantee lives in save_fuzzy;
this only produces the candidate text). Uses the meditation summarizer seam, so
it is mockable; nothing is deleted — aged content loses granularity, not
existence (no silent loss).
"""
from __future__ import annotations

import logging

from .config import Config
from .entries import DiaryEntry, MustRememberEntry
from .summarizers.base import Summarizer

log = logging.getLogger("tigerharness.tiger_memory.fuzzy_recompact")


def recompact_fuzzy(
    summarizer: Summarizer,
    fuzzed_diary: list[DiaryEntry],
    fuzzed_mr: list[MustRememberEntry],
    existing_fuzzy: str,
    cfg: Config,
) -> str:
    """Compact aging items + *existing_fuzzy* into one grouped blob.

    Returns the new fuzzy text (the caller saves it via
    :func:`fuzzy_store.save_fuzzy`, which hard-bounds it). If there is nothing
    new to fold in (no aged items this meditation), returns *existing_fuzzy*
    unchanged — NO summarizer call. If the summarizer returns empty, falls back
    to *existing_fuzzy* (never blanks the store).
    """
    if not fuzzed_diary and not fuzzed_mr:
        return existing_fuzzy

    max_length = cfg.memory.fuzzy.max_length
    parts: list[str] = []
    if existing_fuzzy.strip():
        parts.append("EXISTING FUZZY MEMORY:\n" + existing_fuzzy.strip())
    if fuzzed_mr:
        parts.append(
            "AGING FACTS:\n" + "\n".join(f"- {e.text}" for e in fuzzed_mr)
        )
    if fuzzed_diary:
        parts.append(
            "AGING DIARY ITEMS:\n"
            + "\n".join(f"- ({e.weight:+g}) {e.text}" for e in fuzzed_diary)
        )
    bundle = "\n\n".join(parts)
    # Rough word budget from the character bound (vendor-neutral ~5 chars/word);
    # save_fuzzy enforces the hard character bound regardless.
    max_words = max(1, max_length // 5)
    prompt = (
        "Compact the older memory below into a SHORT, coarse, grouped summary "
        f"(<= {max_words} words). Group related items, keep the gist and drop "
        "detail, and put the most important first. This is long-term 'fuzzy' "
        "memory — losing granularity is fine, losing the gist is not. Return "
        f"ONLY the summary.\n\n{bundle}\n"
    )
    out = summarizer.summarize(prompt=prompt, max_words=max_words).strip()
    log.warning(
        "fuzzy recompact: folded %d diary + %d fact(s) (%d chars) into fuzzy",
        len(fuzzed_diary), len(fuzzed_mr), len(bundle),
    )
    return out or existing_fuzzy

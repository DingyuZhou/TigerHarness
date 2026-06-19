"""The fuzzy store — coarsened, grouped free-text memory (4-store model).

``fuzzy.md`` is the NEW 4th store (brief §store roster). Unlike skills /
must_remember (typed frontmatter entries) and diary (dated weighted bullets),
the fuzzy store is **free text**: the meditation re-compaction phase asks the
summarizer to compact {aging must_remember, aging diary, the existing fuzzy.md}
into one grouped, coarsened blob. So there is no per-entry schema and no parser —
the store is simply loaded whole at session start and **hard-bounded by
characters** on write.

The hard bound is the convergence guarantee (brief §meditation 5): even if the
summarizer returns text over ``memory.fuzzy.max_length``, :func:`bound_fuzzy`
deterministically trims it back so ``len(fuzzy.md) <= max_length`` ALWAYS holds —
the store converges, never grows. Convention: the re-compaction prompt orders
content most-important-first, so the trimmed TAIL is the least-important
(oldest / coarsest) material. Length is CHARACTERS, never tokens (vendor-neutral).
"""
from __future__ import annotations

import logging

from .config import Config
from .entries import STORE_FUZZY
from .store import Store

log = logging.getLogger("tigerharness.tiger_memory.fuzzy_store")


def fuzzy_path(store: Store):
    """Path to the persona's ``fuzzy.md`` (under the store's journal dir)."""
    return store.paths.journal / f"{STORE_FUZZY}.md"


def load_fuzzy(store: Store) -> str:
    """Read the whole fuzzy store (``""`` if absent).

    Lenient by design (the no-safety-net store has no backup): a non-UTF8 byte
    degrades to a replacement char (logged) rather than raising, mirroring the
    bounded stores' ``load``.
    """
    path = fuzzy_path(store)
    if not path.exists():
        return ""
    text = path.read_bytes().decode("utf-8", errors="replace")
    if "�" in text:
        log.warning(
            "tiger-memory: fuzzy store file %s had non-UTF8 byte(s) replaced.",
            path,
        )
    return text


def bound_fuzzy(text: str, max_length: int) -> tuple[str, int]:
    """Trim *text* to at most *max_length* CHARACTERS on a line boundary.

    Returns ``(bounded_text, dropped_chars)``. Text already within the bound is
    returned unchanged with ``dropped_chars == 0``. Over the bound, it is cut to
    ``max_length`` and then back to the last newline so whole lines survive (the
    least-important tail is dropped); a single line longer than the bound is hard
    cut. The convergence guarantee: the result is always ``<= max_length``.
    """
    if len(text) <= max_length:
        return text, 0
    head = text[:max_length]
    nl = head.rfind("\n")
    bounded = head[: nl + 1] if nl > 0 else head
    return bounded, len(text) - len(bounded)


def save_fuzzy(cfg: Config, store: Store, text: str) -> int:
    """Bound *text* to ``memory.fuzzy.max_length`` then atomically write fuzzy.md.

    The hard bound is enforced HERE (defense in depth) so a writer can never grow
    the fuzzy store past its max even if upstream re-compaction returned an
    over-length blob. Returns the number of characters trimmed (0 if it fit).
    """
    bounded, dropped = bound_fuzzy(text, cfg.memory.fuzzy.max_length)
    store.paths.journal.mkdir(parents=True, exist_ok=True)
    store.atomic_write(fuzzy_path(store), bounded)
    if dropped:
        log.warning(
            "tiger-memory: fuzzy store over max_length (%d); trimmed %d char(s) "
            "from the tail to converge.", cfg.memory.fuzzy.max_length, dropped,
        )
    return dropped

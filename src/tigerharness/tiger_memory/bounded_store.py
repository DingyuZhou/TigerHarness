"""Bounded-store substrate for the memory revamp (design §4, §5; plan §1+§2).

This is the durable layer the scoring and glue layers build on. It owns,
for the three bounded stores (``skills`` / ``must_remember`` / ``topics``):

- **persistence** — ``load`` / ``save_atomic`` over a list of typed entries
  (``entries.py``), crash-safe via temp-file + ``os.replace``;
- **measurement** — ``length_chars`` (CHARACTERS, never tokens, design §8)
  and ``is_over_overflow`` (the hysteresis trigger, design §4);
- **concurrency** — ``store_lock(store_name)``: a per-store file lock so two
  sessions cannot meditate the same store at once (design §5 invariant);
- **the forget-guard** — ``forget()`` REFUSES to drop an ``operator_explicit``
  must-remember entry that has not yet passed this cycle's relevance-check
  (design §5 invariant; the no-safety-net correctness anchor, plan §2.4).

On-disk layout: one markdown file per store under the store's journal dir
(``skills.md`` / ``must_remember.md`` / ``topics.md``). Each file is a
sequence of entries, every entry a YAML-frontmatter block (structured
fields) followed by its body text, blocks separated by a sentinel line. The
whole file is rewritten atomically on every ``save_atomic`` — compaction
operates on the entire store at once, so a whole-file swap is both correct
and the simplest thing that is crash-safe.

Bounds (ADR 0007) are all **characters** from the ``memory:`` config block,
measured over what a persona actually loads: skills and topics are bounded
on their RENDERED INDEX (:mod:`indexes`), since only the index loads at
session start, plus a per-entry detail bound for each skill/topic detail
file; must_remember is bounded on total entry length. ``is_over_overflow``
returns True only at/above ``overflow_limit`` — never inside the ``max <=
n < overflow_limit`` hysteresis band (design §4 no-thrash).
"""
from __future__ import annotations

import errno
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from . import frontmatter, indexes
from .config import Config
from .entries import (
    KIND_OPERATOR_EXPLICIT,
    STORE_MUST_REMEMBER,
    STORE_NAMES,
    STORE_SKILLS,
    STORE_TOPICS,
    BaseEntry,
    EntryError,
    MustRememberEntry,
    SkillEntry,
    TopicEntry,
    entry_from_frontmatter,
)
from .store import Store, _pid_alive

log = logging.getLogger("tigerharness.tiger_memory.bounded_store")


# Filename per store (under the store's journal dir).
STORE_FILENAMES = {name: f"{name}.md" for name in STORE_NAMES}

# Sentinel that separates serialized entries within a store file. A line of
# its own; chosen so it cannot collide with markdown headings or the YAML
# frontmatter delimiter (``---``).
_ENTRY_SEP = "<!-- tiger-memory-entry -->"


class ForgetGuardError(RuntimeError):
    """Raised when ``forget()`` is asked to drop a still-protected directive.

    The no-safety-net anchor (design §5): an ``operator_explicit`` must-remember
    entry MUST pass this meditation cycle's relevance-check before it can be
    forgotten. Asking to drop one that has not is a correctness bug in the
    caller, so we raise loudly rather than silently lose an operator directive.
    """


class StoreLockHeld(RuntimeError):
    """Raised when ``store_lock`` cannot acquire the per-store lock.

    Another session is meditating this store (design §5). The caller should
    back off rather than meditate concurrently.
    """


class BoundedStore:
    """The three bounded stores for one persona, over a base ``Store``.

    Wraps an existing :class:`~tigerharness.tiger_memory.store.Store` (reused
    for path layout + atomic write) and the ``memory:`` config block. Holds
    no mutable state of its own — every method reads/writes the disk.
    """

    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        self.memory = cfg.memory

    # ----- paths --------------------------------------------------------

    def _store_path(self, store_name: str) -> Path:
        if store_name not in STORE_FILENAMES:
            raise EntryError(f"unknown store_name: {store_name!r}")
        return self.store.paths.journal / STORE_FILENAMES[store_name]

    def _lock_path(self, store_name: str) -> Path:
        if store_name not in STORE_FILENAMES:
            raise EntryError(f"unknown store_name: {store_name!r}")
        return self.store.paths.journal / f".{store_name}.lock"

    # ----- load / save --------------------------------------------------

    def load(self, store_name: str) -> list[BaseEntry]:
        """Read all entries for *store_name* (empty list if the file is absent).

        Reconstructs typed entries (``entries.py``) from each frontmatter
        block. The read is **lenient by design** — a corrupt entry (or even a
        corrupt byte) must never take the whole store down with it (the
        no-safety-net store has no backup to fall back on):

        - The file is decoded with ``errors="replace"`` so a single non-UTF8
          byte degrades to a replaced/garbled block (which then skips below)
          rather than raising ``UnicodeDecodeError`` and denying every good
          sibling entry. A warning is logged when replacement occurs.
        - Blocks with no parseable frontmatter are skipped (forward/backward
          tolerant).
        - A block whose frontmatter parses but whose fields are corrupt (e.g. a
          bad numeric type) raises ``EntryError`` from ``entry_from_frontmatter``;
          that single block is skipped and logged.
        - An entry that reconstructs but is schema-INVALID (e.g. an empty
          ``reaction``, or a non-finite weight) is also skipped+logged: load is
          symmetric with ``save_atomic``, which validates every entry. Without
          this, a parseable-but-invalid entry would load fine yet crash the
          next meditation's final ``save_atomic`` after merge/forget already
          mutated state — so we drop it at load time instead (QI-1).
        """
        path = self._store_path(store_name)
        if not path.exists():
            return []
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="replace")
        if "�" in text:
            log.warning(
                "tiger-memory: %s store file %s had non-UTF8 byte(s) replaced; "
                "affected block(s) will be skipped, good siblings still load.",
                store_name,
                path,
            )
        out: list[BaseEntry] = []
        for block in _split_blocks(text):
            fm, body = frontmatter.parse(block)
            if not fm:
                continue
            try:
                entry = entry_from_frontmatter(store_name, fm, body.rstrip("\n"))
                self._validate_entry(entry)
            except EntryError as exc:
                log.warning(
                    "tiger-memory: skipping corrupt %s entry id=%r: %s",
                    store_name,
                    fm.get("id"),
                    exc,
                )
                continue
            out.append(entry)
        return out

    def save_atomic(
        self, store_name: str, entries: Sequence[BaseEntry]
    ) -> None:
        """Atomically rewrite *store_name* with *entries* (temp + ``os.replace``).

        Every entry is validated before any byte is written, so a malformed
        entry aborts the whole save (no partial / corrupt store). Uses
        ``atomic_write`` (write-tmp, fsync, replace).
        """
        path = self._store_path(store_name)
        for entry in entries:
            if entry.store_name != store_name:
                raise EntryError(
                    f"entry {entry.id!r} belongs to store "
                    f"{entry.store_name!r}, not {store_name!r}."
                )
            self._validate_entry(entry)
        self.store.paths.journal.mkdir(parents=True, exist_ok=True)
        self.store.atomic_write(path, _serialize(entries))

    def _validate_entry(self, entry: BaseEntry) -> None:
        entry.validate()

    # ----- measurement (design §8: characters, never tokens) ------------

    def length_chars(self, entries: Iterable[BaseEntry]) -> int:
        """Total length of *entries* in CHARACTERS (design §8).

        The sum of each entry's rendered text + prose fields. Purely
        vendor-neutral character length — never tokens.
        """
        return sum(_entry_chars(e) for e in entries)

    def count(self, entries: Iterable[BaseEntry]) -> int:
        """Number of entries."""
        return sum(1 for _ in entries)

    def index_chars(
        self, store_name: str, entries: Sequence[BaseEntry]
    ) -> int:
        """Rendered-index length for the skills/topics stores (ADR 0007).

        The index is the only session-start load surface for these stores,
        so it is what the ``index_max_length`` bound measures. Raises
        ``EntryError`` for must_remember, which has no index.
        """
        if store_name == STORE_SKILLS:
            return len(indexes.render_skill_index(list(entries)))  # type: ignore[arg-type]
        if store_name == STORE_TOPICS:
            return len(indexes.render_topic_index(list(entries)))  # type: ignore[arg-type]
        raise EntryError(f"store {store_name!r} has no rendered index.")

    def detail_chars(self, entry: BaseEntry) -> int:
        """Rendered detail-file length for one skill/topic entry (ADR 0007)."""
        if isinstance(entry, SkillEntry):
            return len(indexes.render_skill_detail(entry))
        if isinstance(entry, TopicEntry):
            return len(indexes.render_topic_detail(entry))
        raise EntryError(
            f"store {entry.store_name!r} entries have no detail file."
        )

    def is_over_overflow(
        self, store_name: str, entries: Sequence[BaseEntry]
    ) -> bool:
        """True iff *store_name* is AT OR ABOVE its ``overflow_limit``.

        The hysteresis trigger (design §4): compaction fires only here, never
        inside ``max <= n < overflow_limit`` (no-thrash). Skills and topics
        measure their rendered index; must_remember measures entry length.
        """
        if store_name == STORE_SKILLS:
            return (
                self.index_chars(store_name, entries)
                >= self.memory.skills.index_overflow_limit
            )
        if store_name == STORE_TOPICS:
            return (
                self.index_chars(store_name, entries)
                >= self.memory.topics.index_overflow_limit
            )
        return (
            self.length_chars(entries)
            >= self.memory.must_remember.overflow_limit
        )

    def is_detail_over_overflow(self, entry: BaseEntry) -> bool:
        """True iff one skill/topic detail file is at/above its overflow bound."""
        if isinstance(entry, SkillEntry):
            limit = self.memory.skills.detail_overflow_limit
        else:
            limit = self.memory.topics.detail_overflow_limit
        return self.detail_chars(entry) >= limit

    def max_bound(self, store_name: str) -> int:
        """The ``max`` target a compaction shrinks back below (design §4)."""
        if store_name == STORE_SKILLS:
            return self.memory.skills.index_max_length
        if store_name == STORE_TOPICS:
            return self.memory.topics.index_max_length
        return self.memory.must_remember.max_length

    def detail_max_bound(self, entry: BaseEntry) -> int:
        """The per-entry detail ``max`` a compaction rewrites back below."""
        if isinstance(entry, SkillEntry):
            return self.memory.skills.detail_max_length
        if isinstance(entry, TopicEntry):
            return self.memory.topics.detail_max_length
        raise EntryError(
            f"store {entry.store_name!r} entries have no detail file."
        )

    # ----- per-store lock (design §5) -----------------------------------

    @contextmanager
    def store_lock(self, store_name: str) -> Iterator[None]:
        """Exclusive per-store file lock for the duration of the block.

        Two sessions must not meditate the same store concurrently (design
        §5). Acquires a PID-stamped O_EXCL lockfile in the journal dir; a
        stale lock (holder PID is dead) is reclaimed. If a *live* holder
        owns it, raises :class:`StoreLockHeld` so the caller backs off
        (meditation is not urgent — the next session-start retries).

        The lock is released (file removed) on exit only if we acquired it.
        """
        path = self._lock_path(store_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        acquired = self._acquire_store_lock(path)
        if not acquired:
            raise StoreLockHeld(
                f"store {store_name!r} is locked by another live session; "
                "skip meditation and retry next session-start."
            )
        try:
            yield
        finally:
            try:
                path.unlink()
            except FileNotFoundError:  # pragma: no cover - best-effort cleanup
                pass

    def _acquire_store_lock(self, path: Path) -> bool:
        """Try to take *path* as an O_EXCL lock; reclaim a dead holder."""
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(f"{os.getpid()} {time.time():.0f}")
            return True
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
        # Lock exists: reclaim only if the holder PID is dead.
        try:
            holder_pid = int(path.read_text().split()[0])
        except (ValueError, OSError, IndexError):
            holder_pid = -1
        if holder_pid > 0 and _pid_alive(holder_pid):
            return False
        try:
            path.unlink()
        except FileNotFoundError:  # pragma: no cover - raced release
            pass
        # Retry once after reclaiming the dead/garbage lock.
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(f"{os.getpid()} {time.time():.0f}")
            return True
        except OSError as exc:  # pragma: no cover - lost a tight race
            if exc.errno != errno.EEXIST:
                raise
            return False

    # ----- forget-guard (design §5; the no-safety-net anchor) -----------

    def forget(
        self,
        store_name: str,
        entries: Sequence[BaseEntry],
        drop_ids: Iterable[str],
        *,
        relevance_checked_ids: Iterable[str] = (),
    ) -> list[BaseEntry]:
        """Return *entries* minus *drop_ids*, guarding operator directives.

        The no-safety-net invariant (design §5): an ``operator_explicit``
        must-remember entry may be forgotten ONLY after it has passed this
        meditation cycle's relevance-check — i.e. its id is in
        *relevance_checked_ids*. Asking to drop a still-protected operator
        directive raises :class:`ForgetGuardError` rather than losing it.

        ``drop_ids`` referencing entries not present is a no-op for those
        ids (idempotent re-forget is safe). Order of survivors is preserved.
        """
        drop = set(drop_ids)
        checked = set(relevance_checked_ids)
        by_id = {e.id: e for e in entries}
        for did in drop:
            entry = by_id.get(did)
            if entry is None:
                continue
            if (
                store_name == STORE_MUST_REMEMBER
                and isinstance(entry, MustRememberEntry)
                and entry.kind == KIND_OPERATOR_EXPLICIT
                and did not in checked
            ):
                raise ForgetGuardError(
                    f"refusing to forget operator_explicit directive {did!r}: "
                    "it has not passed this cycle's relevance-check "
                    "(design §5 forget-order invariant)."
                )
        return [e for e in entries if e.id not in drop]


# ----- module-level serialization helpers ----------------------------------


def _serialize(entries: Sequence[BaseEntry]) -> str:
    """Render *entries* as one store file (frontmatter blocks + separators)."""
    if not entries:
        # An empty store is a valid, parseable, zero-block file.
        return ""
    blocks = []
    for entry in entries:
        body = entry.text if entry.text.endswith("\n") else entry.text + "\n"
        blocks.append(frontmatter.render(entry.frontmatter(), body))
    return (f"\n{_ENTRY_SEP}\n").join(blocks)


def _split_blocks(text: str) -> list[str]:
    """Split a store file back into per-entry blocks (inverse of _serialize).

    Each block is lstripped of the separator's surrounding newlines so the
    frontmatter ``---`` delimiter lands on the block's first line (which
    ``frontmatter.parse`` requires).
    """
    if not text.strip():
        return []
    return [b.lstrip("\n") for b in text.split(_ENTRY_SEP)]


def _entry_chars(entry: BaseEntry) -> int:
    """Character length contributed by one entry (design §8).

    Counts the body text plus the prose-bearing structured fields so the
    bound reflects what a persona actually reads, not just the body. Purely
    character-based — never tokens.
    """
    total = len(entry.text)
    fm = entry.frontmatter()
    for key in ("name", "trigger", "procedure", "summary"):
        val = fm.get(key)
        if isinstance(val, str):
            total += len(val)
    return total

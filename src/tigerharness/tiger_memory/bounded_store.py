"""Bounded-store substrate for the memory revamp (design §4, §5; plan §1+§2).

This is the durable layer the scoring (Rukawa) and glue (Miyagi) seats build
on. It owns, for the three bounded stores (``skills`` / ``must_remember`` /
``emotional``):

- **persistence** — ``load`` / ``save_atomic`` over a list of typed entries
  (``entries.py``), crash-safe via temp-file + ``os.replace``;
- **measurement** — ``length_chars`` (CHARACTERS, never tokens, design §8)
  and ``is_over_overflow`` (the hysteresis trigger, design §4);
- **concurrency** — ``store_lock(store_name)``: a per-store file lock so two
  sessions cannot meditate the same store at once (design §5 invariant);
- **the forget-guard** — ``forget()`` REFUSES to drop an ``owner_explicit``
  must-remember entry that has not yet passed this cycle's relevance-check
  (design §5 invariant; the no-safety-net correctness anchor, plan §2.4).

On-disk layout: one markdown file per store under the store's journal dir
(``skills.md`` / ``must_remember.md`` / ``emotional.md``). Each file is a
sequence of entries, every entry a YAML-frontmatter block (structured
fields) followed by its body text, blocks separated by a sentinel line. The
whole file is rewritten atomically on every ``save_atomic`` — meditation
operates on the entire store at once, so a whole-file swap is both correct
and the simplest thing that is crash-safe.

The bound is read per store from the ``memory:`` config block: skills are
**count-based** (``max_count`` / ``overflow_limit``); must_remember and
emotional are **length-based** in characters (``max_length`` /
``overflow_limit``). ``is_over_overflow`` returns True only at/above
``overflow_limit`` — never inside the ``max <= n < overflow_limit``
hysteresis band (design §4 no-thrash; plan §4 Kogure R1#2).
"""
from __future__ import annotations

import errno
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from . import frontmatter
from .config import Config
from .entries import (
    KIND_OWNER_EXPLICIT,
    STORE_NAMES,
    STORE_SKILLS,
    BaseEntry,
    EntryError,
    MustRememberEntry,
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

    The no-safety-net anchor (design §5): an ``owner_explicit`` must-remember
    entry MUST pass this meditation cycle's relevance-check before it can be
    forgotten. Asking to drop one that has not is a correctness bug in the
    caller, so we raise loudly rather than silently lose an owner directive.
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
        block. Blocks with no parseable frontmatter are skipped (forward/
        backward tolerant), matching the rest of the module's lenient reads.
        A block whose frontmatter parses but whose fields are corrupt (e.g. a
        bad numeric type) raises ``EntryError`` from ``entry_from_frontmatter``;
        that single block is skipped and logged so its good siblings still
        load — a corrupt entry must never take the whole store down with it.
        """
        path = self._store_path(store_name)
        if not path.exists():
            return []
        text = path.read_text(encoding="utf-8")
        out: list[BaseEntry] = []
        for block in _split_blocks(text):
            fm, body = frontmatter.parse(block)
            if not fm:
                continue
            try:
                out.append(
                    entry_from_frontmatter(store_name, fm, body.rstrip("\n"))
                )
            except EntryError as exc:
                log.warning(
                    "tiger-memory: skipping corrupt %s entry id=%r: %s",
                    store_name,
                    fm.get("id"),
                    exc,
                )
        return out

    def save_atomic(
        self, store_name: str, entries: Sequence[BaseEntry]
    ) -> None:
        """Atomically rewrite *store_name* with *entries* (temp + ``os.replace``).

        Every entry is validated before any byte is written, so a malformed
        entry aborts the whole save (no partial / corrupt store). Uses the
        base store's ``atomic_write`` (write-tmp, fsync, ``os.replace``).
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
        # The emotional store's cap is config-driven; everything else
        # validates with no argument.
        if entry.store_name == "emotional":
            entry.validate(weight_cap=self.memory.emotional_log.weight_cap)
        else:
            entry.validate()

    # ----- measurement (design §8: characters, never tokens) ------------

    def length_chars(self, entries: Iterable[BaseEntry]) -> int:
        """Total length of *entries* in CHARACTERS (design §8).

        Counts the rendered text of each entry — body plus the structured
        fields that contribute prose (the persona never reads token counts;
        the bound is purely vendor-neutral character length).
        """
        return sum(_entry_chars(e) for e in entries)

    def count(self, entries: Iterable[BaseEntry]) -> int:
        """Number of entries (the skills store is count-bounded)."""
        return sum(1 for _ in entries)

    def is_over_overflow(
        self, store_name: str, entries: Sequence[BaseEntry]
    ) -> bool:
        """True iff *store_name* is AT OR ABOVE its ``overflow_limit``.

        The hysteresis trigger (design §4): meditation fires only here, never
        inside ``max <= n < overflow_limit`` (no-thrash, plan §4 Kogure R1#2).
        Skills use count; the length-based stores use character length.
        """
        if store_name == STORE_SKILLS:
            return self.count(entries) >= self.memory.skills.overflow_limit
        if store_name == "must_remember":
            limit = self.memory.must_remember.overflow_limit
        else:  # emotional
            limit = self.memory.emotional_log.overflow_limit
        return self.length_chars(entries) >= limit

    def max_bound(self, store_name: str) -> int:
        """The ``max`` target a meditation compacts back below (design §4)."""
        if store_name == STORE_SKILLS:
            return self.memory.skills.max_count
        if store_name == "must_remember":
            return self.memory.must_remember.max_length
        return self.memory.emotional_log.max_length

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
        """Return *entries* minus *drop_ids*, guarding owner directives.

        The no-safety-net invariant (design §5): an ``owner_explicit``
        must-remember entry may be forgotten ONLY after it has passed this
        meditation cycle's relevance-check — i.e. its id is in
        *relevance_checked_ids*. Asking to drop a still-protected owner
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
                store_name == "must_remember"
                and isinstance(entry, MustRememberEntry)
                and entry.kind == KIND_OWNER_EXPLICIT
                and did not in checked
            ):
                raise ForgetGuardError(
                    f"refusing to forget owner_explicit directive {did!r}: "
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
    for key in ("name", "trigger", "procedure", "reaction"):
        val = fm.get(key)
        if isinstance(val, str):
            total += len(val)
    return total

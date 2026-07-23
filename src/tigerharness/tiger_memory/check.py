"""Format-check + repair for the three memory stores (``tiger-memory check``).

A pure-Python, ZERO-model-call gate that validates the three bounded stores
(``skills`` / ``must_remember`` / ``topics``) against their expected on-disk
shape and, with ``--fix``, repairs **mechanical** drift while
**quarantining** anything it cannot mechanically fix — so the live store is
left valid with no silent data loss:

- every store is a frontmatter entry file; each block is parsed + validated
  via :func:`entry_from_frontmatter`; a block that fails is a problem.

``--fix`` repair:
- mechanical drift -> rewrite the store canonically (re-serialize the good
  entries: whitespace, formatting, trailing newline);
- non-mechanical drift -> move the bad block(s) to a ``<store>.rejected.md``
  sidecar (append, logged) and rewrite the store with only the good entries.

The verb runs as the per-persona gate at the end of the sweep and in CI /
pre-commit, so malformed memory can neither persist nor land in the repo.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from . import frontmatter
from .bounded_store import BoundedStore, _serialize, _split_blocks
from .config import Config
from .entries import (
    STORE_NAMES,
    BaseEntry,
    EntryError,
    entry_from_frontmatter,
)
from .store import Store

log = logging.getLogger("tigerharness.tiger_memory.check")


@dataclass
class StoreCheck:
    """Per-store check outcome."""

    store_name: str
    valid: int = 0
    problems: list[str] = field(default_factory=list)
    quarantined: int = 0
    repaired: bool = False

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass
class CheckReport:
    """The whole-persona check outcome (all three stores)."""

    stores: list[StoreCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.stores)


def _rejected_path(path: Path) -> Path:
    """The quarantine sidecar for a store file (``topics.md`` -> ``topics.rejected.md``)."""
    return path.with_name(path.stem + ".rejected.md")


def _quarantine(path: Path, items: list[str]) -> None:
    """Append the bad *items* (verbatim) to the store's ``.rejected.md`` sidecar."""
    sidecar = _rejected_path(path)
    existing = sidecar.read_text(encoding="utf-8") if sidecar.exists() else ""
    sidecar.write_text(existing + "\n".join(items) + "\n", encoding="utf-8")
    log.warning(
        "tiger-memory check: quarantined %d malformed item(s) to %s",
        len(items), sidecar,
    )


def _check_frontmatter(
    bstore: BoundedStore, store_name: str, path: Path, text: str, *, fix: bool
) -> StoreCheck:
    res = StoreCheck(store_name)
    good: list[BaseEntry] = []
    bad_blocks: list[str] = []
    for block in _split_blocks(text):
        fm, body = frontmatter.parse(block)
        if not fm:
            if block.strip():
                res.problems.append("unparseable block (no frontmatter)")
                bad_blocks.append(block.strip())
            continue
        try:
            entry = entry_from_frontmatter(store_name, fm, body.rstrip("\n"))
            bstore._validate_entry(entry)
        except EntryError as exc:
            res.problems.append(f"invalid {store_name} entry id={fm.get('id')!r}: {exc}")
            bad_blocks.append(block.strip())
            continue
        good.append(entry)
    res.valid = len(good)
    # a parseable store whose canonical re-render differs is mechanical drift.
    if not res.problems and _serialize(good) != text:
        res.problems = [f"{store_name} not in canonical format"]
    if fix and not res.ok:
        if bad_blocks:
            _quarantine(path, bad_blocks)
            res.quarantined = len(bad_blocks)
        bstore.save_atomic(store_name, good)
        res.repaired = True
    return res


def check_store(bstore: BoundedStore, store_name: str, *, fix: bool) -> StoreCheck:
    """Validate (and optionally repair) one store."""
    path = bstore._store_path(store_name)
    if not path.exists():
        return StoreCheck(store_name)
    text = path.read_bytes().decode("utf-8", errors="replace")
    return _check_frontmatter(bstore, store_name, path, text, fix=fix)


def check_all(cfg: Config, store: Store, *, fix: bool = False) -> CheckReport:
    """Check (and optionally ``--fix``) all three stores for one persona."""
    bstore = BoundedStore(cfg, store)
    report = CheckReport()
    for name in STORE_NAMES:
        report.stores.append(check_store(bstore, name, fix=fix))
    return report

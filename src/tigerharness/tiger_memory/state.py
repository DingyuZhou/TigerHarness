"""``tiger-memory state`` payload computation (bounded-store revamp).

Reads the on-disk store and returns a JSON-serialisable snapshot of the three
bounded stores (``skills`` / ``must_remember`` / ``diary``): per-store
entry counts, character length, and whether each is at/over its overflow
limit (the meditation trigger). The retired rollup/archive/``longer_memory``
counters are gone (design §3).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .bounded_store import BoundedStore
from .config import Config
from .entries import STORE_NAMES, STORE_SKILLS
from .store import Store

log = logging.getLogger("tigerharness.tiger_memory.state")


def compute_state(cfg: Config, store: Store) -> dict[str, Any]:
    """The ``tiger-memory state`` JSON snapshot for the three bounded stores."""
    bstore = BoundedStore(cfg, store)
    stores: dict[str, Any] = {}
    for name in STORE_NAMES:
        entries = bstore.load(name)
        count = bstore.count(entries)
        chars = bstore.length_chars(entries)
        stores[name] = {
            "count": count,
            "chars": chars,
            "max": bstore.max_bound(name),
            "over_overflow": bstore.is_over_overflow(name, entries),
            # Skills are count-bounded; the length-based stores compact on chars.
            "bound_unit": "count" if name == STORE_SKILLS else "characters",
        }
    saved = store.read_state() or {}
    return {
        "agent": cfg.agent.name,
        "operator_id": saved.get("operator_id"),
        "last_rebuild_at": saved.get("last_rebuild_at"),
        "lock": _lock_payload(cfg.rebuild.lock_path),
        "stores": stores,
    }


def _lock_payload(lock_path: Path) -> dict[str, Any]:
    if not lock_path.exists():
        return {"held": False, "pid": None}
    try:
        pid = int(lock_path.read_text().strip())
    except (ValueError, OSError):
        pid = None
    return {"held": True, "pid": pid}


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

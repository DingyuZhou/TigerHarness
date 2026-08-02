"""``tiger-memory state`` payload computation (topic-store revamp, ADR 0007).

Reads the on-disk store and returns a JSON-serialisable snapshot of the three
bounded stores (``skills`` / ``must_remember`` / ``topics``): per-store
entry counts, character length (index length for skills/topics — the
session-start load surface), whether each is at/over its overflow limit
(the compaction trigger), and how many detail files are over their own
bound.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .bounded_store import BoundedStore
from .config import Config
from .entries import STORE_MUST_REMEMBER, STORE_NAMES
from .store import Store

log = logging.getLogger("tigerharness.tiger_memory.state")


def compute_state(cfg: Config, store: Store) -> dict[str, Any]:
    """The ``tiger-memory state`` JSON snapshot for the three bounded stores."""
    bstore = BoundedStore(cfg, store)
    stores: dict[str, Any] = {}
    for name in STORE_NAMES:
        entries = bstore.load(name)
        count = bstore.count(entries)
        if name == STORE_MUST_REMEMBER:
            chars = bstore.length_chars(entries)
        else:
            chars = bstore.index_chars(name, entries)
        payload: dict[str, Any] = {
            "count": count,
            "chars": chars,
            "max": bstore.max_bound(name),
            "over_overflow": bstore.is_over_overflow(name, entries),
            "bound_unit": "characters",
        }
        if name != STORE_MUST_REMEMBER:
            payload["details_over_overflow"] = sum(
                1 for e in entries if bstore.is_detail_over_overflow(e)
            )
        stores[name] = payload
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

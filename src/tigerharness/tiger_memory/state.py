"""``tiger-memory state`` payload computation.

Reads the on-disk state and returns the JSON-serialisable snapshot
documented in design doc §3.5.
"""
from __future__ import annotations

import logging

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .store import DAILY_RE, MONTHLY_RE, SHORT_RE, WEEKLY_RE, Store
from . import frontmatter

log = logging.getLogger("tigerharness.tiger_memory.state")


def compute_state(cfg: Config, store: Store) -> dict[str, Any]:
    counts = _count_store(store)
    saved = store.read_state() or {}

    longer = _read_longer_memory(store)

    must_memorize_rows = _count_must_memorize_rows(store)

    lock_payload = _lock_payload(cfg.rebuild.lock_path)

    return {
        "agent": cfg.agent.name,
        "last_rebuild_at": saved.get("last_rebuild_at"),
        "last_op": saved.get("last_op"),
        "last_rebuild_duration_sec": saved.get("last_rebuild_duration_sec"),
        "lock": lock_payload,
        "sessions": saved.get(
            "sessions",
            {"active": 0, "clean": 0, "dirty": 0, "frozen": 0},
        ),
        "store_counts": {
            **counts,
            "must_memorize_rows": must_memorize_rows,
        },
        "longer_memory": longer,
        "cost": {
            "last_rebuild_usd": saved.get("last_rebuild_cost_usd"),
            "last_bootstrap_usd": saved.get("last_bootstrap_cost_usd"),
            "total_usd_since_bootstrap": saved.get("total_cost_usd"),
        },
    }


def _count_store(store: Store) -> dict[str, int]:
    archive = sum(1 for _ in store.paths.archive.glob("*.md"))
    shorts = sum(
        1 for f in store.paths.journal.glob("*.md") if SHORT_RE.match(f.name)
    )
    dailies = sum(
        1 for f in store.paths.journal.glob("*.md") if DAILY_RE.match(f.name)
    )
    weeklies = sum(
        1 for f in store.paths.journal.glob("*.md") if WEEKLY_RE.match(f.name)
    )
    monthlies = sum(
        1 for f in store.paths.journal.glob("*.md") if MONTHLY_RE.match(f.name)
    )
    return {
        "archive": archive,
        "shorts": shorts,
        "dailies": dailies,
        "weeklies": weeklies,
        "monthlies": monthlies,
    }


def _read_longer_memory(store: Store) -> dict[str, Any]:
    p = store.paths.journal / "longer_memory.md"
    if not p.exists():
        return {"covers_until": None, "last_refreshed_at": None}
    fm = frontmatter.read_frontmatter(p)
    return {
        "covers_until": fm.get("covers_until"),
        "last_refreshed_at": fm.get("last_refreshed_at"),
    }


def _count_must_memorize_rows(store: Store) -> int:
    p = store.paths.journal / "must_memorize.md"
    if not p.exists():
        return 0
    text = p.read_text(encoding="utf-8")
    rows = 0
    in_table = False
    for line in text.splitlines():
        if line.lstrip().startswith("|"):
            if not in_table:
                in_table = True
                # header — count if it's a real row, but skip headers/separators
                continue
            stripped = line.strip()
            if stripped.startswith("|---") or "---" in stripped.replace("|", "").strip()[:3]:
                continue
            # Heuristic: separator rows are like |---|---| (all dashes/pipes).
            no_pipes = stripped.replace("|", "").strip()
            if no_pipes and all(c in "- :" for c in no_pipes):
                continue
            rows += 1
        else:
            in_table = False
    # We over-counted the header skip above when the table starts; recompute.
    # Simpler approach: count non-empty, non-separator rows minus 1 (header).
    return max(0, rows - 1) if rows else 0


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

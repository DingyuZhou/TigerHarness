"""B3 team-sweep gating (non-AI bookkeeping).

The B1/B3 in-session memory rebuild is triggered at **persona-session
bootstrap, shared by all personas** (see ``docs/tiger-memory-rework.md``,
"B3 — implementation design"). This module is the *gating* layer that
decides whether THIS session should run the team sweep — the staleness
floor + a soft-lease claim, coordinated through one team-scoped state
file. No AI, no model spend: exactly the ``journal sweep`` mold.

The roster walk and the sub-agent summarization executor build on top
(later slices); this module deliberately knows nothing about either.

State lives in the team's memory root — the parent of every persona's
store dir (``<team>/memories/``). A persona config's ``store.root``
resolves to ``<team>/memories/<persona>/``, so callers pass
``cfg.store.root.parent`` as ``team_memories_dir``; it is the same path
for every persona on the team, which is what makes the claim team-scoped.

**Soft lease, by design.** The claim is a read-check-write on the state
file (atomic tmp+replace), not a hard OS lock. Two sessions that trigger
in the very same instant could both claim — but the staleness floor +
``last_sweep_at`` watermark make simultaneous first-triggers rare, the
downstream per-store lock serialises writes, and per-persona atomic
commits bound any redundant work. A hard ``O_EXCL`` lock can harden this
later if contention is ever observed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SWEEP_STATE_FILENAME = ".tiger-memory-sweep.json"
DEFAULT_STALENESS_FLOOR_HOURS = 24.0
# How long a claim is honoured before a later session may steal it (a
# crashed claimant). Mirrors the drive-journal stuck-timeout default.
DEFAULT_LEASE_SECONDS = 1800.0


@dataclass(frozen=True)
class ClaimResult:
    claimed: bool
    reason: str  # "claimed" | "not_due" | "busy"


# ----- state file IO -------------------------------------------------------


def sweep_state_path(team_memories_dir: Path) -> Path:
    return Path(team_memories_dir) / SWEEP_STATE_FILENAME


def read_sweep_state(team_memories_dir: Path) -> dict:
    """Tolerant read of the team sweep-state file. ``{}`` on any problem."""
    try:
        data = json.loads(sweep_state_path(team_memories_dir).read_text(
            encoding="utf-8"
        ))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_sweep_state(team_memories_dir: Path, state: dict) -> None:
    path = sweep_state_path(team_memories_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)  # atomic on POSIX


# ----- staleness floor -----------------------------------------------------


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def sweep_due(
    last_sweep_at: str | None,
    now: datetime,
    *,
    floor_hours: float = DEFAULT_STALENESS_FLOOR_HOURS,
) -> bool:
    """True iff a sweep is due: never swept, an unparseable watermark, or
    at least *floor_hours* elapsed since *last_sweep_at*."""
    t = _parse_iso(last_sweep_at)
    if t is None:
        return True
    return (now - t).total_seconds() >= floor_hours * 3600


# ----- soft-lease claim ----------------------------------------------------


def try_claim_sweep(
    team_memories_dir: Path,
    *,
    now: datetime,
    token: str,
    floor_hours: float = DEFAULT_STALENESS_FLOOR_HOURS,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> ClaimResult:
    """Decide whether THIS session runs the team sweep.

    1. Staleness floor: if the last completed sweep is recent, return
       ``not_due`` — the cheap common case (every trigger inside the floor
       window is a no-op).
    2. Soft-lease claim: if another session holds a *fresh* claim (within
       *lease_seconds*), return ``busy``; otherwise stamp our token +
       timestamp into the state file and return ``claimed``. A stale claim
       (crashed owner) is stolen; re-claiming our own token is allowed.
    """
    state = read_sweep_state(team_memories_dir)
    if not sweep_due(state.get("last_sweep_at"), now, floor_hours=floor_hours):
        return ClaimResult(False, "not_due")

    claim_at = _parse_iso(state.get("claim_at"))
    held_by_other = (
        claim_at is not None
        and (now - claim_at).total_seconds() < lease_seconds
        and state.get("claim_token") != token
    )
    if held_by_other:
        return ClaimResult(False, "busy")

    state["claim_token"] = token
    state["claim_at"] = now.isoformat()
    write_sweep_state(team_memories_dir, state)
    return ClaimResult(True, "claimed")


def mark_sweep_complete(team_memories_dir: Path, now: datetime) -> None:
    """Advance the team watermark and release the claim. The bumped
    ``last_sweep_at`` is what makes the next trigger inside the floor
    window a cheap no-op."""
    state = read_sweep_state(team_memories_dir)
    state["last_sweep_at"] = now.isoformat()
    state.pop("claim_token", None)
    state.pop("claim_at", None)
    write_sweep_state(team_memories_dir, state)

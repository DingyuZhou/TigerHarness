"""B3 team-sweep gating (non-AI bookkeeping).

The B1/B3 in-session memory rebuild is triggered at **persona-session
bootstrap, shared by all personas** (see ``docs/history/tiger-memory-rework.md``,
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

import logging

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

log = logging.getLogger("tigerharness.tiger_memory.sweep")

SWEEP_STATE_FILENAME = ".tiger-memory-sweep.json"
DEFAULT_STALENESS_FLOOR_HOURS = 24.0
# How long a claim is honoured before a later session may steal it (a
# crashed claimant). Mirrors the drive-journal stuck-timeout default.
DEFAULT_LEASE_SECONDS = 1800.0
# Per-wake cap on how many stale personas one trigger processes, so a big
# backlog spreads across several session-bootstrap wakes instead of making
# one user wait. Pass ``max_personas=None`` for unbounded (all pending).
DEFAULT_MAX_PERSONAS = 3


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
    # Unique tmp name per writer: a FIXED tmp name lets two concurrent
    # writers truncate each other's in-progress bytes and publish an
    # interleaved file via os.replace (audit F2/F6).
    tmp = path.with_name(
        f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    )
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
        log.info("team sweep: not_due (inside staleness floor)")
        return ClaimResult(False, "not_due")

    claim_at = _parse_iso(state.get("claim_at"))
    held_by_other = (
        claim_at is not None
        and (now - claim_at).total_seconds() < lease_seconds
        and state.get("claim_token") != token
    )
    if held_by_other:
        log.info("team sweep: busy (live claim holds the lease)")
        return ClaimResult(False, "busy")

    # Bound a sweep RUN to the staleness floor. A run spans several wakes
    # (per-wake cap → release → resume), and `progress` carries the
    # already-done personas across them. But if the run is abandoned
    # mid-sweep (team silent past the floor), its stale `progress` must
    # NOT skip personas in the next window — that would leave a persona
    # swept long ago unrefreshed ("no persona left behind", B3). So a
    # claim whose in-flight run is older than the floor starts a FRESH
    # run: clear `progress` + restamp `run_started_at`.
    run_started = _parse_iso(state.get("run_started_at"))
    run_abandoned = (
        run_started is not None
        and (now - run_started).total_seconds() >= floor_hours * 3600
    )
    if run_started is None or run_abandoned:
        state["run_started_at"] = now.isoformat()
        state.pop("progress", None)

    state["claim_token"] = token
    state["claim_at"] = now.isoformat()
    write_sweep_state(team_memories_dir, state)
    log.info("team sweep: claimed")
    return ClaimResult(True, "claimed")


def _token_mismatch(state: dict, token: str | None, verb: str) -> bool:
    """True when *token* was given and does NOT match the stored claim.

    A driver whose lease was stolen mid-run must not mutate the live
    owner's claim/progress (audit F6): passing its token makes the
    mutation conditional. ``token=None`` keeps the legacy unconditional
    behavior (manual operator use).
    """
    if token is None:
        return False
    held = state.get("claim_token")
    if held == token:
        return False
    log.warning(
        "team sweep: %s refused — claim token mismatch "
        "(yours %s, current %s); another session owns the sweep",
        verb, token, held,
    )
    return True


def release_sweep_claim(
    team_memories_dir: Path, *, token: str | None = None
) -> bool:
    """Drop the claim WITHOUT advancing the watermark — used when a wake
    finished its per-wake chunk but the roster sweep isn't complete. The
    in-run ``progress`` is preserved so the next wake resumes the rest,
    and the watermark stays stale so the sweep is still "due".

    With *token*, the release is refused (``False``) unless it matches
    the stored claim — a stale driver must not clear the live owner's
    claim."""
    state = read_sweep_state(team_memories_dir)
    if _token_mismatch(state, token, "sweep-release"):
        return False
    state.pop("claim_token", None)
    state.pop("claim_at", None)
    write_sweep_state(team_memories_dir, state)
    return True


def mark_sweep_complete(
    team_memories_dir: Path, now: datetime, *, token: str | None = None,
    force: bool = False,
) -> bool:
    """Advance the team watermark and end the run: clear the claim AND the
    per-persona progress. The bumped ``last_sweep_at`` is what makes the
    next trigger inside the floor window a cheap no-op.

    With *token*, refused (``False``) on a claim mismatch — a stale
    driver advancing the watermark would skip the live run's remaining
    personas for a whole floor window. Also refused when roster personas
    are still PENDING (not in ``progress``) unless *force* — completing
    early parks the skipped personas behind the 24h floor, which is
    exactly how the live roster starvation stayed invisible (practicality
    audit S4). The per-persona ``done_at`` freshness map is kept — it
    orders future roster walks."""
    state = read_sweep_state(team_memories_dir)
    if _token_mismatch(state, token, "sweep-complete"):
        return False
    if not force:
        done = sweep_progress(team_memories_dir)
        pending = [
            t.name for t in enumerate_persona_configs(team_memories_dir)
            if t.name not in done
        ]
        if pending:
            log.warning(
                "team sweep: sweep-complete refused — %d persona(s) not "
                "recorded done this run (%s). Finish them (sweep-done) or "
                "release the claim; pass force to complete anyway.",
                len(pending), ", ".join(pending),
            )
            return False
    state["last_sweep_at"] = now.isoformat()
    state.pop("claim_token", None)
    state.pop("claim_at", None)
    state.pop("progress", None)
    state.pop("run_started_at", None)
    write_sweep_state(team_memories_dir, state)
    return True


# ----- roster walk + per-persona resumable progress (B3 slice b) -----------


@dataclass(frozen=True)
class PersonaTarget:
    name: str
    config_path: Path


@dataclass(frozen=True)
class SweepPlan:
    targets: list[PersonaTarget]  # to process THIS wake (capped, not-yet-done)
    remaining: int                # personas still pending after the cap
    all_personas: int             # personas with a memory store on the roster


def enumerate_persona_configs(team_memories_dir: Path) -> list[PersonaTarget]:
    """Roster personas that have a tiger-memory store, in roster order.

    Reads ``<team>/configs/personas.yaml`` (``team_memories_dir.parent``)
    and keeps each persona whose
    ``<team>/memories/<name>/tiger-memory.config.yaml`` exists. Tolerant:
    a missing/malformed roster yields ``[]`` so the bootstrap hook never
    crashes a persona session over a roster read.
    """
    team_memories_dir = Path(team_memories_dir)
    roster_path = team_memories_dir.parent / "configs" / "personas.yaml"
    try:
        data = yaml.safe_load(roster_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    personas = data.get("personas") if isinstance(data, dict) else None
    if not isinstance(personas, list):
        return []
    out: list[PersonaTarget] = []
    for entry in personas:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        cfg_path = team_memories_dir / name / "tiger-memory.config.yaml"
        if cfg_path.exists():
            out.append(PersonaTarget(name=name, config_path=cfg_path))
    return out


def sweep_progress(team_memories_dir: Path) -> set[str]:
    """Persona names already completed in the in-flight sweep run."""
    prog = read_sweep_state(team_memories_dir).get("progress") or []
    return {str(p) for p in prog} if isinstance(prog, list) else set()


def persona_done_at(team_memories_dir: Path) -> dict[str, str]:
    """Per-persona last-completed timestamps (survives run resets).

    Unlike ``progress`` (in-run bookkeeping, cleared on completion or an
    abandoned-run reset), ``done_at`` is durable freshness memory: it
    orders the roster walk least-recently-swept first, so a team whose
    wakes never finish a full run still rotates through every persona
    instead of re-sweeping the same head of the roster forever (audit:
    live roster starvation — 6 personas unswept while the first 3
    repeated).
    """
    raw = read_sweep_state(team_memories_dir).get("done_at")
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def record_persona_done(
    team_memories_dir: Path, persona: str, *, now: datetime | None = None
) -> None:
    """Mark *persona* completed in the current run (idempotent).

    Also refreshes the claim lease (``claim_at``): a healthy multi-persona
    run can legitimately exceed the 30-min lease, and without renewal a
    later session would steal the claim from a LIVE driver mid-run (audit
    F1). And it stamps the durable ``done_at`` freshness map used to
    order future roster walks.
    """
    now = now or datetime.now(timezone.utc)
    state = read_sweep_state(team_memories_dir)
    prog = state.get("progress")
    prog = list(prog) if isinstance(prog, list) else []
    if persona not in prog:
        prog.append(persona)
    state["progress"] = prog
    done_at = state.get("done_at")
    done_at = dict(done_at) if isinstance(done_at, dict) else {}
    done_at[persona] = now.isoformat()
    state["done_at"] = done_at
    if state.get("claim_token"):
        # Lease renewal: progress IS liveness.
        state["claim_at"] = now.isoformat()
    write_sweep_state(team_memories_dir, state)


def plan_team_sweep(
    team_memories_dir: Path,
    *,
    max_personas: int | None = DEFAULT_MAX_PERSONAS,
) -> SweepPlan:
    """Sequence the roster for THIS wake: skip personas already done in the
    in-flight run, then take at most *max_personas* of the rest
    (default ``DEFAULT_MAX_PERSONAS``; ``None`` for unbounded).

    Pure sequencing (non-AI): the per-persona rebuild — a no-op when the
    persona has no new sessions — runs in the executor (slice c).
    """
    done = sweep_progress(team_memories_dir)
    all_targets = enumerate_persona_configs(team_memories_dir)
    pending = [t for t in all_targets if t.name not in done]
    # Least-recently-completed first (durable done_at map; a persona never
    # completed sorts oldest). With fixed roster order, a team whose runs
    # keep getting reset re-sweeps the same first N personas forever and
    # starves the tail; LRU ordering guarantees rotation ("no persona
    # left behind", B3). Ties keep roster order (sorted() is stable).
    done_at = persona_done_at(team_memories_dir)
    pending.sort(key=lambda t: done_at.get(t.name, ""))
    selected = pending if max_personas is None else pending[:max_personas]
    return SweepPlan(
        targets=selected,
        remaining=len(pending) - len(selected),
        all_personas=len(all_targets),
    )


@dataclass(frozen=True)
class SweepDecision:
    ran: bool
    reason: str             # "claimed" | "not_due" | "busy"
    plan: SweepPlan | None  # the roster targets to process when ran


def maybe_sweep_roster(
    team_memories_dir: Path,
    *,
    now: datetime,
    token: str,
    floor_hours: float = DEFAULT_STALENESS_FLOOR_HOURS,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    max_personas: int | None = DEFAULT_MAX_PERSONAS,
) -> SweepDecision:
    """The shared persona-session-bootstrap hook (B3). Tries to claim the
    team sweep; on success returns the roster `plan` for the caller to
    execute (per-persona plan → sub-agent → finalize, then
    `record_persona_done` and finally `mark_sweep_complete` /
    `release_sweep_claim`). On `not_due` / `busy` it is a cheap no-op.

    Pure gating + sequencing — no AI. The caller owns execution so this
    stays vendor-neutral and unit-testable.
    """
    claim = try_claim_sweep(
        team_memories_dir, now=now, token=token,
        floor_hours=floor_hours, lease_seconds=lease_seconds,
    )
    if not claim.claimed:
        return SweepDecision(ran=False, reason=claim.reason, plan=None)
    plan = plan_team_sweep(team_memories_dir, max_personas=max_personas)
    return SweepDecision(ran=True, reason="claimed", plan=plan)

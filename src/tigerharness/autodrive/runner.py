"""Testable core of ``tigerharness autodrive``.

The autodrive runs a journal *drive* on a fixed interval by spawning an
agentic backend (default ``claude_p``) with a self-contained,
Operator-authorized "drive the journal" prompt. Everything that touches
the model, the clock, or the loop is funnelled through this module behind
dependency-injection seams so the CLI layer stays thin and the whole core
is unit-testable without spawning a real subprocess.

WHY THIS EXISTS AT ALL (read before extending)
-----------------------------------------------
The journal is a *human-triggered* subscription backend: "no programmatic
driver by design". This module is the deliberate, Operator-authorized
exception. It is only safe to run while ``claude -p`` bills the Claude
subscription rather than API tokens (the reason the Slack-drive ban was
lifted in the same period). If Anthropic flips ``claude -p`` to API
billing, an unattended autodrive bills real dollars on every tick --
that is what ``max_budget_usd`` and the ``autodrive stop`` off-switch
guard against. Keep those guardrails loud.

Loop shape: **drive, then sleep** (no overlap). A drive that hits its
budget cap or context ceiling returns a non-terminal ``stop_reason``; the
journal task simply stays ``in_progress``/idle and the next tick resumes
it -- truncation is safe because the journal's session model is
resumable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)


# ----- constants -----

#: Minimum allowed interval. A single drive already takes minutes and the
#: loop never overlaps drives, so a floor mainly stops a typo (``--interval
#: 1``) from hammering the backend the instant a drive returns.
MIN_INTERVAL_SECONDS = 60.0

#: Default per-tick prompt is built from :func:`default_prompt`; the
#: permission mode defaults to ``bypassPermissions`` because the driver
#: runs unattended and must never stall on a permission prompt.
DEFAULT_PERMISSION_MODE = "bypassPermissions"
DEFAULT_BACKEND = "claude_p"


# ----- config -----

@dataclass(frozen=True)
class AutodriveConfig:
    """Static, per-run configuration for one autodrive daemon.

    The live progress fields (pid, tick_count, last_tick_at, ...) live in
    the on-disk state file, not here -- this is only the part that decides
    how each tick behaves.
    """

    interval_seconds: float
    driver: str | None
    backend: str
    model: str | None
    max_budget_usd: float | None
    permission_mode: str
    prompt: str
    cwd: str


def default_prompt(driver: str | None) -> str:
    """The self-contained instruction handed to each drive.

    It must *override* the drive-journal skill's "never drive from claude
    -p / cron / API" boundary, because that is exactly what this process
    is -- but an Operator-authorized one. Spelling that out in the prompt
    is what keeps the spawned agent from (correctly, per its skill)
    refusing to drive.
    """
    if driver:
        claim = (
            f"Claim each task with `--driver {driver} --allow-api-drive` so "
            f"the work is attributed to {driver}'s memory store"
        )
    else:
        claim = "Claim each task with `--allow-api-drive`"
    return (
        "You are an Operator-authorized automatic journal driver "
        "(`tigerharness autodrive`). This is a SANCTIONED programmatic "
        "drive: the usual 'never drive from claude -p / cron / API' "
        "boundary is deliberately lifted for THIS process by explicit "
        "Operator authorization. Drive the journal now using the "
        "drive-journal skill -- sweep, pick exactly one actionable task, "
        f"work it, and cascade through the queue. {claim} (the "
        "`--allow-api-drive` flag is harmless when no Slack thread marker "
        "is set). If nothing is actionable, stop cleanly without making "
        "changes."
    )


def config_to_dict(cfg: AutodriveConfig) -> dict[str, Any]:
    """The serializable, state-file projection of a config."""
    return {
        "interval_seconds": cfg.interval_seconds,
        "driver": cfg.driver,
        "backend": cfg.backend,
        "model": cfg.model,
        "max_budget_usd": cfg.max_budget_usd,
        "permission_mode": cfg.permission_mode,
        "prompt": cfg.prompt,
        "cwd": cfg.cwd,
    }


def config_from_state(state: dict[str, Any]) -> AutodriveConfig:
    """Rebuild a config from a state-file dict (the child ``_loop`` path)."""
    return AutodriveConfig(
        interval_seconds=float(state["interval_seconds"]),
        driver=state.get("driver"),
        backend=state.get("backend", DEFAULT_BACKEND),
        model=state.get("model"),
        max_budget_usd=state.get("max_budget_usd"),
        permission_mode=state.get("permission_mode", DEFAULT_PERMISSION_MODE),
        prompt=state["prompt"],
        cwd=state.get("cwd", "."),
    )


# ----- clock -----

def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 ``...Z`` string. Injectable so
    tests get deterministic timestamps."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----- state file I/O -----

STATE_FILE_NAME = ".autodrive.json"
LOG_FILE_NAME = ".autodrive.log"


def state_path(journal_root: Path) -> Path:
    """Where a journal's autodrive state file lives (sibling of
    ``.drive-sessions.json`` under the journal root)."""
    return journal_root / STATE_FILE_NAME


def log_path(journal_root: Path) -> Path:
    return journal_root / LOG_FILE_NAME


def read_state(path: Path) -> dict[str, Any] | None:
    """Parse the state file, or ``None`` if it is missing or corrupt.

    A corrupt file is treated as absent rather than fatal: a half-written
    JSON (e.g. a crash mid-write) should look like "no daemon" so a fresh
    ``start`` can recover, not wedge every command.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def write_state(path: Path, state: dict[str, Any]) -> None:
    """Atomically write the state file (tmp + ``os.replace``) so a reader
    never sees a half-written JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


def clear_state(path: Path) -> None:
    """Remove the state file if present. Idempotent."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def record_tick(
    path: Path,
    *,
    tick_count: int,
    at: str,
    stop_reason: str | None = None,
    cost_usd: float | None = None,
    error: str | None = None,
) -> None:
    """Read-modify-write the live progress fields after a drive.

    Read-modify-write (not overwrite) so the pid + static config the
    parent wrote survive each tick update. If the file vanished (a
    concurrent ``stop``), there is nothing to update -- skip silently.
    """
    state = read_state(path)
    if state is None:
        return
    state["tick_count"] = tick_count
    state["last_tick_at"] = at
    state["last_stop_reason"] = stop_reason
    state["last_cost_usd"] = cost_usd
    state["last_error"] = error
    write_state(path, state)


# ----- process liveness -----

def pid_alive(pid: int) -> bool:
    """True if ``pid`` names a live process. ``os.kill(pid, 0)`` is the
    portable POSIX liveness probe: signal 0 performs error checking
    without delivering a signal."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - alive but not ours
        return True
    return True


def is_running(
    path: Path, *, alive: Callable[[int], bool] = pid_alive
) -> tuple[bool, dict[str, Any] | None]:
    """``(running, state)``. ``running`` is True only when the state file
    records an integer pid that is currently alive. ``alive`` is injected
    so tests need not spawn real processes."""
    state = read_state(path)
    if state is None:
        return False, None
    pid = state.get("pid")
    if isinstance(pid, int) and alive(pid):
        return True, state
    return False, state


# ----- the drive + the loop -----

async def run_one_drive(
    cfg: AutodriveConfig,
    *,
    backend: Any = None,
) -> Any:
    """Run exactly one journal drive through the agent SDK and return its
    ``RunResult``.

    Vendor-agnostic on purpose: the backend is resolved by name via
    :func:`tigerharness.agent_sdk.get_backend` and given a plain
    ``AgentConfig``. We do NOT pass ``cwd`` to the backend constructor
    (not every backend accepts one) -- instead the child ``_loop`` process
    runs *in* ``cfg.cwd`` (the team root), and an agentic backend like
    ``claude_p`` inherits that as its subprocess working directory. That
    is what lets the spawned drive resolve the team's own journal.
    """
    from ..agent_sdk import AgentConfig, get_backend

    if backend is None:
        backend = get_backend(cfg.backend)

    extra: dict[str, Any] = {"permission_mode": cfg.permission_mode}
    if cfg.max_budget_usd is not None:
        extra["max_budget_usd"] = cfg.max_budget_usd

    agent_cfg = AgentConfig(
        name="autodrive",
        model=cfg.model,
        extra=extra,
    )
    return await backend.run(agent_cfg, cfg.prompt)


DriveFn = Callable[..., Awaitable[Any]]
SleepFn = Callable[[float], Awaitable[Any]]
StopFn = Callable[[], bool]


async def run_loop(
    cfg: AutodriveConfig,
    state_file: Path,
    *,
    backend: Any = None,
    sleep: SleepFn = asyncio.sleep,
    max_ticks: int | None = None,
    should_stop: StopFn | None = None,
    now: Callable[[], str] = utcnow_iso,
    run_drive: DriveFn = run_one_drive,
) -> int:
    """Drive-then-sleep until stopped. Returns the number of drives run.

    Stops when ``should_stop()`` is true, or after ``max_ticks`` drives
    (both injectable for tests; in production neither is set and the loop
    runs until the process is killed by ``autodrive stop``).

    A drive that raises is recorded as ``last_error`` and the loop
    continues -- one bad tick must not take the daemon down; the next
    interval tries again.
    """
    n = 0
    while True:
        if max_ticks is not None and n >= max_ticks:
            break
        if should_stop is not None and should_stop():
            break

        try:
            result = await run_drive(cfg, backend=backend)
            n += 1
            stop_reason = getattr(result, "stop_reason", None)
            record_tick(
                state_file,
                tick_count=n,
                at=now(),
                stop_reason=stop_reason,
                cost_usd=getattr(result, "cost_usd", None),
                error=None,
            )
            log.info("autodrive tick %d done (stop_reason=%s)", n, stop_reason)
        except Exception as exc:  # keep the daemon alive across failures
            n += 1
            log.warning("autodrive tick %d failed: %s", n, exc, exc_info=True)
            record_tick(
                state_file,
                tick_count=n,
                at=now(),
                error=f"{type(exc).__name__}: {exc}",
            )

        if max_ticks is not None and n >= max_ticks:
            break
        if should_stop is not None and should_stop():
            break
        await sleep(cfg.interval_seconds)
    return n


def clamp_interval(interval: float) -> float:
    """Return ``interval`` if it meets the floor, else raise ``ValueError``
    with an actionable message. Kept as a function so both the CLI and any
    future caller share one rule."""
    if interval < MIN_INTERVAL_SECONDS:
        raise ValueError(
            f"--interval must be >= {int(MIN_INTERVAL_SECONDS)}s "
            f"(got {interval}); a journal drive already takes minutes."
        )
    return interval


def with_prompt(cfg: AutodriveConfig, prompt: str) -> AutodriveConfig:
    """Return a copy of ``cfg`` with a replaced prompt (small helper used
    by the CLI when the operator supplies ``--prompt``)."""
    return replace(cfg, prompt=prompt)

"""Per-iteration stuck-watchdog for the task runner.

The watchdog first sleeps for ``stuck_timeout`` seconds. After that it
runs a check loop: gather a diagnostic snapshot of the iteration's
``claude`` subprocess tree, decide STUCK / WORKING / UNCLEAR via a
deterministic heuristic, and (for UNCLEAR) optionally consult an agent.

On a STUCK verdict it escalates: SIGTERMs the whole subtree, waits a
short grace, SIGKILLs survivors, posts a Slack DM, and lets the
runner's exception path handle the rest.

On WORKING (or UNCLEAR with no agent / agent timeout) it waits
``recheck_sec`` and tries again. Loops indefinitely until either the
dispatch completes (``stop_event`` set) or a STUCK verdict is reached.

The heuristic + /proc walking is stdlib-only (no psutil). Linux-only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .notifier import notify_stuck_escalation
from .registry import JobMeta


log = logging.getLogger("tigerharness.task_runner.stuck_watchdog")


# SIGTERM -> SIGKILL grace window when escalating.
STUCK_SIGTERM_GRACE_SEC = 5.0

# Recheck cadence after the first stuck-timeout fire.
STUCK_RECHECK_SEC = 600  # 10 min

# CPU-sample window used inside gather_diagnostic.
HEURISTIC_CPU_SAMPLE_SEC = 2.0

# Heuristic thresholds for the oldest direct ``bash`` child of claude.
HEURISTIC_BASH_STUCK_AGE_SEC = 600   # > 10 min alive -> STUCK regardless of CPU
HEURISTIC_BASH_FRESH_AGE_SEC = 60    # < 1 min alive -> WORKING (fresh tool call)


# ---------------------------------------------------------------------------
# /proc helpers -- stdlib only, Linux-specific
# ---------------------------------------------------------------------------

def _read_btime() -> int | None:
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("btime "):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return None


_BTIME = _read_btime()
_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def proc_comm(pid: int) -> str:
    """Process command name (kernel-truncated to 15 chars). Empty if gone."""
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except OSError:
        return ""


def proc_children(pid: int) -> list[int]:
    """Direct children of ``pid``. Empty if unreadable / no children.

    Reads ``/proc/<pid>/task/<pid>/children`` (Linux 3.5+).
    """
    try:
        with open(f"/proc/{pid}/task/{pid}/children") as f:
            return [int(c) for c in f.read().split()]
    except (OSError, ValueError):
        return []


def _read_stat_fields(pid: int) -> list[str] | None:
    """Return the post-comm fields of ``/proc/<pid>/stat``, or None.

    ``comm`` is enclosed in parens and may itself contain spaces/parens,
    so we find the LAST ``)`` to safely skip it before splitting.
    """
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
    except OSError:
        return None
    rp = data.rfind(")")
    if rp < 0:
        return None
    return data[rp + 2:].split()


def proc_start_time(pid: int) -> float | None:
    """Unix timestamp of when ``pid`` started, or None if unreadable."""
    if _BTIME is None:
        return None
    fields = _read_stat_fields(pid)
    if fields is None or len(fields) < 20:
        return None
    try:
        starttime_jiffies = int(fields[19])
    except ValueError:
        return None
    return _BTIME + starttime_jiffies / _CLK_TCK


def proc_state(pid: int) -> str:
    """Single-letter state from ``/proc/<pid>/stat`` (R, S, D, Z, T, ...). '' if gone."""
    fields = _read_stat_fields(pid)
    if not fields:
        return ""
    return fields[0]


def proc_cpu_ticks(pid: int) -> int:
    """``utime + stime`` (jiffies) for ``pid`` alone (excludes descendants)."""
    fields = _read_stat_fields(pid)
    if fields is None or len(fields) < 13:
        return 0
    try:
        return int(fields[11]) + int(fields[12])
    except ValueError:
        return 0


def subtree_cpu_ticks(pid: int) -> int:
    """``utime + stime`` summed over ``pid`` and all descendants."""
    return proc_cpu_ticks(pid) + sum(
        proc_cpu_ticks(p) for p in descendants_of(pid)
    )


def find_job_claude_pid(runner_pid: int) -> int | None:
    """The runner's claude subprocess. Direct children only.

    Sub-claudes spawned via the agent's Agent tool live deeper in the
    tree and are intentionally NOT matched -- those are legitimate work.
    """
    for child in proc_children(runner_pid):
        if proc_comm(child) == "claude":
            return child
    return None


def descendants_of(pid: int) -> list[int]:
    """All descendant pids, breadth-first. Empty if no children."""
    seen: list[int] = []
    stack = list(proc_children(pid))
    while stack:
        p = stack.pop(0)
        seen.append(p)
        stack.extend(proc_children(p))
    return seen


def sigterm_subtree(pid: int) -> list[int]:
    """SIGTERM the subtree rooted at ``pid``. Returns target pids (incl. pid).

    Sent bottom-up so a shell can't reap children mid-walk.
    """
    targets = [pid] + descendants_of(pid)
    for p in reversed(targets):
        try:
            os.kill(p, signal.SIGTERM)
        except ProcessLookupError:
            pass
    return targets


def sigkill_pids(pids: list[int]) -> None:
    """SIGKILL each pid in ``pids`` (post-SIGTERM stragglers)."""
    for p in reversed(pids):
        try:
            os.kill(p, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def kill_subtree_async(
    pid: int,
    *,
    grace_sec: float = STUCK_SIGTERM_GRACE_SEC,
) -> list[int]:
    """SIGTERM subtree, ``await`` grace, then SIGKILL stragglers."""
    targets = sigterm_subtree(pid)
    await asyncio.sleep(grace_sec)
    sigkill_pids(targets)
    return targets


# ---------------------------------------------------------------------------
# Diagnostic snapshot
# ---------------------------------------------------------------------------

@dataclass
class ProcSample:
    pid: int
    comm: str
    state: str            # one letter from /proc/PID/stat: R/S/D/Z/T/...
    age_sec: float | None
    cpu_ticks: int        # utime + stime for THIS pid only


@dataclass
class Diagnostic:
    """Result of two sampled snapshots of claude's tree, taken
    ``sample_window_sec`` apart. ``bash_children`` is the second-sample
    view of claude's direct bash children. ``bash_subtree_cpu_delta``
    keys are pids present in BOTH samples (no delta for pids that
    appeared or vanished during the window)."""
    claude: ProcSample
    bash_children: list[ProcSample] = field(default_factory=list)
    bash_subtree_cpu_delta: dict[int, int] = field(default_factory=dict)
    sample_window_sec: float = HEURISTIC_CPU_SAMPLE_SEC


def _proc_sample(pid: int) -> ProcSample:
    start = proc_start_time(pid)
    return ProcSample(
        pid=pid,
        comm=proc_comm(pid),
        state=proc_state(pid),
        age_sec=(time.time() - start) if start is not None else None,
        cpu_ticks=proc_cpu_ticks(pid),
    )


async def gather_diagnostic(
    claude_pid: int,
    *,
    sample_window_sec: float = HEURISTIC_CPU_SAMPLE_SEC,
) -> Diagnostic:
    """Take two snapshots of claude + its bash-subtree CPU, one window apart.

    Async because the inter-sample wait uses ``asyncio.sleep`` to avoid
    blocking the event loop (the dispatch task is awaiting in parallel).
    """
    children_t1 = proc_children(claude_pid)
    cpu_t1 = {c: subtree_cpu_ticks(c) for c in children_t1}

    await asyncio.sleep(sample_window_sec)

    children_t2 = proc_children(claude_pid)
    cpu_t2 = {c: subtree_cpu_ticks(c) for c in children_t2}

    # Only deltas for children present in BOTH snapshots.
    common = set(cpu_t1) & set(cpu_t2)
    delta = {c: cpu_t2[c] - cpu_t1[c] for c in common}

    bash_children = [_proc_sample(c) for c in children_t2 if proc_comm(c) == "bash"]

    return Diagnostic(
        claude=_proc_sample(claude_pid),
        bash_children=bash_children,
        bash_subtree_cpu_delta=delta,
        sample_window_sec=sample_window_sec,
    )


# ---------------------------------------------------------------------------
# Heuristic verdict
# ---------------------------------------------------------------------------

Verdict = str  # "STUCK" | "WORKING" | "UNCLEAR"
VerdictResult = tuple[Verdict, str]  # (verdict, reason)


def heuristic_verdict(d: Diagnostic) -> VerdictResult:
    """Deterministic call on a sampled diagnostic. Pure function."""
    if not d.bash_children:
        return ("UNCLEAR", "claude has no bash children "
                           "(thinking, streaming, or wedged -- can't tell)")

    aged = [c for c in d.bash_children if c.age_sec is not None]
    if not aged:
        return ("UNCLEAR", "couldn't read bash child start times")

    oldest = max(aged, key=lambda c: c.age_sec or 0.0)
    age = oldest.age_sec or 0.0

    if age > HEURISTIC_BASH_STUCK_AGE_SEC:
        return ("STUCK", f"bash pid={oldest.pid} alive {int(age)}s "
                         f"> {HEURISTIC_BASH_STUCK_AGE_SEC}s threshold")

    if age < HEURISTIC_BASH_FRESH_AGE_SEC:
        return ("WORKING", f"oldest bash pid={oldest.pid} only {int(age)}s old "
                           f"(likely a fresh tool call)")

    delta = d.bash_subtree_cpu_delta.get(oldest.pid, 0)
    if delta == 0:
        return ("STUCK", f"bash pid={oldest.pid} subtree CPU flat over "
                         f"{d.sample_window_sec}s (age {int(age)}s)")

    return ("WORKING", f"bash pid={oldest.pid} subtree CPU advancing "
                       f"(+{delta} ticks over {d.sample_window_sec}s)")


def render_diagnostic(d: Diagnostic) -> str:
    """Format a diagnostic as a markdown blob for the agent prompt."""
    age_str = (
        f"{int(d.claude.age_sec)}s" if d.claude.age_sec is not None else "unknown"
    )
    lines = [
        f"## claude pid={d.claude.pid}",
        f"- state: {d.claude.state}  (R=running, S=sleep, D=disk-wait, Z=zombie)",
        f"- age: {age_str}",
        f"- own cumulative CPU: {d.claude.cpu_ticks} jiffies "
        f"(~{d.claude.cpu_ticks / _CLK_TCK:.1f}s)",
        "",
        f"## Direct `bash` children of claude ({len(d.bash_children)})",
    ]
    if not d.bash_children:
        lines.append("(none -- claude has no bash-tool subprocesses running)")
    else:
        for c in d.bash_children:
            delta = d.bash_subtree_cpu_delta.get(c.pid, 0)
            age = f"{int(c.age_sec)}s" if c.age_sec is not None else "?"
            lines.append(
                f"- pid={c.pid}  state={c.state}  age={age}  "
                f"subtree_cpu_delta_over_{d.sample_window_sec}s={delta} ticks"
            )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Watchdog loop
# ---------------------------------------------------------------------------

AgentCheckFn = Callable[[str], Awaitable[VerdictResult]]


def _log_event(log_path: Path, payload: dict[str, Any]) -> None:
    """Append a stuck-watchdog event to the run.log."""
    payload.setdefault("t", time.time())
    payload.setdefault("kind", "stuck")
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, default=str) + "\n")
    except OSError:
        pass


async def _wait_or_done(stop_event: asyncio.Event, timeout: float) -> bool:
    """Returns True if stop_event fired during the wait; False on timeout."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


async def _escalate_and_kill(
    *,
    log_path: Path,
    meta: JobMeta,
    iter_num: int,
    reason: str,
    runner_pid: int,
    sigterm_grace_sec: float,
    escalation_signal: asyncio.Event | None = None,
    is_last_iter: bool = False,
) -> None:
    """Common tail for STUCK verdicts: log, find claude, kill, notify.

    The order is deliberate: locate claude FIRST; only if a real claude
    subprocess exists do we set ``escalation_signal``, kill the subtree,
    and post the Slack DM. If claude already exited (the iteration was
    finishing naturally on its own), we log ``escalate_no_claude`` and
    return without affecting the dispatch -- preventing a spurious
    ``StuckWatchdogEscalation`` for an iter that actually succeeded.

    ``is_last_iter`` only affects the DM wording (continuing vs. ending);
    the actual continue-or-end decision is made in the runner.
    """
    _log_event(log_path, {
        "event": "escalate",
        "iter": iter_num,
        "reason": reason,
    })

    claude_pid = find_job_claude_pid(runner_pid)
    if claude_pid is None:
        # Dispatch is wrapping up naturally -- don't poison the result.
        _log_event(log_path, {"event": "escalate_no_claude", "iter": iter_num})
        return

    # We're committed to killing this iter. Signal the wrapper (so the
    # raised dispatch error gets converted to StuckWatchdogEscalation)
    # and stash the reason for the wrapper to surface in meta.error.
    #
    # NOTE on the .reason attribute hack: ``asyncio.Event`` doesn't have
    # a ``.reason`` field, but Python lets us tack one on dynamically.
    # The dispatch wrapper in runner.py reads it via ``getattr(signal,
    # "reason", "")``. This convention is intentional -- refactoring to
    # a typed container would be ~30 lines of churn for one call site.
    # If this expands to multiple writers or readers, promote to a real
    # dataclass.
    if escalation_signal is not None:
        escalation_signal.reason = reason  # type: ignore[attr-defined]
        escalation_signal.set()

    # Notify Slack. Done AFTER claude is located but BEFORE the kill
    # finishes -- the DM goes out promptly so the user sees it ASAP.
    detail = f"stuck verdict: {reason}"
    if is_last_iter:
        detail += " -- cancelling iter (last in run)"
    else:
        detail += " -- cancelling iter, continuing with next iteration"
    try:
        notify_stuck_escalation(
            meta, iter_num=iter_num, detail=detail,
        )
    except Exception:
        log.exception("notify_stuck_escalation raised; ignored")

    # Shield the kill so SIGKILL still happens if the wrapper cancels us
    # mid-grace (dispatch returns quickly after SIGTERM lands).
    try:
        await asyncio.shield(
            kill_subtree_async(claude_pid, grace_sec=sigterm_grace_sec)
        )
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("kill_subtree_async on claude_pid=%d failed", claude_pid)


async def stuck_watchdog(
    *,
    stop_event: asyncio.Event,
    log_path: Path,
    meta: JobMeta,
    iter_num: int,
    stuck_timeout_sec: int,
    agent_check_fn: AgentCheckFn | None = None,
    escalation_signal: asyncio.Event | None = None,
    is_last_iter: bool = False,
    # Test/override knobs.
    recheck_sec: int = STUCK_RECHECK_SEC,
    sample_window_sec: float = HEURISTIC_CPU_SAMPLE_SEC,
    sigterm_grace_sec: float = STUCK_SIGTERM_GRACE_SEC,
    runner_pid: int | None = None,
) -> None:
    """Run the hybrid stuck-watchdog for one iteration.

    The caller starts this as a task alongside the dispatch task. When
    the dispatch completes, the caller sets ``stop_event``; the watchdog
    exits cleanly at any wait point.

    Flow:
      1. Wait ``stuck_timeout_sec``. If dispatch completes first -> return.
      2. Check loop, every ``recheck_sec``:
         a. gather_diagnostic(claude) -- sample twice, ``sample_window_sec`` apart.
         b. heuristic_verdict(d).
         c. If UNCLEAR and ``agent_check_fn`` provided: ask the agent
            (timeout / parse failure -> treat as WORKING).
         d. STUCK -> escalate (log, Slack, SIGTERM/SIGKILL) and return.
            WORKING -> wait recheck_sec, loop.
    """
    if stuck_timeout_sec <= 0:
        return

    if await _wait_or_done(stop_event, stuck_timeout_sec):
        return  # dispatch finished before first check

    pid = runner_pid if runner_pid is not None else os.getpid()
    _log_event(log_path, {
        "event": "watchdog_fired",
        "iter": iter_num,
        "stuck_timeout_sec": stuck_timeout_sec,
    })

    while not stop_event.is_set():
        claude_pid = find_job_claude_pid(pid)
        if claude_pid is None:
            # Dispatch is wrapping up on its own.
            _log_event(log_path, {"event": "check_no_claude", "iter": iter_num})
            return

        try:
            diag = await gather_diagnostic(
                claude_pid, sample_window_sec=sample_window_sec,
            )
        except Exception:
            log.exception("gather_diagnostic failed")
            _log_event(log_path, {
                "event": "check_error",
                "iter": iter_num,
                "stage": "gather",
            })
            if await _wait_or_done(stop_event, recheck_sec):
                return
            continue

        verdict, reason = heuristic_verdict(diag)
        _log_event(log_path, {
            "event": "check_heuristic",
            "iter": iter_num,
            "verdict": verdict,
            "reason": reason,
        })

        if verdict == "UNCLEAR":
            if agent_check_fn is None:
                # Conservative fallback: keep waiting.
                verdict = "WORKING"
                reason = "heuristic UNCLEAR, no agent configured; treating as WORKING"
            else:
                diagnostic_text = render_diagnostic(diag)
                try:
                    agent_verdict, agent_reason = await agent_check_fn(diagnostic_text)
                    if agent_verdict not in ("STUCK", "WORKING"):
                        agent_verdict = "WORKING"
                        agent_reason = (
                            f"agent returned unknown verdict; treating as "
                            f"WORKING ({agent_reason})"
                        )
                    verdict = agent_verdict
                    reason = f"agent: {agent_reason}"
                    _log_event(log_path, {
                        "event": "check_agent",
                        "iter": iter_num,
                        "verdict": verdict,
                        "reason": reason,
                    })
                except Exception:
                    log.exception("agent_check_fn raised; treating as WORKING")
                    _log_event(log_path, {
                        "event": "check_agent_error",
                        "iter": iter_num,
                    })
                    verdict = "WORKING"
                    reason = "agent check failed; treating as WORKING"

        if verdict == "STUCK":
            await _escalate_and_kill(
                log_path=log_path,
                meta=meta,
                iter_num=iter_num,
                reason=reason,
                runner_pid=pid,
                sigterm_grace_sec=sigterm_grace_sec,
                escalation_signal=escalation_signal,
                is_last_iter=is_last_iter,
            )
            return

        # WORKING -- wait recheck_sec, loop.
        if await _wait_or_done(stop_event, recheck_sec):
            return

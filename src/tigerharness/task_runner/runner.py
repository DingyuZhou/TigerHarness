"""The iteration loop. Runs in a detached child process -- one per job.

Invoked from the CLI as:
    python -m tigerharness.task_runner _run <job_id>

Lifecycle
---------
1. Read JobMeta from registry by job_id. If missing -> exit 1.
2. Build the persona's AgentConfig (lazy -- file IO may fail here).
3. Status: pending -> running. Persist PID.
4. Loop `i = 1 .. cap`:
    a. Check cancel flag -> break with status=cancelled.
    b. Send the user message:
        - `i == 1` -> original prompt (from prompt.txt)
        - `i > 1`  -> continuation template
    c. Persist iter, session_id, last_update; append run.log entry;
       overwrite result.txt with `final_output`.
    d. If `compact_every > 0` and `i % compact_every == 0` and
       `i < cap`, send `"/compact"` as its own turn.
5. Status -> done | cancelled | error; clear PID.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import signal
import sys
import time
import traceback
from pathlib import Path
from types import FrameType

from tigerharness.agent_sdk import get_backend, run_with_retry
from tigerharness.agent_sdk.types import AgentConfig, Session

from .notifier import notify_job_end, notify_job_start
from .personas import resolve
from .registry import JobMeta, JobStore, default_state_path
from .stuck_watchdog import stuck_watchdog


log = logging.getLogger("tigerharness.task_runner.runner")


class StuckWatchdogEscalation(Exception):
    """Raised by ``_dispatch_turn`` when the stuck-watchdog killed the
    iteration's claude. The runner's main loop catches this and either
    continues with the next iteration (if any remain) or ends the job
    as ``error`` (if this was the final iteration). Distinct from a
    regular backend exception so we don't mistakenly cancel the whole
    job on a watchdog-triggered failure."""
    def __init__(self, iter_num: int, reason: str = "") -> None:
        super().__init__(
            f"stuck-watchdog escalated iter {iter_num}"
            + (f": {reason}" if reason else "")
        )
        self.iter_num = iter_num
        self.reason = reason


# ---------------------------------------------------------------------------
# Default prompt components
# ---------------------------------------------------------------------------

TASK_PREAMBLE = """\
## Task execution guidelines

These defaults apply unless the task instructions below explicitly override them.

### Iterative improvement
After each iteration, document:
- What you learned this iteration
- What could be stronger or done differently next iteration
- Key findings the next iteration should build on

### Self-critique between iterations
When continuing from a prior iteration, start by critically reviewing \
your previous work. Identify gaps, weaknesses, or missed angles before \
proceeding.

### Exhaustive exploration
Even if you believe the task is complete, push further: are there \
alternative approaches, unexplored angles, or deeper insights you \
haven't considered? Completeness beats speed.

### Watch your child processes
If you spawn subprocesses via the `Bash` tool (e.g. `pytest`, `npm`, \
training jobs, long-running scripts), periodically check on any that \
have been running more than 10 minutes. Use `ps -eo pid,ppid,etime,stat,pcpu,command` \
(or similar) to inspect them. Decide whether each is genuinely making \
progress (nonzero CPU, log output advancing, expected wall-clock for \
the workload) or wedged (zero CPU, no log output, frozen subtree).

If wedged:
1. Kill the subtree (e.g. `pkill -P <pid>` for direct children, or \
   `kill -- -<pgid>` for the whole process group).
2. Diagnose the root cause: deadlock, network hang, broken test, \
   missing input, OOM, blocked on stdin, etc.
3. Fix the underlying problem before any re-run.
4. Only after the fix is in, re-run.

Don't blindly retry -- that leaks process trees and burns your iteration \
budget on the same wedge.

### Autonomy under ambiguity
When you need clarification that isn't available, use your best \
judgment and make reasonable assumptions. Document every assumption \
clearly in your output so the CEO can review them later.

---

"""

CONTINUATION_PREAMBLE = """\
## Iteration guidelines

- Start by critiquing your previous iteration's work -- what could be \
stronger, what was missed?
- Even if prior iterations concluded the task is done, look for \
unexplored angles or deeper insights before declaring it finished.
- When you need clarification, use your best judgment and document \
your assumptions.
- Document what you learn and findings for the next iteration.
- Do not repeat work already completed.
- **Watch your child processes.** Any subprocess you spawned via the \
`Bash` tool that has been running more than 10 minutes -- check it with \
`ps -eo pid,ppid,etime,stat,pcpu,command`. If it shows zero CPU, no \
log progress, or otherwise looks wedged, kill the subtree, diagnose \
the root cause (deadlock, hung network call, broken test, missing \
input, OOM, ...), fix the underlying problem, and only then re-run. \
Don't blindly retry -- it leaks process trees and burns your iteration \
budget.

---

"""

CONTINUATION_DEFAULT = (
    CONTINUATION_PREAMBLE
    + "Continue the task. Pick the next concrete step and execute it."
)

COMPACT_TRIGGER = "/compact"

# Early-exit: 3 consecutive DONE or ERROR verdicts trigger early exit.
EARLY_EXIT_CONSECUTIVE = 3

_CLASSIFY_PROMPT_TEMPLATE = """\
Read this agent iteration output and classify it as exactly one of:
- DONE: the agent believes the task is fully complete and has no more work to do
- ERROR: the agent hit a blocker, error, or unresolvable problem
- CONTINUING: the agent is still making progress or found new angles to explore

Reply with exactly one word: DONE, ERROR, or CONTINUING. No explanation.

---
{snippet}
---"""

_CLASSIFY_CFG = AgentConfig(
    name="output-classifier",
    instructions="You are a classifier. Reply with exactly one word.",
    max_turns=1,
    extra={"permission_mode": "plan"},
)

_NOVELTY_PROMPT_TEMPLATE = """\
Compare these two consecutive iteration outputs from the same agent task.
Did the CURRENT iteration produce substantively new work compared to the
PREVIOUS iteration? "New work" means: new analysis, new code, new sections,
deeper reasoning, new data, new strategies explored, or meaningful revisions.
"Not new" means: the agent is just restating, summarizing, or polishing
what was already there with no substantive additions.

Reply with exactly one word: NEW or STALE. No explanation.

--- PREVIOUS ITERATION OUTPUT (first 3000 chars) ---
{prev_snippet}
--- END PREVIOUS ---

--- CURRENT ITERATION OUTPUT (first 3000 chars) ---
{curr_snippet}
--- END CURRENT ---"""


# Stuck-check agent: invoked by the stuck-watchdog when the deterministic
# heuristic returns UNCLEAR. Uses the SAME model as the iteration's agent
# (derived from agent_cfg via dataclasses.replace) -- the watchdog's
# verdict matters too much to downgrade to a cheaper model.
_STUCK_CHECK_PROMPT_TEMPLATE = """\
An autonomous code agent is running an iteration of work in its own
claude subprocess. A watchdog noticed the iteration exceeded its time
budget and sampled the process tree.

Decide whether the iteration looks STUCK (frozen, blocked, unrecoverable)
or WORKING (still actively making progress -- thinking, running a fresh
tool call, streaming output).

Hints:
- claude state R or D and CPU advancing -> likely WORKING.
- claude state S and CPU not advancing (and no bash children) -> could
  be waiting on network/stream input (often WORKING) but if it stays
  that way for many checks it may be wedged.
- A bash child older than 10 min is almost always STUCK; the heuristic
  already catches that, so you won't see those here.

Reply with EXACTLY one line in this format:
STUCK: <one-sentence reason>
or
WORKING: <one-sentence reason>

--- process tree snapshot ---
{diagnostic}
--- end snapshot ---
"""

# Hard timeout on the stuck-check agent call so a wedged check can't
# itself wedge the watchdog.
AGENT_STUCK_CHECK_TIMEOUT_SEC = 90



def _get_slack_thread_notice(thread_ts: str) -> str:
    """Build the Slack threading notice injected into prompts.

    Uses TIGERHARNESS_SLACK_BRIDGE_DIR env var for the notify path.
    Falls back to a generic `python -m tigerharness.slack_bridge.notify` command.
    """
    bridge_dir = os.environ.get("TIGERHARNESS_SLACK_BRIDGE_DIR", "").strip()
    if bridge_dir:
        notify_cmd = f"cd {bridge_dir}\nuv run python -m slack_bridge.notify"
    else:
        notify_cmd = "python -m tigerharness.slack_bridge.notify"

    return f"""
### Slack threading -- CRITICAL, applies to EVERY message

This task is anchored to Slack thread `{thread_ts}`. Every Slack DM you \
post -- status updates, questions, file uploads, completion notices -- \
MUST be threaded under that ts. The user's Slack inbox is one thread \
per task; top-level DMs are noise.

When calling the notify CLI, ALWAYS pass `--thread {thread_ts}`:

```
{notify_cmd} text "your message" --thread {thread_ts}
{notify_cmd} file --file /tmp/chart.png --comment "caption" --thread {thread_ts}
```

If you find yourself about to post a Slack DM without `--thread {thread_ts}`, \
STOP and add it. This reminder is re-issued every iteration because \
`/compact` can otherwise drop the thread_ts from your context.

"""


async def _dispatch_one(
    backend,
    agent_cfg,
    session: Session,
    prompt: str,
    log_path: Path,
    *,
    kind: str,
    iter_num: int,
    job_id: str,
) -> tuple[str, float | None]:
    """One backend turn + structured log entry.

    Uses `run_with_retry` (3 attempts, exponential backoff) to ride out
    transient backend errors.
    """
    started = time.time()
    try:
        result = await run_with_retry(
            backend, agent_cfg, prompt,
            session=session,
            max_attempts=3,
            label=f"job={job_id} iter={iter_num} kind={kind}",
        )
    except Exception as exc:
        _append_log(
            log_path,
            {
                "t": time.time(),
                "elapsed_s": round(time.time() - started, 2),
                "session": getattr(session, "id", ""),
                "iter": iter_num,
                "kind": kind,
                "error": repr(exc),
            },
        )
        raise

    text = (result.final_output or "") if isinstance(result.final_output, str) \
        else str(result.final_output or "")
    _append_log(
        log_path,
        {
            "t": time.time(),
            "elapsed_s": round(time.time() - started, 2),
            "session": session.id,
            "iter": iter_num,
            "kind": kind,
            "cost_usd": result.cost_usd,
            "chars_in": len(prompt),
            "chars_out": len(text),
            "stop_reason": getattr(result, "stop_reason", None),
            "preview": text[:200],
        },
    )
    return text, result.cost_usd


def _append_log(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


# ---------------------------------------------------------------------------
# Iteration log -- persistent, git-trackable markdown in the project dir
# ---------------------------------------------------------------------------

def _iter_log_path(meta) -> Path:
    """Compute the iteration log path: <cwd>/lab_notebooks/tasks/<slug>.md."""
    cwd = Path(meta.cwd)
    name = (meta.name or "").strip()
    if name:
        safe_name = name.replace("/", "-").replace(" ", "-").replace("\\", "-")
        slug = f"{safe_name}--{meta.job_id}"
    else:
        slug = meta.job_id
    return cwd / "lab_notebooks" / "tasks" / f"{slug}.md"


def _write_iter_header(path: Path, meta) -> None:
    """Write the iteration log header (once, at job start)."""
    import datetime
    started = datetime.datetime.fromtimestamp(
        meta.started_at, tz=datetime.timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")
    cap = str(meta.max_iters) if meta.max_iters > 0 else "forever"

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write(f"# Task: {meta.name or meta.job_id} (`{meta.job_id}`)\n\n")
        f.write("| Field | Value |\n|---|---|\n")
        f.write(f"| Persona | `{meta.persona}` |\n")
        f.write(f"| Started | {started} |\n")
        f.write(f"| Max iterations | {cap} |\n")
        f.write(f"| Working dir | `{meta.cwd}` |\n\n")


def _append_iteration(path: Path, iter_num: int, text: str) -> None:
    """Append one iteration's full output to the log. Best-effort."""
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(f"---\n\n## Iteration {iter_num}\n\n")
            f.write(text.rstrip())
            f.write("\n\n")
    except OSError:
        pass


def _append_runner_event(path: Path, message: str) -> None:
    """Append a runner-level event. Best-effort."""
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(f"---\n\n## [Runner] {message}\n\n")
    except OSError:
        pass


def _build_initial_prompt(user_prompt: str, *, thread_ts: str = "") -> str:
    """Prepend the task preamble (+ optional Slack threading notice) to
    the user's task prompt."""
    parts = [TASK_PREAMBLE]
    if thread_ts:
        parts.append(_get_slack_thread_notice(thread_ts))
    parts.append(user_prompt)
    return "".join(parts)


def _build_continuation(custom: str, *, thread_ts: str = "") -> str:
    """Build the continuation prompt."""
    base = (CONTINUATION_PREAMBLE + custom) if custom else CONTINUATION_DEFAULT
    if thread_ts:
        base += _get_slack_thread_notice(thread_ts)
    return base


async def _classify_output(
    backend,
    text: str,
    log_path: Path,
    *,
    job_id: str,
    iter_num: int,
) -> str:
    """Classify iteration output as DONE, ERROR, or CONTINUING."""
    snippet = text[:3000]
    prompt = _CLASSIFY_PROMPT_TEMPLATE.format(snippet=snippet)
    session = await backend.open_session()
    try:
        result = await run_with_retry(
            backend, _CLASSIFY_CFG, prompt,
            session=session,
            max_attempts=2,
            label=f"classify job={job_id} iter={iter_num}",
        )
        raw = (result.final_output or "").strip().upper()
        for word in raw.split():
            if word in ("DONE", "ERROR", "CONTINUING"):
                verdict = word
                break
        else:
            verdict = "CONTINUING"
        _append_log(
            log_path,
            {
                "t": time.time(),
                "kind": "classify",
                "iter": iter_num,
                "verdict": verdict,
                "raw": raw[:100],
                "cost_usd": result.cost_usd,
            },
        )
        return verdict
    except Exception as exc:
        log.warning(
            "classify failed for job=%s iter=%d: %r; defaulting to CONTINUING",
            job_id, iter_num, exc,
        )
        _append_log(
            log_path,
            {
                "t": time.time(),
                "kind": "classify",
                "iter": iter_num,
                "verdict": "CONTINUING",
                "error": repr(exc),
            },
        )
        return "CONTINUING"
    finally:
        try:
            await session.close()
        except Exception:
            pass


async def _classify_novelty(
    backend,
    prev_text: str,
    curr_text: str,
    log_path: Path,
    *,
    job_id: str,
    iter_num: int,
) -> str:
    """Compare two consecutive outputs for substantive novelty."""
    prev_snippet = prev_text[:3000]
    curr_snippet = curr_text[:3000]
    prompt = _NOVELTY_PROMPT_TEMPLATE.format(
        prev_snippet=prev_snippet, curr_snippet=curr_snippet,
    )
    session = await backend.open_session()
    try:
        result = await run_with_retry(
            backend, _CLASSIFY_CFG, prompt,
            session=session,
            max_attempts=2,
            label=f"novelty job={job_id} iter={iter_num}",
        )
        raw = (result.final_output or "").strip().upper()
        for word in raw.split():
            if word in ("NEW", "STALE"):
                verdict = word
                break
        else:
            verdict = "NEW"
        _append_log(
            log_path,
            {
                "t": time.time(),
                "kind": "novelty",
                "iter": iter_num,
                "verdict": verdict,
                "raw": raw[:100],
                "cost_usd": result.cost_usd,
            },
        )
        return verdict
    except Exception as exc:
        log.warning(
            "novelty classify failed for job=%s iter=%d: %r; defaulting to NEW",
            job_id, iter_num, exc,
        )
        _append_log(
            log_path,
            {
                "t": time.time(),
                "kind": "novelty",
                "iter": iter_num,
                "verdict": "NEW",
                "error": repr(exc),
            },
        )
        return "NEW"
    finally:
        try:
            await session.close()
        except Exception:
            pass


def _build_agent_stuck_check(backend, agent_cfg: AgentConfig, *, job_id: str):
    """Closure used by the stuck-watchdog for its UNCLEAR-verdict agent fallback.

    The check uses the *same model* as the iteration's agent -- the
    config is derived from ``agent_cfg`` via ``dataclasses.replace`` with
    ``name`` / ``instructions`` / ``max_turns`` overridden for the
    focused one-shot check. We force ``permission_mode=plan`` so the
    check can't issue tool calls (it's pure classification).

    Bounded by ``AGENT_STUCK_CHECK_TIMEOUT_SEC`` so the check itself
    can't wedge. Best-effort: timeouts / parse failures return
    ``("WORKING", ...)`` so we never escalate without confidence.
    """
    extra = dict(agent_cfg.extra or {})
    extra["permission_mode"] = "plan"
    stuck_cfg = dataclasses.replace(
        agent_cfg,
        name="stuck-checker",
        instructions=(
            "You are a process-tree analyzer. Reply with EXACTLY one line "
            "in the format 'STUCK: <reason>' or 'WORKING: <reason>'."
        ),
        max_turns=1,
        extra=extra,
    )

    async def _check(diagnostic: str) -> tuple[str, str]:
        session = await backend.open_session()
        try:
            prompt = _STUCK_CHECK_PROMPT_TEMPLATE.format(diagnostic=diagnostic)
            try:
                result = await asyncio.wait_for(
                    run_with_retry(
                        backend, stuck_cfg, prompt,
                        session=session, max_attempts=1,
                        label=f"stuck-check job={job_id}",
                    ),
                    timeout=AGENT_STUCK_CHECK_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                return ("WORKING", "agent check timed out -- treating as WORKING")
            raw = (result.final_output or "").strip()
            first_line = raw.splitlines()[0].strip() if raw else ""
            upper = first_line.upper()
            if upper.startswith("STUCK"):
                reason = first_line.split(":", 1)[1].strip() if ":" in first_line \
                    else "agent said STUCK"
                return ("STUCK", reason)
            if upper.startswith("WORKING"):
                reason = first_line.split(":", 1)[1].strip() if ":" in first_line \
                    else "agent said WORKING"
                return ("WORKING", reason)
            # Unparseable -- default conservative.
            return ("WORKING", f"agent reply unparseable: {first_line[:80]!r}")
        finally:
            try:
                await session.close()
            except Exception:
                pass

    return _check


async def _dispatch_turn(
    backend,
    agent_cfg,
    session: Session,
    prompt: str,
    log_path: Path,
    meta: JobMeta,
    *,
    iter_num: int,
    job_id: str,
    is_last_iter: bool = False,
) -> tuple[str, float | None]:
    """Dispatch one user-turn with the stuck-watchdog active.

    If ``meta.stuck_timeout > 0`` a watchdog runs concurrently with the
    dispatch. When the dispatch completes normally the watchdog exits
    via ``stop_event``. If the watchdog escalates (STUCK verdict), it
    SIGTERMs/SIGKILLs claude *and* sets an ``escalation_signal``; the
    dispatch then fails, and this wrapper raises
    :class:`StuckWatchdogEscalation` so the runner's main loop can
    distinguish it from a regular backend error and decide whether to
    continue with the next iteration.

    ``is_last_iter`` only influences the Slack-DM wording (continuing
    vs. ending). The actual continue-or-end decision is made by the
    caller.

    Only used for ``kind="turn"`` -- ``/compact`` turns are fast slash
    commands and don't warrant the watchdog overhead.
    """
    if meta.stuck_timeout <= 0:
        return await _dispatch_one(
            backend, agent_cfg, session, prompt, log_path,
            kind="turn", iter_num=iter_num, job_id=job_id,
        )

    stop_event = asyncio.Event()
    escalation_signal = asyncio.Event()

    async def _dispatch_then_signal():
        try:
            return await _dispatch_one(
                backend, agent_cfg, session, prompt, log_path,
                kind="turn", iter_num=iter_num, job_id=job_id,
            )
        finally:
            stop_event.set()

    dispatch_task = asyncio.create_task(_dispatch_then_signal())
    agent_check_fn = _build_agent_stuck_check(backend, agent_cfg, job_id=job_id)
    watchdog_task = asyncio.create_task(stuck_watchdog(
        stop_event=stop_event,
        log_path=log_path,
        meta=meta,
        iter_num=iter_num,
        stuck_timeout_sec=meta.stuck_timeout,
        agent_check_fn=agent_check_fn,
        escalation_signal=escalation_signal,
        is_last_iter=is_last_iter,
    ))
    try:
        try:
            return await dispatch_task
        except Exception as exc:
            if escalation_signal.is_set():
                # Convention: the watchdog stashes the verdict reason as
                # a dynamic ``.reason`` attribute on this asyncio.Event
                # (see _escalate_and_kill in stuck_watchdog.py). Read via
                # getattr so a future watchdog that doesn't set it
                # degrades gracefully to empty.
                reason = getattr(escalation_signal, "reason", "") or ""
                raise StuckWatchdogEscalation(iter_num, reason=reason) from exc
            raise
    finally:
        # stop_event was already set in dispatch's finally, so the
        # watchdog exits naturally. Cancel handles the case where the
        # watchdog is mid-asyncio.sleep when we get here.
        watchdog_task.cancel()
        try:
            await watchdog_task
        except asyncio.CancelledError:
            pass  # expected after cancel()
        except Exception:  # pragma: no cover — defensive: watchdog bug
            log.exception(
                "stuck_watchdog raised unexpectedly for job=%s iter=%d",
                job_id, iter_num,
            )


async def run_job(
    job_id: str,
    *,
    state_dir: Path | None = None,
    resume_session_id: str = "",
    start_iter: int = 0,
) -> int:
    """Run a single job to completion. Returns shell exit code."""
    store = JobStore(state_dir or default_state_path())
    meta = store.get(job_id)
    if meta is None:
        log.error("job_id %r not found in registry", job_id)
        return 1

    try:
        persona = resolve(meta.persona)
    except KeyError as exc:
        meta.status = "error"
        meta.error = f"persona resolve failed: {exc}"
        meta.last_update = time.time()
        store.set(meta)
        return 2

    try:
        agent_cfg = persona.build_config()
    except FileNotFoundError as exc:
        meta.status = "error"
        meta.error = f"persona config build failed: {exc}"
        meta.last_update = time.time()
        store.set(meta)
        return 2

    prompt_path = store.prompt_path(job_id)
    if not prompt_path.exists():
        meta.status = "error"
        meta.error = f"prompt.txt missing: {prompt_path}"
        meta.last_update = time.time()
        store.set(meta)
        return 2
    _raw_prompt = prompt_path.read_text()

    forever = meta.max_iters <= 0
    log_path = store.run_log(job_id)
    result_path = store.result_path(job_id)

    iter_log = _iter_log_path(meta)
    try:
        if resume_session_id:
            _append_runner_event(
                iter_log,
                f"Continued: +{meta.max_iters - start_iter} iterations "
                f"(from iter {start_iter}, new cap {meta.max_iters})",
            )
        else:
            _write_iter_header(iter_log, meta)
    except OSError as exc:
        log.warning("could not write iteration log header at %s: %r", iter_log, exc)

    meta.pid = os.getpid()
    meta.status = "running"
    meta.last_update = time.time()
    store.set(meta)
    _append_log(
        log_path,
        {
            "t": time.time(),
            "kind": "start",
            "pid": os.getpid(),
            "persona": persona.name,
            "cwd": str(persona.cwd),
            "max_iters": meta.max_iters,
            "forever": forever,
            "compact_every": meta.compact_every,
        },
    )

    # Post a "job started" DM and capture the thread anchor ts.
    if not meta.slack_thread_ts:
        anchor_ts = notify_job_start(meta)
        if anchor_ts:
            meta.slack_thread_ts = anchor_ts
            store.set(meta)
            log.info("job %s: thread anchor ts=%s", job_id, anchor_ts)
    else:
        notify_job_start(meta)

    # SIGTERM -> request cancel gracefully.
    def _on_term(signum: int, frame: FrameType | None) -> None:  # noqa: ARG001
        store.request_cancel(job_id)
    try:
        signal.signal(signal.SIGTERM, _on_term)
    except ValueError:
        pass

    backend = get_backend("claude_p", cwd=str(persona.cwd))
    total_cost = (meta.total_cost_usd or 0.0) if start_iter else 0.0
    completed = False
    if resume_session_id:
        session = await backend.open_session(resume_id=resume_session_id)
    else:
        session = await backend.open_session()
    consecutive_stale = 0
    consecutive_error = 0
    prev_text = ""

    try:
        i = start_iter
        while True:
            i += 1
            if store.is_cancel_requested(job_id):
                meta.status = "cancelled"
                _append_log(log_path, {"t": time.time(), "kind": "cancel", "at_iter": i})
                break

            live_meta = store.get(job_id)
            if live_meta is not None:
                meta.continuation = live_meta.continuation
                meta.slack_thread_ts = live_meta.slack_thread_ts

            # "First iter" = the agent has never received the original
            # task prompt. We can't use `i == 1` directly because iter 1
            # might have been stuck-cancelled with no successful dispatch,
            # in which case session.id is still empty and the agent has
            # never seen the original prompt. Use session.id as truth.
            is_first_iter = (not session.id) and not resume_session_id
            if is_first_iter:
                prompt = _build_initial_prompt(
                    _raw_prompt, thread_ts=meta.slack_thread_ts,
                )
            else:
                # Re-inject the Slack threading notice on EVERY continuation
                # when slack_thread_ts is set -- /compact drops the iter-1
                # reminder, and without re-injection the agent posts
                # top-level DMs on later iters.
                prompt = _build_continuation(
                    meta.continuation,
                    thread_ts=meta.slack_thread_ts,
                )

            # Pre-compute whether this is the last iter (the dispatch may
            # take a long time; the watchdog needs this for DM wording).
            iter_is_last = (not forever) and i >= meta.max_iters

            try:
                text, cost = await _dispatch_turn(
                    backend, agent_cfg, session, prompt, log_path, meta,
                    iter_num=i, job_id=job_id,
                    is_last_iter=iter_is_last,
                )
            except StuckWatchdogEscalation as stuck:
                # Watchdog escalated and killed claude. Record the iter
                # as stuck, then either end (if last) or re-open the
                # session and continue with the next iteration.
                meta.current_iter = i
                meta.last_update = time.time()
                live_post = store.get(job_id)
                if live_post is not None:
                    meta.continuation = live_post.continuation
                    meta.slack_thread_ts = live_post.slack_thread_ts
                store.set(meta)
                _append_log(log_path, {
                    "t": time.time(),
                    "kind": "iter_stuck",
                    "iter": i,
                    "action": "ending" if iter_is_last else "continuing",
                    "reason": stuck.reason,
                })
                reason_part = f": {stuck.reason}" if stuck.reason else ""
                tail = (
                    "Final iteration -- job ending as error."
                    if iter_is_last
                    else f"Continuing with iteration {i + 1}."
                )
                _append_iteration(
                    iter_log, i,
                    f"_[Iteration cancelled by stuck-watchdog{reason_part}. {tail}]_",
                )
                result_path.write_text(
                    f"[Iteration {i} cancelled by stuck-watchdog"
                    + reason_part + ".]\n"
                )

                if iter_is_last:
                    meta.status = "error"
                    suffix = f" ({stuck.reason})" if stuck.reason else ""
                    meta.error = (
                        f"final iteration {i} cancelled by stuck-watchdog"
                        + suffix
                    )
                    break

                # More iters remain -- re-open the session and continue.
                try:
                    await session.close()
                except Exception:
                    log.exception("session.close failed after stuck escalation")
                try:
                    if meta.session_id:  # pragma: no cover — stuck escalation recovery
                        session = await backend.open_session(resume_id=meta.session_id)
                    else:
                        session = await backend.open_session()
                except Exception as exc:
                    log.exception("failed to re-open session after stuck escalation")
                    meta.status = "error"
                    meta.error = (
                        f"iter {i} stuck-watchdog cancelled; could not "
                        f"resume session: {exc!r}"
                    )
                    break
                continue

            if cost:
                total_cost += cost

            meta.current_iter = i
            meta.session_id = session.id
            meta.last_update = time.time()
            meta.total_cost_usd = round(total_cost, 6)

            live_post = store.get(job_id)
            if live_post is not None:
                meta.continuation = live_post.continuation
                meta.slack_thread_ts = live_post.slack_thread_ts

            store.set(meta)
            result_path.write_text(text)
            _append_iteration(iter_log, i, text)

            is_last_iter = (not forever) and i >= meta.max_iters
            if is_last_iter:
                meta.status = "done"
                completed = True
                break

            # ---- Early exit (opt-in via --early-exit) ----
            if meta.early_exit:
                verdict = await _classify_output(
                    backend, text, log_path,
                    job_id=job_id, iter_num=i,
                )
                if verdict == "ERROR":
                    consecutive_error += 1
                    consecutive_stale = 0
                elif verdict == "DONE":
                    consecutive_error = 0
                    if prev_text:
                        novelty = await _classify_novelty(
                            backend, prev_text, text, log_path,
                            job_id=job_id, iter_num=i,
                        )
                        if novelty == "STALE":
                            consecutive_stale += 1
                        else:
                            consecutive_stale = 0
                    else:
                        consecutive_stale += 1
                else:
                    consecutive_stale = 0
                    consecutive_error = 0

                if consecutive_stale >= EARLY_EXIT_CONSECUTIVE:
                    meta.status = "done"
                    completed = True
                    _append_log(log_path, {
                        "t": time.time(),
                        "kind": "early_exit",
                        "reason": "consecutive_stale",
                        "count": consecutive_stale,
                        "at_iter": i,
                    })
                    _append_runner_event(
                        iter_log,
                        f"Early exit: {consecutive_stale} consecutive iterations "
                        f"classified as DONE with genuinely nothing new "
                        f"(at iteration {i})",
                    )
                    break

                if consecutive_error >= EARLY_EXIT_CONSECUTIVE:
                    meta.status = "error"
                    meta.error = (
                        f"early exit: {consecutive_error} consecutive iterations "
                        f"classified as ERROR"
                    )
                    _append_log(log_path, {
                        "t": time.time(),
                        "kind": "early_exit",
                        "reason": "consecutive_error",
                        "count": consecutive_error,
                        "at_iter": i,
                    })
                    _append_runner_event(
                        iter_log,
                        f"Early exit: {consecutive_error} consecutive iterations "
                        f"classified as ERROR (at iteration {i})",
                    )
                    break

            prev_text = text

            # Compaction turn (free side-effect; not counted against cap).
            should_compact = (
                meta.compact_every > 0
                and i % meta.compact_every == 0
                and not store.is_cancel_requested(job_id)
            )
            if should_compact:
                _, ccost = await _dispatch_one(
                    backend, agent_cfg, session, COMPACT_TRIGGER, log_path,
                    kind="compact", iter_num=i, job_id=job_id,
                )
                if ccost:
                    total_cost += ccost
                meta.total_cost_usd = round(total_cost, 6)
                meta.last_update = time.time()
                store.set(meta)
    except Exception as exc:
        meta.status = "error"
        meta.error = f"{exc!r}\n{traceback.format_exc()}"
        _append_log(log_path, {"t": time.time(), "kind": "exception", "error": repr(exc)})
    finally:
        meta.last_update = time.time()
        meta.pid = None
        store.set(meta)
        _append_log(log_path, {"t": time.time(), "kind": "end", "status": meta.status})

        cost_str = f"${meta.total_cost_usd:.4f}" if meta.total_cost_usd else "$0"
        cap_str = str(meta.max_iters) if meta.max_iters > 0 else "forever"
        _append_runner_event(
            iter_log,
            f"Final status: **{meta.status}** | "
            f"Iterations: {meta.current_iter}/{cap_str} | "
            f"Cost: {cost_str}",
        )

        try:
            await session.close()
        except Exception:
            pass
        try:
            notify_job_end(meta, store)
        except Exception:
            log.exception("notify_job_end raised; ignored")

    return 0 if completed else 1


def main(argv: list[str] | None = None) -> int:
    """Entrypoint for the detached child: `python -m tigerharness.task_runner _run <id>`."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv:
        print("usage: tigerharness.task_runner._run <job_id> "
              "[--resume-session ID] [--start-iter N]", file=sys.stderr)
        return 2
    job_id = argv[0]
    resume_session_id = ""
    start_iter = 0
    rest = argv[1:]
    while rest:
        flag = rest.pop(0)
        if flag == "--resume-session" and rest:
            resume_session_id = rest.pop(0)
        elif flag == "--start-iter" and rest:
            start_iter = int(rest.pop(0))
    return asyncio.run(run_job(
        job_id,
        resume_session_id=resume_session_id,
        start_iter=start_iter,
    ))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

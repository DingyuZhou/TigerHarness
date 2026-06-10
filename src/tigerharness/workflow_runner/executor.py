"""Sequential workflow executor -- Phase 1 sub-step #4.

This module owns the deterministic loop the spec calls out in
``docs/workflow-runner.md`` under "Architecture", "Loop semantics",
"Constraints", and "Cancel/resume/concurrency". One executor per
task; one task per executor. Two-process protection is enforced by
acquiring the per-task POSIX flock up-front and holding it for the
lifetime of the run.

It consumes everything Wave 1 + Wave 2 built and adds the loop on
top:

* :class:`tigerharness.workflow_runner.sessions.SessionManager` for
  per-persona ``claude -p --resume`` dispatch.
* :func:`tigerharness.journal.wfcore.trailer.parse_trailer` for
  verdict extraction; the executor branches on the typed verdict ADT
  (``isinstance(verdict, Approve|Revise|Block|ParseError)``).
* :class:`Status` / :class:`StepHistoryEntry` / :class:`Orchestration`
  for typed state I/O.
* ``locks.acquire_task_lock`` / ``write_pid`` / ``heartbeat`` for the
  two-process protection contract Akagi flagged.
* ``events.append_event`` for the machine-truth event stream.

What it deliberately does NOT do:

* No compile (Phase 2).
* No human gate / Tier 3 (Phase 3).
* No watchdog (Phase 4 -- for now we trust the per-step ``timeout_sec``
  enforced by ``SessionManager`` and its subtree reap).
* No parallel dispatch (Phase 5 -- ``parallel_with`` is parsed but
  ignored, exactly as the spec says).

Two source-of-truth invariants are load-bearing here and worth
calling out:

1. **``status.last_heartbeat`` and ``.pid`` ``last_heartbeat`` move
   together.** Every status write goes through :meth:`_write_status`,
   which bumps both atomically. ``workflow sweep`` reads either one
   to decide "stale vs alive" and they must not drift.

2. **``iter_counts[step]`` tracks dispatches, not successful verdicts.**
   That makes the per-step ``max_iters`` cap cover parse-failure
   loops too, and guarantees each iteration's ``logs/<step>/iter-NN``
   directory is unique (no overwrites on retry).
"""

from __future__ import annotations

import datetime as _dt
import time as _time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import yaml

from tigerharness.workflow_runner.atomic import (
    read_json,
    write_json_atomic,
)
from tigerharness.workflow_runner.events import append_event
from tigerharness.workflow_runner.locks import (
    LockHeldError,
    acquire_task_lock,
    heartbeat,
    write_pid,
)
from tigerharness.journal.wfcore.models import (
    Orchestration,
    Status,
    StepFrontmatter,
    StepHistoryEntry,
    WorkflowModelError,
    now_iso,
)
from tigerharness.workflow_runner.paths import TaskPaths
from tigerharness.workflow_runner.sessions import (
    InvocationResult,
    SessionManager,
)
from tigerharness.journal.wfcore.trailer import (
    Approve,
    Block,
    ParseError,
    Revise,
    Verdict,
    parse_trailer,
)


__all__ = [
    "ExecutionOutcome",
    "ExecutorError",
    "MAX_PARSE_FAILURES",
    "WorkflowExecutor",
]


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Hard cap before a parse-failure loop escalates. Per the spec's
#: "Persona response trailer protocol" (docs/workflow-runner.md), the
#: persona gets the original shot plus exactly one re-prompt. If the
#: second attempt also fails to produce a clean trailer, the contract
#: is broken and we escalate.
MAX_PARSE_FAILURES = 2

#: Spec-mandated re-prompt text injected before the step body on the
#: second attempt (i.e., when ``parse_failure_count == 1``). Verbatim
#: from docs/workflow-runner.md "Persona response trailer protocol".
_PARSE_FAILURE_REPROMPT = (
    "I couldn't find your WORKFLOW: trailer. Please end your next "
    "reply with one of WORKFLOW: APPROVE / REVISE / BLOCK."
)

#: Sentinels recognised in step edge targets.
_SENTINEL_DONE = "__done__"
_SENTINEL_ESCALATE = "__escalate__"

#: Filename of the cancel sentinel created by ``workflow cancel`` CLI.
_CANCEL_FLAG = ".cancel"

#: YAML frontmatter delimiter for step files.
_FM_DELIM = "---"


# --------------------------------------------------------------------------- #
# Public types
# --------------------------------------------------------------------------- #


class ExecutorError(RuntimeError):
    """Raised when the executor refuses to start (lock contention,
    missing journal, malformed orchestration). Distinct from in-loop
    problems, which become :class:`ExecutionOutcome` rows or
    escalations rather than exceptions."""


@dataclass(frozen=True)
class ExecutionOutcome:
    """Return value of :meth:`WorkflowExecutor.run`.

    ``final_phase`` matches the terminal value written to
    ``status.json`` (``"done"`` / ``"escalated"`` / ``"cancelled"``).
    ``reason`` is a short human-readable summary suitable for logging
    and for the optional Slack notification at higher layers.
    """

    final_phase: str
    reason: str
    total_cost_usd: float


# --------------------------------------------------------------------------- #
# Executor
# --------------------------------------------------------------------------- #


class WorkflowExecutor:
    """Deterministic sequential loop for a single workflow task."""

    def __init__(
        self,
        paths: TaskPaths,
        *,
        session_manager: Optional[SessionManager] = None,
        now_iso_fn: Callable[[], str] = now_iso,
        now_epoch_fn: Callable[[], float] = _time.time,
    ) -> None:
        """Wire the executor to a task folder.

        ``session_manager`` is the test seam: by default we build a
        real :class:`SessionManager` rooted at ``paths.task_dir``; tests
        inject a fake so the loop can run without spawning ``claude``.

        ``now_iso_fn`` and ``now_epoch_fn`` are injected for
        deterministic constraint tests (so ``max_task_wall_sec`` can be
        triggered without sleeping).
        """
        self._paths = paths
        self._sessions = (
            session_manager
            if session_manager is not None
            else SessionManager(paths.task_dir)
        )
        self._now_iso = now_iso_fn
        self._now_epoch = now_epoch_fn

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #

    def run(self) -> ExecutionOutcome:
        """Acquire the task lock and run the loop to a terminal phase.

        Returns the :class:`ExecutionOutcome`. Raises
        :class:`ExecutorError` if the task lock is already held or the
        journal is missing required files.
        """
        try:
            with acquire_task_lock(self._paths.task_dir, blocking=False):
                return self._run_locked()
        except LockHeldError as exc:
            raise ExecutorError(
                f"task {self._paths.task_id!r} is already running "
                f"({exc})"
            ) from exc

    # ------------------------------------------------------------------ #
    # Locked loop
    # ------------------------------------------------------------------ #

    def _run_locked(self) -> ExecutionOutcome:
        # Establish the pid file *before* any status writes so the
        # heartbeat helper called by ``_write_status`` always has a
        # file to update.
        write_pid(self._paths.task_dir, now=self._now_iso())

        orch = self._load_orchestration()
        status = self._load_status()

        # Wall-clock baseline is ``status.started_at`` so the constraint
        # survives crashes/resumes (a 24h cap doesn't reset on every
        # resume). Fall back to "now" if the timestamp is unparseable
        # -- the model already validated the shape on load, so this
        # branch is purely defensive.
        wall_start = (
            _iso_to_epoch(status.started_at) or self._now_epoch()
        )

        while True:
            terminal = self._step_once(orch, status, wall_start)
            if terminal is not None:
                return terminal

    # ------------------------------------------------------------------ #
    # One iteration boundary -- returns ExecutionOutcome on terminal,
    # ``None`` to continue.
    # ------------------------------------------------------------------ #

    def _step_once(
        self,
        orch: Orchestration,
        status: Status,
        wall_start: float,
    ) -> Optional[ExecutionOutcome]:
        if self._cancel_requested(status):
            return self._finalize_cancel(status)

        breach = self._check_global_constraints(
            orch=orch, status=status, wall_start=wall_start
        )
        if breach is not None:
            return self._finalize_escalate(
                status, reason=breach, kind="constraint_breached"
            )

        step_id = status.current_step
        if step_id is None:
            # The CLI initialises ``current_step`` to the entrypoint;
            # reaching here means external code corrupted the pointer.
            # Escalate rather than crash so the journal stays
            # inspectable.
            return self._finalize_escalate(
                status,
                reason="current_step_missing",
                kind="constraint_breached",
            )
        try:
            step, body = self._load_step(step_id)
        except (FileNotFoundError, WorkflowModelError) as exc:
            return self._finalize_escalate(
                status,
                reason=f"step_load_failed:{step_id}:{exc}",
                kind="constraint_breached",
            )

        # Per-step cap is checked on the would-be-next value so we
        # never start an iter that's already over budget.
        next_iter = status.iter_counts.get(step_id, 0) + 1
        cap = self._effective_iter_cap(step, orch)
        if next_iter > cap:
            return self._finalize_escalate(
                status,
                reason=f"max_loop_iters:{step_id}:{cap}",
                kind="constraint_breached",
            )

        parse_prologue = self._parse_failure_prologue(status)
        feedback_prologue = self._feedback_prologue(status, step_id, next_iter)
        prompt = parse_prologue + feedback_prologue + body

        # Bump current_iter + step_started_at so `workflow show`
        # mid-flight reports something honest.
        status.current_iter = next_iter
        status.step_started_at = self._now_iso()
        self._write_status(status)
        iter_started_at = status.step_started_at
        append_event(
            self._paths.events_jsonl,
            "step_started",
            step=step_id,
            iter=next_iter,
            persona=step.persona,
        )

        # ensure_iter_dir before dispatch: mkdir failures should
        # surface now, not after the claude call returns.
        log_dir = self._paths.ensure_iter_dir(step_id, next_iter)

        result = self._sessions.invoke(
            step.persona,
            prompt,
            timeout_sec=step.timeout_sec,
            log_dir=log_dir,
        )

        # Brief step 6: persist cost + iter_counts BEFORE trailer
        # parse. Crash here leaves cost durable; resume re-runs the
        # iter (bounded by max_loop_iters). Counting dispatches
        # (success or parse failure) is what makes that cap bound
        # parse-failure storms; without the bump a malformed-trailer
        # loop could spin forever inside one iter number.
        cost_delta = result.cost_usd or 0.0
        status.cost_usd_total += cost_delta
        status.cost_usd_per_step[step_id] = (
            status.cost_usd_per_step.get(step_id, 0.0) + cost_delta
        )
        status.iter_counts[step_id] = next_iter
        self._write_status(status)

        verdict = parse_trailer(result.stdout)
        return self._apply_verdict(
            status=status,
            step=step,
            iter_n=next_iter,
            iter_started_at=iter_started_at,
            cost_usd=cost_delta,
            verdict=verdict,
            invocation_error=result.error,
        )

    # ------------------------------------------------------------------ #
    # Verdict branching
    # ------------------------------------------------------------------ #

    def _apply_verdict(
        self,
        *,
        status: Status,
        step: StepFrontmatter,
        iter_n: int,
        iter_started_at: str,
        cost_usd: float,
        verdict: Verdict,
        invocation_error: Optional[str],
    ) -> Optional[ExecutionOutcome]:
        step_id = step.id

        if isinstance(verdict, (Approve, Revise, Block)):
            reason = (
                verdict.summary
                if isinstance(verdict, (Revise, Block))
                else None
            )
            self._record_verdict(
                status,
                step=step,
                iter_n=iter_n,
                iter_started_at=iter_started_at,
                verdict=verdict,
                reason=reason,
                cost_usd=cost_usd,
            )
            if isinstance(verdict, Block):
                return self._finalize_escalate(
                    status,
                    reason=f"block:{verdict.summary}",
                    kind="block",
                )
            if isinstance(verdict, Approve):
                target = step.edges.on_approve
                source = "approve"
            else:
                target = verdict.target or step.edges.on_revise
                source = "revise"
            return self._route(status, step_id, target, source=source)

        # ParseError: no history entry (the verdict didn't actually
        # happen). Bump the counter, emit the event, re-dispatch on
        # the next loop iteration -- or escalate if the contract is
        # broken three times running.
        assert isinstance(verdict, ParseError)
        new_count = int(
            status.phase_state.get("parse_failure_count", 0)
        ) + 1
        status.phase_state["parse_failure_count"] = new_count
        self._write_status(status)
        append_event(
            self._paths.events_jsonl,
            "verdict_parse_failed",
            step=step_id,
            iter=iter_n,
            parse_failure_count=new_count,
            reason=verdict.reason,
            invocation_error=invocation_error,
        )
        if new_count >= MAX_PARSE_FAILURES:
            return self._finalize_escalate(
                status,
                reason="parse_failure_loop",
                kind="constraint_breached",
            )
        return None

    def _record_verdict(
        self,
        status: Status,
        *,
        step: StepFrontmatter,
        iter_n: int,
        iter_started_at: str,
        verdict: Verdict,
        reason: Optional[str],
        cost_usd: float,
    ) -> None:
        """Three shared post-dispatch actions: history append, reset
        the parse-failure counter (a clean verdict means the contract
        is back on the rails), emit ``step_completed``.
        """
        self._append_history(
            status,
            step=step,
            iter_n=iter_n,
            iter_started_at=iter_started_at,
            verdict_tag=verdict.kind,
            reason=reason,
            cost_usd=cost_usd,
        )
        self._reset_parse_failures(status)
        self._emit_step_completed(step.id, iter_n, verdict.kind, cost_usd)

    def _route(
        self,
        status: Status,
        step_id: str,
        target: str,
        *,
        source: str,
    ) -> Optional[ExecutionOutcome]:
        """Apply edge target: real step, ``__done__``, or
        ``__escalate__``. REVISE -> __done__ escalates because looping
        back to done is nonsensical; APPROVE -> __escalate__ honors a
        graph that wants approval-as-termination.
        """
        if target == _SENTINEL_DONE:
            if source == "revise":
                return self._finalize_escalate(
                    status,
                    reason=f"revise_to_done:{step_id}",
                    kind="constraint_breached",
                )
            return self._finalize_done(
                status, reason=f"approve to __done__ on {step_id}"
            )
        if target == _SENTINEL_ESCALATE:
            return self._finalize_escalate(
                status,
                reason=f"{source}_to_escalate:{step_id}",
                kind="constraint_breached",
            )
        self._advance_pointer(status, target)
        return None

    # ------------------------------------------------------------------ #
    # Status helpers
    # ------------------------------------------------------------------ #

    def _write_status(self, status: Status) -> None:
        """Atomically persist ``status.json`` AND sync ``.pid`` heartbeat.

        Akagi's load-bearing contract: ``status.last_heartbeat`` and the
        ``.pid`` file's ``last_heartbeat`` are the two sources of truth
        the sweep CLI reads. They must move together so a sweep that
        catches one mid-cycle doesn't see drift. Centralising both
        writes here is the single place to maintain that invariant.
        """
        ts = self._now_iso()
        status.last_heartbeat = ts
        write_json_atomic(self._paths.status_json, status.to_dict())
        try:
            heartbeat(self._paths.task_dir, now=ts)
        except FileNotFoundError:  # pragma: no cover - defensive
            # pid file not yet written -- only possible if a caller
            # invokes _write_status before _run_locked's write_pid.
            # Not a real path in production but keeps tests harmless.
            pass

    def _append_history(
        self,
        status: Status,
        *,
        step: StepFrontmatter,
        iter_n: int,
        iter_started_at: str,
        verdict_tag: str,
        reason: Optional[str],
        cost_usd: float,
    ) -> None:
        status.step_history.append(
            StepHistoryEntry(
                step=step.id,
                iter=iter_n,
                persona=step.persona,
                started_at=iter_started_at,
                ended_at=self._now_iso(),
                verdict=verdict_tag,
                reason=reason,
                cost_usd=cost_usd,
            )
        )

    def _advance_pointer(self, status: Status, next_step: str) -> None:
        """Move the pointer to ``next_step`` and persist.

        ``current_iter`` mirrors ``iter_counts.get(next_step, 0)`` so
        the value reflects "iters completed for the new current step".
        """
        status.current_step = next_step
        status.current_iter = status.iter_counts.get(next_step, 0)
        status.step_started_at = None
        self._write_status(status)

    def _reset_parse_failures(self, status: Status) -> None:
        """Clear the parse-failure counter -- only *consecutive*
        parse failures count toward the loop.
        """
        status.phase_state.pop("parse_failure_count", None)

    # ------------------------------------------------------------------ #
    # Cancellation
    # ------------------------------------------------------------------ #

    def _cancel_requested(self, status: Status) -> bool:
        flag = self._paths.task_dir / _CANCEL_FLAG
        return flag.exists() or status.phase == "cancelling"

    # ------------------------------------------------------------------ #
    # Constraint checks
    # ------------------------------------------------------------------ #

    def _check_global_constraints(
        self,
        *,
        orch: Orchestration,
        status: Status,
        wall_start: float,
    ) -> Optional[str]:
        """Return a short ``reason`` string if a breach hit, else ``None``.

        Per-step iter caps are checked in :meth:`_step_once` once the
        step is known; this routine covers the cross-step ceilings
        only.
        """
        cfg = orch.workflow_config
        if (
            cfg.max_cost_usd > 0
            and status.cost_usd_total >= cfg.max_cost_usd
        ):
            return (
                f"max_cost_usd:{status.cost_usd_total:.6f}>="
                f"{cfg.max_cost_usd:.6f}"
            )
        elapsed = self._now_epoch() - wall_start
        if cfg.max_task_wall_sec > 0 and elapsed >= cfg.max_task_wall_sec:
            return (
                f"max_task_wall_sec:{elapsed:.1f}>="
                f"{cfg.max_task_wall_sec}"
            )
        return None

    @staticmethod
    def _effective_iter_cap(
        step: StepFrontmatter, orch: Orchestration
    ) -> int:
        """Per-step cap honored by the executor.

        Both the playbook ``workflow_config.max_loop_iters`` and the
        per-step ``max_iters`` are spec-relevant; the tighter of the
        two wins so neither gets accidentally bypassed.
        """
        return min(step.max_iters, orch.workflow_config.max_loop_iters)

    # ------------------------------------------------------------------ #
    # Finalisation
    # ------------------------------------------------------------------ #

    def _finalize_done(
        self, status: Status, *, reason: str
    ) -> ExecutionOutcome:
        status.phase = "done"
        status.escalation = None
        self._write_status(status)
        append_event(
            self._paths.events_jsonl,
            "task_completed",
            task_id=self._paths.task_id,
            cost_usd_total=status.cost_usd_total,
        )
        return ExecutionOutcome(
            final_phase="done",
            reason=reason,
            total_cost_usd=status.cost_usd_total,
        )

    def _finalize_escalate(
        self, status: Status, *, reason: str, kind: str
    ) -> ExecutionOutcome:
        status.phase = "escalated"
        status.escalation = reason
        self._write_status(status)
        append_event(
            self._paths.events_jsonl,
            kind,
            task_id=self._paths.task_id,
            reason=reason,
        )
        return ExecutionOutcome(
            final_phase="escalated",
            reason=reason,
            total_cost_usd=status.cost_usd_total,
        )

    def _finalize_cancel(self, status: Status) -> ExecutionOutcome:
        status.phase = "cancelled"
        self._write_status(status)
        append_event(
            self._paths.events_jsonl,
            "cancel_complete",
            task_id=self._paths.task_id,
        )
        return ExecutionOutcome(
            final_phase="cancelled",
            reason="cancel requested",
            total_cost_usd=status.cost_usd_total,
        )

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #

    def _emit_step_completed(
        self,
        step_id: str,
        iter_n: int,
        verdict_tag: str,
        cost_usd: float,
    ) -> None:
        append_event(
            self._paths.events_jsonl,
            "step_completed",
            step=step_id,
            iter=iter_n,
            verdict=verdict_tag,
            cost_usd=cost_usd,
        )

    # ------------------------------------------------------------------ #
    # Loading
    # ------------------------------------------------------------------ #

    def _load_orchestration(self) -> Orchestration:
        try:
            raw = read_json(self._paths.orchestration_json)
        except FileNotFoundError as exc:
            raise ExecutorError(
                f"orchestration.json missing: {exc}"
            ) from exc
        try:
            return Orchestration.from_dict(raw)
        except WorkflowModelError as exc:
            raise ExecutorError(
                f"orchestration.json invalid: {exc}"
            ) from exc

    def _load_status(self) -> Status:
        try:
            raw = read_json(self._paths.status_json)
        except FileNotFoundError as exc:
            raise ExecutorError(
                f"status.json missing: {exc}"
            ) from exc
        try:
            return Status.from_dict(raw)
        except WorkflowModelError as exc:
            raise ExecutorError(
                f"status.json invalid: {exc}"
            ) from exc

    def _load_step(self, step_id: str) -> tuple[StepFrontmatter, str]:
        """Read the step file once, return (frontmatter, body).

        Single I/O + single fence-scan per dispatch; both halves stay
        consistent with the file as it was at this instant.
        """
        text = self._paths.step_file(step_id).read_text(encoding="utf-8")
        fm = _parse_frontmatter(text)
        if not fm:
            raise WorkflowModelError(
                f"step file {step_id}.md has no YAML frontmatter"
            )
        return StepFrontmatter.from_dict(fm), _extract_body(text)

    # ------------------------------------------------------------------ #
    # Parse-failure prologue (spec re-prompt on the second attempt)
    # ------------------------------------------------------------------ #

    def _parse_failure_prologue(self, status: Status) -> str:
        """Inject the spec-mandated re-prompt text before the next
        dispatch when the prior attempt failed to produce a clean
        trailer. Empty otherwise.
        """
        if int(status.phase_state.get("parse_failure_count", 0)) <= 0:
            return ""
        return _PARSE_FAILURE_REPROMPT + "\n\n"

    # ------------------------------------------------------------------ #
    # Feedback prologue (REVISE rewinds)
    # ------------------------------------------------------------------ #

    def _feedback_prologue(
        self, status: Status, step_id: str, iter_n: int
    ) -> str:
        """Build the spec's "Iteration N -- previous attempts produced
        this feedback" prologue from the tail of ``step_history``.

        We collect consecutive REVISE entries at the tail (oldest
        first) until the chain breaks. That matches the spec
        ("verbatim REVISE reasons from the rewind chain, oldest
        first") and stays empty on a fresh iter 1 of any step.
        """
        if iter_n <= 1 or not status.step_history:
            return ""
        reasons: list[str] = []
        for entry in reversed(status.step_history):
            if entry.verdict == "REVISE" and entry.reason:
                reasons.append(entry.reason)
            else:
                break
        if not reasons:
            return ""
        reasons.reverse()
        bullet = "\n".join(f"- {r}" for r in reasons)
        return (
            f"[Iteration {iter_n} -- previous attempts produced this "
            f"feedback:]\n{bullet}\n\n"
        )


# --------------------------------------------------------------------------- #
# Module-level helpers (frontmatter parsing + ts conversion)
# --------------------------------------------------------------------------- #


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Extract the YAML mapping fenced by ``---`` at the top of ``text``.

    Returns an empty dict if no fence is present or the body is not a
    YAML mapping. Mirrors the contract used by ``cli._parse_frontmatter``
    but kept local so executor.py has no cross-CLI dependency.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != _FM_DELIM:
        return {}
    end: Optional[int] = None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == _FM_DELIM:
            end = i
            break
    if end is None:
        return {}
    fm_text = "".join(lines[1:end])
    try:
        data = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _extract_body(text: str) -> str:
    """Return the post-frontmatter body of a step file.

    If the file has no fenced frontmatter, the whole text is the body.
    Leading blank lines after the closing fence are stripped so the
    prompt the persona sees doesn't start with empty lines.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != _FM_DELIM:
        return text
    end: Optional[int] = None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == _FM_DELIM:
            end = i
            break
    if end is None:
        return text
    body = "".join(lines[end + 1:])
    return body.lstrip("\r\n")


def _iso_to_epoch(ts: Optional[str]) -> Optional[float]:
    """Best-effort ISO-8601 -> epoch seconds.

    Returns ``None`` on a missing or unparseable input so callers can
    fall back without crashing.
    """
    if not ts:
        return None
    try:
        normalised = ts.replace("Z", "+00:00")
        dt = _dt.datetime.fromisoformat(normalised)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None

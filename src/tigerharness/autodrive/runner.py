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
exception. Which run shapes are sanctioned, why, and what changes if the
vendor's billing terms shift is the rails doctrine in
``docs/subscription-backend.md`` -- re-read it before extending this
module. The guardrails here exist because an unattended loop must always
be budget-capped and killable: that is what ``max_budget_usd`` and the
``autodrive stop`` off-switch guard. Keep those guardrails loud.

Loop shape: **fire on a fixed cadence, do NOT wait** (overlap allowed).
Every ``interval`` seconds the loop launches a fresh drive and immediately
goes back to waiting for the next tick -- it does not block on the drive
finishing, so a slow drive and the next fire can run concurrently. This is
safe and self-limiting because the journal coordinates through its claim
compare-and-set lease: a redundant overlapping fire sweeps, finds the
active task **busy**, and exits cheaply, while genuinely parallel work
(multiple actionable tasks) is picked up by different fires. Each fire is
also a brand-new agent session (no ``--resume``), so context stays clean
and compact every time. A drive that hits its budget cap or context
ceiling returns a non-terminal ``stop_reason``; the journal task simply
stays ``in_progress``/idle and a later fire resumes it -- truncation is
safe because the journal's session model is resumable.

There is deliberately **no concurrency cap**: the busy-lease no-op makes a
pile-up of redundant fires cheap, and each individual drive is still
bounded by its own ``max_budget_usd``. (Note the multiplier, though: N
concurrent drives can spend up to N x the per-drive cap within one
interval.)

THE QUEUE PROBE AND AUTO-STOP (ADR 0010)
-----------------------------------------
Before each fire the loop runs the journal's **plain-Python** sweep itself
(:func:`probe_queue`) and acts on the verdict, so an empty queue costs a
file walk instead of a model session:

- ``actionable`` -- fire a drive (the legacy behaviour).
- ``busy`` -- a live session already owns the in-flight task; **skip** the
  fire entirely. The redundant-fire-is-cheap argument above still holds as
  the safety net, but not paying for it at all is cheaper.
- ``idle`` -- nothing actionable and nothing busy. Fire **one** last drive
  (its prompt ends with the idle-maintenance tail), and once that drive has
  completed with nothing new in the queue, **exit the loop**: the daemon
  stops itself rather than burning an interval forever on an empty journal.

Any ``actionable``/``busy`` verdict resets the maintenance latch, so work
arriving mid-maintenance keeps the daemon alive. The probe is fail-soft: an
unreadable journal degrades to ``actionable``, i.e. exactly the pre-ADR-0010
always-fire behaviour, never a silent stall.
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

from .notifier import Notifier, NullNotifier

log = logging.getLogger(__name__)


# ----- constants -----

#: Minimum allowed interval. A single drive already takes minutes, so a
#: floor mainly stops a typo (``--interval 1``) from firing a fresh drive
#: every second and piling up dozens of concurrent backends. Overlap is
#: allowed by design, but the floor keeps the pile-up rate sane.
MIN_INTERVAL_SECONDS = 60.0

#: Default cadence when neither ``--interval`` nor the team's
#: ``TIGERHARNESS_AUTODRIVE_INTERVAL`` says otherwise: 10 minutes.
DEFAULT_INTERVAL_SECONDS = 600.0

#: Default per-tick prompt is built from :func:`default_prompt`; the
#: permission mode defaults to ``bypassPermissions`` because the driver
#: runs unattended and must never stall on a permission prompt.
DEFAULT_PERMISSION_MODE = "bypassPermissions"
DEFAULT_BACKEND = "claude_p"

#: Notification backend. ``"slack"`` posts a heartbeat per *cycle* -- a fire
#: heartbeat when it launches a drive, a skip pulse when the probe declines
#: to (see :func:`skip_text`) -- plus a threaded status/summary on
#: completion; ``"none"`` mutes (the loop runs unchanged, only the posting
#: is suppressed). See ``docs/autodrive-notifications.md``.
DEFAULT_NOTIFY = "slack"

#: Slack messages have generous limits, but a drive's closing summary can be
#: long; cap it so a heartbeat thread stays skimmable.
SUMMARY_MAX_CHARS = 600

#: Queue-probe verdicts (ADR 0010). Plain strings so they serialize into
#: logs and the state file without a codec.
QUEUE_ACTIONABLE = "actionable"
QUEUE_BUSY = "busy"
QUEUE_IDLE = "idle"

#: Why the loop exited. ``queue-drained`` is the ADR 0010 self-stop; the
#: others are the pre-existing external exits.
EXIT_DRAINED = "queue-drained"
EXIT_STOP_REQUESTED = "stop-requested"
EXIT_MAX_TICKS = "max-ticks"


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
    # Notification config (defaulted so older state files / call sites that
    # predate notifications deserialize cleanly). ``notify`` is "slack" or
    # "none"; ``notify_channel`` is a Slack channel id, or None for the
    # operator DM.
    notify: str = DEFAULT_NOTIFY
    notify_channel: str | None = None
    # The journal the daemon drives, as an absolute path string. Used by the
    # in-daemon queue probe (ADR 0010). ``None`` means "no probe configured"
    # -- the loop then fires unconditionally, i.e. the pre-ADR-0010
    # behaviour. That is what a state file written by an older version
    # deserializes to, so an upgrade never silently changes an existing
    # daemon's shape mid-flight.
    journal_root: str | None = None


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
        own = (
            f" -- your --driver persona {driver} is the sweep's own persona"
        )
    else:
        claim = "Claim each task with `--allow-api-drive`"
        own = (
            " -- with no driver persona set, it runs as the plain "
            "team-floor sweep (no own-persona bypass)"
        )
    return (
        "You are an Operator-authorized automatic journal driver "
        "(`tigerharness autodrive`). This is a SANCTIONED programmatic "
        "drive: the usual 'never drive from claude -p / cron / API' "
        "boundary is deliberately lifted for THIS process by explicit "
        "Operator authorization. Drive the journal now using the "
        "drive-journal skill -- sweep, pick exactly one actionable task, "
        "work it, and cascade through the queue. When you claim a task, "
        "load the assigned persona's memory per "
        "`memories/<persona>/briefing/README.md` before working it -- "
        f"directives and skills must reach the work. {claim} (the "
        "`--allow-api-drive` flag is harmless when no Slack thread marker "
        "is set). When the final sweep finds nothing actionable and "
        "nothing busy, run the skill's idle-maintenance tail before "
        "stopping: `tigerharness slack-bridge compact-idle` (self-gating; "
        "its only model call is one bounded /compact turn per heavy idle "
        "lane) and the team's sweep-memory skill (self-gating via its "
        f"split gate + watermark + lease{own}; its summarize work runs "
        "in Task-tool sub-agents, which THIS session may spawn). Then "
        "stop cleanly."
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
        "notify": cfg.notify,
        "notify_channel": cfg.notify_channel,
        "journal_root": cfg.journal_root,
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
        notify=state.get("notify", DEFAULT_NOTIFY),
        notify_channel=state.get("notify_channel"),
        journal_root=state.get("journal_root"),
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
    in_flight: int | None = None,
) -> None:
    """Read-modify-write the live progress fields after a drive *completes*.

    ``tick_count`` counts *completed* drives (a fire that has returned or
    raised), distinct from ``fire_count`` (drives *launched*); with overlap
    the two diverge while drives are in flight. Read-modify-write (not
    overwrite) so the pid + static config the parent wrote survive each
    update. If the file vanished (a concurrent ``stop``), there is nothing
    to update -- skip silently. ``in_flight`` is only written when given so
    a completion update can refresh the live in-flight gauge.
    """
    state = read_state(path)
    if state is None:
        return
    state["tick_count"] = tick_count
    state["last_tick_at"] = at
    state["last_stop_reason"] = stop_reason
    state["last_cost_usd"] = cost_usd
    state["last_error"] = error
    if in_flight is not None:
        state["in_flight"] = in_flight
    write_state(path, state)


def record_fire(
    path: Path, *, fire_count: int, at: str, in_flight: int
) -> None:
    """Read-modify-write the live progress fields when a drive is *launched*.

    Separate from :func:`record_tick` because in the fixed-cadence/overlap
    model a fire and its completion are distinct events: ``fire_count`` and
    ``last_fire_at`` advance the instant a drive starts, while
    ``tick_count``/``last_tick_at`` only move when one finishes. ``in_flight``
    is the live count of drives currently running. Skips silently if the
    state file vanished (a concurrent ``stop``)."""
    state = read_state(path)
    if state is None:
        return
    state["fire_count"] = fire_count
    state["last_fire_at"] = at
    state["in_flight"] = in_flight
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


# ----- the queue probe (ADR 0010) -----

def probe_queue(cfg: AutodriveConfig) -> str:
    """Classify the journal queue **without spending a model call**.

    Returns one of :data:`QUEUE_ACTIONABLE`, :data:`QUEUE_BUSY`,
    :data:`QUEUE_IDLE`. This is the journal's own lazy sweep -- plain,
    non-AI Python -- run inside the daemon so an empty queue costs a file
    walk instead of a whole ``claude -p`` session.

    ``deferred/`` entries count as **actionable**: a deferred conversation is
    queued work that the drive materializes at the top of OPERATING.md. Miss
    that and the daemon would cheerfully stop with an inbox full of Slack
    asks. ``needs_input`` and ``blocked`` deliberately do **not** count --
    they are waiting on the Operator, and spinning a daemon against them
    would burn an interval forever for no progress.

    **Not a read-only walk**, despite the name: this is the journal's real
    sweep, so it archives ``state=done`` tasks and materializes any due
    schedule definition. That is deliberate -- the probe must classify the
    queue exactly as a drive would, or the daemon would stop on a queue the
    drive considers full -- but it does mean the daemon writes to the journal
    once per interval even on a tick that fires nothing.

    Fail-soft by design: no configured journal root, or any error reading
    the journal, returns ``actionable``. That degrades to the pre-ADR-0010
    "always fire" behaviour -- the drive then does its own sweep and reports
    properly -- rather than silently stalling a queue that has real work in
    it. Stalling is the worse failure: an over-fire costs one drive, a
    false idle costs every task in the queue.
    """
    if not cfg.journal_root:
        return QUEUE_ACTIONABLE
    try:
        from ..journal.deferred import list_deferred
        from ..journal.paths import JournalPaths
        from ..journal.sweep import sweep

        paths = JournalPaths(Path(cfg.journal_root))
        result = sweep(paths)
        if result.has_actionable() or list_deferred(paths):
            return QUEUE_ACTIONABLE
        if result.in_progress_busy:
            return QUEUE_BUSY
        return QUEUE_IDLE
    except Exception as exc:
        log.warning(
            "autodrive queue probe failed (%s: %s); assuming actionable",
            type(exc).__name__, exc,
        )
        return QUEUE_ACTIONABLE


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


# ----- notification text builders -----

def heartbeat_text(fire_no: int, at: str, in_flight: int) -> str:
    """The fire heartbeat (parent message): a fixed-shape pulse whose rhythm
    is the health signal. Detail rides in the threaded completion reply."""
    return (
        f"autodrive heartbeat - fire #{fire_no} launched {at} "
        f"(in-flight {in_flight})"
    )


def _truncate_summary(text: str, limit: int = SUMMARY_MAX_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " [...]"


def completion_text(fire_no: int, outcome: Any) -> str:
    """The threaded reply for a drive that *returned*: stop reason, cost, and
    the drive's own closing summary (``final_output``) if it produced one."""
    stop = getattr(outcome, "stop_reason", None)
    cost = getattr(outcome, "cost_usd", None)
    summary = getattr(outcome, "final_output", None)
    head = f"fire #{fire_no} done: stop_reason={stop}"
    if cost is not None:
        head += f"  cost=${cost:.2f}"
    if summary:
        return f"{head}\n{_truncate_summary(str(summary))}"
    return head


def error_text(fire_no: int, exc: Any) -> str:
    """The threaded reply for a drive that *raised*."""
    return f"fire #{fire_no} FAILED: {type(exc).__name__}: {exc}"


#: Why a cycle declined to fire. Rides in the skip pulse so the channel
#: says *why* the rhythm ticked without launching a drive.
SKIP_BUSY = "queue busy - a live session owns the in-flight task"
SKIP_WAITING = "queue idle - waiting on an in-flight drive"


def skip_text(launched: int, at: str, reason: str, in_flight: int) -> str:
    """The pulse for a cycle that probes and fires *nothing* (ADR 0010).

    Before the queue probe every cycle fired, so every cycle posted a
    heartbeat and the rhythm itself was the health signal. The probe made
    the no-work cycle free, but it also made it *silent* -- and a long busy
    stretch then reads exactly like a crash. This restores the rhythm at
    zero model cost: same ``autodrive heartbeat`` prefix as
    :func:`heartbeat_text`, so the channel reads as one continuous pulse.
    """
    return (
        f"autodrive heartbeat - no fire at {at} - {reason} "
        f"(in-flight {in_flight}, {launched} drive(s) so far)"
    )


def drained_text(launched: int, at: str) -> str:
    """The closing message when the daemon stops itself (ADR 0010). Makes a
    deliberate stop legible: without it, silence after the last heartbeat
    would read as a crash."""
    return (
        f"autodrive stopped {at} - queue drained after {launched} drive(s); "
        "idle maintenance complete. Scheduling new work starts it again."
    )


DriveFn = Callable[..., Awaitable[Any]]
SleepFn = Callable[[float], Awaitable[Any]]
StopFn = Callable[[], bool]


ProbeFn = Callable[[AutodriveConfig], str]

#: Last-moment veto on the drained-queue exit. Returns True to commit to
#: stopping, False to stay up because work arrived. See ``run_loop``.
ConfirmExitFn = Callable[[], bool]


@dataclass
class _Fire:
    """One launched drive plus its notification thread handle. The loop holds
    these so each completion threads its status under the right heartbeat."""

    task: "asyncio.Task[Any]"
    thread: str | None
    fire_no: int
    #: True when this fire was launched *because* the queue went idle -- i.e.
    #: it is the maintenance drive. Its completion is what arms the auto-stop.
    maintenance: bool = False


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
    notifier: Notifier | None = None,
    probe: ProbeFn = probe_queue,
    confirm_exit: ConfirmExitFn | None = None,
) -> int:
    """Fire a fresh drive on a fixed cadence; never wait for it. Returns
    the number of drives *launched*.

    Each cycle probes the queue (:func:`probe_queue`, plain Python, no model
    call), and when there is work to do posts a heartbeat (the parent
    message), launches a drive (fire-and-forget via ``asyncio.create_task``),
    records the fire, then sleeps one interval -- it does **not** block on the
    drive finishing, so a slow drive overlaps the next fire. When a drive
    completes, its status + summary is threaded under that fire's heartbeat.
    There is no concurrency cap: the journal's busy-lease makes a redundant
    overlapping fire a cheap no-op, and each drive is still bounded by its
    own ``max_budget_usd``.

    A cycle that fires *nothing* still pulses (:func:`skip_text`), so the
    heartbeat rhythm tracks the daemon's health rather than its spending --
    a long busy stretch must never be indistinguishable from a crash.

    **Auto-stop (ADR 0010).** A ``busy`` verdict skips the fire; an ``idle``
    verdict fires the maintenance drive once and then, once that drive has
    finished and nothing new has arrived, exits the loop -- the daemon stops
    itself on a drained queue. ``cmd_loop`` clears the state file on that
    clean return, so a later ``ensure_running`` starts a fresh daemon.

    ``confirm_exit`` is the last-moment veto on that stop, and it exists to
    close a lost-wakeup race. Auto-start decides "a daemon is already up" by
    reading the state file; a scheduler that reads it in the gap between this
    loop's final probe and the state file being removed sees a live pid,
    stands down, and its task is left with nobody to drive it. ``cmd_loop``
    supplies a callback that re-probes while holding the same lock the
    scheduler takes, so the two decisions cannot interleave. A veto clears
    the maintenance latch and re-probes immediately (no sleep) -- the work
    that vetoed the exit is the work the next cycle fires on. That is the one
    path here that skips the sleep, and it cannot spin: re-arming the latch
    needs a maintenance drive to be launched *and* completed, and every launch
    is followed by a sleep, so two vetoes can never be adjacent.

    Also stops when ``should_stop()`` is true, or after ``max_ticks`` fires
    (both injectable for tests; in production neither is set). ``max_ticks``
    additionally bounds *cycles*, so a test whose probe never returns
    ``actionable`` cannot spin forever. On exit it drains any still-running
    drives so their results/errors are recorded and notified, then flushes
    pending notification posts.

    A drive that raises is recorded as ``last_error`` and the loop
    continues -- one bad drive must not take the daemon down. Notifications
    run via ``asyncio.to_thread`` and never raise (the notifier swallows its
    own errors), so a slow or failing Slack post never stalls or crashes the
    loop.
    """
    if notifier is None:
        notifier = NullNotifier()

    launched = 0
    completed = 0
    cycles = 0
    # True once an idle-triggered maintenance drive has *completed*. Armed by
    # that drive's completion, disarmed by any sign of work. Set on failure
    # too: a maintenance drive that keeps crashing must not pin the daemon
    # open forever -- the error is recorded and notified instead.
    maintenance_done = False
    exit_reason = EXIT_STOP_REQUESTED
    in_flight: list[_Fire] = []
    notif_tasks: list[asyncio.Task[Any]] = []

    def _schedule_update(thread: str | None, text: str) -> None:
        notif_tasks.append(
            asyncio.create_task(asyncio.to_thread(notifier.update, thread, text))
        )

    async def _pulse_skip(reason: str) -> None:
        """Keep the heartbeat rhythm alive on a cycle that fires nothing.

        Model-free by construction: a plain Slack POST, never a drive, so
        visibility on a busy queue costs nothing. Awaited (not scheduled)
        so the pulse lands before the cycle sleeps -- the notifier never
        raises, and ``to_thread`` keeps a slow POST off the event loop.
        """
        await asyncio.to_thread(
            notifier.heartbeat,
            skip_text(launched, now(), reason, len(in_flight)),
        )

    def _prune_notifs() -> None:
        """Drop completed notification tasks so the list cannot grow
        unbounded over a long-running daemon (otherwise one update task per
        completed drive accumulates forever). Each task wraps a notifier call
        that swallows its own errors; we still retrieve any result so asyncio
        does not warn about an un-retrieved task."""
        for t in [t for t in notif_tasks if t.done()]:
            notif_tasks.remove(t)
            try:
                t.exception()  # retrieve; the wrapped call never raises
            except asyncio.CancelledError:  # pragma: no cover - never cancelled
                pass

    def _record_completion(fire: _Fire, outcome: Any, *, is_error: bool) -> None:
        nonlocal completed, maintenance_done
        completed += 1
        # Arm auto-stop only if this maintenance drive is still the *newest*
        # fire. A maintenance drive is only ever launched with nothing in
        # flight, so a higher `launched` means real work was fired while it
        # ran -- and that work dirtied memory the tail has not swept yet.
        # Arming here would exit without a second maintenance pass.
        #
        # `fire.maintenance` is load-bearing too, and the cost of requiring it
        # is one extra fire per drain: an *actionable* drive that empties the
        # queue was told to run the same tail itself (`drive_prompt` is the
        # same for every fire), but we observe a session exiting, not what it
        # chose to do, never its tail. Arming from "the drive finished and
        # the next probe says idle" would arm identically for a drive that
        # died early, stopping the daemon with team memory unswept. One
        # no-op-when-fresh session buys an observed stop.
        if fire.maintenance and fire.fire_no == launched:
            maintenance_done = True
        if is_error:
            log.warning("autodrive drive failed: %s", outcome)
            record_tick(
                state_file,
                tick_count=completed,
                at=now(),
                error=f"{type(outcome).__name__}: {outcome}",
                in_flight=len(in_flight),
            )
            _schedule_update(fire.thread, error_text(fire.fire_no, outcome))
        else:
            stop_reason = getattr(outcome, "stop_reason", None)
            record_tick(
                state_file,
                tick_count=completed,
                at=now(),
                stop_reason=stop_reason,
                cost_usd=getattr(outcome, "cost_usd", None),
                in_flight=len(in_flight),
            )
            log.info(
                "autodrive drive %d done (stop_reason=%s)",
                completed, stop_reason,
            )
            _schedule_update(
                fire.thread, completion_text(fire.fire_no, outcome)
            )

    def _reap_done() -> None:
        """Account for any drives that finished since the last pass. Done
        before each fire so the in-flight gauge and completion records stay
        fresh without waiting for the final drain."""
        for fire in [f for f in in_flight if f.task.done()]:
            in_flight.remove(fire)
            exc = fire.task.exception()
            if exc is not None:
                _record_completion(fire, exc, is_error=True)
            else:
                _record_completion(fire, fire.task.result(), is_error=False)

    while True:
        _reap_done()
        _prune_notifs()
        if max_ticks is not None and launched >= max_ticks:
            exit_reason = EXIT_MAX_TICKS
            break
        if should_stop is not None and should_stop():
            exit_reason = EXIT_STOP_REQUESTED
            break
        if max_ticks is not None and cycles >= max_ticks:
            # Cycle bound: a probe that never says "actionable" must not
            # spin a bounded test forever.
            exit_reason = EXIT_MAX_TICKS
            break
        cycles += 1

        # --- queue probe (ADR 0010): decide whether to spend a drive ---
        verdict = await asyncio.to_thread(probe, cfg)
        is_maintenance = False
        if verdict == QUEUE_IDLE:
            if maintenance_done and not in_flight:
                # Queue drained AND the idle-maintenance tail has run. There
                # is nothing left for this daemon to do; stop rather than
                # hold a process open on an empty journal -- but only once
                # the veto agrees, under the same lock a scheduler takes.
                # `to_thread` for the same reason the probe above uses it:
                # the veto blocks on a flock and then runs a full sweep, and
                # neither belongs on the event loop -- notification posts are
                # still draining, and a contended lock or a network-backed
                # journal makes "briefly" untrue.
                if confirm_exit is None or await asyncio.to_thread(confirm_exit):
                    exit_reason = EXIT_DRAINED
                    break
                # Work landed after the probe. Whoever queued it saw our pid
                # and stood down, so exiting now would strand it.
                log.info(
                    "autodrive: drained exit vetoed -- work queued during "
                    "the stop; staying up"
                )
                maintenance_done = False
                continue
            if in_flight:
                # A drive is still finishing; let it settle before deciding.
                log.info("autodrive: queue idle, waiting on in-flight drive")
                await _pulse_skip(SKIP_WAITING)
                await sleep(cfg.interval_seconds)
                continue
            # First idle cycle: fire the maintenance drive (its prompt ends
            # with compact-idle + sweep-memory) and arm the auto-stop.
            is_maintenance = True
            log.info("autodrive: queue idle -- firing maintenance drive")
        elif verdict == QUEUE_BUSY:
            # A live session owns the in-flight task; a fire would only
            # sweep, see busy, and exit. Skip it and keep the daemon alive.
            maintenance_done = False
            log.info("autodrive: queue busy, skipping fire")
            await _pulse_skip(SKIP_BUSY)
            await sleep(cfg.interval_seconds)
            continue
        else:
            maintenance_done = False

        launched += 1
        # Post the heartbeat first so its `ts` is the thread handle the
        # completion update replies under. to_thread keeps a slow Slack POST
        # off the event loop; the notifier never raises.
        thread = await asyncio.to_thread(
            notifier.heartbeat,
            heartbeat_text(launched, now(), len(in_flight) + 1),
        )
        task = asyncio.create_task(run_drive(cfg, backend=backend))
        in_flight.append(
            _Fire(
                task=task,
                thread=thread,
                fire_no=launched,
                maintenance=is_maintenance,
            )
        )
        record_fire(
            state_file,
            fire_count=launched,
            at=now(),
            in_flight=len(in_flight),
        )
        log.info("autodrive fire %d launched", launched)

        if max_ticks is not None and launched >= max_ticks:
            exit_reason = EXIT_MAX_TICKS
            break
        if should_stop is not None and should_stop():
            exit_reason = EXIT_STOP_REQUESTED
            break
        await sleep(cfg.interval_seconds)

    # Drain: wait for every still-running drive so its result is recorded
    # before the daemon exits. Errors are captured (not raised) so one bad
    # drive cannot mask the others.
    pending = list(in_flight)
    in_flight.clear()
    if pending:
        results = await asyncio.gather(
            *(f.task for f in pending), return_exceptions=True
        )
        for fire, res in zip(pending, results):
            if isinstance(res, BaseException):
                _record_completion(fire, res, is_error=True)
            else:
                _record_completion(fire, res, is_error=False)

    if exit_reason == EXIT_DRAINED:
        # The heartbeat rhythm is the health signal, so its disappearance
        # would otherwise be indistinguishable from a crash. Say plainly
        # that the daemon stopped on purpose.
        log.info("autodrive stopping: queue drained after %d drives", launched)
        await asyncio.to_thread(
            notifier.heartbeat, drained_text(launched, now())
        )

    # Flush any pending notification posts before returning, so a stop never
    # drops an in-flight drive's final status. Errors are swallowed.
    if notif_tasks:
        await asyncio.gather(*notif_tasks, return_exceptions=True)
    return launched


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

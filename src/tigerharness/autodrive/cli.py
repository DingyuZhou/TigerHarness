"""``tigerharness autodrive`` -- a periodic, vendor-agnostic journal driver.

Sub-commands::

    tigerharness autodrive start   [--interval N] [--driver P] [--backend B] ...
    tigerharness autodrive status
    tigerharness autodrive stop
    tigerharness autodrive _loop --state-file PATH   (internal; the daemon body)

``start`` spawns a detached background process that runs the hidden
``_loop`` command; ``_loop`` drives the journal on the configured interval
(see :mod:`tigerharness.autodrive.runner`). State lives in a single JSON
file under the journal root, which ``status`` reads and ``stop`` clears.

Read the module docstring of ``runner`` for *why* this exists and when it
is safe to run.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import logging
import os
import shutil
import signal
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .._logging import configure_cli_logging
from ..journal.paths import default_journal_root
from ..journal.scaffold import resolve_default_persona
from ..slack_bridge import notify_health
from .notifier import build_notifier
from .settings import (
    AUTOSTART_ENV,
    DM_SENTINEL,
    DRIVER_ENV,
    INTERVAL_ENV,
    MAX_BUDGET_ENV,
    NOTIFY_CHANNEL_ENV,
    NOTIFY_ENV,
    SLACK_NOTIFY_CHANNEL_ENV,
    Settings,
)
from .runner import (
    DEFAULT_BACKEND,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_NOTIFY,
    DEFAULT_PERMISSION_MODE,
    MIN_INTERVAL_SECONDS,
    QUEUE_IDLE,
    AutodriveConfig,
    clamp_interval,
    clear_state,
    config_from_state,
    config_to_dict,
    default_prompt,
    is_running,
    log_path,
    probe_queue,
    read_state,
    run_loop,
    state_path,
    utcnow_iso,
    write_state,
)

#: Exit code from ``cmd_start`` meaning "a daemon was already up, so nothing
#: was started". Distinct from 2 (bad argument) so :func:`ensure_running` can
#: treat it as success -- the invariant it wants is "a daemon is running",
#: and one already running satisfies it.
RC_ALREADY_RUNNING = 1

log = logging.getLogger(__name__)

#: Advisory lock file guarding the check-and-spawn critical section. Separate
#: from the state file because the state file is rewritten (tmp + rename)
#: constantly, and ``flock`` follows the *inode* -- locking a file that gets
#: replaced under you locks nothing.
LOCK_FILE_NAME = ".autodrive.lock"

#: Turn-scoped Slack markers the bridge injects into a persona's subprocess
#: (``slack_bridge/bridge.py:_with_thread_env``). They must NOT reach the
#: detached daemon.
#:
#: This is not hypothetical: ``journal defer`` is the flagship auto-start
#: trigger and it runs *inside* a Slack turn, so an unscrubbed env would pin
#: the daemon -- and every drive it ever spawns, for as long as it lives -- to
#: that one thread. Three things then go wrong quietly: the journal claim gate
#: reads the marker and refuses the drive as "a Slack session" (only the
#: prompt's ``--allow-api-drive`` saves it); a ``journal defer`` from inside a
#: drive records the stale thread/channel as its origin, so the completion
#: notice threads back into an unrelated conversation; and drive-transcript
#: suppression registers against that conversation, hiding it from the memory
#: sweep. Scrubbed at the spawn boundary rather than in the drive prompt,
#: because a boundary holds whether or not a model reads its instructions.
#:
#: Slack *credential* vars are deliberately NOT scrubbed -- the daemon needs
#: them to post its own heartbeats.
TURN_SCOPED_ENV_VARS = (
    "TIGERHARNESS_SLACK_THREAD_TS",
    "TIGERHARNESS_SLACK_CHANNEL",
)


def daemon_env(base: Mapping[str, str], journal_root: Path) -> dict[str, str]:
    """The environment the detached daemon runs with (and hence every drive
    it spawns): the caller's env minus the turn-scoped Slack markers, plus
    the journal pin so a custom ``--journal-dir`` or a personal journal
    resolves to exactly the one this daemon manages."""
    env = {k: v for k, v in base.items() if k not in TURN_SCOPED_ENV_VARS}
    env["TIGERHARNESS_JOURNAL_DIR"] = str(journal_root)
    return env


def lock_path(state_root: Path) -> Path:
    return state_root / LOCK_FILE_NAME


@contextmanager
def start_lock(state_root: Path) -> Iterator[None]:
    """Serialize ``start``'s check-and-spawn against every other process in
    the same team (ADR 0010).

    Without this the guard was a read-then-write: two ``journal new`` calls in
    the same second both saw "not running" and both spawned a daemon. Once
    scheduling *auto-starts* the daemon that race stops being theoretical, so
    the whole decision runs under an exclusive advisory lock.

    **Two** critical sections take this lock, and they are two halves of one
    invariant -- "exactly one daemon per team, and never zero while work is
    queued":

    * ``cmd_start``/``ensure_running`` -- check the state file, then spawn.
    * ``cmd_loop``'s ``confirm_exit`` -- re-probe, then clear the state file.

    They must exclude each other or a scheduler reads a live pid from a daemon
    that is already committed to exiting, stands down, and strands its task.

    Blocking (not ``LOCK_NB``) on purpose: a waiter is delayed and then
    correctly observes whichever decision won. Keep both sections short --
    ``start``'s is a few small writes plus a ``Popen`` that returns
    immediately; ``confirm_exit``'s is one journal sweep, a bounded file walk.
    Anything genuinely slow does not belong in here, because every scheduler
    in the team queues behind it. The kernel releases the lock if the holder
    dies, so a crash mid-start (or mid-exit) cannot wedge the team.
    """
    path = lock_path(state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


# ----- context resolution -----

def _resolve_journal_root(args: argparse.Namespace) -> Path:
    """Journal root for the command: an explicit ``--journal-dir`` wins,
    else the standard resolution (env override / cwd-as-team / XDG).

    The result is always **absolute**, anchored to the invoking command's
    cwd. This matters because ``start`` detaches a daemon that runs in a
    *different* cwd (the team root) and is pinned to this journal via the
    ``--state-file`` argv and ``TIGERHARNESS_JOURNAL_DIR``; a relative root
    would re-resolve against the child's cwd and miss the journal entirely.
    We anchor (not ``resolve``) so symlinked paths keep their identity."""
    override = getattr(args, "journal_dir", None)
    if override:
        root = Path(override).expanduser()
    else:
        root = default_journal_root()
    return root if root.is_absolute() else (Path.cwd() / root)


def _team_root_for(journal_root: Path) -> Path | None:
    """The team root owning this journal, or ``None`` for a personal
    (non-team) journal. Convention: ``<team>/journal`` with a
    ``configs/personas.yaml`` sibling -- exactly what ``default_journal_root``
    produces when cwd is a team directory."""
    parent = journal_root.parent
    if journal_root.name == "journal" and (
        parent / "configs" / "personas.yaml"
    ).is_file():
        return parent
    return None


def _state_root(args: argparse.Namespace) -> Path:
    """Filesystem anchor for the single-instance state file -- the lock.

    Team-scoped by design (one autodrive per team): when the command is
    invoked from a team root (cwd has ``configs/personas.yaml``), the lock
    anchors to that team's canonical ``<team>/journal`` *regardless of any
    ``--journal-dir`` override*, so a second ``start`` anywhere in the same
    team resolves to the SAME state file, sees the live pid, and is refused.

    For a personal (non-team) journal there is no team to scope to, so the
    lock stays under the resolved journal root, as before. Only the lock
    location is team-canonical; the *driven* journal is still
    :func:`_resolve_journal_root` and may point elsewhere via
    ``--journal-dir``.

    Detection keys off the invoking cwd (not the resolved journal): a custom
    ``--journal-dir`` must not be able to slip a second daemon past the guard
    while standing in the same team root.
    """
    cwd = Path.cwd()
    if (cwd / "configs" / "personas.yaml").is_file():
        return cwd / "journal"
    return _resolve_journal_root(args)


# ----- detached spawn / kill seams (injected in tests) -----

def cgroup_scope_prefix(env: Mapping[str, str] | None = None) -> list[str]:
    """A ``systemd-run`` prefix that puts the daemon in its **own** cgroup,
    or ``[]`` when that is not available.

    ``start_new_session=True`` detaches the process *session*, but a cgroup
    is inherited and only systemd can move a process out of one. So a
    daemon auto-started from inside a long-lived service -- the Slack
    bridge is the case that bit us -- lands in that service's cgroup and
    shares its memory accounting. A drive that spikes memory then gets the
    whole unit OOM-killed: daemon, drives, *and* the bridge that was only
    ever the launcher. Placing the daemon in a transient scope under
    ``user.slice`` breaks that shared fate.

    ``--collect`` reaps the scope when it exits so repeated starts cannot
    leave failed units behind. ``--quiet`` keeps systemd's own chatter out
    of the daemon log.

    **``--scope`` preserves the pid** -- systemd-run registers the transient
    unit and then ``execve``s the command in its own process rather than
    forking a child. Verified on the deployment host: the pid ``Popen``
    returns is the daemon's own, its pgid equals its pid, and its cgroup is
    ``.../app.slice/run-pNNN-iNNN.scope``. That is load-bearing, not
    incidental -- every pid check downstream (``is_running``, the pid in
    ``.autodrive.json``, :func:`kill_process_group` signalling the group)
    assumes the spawned pid *is* the daemon. Swap ``--scope`` for
    ``--service-type``/``--unit`` and all of that silently breaks.

    No caller-supplied value is interpolated into the argv: the prefix is a
    fixed literal list, and ``env`` is only ever *tested* for a key. A
    hostile ``XDG_RUNTIME_DIR`` can flip the decision, never inject an
    argument.

    Degrades to ``[]`` -- plain, in-cgroup spawn, exactly the old behaviour
    -- when there is no ``systemd-run`` or no user manager to talk to (a
    container, a non-systemd host, CI). Detection is deliberately
    conservative: a wrong "yes" makes the daemon fail to start at all,
    while a wrong "no" only forfeits the isolation.
    """
    src = os.environ if env is None else env
    if not shutil.which("systemd-run"):
        return []
    if not src.get("XDG_RUNTIME_DIR"):
        # No per-user runtime dir means no user bus to register a scope on.
        return []
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect"]


def spawn_loop_process(
    state_file: Path, *, cwd: str, log_file: Path, env: dict[str, str]
) -> int:  # pragma: no cover - spawns a real detached process
    """Launch the hidden ``_loop`` as a detached background process and
    return its pid. ``start_new_session=True`` puts it in its own process
    group so ``stop`` can signal the whole tree; stdout/stderr append to
    the log file; ``env`` pins the journal the spawned drives target.

    Wrapped in :func:`cgroup_scope_prefix` where systemd allows it, so the
    daemon does not share an OOM fate with whatever launched it."""
    log_fh = open(log_file, "a", encoding="utf-8")
    proc = subprocess.Popen(
        cgroup_scope_prefix(env) + [
            sys.executable,
            "-m",
            "tigerharness.autodrive",
            "_loop",
            "--state-file",
            str(state_file),
        ],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return proc.pid


def kill_process_group(pid: int) -> None:  # pragma: no cover - real signal
    """Best-effort SIGTERM to the daemon's whole process group, so an
    in-flight ``claude -p`` child dies with it. A vanished process is not
    an error (already stopped)."""
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


# ----- commands -----

def cmd_start(
    args: argparse.Namespace,
    *,
    spawn: Callable[..., int] | None = None,
    now: Callable[[], str] = utcnow_iso,
    quiet: bool = False,
) -> int:
    """Start the team's daemon. ``quiet`` collapses the operator banner to a
    single line -- what :func:`ensure_running` wants, since its caller is a
    ``journal`` command whose own output should stay readable.

    ``spawn`` defaults to ``None`` and resolves to the module-level
    :func:`spawn_loop_process` *at call time*, not at ``def`` time. That
    distinction is load-bearing, and it was learned the hard way. Written as
    ``spawn=spawn_loop_process`` the default binds at import, so patching
    ``cli.spawn_loop_process`` silently does nothing -- the seam this
    module's docstring advertises did not exist for any caller that omitted
    the argument, and :func:`ensure_running` is exactly such a caller.

    The consequence was not theoretical. A leaked ``AUTOSTART`` env var let
    the *test suite* reach the auto-start hook, and because the seam could
    not be closed from outside, every affected test spawned a real detached
    daemon that outlived pytest and fired real drives forever. Thirty of
    them were found alive at once. Late binding is what lets
    ``tests/conftest.py`` bolt that door shut for the whole suite.
    """
    spawn = spawn_loop_process if spawn is None else spawn
    journal_root = _resolve_journal_root(args)
    journal_root.mkdir(parents=True, exist_ok=True)
    # The lock (state file) is team-canonical so the guard is one-per-team,
    # even when --journal-dir redirects the *driven* journal elsewhere.
    state_root = _state_root(args)
    state_root.mkdir(parents=True, exist_ok=True)
    sfile = state_path(state_root)
    team_root = _team_root_for(journal_root)
    settings = Settings(team_root=team_root)

    # Every knob: flag > process env > team configs/.env > built-in default.
    try:
        raw_interval = args.interval
        if raw_interval is None:
            raw_interval = settings.number(INTERVAL_ENV)
        interval = clamp_interval(
            DEFAULT_INTERVAL_SECONDS if raw_interval is None
            else float(raw_interval)
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    driver = args.driver or settings.get(DRIVER_ENV)
    if driver is None and team_root is not None:
        driver = resolve_default_persona(team_root)
    max_budget = args.max_budget
    if max_budget is None:
        max_budget = settings.number(MAX_BUDGET_ENV)
    notify = args.notify or settings.get(NOTIFY_ENV) or DEFAULT_NOTIFY
    if notify not in ("slack", "none"):
        print(
            f"error: {NOTIFY_ENV}={notify!r} is not 'slack' or 'none'.",
            file=sys.stderr,
        )
        return 2
    # Notify channel resolution: flag > TIGERHARNESS_AUTODRIVE_NOTIFY_CHANNEL
    # > SLACK_NOTIFY_CHANNEL > operator DM (None), each layer reading process
    # env before the team .env. Any layer may say "dm" to force the DM.
    notify_channel = settings.notify_channel(args.notify_channel)

    cwd = str(team_root if team_root is not None else journal_root.parent)
    prompt = args.prompt if args.prompt else default_prompt(driver)
    cfg = AutodriveConfig(
        interval_seconds=interval,
        driver=driver,
        backend=args.backend,
        model=args.model,
        max_budget_usd=max_budget,
        permission_mode=args.permission_mode,
        prompt=prompt,
        cwd=cwd,
        notify=notify,
        notify_channel=notify_channel,
        journal_root=str(journal_root),
    )
    logf = log_path(state_root)
    # Pin the journal the spawned drives target and drop the caller's
    # turn-scoped Slack markers (see TURN_SCOPED_ENV_VARS -- auto-start is
    # normally triggered from inside a Slack turn), then run the daemon in
    # `cwd` (the team root) so the persona's CLAUDE.md / team detection /
    # memory attribution all line up.
    child_env = daemon_env(os.environ, journal_root)

    # The whole decision -- "is one already up?" through "spawn it" -- runs
    # under the team lock, so concurrent schedulers can never both spawn.
    with start_lock(state_root):
        running, state = is_running(sfile)
        if running:
            assert state is not None
            if quiet:
                # Auto-start's normal, uninteresting case: the daemon is
                # already doing its job. Log it; don't nag the operator.
                log.info(
                    "autodrive already running (pid %s); nothing to start",
                    state.get("pid"),
                )
            else:
                print(
                    f"autodrive already running (pid {state.get('pid')}) for "
                    "this team. Stop it first: tigerharness autodrive stop",
                    file=sys.stderr,
                )
            return RC_ALREADY_RUNNING

        # Write the state file BEFORE spawning so the child can read its
        # config the instant it starts; fill in the pid right after.
        state = {
            **config_to_dict(cfg),
            "pid": None,
            "started_at": now(),
            "fire_count": 0,
            "last_fire_at": None,
            "in_flight": 0,
            "tick_count": 0,
            "last_tick_at": None,
            "stop_requested": False,
            "last_stop_reason": None,
            "last_cost_usd": None,
            "last_error": None,
        }
        write_state(sfile, state)
        pid = spawn(sfile, cwd=cwd, log_file=logf, env=child_env)
        fresh = read_state(sfile) or state
        fresh["pid"] = pid
        write_state(sfile, fresh)
    log.info(
        "autodrive started pid=%s interval=%ss backend=%s driver=%s",
        pid, int(interval), cfg.backend, driver,
    )

    if quiet:
        print(
            f"autodrive auto-started (pid {pid}); checking the queue every "
            f"{int(interval)}s until it drains."
        )
        return 0

    print(
        f"autodrive started (pid {pid}) -- firing a drive every "
        f"{int(interval)}s (overlap allowed) via backend {cfg.backend!r}, "
        f"driver {driver or '(none)'}."
    )
    print(f"  state: {sfile}")
    print(f"  log:   {logf}")
    if cfg.notify == "slack":
        target = cfg.notify_channel or "operator DM"
        print(f"  notify: slack -> {target}")
    else:
        print("  notify: none (muted; use `autodrive status` for health)")
    if cfg.max_budget_usd is None:
        print(
            "  note:  no --max-budget set. claude -p bills the "
            "subscription TODAY, but set a per-drive cap before that "
            "changes."
        )
    print("  stop:  tigerharness autodrive stop")
    return 0


def ensure_running(
    journal_root: Path,
    *,
    start: Callable[..., int] = cmd_start,
    settings: Settings | None = None,
) -> bool:
    """Make sure the team's autodrive daemon is up after work was queued.

    This is the auto-start half of ADR 0010: a persona that defers or
    schedules a task should not also need a human to press play. Called by
    ``journal new`` / ``defer`` / ``materialize`` / ``answer`` **after** the
    queue write succeeds.

    Three properties matter more than the happy path:

    - **Opt-in.** A no-op unless ``TIGERHARNESS_AUTODRIVE_AUTOSTART`` is set
      in the process env or the team's ``configs/.env``. Auto-start is only
      safe while ``claude -p`` bills the subscription, and the harness ships
      to deployments we cannot see (see ADR 0010 / docs/autodrive.md).
    - **Never fatal.** Any failure logs and returns False. The task is
      already safely on disk; losing the daemon must not lose the task, and
      a scheduling command must not start failing because a daemon did not.
    - **Idempotent.** "Already running" is success -- the invariant is "a
      daemon is up", and the lock makes concurrent callers converge on one.

    Returns True when a daemon is running as a result of this call.
    """
    cfg = Settings(team_root=_team_root_for(journal_root)) \
        if settings is None else settings
    if not cfg.autostart:
        return False
    args = argparse.Namespace(
        journal_dir=str(journal_root),
        interval=None,
        driver=None,
        backend=DEFAULT_BACKEND,
        model=None,
        max_budget=None,
        permission_mode=DEFAULT_PERMISSION_MODE,
        prompt=None,
        notify=None,
        notify_channel="",
    )
    try:
        rc = int(start(args, quiet=True))
    except Exception as exc:
        log.warning(
            "autodrive auto-start failed (%s: %s); the task is queued -- "
            "start the driver by hand with `tigerharness autodrive start`",
            type(exc).__name__, exc,
        )
        return False
    if rc == 0:
        log.info("autodrive auto-started for %s", journal_root)
        return True
    if rc == RC_ALREADY_RUNNING:
        return True
    log.warning(
        "autodrive auto-start refused (exit %s); the task is still queued", rc
    )
    return False


def _print_state_anchor(sfile: Path) -> None:
    print(f"  read:         {sfile}")
    print("                (team-canonical: one autodrive per team, so")
    print("                 --journal-dir does not move this anchor)")


def cmd_status(args: argparse.Namespace) -> int:
    # Read from the team-canonical lock (same anchor `start` wrote), so a
    # muted operator standing anywhere in the team still sees daemon health.
    state_root = _state_root(args)
    sfile = state_path(state_root)
    # `--journal-dir` cannot move that anchor: a daemon started inside this
    # team keeps its state at the team's journal even while driving another
    # root, so honouring the flag here would report `stopped` for a daemon
    # that is genuinely running. Name the file read instead of answering
    # silently about a journal the operator did not ask about.
    misaimed = bool(getattr(args, "journal_dir", None)) and (
        _resolve_journal_root(args) != state_root
    )
    state = read_state(sfile)
    # Rendered before the early return on purpose: the counter belongs to the
    # notifier, not to daemon liveness, and notify.py is used outside
    # autodrive entirely. Note the anchor differs from the line above --
    # the sidecar belongs to the *driven* journal, so it honours
    # --journal-dir, while the lock stays team-canonical.
    health_lines = notify_health.status_lines(_resolve_journal_root(args))
    if state is None:
        print("autodrive: stopped (no state file)")
        if misaimed:
            _print_state_anchor(sfile)
        for line in health_lines:
            print(line)
        return 0
    running, _ = is_running(sfile)
    label = "running" if running else "stopped (stale state file)"
    # After SIGKILL / OOM / reboot nothing rewrites the persisted counters, so
    # `in_flight` keeps whatever the daemon last wrote. Label it instead of
    # zeroing it: `in_flight: 1` at the moment of death says how it died.
    in_flight_suffix = (
        "(running now)" if running else "(last recorded, daemon not running)"
    )
    notify = state.get("notify", DEFAULT_NOTIFY)
    notify_channel = state.get("notify_channel")
    notify_target = (
        f"slack -> {notify_channel or 'operator DM'}"
        if notify == "slack"
        else "none (muted)"
    )
    print(f"autodrive: {label}")
    if misaimed:
        _print_state_anchor(sfile)
    if not running:
        print("  note:         counters below are frozen at the daemon's")
        print("                last write; nothing is running now.")
    print(f"  pid:          {state.get('pid')}")
    print(f"  journal:      {state.get('journal_root') or '(unknown)'}")
    print(f"  interval:     {int(float(state.get('interval_seconds', 0)))}s")
    print(f"  backend:      {state.get('backend')}")
    print(f"  driver:       {state.get('driver') or '(none)'}")
    print(f"  max_budget:   {state.get('max_budget_usd')}")
    print(f"  notify:       {notify_target}")
    print(f"  started_at:   {state.get('started_at')}")
    print(f"  fire_count:   {state.get('fire_count', 0)} (drives launched)")
    print(f"  last_fire_at: {state.get('last_fire_at') or '(none yet)'}")
    print(f"  in_flight:    {state.get('in_flight', 0)} {in_flight_suffix}")
    print(f"  done_count:   {state.get('tick_count', 0)} (drives completed)")
    print(f"  last_done_at: {state.get('last_tick_at') or '(none yet)'}")
    if state.get("last_stop_reason"):
        print(f"  last_stop:    {state.get('last_stop_reason')}")
    if state.get("last_error"):
        print(f"  last_error:   {state.get('last_error')}")
    for line in health_lines:
        print(line)
    return 0


def cmd_stop(
    args: argparse.Namespace,
    *,
    kill: Callable[[int], None] = kill_process_group,
) -> int:
    # Stop the team's one daemon via the same team-canonical lock `start`
    # used, so `stop` works from anywhere in the team regardless of the
    # driven journal's --journal-dir.
    sfile = state_path(_state_root(args))
    state = read_state(sfile)
    if state is None:
        print("autodrive: not running (no state file).")
        return 0

    running, _ = is_running(sfile)
    pid = state.get("pid")
    if running and isinstance(pid, int):
        # Set the cooperative flag first (a clean between-ticks exit), then
        # signal the group so an in-flight drive dies promptly too.
        state["stop_requested"] = True
        write_state(sfile, state)
        kill(pid)
        log.info("autodrive stopped pid=%s", pid)
        print(f"autodrive stopped (pid {pid}).")
    else:
        print("autodrive: not running (cleared stale state file).")
    clear_state(sfile)
    return 0


def cmd_loop(
    args: argparse.Namespace,
    *,
    runner: Callable[..., Any] = run_loop,
) -> int:
    """The detached daemon body. Reads config from the state file, runs
    the drive loop, and clears the state file on a clean exit."""
    # Give the daemon's own INFO logging somewhere to land. `start` points
    # this process's stdout+stderr at `.autodrive.log`, but nothing ever
    # configured a handler, so `logging.lastResort` dropped everything below
    # WARNING and the log stayed empty -- which is why a six-fire rescue
    # stampede left no forensics at all and had to be reconstructed from
    # file timestamps and the kernel OOM log. INFO by default (like
    # `notify`) because this is an unattended process nobody is watching:
    # its log IS the record. `TIGERHARNESS_LOG_LEVEL` still overrides.
    configure_cli_logging(default="INFO")
    sfile = Path(args.state_file)
    state = read_state(sfile)
    if state is None:
        print(
            f"autodrive _loop: no state file at {sfile}; nothing to do.",
            file=sys.stderr,
        )
        return 1
    cfg = config_from_state(state)
    # The lock lives beside the state file, by construction: `start` derives
    # both from the same team-canonical state root.
    state_root = sfile.parent
    surrendered = False

    def should_stop() -> bool:
        cur = read_state(sfile)
        return cur is None or bool(cur.get("stop_requested"))

    def confirm_exit() -> bool:
        """Commit to the drained-queue exit atomically against auto-start.

        :func:`ensure_running` decides "a daemon is already up" by reading
        this state file under the team lock. Between the loop's last probe
        and this file being removed there is a gap in which a ``journal
        defer`` sees a live pid, stands down -- and then we exit, leaving its
        task queued with nothing to drive it. Silent, and it strands the task
        until the next queue write, which is the one failure that would make
        the whole self-driving story untrustworthy.

        Taking the same lock and re-probing collapses that gap: either the
        scheduler's write is already visible here (veto, stay up and work
        it), or we remove the state file first and the scheduler -- blocked
        on the lock, reading after us -- finds no daemon and starts a fresh
        one. Removing it here rather than after the loop is what makes the
        handover atomic, so the trailing clear below must not fire too.
        """
        nonlocal surrendered
        with start_lock(state_root):
            if probe_queue(cfg) != QUEUE_IDLE:
                return False
            clear_state(sfile)
            surrendered = True
            return True

    # Build the notifier from the persisted config. ``build_notifier`` never
    # raises: muted (``notify=none``) or unloadable creds degrade to a no-op
    # notifier, never a crash that would take the daemon down.
    notifier = build_notifier(cfg.notify, cfg.notify_channel)
    asyncio.run(
        runner(
            cfg, sfile, should_stop=should_stop, notifier=notifier,
            confirm_exit=confirm_exit,
        )
    )
    if not surrendered:
        # Only clear a file we still own. A surrendered exit already removed
        # it under the lock, and a successor daemon may have written its own
        # in the meantime -- deleting *that* would strand the successor.
        clear_state(sfile)
    return 0


# ----- parser / entry point -----

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tigerharness autodrive",
        description=(
            "Periodically drive the journal via the agent SDK "
            "(vendor-agnostic; default backend claude_p)."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def _add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--journal-dir",
            default=None,
            help="Journal root (default: env / cwd-as-team / XDG state).",
        )

    p_start = sub.add_parser("start", help="Start the autodrive daemon.")
    _add_common(p_start)
    p_start.add_argument(
        "--interval",
        type=float,
        default=None,
        help=(
            "Seconds between fires (floor "
            f"{int(MIN_INTERVAL_SECONDS)}). Default: {INTERVAL_ENV} from the "
            f"env / team configs/.env, else {int(DEFAULT_INTERVAL_SECONDS)} "
            "(10 min)."
        ),
    )
    p_start.add_argument(
        "--driver",
        default=None,
        help=(
            f"Persona to attribute work to. Default: {DRIVER_ENV}, else the "
            "team default_persona."
        ),
    )
    p_start.add_argument(
        "--backend",
        default=DEFAULT_BACKEND,
        help=f"agent_sdk backend name (default {DEFAULT_BACKEND!r}).",
    )
    p_start.add_argument(
        "--model", default=None, help="Model override (backend-specific)."
    )
    p_start.add_argument(
        "--max-budget",
        type=float,
        default=None,
        dest="max_budget",
        help=(
            "Per-drive USD cap (passed to the backend). Strongly advised. "
            f"Default: {MAX_BUDGET_ENV} from the env / team configs/.env."
        ),
    )
    p_start.add_argument(
        "--permission-mode",
        default=DEFAULT_PERMISSION_MODE,
        help=(
            "Backend permission mode for unattended edits "
            f"(default {DEFAULT_PERMISSION_MODE!r})."
        ),
    )
    p_start.add_argument(
        "--prompt",
        default=None,
        help="Override the built-in 'drive the journal' instruction.",
    )
    p_start.add_argument(
        "--notify",
        choices=("slack", "none"),
        default=None,
        help=(
            "Daemon-level notifications: 'slack' posts a heartbeat per fire "
            "plus a threaded status/summary on completion; 'none' mutes "
            f"(default: {NOTIFY_ENV} from the env / team configs/.env, else "
            f"{DEFAULT_NOTIFY!r}). Muted still has `autodrive status` as the "
            "pull-based health check."
        ),
    )
    p_start.add_argument(
        "--notify-channel",
        default="",
        dest="notify_channel",
        help=(
            "Slack channel id for daemon events (e.g. C0ABC123). Resolution: "
            f"this flag > {NOTIFY_CHANNEL_ENV} > {SLACK_NOTIFY_CHANNEL_ENV} > "
            f"operator DM. Pass {DM_SENTINEL!r} at any layer to force the DM "
            "when a team-wide channel key is set."
        ),
    )
    p_start.set_defaults(func=cmd_start)

    p_status = sub.add_parser("status", help="Show daemon status.")
    _add_common(p_status)
    p_status.set_defaults(func=cmd_status)

    p_stop = sub.add_parser("stop", help="Stop the daemon.")
    _add_common(p_stop)
    p_stop.set_defaults(func=cmd_stop)

    # ``_loop`` is the internal daemon body, not for direct use; no help
    # text so it stays out of the user-facing listing.
    p_loop = sub.add_parser("_loop")
    p_loop.add_argument("--state-file", required=True)
    p_loop.set_defaults(func=cmd_loop)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

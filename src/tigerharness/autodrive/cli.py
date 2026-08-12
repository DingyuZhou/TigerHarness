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
import signal
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from ..journal.paths import default_journal_root
from ..journal.scaffold import resolve_default_persona
from .notifier import build_notifier
from .settings import (
    AUTOSTART_ENV,
    DRIVER_ENV,
    INTERVAL_ENV,
    MAX_BUDGET_ENV,
    NOTIFY_CHANNEL_ENV,
    NOTIFY_ENV,
    Settings,
)
from .runner import (
    DEFAULT_BACKEND,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_NOTIFY,
    DEFAULT_PERMISSION_MODE,
    MIN_INTERVAL_SECONDS,
    AutodriveConfig,
    clamp_interval,
    clear_state,
    config_from_state,
    config_to_dict,
    default_prompt,
    is_running,
    log_path,
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

#: Advisory lock file guarding the check-and-spawn critical section. Separate
#: from the state file because the state file is rewritten (tmp + rename)
#: constantly, and ``flock`` follows the *inode* -- locking a file that gets
#: replaced under you locks nothing.
LOCK_FILE_NAME = ".autodrive.lock"

log = logging.getLogger(__name__)


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

    Blocking (not ``LOCK_NB``) on purpose: the critical section contains no
    blocking calls -- a few small writes and a ``Popen`` that returns
    immediately -- so a waiter is delayed by milliseconds and then correctly
    observes the daemon its rival just started. The kernel releases the lock
    if the holder dies, so a crash mid-start cannot wedge the team.
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

def spawn_loop_process(
    state_file: Path, *, cwd: str, log_file: Path, env: dict[str, str]
) -> int:  # pragma: no cover - spawns a real detached process
    """Launch the hidden ``_loop`` as a detached background process and
    return its pid. ``start_new_session=True`` puts it in its own process
    group so ``stop`` can signal the whole tree; stdout/stderr append to
    the log file; ``env`` pins the journal the spawned drives target."""
    log_fh = open(log_file, "a", encoding="utf-8")
    proc = subprocess.Popen(
        [
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
    spawn: Callable[..., int] = spawn_loop_process,
    now: Callable[[], str] = utcnow_iso,
    quiet: bool = False,
) -> int:
    """Start the team's daemon. ``quiet`` collapses the operator banner to a
    single line -- what :func:`ensure_running` wants, since its caller is a
    ``journal`` command whose own output should stay readable."""
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
    # Notify channel resolution: flag > env > team .env > operator DM (None).
    notify_channel = args.notify_channel or settings.get(NOTIFY_CHANNEL_ENV)

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
    # Pin the journal the spawned drives target (so a custom --journal-dir,
    # or a personal journal, resolves to exactly the one autodrive manages),
    # then run the daemon in `cwd` (the team root) so the persona's CLAUDE.md
    # / team detection / memory attribution all line up.
    child_env = {**os.environ, "TIGERHARNESS_JOURNAL_DIR": str(journal_root)}

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


def cmd_status(args: argparse.Namespace) -> int:
    # Read from the team-canonical lock (same anchor `start` wrote), so a
    # muted operator standing anywhere in the team still sees daemon health.
    sfile = state_path(_state_root(args))
    state = read_state(sfile)
    if state is None:
        print("autodrive: stopped (no state file)")
        return 0
    running, _ = is_running(sfile)
    label = "running" if running else "stopped (stale state file)"
    notify = state.get("notify", DEFAULT_NOTIFY)
    notify_channel = state.get("notify_channel")
    notify_target = (
        f"slack -> {notify_channel or 'operator DM'}"
        if notify == "slack"
        else "none (muted)"
    )
    print(f"autodrive: {label}")
    print(f"  pid:          {state.get('pid')}")
    print(f"  interval:     {int(float(state.get('interval_seconds', 0)))}s")
    print(f"  backend:      {state.get('backend')}")
    print(f"  driver:       {state.get('driver') or '(none)'}")
    print(f"  max_budget:   {state.get('max_budget_usd')}")
    print(f"  notify:       {notify_target}")
    print(f"  started_at:   {state.get('started_at')}")
    print(f"  fire_count:   {state.get('fire_count', 0)} (drives launched)")
    print(f"  last_fire_at: {state.get('last_fire_at') or '(none yet)'}")
    print(f"  in_flight:    {state.get('in_flight', 0)} (running now)")
    print(f"  done_count:   {state.get('tick_count', 0)} (drives completed)")
    print(f"  last_done_at: {state.get('last_tick_at') or '(none yet)'}")
    if state.get("last_stop_reason"):
        print(f"  last_stop:    {state.get('last_stop_reason')}")
    if state.get("last_error"):
        print(f"  last_error:   {state.get('last_error')}")
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
    sfile = Path(args.state_file)
    state = read_state(sfile)
    if state is None:
        print(
            f"autodrive _loop: no state file at {sfile}; nothing to do.",
            file=sys.stderr,
        )
        return 1
    cfg = config_from_state(state)

    def should_stop() -> bool:
        cur = read_state(sfile)
        return cur is None or bool(cur.get("stop_requested"))

    # Build the notifier from the persisted config. ``build_notifier`` never
    # raises: muted (``notify=none``) or unloadable creds degrade to a no-op
    # notifier, never a crash that would take the daemon down.
    notifier = build_notifier(cfg.notify, cfg.notify_channel)
    asyncio.run(
        runner(cfg, sfile, should_stop=should_stop, notifier=notifier)
    )
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
            "Slack channel id for daemon events (e.g. C0ABC123). Default: "
            "operator DM. Resolution: this flag > env "
            f"{NOTIFY_CHANNEL_ENV} > DM."
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

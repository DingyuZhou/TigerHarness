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
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from ..journal.paths import default_journal_root
from ..journal.scaffold import resolve_default_persona
from .runner import (
    DEFAULT_BACKEND,
    DEFAULT_PERMISSION_MODE,
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

log = logging.getLogger(__name__)


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
) -> int:
    journal_root = _resolve_journal_root(args)
    journal_root.mkdir(parents=True, exist_ok=True)
    sfile = state_path(journal_root)

    running, state = is_running(sfile)
    if running:
        assert state is not None
        print(
            f"autodrive already running (pid {state.get('pid')}). Stop it "
            "first: tigerharness autodrive stop",
            file=sys.stderr,
        )
        return 1

    try:
        interval = clamp_interval(float(args.interval))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    team_root = _team_root_for(journal_root)
    driver = args.driver
    if driver is None and team_root is not None:
        driver = resolve_default_persona(team_root)

    cwd = str(team_root if team_root is not None else journal_root.parent)
    prompt = args.prompt if args.prompt else default_prompt(driver)
    cfg = AutodriveConfig(
        interval_seconds=interval,
        driver=driver,
        backend=args.backend,
        model=args.model,
        max_budget_usd=args.max_budget,
        permission_mode=args.permission_mode,
        prompt=prompt,
        cwd=cwd,
    )

    # Write the state file BEFORE spawning so the child can read its
    # config the instant it starts; fill in the pid right after.
    state = {
        **config_to_dict(cfg),
        "pid": None,
        "started_at": now(),
        "last_tick_at": None,
        "tick_count": 0,
        "stop_requested": False,
        "last_stop_reason": None,
        "last_cost_usd": None,
        "last_error": None,
    }
    write_state(sfile, state)
    logf = log_path(journal_root)
    # Pin the journal the spawned drives target (so a custom --journal-dir,
    # or a personal journal, resolves to exactly the one autodrive manages),
    # then run the daemon in `cwd` (the team root) so the persona's CLAUDE.md
    # / team detection / memory attribution all line up.
    child_env = {**os.environ, "TIGERHARNESS_JOURNAL_DIR": str(journal_root)}
    pid = spawn(sfile, cwd=cwd, log_file=logf, env=child_env)
    fresh = read_state(sfile) or state
    fresh["pid"] = pid
    write_state(sfile, fresh)
    log.info(
        "autodrive started pid=%s interval=%ss backend=%s driver=%s",
        pid, int(interval), cfg.backend, driver,
    )

    print(
        f"autodrive started (pid {pid}) -- driving every "
        f"{int(interval)}s via backend {cfg.backend!r}, "
        f"driver {driver or '(none)'}."
    )
    print(f"  state: {sfile}")
    print(f"  log:   {logf}")
    if cfg.max_budget_usd is None:
        print(
            "  note:  no --max-budget set. claude -p bills the "
            "subscription TODAY, but set a per-drive cap before that "
            "changes."
        )
    print("  stop:  tigerharness autodrive stop")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    journal_root = _resolve_journal_root(args)
    sfile = state_path(journal_root)
    state = read_state(sfile)
    if state is None:
        print("autodrive: stopped (no state file)")
        return 0
    running, _ = is_running(sfile)
    label = "running" if running else "stopped (stale state file)"
    print(f"autodrive: {label}")
    print(f"  pid:          {state.get('pid')}")
    print(f"  interval:     {int(float(state.get('interval_seconds', 0)))}s")
    print(f"  backend:      {state.get('backend')}")
    print(f"  driver:       {state.get('driver') or '(none)'}")
    print(f"  max_budget:   {state.get('max_budget_usd')}")
    print(f"  started_at:   {state.get('started_at')}")
    print(f"  last_tick_at: {state.get('last_tick_at') or '(none yet)'}")
    print(f"  tick_count:   {state.get('tick_count', 0)}")
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
    journal_root = _resolve_journal_root(args)
    sfile = state_path(journal_root)
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

    asyncio.run(runner(cfg, sfile, should_stop=should_stop))
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
        default=600.0,
        help="Seconds between drives (floor 60; default 600 = 10 min).",
    )
    p_start.add_argument(
        "--driver",
        default=None,
        help="Persona to attribute work to (default: team default_persona).",
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
        help="Per-drive USD cap (passed to the backend). Strongly advised.",
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

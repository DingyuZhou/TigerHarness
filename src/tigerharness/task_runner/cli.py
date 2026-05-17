"""User-facing CLI: `assign`, `list`, `cancel`, `show`, `logs`.

Driven via:
    python -m tigerharness.task_runner <subcommand> [args]

Design picks:

- `assign` fork-execs the runner in a detached child
  (`start_new_session=True`) so the loop survives session exit.
- Job ids are 8-hex-char `secrets.token_hex(4)`. Commands accept any
  unambiguous prefix (git-short-SHA style).
- `cancel` writes a sentinel file the runner polls each iteration.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

from . import personas, registry, runner


# ---------------------------------------------------------------------------
# `assign`
# ---------------------------------------------------------------------------

def _parse_iters(raw: str) -> int:
    """Validate --iters. Returns 0 for 'forever'."""
    s = str(raw).strip().lower()
    if s in ("forever", "0", "inf", "infinite"):
        return 0
    try:
        n = int(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"--iters must be int or 'forever'; got {raw!r}"
        ) from e
    if n < 1:
        raise argparse.ArgumentTypeError(
            f"--iters must be >= 1 or 'forever'; got {n}"
        )
    return n


def cmd_assign(args: argparse.Namespace) -> int:
    try:
        persona = personas.resolve(args.persona)
    except KeyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        persona.build_config()
    except FileNotFoundError as e:
        print(f"error: persona config build failed: {e}", file=sys.stderr)
        return 2

    if args.prompt:
        prompt_text = args.prompt
    else:
        prompt_path = Path(args.prompt_file).expanduser()
        if not prompt_path.exists():
            print(f"error: prompt file not found: {prompt_path}", file=sys.stderr)
            return 2
        prompt_text = prompt_path.read_text()

    if not prompt_text.strip():
        print("error: prompt is empty", file=sys.stderr)
        return 2

    iters = args.iters if isinstance(args.iters, int) else _parse_iters(args.iters)
    compact_every = max(0, args.compact_every)

    store = registry.JobStore(registry.default_state_path())
    job_id = registry.new_job_id()
    job_dir = store.job_dir(job_id)
    store.prompt_path(job_id).write_text(prompt_text)

    meta = registry.JobMeta(
        job_id=job_id,
        persona=persona.name,
        prompt_chars=len(prompt_text),
        max_iters=iters,
        compact_every=compact_every,
        continuation=(args.continuation or ""),
        name=(args.name or "").strip(),
        notify=not args.quiet,
        slack_thread_ts=(args.thread or "").strip(),
        early_exit=args.early_exit,
        stuck_timeout=max(0, int(args.stuck_timeout)),
        cwd=str(persona.cwd),
        started_at=time.time(),
        status="pending",
        pid=None,
        current_iter=0,
        session_id="",
        last_update=time.time(),
    )
    store.set(meta)

    cmd = [sys.executable, "-m", "tigerharness.task_runner", "_run", job_id]
    env = os.environ.copy()
    pkg_parent = str(Path(__file__).resolve().parent.parent.parent)
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{pkg_parent}{os.pathsep}{existing_pp}" if existing_pp
        else pkg_parent
    )
    stdout_log = (job_dir / "stdout.log").open("ab")
    stderr_log = (job_dir / "stderr.log").open("ab")
    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=stdout_log,
        stderr=stderr_log,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        cwd=str(persona.cwd),
    )
    # Close our copies — the child inherited the fds via Popen.
    stdout_log.close()
    stderr_log.close()

    meta.pid = proc.pid
    store.set(meta)

    cap_label = str(iters) if iters > 0 else "forever (cancel to stop)"
    if meta.stuck_timeout > 0:
        stuck_label = f"{meta.stuck_timeout}s ({meta.stuck_timeout // 60} min)"
    else:
        stuck_label = "disabled"
    print(f"Assigned: {job_id}")
    print(f"  persona:        {persona.name}")
    print(f"  iters:          {cap_label}")
    print(f"  compact_every:  {compact_every}")
    print(f"  stuck_timeout:  {stuck_label}")
    print(f"  cwd:            {persona.cwd}")
    print(f"  pid:            {proc.pid}")
    print(f"  log:            {store.run_log(job_id)}")
    print()
    print(f"Cancel:   python -m tigerharness.task_runner cancel {job_id}")
    print(f"Show:     python -m tigerharness.task_runner show {job_id}")
    print(f"Tail log: python -m tigerharness.task_runner logs {job_id} --follow")
    return 0


# ---------------------------------------------------------------------------
# `list`
# ---------------------------------------------------------------------------

def _fmt_ago(t: float) -> str:
    d = max(0, time.time() - t)
    if d < 60:
        return f"{int(d)}s ago"
    if d < 3600:
        return f"{int(d // 60)}m ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    return f"{int(d // 86400)}d ago"


def cmd_list(args: argparse.Namespace) -> int:
    store = registry.JobStore(registry.default_state_path())
    jobs = store.all()
    if args.format == "json":
        print(json.dumps(
            {jid: asdict(m) for jid, m in jobs.items()},
            indent=2,
        ))
        return 0

    items = sorted(jobs.values(), key=lambda m: m.started_at, reverse=True)
    if not args.all:
        items = [m for m in items if m.status in ("pending", "running")]
    if not items:
        print("No active jobs." if not args.all else "No jobs.")
        print("(use --all to include finished jobs)" if not args.all else "")
        return 0

    cols = ["ID", "persona", "iter", "status", "name", "started"]
    rows = []
    for m in items:
        cap = str(m.max_iters) if m.max_iters > 0 else "inf"
        rows.append([
            m.job_id,
            m.persona,
            f"{m.current_iter}/{cap}",
            m.status,
            (m.name or "")[:30],
            _fmt_ago(m.started_at),
        ])
    widths = [
        max(len(c), *(len(r[i]) for r in rows))
        for i, c in enumerate(cols)
    ]
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)))
    return 0


# ---------------------------------------------------------------------------
# `cancel`
# ---------------------------------------------------------------------------

def cmd_cancel(args: argparse.Namespace) -> int:
    store = registry.JobStore(registry.default_state_path())
    try:
        meta = store.resolve_prefix(args.job_id)
    except KeyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if meta.status not in ("pending", "running"):
        print(f"job {meta.job_id} is already {meta.status}; nothing to cancel.")
        return 0

    store.request_cancel(meta.job_id)
    print(
        f"Cancel requested for {meta.job_id} (persona={meta.persona}, "
        f"iter={meta.current_iter}). Runner will exit at the next "
        f"iteration boundary."
    )

    if args.signal and meta.pid:
        try:
            os.kill(meta.pid, 15)  # SIGTERM
            print(f"  also sent SIGTERM to pid {meta.pid}.")
        except ProcessLookupError:
            print(f"  pid {meta.pid} no longer alive -- flag is enough.")
    return 0


# ---------------------------------------------------------------------------
# `amend`
# ---------------------------------------------------------------------------

def cmd_amend(args: argparse.Namespace) -> int:
    store = registry.JobStore(registry.default_state_path())
    try:
        meta = store.resolve_prefix(args.job_id)
    except KeyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    changed: list[str] = []

    if args.continuation is not None:
        old = (meta.continuation or "(default)")[:60]
        meta.continuation = args.continuation
        changed.append(f"continuation: {old!r} -> {args.continuation[:60]!r}")

    if args.thread is not None:
        old = meta.slack_thread_ts or "(none)"
        meta.slack_thread_ts = args.thread.strip()
        changed.append(f"slack_thread_ts: {old} -> {meta.slack_thread_ts or '(none)'}")

    if args.stuck_timeout is not None:
        old = meta.stuck_timeout
        meta.stuck_timeout = max(0, int(args.stuck_timeout))
        changed.append(
            f"stuck_timeout: {old}s -> {meta.stuck_timeout}s "
            f"({'disabled' if meta.stuck_timeout == 0 else 'enabled'})"
        )

    if not changed:
        print("nothing to amend (pass --continuation, --thread, "
              "and/or --stuck-timeout).",
              file=sys.stderr)
        return 1

    meta.last_update = time.time()
    store.set(meta)
    print(f"Amended job {meta.job_id}:")
    for c in changed:
        print(f"  {c}")
    return 0


# ---------------------------------------------------------------------------
# `continue`
# ---------------------------------------------------------------------------

def cmd_continue(args: argparse.Namespace) -> int:
    """Continue a finished/early-exited/cancelled task for more iterations."""
    store = registry.JobStore(registry.default_state_path())
    try:
        meta = store.resolve_prefix(args.job_id)
    except KeyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if meta.status == "running":
        print(f"error: job {meta.job_id} is still running (iter {meta.current_iter}). "
              f"Use `amend` to change it, or `cancel` first.", file=sys.stderr)
        return 1
    if meta.status == "pending":
        print(f"error: job {meta.job_id} hasn't started yet.", file=sys.stderr)
        return 1

    try:
        persona = personas.resolve(meta.persona)
    except KeyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        persona.build_config()
    except FileNotFoundError as e:
        print(f"error: persona config build failed: {e}", file=sys.stderr)
        return 2

    extra_iters = args.iters if isinstance(args.iters, int) else _parse_iters(args.iters)
    if extra_iters <= 0:
        print("error: --iters must be >= 1 for continue", file=sys.stderr)
        return 1

    start_iter = meta.current_iter
    meta.max_iters = meta.current_iter + extra_iters
    meta.status = "pending"
    meta.error = None

    if args.continuation is not None:
        meta.continuation = args.continuation

    if args.stuck_timeout is not None:
        meta.stuck_timeout = max(0, int(args.stuck_timeout))

    meta.last_update = time.time()
    store.set(meta)

    cancel_flag = store.cancel_flag(meta.job_id)
    if cancel_flag.exists():
        cancel_flag.unlink()

    session_id = meta.session_id or ""
    cmd = [
        sys.executable, "-m", "tigerharness.task_runner", "_run", meta.job_id,
        "--resume-session", session_id,
        "--start-iter", str(start_iter),
    ]
    env = os.environ.copy()
    pkg_parent = str(Path(__file__).resolve().parent.parent.parent)
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{pkg_parent}{os.pathsep}{existing_pp}" if existing_pp
        else pkg_parent
    )
    job_dir = store.job_dir(meta.job_id)
    stdout_log = (job_dir / "stdout.log").open("ab")
    stderr_log = (job_dir / "stderr.log").open("ab")
    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=stdout_log,
        stderr=stderr_log,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        cwd=str(persona.cwd),
    )
    # Close our copies — the child inherited the fds via Popen.
    stdout_log.close()
    stderr_log.close()

    meta.pid = proc.pid
    store.set(meta)

    if meta.stuck_timeout > 0:
        stuck_label = f"{meta.stuck_timeout}s ({meta.stuck_timeout // 60} min)"
    else:
        stuck_label = "disabled"
    print(f"Continuing: {meta.job_id}")
    print(f"  resuming from iter:  {meta.current_iter}")
    print(f"  additional iters:    {extra_iters}")
    print(f"  new max_iters:       {meta.max_iters}")
    print(f"  stuck_timeout:       {stuck_label}")
    print(f"  session:             {session_id[:16]}..." if session_id else "  session:             (new)")
    print(f"  pid:                 {proc.pid}")
    print()
    print(f"Cancel:   python -m tigerharness.task_runner cancel {meta.job_id}")
    print(f"Show:     python -m tigerharness.task_runner show {meta.job_id}")
    print(f"Tail log: python -m tigerharness.task_runner logs {meta.job_id} --follow")
    return 0


# ---------------------------------------------------------------------------
# `show`
# ---------------------------------------------------------------------------

def cmd_show(args: argparse.Namespace) -> int:
    store = registry.JobStore(registry.default_state_path())
    try:
        meta = store.resolve_prefix(args.job_id)
    except KeyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(json.dumps(asdict(meta), indent=2))
    rp = store.result_path(meta.job_id)
    if rp.exists():
        text = rp.read_text()
        preview = text[:800] + (
            "\n... (truncated -- read full at: "
            f"{rp})" if len(text) > 800 else ""
        )
        print()
        print("--- latest result ---")
        print(preview)
    return 0


# ---------------------------------------------------------------------------
# `logs`
# ---------------------------------------------------------------------------

def cmd_logs(args: argparse.Namespace) -> int:
    store = registry.JobStore(registry.default_state_path())
    try:
        meta = store.resolve_prefix(args.job_id)
    except KeyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    log_path = store.run_log(meta.job_id)
    if args.follow:
        cmd = ["tail", "-n", "+1", "-F", str(log_path)]
        try:
            subprocess.run(cmd)
        except KeyboardInterrupt:
            pass
        return 0

    if not log_path.exists():
        print("(no log yet -- job hasn't started writing)")
        return 0
    print(log_path.read_text())
    return 0


# ---------------------------------------------------------------------------
# `_run` (internal: the detached child enters here)
# ---------------------------------------------------------------------------

def cmd_run_internal(args: argparse.Namespace) -> int:
    import asyncio
    return asyncio.run(runner.run_job(
        args.job_id,
        resume_session_id=getattr(args, "resume_session", "") or "",
        start_iter=getattr(args, "start_iter", 0) or 0,
    ))


# ---------------------------------------------------------------------------
# `personas`
# ---------------------------------------------------------------------------

def cmd_personas(args: argparse.Namespace) -> int:  # noqa: ARG001
    for p in personas.list_personas():
        print(f"{p.name:>16}  ({', '.join(p.aliases)})")
        print(f"                  cwd: {p.cwd}")
        print(f"                  {p.description}")
        print()
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tigerharness-task-runner",
        description="Iterative task assignment for personas.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # assign
    a = sub.add_parser("assign", help="Spawn a new background task.")
    a.add_argument(
        "--to", "--persona", dest="persona", required=True,
        help="Persona to assign to (must be registered).",
    )
    g = a.add_mutually_exclusive_group(required=True)
    g.add_argument("--prompt", help="Task instructions (inline text).")
    g.add_argument("--prompt-file", help="Read task instructions from this file.")
    a.add_argument("--iters", type=_parse_iters, default=1,
                   help="Iterations to run. Pass `forever` (or `0`) for "
                        "truly unbounded. Default: 1.")
    a.add_argument("--compact-every", type=int, default=5,
                   help="Run /compact every N iterations. 0 disables. Default: 5.")
    a.add_argument("--continuation", default="",
                   help="User message sent on iterations after the first.")
    a.add_argument("--name", default="",
                   help="Short human label (free text). Shown in `list`.")
    a.add_argument("--quiet", "-q", action="store_true",
                   help="Suppress the Slack DM notification on job completion.")
    a.add_argument("--thread", default="",
                   help="Slack thread_ts to post completion DM into.")
    a.add_argument("--early-exit", action="store_true", default=False,
                   help="Enable early exit when 3 consecutive iterations "
                        "produce genuinely nothing new.")
    a.add_argument("--stuck-timeout", type=int, default=1200,
                   help="Initial wait before the stuck-watchdog starts "
                        "its first check (seconds; default 1200 = 20 min). "
                        "After the first check, the watchdog rechecks "
                        "every 10 min using heuristic + agent hybrid. "
                        "On STUCK it cancels the iteration; the runner "
                        "then continues with the next iter unless this "
                        "was the last. Pass 0 to disable.")
    a.set_defaults(func=cmd_assign)

    # list
    l = sub.add_parser("list", help="Show jobs (default: only active).")
    l.add_argument("--all", action="store_true", help="Include finished jobs.")
    l.add_argument("--format", choices=["table", "json"], default="table")
    l.set_defaults(func=cmd_list)

    # cancel
    c = sub.add_parser("cancel", help="Cancel a job by id-prefix.")
    c.add_argument("job_id")
    c.add_argument("--signal", action="store_true",
                   help="Also SIGTERM the runner pid.")
    c.set_defaults(func=cmd_cancel)

    # amend
    am = sub.add_parser("amend", help="Update a running job's continuation or thread.")
    am.add_argument("job_id")
    am.add_argument("--continuation",
                    help="New user message for iterations after the current one.")
    am.add_argument("--thread",
                    help="Slack thread_ts to post completion DM into.")
    am.add_argument("--stuck-timeout", type=int, default=None,
                    help="New stuck-watchdog timeout in seconds; 0 disables.")
    am.set_defaults(func=cmd_amend)

    # show
    s = sub.add_parser("show", help="Print one job's full state + latest result.")
    s.add_argument("job_id")
    s.set_defaults(func=cmd_show)

    # logs
    lg = sub.add_parser("logs", help="Print or tail a job's run.log.")
    lg.add_argument("job_id")
    lg.add_argument("--follow", "-f", action="store_true")
    lg.set_defaults(func=cmd_logs)

    # continue
    cn = sub.add_parser("continue", help="Continue a finished/early-exited task.")
    cn.add_argument("job_id")
    cn.add_argument("--iters", type=_parse_iters, required=True,
                    help="Number of additional iterations to run.")
    cn.add_argument("--continuation", default=None,
                    help="Optional new continuation prompt for the extra iterations.")
    cn.add_argument("--stuck-timeout", type=int, default=None,
                    help="Override the stuck-watchdog timeout (seconds) "
                         "for the additional iterations; 0 disables. If "
                         "omitted, keeps the existing setting from the "
                         "original assign.")
    cn.set_defaults(func=cmd_continue)

    # personas
    pn = sub.add_parser("personas", help="List available personas.")
    pn.set_defaults(func=cmd_personas)

    # _run (hidden)
    r = sub.add_parser("_run", help=argparse.SUPPRESS)
    r.add_argument("job_id")
    r.add_argument("--resume-session", dest="resume_session", default="")
    r.add_argument("--start-iter", dest="start_iter", type=int, default=0)
    r.set_defaults(func=cmd_run_internal)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

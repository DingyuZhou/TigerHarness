# task-runner

Fire-and-forget iterative task execution for Claude Code agents.

## What it does

Drives a named persona through N resume-based Claude turns, with
periodic `/compact` to keep context tight. Runs as a detached child
process so it survives session exit, SSH disconnect, or IDE close.

## Architecture

```
CLI (assign/cancel/list/show/continue/amend)
    |
    v
JobStore (filesystem: ~/.local/state/tigerharness-tasks/)
    |
    v
Runner (detached child process)
    |-- open_session (agent-sdk claude_p backend)
    |-- iteration loop: prompt -> dispatch -> log -> compact
    |-- early-exit classifier (opt-in)
    |-- Slack notifier (job start/end DMs)
    v
Result: run.log + result.txt + lab_notebooks/tasks/<slug>.md
```

## Key modules

| Module | Purpose |
|---|---|
| `cli.py` | argparse CLI: assign, list, cancel, show, logs, amend, continue, personas |
| `runner.py` | The async iteration loop (runs in detached child) |
| `personas.py` | Config-driven persona registry (register_persona, resolve) |
| `registry.py` | Filesystem-backed job state (JobStore, JobMeta) |
| `notifier.py` | Best-effort Slack DMs for job lifecycle events |
| `stuck_watchdog.py` | Per-iteration stuck-detection (heuristic + agent fallback) with SIGTERM/SIGKILL escalation |

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `TIGERHARNESS_STATE_DIR` | `~/.local/state/tigerharness-tasks/` | Job registry + per-job state |
| `TIGERHARNESS_PERSONAS_DIR` | (none) | Directory of `<name>.md` prompt files |
| `TIGERHARNESS_SLACK_BRIDGE_DIR` | (none) | For notify CLI path in thread notice |
| `TIGERHARNESS_SLACK_ENV` | (none) | Path to .env with SLACK_BOT_TOKEN |

## Persona registration

```python
from tigerharness.task_runner.personas import register_persona

register_persona(
    "researcher",
    aliases=("researcher", "rs"),
    cwd="/path/to/project",
    prompt_file="researcher",       # reads <PERSONAS_DIR>/researcher.md
    permission_mode="bypassPermissions",
    disallowed_tools=["Bash(sudo:*)"],
    description="Research agent",
)
```

Or via environment:
```bash
export TIGERHARNESS_PERSONAS_DIR=./personas
# Then any file ./personas/<name>.md is auto-discoverable
```

## Usage

```bash
# Assign a task
python -m tigerharness.task_runner assign \
    --to researcher --prompt "Research X" --iters 10

# List running jobs
python -m tigerharness.task_runner list

# Cancel
python -m tigerharness.task_runner cancel <id-prefix>

# Continue a finished job
python -m tigerharness.task_runner continue <id> --iters 5
```

## Early exit

Pass `--early-exit` to enable automatic stopping. After each iteration:
1. A classifier determines if the agent thinks it's DONE, CONTINUING, or ERROR.
2. If DONE, a novelty classifier compares output to the previous iteration.
3. Three consecutive DONE+STALE triggers exit.
4. Three consecutive ERROR also triggers exit.

## Stuck-watchdog

Every iteration runs under a per-turn watchdog (default first-check at
`--stuck-timeout` seconds, 1200 = 20 min by default; rechecks every 10
min). The watchdog uses a two-stage **hybrid**: a deterministic
heuristic over the `/proc` tree first, an agent fallback for unclear
cases. On a STUCK verdict it cancels the *iteration* (not the whole
job) and the runner auto-continues to the next iteration if any remain.

**Flow:**

1. **Initial wait.** Sleep `stuck_timeout` seconds. If the iteration's
   `claude` subprocess returns before then, exit silently.
2. **Check loop (every 10 min after the initial wait):**
   a. Sample claude + its direct `bash` children + each bash subtree's
      CPU delta over a 2 s window.
   b. Run the heuristic verdict:
      - No `bash` children -> `UNCLEAR`.
      - Oldest `bash` > 10 min old -> `STUCK`.
      - Oldest `bash` < 1 min old -> `WORKING`.
      - Middle band: subtree CPU flat -> `STUCK`, advancing -> `WORKING`.
   c. If `UNCLEAR`, fall back to a one-shot agent that uses the *same
      model* as the iteration's persona (config derived from
      `agent_cfg` via `dataclasses.replace`, with `permission_mode=plan`,
      90 s hard timeout). Reply must be `STUCK: ...` or `WORKING: ...`.
      Timeouts / parse failures default to `WORKING`.
   d. `STUCK` -> log `event="escalate"`, post Slack DM
      (`:rotating_light:`), SIGTERM the claude subtree, 5 s grace,
      SIGKILL stragglers.
   e. `WORKING` -> wait 10 min, re-check.
3. **Auto-continue.** When the watchdog escalates, the dispatch wrapper
   raises `StuckWatchdogEscalation`. The runner catches it:
   - More iters remain -> log `iter_stuck` with `action="continuing"`,
     re-open the claude session (resume by `session_id` if available),
     proceed to iter N+1.
   - Final iter -> log `iter_stuck` with `action="ending"`, finish job
     with `status=error`.

The loop has no hard cap -- if both heuristic and agent keep returning
`WORKING`, the iteration runs as long as it needs.

**Knobs:**

- `--stuck-timeout SEC` on `assign` (default `1200`, `0` disables).
- `--stuck-timeout SEC` on `continue` (override for the resumed
  iterations).
- `python -m tigerharness.task_runner amend <id> --stuck-timeout SEC`
  mid-run (takes effect at the next iteration boundary).

The 10-min recheck cadence is fixed; only the initial wait is
user-configurable.

**Cost note:** because the stuck-checker inherits the persona's model
(typically Opus-class), each `UNCLEAR` verdict costs roughly what one
short Opus call costs. Heuristic `STUCK`/`WORKING` verdicts cost
nothing -- the agent only fires on `UNCLEAR`. If this becomes a budget
concern, disable with `--stuck-timeout 0`.

**Agent-side child-process watch.** The default task preamble
(`runner.TASK_PREAMBLE`) instructs the iteration's agent to inspect
its own subprocesses older than 10 min via
`ps -eo pid,ppid,etime,stat,pcpu,command`, kill wedged subtrees,
diagnose root cause, and fix before re-running. The watchdog is the
safety net for cases the agent misses.

## Iteration log

Each job writes a git-trackable markdown log at:
```
<cwd>/lab_notebooks/tasks/<name>--<job_id>.md
```

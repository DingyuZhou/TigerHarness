---
name: assign-task
description: Fire-and-forget a dedicated, iterative Claude task to a named persona. Use when the user asks to "assign", "delegate", "kick off", "spin up", "iterate on", "continue", or "task out" work to a persona for a fixed number of iterations. Also use to list, cancel, continue, or check the status of background tasks.
---

# assign-task

A skill for managing the `task-runner` service -- a fire-and-forget iterative loop that runs a single persona across N resume-based Claude turns, with periodic `/compact` to keep context tight. Runs detached in the background so it survives session exit.

## When to use this skill

Trigger when the user asks anything like:

- "assign <persona> to do X for N iterations"
- "spin up a task to ..."
- "delegate this to <persona> for the next 10 iterations"
- "continue that task for 10 more iterations"
- "what background tasks are running?"
- "kill / cancel / stop the <persona> task"

## How to invoke

```bash
# Assign a task -- inline prompt
python -m tigerharness.task_runner assign \
    --to <persona> \
    --prompt "Your task instructions here" \
    --iters 5 \
    --compact-every 5 \
    --name "task-name"

# Assign -- prompt from file
python -m tigerharness.task_runner assign \
    --to <persona> \
    --prompt-file /path/to/task.md \
    --iters 10

# Assign with early-exit enabled
python -m tigerharness.task_runner assign \
    --to <persona> \
    --prompt "Research X" \
    --iters 50 \
    --early-exit

# Continue a finished task
python -m tigerharness.task_runner continue <job-id> --iters 10

# List active jobs
python -m tigerharness.task_runner list

# Cancel by id-prefix
python -m tigerharness.task_runner cancel <id-prefix>

# Show one job's state
python -m tigerharness.task_runner show <id-prefix>

# Tail the structured log
python -m tigerharness.task_runner logs <id-prefix> --follow

# List available personas
python -m tigerharness.task_runner personas
```

## Args reference (assign)

| Flag | Default | Meaning |
|---|---|---|
| `--to PERSONA` | required | Persona name (must be registered). |
| `--prompt TEXT` | required* | Task instructions inline. *or `--prompt-file` |
| `--prompt-file PATH` | required* | Read instructions from file. |
| `--iters N` | `1` | Iterations. `forever`/`0` = unbounded. |
| `--compact-every K` | `5` | `/compact` every K iters. `0` disables. |
| `--continuation TEXT` | default template | User message for iter 2..N. |
| `--name LABEL` | "" | Free-text label, shown in `list`. |
| `--thread TS` | "" | Slack thread_ts for notification threading. |
| `--early-exit` | `False` | Auto-stop when 3 consecutive stale iterations. |
| `--quiet` | `False` | Suppress Slack notification on completion. |

## Lifecycle

1. CLI writes prompt + registers job in `$TIGERHARNESS_STATE_DIR`.
2. Fork-execs detached child (`start_new_session=True`).
3. Child opens claude session, runs N iterations with periodic compact.
4. On cancel flag, exits cleanly at next iteration boundary.
5. On completion/error, posts Slack notification (unless --quiet).

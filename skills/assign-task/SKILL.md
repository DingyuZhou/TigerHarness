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

## Slack threading -- CRITICAL when assigning from Slack

When you assign a task from inside a Slack thread (i.e. the user's message has a `[bridge-context]` block), you **MUST** pass `--thread <slack_thread_ts>` so that:

1. The task runner posts start/done notifications **into that thread** (not as a new top-level message).
2. The agent receives a threading notice in every iteration prompt, so its proactive DMs (questions, milestones, completion) also land **in that thread**.

Extract the `slack_thread_ts` value from the `[bridge-context]` block at the bottom of the user's message:

```
[bridge-context]
slack_thread_ts: 1779802780.372489    <-- use this value
slack_channel: D0B4L5V7RFG
```

Example:

```bash
python -m tigerharness.task_runner assign \
    --to <persona> \
    --prompt "Your task here" \
    --iters 10 \
    --thread 1779802780.372489
```

If there is **no** `[bridge-context]` block (e.g. you're running from an IDE or terminal), omit `--thread` -- the task runner will create its own notification thread.

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
| `--thread TS` | "" | Slack thread_ts for notification threading. **Always pass this when assigning from a Slack thread** (see above). |
| `--early-exit` | `False` | Auto-stop when 3 consecutive stale iterations. |
| `--stuck-timeout SEC` | `1200` | Stuck-watchdog initial wait (seconds). `0` disables. |
| `--quiet` | `False` | Suppress Slack notification on completion. |

## Args reference (continue)

| Flag | Default | Meaning |
|---|---|---|
| `job_id` | required | Job id (or unambiguous prefix) of a finished/cancelled job. |
| `--iters N` | required | Number of additional iterations. |
| `--continuation TEXT` | (unchanged) | Optional new continuation prompt. |
| `--stuck-timeout SEC` | (unchanged) | Override stuck-watchdog timeout for the continued iterations. |

## Amending a running job

```bash
# Change what the agent gets on the next iteration
python -m tigerharness.task_runner amend <id> --continuation "Focus on X only."

# Change which Slack thread gets the completion DM
python -m tigerharness.task_runner amend <id> --thread 1778713006.341509

# Disable the stuck-watchdog
python -m tigerharness.task_runner amend <id> --stuck-timeout 0
```

Amendments take effect at the next iteration boundary.

## Lifecycle

1. CLI writes prompt + registers job in `$TIGERHARNESS_STATE_DIR`.
2. Fork-execs detached child (`start_new_session=True`).
3. Child opens claude session, runs N iterations with periodic compact.
4. On cancel flag, exits cleanly at next iteration boundary.
5. On completion/error, posts Slack notification (unless --quiet).

## Slack-attached files

When the user uploads a file in the same Slack DM that triggers a task assignment, the slack-bridge downloads it and appends the path to your incoming message (under "Attached files (paths on disk)"). Pass those paths through in `--prompt` so the assignee can `Read` them.

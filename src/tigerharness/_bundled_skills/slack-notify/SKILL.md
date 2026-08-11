---
name: slack-notify
description: Send a Slack message (text or file) from a Claude agent. Use when the agent needs to proactively message a user during a run -- questions, completion notices, blockers, file uploads.
---

# slack-notify

Procedural skill for any Claude agent to message a user via the notify CLI.

## When to use this skill

- Proactive DM during a detached agent run (question, milestone, blocker).
- Post a chart, CSV, or screenshot the agent just generated.
- Post a follow-up inside an existing Slack thread (route via `--thread`).

## Reading the `[bridge-context]` block

When the slack-bridge forwards a message to an agent, it appends:

```
[bridge-context]
slack_thread_ts: 1778702517.507109
slack_channel: D012ABCDEF
```

Use `--thread <slack_thread_ts>` to route follow-ups into the same thread.

## Send a text DM

```bash
python -m tigerharness.slack_bridge.notify text "Your message here"

# Into a specific thread:
python -m tigerharness.slack_bridge.notify text "<message>" \
    --thread 1778702517.507109
```

## Journal-task notifies: `--task` (route to the origin thread)

A journal task materialized from the Slack inbox carries the Operator's
origin thread in its `deferred_origin.json` sidecar (`thread_ts`, plus
`channel` on newer sidecars). For a completion / park / progress notify
about such a task, pass the task id instead of copying thread ids:

```bash
python -m tigerharness.slack_bridge.notify text "<message>" \
    --task <journal-task-id>
```

`--task` resolves the sidecar (looking in `active/`, `done/`, then
`needs_input/` under the journal root) and threads the message into the
recorded origin thread + channel. Precedence: explicit `--thread` /
`--channel` override `--task`; a task without a recorded origin falls
back to the top-level operator DM with a logged warning. `journal
release` prints an `origin thread:` line whenever a task has one -- if
you see it, use `--task`.

## Upload a file

```bash
python -m tigerharness.slack_bridge.notify file --file /tmp/chart.png \
    --comment "Chart caption"

# Into a specific thread:
python -m tigerharness.slack_bridge.notify file --file /tmp/chart.png \
    --thread 1778702517.507109 --comment "as requested"
```

`file` accepts the same `--task` / `--channel` routing as `text`.

## Environment variables

- `SLACK_BOT_TOKEN` -- required
- `SLACK_CEO_USER_ID` or `SLACK_ALLOWED_USER_IDS` -- target user
  (the legacy spelling `ALLOWED_SLACK_USER_IDS` also works)
- `TIGERHARNESS_SLACK_ENV` -- path to .env file (optional)

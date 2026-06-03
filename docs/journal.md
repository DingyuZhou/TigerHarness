# journal

File-based, human-driven subscription backend (Phase 1).

> **Status:** Phase 1 is shipped (this module). The on-disk schema,
> scaffolder, sweep, and CLI are implemented and tested at 100%
> coverage. The `drive-journal` skill is shipped as the canonical
> driver markdown; the `OPERATING.md` template ships at
> `<journal>/OPERATING.md` on first scaffold. Phase 2 (config switch
> to opt into the api backend; unify `task_journal/` and
> `workflow_journal/`) is deferred.

## What it does

Runs agent work through the **interactive** Claude Code app so the
work counts against a monthly subscription instead of token-billed
API usage. Durable state lives on disk in a `journal/` folder; a
human-triggered skill drains the queue continuously per session.

See [`subscription-backend.md`](subscription-backend.md) for the full
design (push-vs-pull rationale, soft-lease semantics, cascade
behaviour, OPERATING.md protocol).

## Architecture

```
You: write a PRD
    |
    v
Scaffolder (CLI `tigerharness journal new` or `journal-new` skill)
    |-- creates journal/active/<task-id>/ from the PRD
    v
journal/ (passive file-based state machine)         <-- source of truth
    ^
    |
    v
Driver (`drive-journal` skill, interactive session)
  1. Lazy sweep of active/ (no AI, no cron, no daemon)
       - archive done/ tasks
       - classify in_progress as fresh-vs-stale (heartbeat = soft lease)
       - summarise actionable counts in-session
  2. Pick ONE actionable task, run to done / blocked / stop
       - reads OPERATING.md
       - appends progress.md as it goes
       - updates status.json (state, heartbeat, sessions, next_action)
  3. Cascade: re-sweep and pick the next; drain the queue in one sitting
```

## Folder layout

```
<journal>/
  OPERATING.md            # vendor-neutral protocol (installed by scaffolder)
  active/
    <task-id>/
      task.md             # the PRD verbatim
      status.json         # the state machine (single source of truth)
      progress.md         # append-only log, human + AI readable
      artifacts/          # whatever the task produces
  done/
    <task-id>/            # finished tasks moved here by the next drive-journal sweep
```

Task-id format: `<YYYYMMDD>-<slug>-<uuid8>`.

- `YYYYMMDD` — UTC date at creation
- `slug` — `slugify(--title or first H1 of PRD, max=40)` (ASCII
  lowercase, hyphen-separated; falls back to `"task"` if the source
  has no usable chars)
- `uuid8` — 8 hex chars from `secrets.token_hex(4)`
  (collision-rare; the scaffolder regenerates once on hit, then
  hard-errors)

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `TIGERHARNESS_JOURNAL_DIR` | resolver (below) | Override the journal root. |
| `TIGERHARNESS_JOURNAL_STUCK_TIMEOUT` | `1800` (30 min) | Heartbeat age past which an `in_progress` task is classified as stale. |

Journal root resolution priority:

1. `TIGERHARNESS_JOURNAL_DIR` (env override)
2. `<cwd>/journal/` if cwd has `configs/personas.yaml`
   (the convention scaffolded by `tigerharness init`)
3. `$XDG_STATE_HOME/tigerharness-journal`
4. `~/.local/state/tigerharness-journal`

## Usage

```bash
# Scaffold a new task from a PRD.
tigerharness journal new \
    --prd brief.md \
    --persona Mitsui \
    --max-sessions 5

# Quick read-only inspect (no archives, no flags).
tigerharness journal list           # table format
tigerharness journal list --format json
tigerharness journal status <task-id>

# The lazy sweep -- side-effecting: archives `done` tasks, classifies
# the rest, prints a summary. The `drive-journal` skill calls this as
# its first action, but you can also run it ad-hoc.
tigerharness journal sweep
tigerharness journal sweep --format json
tigerharness journal sweep --stuck-timeout 600
```

The driver is **skill-only by design**: there is no
`tigerharness journal drive` CLI because a CLI driver would
reintroduce programmatic billing and defeat the subscription model.
Driving only happens inside an interactive Claude Code session.

## status.json schema

See the field-by-field table and state-transition rules in
[`subscription-backend.md` — "status.json — the heart"](subscription-backend.md).
Phase 1 ships `kind=task` only; `kind=workflow` is reserved.

## Skills

- `skills/journal-new/` — scaffolder skill (CLI form is the primary;
  the skill is a thin wrapper).
- `skills/drive-journal/` — driver skill (skill-only, no CLI form).

The driver skill points the interactive session at the on-disk
`OPERATING.md` for the canonical protocol.

## OPERATING.md

The vendor-neutral contract. Lives at `<journal>/OPERATING.md` and is
installed by the scaffolder on first use. The shipped template is in
[`src/tigerharness/journal/operating_template.py`](../src/tigerharness/journal/operating_template.py).
The scaffolder will NOT overwrite a human-customised `OPERATING.md`
on subsequent runs — once you've edited it, it's yours.

## Non-goals

- **Parallelism.** A single human drives serially. Concurrent drivers
  are explicitly out of scope. The heartbeat acts as a soft lease;
  see [`subscription-backend.md` — "How serial execution is
  enforced"](subscription-backend.md) for the race-window discussion.
- **Replacing the api backend.** The two coexist (Phase 2 config
  switch); pick `subscription` (default) or `api` per task.
- **Automating the interactive app.** No keystroke automation. The
  human trigger is the design.

## Related

- [`subscription-backend.md`](subscription-backend.md) — the design.
- [`task-runner.md`](task-runner.md) — the api-backed single-persona
  runner the subscription backend replaces by default.
- [`workflow-runner.md`](workflow-runner.md) — the api-backed
  multi-persona graph runner.

# subscription backend

A file-based, human-driven execution model that runs multi-step agent
work through the **interactive** Claude Code app, so the work counts
against a monthly subscription instead of token-billed API usage.

> **Status:** Design-only. Nothing in this document is implemented
> yet. It is the source of truth for the design; sections describing
> unbuilt pieces are the plan, not a description of shipped behaviour.
> Phases 1 and 2 are the agreed first build; Phase 3 (wiring this in
> behind a runner config switch and unifying the journal folders) is
> deferred.

## Why this exists

The `task-runner` and `workflow-runner` both drive work by spawning
`claude -p --resume` child processes. That is a *programmatic* entry
point: it bills as API token usage, which is expensive and hard to
cap. A monthly Claude subscription, by contrast, covers **interactive**
use of the Claude Code app — a human at the keyboard.

This backend is the bridge: it lets a person get the most out of a
monthly subscription by doing the actual agent work inside an
interactive session, while a file-based journal holds durable,
resume-able state so the work can be driven in bursts and picked up by
any future session (or any other vendor's agent).

It does **not** replace the existing API-based runners. Those stay for
users who want autonomous, parallel throughput and are willing to pay
per token — and for a future where token prices may fall. The two
coexist behind a config switch (Phase 3).

## Push vs pull

| | **API backend** (task/workflow-runner, exists) | **subscription backend** (this doc) |
|---|---|---|
| Auth | API token (paid per token) | Monthly subscription (interactive app) |
| Control flow | **Push** — the runner spawns and supervises agents | **Pull** — a human-driven session pulls work from the journal |
| Speed | Fast, parallel, autonomous | Human-paced, serial |
| Engine | A detached `claude -p` child process | The human, in an interactive session |
| State of truth | Job registry + session ids | The journal files |

The defining inversion: **nothing autonomous runs the work.** The
backend is a passive file-based state machine plus a written protocol.
A human opens an interactive session, points it at the protocol, and
the session does one increment of work and writes its state back.

## What it does

1. You write a PRD describing the task.
2. You run a command (or invoke a skill) that **scaffolds** a journal
   folder from that PRD.
3. A non-AI **watcher** notices the new task and surfaces it to you on
   Slack, alongside the status of everything else in the journal.
4. You open the interactive Claude Code app and invoke the **driver**
   skill, which reads the protocol and processes the journal: pick up
   a task, do one increment, append progress, update state.
5. Continue the same session for more increments, or stop — the state
   is durable on disk either way.
6. When a task reaches `done`, the watcher archives it so the active
   set stays lean.

Single-persona work (the task-runner's niche) and multi-persona work
(the workflow-runner's niche) both live here: a single-persona task is
just a workflow of one step. The interactive session adopts whichever
persona a step calls for.

## Architecture

```
You: write a PRD
    |
    v
Scaffolder (CLI `journal new` / skill)
    |-- creates journal/active/<task-id>/ from the PRD
    v
journal/ (passive file-based state machine)            <-- source of truth
    ^                                   |
    |                                   v
Watcher (CLI `journal watch`)      Driver (interactive session + skill)
  - no AI, costs nothing             - reads OPERATING.md
  - scans active/, detects stuck     - picks a task, does ONE increment
  - archives done/ tasks             - appends progress.md
  - Slack digest to you              - updates status.json (state, heartbeat,
                                       next_action)
```

The watcher and the driver are decoupled: the watcher is cheap Python
bookkeeping that never calls a model; the driver is the only piece
that consumes the subscription, and it only runs when a human starts
it.

## Folder layout

```
teams/<Team>/journal/
    OPERATING.md              # the protocol (committed, vendor-neutral)
    active/
        <task-id>/
            task.md           # your PRD, verbatim
            status.json       # the state machine (single source of truth)
            progress.md       # append-only log, human + AI readable
            artifacts/        # whatever the task produces or references
    done/
        <task-id>/            # finished tasks moved here by the watcher
```

`<task-id>` format mirrors the workflow-runner:
`<YYYYMMDD>-<short-slug>-<8-char-uuid>`, e.g.
`20260602-subscription-backend-7f2a9c14`.

The `active/` and `done/` split is what keeps the journal lean: the
driver only ever reads `active/`, so archiving finished tasks bounds
how much it has to scan and re-read.

`journal/` is a **runtime artifact**, not git-tracked source (the same
treatment `task_journal/` and `workflow_journal/` get). `OPERATING.md`
is the one exception — it is the committed protocol. The team
`.gitignore` should exclude `journal/active/` and `journal/done/` but
keep `journal/OPERATING.md`.

## status.json — the heart

```json
{
  "id": "20260602-subscription-backend-7f2a9c14",
  "title": "Add the subscription backend",
  "kind": "task",
  "state": "in_progress",
  "persona": "Mitsui",
  "iteration": 3,
  "max_iterations": 10,
  "created_at": "2026-06-02T08:00:00Z",
  "updated_at": "2026-06-02T09:15:00Z",
  "next_action": "Resume step 2; last blocker was the missing schema",
  "session_ref": "5e3b8023-d810-49a1-b763-0751a82baa15"
}
```

| Field | Purpose |
|---|---|
| `state` | `pending` → `in_progress` → (`blocked`) → `done` / `failed`. The watcher reads it to decide archive vs. flag. |
| `iteration` / `max_iterations` | Progress counter and a soft ceiling the driver respects. |
| `updated_at` | **Heartbeat.** Every increment bumps it. The watcher flags an `in_progress` task whose heartbeat is older than a threshold as stale. |
| `next_action` | The handoff note. Lets a *fresh* session resume without re-reasoning the whole `progress.md` — this is what makes the journal the memory, not the vendor's session. |
| `session_ref` | Optional Claude session id, so the human can `--resume` the same conversation cheaply. Null is fine; `next_action` + `progress.md` are enough to resume from files alone. |

Two fields carry the design: `updated_at` (stuck detection) and
`next_action` (resume-from-files). `session_ref` is a convenience, not
a dependency — losing it costs nothing but a re-read.

Writes to `status.json` must be **atomic** (write to a temp file, then
rename) so an interrupted session never leaves a half-written state
file.

There is deliberately **no lease / lock field.** Leasing would guard
against two drivers grabbing the same task at once, but parallelism is
a non-goal here and a single human drives serially. `state` +
`updated_at` already answer "is this being worked / is it stale." A
lease can be added later if concurrent drivers ever become real (see
Non-goals).

## OPERATING.md — the protocol

`OPERATING.md` is the instruction file: a vendor-neutral markdown
contract that teaches *any* file-reading agent to drive the journal.
It is the decoupling layer — Claude reads it via the driver skill, but
so could a human or another vendor's agent. It specifies:

- **Where state lives** — the journal path and folder conventions.
- **How to read state** — the `status.json` schema and what each
  `state` value means.
- **The decision procedure**, run on every sweep:
  1. List `active/*/status.json`.
  2. Pick the next actionable task — a `pending` task to start, or an
     `in_progress` task to resume (prefer the one with the oldest
     heartbeat). Skip `blocked` tasks and instead surface them.
  3. Read its `task.md` (PRD) + `next_action` + the tail of
     `progress.md`.
  4. Do **one increment** of real work.
  5. Append what happened to `progress.md`.
  6. Update `status.json`: bump `iteration`, refresh `updated_at`,
     rewrite `next_action`, set `state` if it changed.
- **Stop conditions** — when to mark `done`, when to mark `blocked`
  (and what to write so a human knows what's needed), when to hand the
  turn back rather than manufacture busywork.

The committed `OPERATING.md` is the contract; the driver skill is thin
sugar that says "read `OPERATING.md` and execute it."

## Skills and CLI

Both the scaffolder and the driver are exposed two ways — a CLI
command for scripting and a skill for the interactive app — so you can
work entirely inside Claude Code if you prefer.

| Surface | CLI | Skill | What it does |
|---|---|---|---|
| Scaffold | `tigerharness journal new --prd <file> [--title ...] [--persona ...]` | `journal-new` | Ingest a PRD, create `active/<task-id>/` with `task.md`, a seeded `status.json`, an empty `progress.md`, and `artifacts/`. |
| Drive | — (human-driven only) | `drive-journal` | Read `OPERATING.md` and process the journal per the decision procedure above. |
| Watch | `tigerharness journal watch` | — | Non-AI bookkeeping loop (see below). |
| Inspect | `tigerharness journal list` / `status` | — | Print the journal state as a table / JSON. |

The driver has no CLI form on purpose: a CLI driver would be a
programmatic entry point and defeat the subscription model. Driving
only happens inside an interactive session a human started.

## The watcher

`tigerharness journal watch` is a plain Python loop (run via cron, a
systemd timer, or foregrounded). **It calls no model and costs
nothing.** Responsibilities:

- Scan `active/*/status.json`.
- Move `done` tasks to `journal/done/` to keep `active/` lean.
- Flag stale tasks: `in_progress` with `updated_at` older than
  `stuck_timeout` → report as needing attention.
- Send a Slack digest (reusing the existing slack-bridge / notify CLI),
  e.g. *"2 pending, 1 in_progress, 1 stale (no heartbeat 40m), 1
  awaiting your review."*

Note the contrast with the task-runner's `stuck_watchdog`: that
watchdog can **kill** a wedged subprocess because the API backend owns
one. The subscription watcher owns no process — there is nothing to
kill. Its "stuck" handling is purely **advisory**: it notifies you, and
you decide whether to re-open a session and nudge the task. This keeps
the watcher AI-free and side-effect-light.

| Env var | Default | Purpose |
|---|---|---|
| `TIGERHARNESS_JOURNAL_DIR` | `<team>/journal/` | Journal root the scaffolder, driver, and watcher operate on. |
| `TIGERHARNESS_JOURNAL_STUCK_TIMEOUT` | `1800` (30 min) | Heartbeat age past which the watcher flags a task as stale. |

## The work loop

1. **Write a PRD.** A plain markdown file describing the task — like
   any brief you'd hand a teammate.
2. **Scaffold.** `tigerharness journal new --prd brief.md` (or the
   `journal-new` skill in the app) creates `active/<task-id>/` and
   seeds `status.json` as `pending`.
3. **Get nudged.** The watcher's next Slack digest includes the new
   task.
4. **Drive.** Open Claude Code, invoke `drive-journal`. The session
   picks up the task, does one increment, appends to `progress.md`,
   and updates `status.json` (heartbeat + `next_action`).
5. **Continue or stop.** Keep going in the same session (use
   `--resume` / `session_ref` for cheap continuity) or close it. State
   is durable on disk regardless.
6. **Archive.** Once a task is `done`, the watcher moves it to
   `done/`.

For multi-persona work the session adopts a persona per step, works
that step to its stop condition, records the handoff in `next_action`,
and the next sweep (same or fresh session) picks up the next step.
Serial and human-paced — by design.

## Configuration (Phase 3)

The intended switch follows tigerharness's env-var-driven config
model:

| Env var | Values | Meaning |
|---|---|---|
| `TIGERHARNESS_RUNNER_BACKEND` | `api` (default) / `subscription` | Which backend the task/workflow runner uses. |

In `subscription` mode the runner CLI does **not** execute anything —
`assign` / `start` simply scaffold a journal entry and notify. The
human is the engine. In `api` mode the behaviour is unchanged from
today.

This integration — and unifying `task_journal/` and `workflow_journal/`
under `journal/` — is Phase 3 and intentionally deferred so it doesn't
churn the existing runners while the model is still settling.

## Phasing

- **Phase 1 — core (first build).** Journal convention + `status.json`
  schema + `OPERATING.md` + the scaffolder (CLI + skill) +
  `drive-journal` skill. This alone makes subscription-driven work
  usable end to end, with no watcher.
- **Phase 2 — watcher (first build).** `journal watch`: stale
  detection, auto-archive, Slack digest, plus `journal list` / `status`.
- **Phase 3 — integration (deferred).** Wire the backend behind
  `TIGERHARNESS_RUNNER_BACKEND`; unify the journal folders; optional
  lease/locking if concurrent drivers ever become a goal.

Phases 1 and 2 are the agreed scope for the first build.

## Non-goals

- **Parallelism.** A single human drives serially. Concurrent drivers
  are explicitly out of scope, which is why there is no lease field in
  the MVP.
- **Automating the interactive app.** Driving the Claude Code UI via
  keystroke automation (tmux/pty) to fake autonomy is brittle and runs
  against the subscription's intended use. The human trigger is the
  design, not a limitation to engineer around.
- **Replacing the API backend.** The two coexist; you pick per task
  whether you want cheap-and-human-paced or fast-and-paid.

## Related

- [`task-runner.md`](task-runner.md) — the single-persona API backend.
- [`workflow-runner.md`](workflow-runner.md) — the multi-persona API
  backend; already file-based, and the closest sibling to this design.
- [`DESIGN.md`](DESIGN.md) — the env-var-driven configuration
  philosophy this backend follows.

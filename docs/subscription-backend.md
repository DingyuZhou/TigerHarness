# subscription backend

A file-based, human-driven execution model that runs agent work —
single-persona tasks and multi-persona workflows alike — through the
**interactive** Claude Code app, so the work counts against a monthly
subscription instead of token-billed API usage.

> **Status:** Phase 1 + Phase 1.5 shipped; Phase 2 partially shipped.
> Phase 1 (journal + scaffolder + `drive-journal` skill with the
> lazy sweep, `kind=task`) shipped in PR #25 (`7d6b9f8` on main).
> Phase 1.5 (`kind=workflow` via in-session compile, the seven new
> compile-side CLIs, the OPERATING.md compile sub-protocol) shipped
> in PR #26 (`155128f` on main). Phase 2 add-ons -- `journal
> compile-retry`, configurable compile-time persona roster, the
> hardcode cleanups -- are shipping on the current closeout branch.
> Still deferred: the TIGERHARNESS_RUNNER_BACKEND runner-config
> switch, folder unification (`task_journal/` + `workflow_journal/`),
> and the api-backed runner deletion sweep (see the closeout
> follow-ups section near the end). User-facing summary lives in
> [`docs/journal.md`](journal.md); workflow-mode details in
> [`docs/journal-workflow-mode.md`](journal-workflow-mode.md).

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

It does **not** remove the existing API-based runners. They remain
available as an opt-in mode for users who want autonomous, parallel
throughput and are willing to pay per token — and for a future where
token prices may fall. The two coexist behind a config switch whose
default is `subscription` (Phase 2).

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
the session drives the task as far as it can in one go — running
continuously until the task is `done`, hits a real blocker, or the
human stops it — then writes its state back.

## What it does

1. You write a PRD describing the task.
2. You run a command (or invoke a skill) that **scaffolds** a journal
   folder from that PRD.
3. You open the interactive Claude Code app and invoke the **driver**
   skill (`drive-journal`). Each invocation begins with a **lazy
   sweep** of `active/` — archive anything that finished, classify
   each `in_progress` task as fresh-or-stale (the heartbeat doubles
   as a soft lease), and summarize what's actionable to you
   in-session. Then the driver picks **one** actionable task —
   leaving any *fresh* `in_progress` task alone (another session owns
   it) — and works it continuously, appending progress and updating
   state as it goes, until the task is `done`, hits a blocker, or you
   stop the session. When that task finishes, the driver
   **cascades**: re-sweeps and picks up the next actionable task,
   draining the queue in one sitting.
4. If a task isn't finished (you stopped, or it blocked), resume
   later — the state is durable on disk. The next `drive-journal`
   invocation's sweep picks up where this one left off; meanwhile any
   *stale* `in_progress` task can be rescued by a future invocation.

Single-persona work (the task-runner's niche) and multi-persona work
(the workflow-runner's niche) both belong here in principle. The
distinction between the two is not steps-vs-no-steps — both can take
many steps. It is **orchestration**: a task-runner job is a single
persona working a PRD freely, with no pre-defined step graph; a
workflow-runner job is a pre-compiled multi-persona graph where each
node names the persona and contract for that step.

**Phase 1 scope:** `kind=task` only. `kind=workflow` shipped in
Phase 1.5 (see [`journal-workflow-mode.md`](journal-workflow-mode.md))
-- the field is in the schema, the scaffolder pre-flights compile
personas, and the driver runs an in-session compile (Anzai drafter +
Akagi / Ayako critics) before walking `orchestration.json`. Task
mode and workflow mode share the same `journal/active/` layout, the
same sweep, and the same `OPERATING.md` protocol; the kind dispatch
happens at step 4 of the driver loop.

### What goes in `task.md`

`task.md` is a free-form markdown brief — there is no required
schema. At minimum it should answer:

- **Goal** — one paragraph on what "done" looks like.
- **Acceptance criteria** — a short list of checks the driver can
  use to recognise completion. (For a code task, "the gate is green
  at 100% coverage and ..."; for a research task, "a memo at
  `artifacts/findings.md` with sections X, Y, Z.")
- **Constraints** — any hard boundaries (e.g., "stay inside
  `src/tigerharness/journal/`", "don't touch the api backend").

A one-paragraph idea is fine; PRD is used loosely here. If you have
attachments, references, or sub-tasks, drop them into `artifacts/`
and reference them from `task.md`.

## Architecture

```
You: write a PRD
    |
    v
Scaffolder (CLI `journal new` / skill)
    |-- creates journal/active/<task-id>/ from the PRD
    v
journal/ (passive file-based state machine)        <-- source of truth
    ^
    |
    v
Driver (interactive session + `drive-journal` skill)
  1. Lazy sweep of active/ (no AI, no cron, no daemon)
       - archive done/ tasks
       - flag stale in_progress tasks (heartbeat aged out)
       - summarize what's actionable, in-session
  2. Pick ONE actionable task, run it to done / blocked / stop
       - reads OPERATING.md
       - appends progress.md as it goes
       - updates status.json (state, heartbeat, sessions, next_action)
```

The driver is the only piece that consumes the subscription, and it
only runs when a human starts it. The sweep is plain non-AI Python
bookkeeping executed *inside* the driver's invocation — there is no
separate process, cron job, or systemd unit.

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
        <task-id>/            # finished tasks moved here by the next drive-journal sweep
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
  "sessions": 2,
  "max_sessions": 5,
  "created_at": "2026-06-02T08:00:00Z",
  "updated_at": "2026-06-02T09:15:00Z",
  "next_action": "Resume step 2; last blocker was the missing schema",
  "session_ref": "5e3b8023-d810-49a1-b763-0751a82baa15"
}
```

| Field | Type | Set by | Purpose |
|---|---|---|---|
| `id` | string | scaffolder | `<YYYYMMDD>-<slug>-<uuid8>`. `slug` = ASCII-lowercase-hyphen slugified `--title` (or first H1 of the PRD), max 40 chars. `uuid8` = 8 hex chars from `secrets.token_hex(4)`. On collision the scaffolder regenerates the uuid once then hard-errors. Path-safety enforced (no `/`, no `..`, no hidden-file prefix). |
| `title` | string, required | scaffolder | Human label. Source: `--title` arg, else first H1 of the PRD, else `"task"`. |
| `kind` | enum: `"task"` (Phase 1) or `"workflow"` (Phase 1.5+) | scaffolder | Phase 1 ships `task`; Phase 1.5 added `workflow` -- see [`journal-workflow-mode.md`](journal-workflow-mode.md). |
| `persona` | string, required for `kind=task` | scaffolder | The persona this task is assigned to (must exist in the team's persona registry). |
| `state` | enum: `pending` / `in_progress` / `blocked` / `done` | driver / sweep | See state-transition table below. |
| `sessions` / `max_sessions` | int / int (default `5`) | driver / scaffolder | How many `drive-journal` invocations the task has consumed, and a soft ceiling. Each invocation counts as one session regardless of how much work happens inside it. When `sessions == max_sessions`, the driver moves the task to `blocked` with a `next_action` explaining why, and the human must raise the cap or close the task. |
| `created_at` | ISO 8601 UTC | scaffolder | Set once at creation, never updated. Used by the sweep summary for the "age" display. |
| `updated_at` | ISO 8601 UTC | driver | **Heartbeat.** Bumped on every `progress.md` append; OPERATING.md requires the driver to append progress at least every 10 minutes of wall-clock active work. A wedged session shows up stale once `updated_at` is older than `stuck_timeout` (default 1800s = 30 min). |
| `next_action` | string | driver | The handoff note. Lets a *fresh* session resume without re-reasoning the whole `progress.md` — this is what makes the journal the memory, not the vendor's session. |
| `session_ref` | string \| null | driver | Optional Claude session id, so the human can `--resume` the same conversation cheaply. Null is fine; `next_action` + `progress.md` are enough to resume from files alone. |
| `compile_pending` | bool (`kind=workflow` only) | scaffolder / `land-compile` | `true` at scaffold; flipped to `false` (the visibility gate) once the compile lands the graph. Absent for `kind=task`. See [`journal-workflow-mode.md`](journal-workflow-mode.md). |
| `compile_phase` | enum (`kind=workflow` only): `pending` / `drafting` / `tier1_pre` / `critiquing` / `tier1_post` / `complete` / `failed` | compile sub-protocol | The compile sub-state machine. Absent for `kind=task`. |
| `playbook_name` | string, required for `kind=workflow` | scaffolder | Bare name of the playbook the workflow was compiled from. Rejected for `kind=task`. |

Two fields carry the design: `updated_at` (heartbeat, doubles as soft
lease) and `next_action` (resume-from-files). `session_ref` is a
convenience, not a dependency — losing it costs nothing but a re-read.
The other fields are bookkeeping or human-facing labels.

### State transitions

| From | To | Who | Trigger | Side effects |
|---|---|---|---|---|
| (none) | `pending` | scaffolder | `tigerharness journal new --prd ...` succeeds | full status.json initialized |
| `pending` | `in_progress` | driver | sweep step 2 picks this task | `sessions += 1`, `updated_at` bumped, `session_ref` set if cheap to capture |
| `in_progress`-stale | `in_progress` | driver | **rescue** -- the sweep classified the task as stale (heartbeat older than `stuck_timeout`) and step 2 picked it up; the previous owner is presumed wedged or crashed | `sessions += 1`, `updated_at` bumped, `session_ref` overwritten if newly captured. The driver reads the tail of `progress.md` to figure out where the previous owner left off |
| `in_progress` | `in_progress` | driver | mid-task progress (clean stop, max_sessions not hit) | `updated_at` bumped, `next_action` rewritten. **No** `sessions` change -- the counter was already incremented on pickup |
| `in_progress` | `done` | driver | task complete per acceptance criteria | `state=done`, `next_action` cleared, sweep will archive on next invocation |
| `in_progress` | `blocked` | driver | real blocker (need human / another agent / external input) OR `sessions == max_sessions` | `state=blocked`, `next_action` names the blocker |
| `blocked` | `in_progress` | human | manual edit of `status.json` to clear the blocker | `next_action` rewritten by the human to point the next session at the resolution |

Phase 1 deliberately does **not** model a `failed` terminal state.
"Failed" in v1 means "the human gave up" — they edit `state=done`
with a `next_action` recording the postmortem and move on. A real
unrecoverable-error state can land in a later phase if the journal
grows to need it.

Writes to `status.json` must be **atomic** (write to a temp file, then
rename) so an interrupted session never leaves a half-written state
file.

There is deliberately **no lease / lock field.** Leasing would guard
against two drivers grabbing the same task at once, but parallelism is
a non-goal here and a single human drives serially. `state` +
`updated_at` already answer "is this being worked / is it stale." A
lease can be added later if concurrent drivers ever become real (see
Non-goals).


### Why one invocation runs the task to completion (and then cascades)

The driver is **not** stepwise. One `drive-journal` invocation is
intended to take its picked-up task as far as it can in a single
interactive sitting — ideally all the way to `done` — and then,
having finished that task, **cascade** by sweeping the journal again
and picking up the next actionable task. One invocation drains as
much of the queue as it can; it does not stop after a single task
when more are waiting.

The session stops only when (a) the picked task is finished, (b) a
real blocker requires a human or another persona, (c) a guard rail
like `max_sessions` fires, or (d) the human ends the session. After
(a) or (b), the cascade decides whether to keep going on a new task;
after (c) or (d), the invocation exits.

This matters because the subscription model rewards long, productive
interactive sessions, not many short ones. Splitting work into
artificial "increments" with a stop-and-yield after each one — or
stopping after one task when the queue still has work — would burn
the human's attention budget for no reason and waste the
resume-from-files dance on work that could have just kept going.

`sessions` counts driver invocations, not steps of work or number of
cascaded tasks inside a single invocation. `updated_at` is a
heartbeat refreshed periodically *during* a session so that a wedged
or crashed session shows up as stale — and so a *fresh* heartbeat
signals to a *later* invocation that another session owns this task
right now and should be left alone. The heartbeat doubles as a soft
lease without an explicit lease field.

## OPERATING.md — the protocol

`OPERATING.md` is the instruction file: a vendor-neutral markdown
contract that teaches *any* file-reading agent to drive the journal.
It is the decoupling layer — Claude reads it via the driver skill, but
so could a human or another vendor's agent. It specifies:

- **Where state lives** — the journal path and folder conventions.
- **How to read state** — the `status.json` schema and what each
  `state` value means.
- **The decision procedure**, run on every `drive-journal` invocation
  and looped after each completed task until no actionable tasks
  remain:
  1. **Lazy sweep** of `active/*/status.json` first:
     a. Move any `done` task to `journal/done/` (the *previous*
        invocation may have finished a task without archiving it).
     b. Classify every `in_progress` task by heartbeat age:
        - **Fresh** (`updated_at` within `stuck_timeout`) — another
          session owns this *right now*. Do **not** pick it up. The
          heartbeat is the de-facto lease.
        - **Stale** (`updated_at` older than `stuck_timeout`) — the
          previous owner wedged or crashed. Flag it as stale in the
          in-session summary; it is now reclaimable by the rescue
          path in step 2.
     c. Print the summary (count of `pending` / `in_progress`-fresh /
        `in_progress`-stale / `blocked`) into the session.
  2. Pick the next actionable task — **exactly one**, never multiple
     in parallel:
     - A `pending` task to start, OR
     - A *stale* `in_progress` task to **rescue** (read its tail of
       `progress.md` first to understand where the previous owner
       left off, then resume from `next_action`).
     - **Never** pick a *fresh* `in_progress` task — leaving it alone
       is the correct behaviour. If no other task is actionable, the
       invocation should exit cleanly, not steal someone else's
       in-flight work.
     - Skip `blocked` tasks; surface them in the summary so the human
       can unblock manually.
     - Prefer the one with the oldest heartbeat among the candidates.
  3. Read its `task.md` (PRD) + `next_action` + the tail of
     `progress.md`.
  4. **Work the task continuously** — do the real work, append to
     `progress.md`, and refresh `updated_at` as a heartbeat as you go.
     Keep going until **one** of the stop conditions below fires.
     Do **not** stop after a single step just because progress was
     made; the goal is to take the task as far as possible in this
     session.
  5. On exit, write a final `progress.md` entry summarising the
     session and update `status.json`: refresh `updated_at` one last
     time, rewrite `next_action` (or clear it if `done`), and set
     `state` to `done` / `blocked` / leave as `in_progress` for a
     clean stop. The `sessions` counter was already bumped on pickup
     (step 2) -- do **not** bump it again on exit.
  6. **Cascade.** If the task you just finished moved to `done` (or
     even `blocked` — a `blocked` task is "off your plate" because
     the human's next action is to unblock it, not to drive it),
     loop back to step 1 (re-sweep) and pick up the next actionable
     task. Keep cycling until step 1 reports nothing actionable
     remains, or the human ends the session, or a session-level
     guard rail fires. Don't manufacture a stopping point just
     because one task finished — drain the queue while the session is
     hot.
- **Stop conditions** — exit the loop in step 4 (for the current
  task) when **any** of these hold: the task is fully `done`; a real
  blocker is hit that needs a human or another agent (`blocked`); the
  work has hit `max_sessions` or another guard rail; or the human
  ends the session. Step 6 then decides whether to cascade or exit
  the whole invocation.

The committed `OPERATING.md` is the contract; the driver skill is thin
sugar that says "read `OPERATING.md` and execute it."

## Skills and CLI

Both the scaffolder and the driver are exposed two ways — a CLI
command for scripting and a skill for the interactive app — so you can
work entirely inside Claude Code if you prefer.

| Surface | CLI | Skill | What it does |
|---|---|---|---|
| Scaffold | `tigerharness journal new --prd <file> [--title ...] [--persona ...]` | `journal-new` | Ingest a PRD, create `active/<task-id>/` with `task.md`, a seeded `status.json`, an empty `progress.md`, and `artifacts/`. |
| Drive | — (human-driven only) | `drive-journal` | Lazy-sweep `active/`, then read `OPERATING.md` and work one task per the decision procedure above. |
| Inspect | `tigerharness journal list` / `status` | — | Print the journal state as a table / JSON. Read-only; the sweep that *acts* (archive, flag) happens only inside `drive-journal`. |

The driver has no CLI form on purpose: a CLI driver would be a
programmatic entry point and defeat the subscription model. Driving
only happens inside an interactive session a human started.

## The lazy sweep (built into the driver)

Every `drive-journal` invocation begins with a sweep over
`active/*/status.json` before any task work happens, and is re-run
between cascaded tasks. **It calls no model and costs nothing beyond
the routine session.** Responsibilities:

- Move `done` tasks to `journal/done/` to keep `active/` lean.
- Classify every `in_progress` task by heartbeat age:
  - **Fresh** (`updated_at` within `stuck_timeout`): another session
    owns this right now. Leave it alone. The heartbeat is the
    de-facto lease.
  - **Stale** (`updated_at` older than `stuck_timeout`): the previous
    owner wedged or crashed. Flag it for rescue — it's reclaimable
    by the next step-2 pick.
- Summarize what's actionable: e.g. *"2 pending, 1 in_progress
  (fresh — owned by another session), 1 stale (no heartbeat 40m), 1
  blocked awaiting your review."* The summary lands in the same
  interactive context where you're about to act on it, instead of a
  separate Slack DM from a daemon.

No cron, no systemd unit, no background process. The sweep is plain
non-AI Python logic invoked at the start of the `drive-journal`
skill and at every cascade boundary, executing inside the human's
interactive session.

Note the contrast with the task-runner's `stuck_watchdog`: that
watchdog can **kill** a wedged subprocess because the api backend owns
one. The subscription backend owns no process — there is nothing to
kill. Stale handling is purely **advisory**: the sweep names the
stale task in-session and you decide whether to re-open it or close
it.

| Env var | Default | Purpose |
|---|---|---|
| `TIGERHARNESS_JOURNAL_DIR` | `<team>/journal/` | Journal root the scaffolder and driver operate on. |
| `TIGERHARNESS_JOURNAL_STUCK_TIMEOUT` | `1800` (30 min) | Heartbeat age past which the sweep flags an `in_progress` task as stale. |

## The work loop

1. **Write a PRD.** A plain markdown file describing the task — like
   any brief you'd hand a teammate.
2. **Scaffold.** `tigerharness journal new --prd brief.md` (or the
   `journal-new` skill in the app) creates `active/<task-id>/` and
   seeds `status.json` as `pending`.
3. **Drive.** Open Claude Code, invoke `drive-journal`. The skill
   first runs the lazy sweep (archive any `done`, classify
   `in_progress` tasks as fresh-or-stale, summarize what's actionable
   in-session); then it picks **one** actionable task — never a
   *fresh* `in_progress` task, since that belongs to another active
   session — and runs it continuously, appending to `progress.md` and
   bumping the heartbeat in `status.json` as it goes, until the task
   is `done`, hits a blocker, or you stop the session. When that task
   finishes, the skill **cascades**: re-runs the sweep and picks up
   the next actionable task. One invocation drains as much of the
   queue as it can in one sitting.
4. **Resume if needed.** If the task isn't `done` (you stopped, it
   blocked, or it hit `max_sessions`), come back later and invoke
   `drive-journal` again — same or fresh session, using `--resume` /
   `session_ref` for cheap continuity. The next invocation's sweep
   picks up where this one left off; state is durable on disk
   regardless.

For multi-persona work — i.e. a pre-defined workflow graph — the
session adopts the persona named by the current node, works that node
continuously until its stop condition (typically: handoff needed to a
different persona), records the handoff in `next_action`, and either
continues in-session under the new persona or hands off to the next
`drive-journal` invocation. For single-persona work — a task with no
pre-orchestration — the session just stays in one persona and drives
the PRD forward across however many steps the task takes, again
running continuously until `done`, blocked, or stopped. Persona
handoff is a natural stop condition; everything short of one (or the
other stop conditions above) is run-through, not stop-and-yield.
Serial and human-paced — by design.

### How serial execution is enforced

Serial means **one task at a time**, not one task per invocation.
The decision procedure picks exactly **one** task at step 2, drives
it to its stop condition at steps 3–5, and *then* cascades at step 6
back to step 1 to pick up the next one. Inside an invocation, tasks
are processed back-to-back, not concurrently.

What prevents parallel execution within a single invocation: step 2
of the procedure picks exactly one task and the driver doesn't move
on until that task hits its stop condition. The interactive session
is one human at one keyboard, so within a single session there is
fundamentally one driver doing one task at a time.

What prevents two invocations from clobbering each other on the same
task: the **heartbeat as a soft lease**. A *fresh* `updated_at` on
an `in_progress` task signals that another session is actively
working it; the sweep classifies it as `in_progress`-fresh and
step 2 declines to pick it up. Only `pending` tasks and *stale*
`in_progress` tasks (heartbeat older than `stuck_timeout`) are
candidates for a new owner. This makes the heartbeat double as a
soft lease without an explicit lease field.

The doc deliberately omits an explicit lease (cut from the MVP) on
the honest assumption that two concurrent drivers is the rare case
and the heartbeat-as-soft-lease catches it well enough. The
remaining race window — two invocations both seeing a `pending`
task as their best pick at the exact same moment — could clobber
each other's `status.json` initial write. Adding an explicit lease
(file lock, dedicated `lease.json`) is a Phase 2 hardening if
concurrent drivers ever become a real workflow.

## Configuration (Phase 2)

The intended switch follows tigerharness's env-var-driven config
model:

| Env var | Values | Meaning |
|---|---|---|
| `TIGERHARNESS_RUNNER_BACKEND` | `subscription` (default) / `api` | **Planned — not yet in code.** Which backend the task/workflow runner would use. |

By default (in `subscription` mode) the runner CLI does **not**
execute anything — `assign` / `start` simply scaffold a journal entry
and notify. The human is the engine. Opt in to `api` mode for the
legacy behaviour, where the runner spawns and supervises agents as it
does today.

This integration — and unifying `task_journal/` and `workflow_journal/`
under `journal/` — is Phase 2 and intentionally deferred so it doesn't
churn the existing runners while the model is still settling.

## Phasing

- **Phase 1 — MVP (first build).** Journal convention + `status.json`
  schema + `OPERATING.md` + the scaffolder (CLI + skill) +
  `drive-journal` skill (with the lazy sweep — archive, flag stale,
  in-session summary — built in) + read-only `journal list` /
  `status` commands. This alone makes subscription-driven work usable
  end to end. The previous draft split scaffolder/driver/journal from
  the watcher across two phases; collapsing the watcher into
  `drive-journal` collapses the split.
- **Phase 2 — integration (deferred).** Wire the backend behind
  `TIGERHARNESS_RUNNER_BACKEND` (defaulting to `subscription`, with
  `api` as the opt-in legacy mode); unify the journal folders;
  optional lease/locking if concurrent drivers ever become a goal.

Phase 1 is the agreed scope for the first build — lazy-triggering the
sweep folded what was previously a separate phase into the driver
skill, so the MVP is one chunk.

## Non-goals

- **Parallelism.** A single human drives serially. Concurrent drivers
  are explicitly out of scope, which is why there is no lease field in
  the MVP.
- **Automating the interactive app.** Driving the Claude Code UI via
  keystroke automation (tmux/pty) to fake autonomy is brittle and runs
  against the subscription's intended use. The human trigger is the
  design, not a limitation to engineer around.
- **Replacing the API backend.** The two coexist; `subscription` is
  the default, and you can opt into `api` per task when you want
  fast-and-paid instead of cheap-and-human-paced.

## Related

- [`task-runner.md`](task-runner.md) — the single-persona,
  no-pre-orchestration API backend (one persona drives the PRD freely).
- [`workflow-runner.md`](workflow-runner.md) — the multi-persona API
  backend driven by a pre-compiled workflow graph; already file-based,
  and the closest sibling to this design.
- [`DESIGN.md`](DESIGN.md) — the env-var-driven configuration
  philosophy this backend follows.

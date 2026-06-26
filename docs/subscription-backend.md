# subscription backend

## At a glance
- **What:** the rails/billing model + `status.json` contract behind `journal`
  — work runs on the interactive (subscription) rail, not token-billed API.
- **When you need it:** scheduling/driving cost rules, the Slack rail rule, or
  the `status.json` field semantics.
- **Must-not-miss:** a Slack-triggered session may SCHEDULE journal tasks but
  must NEVER drive them — `journal claim` enforces it.

## Details

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
>
> **Per-persona memory (2026-06-08).** Journal-driven work now feeds
> **each persona's own** tiger-memory store, not just the driver's, via
> per-turn worklog records written by the journal gates. See the
> "Per-persona memory" section below and the design doc
> [`per-persona-journal-memory.md`](per-persona-journal-memory.md).

## Why this exists

The legacy api runners (retired; ADR 0003) both drove work by spawning
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
   each `in_progress` task as **idle** (detached — `session_ref`
   cleared, resumable now), **busy** (attached + heartbeat fresh
   within the stuck-timeout — a live session owns it), or **crashed**
   (attached + heartbeat stale), and summarize what's actionable
   in-session. If every `in_progress` task is busy and nothing is
   idle/crashed/pending, the invocation **stops cheaply right there** —
   a frequent loop is meant to no-op when work is already healthy. Otherwise
   the driver picks **one** actionable task (priority order: a resumable
   `in_progress` task first; else, if a *busy* task exists, exit cleanly
   rather than start a `pending` one; else the oldest `pending`), and works
   it continuously. Then it **cascades** — the hard loop that is the
   driver's whole job: re-sweep and pick up the next session/task
   **back-to-back in the same turn**, draining the whole queue (and each
   task's whole `max_sessions` budget) in one sitting, **never
   one-session-per-loop-fire**. Context pressure is not a stop reason: the
   driver relies on auto-compaction and re-orients from `progress.md`,
   handing off only at the genuine context ceiling.
4. If a task isn't finished (you stopped, or it blocked), resume
   later — the state is durable on disk. A clean stop **detaches**
   (clears `session_ref`), so the task is **idle** and the next
   `drive-journal` resumes it **immediately**, with no heartbeat wait;
   a **crashed** task (owner went silent) is rescued the same way.

Single-persona work (the legacy iterative runner's niche) and multi-persona work
(the workflow-runner's niche) both belong here in principle. The
distinction between the two is not steps-vs-no-steps — both can take
many steps. It is **orchestration**: a legacy iterative job was a single
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
    .drive-sessions.json      # drive-session registry: thread_ts -> task; tells tiger-memory to skip a drive's fat transcript
    active/
        <task-id>/
            task.md           # your PRD, verbatim
            status.json       # the state machine (single source of truth)
            progress.md       # append-only log, human + AI readable
            worklog/          # per-turn, persona-attributed notes -- the per-persona memory record (gates-only writer)
            artifacts/        # whatever the task produces or references
    done/
        <task-id>/            # finished tasks moved here by the next drive-journal sweep (worklog/ travels with it)
```

This is the `kind=task` shape. A `kind=workflow` task instead carries
`task_brief.md` + `playbook_snapshot.md` (in place of `task.md`), a
`compile/` workspace while compiling, and `orchestration.json` + `steps/`
once the graph lands — see
[`journal-workflow-mode.md`](journal-workflow-mode.md) and
[`journal.md`](journal.md).

`<task-id>` format:
`<YYYYMMDD>-<HHmmSS>-<short-slug>-<8-char-uuid>`, e.g.
`20260602-143052-subscription-backend-7f2a9c14`. The timestamp is
UTC; the time component makes same-day ids sort in creation order.
Legacy date-only ids (`<YYYYMMDD>-<slug>-<uuid8>`) remain valid and
coexist with no migration — ids are never parsed, ordering is plain
lexicographic.

The `active/` and `done/` split is what keeps the journal lean: the
driver only ever reads `active/`, so archiving finished tasks bounds
how much it has to scan and re-read. A third tray, `needs_input/`, holds
tasks **parked** on an Operator question — out of the actionable queue
until the Operator answers (see
[`journal-operator-questions.md`](journal-operator-questions.md)).

`journal/` is a **runtime artifact**, not git-tracked source (the same
treatment `task_journal/` and `workflow_journal/` get). `OPERATING.md`
is the one exception — it is the committed protocol. The team
`.gitignore` should exclude `journal/active/`, `journal/done/` and
`journal/needs_input/` but keep `journal/OPERATING.md`.

## status.json — the heart

```json
{
  "id": "20260602-143052-subscription-backend-7f2a9c14",
  "title": "Add the subscription backend",
  "kind": "task",
  "state": "in_progress",
  "persona": "Mitsui",
  "sessions": 2,
  "max_sessions": 3,
  "created_at": "2026-06-02T08:00:00Z",
  "updated_at": "2026-06-02T09:15:00Z",
  "next_action": "Resume step 2; last blocker was the missing schema",
  "session_ref": null
}
```

| Field | Type | Set by | Purpose |
|---|---|---|---|
| `id` | string | scaffolder | `<YYYYMMDD>-<HHmmSS>-<slug>-<uuid8>` (UTC; legacy date-only ids remain valid). `slug` = ASCII-lowercase-hyphen slugified `--title` (or first H1 of the PRD), max 40 chars. `uuid8` = 8 hex chars from `secrets.token_hex(4)`. On collision the scaffolder regenerates the uuid once then hard-errors. Path-safety enforced (no `/`, no `..`, no hidden-file prefix). |
| `title` | string, required | scaffolder | Human label. Source: `--title` arg, else first H1 of the PRD, else `"task"`. |
| `kind` | enum: `"task"` (Phase 1) or `"workflow"` (Phase 1.5+) | scaffolder | Phase 1 ships `task`; Phase 1.5 added `workflow` -- see [`journal-workflow-mode.md`](journal-workflow-mode.md). |
| `persona` | string, required for `kind=task` | scaffolder | The persona this task is assigned to (must exist in the team's persona registry). |
| `state` | enum: `pending` / `in_progress` / `blocked` / `needs_input` / `done` | driver / sweep | See state-transition table below. `needs_input` = parked on an Operator question; lives in the `needs_input/` tray, never actionable until answered (see [`journal-operator-questions.md`](journal-operator-questions.md)). |
| `sessions` / `max_sessions` | int / int (default `3` task / `10` workflow) | driver / scaffolder | How many `drive-journal` invocations the task has consumed, and a soft ceiling. Each invocation counts as one session regardless of how much work happens inside it. When `sessions >= max_sessions` the budget is spent: the driver marks the task `done` if complete, else `blocked` (raise the cap or close it). `journal claim` self-heals an at-cap task by blocking it rather than running past the cap. |
| `created_at` | ISO 8601 UTC | scaffolder | Set once at creation, never updated. Used by the sweep summary for the "age" display. |
| `updated_at` | ISO 8601 UTC | driver | **Heartbeat.** Bumped on every `progress.md` append (OPERATING.md requires ≤10 min between appends during active work). Consulted **only** to tell a *busy* attached task from a *crashed* one: an `in_progress` task whose `session_ref` is set shows up **crashed** once `updated_at` is older than `stuck_timeout` (default 1800s = 30 min). A *detached* task (`session_ref=null`) is **idle** regardless of heartbeat age. See [`journal-instant-resume.md`](journal-instant-resume.md). |
| `next_action` | string | driver | The handoff note. Lets a resuming session pick up without re-reasoning the whole `progress.md` — this is what makes the journal the memory, not the vendor's session. |
| `session_ref` | string \| null | `journal claim` / `release` | **Attach token.** Set (an opaque id) while a session is actively driving the task; `null` when detached (cleanly handed off, or never claimed). This is what distinguishes a *busy* task (a live session owns it — the soft lease) from an *idle* one (resumable immediately, no heartbeat wait). `journal claim` sets it atomically at pickup; `journal release` clears it on a clean stop. See [`journal-instant-resume.md`](journal-instant-resume.md). |
| `compile_pending` | bool (`kind=workflow` only) | scaffolder / `land-compile` | `true` at scaffold; flipped to `false` (the visibility gate) once the compile lands the graph. Absent for `kind=task`. See [`journal-workflow-mode.md`](journal-workflow-mode.md). |
| `compile_phase` | enum (`kind=workflow` only): `pending` / `drafting` / `tier1_pre` / `critiquing` / `tier1_post` / `complete` / `failed` | compile sub-protocol | The compile sub-state machine. Absent for `kind=task`. |
| `playbook_name` | string, required for `kind=workflow` | scaffolder | Bare name of the playbook the workflow was compiled from. Rejected for `kind=task`. |
| `early_exit` | bool (default `false`) | scaffolder | When `false`, the driver runs the full `max_sessions` budget ("N iterations = exactly N"); when `true`, it may mark `done` as soon as acceptance criteria are met. Set via `journal new --early-exit`. See [`journal-instant-resume.md`](journal-instant-resume.md). |
| `schedule_def` | string (optional) | scheduler | Present iff the task was materialized from a `schedule/` definition: the definition id. |
| `schedule_due` | string (optional) | scheduler | The due timestamp (ISO UTC) this materialization satisfied; with `schedule_def` it makes crash-recovery duplicate-detection exact. |

Three fields carry the design: `session_ref` (the **attach signal** —
is a live session driving this right now?), `updated_at` (the
**heartbeat** — now used only to catch a *crashed* owner, not to gate
resuming), and `next_action` (resume-from-files). Together they let a
cleanly handed-off task resume **immediately** while a genuinely crashed
one is still reclaimed after `stuck_timeout`. The other fields are
bookkeeping or human-facing labels.

> **Instant session hand-off (2026-06-06).** `session_ref` was promoted
> from a convenience to the load-bearing attach signal so same-task
> sessions resume with no 30-minute wait. The canonical mechanics — the
> idle/busy/crashed classification, the `journal claim` / `release`
> CLIs, and the compare-and-set claim — live in
> [`journal-instant-resume.md`](journal-instant-resume.md) and the
> on-disk `OPERATING.md`; this document has been brought in line with
> them. Where any detail still differs, those two sources win.

### State transitions

| From | To | Who | Trigger | Side effects |
|---|---|---|---|---|
| (none) | `pending` | scaffolder | `tigerharness journal new --prd ...` succeeds | full status.json initialized |
| `pending` | `in_progress` | driver | sweep step 2 picks this task | `sessions += 1`, `updated_at` bumped, `session_ref` set to a fresh attach token by `journal claim` |
| `in_progress`-crashed | `in_progress` | driver | **rescue** -- the sweep classified the task as crashed (attached but heartbeat older than `stuck_timeout`) and step 2 picked it up; the previous owner is presumed wedged or crashed | `sessions += 1`, `updated_at` bumped, `session_ref` re-set to a fresh attach token by `journal claim`. The driver reads the tail of `progress.md` to figure out where the previous owner left off |
| `in_progress` | `in_progress` | driver | mid-task progress (clean stop, max_sessions not hit) | `updated_at` bumped, `next_action` rewritten. **No** `sessions` change -- the counter was already incremented on pickup |
| `in_progress` | `done` | driver | task complete per acceptance criteria | `state=done`, `next_action` cleared, sweep will archive on next invocation |
| `in_progress` | `blocked` | driver | real blocker (need human / another agent / external input) OR `sessions >= max_sessions` | `state=blocked`, `next_action` names the blocker |
| `in_progress` | `needs_input` | driver | an Operator decision the driver cannot make itself; **park instead of stalling the turn** | `journal release --state needs_input --question <file>` appends to `questions.md`, detaches, and moves `active/<id>/` → `needs_input/<id>/`. See [`journal-operator-questions.md`](journal-operator-questions.md) |
| `needs_input` | `in_progress` | Operator | the Operator answered in `questions.md` and reopened the task | `journal answer <id>` flips to `in_progress` detached (= idle, resumes immediately), stamps `next_action`, and moves `needs_input/<id>/` → `active/<id>/` |
| `blocked` | `in_progress` | human | manual edit of `status.json` to clear the blocker | `next_action` rewritten by the human to point the next session at the resolution |

Phase 1 deliberately does **not** model a `failed` terminal state.
"Failed" in v1 means "the human gave up" — they edit `state=done`
with a `next_action` recording the postmortem and move on. A real
unrecoverable-error state can land in a later phase if the journal
grows to need it.

Writes to `status.json` must be **atomic** (write to a temp file, then
rename) so an interrupted session never leaves a half-written state
file.

There is deliberately **no hard lock.** Parallelism is a non-goal and a
single human drives serially. The **soft lease** is `session_ref` (the
attach token a session sets at `claim` and clears at `release`):
`session_ref` + `updated_at` together answer idle (detached) / busy
(attached + fresh) / crashed (attached + stale) — see
[`journal-instant-resume.md`](journal-instant-resume.md). `claim` is an
atomic compare-and-set on `session_ref`, so two concurrent drivers can't
both grab a task; a hard lock can still be added if concurrent drivers
ever become real (see Non-goals).


### Why one invocation runs the task to completion (and then cascades)

The driver is **not** stepwise, and the cascade is **not** optional —
it is the driver's whole job. One `drive-journal` invocation runs a
task through its **entire `max_sessions` budget** session-to-session,
and then drains the rest of the queue, **back-to-back in the same
turn**. It does **not** hand the turn back between sessions or after a
single task, and it is **never** one-session-per-loop-fire (that
anti-pattern stretches a day of work across a day of 30-minute waits).

The session stops only when: the picked task is `done` **and**
`early_exit=true` (with the default `early_exit=false` it keeps
iterating through `max_sessions`); a real blocker requires a human or
another persona; `sessions >= max_sessions` fires; the sweep reports
nothing actionable; the human ends the session; or you hit the
**genuine context ceiling**. Short of those, it loops back to the
sweep and keeps going.

**Context pressure is not a stop reason — compact instead.** Every
session checkpoints to `progress.md` + `next_action`, so a context
compaction loses nothing for continuity (re-orient from those after
one). There is NO configured mid-task compaction (retired 2026-06-11);
the driver works to the genuine ceiling, then checkpoints and hands
off — a fresh fire resumes the idle task instantly with fresh
context. The CLI's own near-limit auto-compact remains as a fallback
only. (Compaction is safe for *memory* too — provided a
`kind=task` done-note is assembled from the durable record; see
[Per-persona memory](#per-persona-memory-the-worklog).)

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

## Per-persona memory (the worklog)

A drive runs entirely inside **one** interactive session, which adopts
each persona in-session via the four-line preamble. Left alone,
tiger-memory would attribute that whole transcript to a single owner —
so a specialist's work would land in the **driver's** store, never its
own. The fix is to drive memory off the journal's **structured
records** instead of the raw transcript:

- **Worklog.** Every persona turn ends by handing its output to a
  journal gate, which writes one markdown file under
  `active/<task-id>/worklog/NNNN-<persona>-<step>.md`. The frontmatter
  `persona` is **stamped by code** — from `status.json`'s assigned
  persona (`kind=task`) or the compiled step file / `compile_personas`
  map (`kind=workflow`) — never typed free-hand, so attribution can't
  be wrong. *The note is the ticket to advance:* the gate refuses to
  move the work forward without a non-empty note, so a persona's memory
  of the work it did can't go missing.
- **The gates.** `journal claim --driver <p>` writes a thin "I drove
  this" trace to the driver's store and registers the drive — the drive's
  `thread_ts` is read automatically from the `TIGERHARNESS_SLACK_THREAD_TS`
  env var the bridge stamps on every turn (an explicit `--drive-thread
  <ts>` still wins if passed). `journal release --driver <p> --state done
  --output <note>` files the assigned persona's work note (`kind=task`).
  `journal step-done --task ... --step ... --verdict ... --output
  <note>` files each step persona's note and advances the graph walk
  (`kind=workflow`). `land-compile` normalizes the saved compile rounds
  into per-persona worklog entries. The vendor-neutral details live in
  `OPERATING.md` (next section) and
  [`per-persona-journal-memory.md`](per-persona-journal-memory.md).
- **Double-count suppression.** At `claim`, the drive's Slack
  `thread_ts` — harness-enforced via the `TIGERHARNESS_SLACK_THREAD_TS`
  env var the bridge sets per turn, so the agent never copies it by hand
  — is recorded to `journal/.drive-sessions.json`. tiger-memory's
  `claude_transcript` adapter reads that registry and **skips** a
  registered drive's transcript — the worklog already owns that content,
  so the driver doesn't *also* get a fat summary of the whole drive.
- **Ingestion.** A `journal_worklog` tiger-memory source discovers
  `*/worklog/*.md` under the journal root, groups them per `(task,
  persona)`, and feeds the existing summarize → ingest machinery. It
  reads **only** `worklog/` — `progress.md` is the narrative/continuity
  log and is **never** ingested. Each journal-working persona needs a
  tiger-memory config + store listing that source (the roster
  prerequisite).

The enforcement lives in **harness code**, not a prompt: a turn that
skips its note simply cannot advance. The worst case is a *thin* note,
never a missing or mis-attributed one. Note **prose quality** is not
enforced (code can guarantee a note exists and is correctly attributed,
not that it is well-written).

**Cascade × compaction caveat.** A `kind=task`'s worklog note is written
**once, at `done`**, so it is the assigned persona's *only* ingested
memory of the whole task. Because the cascade-first protocol relies on
auto-compaction across a long task, the driver must build that note from
the **durable record** (`progress.md` + `artifacts/`), not from
possibly-compacted in-context memory. (`kind=workflow` is unaffected:
`step-done` writes each step's note immediately, before any later
compaction.)

## OPERATING.md — the protocol

> **ℹ This is a synopsis.** The on-disk `OPERATING.md` (generated from
> `operating_template.py`) is the authoritative, fuller contract — it
> additionally covers the per-persona memory gates (`--driver` /
> `--output` / `journal step-done`) and the compile / graph-walk
> sub-protocols. See also
> [`journal-instant-resume.md`](journal-instant-resume.md) for the
> idle/busy/crashed classification. The summary below is kept in sync
> with the shipped protocol (cascade-first redesign, 2026-06-08).

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
     b. Classify every `in_progress` task via the `session_ref` attach
        token + heartbeat: **idle** (detached — resumable now),
        **busy** (attached + heartbeat fresh within `stuck_timeout`),
        or **crashed** (attached + heartbeat stale).
     c. Print the summary (`pending` / resumable / busy / crashed /
        `blocked` counts) into the session.
     d. **Cheap no-op fast path:** if every `in_progress` task is
        **busy** and nothing is idle / crashed / pending, **stop the
        invocation here** without reading task context or the rest of
        `OPERATING.md`. A healthy live session already owns the work; a
        frequent loop (every few minutes) is *designed* to no-op
        cheaply at this point.
  2. Pick the next actionable task — **exactly one**, in this
     **priority order (finish before you start)**:
     a. A **resumable** `in_progress` task → resume it: **idle** (clean
        hand-off — resume **immediately**, no wait) or **crashed**
        (rescue). Read the tail of `progress.md` and continue from
        `next_action`; prefer the oldest heartbeat among several.
     b. **Else, if a *busy* `in_progress` task exists, do NOT start new
        work — exit cleanly.** A live session owns it (the soft lease);
        let it finish before any `pending` task begins (a later task
        may depend on the one in flight).
     c. **Else, start the oldest `pending` task.**
     Then **claim atomically** with `tigerharness journal claim <id>`
     (sets `session_ref`, bumps `sessions`, refreshes the heartbeat,
     compare-and-set). In a Slack-driven drive, pass `--driver
     <persona>`. Skip `blocked` tasks; surface them so the human can
     unblock.
  3. Read its `task.md` (PRD) + `next_action` + the tail of
     `progress.md`.
  4. **Work the task continuously** — do the real work, append to
     `progress.md`, and refresh `updated_at` as a heartbeat (≥ every
     ~10 min) as you go. For `kind=workflow`, advance the graph walk
     via `journal step-done` (the gate routes and records each step's
     per-persona worklog note; don't follow edges by hand). Keep going
     until **one** of the stop conditions below fires.
  5. On stop, run `tigerharness journal release <id>` to detach (clears
     `session_ref` → the task is **idle** and resumes instantly next
     time). In a drive, pass the same `--driver` you used at claim:
     a clean stop with work left → `--next-action "<note>"`; a
     `kind=task` `done` → `--state done --output <work-note.md>` (the
     assigned persona's work note — built from the durable record, since
     a long cascade may have compacted earlier sessions; it is the
     persona's only ingested memory); a `kind=workflow` `done` →
     `--state done` (the per-step notes are the record); blocked or
     `sessions >= max_sessions` → `--state blocked --next-action
     "<why>"`. `release` refreshes `updated_at`; the `sessions` counter
     was already bumped at `claim` — do **not** bump it again.
  6. **Cascade — the hard loop, the driver's whole job.** Immediately
     loop back to step 1 **in the same turn** and continue: a task you
     released not-done is now idle (re-claim and keep going); a task you
     finished yields a different next pick. Run a task's whole
     `max_sessions` budget and the whole queue back-to-back in one
     sitting — **never one-session-per-loop-fire**. Don't hand the turn
     back or write a "drive summary" between sessions.
- **Stop conditions** — exit the loop in step 4 (for the current task)
  when **any** of these hold: the task is `done` **and**
  `early_exit=true` (with the default `early_exit=false`, do **not**
  stop here — keep iterating through `max_sessions`); a real blocker
  needs a human or another agent (`blocked`); `sessions >=
  max_sessions`; or the human ends the session. The cascade (step 6)
  ends the whole invocation only when the sweep reports nothing
  actionable, the human ends it, or you hit the genuine **context
  ceiling** — context pressure itself is **not** a stop reason (rely on
  auto-compaction; see the cascade rationale above).

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

**Adopting protocol updates on an existing team.** The skill and
`OPERATING.md` propagate to already-scaffolded teams **hash-gated**, so
an upgrade reaches them without clobbering local edits:
- `tigerharness init --refresh-skills` installs any missing bundled
  skill, **refreshes** any skill byte-identical to a previously-shipped
  version to the current one, **leaves hand-edited skills untouched**,
  and removes the retired mid-task compact override from
  `.claude/settings.json` (only when it still holds the old seeded
  default).
- `OPERATING.md` refreshes on the next `tigerharness journal new`: the
  scaffolder's `_ensure_operating_md` rewrites an on-disk file that
  matches a prior shipped template (`_PRIOR_OPERATING_HASHES`) to the
  current one, and leaves a hand-edited file alone.

## The lazy sweep (built into the driver)

Every `drive-journal` invocation begins with a sweep over
`active/*/status.json` before any task work happens, and is re-run
between cascaded tasks. **It calls no model and costs nothing beyond
the routine session.** Responsibilities:

- Move `done` tasks to `journal/done/` to keep `active/` lean.
- Classify every `in_progress` task via the `session_ref` attach token
  + heartbeat:
  - **idle** (`session_ref` cleared): cleanly detached — resumable
    **now**, no heartbeat wait.
  - **busy** (attached + `updated_at` within `stuck_timeout`): a live
    session owns it right now (the soft lease). Leave it alone.
  - **crashed** (attached + `updated_at` older than `stuck_timeout`):
    the owner wedged or went silent. Reclaimable by the next step-2
    rescue.
- Summarize what's actionable: e.g. *"2 pending, 1 resumable, 1 busy
  (owned by a live session), 1 crashed (no heartbeat 40m), 1 blocked
  awaiting your review."* The summary lands in the same
  interactive context where you're about to act on it, instead of a
  separate Slack DM from a daemon.
- **Cheap no-op fast path:** if every `in_progress` task is busy and
  nothing is idle / crashed / pending, the invocation stops right here —
  a frequent loop (every 5–10 min) is meant to no-op cheaply when work
  is already healthy, without reading task context or `OPERATING.md`.

No cron, no systemd unit, no background process. The sweep is plain
non-AI Python logic invoked at the start of the `drive-journal`
skill and at every cascade boundary, executing inside the human's
interactive session.

Note the contrast with the retired runner's `stuck_watchdog`: that
watchdog can **kill** a wedged subprocess because the api backend owns
one. The subscription backend owns no process — there is nothing to
kill. Stale handling is purely **advisory**: the sweep names the
stale task in-session and you decide whether to re-open it or close
it.

| Env var | Default | Purpose |
|---|---|---|
| `TIGERHARNESS_JOURNAL_DIR` | `<team>/journal/` | Journal root the scaffolder and driver operate on. |
| `TIGERHARNESS_JOURNAL_STUCK_TIMEOUT` | `1800` (30 min) | Heartbeat age (seconds) past which the sweep classifies an *attached* `in_progress` task as **crashed** (and below which it is **busy**). |

## The work loop

1. **Write a PRD.** A plain markdown file describing the task — like
   any brief you'd hand a teammate.
2. **Scaffold.** `tigerharness journal new --prd brief.md` (or the
   `journal-new` skill in the app) creates `active/<task-id>/` and
   seeds `status.json` as `pending`.
3. **Drive.** Open Claude Code, invoke `drive-journal`. The skill
   first runs the lazy sweep (archive any `done`, classify
   `in_progress` tasks as **idle / busy / crashed**, summarize what's
   actionable in-session — and stop cheaply if everything is busy);
   then it picks **one** actionable task — never a *busy* `in_progress`
   task, since that belongs to another active session — and runs it
   continuously, appending to `progress.md` and bumping the heartbeat
   in `status.json` as it goes, until a stop condition fires. Then it
   **cascades** — the hard loop: re-sweep and pick up the next
   session/task **back-to-back in the same turn**. One invocation
   drains the whole queue (and each task's whole `max_sessions` budget)
   in one sitting, never one-session-per-loop-fire; context pressure is
   handled by compaction, not by handing off.
4. **Resume if needed.** If the task isn't `done` (you stopped, it
   blocked, or it hit `max_sessions`), come back later and invoke
   `drive-journal` again — a fresh session resumes the idle task
   instantly (`journal claim` re-attaches; `next_action` + `progress.md`
   carry the context, so no vendor `--resume` is needed). The next
   invocation's sweep picks up where this one left off; state is durable
   on disk
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
fundamentally one driver doing one task at a time. And **finish
before you start**: if a *busy* `in_progress` task exists, step 2
declines to start a `pending` one and exits cleanly — a later task
may depend on the one in flight.

What prevents two invocations from clobbering each other on the same
task: the **`session_ref` attach token** as a soft lease, enforced by
`journal claim`. `claim` is an atomic **compare-and-set** on
`session_ref` — it re-reads and refuses if another session set the
token first ("busy" / "claim lost"), so two concurrent drivers can't
both own a task. The sweep classifies an attached + fresh task as
**busy** (step 2 leaves it alone); a cleanly **idle** (detached) task
is the one available to resume, and a **crashed** (attached + stale)
owner is reclaimable. The heartbeat only distinguishes busy from
crashed — it is no longer the lease itself.

The doc deliberately omits an explicit *hard* lock (cut from the MVP):
`claim`'s compare-and-set already closes the double-owner race for the
serial single-human workflow. A dedicated file lock / `lease.json` is
a Phase 2 hardening only if concurrent drivers ever become real.

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

- [`workflow-runner.md`](workflow-runner.md) — the multi-persona API
  backend driven by a pre-compiled workflow graph; already file-based,
  and the closest sibling to this design.

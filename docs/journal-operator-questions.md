# journal operator questions (async park-and-ask)

## At a glance

- **What:** a drive that hits a genuine decision it cannot resolve alone
  **parks that one task** instead of stalling the turn. It writes the
  question to the task's `questions.md`, moves the task to a new
  `journal/needs_input/` tray, and **cascades on** to the rest of the
  queue. The Operator answers in `questions.md`, runs `journal answer
  <id>`, and the task re-enters the active queue.
- **Why:** before this, a drive that needed an Operator decision had two
  bad options: **stall the interactive thread** waiting for a reply
  (which blocks the whole Claude Code app), or `release --state blocked`
  into a state with no question channel, no answer channel, and **no verb
  to reopen it**. Detached drives (autodrive / cron) had no way to ask at
  all. This makes "I need a human decision" a first-class, non-blocking,
  round-trippable event.
- **Must-not-miss:** the driver **never blocks the turn** on an Operator
  answer; the **folder move is the visual signal** (you can see what's
  waiting with `ls journal/needs_input/` even with Slack off); a Slack
  notification is **mandatory when Slack is configured**, and the tray is
  the fallback when it is not.

## Why a new state instead of reusing `blocked`

`blocked` already has an owner: a failed workflow compile sets
`state=blocked` + `compile_phase=failed` and **deliberately keeps the
task in `active/`** for the Operator to inspect / `compile-retry` /
`abort`. Overloading `blocked` to also mean "parked, waiting on an
Operator answer" — and moving those tasks to a tray — would entangle the
compile-fail flow and blur two different meanings ("this is broken" vs
"the ball is in your court").

So this feature adds a dedicated, isolated state:

- **`needs_input`** — a task voluntarily parked by the driver because it
  needs an Operator decision before it can proceed. It is **not
  actionable** by the cascade (like `blocked`), but unlike `blocked` it
  has a defined question channel, a defined answer channel, and a defined
  re-entry verb.

`blocked` keeps its exact current meaning and behaviour. Nothing about
compile-fail changes.

## The round-trip

```
driver hits a genuine fork it cannot resolve from the brief + its judgment
   |
   |  1. write the question to a file (context + options + recommendation + safe default)
   |  2. tigerharness journal release <id> --state needs_input --question <file>
   |       -> appends a formatted Q block to questions.md
   |       -> moves active/<id>/ -> needs_input/<id>/   (the visual signal)
   |       -> sets state=needs_input, detaches (session_ref=null)
   |  3. notify the Operator (slack-notify) -- MANDATORY when Slack is configured
   |  4. CASCADE ON to the next actionable task   <-- the queue never waited
   v
Operator reads questions.md, writes the answer under the question
   |
   |  tigerharness journal answer <id>
   |     -> moves needs_input/<id>/ -> active/<id>/
   |     -> state=needs_input -> in_progress (detached => idle)
   |     -> stamps next_action: "Operator answered -- read questions.md, continue"
   v
next drive fire sweeps active/, sees an idle in_progress task (top priority),
resumes it, reads the answer in questions.md, and continues
```

### Why re-enter as `in_progress` (idle), not `pending`

The task was mid-flight when it parked. The cascade's #1 priority is
"resume an idle `in_progress` task before starting any fresh `pending`
one" (finish-before-you-start). Re-entering as `in_progress` idle means
the Operator's answer is acted on **before** any new task begins, and the
resuming driver already has `progress.md` context to pick up from.

## The question / answer channel: `questions.md`

A plain markdown file at the task root (human- and AI-readable, like the
rest of the journal — no JSON to hand-edit on a phone). Append-only: each
park appends one or more Q blocks. The CLI owns the format so it is
consistent; the Operator only fills in the **Answer** section.

```markdown
# Operator questions -- <task-id>

## Q1 - 2026-06-26T14:00:00Z - asked by Anzai
**Status:** OPEN

**Context:** <what the driver was doing and why the decision matters>

**Question / decision needed:** <the ask, concretely>

**Options:**
- A) <option> -- <tradeoff>
- B) <option> -- <tradeoff>

**Recommendation:** <the driver's pick + one-line why>

**Safe default if you don't reply:** <what the driver would do, e.g. "go with A">

**Answer:**
> _(Operator: write your answer here, then run
> `tigerharness journal answer <task-id>`)_

---
```

`journal answer` does **not** parse the answer text (friendly markdown is
not reliably machine-parseable, and trying to gate on it would be
fragile). It trusts the Operator: the verb just reactivates the task. The
resuming driver reads the answer prose and acts on it. (A future
convenience could auto-detect a filled answer during the sweep; v1 keeps
the explicit verb because it is robust.)

## CLI surface

### `release --state needs_input --question <path>`

Extends the existing `release` verb with a new exit state and one flag.

- `--state needs_input` is added to the `--state` choices.
- `--question <path>` points at a markdown file the driver wrote with the
  question. **The question is the ticket:** `release --state needs_input`
  REFUSES (non-zero, no state change, no move) if `--question` is missing
  or its file is empty — symmetric with the `--output` done-note gate.
- On success: append a formatted Q block to `questions.md` (creating it
  if absent), set `state=needs_input`, clear `session_ref` (detach),
  refresh `updated_at`, then **move `active/<id>/` -> `needs_input/<id>/`
  eagerly** (immediate visibility — unlike `done`, which the sweep
  archives lazily; parking's whole point is to be seen now).
- It prints a loud reminder: parked to `needs_input/`, and **notify the
  Operator now** (Slack mandatory when configured).
- `--next-action` is optional and, if given, recorded as usual.

The CLI does **not** send Slack itself (the journal layer is model-free
and transport-free, matching the rest of the backend). Notification is
the driver's responsibility via the `slack-notify` skill, mandated by the
protocol below.

### `journal answer <task-id> [--note "<text>"]`

The Operator-run re-entry verb (the missing inverse of park).

- Reads `status.json` from `needs_input/<id>/`. Refuses if the task is
  not in the tray or its state is not `needs_input`.
- Sets `state=in_progress`, `session_ref=null` (=> idle, instantly
  resumable), refreshes `updated_at`.
- Stamps `next_action` = `--note` if given, else the default
  `"Operator answered -- read questions.md and continue"`.
- Moves `needs_input/<id>/` -> `active/<id>/`.

## The sweep learns the tray

`sweep` gains a `needs_input` count surfaced in `to_summary()` (e.g.
`"2 needs-input"`). Tasks in `needs_input/` are **not** classified as
actionable — they are out of the active queue by virtue of living in a
different tray, so the cascade physically cannot pick them up (no "skip"
logic needed). The count is purely informational, so the Operator sees at
a glance how many tasks are waiting on them.

## Driver protocol (OPERATING.md + drive-journal skill)

The behavioural contract — the part that actually fixes the pain:

1. **A drive never blocks the turn waiting on the Operator.** If you hit
   a question/decision you cannot resolve, park the one task and cascade
   on. The rest of the queue does not wait.

2. **Decide-and-document is the default; parking is the exception.** Most
   apparent "questions" in a drive should be resolved by making a
   reasonable, documented decision and proceeding (the detached-mode
   discipline). This integrates with the existing `autonomy` field:
   - `autonomy=judgement` — self-resolve yellow-light calls, log a
     `Decision:` entry in `progress.md`, and proceed. Park only a true
     red-light / irreversible / brief-contradicting fork.
   - `autonomy=ask` — park genuine judgment calls rather than guessing,
     but still resolve the trivial ones yourself.
   Either way, **every parked question carries a safe default**, so "use
   your judgment" is always a valid one-word Operator reply.

3. **Notify after parking.** When Slack is configured for the team, the
   driver MUST send a skimmable `slack-notify` summary (task id, the
   decision needed, the safe default, and "answer in questions.md then
   run `journal answer <id>`"). When Slack is not configured, the
   `needs_input/` tray + the CLI's console reminder are the fallback
   visual signal.

4. **On resume, consume the answer.** A drive that resumes a task whose
   `next_action` points at `questions.md` reads the answer first, then
   continues. It does **not** re-ask the same question (if the Operator
   said "use your judgment", proceed with the safe default).

## What this is not (non-goals for v1)

- No automatic answer detection in the sweep (explicit `journal answer`
  verb only).
- No machine-validated answer schema (friendly markdown; trust the
  Operator).
- No change to `blocked` semantics or the compile-fail flow.
- No new notification transport — reuses `slack-notify`.

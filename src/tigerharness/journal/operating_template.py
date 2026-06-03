"""The canonical ``OPERATING.md`` content, shipped as a Python string.

The scaffolder copies this to ``<journal>/OPERATING.md`` on **first
use only** -- if the file already exists at the journal root,
``_ensure_operating_md`` leaves it untouched (it has no "is this the
template we shipped vs. a human edit" detector; it just checks
existence). That means a tigerharness upgrade that ships a new
template here does NOT auto-update an existing journal's
``OPERATING.md``. Operators who want the newer template must delete
or rename the on-disk file first; the next ``journal new`` then
installs the fresh content.

Keeping the source in Python rather than in ``docs/`` lets us version
the protocol alongside the model and import it cheaply in tests.
"""

from __future__ import annotations


OPERATING_MD = """\
# OPERATING.md -- journal driver protocol (Phase 1 + 1.5)

This file is the vendor-neutral contract that teaches any file-reading
agent how to drive the journal. The interactive Claude Code app reads
this via the `drive-journal` skill, but the contract is identical for
any other vendor's agent that learns to read it.

## Task kinds

Two kinds of tasks live side-by-side in `active/`:

- `kind=task` -- single-persona, free-form work driven by a PRD
  (`task.md`). The protocol below describes its lifecycle.
- `kind=workflow` -- multi-persona, graph-walked work compiled from a
  team playbook. A workflow task that has `compile_pending=true` MUST
  run the **compile sub-protocol** (see bottom of this file) before
  any graph-walking. Once `compile_pending=false`, walk the graph
  described by `orchestration.json`.

The driver branches on `status.kind` at step 4 (work the task). The
sweep / pick / cascade scaffolding is identical for both kinds.

## Where state lives

- Root: this directory.
- `active/<task-id>/` -- in-flight tasks.
  - `task.md` (kind=task) or `task_brief.md` + `playbook_snapshot.md`
    (kind=workflow) -- the PRD / brief.
  - `status.json` -- the state machine. **Single source of truth.**
  - `progress.md` -- append-only narrative log.
  - `artifacts/` -- whatever the task produces.
  - `compile/` (kind=workflow only) -- the in-flight compile workspace
    (round files, transcript). Preserved on success and on abort for
    audit; deleted only by manual cleanup.
  - `orchestration.json` + `steps/` (kind=workflow, post-compile only)
    -- the compiled graph the executor walks.
- `done/<task-id>/` -- archived tasks, same shape, moved here by the
  sweep when `state=done` (or by `journal abort` for failed
  workflows).

## How to read state

`status.json` is the schema described in
`docs/subscription-backend.md` (status.json field table). The
load-bearing fields are:

- `state` -- one of `pending`, `in_progress`, `blocked`, `done`.
- `updated_at` -- the **heartbeat**, refreshed on every `progress.md`
  append. If older than `stuck_timeout` (default 30 min), the task is
  classified as **stale in_progress** and reclaimable. Otherwise it is
  classified as **fresh in_progress** and another session is assumed
  to own it right now.
- `next_action` -- the handoff note. A fresh session reads this and
  the tail of `progress.md` to resume without re-reading everything.
- `sessions` / `max_sessions` -- soft ceiling. When `sessions ==
  max_sessions`, the driver moves the task to `blocked` with a
  `next_action` naming the cap as the blocker.
- `kind` -- `task` or `workflow`. The driver switches behaviour at
  step 4 on this field.
- `compile_pending` + `compile_phase` (workflow only) -- the compile
  sub-state machine. `compile_pending=true` means the graph has not
  been built yet; the driver must run the compile sub-protocol before
  walking. `compile_phase` is one of `pending`, `drafting`,
  `tier1_pre`, `critiquing`, `tier1_post`, `complete`, `failed`.

## The decision procedure

Run on every `drive-journal` invocation and looped after each
completed task until no actionable tasks remain.

1. **Lazy sweep** -- run `tigerharness journal sweep`. It will:
   a. Archive any `done` task (`active/<id>/` -> `done/<id>/`).
   b. Classify every `in_progress` task as fresh or stale by
      comparing `updated_at` to `stuck_timeout`.
   c. Print the summary (pending / in_progress-fresh / in_progress-
      stale / blocked counts) into the session.

2. **Pick exactly ONE actionable task** -- never multiple in parallel:
   - A `pending` task to start, OR
   - A *stale* `in_progress` task to **rescue** (read the tail of
     `progress.md` first to understand where the previous owner left
     off, then resume from `next_action`).
   - **NEVER** pick a *fresh* `in_progress` task -- the heartbeat is
     the soft lease; another session owns it right now. Leaving it
     alone is the correct behaviour.
   - Skip `blocked` tasks; surface them in the summary so the human
     can unblock manually.
   - Prefer the one with the oldest heartbeat among the candidates.
   - If no actionable task remains, exit the invocation cleanly.

3. **Read context** -- for `kind=task`: `task.md` (the PRD),
   `status.json.next_action`, and the tail of `progress.md`. For
   `kind=workflow`: `task_brief.md`, `playbook_snapshot.md`,
   `status.json.next_action`, and the tail of `progress.md`.

4. **Work the task continuously** -- branch on `status.kind`:

   - **`kind=task`** -- do the real work, appending to `progress.md`
     and refreshing `updated_at` as a heartbeat as you go (at least
     once every 10 minutes of active work).
   - **`kind=workflow`** -- if `status.compile_pending=true`, run the
     **compile sub-protocol** (see below) FIRST. After compile
     completes, walk the graph in `orchestration.json` per the
     graph-walk sub-protocol (see below). Both sub-protocols append
     to `progress.md` and refresh `updated_at` on the same cadence.

   Keep going until **one** of the stop conditions below fires. Do
   NOT stop after a single step just because progress was made; the
   goal is to take the task as far as possible in this session.

5. **On stop**, write a final `progress.md` entry summarising the
   session and update `status.json`:
   - Refresh `updated_at` one last time.
   - **Do NOT bump `sessions` here.** The counter is incremented once
     per *pickup* (when step 2 first picks the task on the current
     invocation), not on exit. Bumping on exit too would double-count.
   - Rewrite `next_action` (or clear it if `done`).
   - Set `state` to one of:
     - `done` -- task fully complete per acceptance criteria.
     - `blocked` -- real blocker (need human, missing input, etc.) OR
       `sessions == max_sessions`.
     - leave as `in_progress` -- clean stop (human ended session).

6. **Cascade** -- if step 5 moved the task to `done` or `blocked`,
   loop back to step 1 (re-sweep) and pick up the next actionable
   task. Keep cycling until:
   - Step 1 reports nothing actionable, OR
   - The human ends the session, OR
   - A session-level guard rail fires.

   Don't manufacture a stopping point just because one task finished
   -- drain the queue while the session is hot.

## Stop conditions for step 4

Exit the inner loop on **any** of:

- The task is fully `done` per its acceptance criteria.
- A real blocker requires a human or another persona (`blocked`).
- `sessions == max_sessions` -- write a `blocked` entry naming the
  cap as the blocker.
- The human ends the session.

Otherwise keep going. Do not hand the turn back to manufacture a
checkpoint mid-task.

## Heartbeat cadence

Bump `updated_at` on every `progress.md` append. Append progress at
least every 10 minutes of wall-clock active work. A wedged session
will show as stale once `updated_at` exceeds `stuck_timeout` (default
1800 seconds = 30 min), and a *later* invocation will rescue it via
step 2.

## What NOT to do

- Do NOT pick a fresh `in_progress` task (another session owns it).
- Do NOT skip the sweep -- it's how the protocol stays correct.
- Do NOT mutate `status.json` mid-task except to refresh `updated_at`
  and (on stop) write the final exit state. Anything more invasive
  belongs in the journal layer's CLI, not in the driver session.
- Do NOT pick multiple tasks in parallel within one invocation.

## Compile sub-protocol (kind=workflow, compile_pending=true)

This is the in-session compile: NO `claude -p`, NO API billing. The
session itself adopts each persona via the persona-switching mechanic
below, and shells out only to the Python validators / promotion CLIs.

Trigger: step 4 on a `kind=workflow` task where
`status.compile_pending=true`.

The compile proceeds in rounds. Each round is one drafter turn
followed by both critics, then a Tier 1 re-validation. The drafter is
**Anzai**, the critics are **Akagi** and **Ayako**. A round ends with
both critics emitting `APPROVE` -> land. If either critic emits
`BLOCK` -> mark the task compile-failed (`journal compile-fail
<task-id> --reason '...'`). Otherwise the loop continues with the
critics' `REVISE` feedback merged for the next drafter turn.

Caps (compile gives up rather than spinning forever):

- **3 consecutive Tier 1 failures** on the same draft -> compile-fail
  with `next_action="compile failed at tier1_pre: <last validator errors>"`.
- **8 rounds total** without dual-APPROVE -> compile-fail with
  `next_action="compile failed at critiquing: <last round verdicts>"`.

Both caps land the task in `state=blocked` + `compile_phase=failed`.
The task stays in `active/` for the human to inspect. The operator
then chooses one of:

- `tigerharness journal compile-retry <task-id>` -- wipes
  `compile/`, resets the status to scaffold-time shape
  (`state=pending`, `compile_pending=true`,
  `compile_phase=pending`), and lets the next `drive-journal` retry
  the compile from scratch. Brief + playbook snapshot preserved.
- Edit the playbook and re-scaffold a fresh task.
- `tigerharness journal abort <task-id>` -- archive to `done/`,
  preserving `compile/` for forensics.

### Persona switching (uniform mechanic)

Every persona turn -- compile or graph-walk -- starts with this
four-line preamble inside the session:

```
PERSONA: <name>
ROLE: <role from the playbook or step bundle>
STEP: <step-id or "compile-draft" / "compile-akagi" / "compile-ayako">
OBJECTIVE: <one sentence about what this turn must produce>
```

The persona's prompt body (read from `teams/<team>/personas/<name>/prompt.md`)
follows the preamble. The session writes the persona's turn output
verbatim into `compile/round-NN.json` (round-granular checkpoint), then
ends every turn with one trailer line:

```
WORKFLOW: APPROVE
WORKFLOW: REVISE -- <one-paragraph feedback>
WORKFLOW: BLOCK -- <one-paragraph rationale>
```

This trailer is the machine-readable verdict. `APPROVE` means "this
artifact is good enough to land"; `REVISE` means "feedback follows --
re-draft"; `BLOCK` means "this is unsalvageable -- abort". For
drafter turns the trailer is always `APPROVE` (the drafter never
self-rejects); critics carry the real verdict.

### Step-by-step

1. **Bootstrap.** Run:

   ```bash
   tigerharness journal compile-context <task-id>
   ```

   This prints brief + playbook + roster + the round-1 drafter prompt
   in one block. Read it.

2. **Drafter turn (Anzai).** Adopt the persona via the four-line
   preamble. Emit a `steps-bundle` per the drafter contract (one fence,
   `## step: <id>` headers, frontmatter blocks). End with
   `WORKFLOW: APPROVE`.

3. **Tier 1 (pre-critique).** Save the drafter's bundle to
   `compile/round-NN-draft.md` and run:

   ```bash
   tigerharness journal validate-graph --task <task-id> --draft <path>
   ```

   The command emits `{ok, errors, trace}`. If `ok=false`, treat the
   errors as feedback for the next drafter turn and loop back to step
   2. Do NOT proceed to critics until Tier 1 passes -- that would
   waste critic effort on a malformed graph. Cap: 3 consecutive Tier
   1 failures -> `journal compile-fail`.

4. **Akagi critique.** Run:

   ```bash
   tigerharness journal compile-prompts --task <task-id> \\
       --kind akagi --draft <path> --trace <trace-path>
   ```

   The trace from step 3 goes here. Adopt Akagi via the preamble and
   emit a critique ending with `WORKFLOW: APPROVE` /
   `WORKFLOW: REVISE -- ...` / `WORKFLOW: BLOCK -- ...`. Save the full
   turn to `compile/round-NN-akagi.md`.

5. **Ayako critique.** Same as step 4 but with `--kind ayako` and save
   to `compile/round-NN-ayako.md`.

6. **Round verdict.** Combine the two critic verdicts:

   - **Either BLOCK** -> `tigerharness journal compile-fail <task-id>
     --reason '<one-paragraph postmortem>'`. The task moves to
     `state=blocked` + `compile_phase=failed`; surface the failure to
     the human and stop the cascade. The task is NOT archived -- the
     human runs `journal abort` after inspecting `compile/`.
   - **Both APPROVE** -> proceed to step 7 (land).
   - **Anything else** -- one or both `REVISE` -- merge the critics'
     feedback into a single block (use the `--feedback` argument to
     the next drafter prompt) and loop back to step 2. Cap: 8 rounds
     total -> `journal compile-fail` (same shape as the BLOCK case).

7. **Tier 1 (post-critique).** Defensive re-validation: a critic may
   have asked the drafter to make a change that re-broke a Tier 1
   invariant. Re-run `validate-graph` on the final drafter bundle. If
   it fails, loop back to step 2 with the new errors as feedback.

8. **Land.** Run:

   ```bash
   tigerharness journal land-compile --task <task-id> \\
       --draft <final-draft-path> \\
       --transcript <compile/transcript.md> \\
       --rounds <NN>
   ```

   `land-compile` defensively re-runs Tier 1 a third time, builds
   `Orchestration`, writes step files + `orchestration.json` +
   `compile_critique.md` atomically into the task directory, and
   flips `status.compile_pending=false` +
   `status.compile_phase=complete` LAST (the visibility gate).

After step 8 the compile is done. Continue at step 4 of the outer
protocol -- the graph-walk sub-protocol described next.

### Heartbeat during compile

Bump `updated_at` after every round (step 6) at minimum. A wedged
compile session is still subject to the stale-rescue rule: a later
invocation finding `compile_pending=true` + a stale heartbeat resumes
from `compile_phase` -- the round files in `compile/round-NN-*.md`
are the resume points.

## Graph-walk sub-protocol (kind=workflow, compile_pending=false)

Walk the DAG in `orchestration.json`. Each step is a persona turn
using the same four-line preamble + `WORKFLOW: APPROVE|REVISE|BLOCK`
trailer. The trailer drives the routing:

- `APPROVE` -> follow the step's `on_approve` edge.
- `REVISE` -> follow `on_revise` (typically loops back to the same
  step with feedback, bounded by `max_iters`).
- `BLOCK` -> follow `on_block` (typically `__escalate__` -> set
  `status.state=blocked` with a postmortem `next_action`).

Sentinels: `__done__` ends the walk with `status.state=done`;
`__escalate__` ends with `status.state=blocked`. Otherwise the next
edge value is the next step id. Bump `updated_at` after every step.

If a step is in `parallel_with`, the runtime may dispatch the listed
steps concurrently; the journal driver does NOT need to thread these
itself -- it asks each step for its trailer in document order and
honours all `parallel_with` edges as a group barrier.
"""

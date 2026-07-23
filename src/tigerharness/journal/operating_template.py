"""The canonical ``OPERATING.md`` content, shipped as a Python string.

The scaffolder installs this to ``<journal>/OPERATING.md`` via
``_ensure_operating_md``, which is **hash-gated** (see ``scaffold.py``):
- no file on disk -> write the current template;
- on-disk content byte-identical to the current template -> no-op;
- on-disk content byte-identical to a *previously shipped* template
  (its sha256 is in ``_PRIOR_OPERATING_HASHES``) -> **refresh** it to the
  current template, so a tigerharness upgrade propagates to an existing
  journal on its next ``journal new`` with no manual delete;
- otherwise (a hand-edited file matching no shipped version) -> leave it
  untouched, so a human's customization is never clobbered.

When you change this string, append the OLD rendered content's sha256 to
``scaffold._PRIOR_OPERATING_HASHES`` so the now-prior version is still
recognized as "ours, safe to refresh" on existing journals.

Keeping the source in Python rather than in ``docs/`` lets us version
the protocol alongside the model and import it cheaply in tests.
"""

from __future__ import annotations


OPERATING_MD = """\
# OPERATING.md -- journal driver protocol (Phase 1 + 1.5 + per-persona memory)

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
  - `worklog/` -- per-turn, persona-attributed work notes (one
    markdown file per turn, `NNNN-<persona>-<step>.md`). Written
    **only** by the journal gates (`claim` / `release` / `step-done` /
    `land-compile`), never by hand. These ARE the per-persona memory
    records tiger-memory ingests, and they survive archival to `done/`.
  - `artifacts/` -- whatever the task produces.
  - `compile/` (kind=workflow only) -- the in-flight compile workspace
    (round files, transcript). Preserved on success and on abort for
    audit; deleted only by manual cleanup.
  - `orchestration.json` + `steps/` (kind=workflow, post-compile only)
    -- the compiled graph the executor walks.
- `done/<task-id>/` -- archived tasks, same shape, moved here by the
  sweep when `state=done` (or by `journal abort` for failed
  workflows).
- `needs_input/<task-id>/` -- **parked** tasks awaiting an Operator
  answer, moved here from `active/` by `journal release --state
  needs_input` (which also writes/appends `questions.md` -- the
  Operator's ticket). The folder move is itself the visual signal even
  when Slack is not configured. `journal answer <task-id>` moves the task
  back to `active/` as a resumable `in_progress`. See "Parking on an
  Operator question."

## How to read state

`status.json` is the schema described in
`docs/subscription-backend.md` (status.json field table). The
load-bearing fields are:

- `state` -- one of `pending`, `in_progress`, `blocked`, `needs_input`,
  `done`. A `needs_input` task is **parked** on an Operator question: it
  lives in the `needs_input/` tray (not `active/`), out of the actionable
  queue until the Operator answers. See "Parking on an Operator question."
- `session_ref` -- the **attach token**. Set (an opaque id) while a
  session is actively driving the task; `null` when no session is
  attached (cleanly handed off, or never claimed). This is the signal
  that distinguishes "a live session owns this" from "paused and ready."
- `updated_at` -- the **heartbeat**, refreshed while a session drives
  the task. It is consulted **only** to tell a *busy* attached task from
  a *crashed* one -- NOT to gate resuming a detached task. An
  `in_progress` task is classified as:
  - **idle** -- `session_ref` is `null`: cleanly handed off (or never
    claimed). Resumable **immediately**, no heartbeat wait.
  - **busy** -- `session_ref` set + heartbeat fresh (within
    `stuck_timeout`, default 30 min): a live session owns it right now
    (the **soft lease**); leave it alone.
  - **crashed** -- `session_ref` set + heartbeat stale (older than
    `stuck_timeout`): the owner went silent; reclaimable.
- `next_action` -- the handoff note. A fresh session reads this and
  the tail of `progress.md` to resume without re-reading everything.
- `sessions` / `max_sessions` -- soft ceiling. When `sessions >=
  max_sessions` the budget is spent: the driver marks the task `done`
  if the work is complete, else `blocked` with a `next_action` naming
  the cap.
- `early_exit` -- bool. When `false` (the **default**), the driver runs
  the full `max_sessions` budget -- "N iterations means exactly N" --
  and does NOT mark the task `done` before the budget is spent (it keeps
  iterating: review, harden, extend). When `true`, the driver may mark
  `done` as soon as the acceptance criteria are met, before the budget
  is spent. Mirrors the retired runner's `--early-exit`.
  **Meaningful for `kind=task` only.** For `kind=workflow` the compiled
  graph is the authority on completion: when the walk reaches `__done__`
  the task is released `done` even with budget remaining. There
  `max_sessions` is a runaway ceiling, not a quota to fill -- the
  iteration depth lives in the graph's own critique loops.
- `kind` -- `task` or `workflow`. The driver switches behaviour at
  step 4 on this field.
- `compile_pending` + `compile_phase` (workflow only) -- the compile
  sub-state machine. `compile_pending=true` means the graph has not
  been built yet; the driver must run the compile sub-protocol before
  walking. `compile_phase` is one of `pending`, `drafting`,
  `tier1_pre`, `critiquing`, `tier1_post`, `complete`, `failed`.

## Per-persona memory (why the gates take --driver / --output)

A drive runs entirely inside **one** session (yours), which adopts each
persona in-session via the four-line preamble. Left alone, tiger-memory
would fold the whole drive transcript into a single owner -- so a
specialist's work would land in the driver's memory, not its own.

The fix is the **worklog**: each persona turn ends by handing its output
to a journal gate that writes a worklog entry **stamped with that
persona, from the orchestration/compile mapping or `status.json` -- never
typed free-hand**, so attribution can't be wrong. *The note is the
ticket to advance:* the gate refuses to move the work forward without a
non-empty note. tiger-memory then reads the worklog (not the transcript)
and files each slice in the right persona's store.

To make this work, a Slack-driven drive identifies itself with **one
flag** it already knows:

- `--driver <your-persona>` -- the persona THIS session runs as (your
  bridge identity, e.g. `Anzai`). It writes a one-line "I drove this"
  trace to your own memory, switches on the completion gates, AND tells
  `claim` to register this drive so tiger-memory does NOT *also* fold the
  fat transcript into your store (the worklog already owns that content).

You do **not** copy the thread id by hand. The slack bridge exports
`TIGERHARNESS_SLACK_THREAD_TS` (the `slack_thread_ts` from the
`[bridge-context]` block) into every turn's environment, and `claim`
reads it automatically when `--driver` is present. An explicit
`--drive-thread <thread_ts>` still overrides it if you ever need to (a
non-bridge driver, or a manual run).

Outside a Slack-driven drive (a plain terminal with no `[bridge-context]`
and no persona identity), **omit `--driver`** -- claim/release behave
exactly as the plain subscription backend with no memory side-effect.

## The Slack rail rule (hard, red-light)

Slack-triggered (bridge-spawned) sessions bill API tokens: they may
SCHEDULE journal tasks (`journal new`) but must NEVER drive them -- no
`drive-journal`, no claim, no graph-walk, no compile turns; driving
belongs to the subscription rail (interactive sessions). `journal
claim` enforces this mechanically: it refuses when
`TIGERHARNESS_SLACK_THREAD_TS` is set in the environment unless the
deliberate `--allow-api-drive` override is passed. Rails and billing:
`docs/subscription-backend.md` in the tigerharness repo.

## The decision procedure

Run on every `drive-journal` invocation and looped after each
completed task until no actionable tasks remain.

1. **Lazy sweep** -- run `tigerharness journal sweep`. It will:
   a. Archive any `done` task (`active/<id>/` -> `done/<id>/`).
   b. Classify every `in_progress` task as **idle** (detached),
      **busy** (attached + fresh), or **crashed** (attached + stale).
      "Fresh" means the heartbeat is within the stuck-timeout (default
      30 min; override `TIGERHARNESS_JOURNAL_STUCK_TIMEOUT`, seconds).
   c. Print the summary (pending / resumable / busy / crashed / blocked
      / needs-input counts) into the session. Parked (`needs_input`)
      tasks are surfaced for visibility but are **never actionable** --
      only the Operator reopens them via `journal answer`.
   d. **Cheap no-op fast path.** If every `in_progress` task is **busy**
      and nothing is idle / crashed / pending, you are done -- **stop the
      invocation here, without reading any task context or this file's
      later sections.** A healthy live session already owns the work; a
      frequent loop (every few minutes) is *designed* to no-op cheaply at
      this point. Only an **idle** task (detached) is yours to resume.

2. **Pick exactly ONE actionable task** -- never multiple in parallel.
   Resolve candidates in this **priority order (finish before you
   start)** so a task and *all* its sessions complete before any new
   task begins -- a later task may depend on the one already in flight:

   a. **A *resumable* `in_progress` task -> resume it.** That is an
      **idle** task (cleanly handed off -- resume **immediately**, no
      wait) or a **crashed** task (rescue). Read the tail of
      `progress.md` and continue from `next_action`. Among several,
      prefer the oldest heartbeat.
   b. **Else, if a *busy* `in_progress` task exists, do NOT start new
      work -- exit the invocation cleanly.** A live session owns it right
      now (the soft lease); let it finish before any `pending` task
      begins. A later invocation resumes it once it goes idle (clean
      hand-off) or crashed (the owner's heartbeat ages out).
   c. **Else, start the oldest `pending` task** -- reached only when
      nothing is `in_progress`.
   d. **Else, if the sweep listed a `deferred/` inbox entry,
      materialize the oldest** -- run `tigerharness journal
      materialize <deferred-id>` (subscription rail; it scaffolds via
      the same path as `journal new`, persona preflight included),
      then re-sweep and claim the new task. A malformed entry exits 1
      with a JSON envelope and STAYS in the inbox: surface it to the
      human, skip it, and continue. Deferred entries are written by
      the Slack-side `journal defer` verb (the cheap API-rail
      scheduler -- verbatim conversation only; see the journal-new
      skill).

      **Materialize is a preparatory step, NOT a turn-end. Do NOT stop
      here.** Materializing lands the new task as `pending` -- continue
      in the SAME turn: re-sweep -> claim -> compile -> walk it (or the
      next actionable task), exactly as the step-6 cascade demands.
      Stopping at this seam (leaving the fresh task `pending` for the
      next drive fire) re-introduces the one-session-per-loop-fire
      anti-pattern step 6 exists to kill. The only legitimate turn-ends
      remain: nothing actionable in the sweep, a real blocker, the human
      ends the session, or the genuine context ceiling.

   **Claim it atomically.** Once you have chosen a task, run
   `tigerharness journal claim <task-id>` BEFORE doing any work. Claim
   sets `session_ref` to a fresh token, flips the task to `in_progress`,
   bumps `sessions`, and refreshes the heartbeat -- atomically, with a
   compare-and-set re-read so two concurrent drives cannot both grab it.
   If claim exits non-zero ("busy" / "claim lost"), another session won:
   re-sweep and pick again, or exit.

   **In a Slack-driven drive**, add the driver flag (see "Per-persona
   memory" above):

   ```bash
   tigerharness journal claim <task-id> --driver <your-persona>
   ```

   `--driver` is the persona this session runs as; the drive's
   `thread_ts` is picked up automatically from the
   `TIGERHARNESS_SLACK_THREAD_TS` env var the bridge sets (pass
   `--drive-thread <thread_ts>` only to override). Omit `--driver`
   outside a Slack-driven drive.

   - **NEVER** work a *busy* task -- the attach token + fresh heartbeat
     means a live session owns it right now.
   - Skip `blocked` tasks; surface them in the summary so the human can
     unblock manually.
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
   session, then run **`tigerharness journal release <task-id>`** to
   record the exit and **detach** (clear `session_ref`). In a drive,
   pass the same `--driver <your-persona>` you used at `claim` -- it
   activates the completion gates below. (Outside a drive, omit
   `--driver`: release is the plain stop with no gate.)
   - Clean stop, work remains: `release <id> --driver <p> --next-action
     "<handoff note>"` (leaves `state=in_progress`). Detaching makes the
     task **idle**, so the very next drive resumes it **immediately** --
     no 30-minute wait.
   - **`kind=task` done**: `release <id> --driver <p> --state done
     --output <work-note.md>`. The note is the **assigned persona's**
     work record (what was built / decided / produced); the gate stamps
     it with `status.json`'s assigned `persona` and files it in that
     persona's memory. **The note is the ticket** -- `release --state
     done` REFUSES (non-zero, no state change) without a non-empty
     `--output`. Write the note file first, then release.
     **Build the note from the durable record** -- `progress.md`, the
     task's `artifacts/`, prior worklog entries -- *not* from in-context
     memory alone. For a `kind=task` this single done-note is written
     **once, at the end**, so across a long cascade the early sessions may
     have been **compacted away**; and tiger-memory ingests only
     `worklog/` (never `progress.md`), so this note is the assigned
     persona's *only* substantive memory of the whole task. Reconstruct
     it from what survived on disk.
   - **`kind=workflow` done**: `release <id> --driver <p> --state done`
     -- **no `--output`** here. The per-step notes you already wrote via
     `journal step-done` ARE the record. The gate refuses unless the
     graph walk reached `__done__` (every executed step left its note).
   - Blocked (real blocker, or `sessions >= max_sessions`):
     `release <id> --driver <p> --state blocked --next-action "<why>"`.
   - **Parked on an Operator question** (you hit a judgment call or a
     decision you genuinely cannot resolve yourself): write the question
     to a file, then `release <id> --driver <p> --state needs_input
     --question <question.md>`. This appends the question to the task's
     `questions.md`, moves the task to the `needs_input/` tray, and
     detaches. **Do NOT stall the turn waiting for an answer** -- park,
     notify the Operator, then fall straight into the step-6 cascade:
     re-sweep and pick the next actionable task. Parking is a clean stop,
     **not** a turn-end -- the rest of the queue keeps moving. See
     "Parking on an Operator question."

   `release` refreshes `updated_at` and clears `session_ref` for you.
   Do NOT bump `sessions` here -- that happened atomically in `claim`
   at pickup. For a mid-task heartbeat (not a stop), just append to
   `progress.md` and refresh `updated_at`; keep `session_ref` set while
   you are still driving.

6. **Cascade / keep going -- the hard loop. THIS is the driver's whole
   job.** After a stop, **immediately** loop back to step 1 (re-sweep) and
   pick up the next actionable task **in the SAME turn**. ⛔ Do **NOT** hand
   the turn back between sessions -- do not end your turn, write a "drive
   summary", or wait for the next loop fire:
   - If you moved a task to `done` / `blocked`, the next pick is a
     different task.
   - If you **parked** a task as `needs_input`, it has left the active
     queue (it lives in the `needs_input/` tray now), so the next pick is
     a **different** actionable task -- do **NOT** try to re-claim the
     parked one; it is gone until the Operator answers. (After notifying
     the Operator, just loop back to step 1 like any other stop.)
   - If you cleanly stopped a task that is NOT done (a natural
     checkpoint, or you split a long job into sessions), it is now
     **idle** -- re-`claim` it and continue **immediately**, no wait.

   Drive a task through its whole session budget *in this one sitting*:
   a task scaffolded with `max_sessions=10` runs its sessions one after
   another here, **NEVER one-session-per-loop-fire** (that anti-pattern
   stretches a day of work across a day of 30-min waits). Stop the loop
   only when the task is `done` / `blocked`, `sessions >= max_sessions`,
   step 1 reports nothing actionable, the human ends the session, or you
   hit the genuine **context ceiling** (see below).

   **Context is NOT a stop reason -- compact instead.** Every session
   checkpoints to `progress.md` + `next_action`, so a context
   **compaction loses nothing**: after one, just re-orient from those.
   There is NO configured mid-task compaction (retired by Operator
   ruling 2026-06-11: compacting in the middle of a task can cause
   unexpected results); the CLI's own auto-compact fires only near the
   hard context limit. A session that nears the ceiling checkpoints to
   `progress.md` + `next_action` and hands off (release idle + end the
   turn) -- instant-resume picks the task back up with fresh context.
   The only proactive compaction is the bridge's idle compaction
   (between tasks, journal idle -- see docs/slack-bridge.md). **If a
   compaction does happen, re-sweep (step 1) and continue.**

   Compaction loses nothing **for continuity** (you re-orient from
   `progress.md`) -- and nothing **for memory** *either*, **provided** a
   `kind=task` done-note is assembled from the durable record (step 5),
   since that note is written once at the end and is the only thing
   ingested. (`kind=workflow` is inherently safe: `step-done` writes each
   step's note immediately, before any later compaction.)

   Don't manufacture a stopping point just because one task finished or
   the conversation feels long -- drain the queue while the session is hot.

   **Idle-maintenance tail.** When the drive ends because the sweep found
   *nothing actionable and nothing busy* (queue drained), run the team's
   two self-gating maintenance chores before stopping: (1)
   `tigerharness slack-bridge compact-idle` -- plain CLI orchestration
   whose only model call is the single bounded `/compact` turn it may
   send per heavy, quiet Slack bridge lane, and only when the team opted
   in and the journal is idle; (2) the team's memory sweep
   (the `sweep-memory` skill) -- self-gating via its staleness floor,
   watermark, and soft lease, so a fresh team costs a few tokens. Skip
   the tail entirely when anything is busy or when you are stopping at
   the context ceiling (hand off instead). Both chores are cheap no-ops
   when fresh, so a frequent idle loop fire stays cheap.

## Stop conditions for step 4

Exit the inner loop on **any** of:

- The task is fully `done` per its acceptance criteria **and**
  `early_exit=true`. If `early_exit=false` (the default), do NOT stop
  here -- keep iterating (review, harden, extend) and spend the session
  budget; "N iterations means exactly N". On the final session do the
  full work pass first, *then* `release --state done` at its end -- do
  NOT treat merely reaching the cap on entry as "already done" and skip
  the work. Mark `done` earlier only when `early_exit=true`.
  (**`kind=task` semantics.** For `kind=workflow` the walk reaching
  `__done__` ends the task immediately -- `release --state done` --
  regardless of `early_exit` or remaining budget.)
- A real blocker requires a human or another persona (`blocked`).
- `sessions >= max_sessions` -- the budget is spent. Mark `done` if the
  work is complete, else `blocked` with a `next_action` naming the cap.
- The human ends the session.

Otherwise keep going. Do not hand the turn back to manufacture a
checkpoint mid-task.

## Heartbeat cadence

Bump `updated_at` on every `progress.md` append. Append progress at
least every 10 minutes of wall-clock active work -- this is also what
keeps a healthy long-cascading drive classified **busy** (not crashed) so
a concurrent loop fire correctly no-ops on it (step 1d). A wedged session
that never released will show as **crashed** once `updated_at` exceeds the
`stuck_timeout` (default 1800 seconds = 30 min; override
`TIGERHARNESS_JOURNAL_STUCK_TIMEOUT`) *while still attached*, and a *later*
invocation rescues it via step 2. A session that stops
cleanly should `release` instead (detach) -- a detached task is **idle**
and resumes with no wait, so the 30-minute window only ever applies to a
genuine crash, never to a normal hand-off.

## Parking on an Operator question (never block the turn)

A drive runs unattended -- there is no human watching the thread to
answer a mid-drive question in real time. So when a task needs an
Operator decision you cannot make yourself, **do not stall**: write the
question back to the task, park it out of the queue, notify the Operator,
and keep driving the rest of the queue. The Operator answers later and
reopens the task. Four rules govern this:

1. **Never block the turn -- park, then cascade on.** Waiting in-session
   for an answer wedges the whole drive (and, in interactive Claude Code,
   the human's thread). Parking converts a blocking wait into an
   asynchronous round-trip: `release --state needs_input --question <file>`
   records the question, moves `active/<id>/` -> `needs_input/<id>/`,
   detaches, and frees you to cascade. The task is now out of
   `actionable()` until the Operator answers -- a later task cannot
   accidentally depend on an unanswered one. **Parking is a clean stop,
   not a turn-end:** the moment you have parked + notified, go straight
   back to step 1 (re-sweep) and pick the next actionable task -- a
   resumable `in_progress`, else a `pending` one. Keep draining the queue
   exactly as the step-6 cascade demands; **only** end the turn when that
   re-sweep finds nothing else actionable (then simply stop).

2. **Decide by default; park only what you truly cannot.** Most judgment
   calls are yours to make. If the task's `autonomy` is `judgement`,
   resolve the call yourself, record it as a `Decision:` line in
   `progress.md`, and keep going -- do NOT park. Park only a genuine
   Operator-only decision (scope, priorities, irreversible/risky choices),
   or when `autonomy=ask`. Parking a question you could have answered just
   adds Operator round-trips.

3. **Notify after parking -- mandatory when Slack is configured.** Once
   parked, tell the Operator: a short Slack message (the `slack-notify`
   skill) naming the task id and summarising what you need. This is
   **required** whenever Slack is configured for the team. When it is not,
   the `needs_input/` tray move is itself the visual signal (the Operator
   sees the parked task on `ls journal/needs_input/`). Either way the
   question lives in the task's `questions.md`, so the signal always
   points back to the full ask.

4. **Consume the answer on resume.** The Operator answers inside
   `questions.md` and runs `tigerharness journal answer <task-id>`, which
   moves the task back to `active/` as a **resumable `in_progress`**
   (detached -> idle). The very next sweep surfaces it under step 2a; when
   you resume, read the `**Answer:**` section of `questions.md` (and
   `next_action`) FIRST, act on the Operator's decision, then continue the
   work. The question block's `**Status:**` flips from OPEN to answered by
   the Operator's edit -- treat an OPEN block as still-unanswered and do
   not re-park it.

## What NOT to do

- Do NOT pick a fresh `in_progress` task (another session owns it).
- Do NOT skip the sweep -- it's how the protocol stays correct.
- Do NOT mutate `status.json` mid-task except to refresh `updated_at`
  and (on stop) write the final exit state. Anything more invasive
  belongs in the journal layer's CLI, not in the driver session.
- Do NOT pick multiple tasks in parallel within one invocation.
- Do NOT follow graph edges by hand in a workflow walk. Run `journal
  step-done` and drive whatever step it names next -- the gate is what
  writes each persona's memory. Reading `orchestration.json` to skip
  ahead bypasses the record.
- Do NOT mark a task `done` without the note. `kind=task` needs
  `release --state done --output <note>`; `kind=workflow` needs the
  walk at `__done__` via `step-done`. The gates refuse otherwise -- by
  design, so a persona's memory of the work can't go missing.
- Do NOT hand-write files under `worklog/`. Only the journal gates
  write there; the frontmatter `persona` is stamped from the
  orchestration/compile mapping so attribution can't be wrong.

## Compile sub-protocol (kind=workflow, compile_pending=true)

This is the in-session compile: NO `claude -p`, NO API billing. The
session itself adopts each persona via the persona-switching mechanic
below, and shells out only to the Python validators / promotion CLIs.

Trigger: step 4 on a `kind=workflow` task where
`status.compile_pending=true`.

The compile proceeds in rounds. Each round is one drafter turn
followed by both critics, then a Tier 1 re-validation. Three roles
are involved: the **drafter** writes the steps bundle, and two
critics -- the **akagi-role** critic (execution-mechanics lens) and
the **ayako-role** critic (QA / acceptance lens) -- review it.

The persona NAME for each role comes from the team's
`configs/workflow.yaml` (`compile_personas` key). The defaults are
`drafter=Anzai`, `akagi=Akagi`, `ayako=Ayako`; the `compile-context`
output prints the resolved mapping for the task at hand so you know
which persona to adopt at each turn.

A round ends with both critics emitting `APPROVE` -> land. If either
critic emits `BLOCK` -> mark the task compile-failed (`journal
compile-fail <task-id> --reason '...'`). Otherwise the loop
continues with the critics' `REVISE` feedback merged for the next
drafter turn.

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

   This prints brief + playbook + roster + the role->persona mapping
   + the round-1 drafter prompt in one block. Read it. The
   "Compile personas" section tells you which persona to adopt for
   each role -- the default is `drafter=Anzai`, `akagi=Akagi`,
   `ayako=Ayako`, but the team's `configs/workflow.yaml` may have
   remapped them.

2. **Drafter turn (the drafter-role persona).** Adopt the persona
   named in the bootstrap's "Compile personas" section under
   `drafter:` via the four-line preamble. Emit a `steps-bundle` per
   the drafter contract (one fence, `## step: <id>` headers,
   frontmatter blocks). End with `WORKFLOW: APPROVE`.

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

4. **Akagi-role critique** (execution-mechanics lens). Run:

   ```bash
   tigerharness journal compile-prompts --task <task-id> \\
       --kind akagi --draft <path> --trace <trace-path>
   ```

   The trace from step 3 goes here. Adopt the persona named under
   `akagi:` in the bootstrap mapping (default: Akagi) via the
   preamble, and emit a critique ending with `WORKFLOW: APPROVE` /
   `WORKFLOW: REVISE -- ...` / `WORKFLOW: BLOCK -- ...`. Save the
   full turn to `compile/round-NN-akagi.md`.

5. **Ayako-role critique** (QA / acceptance lens). Same as step 4
   but with `--kind ayako` and the persona under `ayako:` in the
   bootstrap mapping (default: Ayako). Save to
   `compile/round-NN-ayako.md`.

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
   `status.compile_phase=complete` LAST (the visibility gate). It also
   normalises the saved round files (`compile/round-NN-<role>.md`) into
   per-persona worklog entries -- one per round, attributed via the
   `compile_personas` role->persona map -- so each compile persona
   remembers the drafting/critique it did. You do NOT write those by
   hand; `land-compile` does it.

After step 8 the compile is done. Continue at step 4 of the outer
protocol -- the graph-walk sub-protocol described next.

### Heartbeat during compile

Bump `updated_at` after every round (step 6) at minimum. A wedged
compile session is still subject to the stale-rescue rule: a later
invocation finding `compile_pending=true` + a stale heartbeat resumes
from `compile_phase` -- the round files in `compile/round-NN-*.md`
are the resume points.

## Graph-walk sub-protocol (kind=workflow, compile_pending=false)

Walk the DAG in `orchestration.json` one step at a time. Each step is a
persona turn using the same four-line preamble + `WORKFLOW:
APPROVE|REVISE|BLOCK` trailer. **You do NOT follow the edges yourself --
the `journal step-done` gate does**, and writing the step's
persona-attributed worklog note is the ticket to advance (see
"Per-persona memory" above).

For each step:

1. **Adopt the step's persona** (read from the compiled `steps/<id>.md`)
   via the preamble and do the work. Save the turn's substantive output
   to a note file (e.g. `artifacts/<step>-note.md`).
2. **End the turn at the gate:**

   ```bash
   tigerharness journal step-done --task <task-id> --step <step-id> \\
       --verdict <APPROVE|REVISE|BLOCK> --output <note-path>
   ```

   The gate: validates `<step-id>` is the walk's CURRENT step
   (out-of-order is refused), reads persona/role from the compiled step
   file (so the note can't be mis-filed), writes the worklog entry
   stamped with that persona, advances the walk cursor along the
   verdict's edge, and prints the NEXT step id (or `__done__` /
   `__escalate__`). It REFUSES (non-zero, walk **not** advanced) if
   `--output` is missing or empty -- the note is the ticket.
3. **Drive whatever step the gate names next.** Repeat until the gate
   prints `__done__` (walk complete -> `release --state done`) or
   `__escalate__` (-> `release --state blocked`).

Routing reference (the gate applies this for you, from `--verdict`):

- `APPROVE` -> the step's `on_approve` edge.
- `REVISE` -> `on_revise` (typically loops back to the same step with
  feedback, bounded by `max_iters`).
- `BLOCK` -> `on_block` (typically `__escalate__`).

Sentinels: `__done__` ends the walk (`release --state done`);
`__escalate__` ends it (`release --state blocked`). Bump `updated_at`
after every step. Learn the next step **only from `step-done`'s
output**, never by reading the edges yourself -- the gate is what
records each persona's memory, so skipping it loses the record.

If a step is in `parallel_with`, the runtime may dispatch the listed
steps concurrently; the journal driver does NOT need to thread these
itself -- it runs each step's turn (and its `step-done`) in document
order and honours all `parallel_with` edges as a group barrier.

## Step-append sub-protocol (kind=workflow, compile_phase=complete)

Phase 3 lets a graph-walk step discover concrete follow-up work and
**append** it to the running graph without re-scaffolding. Trigger:
while walking the graph, a step's output identifies one or more new
steps that need to exist before the walk can complete (e.g. Anzai's
plan step calls for an extra QA pass that isn't in the compiled
graph yet).

Append is single-round: drafter writes a new bundle -> Tier 1
re-validates the combined graph -> CLI atomically extends
`orchestration.json` + writes the new step files. NO critic loop --
the append is much smaller in scope than the original compile, and a
Tier 1 failure (ref-resolution / roster / cycle bound) is enough to
catch a bad append. The CLI is `journal append-steps`; the
human-facing skill is `workflow-append-steps`.

### Step-by-step

1. **Adopt the drafter-role persona** (same as round 1 of the original
   compile: the name under `drafter:` in the
   `compile-context` bootstrap mapping). The append is a mini-compile,
   so the same drafter discipline applies.

2. **Emit a `steps-bundle`** containing ONLY the new step file(s) --
   the same drafter format as the original compile output. The bundle
   must NOT include any existing step ids; the CLI will reject
   collisions.

3. **Save the bundle** to a file under the task's
   `compile/append-NN.md` (the suffix is operator-chosen; the CLI
   doesn't care). Refresh `updated_at` as you go.

4. **Run:**

   ```bash
   tigerharness journal append-steps --task <task-id> \\
       --new-bundle <path>
   ```

   - Exit 0: graph extended. Continue the graph-walk; subsequent
     steps can route into the new step ids.
   - Exit 1: Tier 1 failure -- a JSON envelope
     `{ok: false, errors: [...], trace: "..."}` is on stdout.
     Treat the errors as drafter feedback, redraft the bundle, retry.
     `orchestration.json` is untouched on failure.
   - Exit 2: operator error (bad task id, wrong phase, unreadable
     bundle). Read stderr.

5. **Resume the graph walk** at whatever step you were on. The newly
   appended steps are reachable via the `on_approve` / `on_revise` /
   `on_block` edges of existing steps that the drafter wired up
   (which is why the original-graph-step that triggered the append
   needs to reference the new step id in one of its edges -- or the
   new steps are unreachable). A future Phase 3+ enhancement may let
   `append-steps` rewire an existing step's edge; today it only
   *adds* nodes.

### Caps + edge cases

- **Append-only invariant**: the CLI enforces `existing_ids ∩
  new_ids == ∅`. Reorders, renames, and rewrites are NOT supported.
- **Reachability**: append is purely additive -- it never rewires an
  existing step's edges. A new step is only reachable from the
  graph-walk if some EXISTING edge already pointed at its id (a
  "promise slot" the original compile planned for) or if a NEW
  step routes into it from a reachable position. Otherwise the
  appended step is documented but unreachable. Surface this to the
  human if the original playbook didn't leave a promise slot.
- **No critic loop**: append is single-round drafter -> Tier 1 ->
  commit. If the drafter's bundle has logical problems Tier 1 won't
  catch, the human is the gate (read it before running the CLI).
- **Phase requirement**: refused unless
  `status.compile_phase=complete`. A compile-in-flight task cannot
  be appended to; a compile-failed task must be retried (or
  re-scaffolded) first.
"""

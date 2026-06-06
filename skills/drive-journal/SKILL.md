---
name: drive-journal
description: Drive the journal -- sweep active/, pick exactly one actionable task, work it continuously until done/blocked/stop, then cascade to the next task. Use when the user asks to "work the journal", "pick up the next task", "drive a journal task", or simply gives you the floor and there are journal entries waiting. The skill is intentionally human-triggered -- there is no CLI form by design.
---

# drive-journal

The **subscription-backend driver**. One invocation drains as much of
the journal's queue as it can in a single interactive session.

See [`docs/subscription-backend.md`](../../docs/subscription-backend.md)
for the design and the on-disk
`<journal>/OPERATING.md` (installed automatically by the scaffolder)
for the vendor-neutral protocol you actually execute.

## When to use this skill

Trigger when the user asks anything like:

- "drive the journal"
- "pick up the next task"
- "work on what's queued"
- "continue the journal"
- gives you the floor with a clear "go" and there are journal entries
  waiting

Do NOT invoke this from a non-interactive context. The driver is
human-triggered by design; a CLI driver would defeat the subscription
model. If the user is asking you to drive in a `claude -p` /
task-runner / cron / API context, surface the boundary.

## Protocol (the short version)

Read `<journal>/OPERATING.md` for the canonical procedure. The short
version, every invocation:

1. **Sweep first.** Run:

   ```bash
   tigerharness journal sweep
   ```

   It archives `done/`, classifies every `in_progress` task as **idle**
   (detached -- resumable now), **busy** (a live session owns it), or
   **crashed** (owner went silent), and prints a one-line summary. Read
   the summary out loud to the user.

2. **Pick exactly ONE actionable task** -- never multiple in parallel.
   Resolve candidates in this **priority order (finish before you
   start)** so a task and *all* its sessions complete before any new
   task begins -- a later task may depend on the one already in flight:

   a. **A *resumable* `in_progress` task -> resume it.** Idle (cleanly
      handed off -- resume **immediately**, no wait) or crashed
      (rescue). Read the tail of `progress.md` and continue from
      `next_action`. Among several, prefer the oldest heartbeat.
   b. **Else, if a *busy* `in_progress` task exists, do NOT start new
      work -- exit cleanly.** A live session owns it (the soft lease);
      let it finish before any `pending` task begins.
   c. **Else, start the oldest `pending` task** -- reached only when
      nothing is `in_progress`.

   Then **claim it atomically**: `tigerharness journal claim <task-id>`
   BEFORE working. Claim sets `session_ref`, flips to `in_progress`,
   bumps `sessions`, and refreshes the heartbeat with a compare-and-set
   re-read. If it exits non-zero ("busy" / "claim lost"), another
   session won -- re-sweep and pick again, or exit.

   - **NEVER** work a *busy* task -- a live session owns it right now.
   - Skip `blocked` tasks; surface them so the user can unblock.
   - If nothing is actionable, **exit the invocation cleanly** -- the
     queue is drained.

3. **Read context** -- for `kind=task`, the task's `task.md` (PRD),
   `status.json`'s `next_action`, and the tail of `progress.md`. For
   `kind=workflow`, the task's `task_brief.md` + `playbook_snapshot.md`
   in place of `task.md`.

4. **Work it continuously**, branching on `status.kind`:

   - `kind=task` -- do the real work directly.
   - `kind=workflow` with `compile_pending=true` -- run the **compile
     sub-protocol** from OPERATING.md ("Compile sub-protocol" section)
     FIRST: adopt Anzai/Akagi/Ayako via the four-line preamble, loop
     drafter+critics with `tigerharness journal compile-context |
     compile-prompts | validate-graph | land-compile`, then walk the
     graph.
   - `kind=workflow` with `compile_pending=false` -- walk the DAG in
     `orchestration.json` per the **graph-walk sub-protocol** in
     OPERATING.md.

   Stop conditions (same for all kinds):
   - The task is fully `done` per its acceptance criteria.
   - A real blocker requires a human or another persona.
   - `sessions == max_sessions` -- move to `blocked` with a
     `next_action` naming the cap as the blocker.
   - The human ends the session.

   Append to `progress.md` and refresh `updated_at` (the heartbeat)
   periodically -- at least every 10 minutes of wall-clock active
   work, ideally on every progress entry. For workflows, refresh
   after every compile round and after every graph-walk step.

5. **On stop**, write a final `progress.md` entry, then
   **`tigerharness journal release <task-id>`** to record the exit and
   **detach**: clean stop with work left -> `release <id> --next-action
   "<note>"` (stays `in_progress`, now **idle** so the next drive
   resumes instantly, no wait); done -> `release <id> --state done`;
   blocked, or `sessions == max_sessions` -> `release <id> --state
   blocked --next-action "<why>"`. `release` clears `session_ref` +
   refreshes `updated_at`; do NOT bump `sessions` (that happened in
   `claim` at pickup).

6. **Cascade / keep going.** After a stop, loop back to step 1
   (re-sweep). A task you moved to `done`/`blocked` yields a different
   next pick; a task you cleanly stopped (not done) is now **idle** and
   you may re-`claim` it and keep going **immediately**, no wait. Keep
   cycling until step 1 reports nothing actionable, the human ends the
   session, a guard rail fires, or your context is exhausted (hand off
   to a fresh drive, which resumes instantly). **Don't manufacture a
   stopping point just because one task finished -- drain the queue
   while the session is hot.**

## What NOT to do

- Don't skip the sweep. It's how the protocol stays correct.
- Don't work a *busy* task (attached + fresh heartbeat). Soft lease --
  a live session owns it. (An *idle* detached task IS yours to resume.)
- Don't pick multiple tasks in parallel within one invocation.
- Don't hand-edit `status.json` for pickup/stop -- use `journal claim`
  (pickup: sets `session_ref`, bumps `sessions`) and `journal release`
  (stop: clears `session_ref`, sets exit state). Mid-task you may
  refresh `updated_at` on a `progress.md` append. For workflows, bump
  `compile_phase` only through the compile CLIs -- never hand-edit.
- Don't invent state values. The allowed states are `pending`,
  `in_progress`, `blocked`, `done`. The allowed `compile_phase`
  values are `pending`, `drafting`, `tier1_pre`, `critiquing`,
  `tier1_post`, `complete`, `failed`.
- Don't invoke `claude -p` from inside a compile turn. Adopt the
  persona by reading `personas/<name>/prompt.md` and prepending the
  four-line preamble. The compile is in-session; the API budget is
  zero.

## If you get confused

The on-disk `<journal>/OPERATING.md` is the contract. Re-read it. If
something in this skill description seems to contradict OPERATING.md,
OPERATING.md wins (it's the file the scaffolder shipped with this
specific journal, this skill is generic guidance).

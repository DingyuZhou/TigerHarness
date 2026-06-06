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

   It archives `done/`, classifies every `in_progress` task as
   *fresh* (heartbeat within `stuck_timeout`) or *stale* (older), and
   prints a one-line summary. Read the summary out loud to the user.

2. **Pick exactly ONE actionable task** -- never multiple in parallel.
   Resolve candidates in this **priority order (finish before you
   start)** so a task and *all* its sessions complete before any new
   task begins -- a later task may depend on the one already in flight:

   a. **A *stale* `in_progress` task -> resume it (rescue).** Finishing
      started work beats starting new work. Read the tail of
      `progress.md` to see where the previous owner left off, then
      continue from `next_action`. Among several stale candidates,
      prefer the oldest heartbeat.
   b. **Else, if a *fresh* `in_progress` task exists, do NOT start new
      work -- exit cleanly.** Another session owns it right now (the
      heartbeat is a soft lease); let it finish before any `pending`
      task begins. A later invocation resumes it once its heartbeat
      goes stale.
   c. **Else, start the oldest `pending` task** -- reached only when
      nothing is `in_progress`.

   - **NEVER** pick a *fresh* `in_progress` task -- the soft lease means
     another session owns it right now.
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

5. **On stop**, write a final `progress.md` entry summarising the
   session, then update `status.json` (bump `sessions` on the
   *pickup*; refresh `updated_at`; rewrite `next_action`; set `state`
   to `done` / `blocked` / leave `in_progress` for a clean stop).

6. **Cascade.** If you moved the task to `done` or `blocked`, loop
   back to step 1 (re-sweep) and pick up the next actionable task.
   Keep cycling until step 1 reports nothing actionable, the human
   ends the session, or a session-level guard rail fires. **Don't
   manufacture a stopping point just because one task finished --
   drain the queue while the session is hot.**

## What NOT to do

- Don't skip the sweep. It's how the protocol stays correct.
- Don't pick a fresh `in_progress` task. Soft lease -- another
  session owns it.
- Don't pick multiple tasks in parallel within one invocation.
- Don't mutate `status.json` mid-task except to refresh `updated_at`
  and (on stop) write the exit state. For workflows, also bump
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

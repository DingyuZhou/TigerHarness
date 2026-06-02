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
# OPERATING.md -- journal driver protocol (Phase 1)

This file is the vendor-neutral contract that teaches any file-reading
agent how to drive the journal. The interactive Claude Code app reads
this via the `drive-journal` skill, but the contract is identical for
any other vendor's agent that learns to read it.

## Where state lives

- Root: this directory.
- `active/<task-id>/` -- in-flight tasks.
  - `task.md` -- the PRD / brief.
  - `status.json` -- the state machine. **Single source of truth.**
  - `progress.md` -- append-only narrative log.
  - `artifacts/` -- whatever the task produces.
- `done/<task-id>/` -- archived tasks, same shape, moved here by the
  sweep when `state=done`.

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

3. **Read context** -- `task.md` (the PRD), `status.json.next_action`,
   and the tail of `progress.md`.

4. **Work the task continuously** -- do the real work, appending to
   `progress.md` and refreshing `updated_at` as a heartbeat as you go
   (at least once every 10 minutes of active work). Keep going until
   **one** of the stop conditions below fires. Do NOT stop after a
   single step just because progress was made; the goal is to take
   the task as far as possible in this session.

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
"""

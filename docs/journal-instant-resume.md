# Journal instant session hand-off

> **Status: implemented (2026-06-06).** `session_ref` is the attach
> signal; the sweep classifies `in_progress` as idle / busy / crashed;
> `journal claim` / `release` are the atomic pickup / hand-off; the
> 30-minute `stuck_timeout` is now crash-detection only. Decisions taken
> at implementation: **Q1** reuse `session_ref` (it was an unused
> `str|null` field); **Q2** compare-and-set on `session_ref` (write +
> re-read; a narrow TOCTOU window remains by design — `flock` is the
> upgrade path if real concurrency emerges). **Q3** resolved: task
> `max_sessions` default lowered to 3 (raise per-task with
> `--max-sessions N`). **Q4** resolved: the driver runs sessions
> back-to-back in one sitting (no self-relaunch — that would defeat the
> subscription model; a `/loop` bridges context boundaries, now without
> the old short-loop no-op). Plus a new `early_exit` toggle (default
> off = run exactly N iterations; on = stop when done). Companion to the
> "finish-before-start" pick rule. Authored by Anzai at the Operator's
> request.

## Problem

The driver classifies an `in_progress` task using one signal:
`updated_at` (the heartbeat). That single field is overloaded to answer
two unrelated questions:

1. *Is a live session driving this task right now?* (collision avoidance)
2. *Has the session that claimed it crashed?* (crash recovery)

A cleanly-paused task — a session finished its chunk and stepped away,
ready for the next session — looks **identical** to an actively-driven
one: both are `in_progress` with a recent `updated_at`. So the driver
plays it safe and **waits `stuck_timeout` (30 min)** before any session
may resume it. That wait is pure overhead on every clean hand-off: a
5-session task advances only ~once per 30 min even though nothing is
being worked *between* sessions.

## Goal

- A clean hand-off between sessions of the **same task** resumes
  **immediately** — no 30-minute wait.
- The 30-minute heartbeat check is kept, but **demoted to crash
  detection only** — it fires only when a session said "I'm driving
  this" and then went silent.
- The driver "keeps going": after a clean stop it cascades straight into
  the next session until done / blocked / out of session budget / out of
  context.
- Composes with the serial "finish-before-start" pick rule.

## Core idea: an explicit "session attached" signal

Split the two questions by recording, explicitly, whether a session is
*currently attached* — independent of the heartbeat.

Candidate mechanism (to verify at implementation):

- **Reuse the existing `session_ref` field** (today always `null`).
  Pick-up writes an opaque attach token (e.g. a run id); a clean stop
  writes `null`. `session_ref != null` ⇒ "a session is attached." This
  reuses the schema and adds observability (what is driving the task).
- **Fallback** if `session_ref` already carries meaning in code: add a
  dedicated boolean `session_active`.

## Classification (the new sweep logic)

| `state` | attached? | heartbeat | classification | pick action |
|---|---|---|---|---|
| `in_progress` | **no** (detached) | — | **idle / resumable** | **resume now (zero wait)** |
| `in_progress` | yes | fresh (`< stuck_timeout`) | **busy** (live driver) | skip — don't collide |
| `in_progress` | yes | stale (`>= stuck_timeout`) | **crashed** | reclaim (rescue), re-attach |
| `pending` | — | — | new | start only if nothing `in_progress` |
| `blocked` | — | — | blocked | skip, surface |

The 30-minute `stuck_timeout` is consulted **only** for the "attached"
rows — i.e. only to tell *busy* from *crashed*. Detached tasks never
look at it. That is the whole fix.

## Lifecycle — who writes what

- **Pick up** (a drive starts a session): set the attach token;
  `state` → `in_progress`; bump `sessions`; start heartbeating
  `updated_at`.
- **During work:** refresh `updated_at` at least every 10 min (liveness).
- **Clean stop, not done** (the hand-off): **clear the attach token**,
  write `next_action`, bump `updated_at`, leave `state=in_progress`.
  ← the one new write that unlocks instant resume.
- **Done / blocked:** set `state`, clear the attach token.
- **Crash** (no clean stop): the attach token stays set and `updated_at`
  ages out → reclaimed after `stuck_timeout`; the rescuer overwrites the
  token with its own.

## "Keep going"

After a clean stop that leaves the task resumable, the driver re-sweeps
and **continues the same task** (now the highest-priority idle
`in_progress` task) rather than ending — looping until done / blocked /
`sessions >= max_sessions` / nothing actionable / the human ends it /
the **genuine context ceiling**. Context pressure is **not** a routine
stop: the cascade-first redesign (2026-06-08) relies on **auto-compaction**
(~50% of the window by default, via `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`)
and re-orients from `progress.md` after a compaction, so a single drive
keeps going through many sessions rather than handing off when the
conversation merely feels long.

The one floor we cannot remove: a single interactive session has a true
hard context ceiling, so the driver must *eventually* end and a **fresh**
drive resume. With this change that hand-off is **instant** (detached →
immediately resumable) instead of a 30-minute stall — a brief
context-boundary blink, nothing more. Something still has to launch the
next drive: a loop (now safe at **any** interval — the old "no short
loop" caveat disappears) or a manual run. The driver does **not**
self-relaunch (see Decisions §4).

## Interaction with finish-before-start

The pick rule on `work/2026-06-05-journal-finish-before-start` already
says "resume `in_progress` before starting `pending`; if a *fresh*
`in_progress` task exists, wait." This proposal **sharpens "fresh"**:
the "wait, don't start new work" case now means specifically *attached +
fresh heartbeat* (a genuinely live driver). A *detached* `in_progress`
task is no longer something to wait on — it is the thing to resume
immediately. The two changes compose; the pick-rule wording gets one
update.

## Edge cases & risks

- **Double-pick race** (two drives grab the same idle task at once): the
  attach write must be an **atomic claim** — compare-and-set on the
  token, or take the journal's existing file lock during pick-up, then
  re-read to confirm you won. This is the one genuinely load-bearing bit
  of the implementation.
- **Crash mid-work:** attached + stale → reclaimed after `stuck_timeout`
  (unchanged crash path). Heartbeat cadence ≤10 min keeps a *legit* long
  operation from being misread as a crash (same risk as today).
- **Crash exactly at hand-off:** if it dies right after clearing the
  token, the task is simply idle and the next drive resumes it.
  Harmless.
- **Migration:** existing `status.json` files predate the attach
  semantics → treat missing / `null` as *detached / idle*. Safe for an
  empty queue (Shohoku's is empty now). A task that happened to be
  genuinely mid-work across the upgrade could be treated as idle and
  double-picked — acceptable given the empty queue, but worth a one-line
  rollout note.

## Scope of the change

- `models.py` — define the attach field semantics + validation +
  (de)serialization.
- `sweep.py` — the idle / busy / crashed classification above.
- `operating_template.py` + both `drive-journal` SKILL.md copies —
  pick-up sets the token, clean-stop clears it, the pick rule uses
  idle/busy/crashed, the cascade "keeps going."
- tests — classification matrix, clean-stop-clears-token, crash-reclaim,
  migration default, atomic-claim race.
- `docs/subscription-backend.md` — schema field-table entry + a note in
  the lifecycle section.

## What this deliberately does NOT change

- The `stuck_timeout` value (30 min stays; now crash-only).
- The serial finish-before-start ordering (kept; composes).

## Decisions (resolved)

1. **Field:** reuse `session_ref` — it was a declared-but-unused
   `str|null`, so no new field was needed.
2. **Atomic claim:** compare-and-set on `session_ref` (`journal claim`
   writes a token + re-reads to confirm). A narrow TOCTOU window remains
   by design; `flock` is the upgrade path if real concurrency emerges.
3. **`max_sessions` sizing:** task default lowered to **3**; raise
   per-task with `--max-sessions N` (e.g. "do this in 10 iterations").
   Workflow default stays 10 (compile budget).
4. **Self-relaunch:** not built — a driver spawning its own
   continuation would route work through the API/CLI and defeat the
   subscription model. Instead the driver runs a task's sessions
   **back-to-back in one sitting**; a `/loop` bridges context
   boundaries (and, now that resume is instant, a short loop no longer
   no-ops).

## Related: the `early_exit` toggle

Added alongside this work: a per-task `early_exit` flag (`journal new
--early-exit`). Default **off** runs the full `max_sessions` budget —
"N iterations means exactly N", mirroring the task-runner's default.
Set it on to let the driver stop as soon as the task is done per its
acceptance criteria.

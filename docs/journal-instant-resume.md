# Journal instant session hand-off (design proposal)

> **Status: design proposal — not implemented.** Spec for decoupling the
> journal's crash-detection lease from its session-resume timing, so
> same-task sessions resume *instantly* on a clean hand-off while the
> 30-minute health check stays as a pure crash detector. Companion to
> the "finish-before-start" pick rule on
> `work/2026-06-05-journal-finish-before-start`. Authored by Anzai at
> the Operator's request, 2026-06-05.

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
`sessions == max_sessions` / the interactive **context window is
exhausted**.

The one floor we cannot remove: a single interactive session has finite
context, so the driver must eventually end and a **fresh** drive must
resume. With this change that hand-off is **instant** (detached →
immediately resumable) instead of a 30-minute stall — a brief
context-boundary blink, nothing more. Something still has to launch the
next drive: a loop (now safe at **any** interval — the old "no short
loop" caveat disappears), a manual run, or optionally the driver
self-relaunching (open question 4).

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

## Open questions for the Operator

1. **Field:** reuse `session_ref` (existing field, adds "who's driving"
   observability) vs a fresh `session_active` boolean? Depends on
   whether `session_ref` already carries meaning in code (to check).
2. **Atomic claim:** file-lock during pick-up vs compare-and-set token.
   Both work; the lock is simpler, the token is more inspectable.
3. **`max_sessions` semantics:** with instant resume + cascade, a big
   task burns sessions fast (each context boundary = a session). Should
   the cap count *context-resumes* only, or should the defaults rise?
   (5 is tight — the docs-audit task finished at exactly 5/5.)
4. **Self-relaunch:** should the driver launch the next drive itself at
   a context boundary, or do we rely on a (now-short-OK) loop / manual
   run? The former is more "always keeps going" but couples the driver
   to a spawn mechanism.

## Next step

On approval (and a decision on Q1–Q2), implement as a branch + PR:
schema + sweep + protocol + tests, with the **atomic claim** as the
careful part. Companion to the finish-before-start change.

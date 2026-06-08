---
name: drive-journal
description: Drive the journal -- sweep active/, pick exactly one actionable task, work it continuously until done/blocked/stop, then cascade to the next task. Use when the user asks to "work the journal", "pick up the next task", "drive a journal task", or simply gives you the floor and there are journal entries waiting. The skill is intentionally human-triggered -- there is no CLI form by design.
---

# drive-journal

The **subscription-backend driver**. **One invocation runs the journal's
whole queue to completion when it can** — claim a task, work it, and
**cascade session-to-session and task-to-task back-to-back, in the SAME
turn, without handing control back**, until nothing is actionable or you
truly run out of context. A frequent `/loop` (every 5–10 min) is fine:
when a healthy session already owns the work, this skill is a **cheap
no-op** (step 1 stops in a few tokens).

`<journal>/OPERATING.md` is the full vendor-neutral contract; this skill
is the **checklist**. You only read OPERATING.md *after* you've claimed
real work (step 3) — a no-op fire never loads it.

## When to use

"drive the journal" · "pick up the next task" · "work what's queued" ·
"continue the journal" · or you're given the floor and entries are
waiting. **Do NOT** drive from a non-interactive context (`claude -p` /
cron / API) — the driver is human-triggered by design; surface that
boundary instead.

## The checklist — run top to bottom, every invocation

1. **Sweep (cheap, always first).** Run `tigerharness journal sweep`. It
   archives `done/` and classifies each `in_progress` task as **idle**
   (detached — resumable now), **busy** (a live session owns it: attached
   *and* heartbeat fresh within the stuck-timeout — default 30 min,
   override `TIGERHARNESS_JOURNAL_STUCK_TIMEOUT`), or **crashed** (attached
   but heartbeat stale).
   **➤ Cheap exit:** if every `in_progress` task is **busy** and nothing
   is idle / crashed / pending, **STOP HERE — do nothing.** A healthy
   session is already driving it; a frequent loop is meant to no-op here.
   Don't read context, don't load OPERATING.md. *(Only a `busy` task
   blocks you — an `idle` one is yours to resume.)*

2. **Pick exactly ONE actionable task** (priority order, finish-before-start
   — let an in-flight task and *all* its sessions finish before any new one,
   since a later task may depend on the one already running):
   (a) a **resumable** `in_progress` task — *idle* → resume **immediately**
   (no wait), *crashed* → rescue; among several prefer the oldest heartbeat;
   (b) **else, if a *busy* `in_progress` task exists → do NOT start new work,
   exit cleanly** (the soft lease: a live session owns it right now; let it
   finish before any `pending` task begins — a later fire resumes it once it
   goes idle or crashed);
   (c) **else** the oldest **pending** task.
   **Claim it atomically:** `tigerharness journal claim <id>` *before*
   working (sets `session_ref`, bumps `sessions`, refreshes the heartbeat,
   compare-and-set). If claim exits non-zero (another session won the
   race), re-sweep and pick again, or stop. Skip `blocked` — surface them.

3. **Load the procedure + context** (reached only when there's real work).
   Read `<journal>/OPERATING.md` for the full procedure, **then** the
   task's context: `task.md` (or `task_brief.md` + `playbook_snapshot.md`
   for `kind=workflow`), `status.json.next_action`, and the tail of
   `progress.md`.

4. **Work the task continuously** per OPERATING.md — branch on
   `status.kind` (for `kind=workflow`, follow the compile / graph-walk
   sub-protocols *in OPERATING.md*). **Heartbeat** every ~10 min of work
   (append to `progress.md` + refresh `updated_at`), so a concurrent loop
   correctly sees you as *busy*. Stop the session only on a real **stop
   condition**: task `done` *and* `early_exit=true`; a real blocker; or the
   human ends it. *(`early_exit=false` runs the full `max_sessions`.)*

5. **On stop, release** (never hand-edit `status.json`):
   `tigerharness journal release <id>` — work remains →
   `--next-action "<note>"` (stays `in_progress`, now **idle**); done →
   `--state done`; blocked or `sessions >= max_sessions` →
   `--state blocked --next-action "<why>"`.

6. **CASCADE — the hard loop. THIS is the driver's whole job.** If you
   released a NOT-done task (it's now idle) **OR** the queue still has any
   actionable work, **go back to step 1 RIGHT NOW and continue in the SAME
   turn.** ⛔ Do **NOT** end your turn, write a "drive summary", or wait
   for the next loop fire between sessions. Run a task's entire
   `max_sessions` budget, and the whole queue, **back-to-back in one
   sitting — never one-session-per-loop-fire.** Only end the invocation
   when step 1 finds nothing actionable, the human ends it, or you hit the
   true context ceiling (step 7). **Never manufacture a stopping point
   just because a session finished or the conversation feels long.**

7. **Let compaction keep you going — "context heavy" is NOT a stop reason.**
   Every session checkpoints to `progress.md` + `next_action`, so a context
   **compaction loses nothing** here — you simply re-orient from those.
   Therefore keep cascading and rely on **auto-compaction** (triggers at
   ~50% of the context window by default — set via
   `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` in your team `.claude/settings.json`)
   to reclaim context; do **not** hand off early to "let the loop bridge."
   Hand off (release idle + end the turn) **only** at the genuine hard
   ceiling — and even then a fresh fire resumes the idle task instantly.
   **After any compaction: re-sweep (step 1) and continue.**

## If you get confused

`<journal>/OPERATING.md` is the contract — re-read it. If this checklist
seems to contradict it, **OPERATING.md wins** (it shipped with this
specific journal; this skill is generic guidance). Common reminders the
full contract spells out: don't skip the sweep; never work a *busy* task;
one task at a time; use `claim`/`release` (never hand-edit state); the
in-session compile is `claude -p`-free (API budget zero).

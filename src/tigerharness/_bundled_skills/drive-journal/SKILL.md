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
boundary instead. That hard rule includes Slack: a **Slack-triggered
(bridge-spawned) session bills API tokens** — it may SCHEDULE journal
tasks (the `journal-new` skill) but must **NEVER drive** them. `journal
claim` enforces this mechanically: it refuses when the bridge's
`TIGERHARNESS_SLACK_THREAD_TS` env marker is present, unless the
deliberate `--allow-api-drive` override is passed. Rails and billing:
`docs/subscription-backend.md`.

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
   (c) **else** the oldest **pending** task;
   (d) **else, if the sweep listed `deferred/` inbox entries,
   materialize the oldest** (`tigerharness journal materialize <id>` --
   subscription rail, same scaffolder as `journal new`), then re-sweep
   and claim the materialized task. A malformed entry exits 1 with a
   JSON envelope and stays in the inbox: surface it, skip it, continue.
   **Materialize is a preparatory step, NOT a turn-end — do NOT stop
   here.** It lands the new task as `pending`; continue in the SAME turn
   (re-sweep → claim → compile → walk it or the next actionable task),
   per the step-6 cascade. Stopping at this seam to wait for the next
   fire is the one-session-per-loop-fire anti-pattern the cascade kills;
   the only turn-ends are nothing-actionable / a real blocker / the human
   / the genuine context ceiling.
   **Claim it atomically:** `tigerharness journal claim <id>` *before*
   working (sets `session_ref`, bumps `sessions`, refreshes the heartbeat,
   compare-and-set). If claim exits non-zero (another session won the
   race), re-sweep and pick again, or stop. Skip `blocked` — surface them.
   **In a Slack-driven drive,** add `--driver <your-persona>` so the work
   lands in the right persona's store (see OPERATING.md "Per-persona
   memory"): `tigerharness journal claim <id> --driver <your-persona>`.
   `--driver` is the persona this session runs as; with it set, the drive's
   Slack thread registers automatically (the bridge passes it via the
   `TIGERHARNESS_SLACK_THREAD_TS` env var), so tiger-memory does **not**
   double-count the fat drive transcript — no copying the thread_ts by
   hand. (`--drive-thread <thread_ts>` overrides it; **omit `--driver`
   entirely outside a drive** — claim/release then behave as the plain
   backend with no memory side-effect.)

3. **Load the procedure + context** (reached only when there's real work).
   Read `<journal>/OPERATING.md` for the full procedure, **then** the
   task's context: `task.md` (or `task_brief.md` + `playbook_snapshot.md`
   for `kind=workflow`), `status.json.next_action`, and the tail of
   `progress.md`.

   Then **adopt the assigned persona WITH its memory**: read
   `personas/<persona>/prompt.md` and that persona's
   `memories/<persona>/briefing/must_remember.md` + `skill_index.md`
   (1-3 KB; indexes only -- open a detail file only when its line
   matches this task). A drive turn is that persona's session start for
   its briefing. History references work you don't recognise? Consult
   `memories/team/events.md`, then persona-attributed worklogs.

4. **Work the task continuously** per OPERATING.md — branch on
   `status.kind` (for `kind=workflow`, follow the compile / graph-walk
   sub-protocols *in OPERATING.md*: in a **graph walk**, end each step at
   the `tigerharness journal step-done --task <id> --step <id> --verdict
   <V> --output <note>` gate — it writes that step persona's worklog entry
   and prints the next step, so do **not** follow the edges by hand; in the
   **compile** sub-protocol, `land-compile` records its own per-round
   worklogs). **Heartbeat** every ~10 min of work (append to
   `progress.md` + refresh `updated_at`), so a concurrent loop correctly
   sees you as *busy*. Stop the session only on a real **stop condition**:
   task `done` *and* `early_exit=true`; a real blocker; or the human ends
   it. *(`early_exit=false` runs the full `max_sessions`.)*

5. **On stop, release** (never hand-edit `status.json`):
   `tigerharness journal release <id>` — work remains →
   `--next-action "<note>"` (stays `in_progress`, now **idle**); done →
   `--state done`; blocked or `sessions >= max_sessions` →
   `--state blocked --next-action "<why>"`.
   **In a drive, pass the same `--driver <your-persona>` you used at
   claim** — it activates the completion gates: a **`kind=task` done** needs
   `--state done --output <work-note.md>` (the assigned persona's work note;
   the gate REFUSES `done` without a non-empty `--output` — **the note is
   the ticket**). Build that note from the **durable record**
   (`progress.md` + `artifacts/`), not in-context memory — it's written
   once at the end, a long cascade may have **compacted** earlier sessions
   away, and tiger-memory ingests only `worklog/` (never `progress.md`), so
   this note is the persona's *only* memory of the task. A **`kind=workflow`
   done** needs `--state done` with **no `--output`** (the per-step
   `step-done` notes are the record, and the walk must have reached
   `__done__`). Outside a drive, omit `--driver` (no gate, no `--output`).
   **Hit an Operator-only question/decision you can't resolve? PARK, don't
   stall:** write the question to a file and `release <id> --driver <p>
   --state needs_input --question <file>` — it appends to the task's
   `questions.md`, moves `active/<id>/ → needs_input/<id>/`, and detaches.
   Then **notify the Operator** (Slack `slack-notify` skill — *mandatory*
   when Slack is configured; otherwise the tray move is the signal) and
   cascade on. Decide-by-default first: only park a genuine Operator call
   (or when `autonomy=ask`); if `autonomy=judgement`, resolve it yourself
   and log a `Decision:` in `progress.md` instead. The Operator answers in
   `questions.md` + runs `journal answer <id>`, which returns the task to
   `active/` as a resumable `in_progress`; on resume, read the
   `**Answer:**` section FIRST, then continue. See OPERATING.md "Parking on
   an Operator question."

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
   **Ending because nothing is actionable AND nothing is busy? Run the
   idle-maintenance tail (below) first, then stop.** (Ending on the
   busy cheap-exit or the context ceiling skips the tail — a job is
   running, or you have no context to spare.)

7. **Checkpoint-and-hand-off near the ceiling — "context heavy" still is
   NOT a panic.** Every session checkpoints to `progress.md` +
   `next_action`, so nothing is lost at a hand-off. There is NO
   configured mid-task compaction (retired 2026-06-11: compacting
   mid-task can cause unexpected results); the CLI's own auto-compact
   fires only near the hard limit. Nearing the ceiling: finish the
   current step, checkpoint, release idle, end the turn — instant-resume
   picks the task back up with fresh context. The only proactive
   compaction is the bridge's idle compaction (between tasks; see
   docs/slack-bridge.md). Do **not** hand off early "to be safe" — hand
   off **only** at the genuine hard ceiling; even then a fresh fire
   resumes the idle task instantly. **If a compaction does happen
   (the CLI's near-limit fallback): re-sweep (step 1) and continue.**
   Compaction and hand-offs are safe for *memory* too — as long as a
   `kind=task` done note is built from the durable record (step 5),
   since that note is the only thing ingested.

## Idle-maintenance tail (queue drained — leave the camp clean)

When the drive ends because the sweep found **nothing actionable and
nothing busy** (empty queue, or everything parked/blocked/done), run the
team's two self-gating maintenance chores before stopping. Both are
cheap no-ops when fresh, so an idle loop fire stays cheap; **skip the
whole tail** when anything is busy (a job is running — the user's
"no jobs running" condition) or when you are stopping at the context
ceiling (step 7 — hand off instead).

1. **Compact heavy idle bridge lanes**:
   `tigerharness slack-bridge compact-idle`. Self-gating: it does
   nothing unless the team opted in (`idle_compact: true`), a lane's
   last-stamped usage crosses the threshold, the lane is quiet, and the
   journal is idle. Its only model call is the single bounded
   `/compact` turn it sends per eligible lane. Run it from the team
   root.
2. **Sweep the team's memory**: invoke the `sweep-memory` skill.
   Self-gating via its staleness floor + watermark + soft lease — a
   fresh team is a few tokens of no-op. Its summarize work runs in
   Task-tool sub-agents, which any agent drive session (including an
   autodrive `claude -p` fire) can spawn; the executor ban only covers
   plain daemons that cannot host sub-agents.

Order matters slightly: compact first (bounded, usually a no-op), then
the memory sweep (it may fan out sub-agents). If the sweep claims work,
finish it per that skill (every claim ends in `sweep-complete` or
`sweep-release`) — do not abandon a claimed sweep just because the
drive is "done".

## If you get confused

`<journal>/OPERATING.md` is the contract — re-read it. If this checklist
seems to contradict it, **OPERATING.md wins** (it shipped with this
specific journal; this skill is generic guidance). Common reminders the
full contract spells out: don't skip the sweep; never work a *busy* task;
one task at a time; use `claim`/`release` (never hand-edit state); the
in-session compile is `claude -p`-free (API budget zero). In a drive, mark
`done` only through the gate (`kind=task` → `release --state done --output
<note>`; `kind=workflow` → walk to `__done__` via `step-done`) and never
hand-write `worklog/` — the gate stamps each entry's persona attribution.

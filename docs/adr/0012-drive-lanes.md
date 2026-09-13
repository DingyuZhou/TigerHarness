# ADR 0012 — Drive lanes: journal work runs on its owner's vendor

- Status: accepted
- Date: 2026-09-13
- Deciders: Operator (direction: "the driver is a coordinator; the persona's
  model setting decides the actual work", 2026-09-13), Shohoku (design +
  execution)

## Context

[ADR 0011](0011-model-vendors-per-persona.md) let each persona declare a
model vendor and model, and stated its first limit plainly: a journal
task ran on the **driver's** vendor, not on its assigned persona's. A
drive is one cascading session that claims tasks and adopts their
personas in-session, so the vendor is fixed the moment the session is
launched. Put Rukawa on ChatGPT, wake Ayako's Claude drive, and Ayako's
Claude brain would do Rukawa's task while reading Rukawa's prompt.

The Operator's framing was the right one: the driver and the memory
sweeper are *coordinators*. Coordination can stay where it is; the
actual work should be launched on the owning persona's setting.

## Decision

### 1. A lane is a resolved policy; a drive runs on exactly one

A **lane** is one vendor + model, exactly what `tigerharness.vendors`
resolves a persona to. Every persona is on one lane; a drive session
runs on the lane its `--driver` persona resolves to. A team whose
personas share one policy has one lane and never sees any of this.
Grouping by *policy* rather than by persona is the choice that keeps
today's single-vendor behaviour byte-identical: one Claude session still
adopts every Claude persona in turn, as it always has.

### 2. Every unit of work has an owner (`journal.lanes.work_owner`)

| Work | Owner |
|---|---|
| `kind=task` | the assigned persona |
| `kind=workflow`, compile not complete | the captain, else the team default. The compile loop adopts its drafter/critic roles in one session; it is coordination and runs as a unit on the captain's lane |
| `kind=workflow`, walk in progress | the persona of the step the walk is *at* (before the first `step-done`, the entrypoint step) |
| `kind=workflow`, walk terminal | the captain (only `release` remains) |
| `deferred/` inbox entry | the entry's persona, else the team default |

### 3. The journal enforces it mechanically

- `journal sweep --driver <p>` adds a **lane view**: each actionable item
  and deferred entry is marked `[mine]` or "not yours" with its owner and
  lane (JSON: `actionable_mine`, `actionable_other_lane`,
  `deferred_mine`, `lanes`).
- `journal claim --driver <p>` **refuses** work whose owner is on another
  lane — exit code **3**, before any mutation — so a misjudged pick
  changes nothing. `--any-lane` is the deliberate override (a hand drive
  when no drive on the other lane exists). No `--driver` means no lane
  identity and no gate, as before.
- `journal step-done --driver <p>` applies the same gate to the step's
  persona *before* the worklog write, so no note is ever recorded as
  work done on the wrong vendor; and when the **next** step belongs to
  another lane it prints a `handoff:` cue (JSON `handoff`) telling the
  drive to release the task now so a drive on that lane picks it up.
  The lane check on the next step is advisory and fail-soft (a malformed
  vendor logs and yields no cue); the gate on the current step is not.

The ordering rules of the queue are untouched: "finish before you start"
still holds across lanes, so a busy task on one lane still holds pending
work on another. Lanes decide *who* takes actionable work, never *when*
work becomes actionable.

### 4. Autodrive is the coordinator: one drive per lane with work

On an actionable cycle the daemon runs a second non-AI probe
(`probe_lanes`) that attributes every actionable task and inbox entry to
its owner and its lane, then fires **one drive per lane** that has work
— as a persona on that lane (the configured `--driver` when it lives
there, else the owner of the lane's first item, else the team default
persona when that is on the lane), on that lane's backend and model, with
the drive prompt carrying the lane rule. Guardrails, each answering a
specific way this could go wrong:

- **At most one drive per lane in flight.** A cycle whose lanes all have
  a drive out pulses `lanes busy` and fires nothing. This is the cap the
  rescue-storm incident (ADR 0010) taught us to want.
- **Early wake, floored.** A drive that completes *cleanly* wakes the loop
  before the interval elapses — its release may have handed a workflow
  step to another lane, and waiting out ten minutes per handoff would
  make a mixed-vendor pair loop crawl. A lane may not be fired again
  within `MIN_INTERVAL_SECONDS` (60 s) of its last fire on a woken cycle,
  so a fast no-op drive cannot turn the cadence into a storm; an errored
  drive never wakes.
- **Fail-soft to the old shape.** No team root, a malformed vendor, or
  any probe error means "no lanes" and the single default drive fires
  exactly as before this ADR. `--backend`, `--model` or `--prompt` on
  `autodrive start` pin every drive and turn lanes off (`status` says
  so). The maintenance fire stays a single default drive.
- **The rescue hold is still global.** Any drive of ours in flight holds
  a rescue, lane or not.

### 5. A hand drive is one brain, and says so

An interactive drive (a human in the agent app) runs on whatever vendor
that app is. With `--driver`, it sees the lane view, takes only its own
lane's work, and hands the rest to autodrive. Without an autodrive (the
team never opted in), the other lane's work waits until someone drives
on that vendor or passes `--any-lane` — the sweep output says which.

### 6. Part 2 — memory sweeps per lane (same coordinator)

The sweep-memory skill's executor rule stands: extraction runs in a
session's own helper sub-agents, never in a shelled-out model process.
So the vendor of a persona's sweep is the vendor of the session that
sweeps it, and the fix is again to launch the right session:

- `tiger-memory sweep-plan --lane-of <persona>` restricts a team run's
  *other* targets to the personas on that lane (`sweep.lane_members`),
  so a Claude session never extracts a ChatGPT persona's transcripts.
  The skill passes it whenever it has a persona identity.
- `tiger-memory sweep-plan --own-only` never widens to a team run: it
  claims `own-only` when the named persona has pending sources, else
  `not_due`.
- Autodrive's idle path asks `probe_sweep_lanes` which lanes hold
  personas with un-swept sessions (the split gate's exact pending check,
  per persona) and, while any does, fires **one sweep session for the
  first such lane** with `maintenance_prompt` naming those personas —
  own-only sweeps, on that lane's vendor — before the ordinary
  maintenance fire runs and arms the auto-stop. A failed sweep fire marks
  its lane for the rest of the daemon run.

The team watermark stays a single team-wide value on purpose: a lane's
personas are swept by exact pending checks (per persona), not by the
floor, so no per-lane watermark is needed and the sweep-state file keeps
one writer at a time under the existing lease.

## Consequences

- A persona's vendor now decides the brain that does its journal work,
  for tasks, for workflow steps, and for inbox entries — the "always that
  vendor" the Operator asked for in ADR 0011, minus the compile loop
  (captain's lane, stated above).
- Mixed-vendor workflows pay one session launch per lane handoff. The
  early wake keeps that to seconds of daemon latency plus the session's
  own start-up; without a daemon it is a human's next drive.
- Persona attribution in memory is unchanged: `step-done` still stamps
  the step's persona, `release --output` the task's persona, and the
  driver's thin trace lands in the driver's store.
- The memory sweep follows the same coordinator pattern (part 2 above):
  a persona's transcripts are extracted by a session on its own lane
  whenever an autodrive daemon or the persona's own session does the
  sweeping; a team without autodrive relies on each persona's own
  sessions for the other lanes.

**Rejected:** letting the driver session itself spawn a child session on
the other vendor. It would work, but the daemon already knows the queue,
the leases, and what it has in flight; a model-spawned child is invisible
to that accounting and to the one-per-lane cap. Coordination stays in
Python, brains stay in sessions.

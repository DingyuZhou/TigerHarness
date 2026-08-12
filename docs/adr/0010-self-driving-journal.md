# ADR 0010 — The self-driving journal (autodrive auto-start + auto-stop)

- Status: accepted
- Date: 2026-08-12
- Deciders: Operator (direction + explicit authorization, 2026-08-12),
  Shohoku (execution)

## Context

Three facts collided.

**1. The journal is human-triggered by design.** `docs/subscription-backend.md`
states the rule and the reason: a CLI/cron/API driver is a programmatic entry
point that bills token-metered API instead of the monthly subscription. The
`drive-journal` skill has no CLI form on purpose, and `journal claim` refuses
when `TIGERHARNESS_SLACK_THREAD_TS` is set (Slack schedules; Slack never
drives).

**2. `autodrive` already exists as the one sanctioned exception** (ADR-less
until now), and it rests on a single load-bearing fact: **`claude -p` bills
the subscription, not the API.** As of 2026-08-12 the Operator re-confirmed
this is still true — the vendor has not changed the payment structure.

**3. The human is the bottleneck.** A persona in Slack that recognises heavy
work can `journal defer` it, but nothing then happens: someone has to
hand-start a drive, or hand-start `autodrive`. Work sits in the queue until a
human notices. Conversely, a hand-started `autodrive` runs forever, firing a
full `claude -p` session every interval even when the queue has been empty for
hours — because the emptiness check happens *inside* the model.

So the queue had a manual start edge and no stop edge at all.

## Decision

Make the journal self-driving between those two edges: **scheduling work
starts the daemon; draining the queue stops it.** Nothing runs when there is
nothing to run.

### 1. Auto-start on schedule (opt-in, off by default)

`journal new`, `journal defer`, `journal materialize`, and `journal answer`
call a new `autodrive.ensure_running()` after they successfully write queue
state. It is a **no-op unless `TIGERHARNESS_AUTODRIVE_AUTOSTART` is truthy**,
and it never fails the scheduling command — a daemon that cannot start logs a
warning and the task is still queued.

Default is **off in the package** and **on in a team's `configs/.env`**. The
harness ships to users whose billing situation we do not know; a team turns it
on deliberately, in a file it owns.

The Slack "schedule, never drive" rule is **unchanged**. A Slack-triggered
session still cannot claim a task. What changes is that its `defer` now wakes
a *separate, detached, Operator-authorized* daemon that does the driving —
the rail boundary holds because the Slack session is still not the driver.

### 2. Auto-stop on a drained queue

The daemon now runs the plain-Python `journal.sweep.sweep()` **itself, before
each fire**, and acts on the verdict:

| Verdict | Meaning | Action |
|---|---|---|
| `actionable` | resumable / crashed / pending tasks, or a `deferred/` entry | fire a drive |
| `busy` | a live session owns the in-flight task | **skip the fire** (free tick) |
| `idle` | nothing actionable, nothing busy | run the maintenance tail once, then stop |

On `idle` the daemon fires **one** drive — the maintenance fire, whose prompt
already ends with the idle tail (`slack-bridge compact-idle` + the
`sweep-memory` skill). When that fire completes, nothing is in flight, and the
next probe is still `idle`, the loop exits cleanly and `cmd_loop` clears the
state file. Any `actionable`/`busy` verdict resets the maintenance latch, so a
task arriving mid-maintenance keeps the daemon alive.

This applies to **every** daemon — hand-started `autodrive start` included.
New work brings it straight back, so there is no reason to hold a process open.

The probe is the reason auto-stop is cheap rather than clever: `sweep()` is
non-AI Python, so an idle tick costs a file walk instead of a model session.
It is the *real* sweep, not a read-only variant — it archives done tasks and
materializes due schedule definitions — because a probe that classified the
queue differently from a drive could stop on a queue the drive considers full.

**The stop is a handover, not just an exit.** Auto-start decides "a daemon is
already up" by reading the state file, so the interval between the daemon's
last probe and the state file being removed is a window in which a scheduler
sees a live pid, stands down, and has its task stranded — silently, until the
next queue write. Since that is precisely the promise this ADR makes ("queue
work and it gets driven"), the exit is committed under the same lock as
§3: the daemon re-probes while holding `.autodrive.lock` and either **vetoes
its own exit** (work arrived — stay up, fire next cycle) or clears the state
file inside the lock, so a scheduler blocked on that lock reads after the
removal and starts a fresh daemon. Exactly one of the two happens. The
corollary is that the daemon must not clear the state file again after the
loop returns: by then the file may belong to its successor.

### 3. One daemon per team, atomically

The existing one-per-team guard was a read-then-write: `cmd_start` checked
`is_running()` and only later wrote the state file. Two personas scheduling in
the same second both passed the check and both spawned. With auto-start firing
on *every* `journal new`/`defer`, that race stops being theoretical.

The check-and-spawn critical section now runs under an exclusive
`fcntl.flock` on `<team>/journal/.autodrive.lock`. It is a separate file from
`.autodrive.json` on purpose: the state file is replaced by tmp-plus-rename on
every update, and `flock` follows the inode — locking a file that gets swapped
under you locks nothing.

The lock *anchor* (`_state_root`) is unchanged and was re-verified as correct:
standing in a team root pins `<team>/journal` regardless of `--journal-dir`,
and from anywhere else `_resolve_journal_root` already lands on the target
team's canonical journal. `ensure_running` passes the journal root the
`journal` command itself resolved, so it inherits that team pinning.

`ensure_running()` treats "already running" as **success**, not an error — the
invariant it wants is "a daemon is up", and one already being up satisfies it.

The spawn also **scrubs the Slack turn that triggered it**. `journal defer` is
the flagship auto-start trigger and it runs inside a bridge turn, so the
child's environment drops `TIGERHARNESS_SLACK_THREAD_TS` and
`TIGERHARNESS_SLACK_CHANNEL`. Inherited, they would pin the daemon *and every
drive it ever spawns* to one thread: the claim gate would refuse each drive as
"a Slack session" (only the prompt's `--allow-api-drive` saving it), an
in-drive `defer` would record that stale thread as its origin, and
drive-transcript suppression would register against an unrelated conversation
and hide it from the memory sweep. Scrubbed at the spawn boundary rather than
handled in the drive prompt, because a boundary holds whether or not a model
reads its instructions. Slack *credentials* are kept — the daemon needs them
to post its own heartbeats.

### 4. Configuration lives in the team's `.env`

`autodrive start` (and therefore auto-start) reads defaults from the team's
`configs/.env`, flag > process env > `.env` > built-in default:

| Key | Default | Meaning |
|---|---|---|
| `TIGERHARNESS_AUTODRIVE_AUTOSTART` | unset (off) | Enable the auto-start hook |
| `TIGERHARNESS_AUTODRIVE_INTERVAL` | `600` | Seconds between fires (floor 60) |
| `TIGERHARNESS_AUTODRIVE_MAX_BUDGET` | unset | Per-drive USD cap |
| `TIGERHARNESS_AUTODRIVE_DRIVER` | team `default_persona` | Attribution persona |
| `TIGERHARNESS_AUTODRIVE_NOTIFY` | `slack` | `slack` or `none` |
| `TIGERHARNESS_AUTODRIVE_NOTIFY_CHANNEL` | operator DM | Slack channel id |

The parser is a dependency-free reader of the same `KEY=value` shape
`slack_bridge` already uses; core `tigerharness` must not grow a hard
`python-dotenv` dependency (it is a `[slack]` extra). It follows dotenv's
value rules — a value that opens with a quote ends at its closing quote and is
taken verbatim from between them; an unquoted value ends at the first
whitespace-preceded `#`. That is load-bearing, not tidiness:
`MAX_BUDGET=5  # cap` parsed as the literal string `"5  # cap"` makes the
numeric read fail, and the budget guard then degrades to *uncapped* — the one
outcome the knob exists to prevent, announced only in a log line nobody reads.
The quote rule has to *close on the quote* rather than test whether the value
happens to end in one, or the very common `CHANNEL="C0AB"  # operator DM` keeps
its quotes and Slack refuses the post, equally quietly. One file must not mean
two different things depending on which parser read it.

### 5. Recurring schedule definitions are deprecated

`journal schedule` definitions materialize **only inside a sweep**, and after
this change a sweep only happens while a daemon is alive. A daemon that
correctly stops on an empty queue would therefore silently never fire the
09:00 daily task — auto-stop and recurring schedules are structurally
incompatible as built.

Per the Operator (2026-08-12): recurring tasks are not a load-bearing feature
today. `journal schedule add` now emits a **deprecation warning** naming this
ADR; the code, the trays, and existing definitions keep working exactly as
before (the daemon still materializes due definitions while it is awake).
Removal is a separate, announced step — the ADR 0003 / ADR 0009 pattern.

If the feature comes back it needs a design that does not depend on a
long-lived process: an OS-level timer that runs the materialization sweep and
then calls `ensure_running()` is the obvious shape.

### 6. The "defer test"

Personas had no trigger telling them when a Slack ask is too big for a chat
turn. That guidance goes in the bridge's first-turn injection and the bundled
skills — **one place each**, not nine persona prompts:

> If the ask needs more than one working session, spans several files, or
> needs another persona's hands, do not start it inline — `journal defer` it
> and say so in one line.

## Consequences

- **The queue drains itself.** Defer in Slack → daemon wakes → work happens →
  memory sweep + idle compaction run → daemon exits. Steady state is *no
  process running*, which is also the cheapest possible steady state.
- **Idle cost drops to ~zero.** Before: one `claude -p` session per interval,
  forever. After: one file walk per interval, and only while work exists.
- **The rails doctrine is narrowed, not widened.** Slack still cannot drive.
  What the Operator authorized is the *trigger* moving from a human hand to a
  queue write — the driver is still the same single, budget-capped,
  killable, Operator-authorized daemon.
- **The load-bearing fact is now written down in three places** (this ADR,
  `docs/autodrive.md`, `docs/subscription-backend.md`): auto-start is safe
  **only while `claude -p` bills the subscription**. If that changes, set
  `TIGERHARNESS_AUTODRIVE_AUTOSTART=0` and the system reverts to
  human-triggered with no code change.
- **Recurring schedules are on notice.** Documented as deprecated, warned at
  the CLI, still functional while a daemon is awake.

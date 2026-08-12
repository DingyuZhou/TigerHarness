---
name: journal-autodrive
description: Start, check, or stop a background process that drives the journal on a fixed interval via the agent SDK (vendor-agnostic; default backend `claude -p`). Use when the user asks to "drive the journal every N minutes", "autodrive the journal", "keep working the queue automatically", "start/stop the auto-driver", "is the autodrive running?", or "why did the autodrive stop?". Wraps `tigerharness autodrive`. This is the Operator-authorized exception to the journal's human-only drive rule -- read the safety note before starting one.
---

# journal-autodrive

A small daemon that **fires "drive the journal" on a fixed cadence**. Every
`interval` seconds it checks the queue and, if there is work, spawns an
agentic backend (default `claude -p`) with a self-contained,
Operator-authorized drive prompt and immediately goes back to waiting -- it
does **not** wait for the drive to finish, so **overlap is allowed** and each
fire is a **fresh, context-clean session**. It is **not** built on Claude
Code's `/loop`; it is a plain detached OS process you can `stop` any time.

**It also stops itself.** Read "It probes before it fires, and it stops
itself" below before you tell anyone something is wrong because "the
autodrive isn't running" -- on a drained queue, that is the correct state.

## Read this before you start one (safety)

The journal is a **human-triggered** subscription backend -- "no
programmatic driver by design". This is the deliberate, **Operator-
authorized** exception, and it is **only safe while `claude -p` bills the
Claude subscription** rather than API tokens. If that billing flips, an
unattended autodrive spends real dollars on **every fire**. Guardrails:

- **`--max-budget <usd>`** caps each drive's reported cost. Strongly
  advised; it is your protection for the day billing changes. **Note the
  multiplier:** because fires overlap, up to N concurrent drives can each
  spend the cap within one interval -- size `--interval` and `--max-budget`
  together.
- **`tigerharness autodrive stop`** is the off-switch. Use it.
- The interval has a **60s floor**; it keeps a typo from piling up dozens of
  concurrent drives.
- Only **one** autodrive daemon runs per **team** (the lock is team-canonical,
  so a second `start` anywhere in the same team is refused -- even with a
  different `--journal-dir`). There is no cap on *concurrent drives* -- the
  journal's busy lease makes a redundant overlapping fire a cheap no-op.

If the user asks for this and you are unsure billing is still on the
subscription, **say so** before starting it.

A team can also opt into **auto-start**: with
`TIGERHARNESS_AUTODRIVE_AUTOSTART` truthy in the team's `configs/.env`,
`journal new` / `defer` / `materialize` / `answer` start the daemon
themselves after writing queue state. Off by default in the package -- a
team turns it on deliberately, in a file it owns. To revert the whole
system to human-triggered, set `TIGERHARNESS_AUTODRIVE_AUTOSTART=0`; no
code change is needed.

## It probes before it fires, and it stops itself

Before each fire the daemon runs the journal's plain-Python sweep **itself**
and acts on the verdict:

- **actionable** (pending / resumable / crashed tasks, or a `deferred/`
  entry) -- fire a drive.
- **busy** (a live session owns the in-flight task) -- **skip the fire.** A
  drive would only sweep, see busy, and exit; the tick is free instead.
- **idle** (nothing actionable, nothing busy) -- fire **one** drive, the
  maintenance one whose tail runs `slack-bridge compact-idle` + the
  `sweep-memory` skill. When that finishes and the queue is still idle, the
  **daemon exits** and clears its state file.

So an idle interval costs a file walk, not a `claude -p` session, and a
drained queue costs nothing at all. This applies to **every** daemon,
including one you started by hand.

**When a user asks "why did the autodrive stop?"** -- check
`<team>/journal/.autodrive.log` and the Slack notifications. A clean
auto-stop posts a final message saying "queue drained after N drive(s);
idle maintenance complete." That is healthy, not a crash. Scheduling new
work brings it straight back (auto-start), or `start` it again by hand.

## Commands

Run from the **team root** (so the team's own journal is the target).

Start (the common request -- "drive every 10 minutes"):

    tigerharness autodrive start --interval 600 --driver <persona> \
        --max-budget 5

Every flag below resolves **flag > process env > the team's `configs/.env`
> built-in default**, so a team that set `TIGERHARNESS_AUTODRIVE_INTERVAL` /
`_MAX_BUDGET` / `_DRIVER` / `_NOTIFY` / `_NOTIFY_CHANNEL` once does not need
them on the command line.

- `--interval` seconds between *fires* (cadence, not spacing -- the loop
  does not wait for a drive to finish; default 600 = 10 min; floor 60).
- `--driver` persona the work is attributed to (defaults to the team's
  `default_persona`). Pass it so worklogs land in the right memory store.
- `--max-budget` per-drive USD cap (advised; see safety note).
- `--backend` agent_sdk backend (default `claude_p`). **Vendor-agnostic
  caveat:** only an *agentic CLI* backend (like `claude_p`) can actually
  invoke skills/tools and drive; a raw chat-completion backend cannot.
- `--model`, `--permission-mode`, `--prompt` override the model, the
  unattended permission mode (default `bypassPermissions`), and the
  built-in drive instruction.
- `--notify {slack,none}` (default `slack`) and `--notify-channel <id>`
  control daemon-level notifications: by default the daemon posts a Slack
  **heartbeat per fire** plus a **threaded status/summary on completion**;
  `none` mutes the push (status is then the pull-based health check). Channel
  resolution: flag > `TIGERHARNESS_AUTODRIVE_NOTIFY_CHANNEL` >
  `SLACK_NOTIFY_CHANNEL` (the team-wide key, so an ops channel is named once)
  > operator DM. The literal `dm` at any layer forces the DM.

Check:

    tigerharness autodrive status

Stop:

    tigerharness autodrive stop

## How to read the request

- "drive the journal every 10 minutes" -> `start --interval 600`.
- "every 5 minutes" -> `start --interval 300`. (Below 60s is refused.)
- "stop the autodrive" / "stop driving automatically" -> `stop`.
- "is it still running?" / "autodrive status" -> `status`.
- "why did it stop?" / "the autodrive died" -> `status`, then the log's
  tail. A drained-queue exit is the healthy case; say so plainly rather
  than restarting reflexively.
- A bare "keep the queue moving automatically" -> `start` at the default
  10-minute interval; mention the `stop` command and the budget cap.

## What it does each fire

Spawns the backend in the team root (in a **fresh session**, no `--resume`)
with a prompt that **explicitly lifts** the "never drive from claude -p /
cron / API" boundary for this sanctioned process, then drives via the
**drive-journal** skill (sweep -> claim one actionable task with `--driver
<persona> --allow-api-drive` -> work it -> cascade). The loop does not wait
for the drive: it records the launch and returns to the cadence, so a slow
drive overlaps the next fire. The journal's busy lease keeps redundant
overlapping fires cheap (they sweep, find the active task busy, and exit). A
drive that hits its budget cap or context ceiling just leaves the task
`in_progress`/idle; a later fire resumes it -- truncation is safe because the
journal's session model is resumable. A drive that errors is logged to
`<team>/journal/.autodrive.log` and the loop keeps firing. By default each
fire also posts a Slack heartbeat, with the drive's status + summary threaded
under it once that fire finishes.

State (and the team-canonical lock) lives in `<team>/journal/.autodrive.json`
with two gauges -- **launched** (`fire_count`, `last_fire_at`, `in_flight`)
and **completed** (`tick_count`, `last_tick_at`, last stop reason / error);
`status` reads it, `stop` clears it, and a drained-queue auto-stop clears it
too. A sibling `<team>/journal/.autodrive.lock` is the `flock` file that
makes the one-per-team check-and-spawn atomic -- it is empty by design;
never read or edit it.

## If you get confused

It is just a wrapper around `tigerharness autodrive {start,status,stop}`.
When in doubt, run `status` to see whether a daemon is live. You no longer
need to babysit the off-switch: the daemon stops on its own once the queue
drains and the idle maintenance is done. Use `stop` when the user wants
work **halted early**, not merely finished.

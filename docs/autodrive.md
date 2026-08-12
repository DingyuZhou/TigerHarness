# autodrive

## At a glance
- **What:** `tigerharness autodrive` — a small detached daemon that fires
  **"drive the journal"** on a fixed cadence via the backend-agnostic
  [agent SDK](agent_sdk.md) (default backend `claude -p`). Fire every N
  seconds and do **not** wait — overlap is allowed; each fire is a fresh,
  context-clean session. **Not** built on Claude Code's `/loop`.
- **When you need it:** the Operator wants the journal queue worked
  automatically — "drive the journal every 10 minutes", "keep the queue
  moving" — without a human re-invoking `drive-journal` each time.
- **Self-driving (opt-in, [ADR 0010](adr/0010-self-driving-journal.md)):**
  with `TIGERHARNESS_AUTODRIVE_AUTOSTART` set in the team's `configs/.env`,
  **scheduling work starts the daemon and draining the queue stops it**. You
  do not start or stop it by hand; steady state is *no process running*.
- **Must-not-miss:** the journal is **human-triggered by design** (see
  [subscription-backend.md](subscription-backend.md)). autodrive is the
  deliberate, **Operator-authorized** exception, and it is **only safe while
  `claude -p` bills the subscription** rather than API tokens. Set
  `--max-budget` and use `autodrive stop`. If billing ever changes, set
  `TIGERHARNESS_AUTODRIVE_AUTOSTART=0` — that reverts the whole system to
  human-triggered with no code change.

## Details

### Why this exists (and why it is the exception)

The journal has "no programmatic driver by design": a CLI/cron/API driver is a
programmatic entry point that would bill token-metered API instead of the
monthly subscription, defeating the whole [subscription
backend](subscription-backend.md) model. autodrive is the one sanctioned
break, and it stands on a single load-bearing fact: **`claude -p` currently
bills the subscription, not the API.** The per-tick prompt says so explicitly,
so the spawned agent (which would otherwise refuse, per the `drive-journal`
skill) knows this drive is authorized.

If Anthropic flips `claude -p` to API billing, an unattended autodrive spends
real dollars on **every tick**. That is the risk the guardrails below exist to
contain — keep them loud, and stop the daemon if you are unsure billing is
still on the subscription.

### Loop shape

**Probe first, then fire on a fixed cadence — do not wait (overlap allowed).**
Every `interval` seconds the loop runs the journal's plain-Python
[`sweep()`](journal.md) **itself** and decides whether the tick is worth a
model session:

| Verdict | Meaning | Action |
|---|---|---|
| `actionable` | resumable / crashed / pending tasks, or a `deferred/` entry | fire a drive |
| `busy` | a live session already owns the in-flight task | **skip the fire** (a free tick) |
| `idle` | nothing actionable, nothing busy | fire the maintenance tail once, then **stop** |

The probe is non-AI Python, so an **idle interval costs a file walk instead of
a whole `claude -p` session**. It is deliberately fail-soft: if the probe
raises, the verdict degrades to `actionable` (the pre-ADR-0010 always-fire
behaviour), because an over-fire costs one drive while a false `idle` would
strand the entire queue.

When it does fire, the loop launches a fresh drive and immediately goes back
to waiting for the next tick; it does **not** block on the drive finishing, so
a slow drive and the next fire can run at the same time. Each fire is a
**brand-new agent session** (no `--resume`), which keeps every drive's context
clean and compact.

This is safe and self-limiting because the journal coordinates through its
claim compare-and-set lease: a redundant overlapping fire sweeps, finds the
active task **busy**, and exits cheaply, while genuinely parallel work (several
actionable tasks) gets picked up across fires. There is deliberately **no
concurrency cap** — the busy-lease no-op makes a pile-up of redundant fires
cheap, and each drive is still bounded by its own `--max-budget`. **Mind the
multiplier, though:** N concurrent drives can spend up to N × the per-drive cap
within one interval, so size `--interval` and `--max-budget` together.

A drive that hits its budget cap or context ceiling returns a non-terminal
`stop_reason`; the journal task simply stays `in_progress`/idle and a later
fire resumes it — truncation is safe because the journal's session model is
resumable. A drive that raises is logged as `last_error` and the loop keeps
firing; one bad drive never takes the daemon down. On `stop`, any still-running
drives are drained so their results are recorded before the daemon exits.

### The idle fire runs the maintenance tail, then the daemon stops

When the probe returns `idle`, the daemon fires **exactly one** drive — the
maintenance fire. Per the drive-journal skill's idle-maintenance tail (also
spelled out in the per-tick prompt), that drive runs the team's two
self-gating chores — `tigerharness slack-bridge compact-idle` (its only model
call is one bounded `/compact` turn per heavy, quiet Slack bridge lane, when
the team opted in — see docs/slack-bridge.md "Idle compaction") and the
`sweep-memory` skill (team memory refresh, gated by its staleness floor +
watermark + lease; its summarize work runs in Task-tool sub-agents, which a
`claude -p` drive session can spawn). Both are cheap no-ops when fresh.

Once that fire **completes**, nothing is in flight, and the next probe is
still `idle`, the loop exits cleanly and the state file is cleared. Nothing is
left to hold a process open for, and new work brings the daemon straight back.

Two details keep the stop honest:

- The auto-stop latch arms on a **failed** maintenance drive too, so a
  crashing tail cannot pin the daemon open forever (the error is recorded and
  notified instead).
- The latch arms only if that maintenance fire is still the newest. If work
  arrived while the tail ran, a real drive was fired after it — so the daemon
  runs a **second** maintenance pass for the memory that work dirtied rather
  than exiting on the stale latch.

This applies to **every** daemon, hand-started `autodrive start` included.

### Notifications (heartbeat on fire, summary on done)

By default the daemon posts a **heartbeat to Slack on every fire**, then
**threads the real drive status + summary** under that heartbeat once the fire
finishes — the steady rhythm of heartbeats is the health signal (its absence
tells you the daemon died), and the threaded reply is the substance. The push
is **model-free** (a plain Slack POST, never a spawned agent), it **never
breaks a drive** (every failure is swallowed + logged), and it is **mutable**
(`--notify none`). When muted, `autodrive status` is the always-available
pull-based health check. Full design: [autodrive-notifications.md](autodrive-notifications.md).

Because the heartbeat rhythm *is* the health signal, an auto-stop would
otherwise be indistinguishable from a crash. So a daemon that exits on a
drained queue posts one final message saying so explicitly — "queue drained
after N drive(s); idle maintenance complete. Scheduling new work starts it
again." Silence after that line is expected; silence without it is not.

### Commands

Run from the **team root**, so the team's own journal is the target.

```
tigerharness autodrive start --interval 600 --driver <persona> --max-budget 5
tigerharness autodrive status
tigerharness autodrive stop
```

`start` flags:

| Flag | Default | Purpose |
|---|---|---|
| `--interval` | `600` (10 min) | Seconds between *fires* (cadence, not spacing — the loop does not wait for a drive to finish). **Floor 60** — keeps a typo from piling up dozens of concurrent drives. |
| `--driver` | team `default_persona` | Persona the work is attributed to (worklogs land in its memory store). |
| `--max-budget` | none | Per-drive USD cap, passed to the backend. **Strongly advised** — your protection for the day billing changes. |
| `--backend` | `claude_p` | agent SDK backend name. **Vendor-agnostic caveat:** only an *agentic CLI* backend can actually invoke skills and drive; a raw chat-completion backend cannot. |
| `--model` | backend default | Model override. |
| `--permission-mode` | `bypassPermissions` | Unattended permission mode (the daemon must never stall on a prompt). |
| `--prompt` | built-in | Override the built-in "drive the journal" instruction. |
| `--journal-dir` | env / cwd-as-team / XDG | Journal root to manage. |
| `--notify` | `slack` | Daemon-level notifications: `slack` posts a heartbeat per fire + a threaded status/summary on completion; `none` mutes. See [autodrive-notifications.md](autodrive-notifications.md). |
| `--notify-channel` | operator DM | Slack channel id for daemon events. Resolution: flag > env `TIGERHARNESS_AUTODRIVE_NOTIFY_CHANNEL` > DM. |

Guardrails baked in: the 60s interval floor, **one autodrive per team**, the
cooperative `stop` off-switch, and the optional `--max-budget` cap.

The one-per-team guard is now **atomic**. It used to be a read-then-write —
check `is_running()`, spawn, write the state file later — so two personas
scheduling in the same second could both pass the check and both spawn. With
auto-start firing on *every* `journal new`/`defer`, that race stops being
theoretical, so the whole check-and-spawn critical section runs under an
exclusive `flock` on `<team>/journal/.autodrive.lock`. That is a **separate
file from `.autodrive.json` on purpose**: the state file is replaced by
tmp-plus-rename on every write, and `flock` follows the inode — locking a file
that gets swapped under you locks nothing.

The lock is still team-canonical: a second `start` anywhere in the same team
is refused, even with a different `--journal-dir` (a personal, non-team
journal keeps its own lock under its own root).

### Configuration from the team's `.env`

Every knob resolves **flag > process env > `<team>/configs/.env` > built-in
default**, so a team sets its cadence and cap once instead of on every
invocation:

| Key | Default | Meaning |
|---|---|---|
| `TIGERHARNESS_AUTODRIVE_AUTOSTART` | unset (off) | Enable the auto-start hook (below) |
| `TIGERHARNESS_AUTODRIVE_INTERVAL` | `600` | Seconds between fires (floor 60) |
| `TIGERHARNESS_AUTODRIVE_MAX_BUDGET` | unset | Per-drive USD cap |
| `TIGERHARNESS_AUTODRIVE_DRIVER` | team `default_persona` | Attribution persona |
| `TIGERHARNESS_AUTODRIVE_NOTIFY` | `slack` | `slack` or `none` |
| `TIGERHARNESS_AUTODRIVE_NOTIFY_CHANNEL` | operator DM | Slack channel id |

The reader is a dependency-free parser of the same `KEY=value` shape
`slack_bridge` already uses — core `tigerharness` must not grow a hard
`python-dotenv` dependency (it is a `[slack]` extra). Unrecognised truthiness
and non-numeric values warn and fall back to the default rather than failing a
scheduling command.

### Auto-start on schedule (opt-in, off by default)

`journal new`, `journal defer`, `journal materialize`, and `journal answer`
call `autodrive.ensure_running()` after they have successfully written queue
state. It is a **no-op unless `TIGERHARNESS_AUTODRIVE_AUTOSTART` is truthy**,
it treats "already running" as **success** (the invariant it wants is "a
daemon is up"), and it **never fails the scheduling command** — a daemon that
cannot start logs a warning and the task is still safely queued.

Default is **off in the package** and **on in a team's `configs/.env`**: the
harness ships to users whose billing situation we do not know, so a team turns
this on deliberately, in a file it owns.

**The Slack rail is unchanged.** A Slack-triggered session still cannot claim
a task (`journal claim` refuses it mechanically). What changes is that its
`defer` now rings a bell for a *separate, detached, Operator-authorized*
daemon — the rail boundary holds because the Slack session is still not the
driver. See [subscription-backend.md](subscription-backend.md).

### How it runs

`start` writes a state file, then spawns the hidden `_loop` subcommand as a
**detached** process (`start_new_session=True`, its own process group). The
child runs **in the team root** (`cwd`) so the persona's `CLAUDE.md`, team
detection, and memory attribution all line up, and `start` pins
`TIGERHARNESS_JOURNAL_DIR` in the child's environment so the spawned drive
resolves exactly the journal autodrive manages (even with a custom
`--journal-dir`). The backend is resolved **by name** via the agent SDK and
given a plain `AgentConfig` — autodrive never passes `cwd` to the backend
constructor (not every backend accepts one); the agentic backend inherits the
child's working directory instead. That name-resolution seam is what keeps
autodrive vendor-agnostic.

`stop` sets a cooperative `stop_requested` flag (a clean between-ticks exit),
then SIGTERMs the whole process group so an in-flight drive dies promptly too,
and clears the state file.

### State and logs

| Path | What |
|---|---|
| `<team>/journal/.autodrive.json` | State **and the team-canonical lock**: pid, interval, backend, driver, max_budget, notify config, started_at, plus two gauges — **launched** (`fire_count`, `last_fire_at`, `in_flight`) and **completed** (`tick_count`, `last_tick_at`, `last_stop_reason` / `last_error`). With overlap the two diverge while drives are in flight. `status` reads it; `stop` clears it. Written atomically; a corrupt file reads as "no daemon" so a fresh `start` can recover. Anchored to the team's canonical journal regardless of `--journal-dir`, so the one-per-team guard holds (a personal, non-team journal keeps the lock under its own root). |
| `<team>/journal/.autodrive.lock` | `flock` target guarding the check-and-spawn critical section, so two simultaneous `start`/auto-start calls cannot both spawn. Deliberately **not** `.autodrive.json`: that file is replaced on every write and `flock` follows the inode. Zero-length; never read. |
| `<team>/journal/.autodrive.log` | Appended stdout/stderr of the detached `_loop` process. |

### Skill

The `journal-autodrive` bundled skill wraps these commands so any team can ask
for it conversationally ("drive the journal every 10 minutes", "stop the
autodrive", "is it still running?"). It carries the same safety note. The
driving itself still routes through the `drive-journal` skill inside each tick.

## Related

- [adr/0010-self-driving-journal.md](adr/0010-self-driving-journal.md) — the
  decision record for auto-start, auto-stop, the atomic lock, and the
  deprecation of recurring `schedule/` definitions.
- [autodrive-notifications.md](autodrive-notifications.md) — the heartbeat +
  threaded-summary notification model, config, and the mute/pull fallback.
- [subscription-backend.md](subscription-backend.md) — the "no programmatic
  driver" rule autodrive deliberately, and narrowly, breaks.
- [agent_sdk.md](agent_sdk.md) — the backend-agnostic runtime each tick spawns.
- [journal.md](journal.md) — the journal autodrive drives.

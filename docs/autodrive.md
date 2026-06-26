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
- **Must-not-miss:** the journal is **human-triggered by design** (see
  [subscription-backend.md](subscription-backend.md)). autodrive is the
  deliberate, **Operator-authorized** exception, and it is **only safe while
  `claude -p` bills the subscription** rather than API tokens. Set
  `--max-budget` and use `autodrive stop`.

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

**Fire on a fixed cadence — do not wait (overlap allowed).** Every `interval`
seconds the loop launches a fresh drive and immediately goes back to waiting
for the next tick; it does **not** block on the drive finishing, so a slow
drive and the next fire can run at the same time. Each fire is a **brand-new
agent session** (no `--resume`), which keeps every drive's context clean and
compact.

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

### Notifications (heartbeat on fire, summary on done)

By default the daemon posts a **heartbeat to Slack on every fire**, then
**threads the real drive status + summary** under that heartbeat once the fire
finishes — the steady rhythm of heartbeats is the health signal (its absence
tells you the daemon died), and the threaded reply is the substance. The push
is **model-free** (a plain Slack POST, never a spawned agent), it **never
breaks a drive** (every failure is swallowed + logged), and it is **mutable**
(`--notify none`). When muted, `autodrive status` is the always-available
pull-based health check. Full design: [autodrive-notifications.md](autodrive-notifications.md).

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

Guardrails baked in: the 60s interval floor, **one autodrive per team** (the
single-instance lock is team-canonical — a second `start` anywhere in the same
team is refused, even with a different `--journal-dir`), the cooperative `stop`
off-switch, and the optional `--max-budget` cap.

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
| `<team>/journal/.autodrive.log` | Appended stdout/stderr of the detached `_loop` process. |

### Skill

The `journal-autodrive` bundled skill wraps these commands so any team can ask
for it conversationally ("drive the journal every 10 minutes", "stop the
autodrive", "is it still running?"). It carries the same safety note. The
driving itself still routes through the `drive-journal` skill inside each tick.

## Related

- [autodrive-notifications.md](autodrive-notifications.md) — the heartbeat +
  threaded-summary notification model, config, and the mute/pull fallback.
- [subscription-backend.md](subscription-backend.md) — the "no programmatic
  driver" rule autodrive deliberately, and narrowly, breaks.
- [agent_sdk.md](agent_sdk.md) — the backend-agnostic runtime each tick spawns.
- [journal.md](journal.md) — the journal autodrive drives.

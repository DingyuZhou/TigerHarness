# TigerHarness

[![PyPI](https://img.shields.io/pypi/v/tigerharness.svg)](https://pypi.org/project/tigerharness/)
[![Python](https://img.shields.io/pypi/pyversions/tigerharness.svg)](https://pypi.org/project/tigerharness/)
[![License](https://img.shields.io/pypi/l/tigerharness.svg)](https://github.com/DingyuZhou/TigerHarness/blob/main/LICENSE)

A generic Claude Code agent harness: iterative task execution, Slack
integration, and persistent memory management.

> **Docs:** [`docs/INDEX.md`](docs/INDEX.md) is the home — it routes you to the
> right doc in one hop (and separates current reference from design history).

## Sub-packages

| Package | Description |
|---|---|
| `tigerharness.agent_sdk` | Backend-agnostic agent SDK. Same caller code, swappable runtimes: the `claude -p` subprocess backend and Anthropic's `claude-agent-sdk` backend, with a shared typed event/retry/error model. |
| `tigerharness.journal` | **File-based subscription backend.** Routes agent work through the interactive Claude Code app so it counts against a monthly subscription instead of token-billed API. Single-persona tasks (`kind=task`) and multi-persona workflows (`kind=workflow`) compiled in-session by a drafter/two-critic loop over mechanical validators, then walked through gates that enforce step order and write persona-stamped worklog notes (the per-persona memory rail). Crash-safe by lease: tasks classify idle/busy/crashed and a fresh session resumes a crashed walk at the same step. Team-pinned scheduling: every task records the journal root it was scheduled into (provenance), scheduling verbs refuse to fall back silently to the per-user journal, and the sweep flags misplaced tasks. A `deferred/` inbox makes Slack-side scheduling cheap: `journal defer` parks the conversation verbatim; `journal materialize` (inside a drive) turns it into a real task. 20 CLI verbs under `journal`. See [docs/journal.md](docs/journal.md), [docs/journal-workflow-mode.md](docs/journal-workflow-mode.md), [docs/journal-instant-resume.md](docs/journal-instant-resume.md). |
| `tigerharness.autodrive` | **Periodic journal driver** (the Operator-authorized exception to the human-only drive rule). A detached daemon fires "drive the journal" on a fixed cadence via the backend-agnostic `agent_sdk` (default `claude -p`) — fire every N seconds without waiting, so drives may overlap; each fire is a fresh, context-clean session; not built on `/loop`. Overlap is safe and self-limiting because the journal's busy lease makes a redundant fire a cheap no-op. Before each fire it runs the journal's plain-Python sweep itself, so a busy tick is skipped and an idle tick costs a file walk instead of a model session. **Self-driving when opted in** (`TIGERHARNESS_AUTODRIVE_AUTOSTART` in the team's `configs/.env`): scheduling work starts the daemon, and a drained queue — after the idle memory sweep + context compaction — stops it, so steady state is no process running. Only safe while `claude -p` bills the subscription, so it ships with a `--max-budget` cap (mind the N×-concurrent multiplier), a 60s interval floor, an atomically-locked single-instance-per-team daemon, and an `autodrive stop` off-switch. See [docs/autodrive.md](docs/autodrive.md) and [ADR 0010](docs/adr/0010-self-driving-journal.md). |
| `tigerharness.slack_bridge` | Slack Socket Mode bridge. Forwards DMs to a `claude -p` backend and posts replies back to the thread. |
| `tigerharness.tiger_memory` | Persistent **bounded** agent memory: three self-pruning stores (skills/must_remember/topics) projected into an index-only session-start briefing, with forgetting via staged compaction ([ADR 0007](docs/adr/0007-topic-store-revamp.md)). Includes the team-wide sweep protocol (`sweep-plan`/`sweep-done`/`sweep-complete`/`sweep-release` under a lease, watermark, and per-wake cap), a lazy team event log ([ADR 0008](docs/adr/0008-team-event-log.md)), and subscription-rail staging (`plan`, `ingest-extraction`/`ingest-staged`) so memory extraction bills to the subscription. See [docs/tiger-memory.md](docs/tiger-memory.md), [docs/tiger-memory-sweep-protocol.md](docs/tiger-memory-sweep-protocol.md). |

## Bundled Claude Code skills

`tigerharness init` installs six Claude Code skills into a new
team's `.claude/skills/`: `drive-journal` (the subscription drive
loop), `journal-new` (task/workflow scaffolding), `journal-autodrive`
(start/stop the periodic journal driver), `slack-notify`
(proactive Slack messages), `workflow-append-steps` (runtime
graph extension), and `tigerharness-basics` (how to operate the team
itself: the CLI, the file layout, recruiting personas, creating
workflows). Refreshes are hash-aware: a skill that still
matches a previously shipped version is updated in place, while a
hand-edited skill is left alone (`src/tigerharness/init.py`).

## Installation

Pick the strategy that matches how you want to use tigerharness:

### Option A — Scope it to one folder (recommended for teams)

Best when you want a self-contained `teams/` directory that owns its
own `tigerharness` install — nothing global, nothing borrowed:

```bash
mkdir -p ~/projects/teams && cd ~/projects/teams
uv init --bare                       # creates a minimal pyproject.toml
uv add 'tigerharness[all]'           # adds dep, creates .venv + uv.lock
uv run tigerharness init             # interactive team scaffolder
```

`tigerharness` only exists inside this folder's `.venv`; from then on
you invoke it with `uv run tigerharness ...`.

### Option B — Install as a global CLI

Best when you want `tigerharness` available everywhere, like `git` or `gh`:

```bash
uv tool install 'tigerharness[all]'  # one-time; puts `tigerharness` on PATH
tigerharness init                    # works from any directory
```

Equivalent with pipx: `pipx install 'tigerharness[all]'`.

### Option C — Traditional pip into an active venv

```bash
python -m venv .venv && source .venv/bin/activate
pip install 'tigerharness[all]'
tigerharness init
```

### Choosing extras

`tigerharness` has **zero hard dependencies** by design. Optional features
are gated behind extras so you don't pay disk/install cost for things
you don't use:

| Extra | Pulls in | Enables / required for |
|---|---|---|
| *(none)* | — | `init` scaffolder, `dismiss` teardown, the `journal` subscription backend |
| `[anthropic]` | `claude-agent-sdk` | The official Claude Agent SDK backend (`anthropic_sdk`) |
| `[slack]` | `slack-bolt`, `aiohttp`, `python-dotenv` | `slack-bridge` (Slack Socket Mode DM bridge) |
| `[memory]` | `pyyaml` | `tiger-memory` (per-persona persistent memory) |
| `[all]` | union of everything above | Everything works out of the box |

Pick the union that matches what you'll use, e.g.
`'tigerharness[slack,memory,anthropic]'` for a Slack-fronted agent with
persistent memory and the official SDK backend.

> **Heads up:** the install commands quote the extras
> (`'tigerharness[all]'`) because zsh treats `[` as a glob character.
> In bash you can drop the quotes, but quoting always works.

## Quick start

### Scaffold a team and its first persona

`tigerharness init` is interactive — it walks you through:

1. Multi-team Slack mode (recommended for new setups) — opt in or out.
2. Persona name + team.
3. Slack `.env` template + memory config (optional toggles).
4. Slack user-ID allowlist for the team's bridge bot (optional).

The memory store is auto-initialized for each new persona and the
Claude Code transcripts path is auto-detected from the team root, so
the user never has to come back and edit placeholders. Every persona
always belongs to a team, and a team is a self-contained directory.

Every team gets two governance folders alongside the runtime config:
`charter/` holds the team's operating manual (mission, scope, allowed
write zones, working conventions, first-read checklist for new
personas) and `knowledge/` holds the team's curated, lazy-loaded
reference base. The generated persona prompt wires both into each
persona's first-read flow, so they're load-bearing from day one
rather than decorative.

```
tigers/
├── .claude/
│   ├── settings.json             # wires TIGERHARNESS_PERSONAS_CONFIG
│   └── skills/                   # bundled skills (drive-journal, journal-new,
│                                 #   journal-autodrive, slack-notify,
│                                 #   workflow-append-steps, tigerharness-basics)
├── configs/
│   ├── personas.yaml              # team registry (auto-updated)
│   └── .env                       # Slack tokens (gitignored)
├── charter/
│   └── README.md                  # team's mission, scope, conventions
│                                  # (single entry point for personas)
├── knowledge/
│   └── README.md                  # curated, lazy-loaded reference base
├── skills/
│   └── README.md                  # drop team-shared skills here
├── personas/
│   ├── chief/
│   │   └── prompt.md              # the persona's system prompt
│   └── scout/
│       └── prompt.md
└── memories/
    ├── chief/
    │   └── tiger-memory.config.yaml   # per-persona memory config
    └── scout/
        └── tiger-memory.config.yaml
```

Persona and team names are space-separated words: letters, digits,
`-`, and `_`, each word starting with a letter or digit, single
spaces between words (so `chief`, `scout-7`, and `Chuan Ying` are all
valid; leading, trailing, or consecutive spaces are not). Paths
printed by init are shell-quoted when a name contains a space.

```bash
# Fully interactive — prompts for persona name, team, slack/memory opts
tigerharness init

# Non-interactive — creates team 'tigers' with persona 'chief'
tigerharness init --persona chief --team tigers --yes

# Names may contain single internal spaces
tigerharness init --persona 'Chuan Ying' --team tigers --yes

# Add a second persona to the same team
tigerharness init --persona scout --team tigers --yes

# Skip Slack or memory generation
tigerharness init --persona chief --team tigers --no-memory --no-slack --yes
```

### Tear down a team or persona

`tigerharness dismiss` is the symmetric counterpart of `init` — it
removes a team (or a single persona inside a team) and all of the
associated state: configs, prompts, per-persona memory data, the
multi-team index entry, and — for the *last* team in a multi-team
setup — the root's per-root `slack-bridge-<root>-<hash>` systemd user
unit too (discovered by content, so older `slack-bridge-multi-*` names
and the legacy global `slack-bridge-multi.service` are found as well).

The command is always interactive and gated behind two confirmations
(a backup acknowledgement and a type-the-name check), so it's hard to
fire by accident:

```bash
# Walks you through: pick team-or-persona → preview → backup confirm
# → type the name → execute. Out of scope: deleting the Slack app on
# api.slack.com (the command prints a manual reminder).
tigerharness dismiss

# Same flow but exits after the preview — useful for checking what
# would happen without touching anything.
tigerharness dismiss --dry-run
```

Refusals are deliberate, not bugs:

- Dismissing the **last** persona of a team is refused — use team-level
  dismissal instead, or add another persona first.
- Dismissing a persona that's the team's `default_persona` in the
  Slack-bridge fragment is refused — pick a new default in the fragment
  first, then re-run.

### Journal (subscription backend)

```bash
# 1. Point tigerharness at your team's persona registry
export TIGERHARNESS_PERSONAS_CONFIG=./tigers/configs/personas.yaml

# 2. Scaffold a task for a persona (the drive-journal skill works it)
tigerharness journal new --kind task --persona chief --prd brief.md

# 3. Check the queue
tigerharness journal list

# 4. Sweep state (archives done, classifies in-progress)
tigerharness journal sweep
```

### Slack bridge

One bridge process serves 1..N teams (lanes) — one Slack app per team,
separate bot identities, separate `threads.json`. A single team is just a
one-lane index.

```bash
# 1. Fill in each team's Slack tokens in <team>/configs/.env (from api.slack.com)
#    SLACK_APP_TOKEN=xapp-...  SLACK_BOT_TOKEN=xoxb-...  SLACK_ALLOWED_USER_IDS=U0123ABC
# 2. Create a lanes index listing your team(s):
printf 'lanes:\n  - tigers\n' > slack-bridge.yaml
# 3. Point the bridge at it and run:
export TIGERHARNESS_BRIDGES_CONFIG=$PWD/slack-bridge.yaml
python -m tigerharness.slack_bridge
```

`tigerharness init` auto-registers each new team's lane.
`TIGERHARNESS_BRIDGES_CONFIG` is **required**: the former single-tenant
fallback was removed on 2026-08-11 (ADR 0009), and the bridge now fails
fast at startup with a migration pointer when it is unset. See
[`docs/slack-bridge.md`](docs/slack-bridge.md#the-bridge-one-process-1n-lanes)
for the full setup and
[migrating off single-tenant](docs/slack-bridge.md#migrating-off-single-tenant).

### Tiger memory

Each persona has its own memory config under
`tigers/memories/<persona>/tiger-memory.config.yaml`. Edit it to point
at your Claude Code project path, then:

`--config` is a top-level option for the `tiger-memory` sub-command, so
it must appear **before** the verb:

```bash
# Save typing — the same path is reused everywhere
CFG=tigers/memories/chief/tiger-memory.config.yaml

# 1. Initialize the memory store (per persona)
tigerharness tiger-memory --config $CFG init

# 2. Bootstrap (one-time backfill from existing transcripts)
tigerharness tiger-memory --config $CFG bootstrap --dry-run
tigerharness tiger-memory --config $CFG bootstrap

# 3. Rebuild (incremental, run after each session)
tigerharness tiger-memory --config $CFG rebuild

# 4. Search memory
tigerharness tiger-memory --config $CFG search "solar energy"

# 5. Pin a must-memorize fact
tigerharness tiger-memory --config $CFG pin "Prefers solar over wind"
```

## Configuration

All paths are resolved from environment variables -- no hardcoded paths.

| Variable | Default | Description |
|---|---|---|
| `TIGERHARNESS_BRIDGES_CONFIG` | (none; **required** for the bridge) | Path to the `slack-bridge.yaml` lanes index (see [docs/slack-bridge.md](docs/slack-bridge.md)) |
| `TIGERHARNESS_SLACK_ENV` | (none) | Explicit `.env` path for the notify CLI's credential lookup |
| `TIGERHARNESS_ATTACHMENT_DIR` | `/tmp/slack-attachments` | Where to stage downloaded files |
| `TIGER_MEMORY_CONFIG` | (none) | Path to tiger-memory YAML config |
| `TIGER_MEMORY_CLI` | (none) | Path to tiger-memory CLI binary |
| `TIGERHARNESS_JOURNAL_STUCK_TIMEOUT` | `1800` (30 min) | Heartbeat age (seconds) past which the journal sweep treats an attached `in_progress` task as **crashed** (below it, **busy**) |

## Examples

See [`examples/`](examples/) for a fully-populated sample team folder
(`examples/tigers/`) and standalone reference configs:
- [`examples/tigers/`](examples/tigers/) -- sample team scaffolded by `tigerharness init`
- [`tiger-memory.config.yaml`](examples/tiger-memory.config.yaml) -- annotated memory config (standalone)
- [`env.example`](examples/env.example) -- Slack bridge env template (standalone)

## Requirements

- Python 3.11+
- For the default `claude_p` backend: the [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) (`claude`) on `PATH`.
- For the `anthropic_sdk` backend: install with `[anthropic]` extra; pulls in [`claude-agent-sdk`](https://pypi.org/project/claude-agent-sdk/).

## Known limitations & roadmap

Gaps we've hit in real use, tracked here so they can be picked up later. None of these block normal use once the listed workaround is applied.

### Bridge boot environment

- **`claude` not found on PATH when the bridge auto-starts at boot.** On distros where the Claude Code CLI is installed outside `/usr/bin` (e.g. NixOS at `/run/current-system/sw/bin/`, npm-global at `~/.npm-global/bin/`, pipx at `~/.local/bin/`), the systemd unit emitted by `tigerharness slack-bridge gen-service` has no `Environment=PATH=...` line. Restarting from an interactive shell works (the rich PATH is inherited from the live user session), but a **cold boot auto-start** sees only the minimal systemd PATH (`/usr/bin:/bin`), so `shutil.which("claude")` in the SDK fails with `backend error: \`claude\` not found on PATH`.
  - **Workaround**: a systemd drop-in at `~/.config/systemd/user/<your-bridge-unit>.service.d/path.conf` (e.g. `slack-bridge-teams-4a8c8b.service.d/`) containing `[Service]\nEnvironment="PATH=/run/current-system/sw/bin:/usr/bin:/bin"` (adapted to the local install location). Drop-ins survive `gen-service` regeneration.
  - **Fix candidates**: (a) `gen-service` emits a sensible default `Environment=PATH=` covering common install locations; (b) add a `CLAUDE_CLI` env var the bridge reads and forwards as `cli=` to `ClaudePBackend()`, mirroring the existing `TIGER_MEMORY_CLI` knob.
- **Same shape applies to the `tiger-memory` binary** used by the bridge's post-thread rebuild trigger. The existing `TIGER_MEMORY_CLI` env var already provides the per-team-`.env` workaround, but a PATH default in `gen-service` would fix both at once.

### Bridge setup ergonomics

- **`gen-service` references `multi-bridge.env` but doesn't create it.** The generated systemd unit has `EnvironmentFile=<teams-root>/multi-bridge.env`, and the bridge won't start without that file existing (systemd skips a missing optional EnvironmentFile silently, but the bridge then fails because `$TIGERHARNESS_BRIDGES_CONFIG` is unset). The user has to manually `echo "TIGERHARNESS_BRIDGES_CONFIG=/path/to/slack-bridge.yaml" > <teams-root>/multi-bridge.env` as a separate step after `gen-service`. **Fix candidate**: have `gen-service` either emit the env file alongside the unit (skipping if it already exists), or at least print a clear "you must now create this file with the following contents" hint.
- **Two `.env` files with non-obvious, very different semantics.** In multi-team mode there are two distinct env files:
  - `multi-bridge.env` (referenced by systemd `EnvironmentFile=`): loaded into the bridge process's `os.environ`. Used **only** for the bootstrap pointer `TIGERHARNESS_BRIDGES_CONFIG`. Adding other env vars here does **not** reach per-lane behavior, because the per-team loader reads from disk into a separate dict.
  - per-team `configs/.env` (referenced by the YAML index's per-lane `env:` key): loaded via `_load_env_file` into a per-lane dict that deliberately does **not** pollute `os.environ`. This is where Slack tokens, `TIGER_MEMORY_CLI`, `SLACK_NOTIFY_CHANNEL`, `TIGERHARNESS_PERSONAS_CONFIG`, and any agent-facing env vars go.

  The Configuration table above doesn't distinguish which env vars belong in which file. Worth a docs pass (or a single config table with a "where it goes" column) so users don't have to read the loader to find out.

### Bridge shutdown

- **The drain budget has only ever been exercised with one turn in flight.** On shutdown the bridge waits up to `_DRAIN_TIMEOUT_S` for its in-flight turns to finish, and that wait is released by a single `self._drained.set()` that fires when the in-flight counter reaches zero. The counter can only exceed 1 with concurrent dispatches, which no test creates — so the "still in flight, do not release yet" side of that check has never run, and it sits behind a `# pragma: no branch` that keeps the 100% coverage gate green over it. The margins in the budget chain (`STOP_DRAIN_S + FINISH_POST_S <= _DRAIN_TIMEOUT_S <= TimeoutStopSec`) exist precisely for the multi-turn case, so the case they were sized for is the one with no coverage. See ["The drain budget is a chain, not a number"](docs/slack-bridge.md) for the full ordering and the reasoning behind each margin.
  - **Fix candidate**: a test with two concurrent dispatches in which the first to finish must *not* set `_drained`. That reaches the untested branch and lets the pragma come off.
  - **Why it isn't already written** — so this doesn't read as neglect. The gap was found and documented by a *documentation* step, and the pragma predates that branch, so writing a new test there would have shipped unreviewed new behaviour from a docs seat. The reason it is recorded rather than closed is a scope boundary, not an oversight or a difficulty. The test is small; whoever picks it up should expect it to be.
  - **The branch is unexercised, not unreachable** — this matters if you are cleaning up pragmas. The standing rule at a 100% branch floor is to *delete* a branch the design made unreachable rather than pragma over it. That rule does not apply here: concurrent dispatches genuinely reach the "still in flight" side, no test creates them. Delete the pragma once the test above exists; do not delete the branch.
  - Not known to misbehave — this is an untested path, not a reported bug.

### Agent Slack notifications

- **Notifications require the bot to be in `SLACK_NOTIFY_CHANNEL`.** Each team has its own Slack app (own bot user). After creating a new team's Slack app and setting `SLACK_NOTIFY_CHANNEL` in `configs/.env`, the bot must be **invited to that channel** (`/invite @BotName` in Slack). Without this, `chat.postMessage` returns `channel_not_found` and notifications are silently skipped. The notify CLI logs the error to stderr but doesn't surface it to the user.
  - **Fix candidate**: `tigerharness init` could print a reminder ("Don't forget to invite your bot to the ops-log channel"), or the notifier could log a more prominent first-time warning.
  - This now also covers **autodrive**, which inherits `SLACK_NOTIFY_CHANNEL` as the last layer of its notify-channel chain. A team that set the key but never invited the bot can decline the inheritance with `TIGERHARNESS_AUTODRIVE_NOTIFY_CHANNEL=dm` and keep its daemon heartbeats in the operator DM. See [autodrive.md](docs/autodrive.md).
- **Agents need `.claude/settings.json` + skills to send proactive DMs.** Agents use the `slack-notify` skill to send per-iteration updates via `python -m tigerharness.slack_bridge.notify`. Without `.claude/settings.json` (which wires `TIGERHARNESS_PERSONAS_CONFIG`) and `.claude/skills/slack-notify/SKILL.md` (which teaches the agent the notify CLI exists), the agent doesn't know how to send Slack messages. As of v0.2.1+, `tigerharness init` scaffolds these automatically for new teams. Existing teams adopt them with `tigerharness init --refresh-skills`, which installs any missing skills, refreshes un-customized ones to the latest, and removes the retired mid-task compact override from `.claude/settings.json` (only when it still holds the old seeded default) — without clobbering hand edits.
- **`--thread` must be passed when notifying from a Slack thread.** When an agent sends a proactive message from a Slack DM thread, it must pass `--thread <slack_thread_ts>` (from the `[bridge-context]` block) so notifications land in the right thread. Without it, notifications go to a new top-level message instead of the conversation thread. The `slack-notify` skill documents this prominently, but it's easy to forget.

### `tigerharness init`

- **Auto-init of the tiger-memory store fails intermittently** with `CalledProcessError` during `tigerharness init`'s last step. Running `tigerharness tiger-memory --config <path> init` by hand afterward always succeeds, suggesting environment propagation in the `sys.executable -m tigerharness ...` subprocess is the moving part. A direct in-process call would remove it.

## License

MIT

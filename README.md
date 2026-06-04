# TigerHarness

[![PyPI](https://img.shields.io/pypi/v/tigerharness.svg)](https://pypi.org/project/tigerharness/)
[![Python](https://img.shields.io/pypi/pyversions/tigerharness.svg)](https://pypi.org/project/tigerharness/)
[![License](https://img.shields.io/pypi/l/tigerharness.svg)](https://github.com/DingyuZhou/TigerHarness/blob/main/LICENSE)

A generic Claude Code agent harness: iterative task execution, Slack
integration, and persistent memory management.

## Sub-packages

| Package | Description |
|---|---|
| `tigerharness.agent_sdk` | Backend-agnostic agent SDK. Same caller code, swappable runtimes: `claude -p` subprocess, Anthropic's `claude-agent-sdk`, OpenAI's `openai-agents` (planned). |
| `tigerharness.journal` | **File-based subscription backend.** Routes agent work through the interactive Claude Code app so it counts against a monthly subscription instead of token-billed API. Supports single-persona tasks (`kind=task`) and multi-persona workflows (`kind=workflow`, compiled in-session from a team playbook). CLI: `journal`. See [docs/journal.md](docs/journal.md). |
| `tigerharness.task_runner` | Fire-and-forget iterative task execution via `claude -p`. Phase 1.5+ users typically prefer the journal subscription backend; this remains for api-budget workloads. |
| `tigerharness.slack_bridge` | Slack Socket Mode bridge. Forwards DMs to a `claude -p` backend and posts replies back to the thread. |
| `tigerharness.tiger_memory` | Persistent agent memory: archive, journal, briefing with lazy rebuild, kind-decay must-memorize, and drill-down. |
| `tigerharness.workflow_runner` | Multi-persona workflow orchestration via `claude -p`. Phase 1.5+ users typically prefer `journal` with `--kind workflow` (same compile pipeline, in-session compile, no API billing). The api-backed runner remains for api-budget workloads. See [docs/workflow-runner.md](docs/workflow-runner.md). |

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
| *(none)* | — | `init` scaffolder, `dismiss` teardown, `task-runner` with the default `claude -p` subprocess backend |
| `[anthropic]` | `claude-agent-sdk` | The official Claude Agent SDK backend (`anthropic_sdk`) |
| `[slack]` | `slack-bolt`, `aiohttp`, `python-dotenv` | `slack-bridge` (Slack Socket Mode DM bridge) |
| `[memory]` | `pyyaml` | `tiger-memory` (per-persona persistent memory; substring search only) |
| `[memory-rag]` | `pyyaml`, `fastembed`, `sqlite-vec` | `tiger-memory` with semantic search via local embeddings (free, ~50 MB model download on first use) |
| `[memory-rag-openai]` | `pyyaml`, `openai`, `sqlite-vec` | `tiger-memory` with semantic search via OpenAI embeddings (API key needed, no model download) |
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

```bash
# Fully interactive — prompts for persona name, team, slack/memory opts
tigerharness init

# Non-interactive — creates team 'tigers' with persona 'chief'
tigerharness init --persona chief --team tigers --yes

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
setup — the `slack-bridge-multi` systemd user unit too.

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

### Task runner

```bash
# 1. Point tigerharness at your team's persona registry
export TIGERHARNESS_PERSONAS_CONFIG=./tigers/configs/personas.yaml

# 2. Assign a task (5 iterations)
python -m tigerharness.task_runner assign \
    --to chief \
    --prompt "Research the latest developments in solar energy" \
    --iters 5

# 3. Check status
python -m tigerharness.task_runner list

# 4. View logs
python -m tigerharness.task_runner logs <task-id>
```

### Slack bridge

```bash
# 1. Fill in your Slack tokens in tigers/configs/.env (from api.slack.com)
#    SLACK_APP_TOKEN=xapp-...
#    SLACK_BOT_TOKEN=xoxb-...
#    ALLOWED_SLACK_USER_IDS=U0123ABC

# 2. Run the bridge
python -m tigerharness.slack_bridge
```

**Multi-team mode** — one bridge process can serve N teams (one Slack
app per team, separate bot identities, separate `threads.json`). Opt
in by creating `slack-bridge.yaml` in your teams dir; subsequent
`tigerharness init` runs auto-register each team. See
[`docs/slack-bridge.md#multi-team-mode`](docs/slack-bridge.md#multi-team-mode)
for the full setup.

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
| `TIGERHARNESS_STATE_DIR` | `~/.local/state/tigerharness-tasks/` | Task runner state directory |
| `TIGERHARNESS_PERSONAS_DIR` | (none) | Directory containing `<name>.md` prompt files |
| `TIGERHARNESS_SLACK_ENV` | `.env` | Path to slack-bridge .env file |
| `TIGERHARNESS_AGENT_CWD` | `.` | Working directory for the Claude agent |
| `TIGERHARNESS_AGENT_PROMPT` | (none) | Path to the agent's system prompt |
| `TIGERHARNESS_SLACK_BRIDGE_DIR` | (none) | Path to slack-bridge service dir (for notify CLI) |
| `TIGERHARNESS_ATTACHMENT_DIR` | `/tmp/slack-attachments` | Where to stage downloaded files |
| `TIGER_MEMORY_CONFIG` | (none) | Path to tiger-memory YAML config |
| `TIGER_MEMORY_CLI` | (none) | Path to tiger-memory CLI binary |

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
  - **Workaround**: a systemd drop-in at `~/.config/systemd/user/slack-bridge-multi.service.d/path.conf` containing `[Service]\nEnvironment="PATH=/run/current-system/sw/bin:/usr/bin:/bin"` (adapted to the local install location). Drop-ins survive `gen-service` regeneration.
  - **Fix candidates**: (a) `gen-service` emits a sensible default `Environment=PATH=` covering common install locations; (b) add a `CLAUDE_CLI` env var the bridge reads and forwards as `cli=` to `ClaudePBackend()`, mirroring the existing `TIGER_MEMORY_CLI` knob.
- **Same shape applies to the `tiger-memory` binary** used by the bridge's post-thread rebuild trigger. The existing `TIGER_MEMORY_CLI` env var already provides the per-team-`.env` workaround, but a PATH default in `gen-service` would fix both at once.

### Bridge setup ergonomics

- **`gen-service` references `multi-bridge.env` but doesn't create it.** The generated systemd unit has `EnvironmentFile=<teams-root>/multi-bridge.env`, and the bridge won't start without that file existing (systemd skips a missing optional EnvironmentFile silently, but the bridge then fails because `$TIGERHARNESS_BRIDGES_CONFIG` is unset). The user has to manually `echo "TIGERHARNESS_BRIDGES_CONFIG=/path/to/slack-bridge.yaml" > <teams-root>/multi-bridge.env` as a separate step after `gen-service`. **Fix candidate**: have `gen-service` either emit the env file alongside the unit (skipping if it already exists), or at least print a clear "you must now create this file with the following contents" hint.
- **Two `.env` files with non-obvious, very different semantics.** In multi-team mode there are two distinct env files:
  - `multi-bridge.env` (referenced by systemd `EnvironmentFile=`): loaded into the bridge process's `os.environ`. Used **only** for the bootstrap pointer `TIGERHARNESS_BRIDGES_CONFIG`. Adding other env vars here does **not** reach per-lane behavior, because the per-team loader reads from disk into a separate dict.
  - per-team `configs/.env` (referenced by the YAML index's per-lane `env:` key): loaded via `_load_env_file` into a per-lane dict that deliberately does **not** pollute `os.environ`. This is where Slack tokens, `TIGER_MEMORY_CLI`, `SLACK_NOTIFY_CHANNEL`, `TIGERHARNESS_PERSONAS_CONFIG`, and any agent-facing env vars go.

  The Configuration table above doesn't distinguish which env vars belong in which file. Worth a docs pass (or a single config table with a "where it goes" column) so users don't have to read the loader to find out.

### Task-runner Slack notifications

- **Notifications require the bot to be in `SLACK_NOTIFY_CHANNEL`.** Each team has its own Slack app (own bot user). After creating a new team's Slack app and setting `SLACK_NOTIFY_CHANNEL` in `configs/.env`, the bot must be **invited to that channel** (`/invite @BotName` in Slack). Without this, `chat.postMessage` returns `channel_not_found` and notifications are silently skipped. The task runner logs the error to stderr but doesn't surface it to the user.
  - **Fix candidate**: `tigerharness init` could print a reminder ("Don't forget to invite your bot to the ops-log channel"), or the notifier could log a more prominent first-time warning.
- **Agents need `.claude/settings.json` + skills to send proactive DMs.** Task-runner agents use the `slack-notify` skill to send per-iteration updates via `python -m tigerharness.slack_bridge.notify`. Without `.claude/settings.json` (which wires `TIGERHARNESS_PERSONAS_CONFIG`) and `.claude/skills/slack-notify/SKILL.md` (which teaches the agent the notify CLI exists), the agent doesn't know how to send Slack messages. As of v0.2.1+, `tigerharness init` scaffolds these automatically for new teams. Existing teams need the files added manually.
- **`--thread` must be passed when assigning from a Slack thread.** When a task is assigned from a Slack DM thread, the agent must pass `--thread <slack_thread_ts>` (from the `[bridge-context]` block) so notifications land in the right thread. Without it, notifications go to a new top-level message instead of the conversation thread. The `assign-task` skill documents this prominently, but it's easy to forget.

### `tigerharness init`

- **Auto-init of the tiger-memory store fails intermittently** with `CalledProcessError` during `tigerharness init`'s last step. Running `tigerharness tiger-memory --config <path> init` by hand afterward always succeeds, suggesting environment propagation in the `sys.executable -m tigerharness ...` subprocess is the moving part. A direct in-process call would remove it.

### Tiger-memory

- **Stores are not relocatable.** `.embeddings.db` holds absolute paths in the `docs` table. Moving a memory store (team rename, persona rename, workspace move) breaks `tiger-memory search` until those rows are rewritten. A `tiger-memory relocate <new-root>` subcommand (or a `--rewrite-paths` flag on `rebuild`) would smooth team/persona migrations.

## License

MIT

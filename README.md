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
| `tigerharness.task_runner` | Fire-and-forget iterative task execution. Drives a persona through N Claude turns with periodic `/compact`. |
| `tigerharness.slack_bridge` | Slack Socket Mode bridge. Forwards DMs to a `claude -p` backend and posts replies back to the thread. |
| `tigerharness.tiger_memory` | Persistent agent memory: archive, journal, briefing with lazy rebuild, kind-decay must-memorize, and drill-down. |

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
| *(none)* | — | `init` scaffolder, `task-runner` with the default `claude -p` subprocess backend |
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

`tigerharness init` is interactive — it walks you through picking (or
creating) a team and adding a persona inside it. Every persona always
belongs to a team, and a team is a self-contained directory:

```
tigers/
├── configs/
│   ├── personas.yaml              # team registry (auto-updated)
│   └── .env                       # Slack tokens (gitignored)
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

## License

MIT

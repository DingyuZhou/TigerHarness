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

```bash
# Core (claude -p backend only; Claude Code CLI must be on PATH)
pip install tigerharness

# With the official claude-agent-sdk backend
pip install tigerharness[anthropic]

# With Slack bridge support
pip install tigerharness[slack]

# With memory support
pip install tigerharness[memory]

# With memory + RAG (local embeddings, free)
pip install tigerharness[memory-rag]

# Everything
pip install tigerharness[all]
```

Or with uv:
```bash
uv add tigerharness
uv add tigerharness --extra all
```

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

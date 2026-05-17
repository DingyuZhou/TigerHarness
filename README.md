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

### Scaffold a new project

```bash
# Create config files in the current directory
tigerharness init --name researcher

# Or with tiger-memory support
tigerharness init --name researcher --memory

# This creates:
#   personas/researcher.md     -- edit your agent's instructions
#   .env                       -- fill in Slack tokens
#   tiger-memory.config.yaml   -- (only with --memory)
```

### Task runner

```bash
# 1. Point tigerharness at your personas directory
export TIGERHARNESS_PERSONAS_DIR=./personas

# 2. Assign a task (5 iterations)
python -m tigerharness.task_runner assign \
    --to researcher \
    --prompt "Research the latest developments in solar energy" \
    --iters 5

# 3. Check status
python -m tigerharness.task_runner list

# 4. View logs
python -m tigerharness.task_runner logs <task-id>
```

### Slack bridge

```bash
# 1. Fill in your Slack tokens in .env (from api.slack.com)
#    SLACK_APP_TOKEN=xapp-...
#    SLACK_BOT_TOKEN=xoxb-...
#    ALLOWED_SLACK_USER_IDS=U0123ABC

# 2. Run the bridge
python -m tigerharness.slack_bridge
```

### Tiger memory

```bash
# 1. Copy the example config
cp examples/tiger-memory.config.yaml tiger-memory.config.yaml
# Edit: set store.root and sources.project_path

# 2. Initialize the memory store
tiger-memory --config tiger-memory.config.yaml init

# 3. Bootstrap (one-time backfill from existing transcripts)
tiger-memory --config tiger-memory.config.yaml bootstrap --dry-run
tiger-memory --config tiger-memory.config.yaml bootstrap

# 4. Rebuild (incremental, run after each session)
tiger-memory --config tiger-memory.config.yaml rebuild

# 5. Search memory
tiger-memory --config tiger-memory.config.yaml search "solar energy"

# 6. Pin a must-memorize fact
tiger-memory --config tiger-memory.config.yaml pin "Prefers solar over wind"
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

See [`examples/`](examples/) for sample configs:
- [`tiger-memory.config.yaml`](examples/tiger-memory.config.yaml) -- annotated memory config
- [`personas/researcher.md`](examples/personas/researcher.md) -- sample persona prompt
- [`env.example`](examples/env.example) -- Slack bridge env template

## Requirements

- Python 3.11+
- For the default `claude_p` backend: the [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) (`claude`) on `PATH`.
- For the `anthropic_sdk` backend: install with `[anthropic]` extra; pulls in [`claude-agent-sdk`](https://pypi.org/project/claude-agent-sdk/).

## License

MIT

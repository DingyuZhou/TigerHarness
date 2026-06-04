# tigerharness Design Document

> **Historical — Phase-0 design record (pre-implementation).** This
> captures the original design at extraction time, when tigerharness had
> three sub-packages (`task_runner`, `slack_bridge`, `tiger_memory`) and
> `agent_sdk` was an external dependency. The shipped package has since
> grown to six sub-packages — adding the in-tree `agent_sdk`, `journal`,
> and `workflow_runner` — and the subscription backend reframed how work
> is billed. Treat this as a frozen record of initial intent, **not** a
> description of the current system. For the current system see the
> per-feature docs in this directory (README,
> [`journal.md`](journal.md),
> [`subscription-backend.md`](subscription-backend.md),
> [`workflow-runner.md`](workflow-runner.md),
> [`agent_sdk.md`](agent_sdk.md), …) and the ADRs under [`adr/`](adr/).
> Specific drifts below (the 3-service tree, the
> `~/.local/state/tigerharness/` state-dir default, the
> `personas/<role>.md` prompt convention, the `--persona-config` flag)
> are left intact as part of the record.

## Overview

tigerharness is a standalone, open-source Python package that provides a
generic Claude Code agent harness: iterative task execution, Slack
integration, and persistent memory management. Extracted from the
tigerleap workspace's `services/` directory.

## Package structure decision: single package with sub-modules

**Chosen: monolithic package with sub-modules** (not a uv workspace /
monorepo with separate packages).

Rationale:
1. All three services share `agent-sdk` as a core dependency.
2. `task-runner` already cross-references `slack-bridge` for notifications.
3. A single `pip install tigerharness` is simpler for adopters than
   coordinating three separate packages.
4. Optional extras (`[slack]`, `[memory]`, `[memory-rag]`) let users
   install only what they need.

```
tigerharness/
  pyproject.toml          # single package, extras for slack/memory/rag
  src/
    tigerharness/
      __init__.py
      task_runner/        # iterative task execution loop
      slack_bridge/       # Slack Socket Mode bridge to Claude
      tiger_memory/       # persistent memory: archive, journal, briefing
  tests/
    task_runner/
    slack_bridge/
    tiger_memory/
  skills/                 # Claude Code skill definitions (SKILL.md files)
    assign-task/
    slack-notify/
    lab-notebook-quarter-roll/
    tiger-memory-drill-down/
    tiger-memory-search/
  docs/
    DESIGN.md             # this file
    task-runner.md
    slack-bridge.md
    tiger-memory.md
```

## Decoupling strategy

### 1. task-runner decoupling

| Original coupling | Resolution |
|---|---|
| `WORKSPACE_ROOT = Path(__file__).parents[3]` in personas.py | Replace with config-driven persona registry. Personas defined in a YAML/Python config the user provides, not hardcoded. |
| `tigerleap-tasks` state dir name | Rename to `tigerharness-tasks` (configurable via env). |
| `_BRIDGE_ENV_PATH` hardcoded parents[3] path | Resolve `.env` from `SLACK_BRIDGE_ENV` env var or colocated `.env` |
| `_SLACK_THREAD_NOTICE` hardcoded path `/home/tigerleap/projects/...` | Template that uses `TIGERHARNESS_SLACK_BRIDGE_DIR` or auto-discovers |
| Persona system prompt files at `personas/<role>.md` | Configurable `TIGERHARNESS_PERSONAS_DIR` or pass prompt text directly |
| Hardcoded 5 persona names (sai, chief, scout, quartermaster, inquisitor) | Ship as example/default config; users register their own personas |

### 2. slack-bridge decoupling

| Original coupling | Resolution |
|---|---|
| `_WORKSPACE_ROOT = Path(__file__).parents[3]` | Remove; all paths from env/config |
| `_SAI_PROMPT_PATH = _WORKSPACE_ROOT / "personas" / "sai.md"` | `TIGERHARNESS_PERSONA_PROMPT` env var or config file |
| `load_dotenv(… / "sandbox" / "slack-bridge" / ".env")` | Standard dotenv from CWD or explicit path |
| `tiger-memory` venv path hardcoded | `TIGER_MEMORY_CLI` env var or PATH lookup |

### 3. tiger-memory decoupling

Already fully config-driven! No tigerleap references in code.
Only change: move it into the `tigerharness` namespace.

## Dependency map

```
tigerharness (core)
  requires: agent-sdk (will be vendored or declared as git dep initially)

tigerharness[slack]
  adds: slack-bolt, aiohttp, python-dotenv

tigerharness[memory]
  adds: pyyaml

tigerharness[memory-rag]
  adds: fastembed, sqlite-vec

tigerharness[memory-rag-openai]
  adds: openai, sqlite-vec
```

## Configuration model

All hardcoded paths are replaced with a layered config:

1. **Environment variables** (highest priority):
   - `TIGERHARNESS_STATE_DIR` — XDG state (default: `~/.local/state/tigerharness/`)
   - `TIGERHARNESS_PERSONAS_DIR` — directory of `<name>.md` prompt files
   - `TIGERHARNESS_SLACK_BRIDGE_DIR` — where slack-bridge runs (for notify CLI)
   - `TIGER_MEMORY_CONFIG` — path to tiger-memory YAML config

2. **Config file** (`tigerharness.yaml` or per-service configs)

3. **Sensible defaults** that work out of the box for a fresh install.

## Persona system redesign

The original has 5 hardcoded personas. The new design:

```python
# User provides a personas.yaml or Python dict:
personas:
  - name: assistant
    aliases: [ai, helper]
    cwd: /path/to/project
    prompt_file: personas/assistant.md  # relative to TIGERHARNESS_PERSONAS_DIR
    permission_mode: bypassPermissions
    disallowed_tools: ["Bash(sudo:*)"]
```

A `register_persona()` API and CLI `--persona-config` flag allow
runtime configuration. The package ships with NO default personas
(unlike tigerleap's 5) — users define their own.

## Skills extraction

Skills are Claude Code `.claude/skills/` SKILL.md files. They contain
no executable code — just documentation for Claude to follow. We:

1. Copy them into `tigerharness/skills/`
2. Replace all hardcoded paths with generic placeholders
3. Add an install script / docs explaining how to symlink them into a
   project's `.claude/skills/`

## Test strategy

- All existing tests migrate with minimal changes (mock paths, remove
  tigerleap assumptions).
- New tests fill gaps in:
  - Persona config loading (new YAML-driven system)
  - Config resolution (env var precedence)
  - Integration between task-runner and slack-bridge notify
- Target: 100% line coverage via `pytest-cov`.

## Migration path for tigerleap

After tigerharness is stable:
1. `tigerleap` adds `tigerharness` as a dependency
2. `tigerleap/services/` becomes thin wrappers (or is deleted)
3. A `tigerharness.yaml` at tigerleap root configures the personas,
   paths, and memory

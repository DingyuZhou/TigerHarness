# agent_sdk

Backend-agnostic Python interface for running LLM agents. It ships three
working backends — `claude_p` (a `claude -p` subprocess), `codex_exec` (a
`codex exec` subprocess, OpenAI's Codex CLI) and `anthropic_sdk` (the
official `claude-agent-sdk`) — plus an `openai_sdk` stub. Callers swap
them by changing one string, and a team picks one per persona through
`configs/personas.yaml` (`vendor: claude | chatgpt`, resolved by
`tigerharness.vendors`; see
[`docs/agent_sdk.md`](../../../docs/agent_sdk.md#choosing-a-backend-per-persona-model-vendors)).

## Why

Every agent framework defines its own `Agent`, `Runner`, `Tool`, `Session`,
and event types. Pin your production code to one of them and you've made
switching providers expensive. This package extracts the common surface
(`AgentConfig`, `AgentBackend`, normalized `Event`s) so backends are
hot-swappable.

See [`docs/agent_sdk_comparison.md`](docs/agent_sdk_comparison.md) for the
design rationale and [`docs/HANDOFF.md`](docs/HANDOFF.md) for the full
workspace map and open work.

## Install

The SDK is a sub-package of the `tigerharness` distribution and has no
required third-party Python dependencies:

```bash
pip install tigerharness              # or: uv add tigerharness
pip install 'tigerharness[anthropic]' # adds the anthropic_sdk backend
```

The subprocess backends need their CLI on `PATH`: `claude_p` needs the
Claude Code CLI (install from anthropic.com/claude-code), `codex_exec`
needs the Codex CLI signed in with `codex login`. Verify:

```bash
claude --version
codex --version
```

Requires Python 3.11+ (uses PEP 604 union types, `match` statements, and
`from __future__ import annotations`).

## Quick start

```python
import asyncio
from tigerharness.agent_sdk import AgentConfig, get_backend


async def main():
    backend = get_backend("claude_p")
    cfg = AgentConfig(name="qa", instructions="Be concise.")
    result = await backend.run(cfg, "What is 2 + 2?")
    print(result.final_output)
    print(f"cost = ${result.cost_usd}")

asyncio.run(main())
```

## Backends

| Name | Status | Notes |
|---|---|---|
| `claude_p` | working | Spawns `claude -p` per call. Always available. Subprocess transport over stream-json. |
| `codex_exec` | working | Spawns `codex exec --json` per call (OpenAI's Codex CLI, ChatGPT-subscription billed; `codex exec resume` for later turns). Always available; needs `codex` on `PATH`. |
| `anthropic_sdk` | working | Wraps Anthropic's official `claude-agent-sdk`. Install with `pip install tigerharness[anthropic]`. Supports built-in tools, sessions, cancellation, and approval callbacks. |
| `openai_sdk` | stub | Future: `pip install openai-agents`. Will support function tools, hosted tools, handoffs, and approval-loop wrappers. |

Switch backends by changing the factory call — caller code stays identical:

```python
# Subprocess transport, always available
backend = get_backend("claude_p")

# Same agent code on OpenAI's Codex CLI
backend = get_backend("codex_exec")

# Same agent code, but now via the official claude-agent-sdk
backend = get_backend("anthropic_sdk")

# (future)
# backend = get_backend("openai_sdk")
```

You can also register your own:

```python
from tigerharness.agent_sdk import register_backend, AgentBackend

class MyBackend:
    # implement run, run_stream, open_session
    ...

register_backend("mine", lambda **kw: MyBackend(**kw))
backend = get_backend("mine")
```

## Concepts

### AgentConfig
Declarative agent description: `name`, `instructions`, `model`, `tools`,
`builtin_tools`, `output_schema`, `max_turns`, plus an `extra: dict` for
backend-specific knobs.

### Tools
- `ToolSpec(name, description, input_schema, handler)` — Python-defined tools.
  *Not supported by `claude_p`.*
- `BuiltinTool(name, config)` — provider-hosted tools (`Bash`, `Read`,
  `WebSearch`, `web_search`, `code_interpreter`, ...).

### Run vs. run_stream
```python
# One-shot:
result = await backend.run(cfg, prompt)

# Streaming — consume to completion:
handle = backend.run_stream(cfg, prompt)
async for event in handle:
    ...
result = handle.result            # populated after the stream completes

# Streaming — break out early with guaranteed cleanup:
async with backend.run_stream(cfg, prompt) as handle:
    async for event in handle:
        if some_condition:
            break                  # __aexit__ kills the subprocess

# Or explicit cancel:
await handle.cancel()              # mid-stream cancel; SIGINT to subprocess
```

If you neither consume the stream to completion nor wrap it in `async with`
nor call `cancel()`, the underlying subprocess will linger until the OS
eventually reaps it (typically on the next stdout write, which gets
SIGPIPE'd). Prefer the `async with` form.

### Events
Discriminated union: `RunStart`, `TextDelta`, `MessageComplete`, `ToolCall`,
`ToolResult`, `Thinking`, `AgentChanged`, `ErrorEvent`, `RunDone`. Use
`match` / `isinstance` to handle each.

### Sessions
```python
session = await backend.open_session()
await backend.run(cfg, "first turn", session=session)
await backend.run(cfg, "follow-up", session=session)
```
Sessions are **not** portable across backends. The id is empty until the
first run populates it.

### Approval (HITL)
```python
async def gate(req: ApprovalRequest) -> ApprovalDecision:
    if req.tool_call.name == "Bash" and "rm " in str(req.tool_call.arguments):
        return ApprovalDecision(allow=False, reason="rm denied")
    return ApprovalDecision(allow=True)

await backend.run(cfg, prompt, approval=gate)
```
*Not supported by `claude_p`.* Use `cfg.extra={"permission_mode": ...}` for
coarse policy instead, or switch to `anthropic_sdk` for inline approval.

## Examples

See `examples/` — recommended reading order:

1. `basic.py` — one-shot Q&A
2. `streaming.py` — consume streaming events with `async with`
3. `multi_turn.py` — session resume across turns
4. `builtin_tools.py` — Claude Code's `Bash` and `Read` tools

Run any of them with:

```bash
python -m tigerharness.agent_sdk.examples.basic
```

## `claude_p` extras

The `claude_p` backend reads a few keys from `cfg.extra`:

| Key | Type | Maps to |
|---|---|---|
| `permission_mode` | str | `--permission-mode` (default / acceptEdits / plan / bypassPermissions / dontAsk) |
| `max_budget_usd` | float | `--max-budget-usd` |
| `add_dirs` | list[str] | one `--add-dir` per entry |
| `disallowed_tools` | list[str] | `--disallowedTools` |
| `settings` | str | `--settings` |
| `cli_args` | dict[str, str \| None] | arbitrary `--<key> <value>` (None values become bare flags) |

`AgentConfig.output_schema` is wired to `--json-schema` (accepts a JSON
Schema dict or a pydantic model — v1 or v2). The CLI populates
`structured_output` in its result event, which `RunResult.final_output`
reflects.

`codex_exec` reads the same keys where Codex has an equivalent
(`permission_mode` maps onto Codex's sandbox/approval flags, `add_dirs`,
`env`, `cli_args`; `output_schema` becomes `--output-schema`) and logs
and ignores the rest, so a config written for `claude_p` runs unchanged.
The full mapping is in [`docs/agent_sdk.md`](../../../docs/agent_sdk.md).

## Testing

The pytest suite lives at `tests/agent_sdk/` in the tigerharness repo
(outside the wheel). From the repo root:

```bash
# One-time dev setup
uv sync --extra all

# Run the SDK tests
uv run pytest tests/agent_sdk -q

# The whole repo with its coverage gate (100% line + branch,
# configured under [tool.coverage] in pyproject.toml)
uv run pytest -q --cov=tigerharness --cov-report=term
```

The tests use fake `claude` / `codex` shell scripts as stand-ins for the
real CLIs, so the suite runs without either installed.

## Limitations of `claude_p`

- No user-defined Python tools (raises `BackendNotImplementedError`)
- No inline approval callbacks (raises `BackendNotImplementedError`)
- `AgentConfig.temperature` is ignored (the CC CLI doesn't expose it as a
  flag — set it via a settings file passed through `extra={"settings": ...}`)
- `BuiltinTool(name, config={...})` rejects per-tool config (the CLI
  configures hosted tools via settings, not flags)
- One subprocess per `run_stream` call; multi-turn happens via `--resume`
- `cancel()` sends SIGINT; `after_turn=True` is a hint, not a hard guarantee

For any of those features, switch to the `anthropic_sdk` backend (the
interface stays the same). `codex_exec` shares the same limitations, and
additionally ignores `max_turns`, `max_budget_usd`, `disallowed_tools`,
`settings` and `builtin_tools` (Codex has no per-run flag for them).

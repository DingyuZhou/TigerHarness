# agent_sdk

Backend-agnostic agent SDK. One declarative config and one backend
interface, so caller code stays identical when you swap the runtime
underneath it (`claude -p` subprocess today; Anthropic's
`claude-agent-sdk` or OpenAI's `openai-agents` by changing a single
string).

`tigerharness.agent_sdk` is the foundation the other sub-packages build
on: `task_runner`, `slack_bridge`, and `tiger_memory` all run agents
through it rather than shelling out to `claude` directly.

## Concept

Two ideas carry the whole design:

- **`AgentConfig`** — a declarative, backend-neutral description of an
  agent (name, instructions, model, tools, limits). Anything a specific
  backend needs that doesn't generalize lives in the free-form
  `extra` dict.
- **`AgentBackend`** — a `Protocol` every concrete runtime implements.
  Caller code depends on the Protocol, never on a concrete backend.

The interface is defined in `agent_sdk/types.py`, which is the single
source of truth — everything you import from `agent_sdk` ultimately
comes from there.

```python
from tigerharness.agent_sdk import get_backend, AgentConfig

backend = get_backend("claude_p")              # swap this string to switch runtimes
cfg = AgentConfig(name="qa", instructions="Be concise.")
result = await backend.run(cfg, "What is 2 + 2?")
print(result.final_output)
```

## Runtime selection

Backends are looked up from a name registry (`agent_sdk/factory.py`):

| Function | Purpose |
|---|---|
| `get_backend(name="claude_p", **kwargs)` | Instantiate a backend by name; backend-specific `kwargs` are forwarded to its constructor. Raises `AgentSDKError` on an unknown name. |
| `register_backend(name, factory)` | Register your own backend factory (overwrites an existing name). |
| `list_backends()` | Names of all registered backends. |

Built-in registrations:

| Name | Backend | Availability |
|---|---|---|
| `claude_p` | `ClaudePBackend` — spawns `claude -p` as a subprocess | Always registered; requires the Claude Code CLI on `PATH`. |
| `anthropic_sdk` | `AnthropicSDKBackend` — wraps Anthropic's official `claude-agent-sdk` | Requires the `[anthropic]` extra (`pip install 'tigerharness[anthropic]'`). |
| `openai_sdk` | `OpenAISDKBackend` — stub | **Planned.** Will wrap `openai-agents` when implemented. |

Each built-in factory imports its backend module lazily, so the SDK
never pays the import cost (or fails to import) for a backend you aren't
using. Register a custom backend the same way:

```python
from tigerharness.agent_sdk import register_backend, get_backend

register_backend("mine", lambda **kw: MyBackend(**kw))
backend = get_backend("mine")
```

## Core API contract

`AgentBackend` (in `types.py`) has three methods:

- **`async run(config, prompt, *, session=None, approval=None) -> RunResult`**
  — run to completion and return the result.
- **`run_stream(config, prompt, *, session=None, approval=None) -> StreamHandle`**
  — stream `Event`s as they happen. The handle is an async iterator that
  also exposes the final `result`. **Cleanup contract:** callers must
  either fully consume the iterator, call `cancel()`, or use it as an
  `async with` context manager — otherwise backend resources
  (subprocesses, sockets) may linger. Reading `.result` before the
  stream is fully consumed raises `StreamNotConsumedError`.
- **`async open_session(*, resume_id=None) -> Session`** — open a
  multi-turn session. A `Session` is **opaque and non-portable**: a
  session opened by one backend can only be passed back to that same
  backend.

`prompt` is either a `str` or a `list[InputMessage]`.

Key data types (all dataclasses in `types.py`):

- **`AgentConfig`** — `name`, `instructions`, `model`, `tools`
  (`list[ToolSpec]`), `builtin_tools` (`list[BuiltinTool]`),
  `output_schema`, `max_turns`, `temperature`, and the backend-specific
  `extra` dict.
- **`RunResult`** — `final_output`, `transcript`
  (`list[NormalizedMessage]`), `stop_reason`, `usage`, `cost_usd`, and
  `raw` (a backend-native escape hatch).
- **`StopReason`** — one of `end_turn`, `max_turns`, `max_budget`,
  `tool_denied`, `interrupted`, `refusal`, `error`.
- **`Event`** (streamed) — a union of `RunStart`, `TextDelta`,
  `MessageComplete`, `ToolCall`, `ToolResult`, `Thinking`,
  `AgentChanged`, `ErrorEvent`, `RunDone`. A terminal error arrives as
  `RunDone(stop_reason="error")`; `ErrorEvent` is non-terminal.
- **Tools** — `ToolSpec` (user-defined: `name`, `description`,
  `input_schema`, async `handler`, `needs_approval`) and `BuiltinTool`
  (provider-hosted, e.g. `BuiltinTool("Bash")`, mapped to each backend's
  native object).
- **Approval / human-in-the-loop** — pass an `ApprovalCallback`
  (`ApprovalRequest -> ApprovalDecision`) to `run`/`run_stream` to gate
  tool calls; `ApprovalDecision` can `allow`, supply a `reason`, or
  override the tool's `updated_input`.

## Per-backend configuration

Backend-specific knobs go in `AgentConfig.extra` (and a few in the
backend constructor). For `claude_p` (`agent_sdk/backends/claude_p.py`):

- Constructor: `get_backend("claude_p", cli="claude", env=None)` — `cli`
  defaults to `"claude"` and is resolved via `shutil.which`; the backend
  raises `CLIError` if it isn't found on `PATH`.
- `cfg.extra` keys: `permission_mode`
  (`acceptEdits` / `plan` / `bypassPermissions` / `dontAsk`), `add_dirs`,
  `disallowed_tools`, `settings` (path to a `--settings` file),
  `max_budget_usd`, and `cli_args` (a free-form `{flag: value}` escape
  hatch passed straight through to the CLI).

Because `claude_p` shells out, the Claude Code CLI must be on `PATH` —
see the README's cold-boot `PATH` note.

## Error and retry model

Exception hierarchy (`agent_sdk/errors.py`):

| Exception | Raised when |
|---|---|
| `AgentSDKError` | Base class for every SDK error. |
| `BackendNotImplementedError` | A backend doesn't support a requested feature. |
| `StreamNotConsumedError` | `.result` is read before the stream is fully iterated. |
| `ToolApprovalDenied` | A tool call is denied and the backend signals a terminal denial. |
| `CLIError` | The underlying CLI subprocess failed (carries `returncode` and `stderr`). |

The SDK ships a retry helper, `run_with_retry` (`agent_sdk/retry.py`):

```python
from tigerharness.agent_sdk import get_backend, run_with_retry

backend = get_backend("claude_p")
result = await run_with_retry(backend, cfg, prompt, session=session,
                              max_attempts=3, base_delay_s=1.0, label="job-42")
```

- Up to `max_attempts` tries (default 3) with exponential backoff
  (`base_delay_s * 2**(n-1)`: 1 s before attempt 2, 2 s before attempt 3
  — ≤ ~3 s of pure wait on the default schedule).
- **Retries every exception** — there's no reliable transient-vs-permanent
  taxonomy for `claude -p`, and re-running the same prompt is never
  destructive. The last exception is re-raised when all attempts fail.
- `asyncio.CancelledError` propagates immediately (never retried), so a
  task-runner cancel isn't swallowed by a backoff sleep.
- Each retry boundary is logged so an operator can see how many attempts
  a call took and why it retried.

## See also

- `src/tigerharness/agent_sdk/README.md` and
  `agent_sdk/docs/agent_sdk_comparison.md` — design rationale and the
  cross-backend feature comparison.
- [`task-runner.md`](task-runner.md), [`slack-bridge.md`](slack-bridge.md),
  [`tiger-memory.md`](tiger-memory.md) — consumers of this SDK.

# Handoff — `agent_sdk`

This document is the entry point for any future agent (or human) picking up
this project. It assumes you've read nothing else.

## TL;DR

We built a backend-agnostic Python agent SDK in `src/tigerharness/agent_sdk/`.
The user wants to write production code against a stable interface and
swap the underlying runtime — a `claude -p` or `codex exec` subprocess, or
the official `claude-agent-sdk` — for another without rewriting caller
code (OpenAI's `openai-agents` is the remaining stub).

- **Working backends**:
  - `claude_p` — subprocess to `claude -p` (always available).
  - `codex_exec` — subprocess to `codex exec --json` (OpenAI's Codex CLI;
    always available, needs `codex` on `PATH`). The `chatgpt` vendor of
    `tigerharness.vendors`; same shape as `claude_p`.
  - `anthropic_sdk` — wraps Anthropic's official `claude-agent-sdk`
    Python package (install via `pip install tigerharness[anthropic]`).
- **Stub backend**: `openai_sdk` — raises `BackendNotImplementedError`
  on construction. Wiring sketch is in `agent_sdk_comparison.md` and
  the module's docstring.
- **Tests**: `tests/agent_sdk/` (11 modules, incl. `test_codex_exec.py`)
  inside the tigerharness suite (~3.9k tests). Coverage is enforced at
  **100% line + branch** at the tigerharness package level
  (`fail_under = 100` in `pyproject.toml`).
- **Design rationale**: see `agent_sdk_comparison.md` (this directory).
  It documents the v2 interface, the OpenAI ↔ Anthropic mapping, what
  intentionally doesn't generalize, and why.

## Dependencies and setup

| What | Required for | Install |
|---|---|---|
| Python 3.11+ | everything | system / pyenv |
| `claude` CLI on `PATH` | running `claude_p` against real Claude | install Claude Code (see anthropic.com/claude-code), then run `claude` once to log in |
| `codex` CLI on `PATH` | running `codex_exec` against real Codex | install the Codex CLI, then `codex login` |
| `pytest`, `pytest-asyncio`, `pytest-cov`, `coverage` | running tests | `uv sync --extra all` (extras + the default `dev` group) |
| `pydantic` (v1 or v2) | one test exercises pydantic-as-output_schema | optional, `pip install pydantic` |
| `mypy` | optional type check (not in the dev group) | `pip install mypy` |

The package source itself has **no third-party Python dependencies** — only
stdlib (`asyncio`, `json`, `subprocess` via asyncio, `signal`, `shutil`,
`uuid`, `dataclasses`, `typing`).

Optional, only needed for the corresponding backend:
- `claude-agent-sdk` for `anthropic_sdk` — installed via
  `pip install tigerharness[anthropic]`. The backend lazy-imports it
  inside `__init__` so the SDK still imports cleanly without it.
- `openai-agents` for `openai_sdk` (when that stub is implemented) —
  follow the same lazy-import pattern.

## Upstream SDKs we cross-referenced

The v2 interface was extracted by reading these directly. Re-check them
before implementing the stub backends — the public surfaces drift.

- **Claude Agent SDK (Python)**:
  https://github.com/anthropics/claude-agent-sdk-python — particular files:
  `src/claude_agent_sdk/__init__.py`, `client.py`, `query.py`, `types.py`,
  `_internal/transport/subprocess_cli.py`. Inspected ~Oct/Nov 2025.
- **OpenAI Agents SDK (Python)**:
  https://github.com/openai/openai-agents-python — particular files:
  `docs/agents.md`, `docs/running_agents.md`, `docs/results.md`,
  `docs/streaming.md`, `docs/tools.md`, `src/agents/stream_events.py`.
  Inspected ~Oct/Nov 2025.
- **Claude Code docs**:
  https://code.claude.com/docs/en/agent-sdk/python (CLI flags reference).

## Workspace map

```
<tigerharness repo>/
├── pyproject.toml                ← ONE project: tigerharness (hatchling, uv dev group,
│                                   [tool.coverage] with fail_under = 100)
├── docs/agent_sdk.md             ← the user-facing reference for this package
├── src/tigerharness/
│   ├── vendors.py                ← vendor -> backend map + per-persona resolution (ADR 0011)
│   └── agent_sdk/                ← THE importable package
│       ├── __init__.py           ← public re-exports + factory glue
│       ├── types.py              ← single source of truth for the public surface
│       ├── errors.py             ← AgentSDKError hierarchy
│       ├── factory.py            ← get_backend() / register_backend() / list_backends()
│       ├── retry.py              ← run_with_retry (exponential backoff)
│       ├── README.md             ← usage docs + extras + limits
│       ├── backends/
│       │   ├── _base.py          ← BaseStreamHandle + run_via_stream helper
│       │   ├── claude_p.py       ← working backend (claude -p subprocess + stream-json)
│       │   ├── codex_exec.py     ← working backend (codex exec --json subprocess)
│       │   ├── anthropic_sdk.py  ← working backend (wraps claude-agent-sdk)
│       │   └── openai_sdk.py     ← stub, raises with wiring notes
│       ├── examples/             ← basic / streaming / multi_turn / builtin_tools
│       └── docs/
│           ├── HANDOFF.md        ← this file
│           └── agent_sdk_comparison.md  ← design record; v1 critique + v2 interface
└── tests/agent_sdk/              ← outside the package (not in the wheel)
    ├── conftest.py               ← isolated_registry, fake-CLI fixtures
    ├── _helpers.py               ← asyncio_test decorator
    ├── test_types.py / test_errors.py / test_factory.py / test_base.py
    ├── test_claude_p.py          ← the bulk; argv/stdin/runtime/edge cases
    ├── test_codex_exec.py        ← the codex exec twin
    ├── test_anthropic_sdk.py / test_stub_backends.py / test_retry.py
    └── test_examples.py / test_coverage_push.py
```

> **Layout note:** the SDK was folded into the tigerharness package in
> May 2026 (`src/tigerharness/agent_sdk/`); there is no standalone
> `agent-sdk` project or path source any more.

## Out of scope (deliberately — don't try to "fix")

The comparison doc explains the rationale; restated here so a future agent
doesn't waste effort:

- **Multi-agent handoffs.** OpenAI's `Agent.handoffs=[...]` and
  `Agent.as_tool()` are first-class; Anthropic's equivalent is subagents +
  the `Task` tool. The two don't map cleanly. Build orchestration *on top*
  of `AgentBackend` calls in user code instead of inside the interface.
- **Structured-output type parity.** `output_schema` accepts a JSON Schema
  dict or a pydantic model and is best-effort per backend. Not all backends
  will round-trip every type (unions, recursive models, ...). Document
  rather than over-engineer.
- **Hosted-tool catalog standardization.** OpenAI keeps adding hosted tools
  (image gen, file search, computer-use, ...) faster than we can name them.
  `BuiltinTool.name` is a string the *user's codebase* maps to whichever
  provider it targets. Don't try to maintain a cross-provider canonical
  registry.
- **Session portability across backends.** `Session` is owned by one
  backend. Mixing them across backends would require translating
  conversation state, which doesn't round-trip.
- **`AgentConfig.temperature` for `claude_p`.** The CC CLI doesn't expose
  a temperature flag. Set it via a settings file passed through
  `cfg.extra["settings"]` if needed. Don't try to synthesise one.

## The interface in 30 seconds

Defined in `agent_sdk/types.py`. Every backend is a Protocol implementer:

```python
class AgentBackend(Protocol):
    async def run(self, config, prompt, *, session=None, approval=None) -> RunResult: ...
    def run_stream(self, config, prompt, *, session=None, approval=None) -> StreamHandle: ...
    async def open_session(self, *, resume_id=None) -> Session: ...
```

Caller code uses:

```python
from tigerharness.agent_sdk import AgentConfig, get_backend

backend = get_backend("claude_p")           # or "anthropic_sdk"
cfg = AgentConfig(name="qa", instructions="Be concise.")
result = await backend.run(cfg, "What is 2+2?")
```

`AgentConfig` carries the portable fields (`name`, `instructions`, `model`,
`tools`, `builtin_tools`, `output_schema`, `max_turns`) plus an
`extra: dict` escape hatch for backend-specific knobs.

`Event` is a discriminated union: `RunStart | TextDelta | MessageComplete |
ToolCall | ToolResult | Thinking | AgentChanged | ErrorEvent | RunDone`.
`StreamHandle` is an async iterator over `Event` that also exposes
`.result`, `.is_complete`, `.cancel()`, and works as an `async with`.

Other key types:

- `ToolSpec(name, description, input_schema, handler, needs_approval)` —
  user-defined Python tools (claude_p doesn't support these; see Backends).
- `BuiltinTool(name, config)` — provider-hosted tools (`Bash`, `Read`,
  `web_search`, etc.).
- `ApprovalCallback = Callable[[ApprovalRequest], Awaitable[ApprovalDecision]]`
  — gates tool calls. claude_p doesn't support these either.
- `Session` — opaque, owned by a single backend. Not portable across
  backends.

The full public surface is re-exported from `agent_sdk/__init__.py`
(`__all__` lists 41 names).

### Event types at a glance

| Event | When emitted |
|---|---|
| `RunStart(session_id, model)` | First event of a run, after the backend has spawned/initialised. |
| `TextDelta(text)` | Per-token assistant text. Not all backends emit these — `claude_p` doesn't (defaults to `MessageComplete`-only). |
| `MessageComplete(text, role="assistant")` | A full assistant message has been produced. |
| `ToolCall(id, name, arguments)` | Model requested a tool. |
| `ToolResult(id, name, output: ToolOutput)` | Tool produced a result; carries the matching `id`. |
| `Thinking(text)` | Reasoning trace, when the backend exposes one. |
| `AgentChanged(name)` | The active agent changed (OpenAI handoff). |
| `ErrorEvent(message, fatal=False)` | Non-terminal warning (e.g. bad JSON line); fatal errors come through `RunDone(stop_reason="error")`. |
| `RunDone(final_output, stop_reason, usage, cost_usd)` | Last event. `stop_reason ∈ {end_turn, max_turns, max_budget, tool_denied, interrupted, refusal, error}`. |

### Extension points

**`cfg.extra` keys read by `claude_p`** (anything else is silently ignored;
treat this as the contract):

| Key | Type | CLI flag |
|---|---|---|
| `permission_mode` | str | `--permission-mode` |
| `max_budget_usd` | float | `--max-budget-usd` |
| `add_dirs` | list[str] | one `--add-dir` per entry |
| `disallowed_tools` | list[str] | `--disallowedTools` |
| `settings` | str | `--settings <path>` |
| `cli_args` | dict[str, str \| None] | arbitrary `--<key> [value]` (None = bare flag) |

**Registering a custom backend** — the third extension point alongside
`cfg.extra` and `cli_args`:

```python
from tigerharness.agent_sdk import register_backend, AgentBackend, AgentConfig

class MyBackend:                               # implements AgentBackend
    async def run(self, cfg, prompt, *, session=None, approval=None): ...
    def run_stream(self, cfg, prompt, *, session=None, approval=None): ...
    async def open_session(self, *, resume_id=None): ...

register_backend("mine", lambda **kw: MyBackend(**kw))
backend = get_backend("mine", custom_kw="x")   # caller code unchanged
```

The factory forwards kwargs to your factory callable.

## Backends

### `claude_p` — done

`agent_sdk/backends/claude_p.py`. Spawns the `claude` CLI in headless mode
per `run_stream` call. Talks newline-delimited JSON over stdin/stdout (same
wire format the official `claude-agent-sdk` Python package uses).

Capabilities: built-in tools (via `--tools` and `--allowedTools`), multi-turn
sessions (via `--resume <session-id>`), structured output (via
`--json-schema`), permission modes through `cfg.extra["permission_mode"]`,
cancellation via SIGINT, concurrent stderr drain to prevent pipe-buffer
deadlock.

Hard limitations (raise `BackendNotImplementedError` upfront):

- `cfg.tools` (Python-defined tools) — would need an in-process MCP server.
- `approval` callback — would need an MCP permission-prompt tool.
- `BuiltinTool(..., config={...})` — the CC CLI configures hosted tools via
  settings files, not flags.

### `codex_exec` — done

`agent_sdk/backends/codex_exec.py`. The `chatgpt` vendor of
`tigerharness.vendors`. Spawns `codex exec --json` (or `codex exec resume
<thread-id> --json` for a later turn) per `run_stream` call, sends the
prompt on stdin and parses Codex's JSONL event stream (`thread.started` /
`turn.started` / `item.*` / `turn.completed` / `turn.failed`) into the same
normalized events `claude_p` emits.

Capabilities: multi-turn via the captured thread id; `instructions` as
`-c developer_instructions=<TOML string>`; `model` -> `-m`;
`permission_mode` mapped onto Codex's sandbox/approval flags
(`bypassPermissions` / `dontAsk` -> `--dangerously-bypass-approvals-and-sandbox`,
`acceptEdits` -> `workspace-write`, `plan` -> `read-only`); `add_dirs` and
`output_schema` (new sessions only); per-call `env`; `cli_args`;
cancellation via SIGINT. Knobs Codex has no per-run flag for (`max_turns`,
`max_budget_usd`, `disallowed_tools`, `settings`, `builtin_tools`) are
logged at DEBUG and ignored, so a config written for `claude_p` runs
unchanged. Same hard limitations as `claude_p` (no `ToolSpec`, no approval
callback). Tests: `tests/agent_sdk/test_codex_exec.py`, driven by fake
`codex` scripts.

### `anthropic_sdk` — working

`agent_sdk/backends/anthropic_sdk.py`. Wraps Anthropic's official
`claude-agent-sdk` Python package. `__init__` lazy-imports the SDK and
raises `BackendNotImplementedError` (with the install hint
`pip install tigerharness[anthropic]`) if the package isn't on PATH.

Translation:
- `AgentConfig.instructions` → `system_prompt`
- `AgentConfig.model` → `model`
- `AgentConfig.builtin_tools` → `allowed_tools` (caller's
  `extra["allowed_tools"]` overrides)
- `AgentConfig.max_turns` → `max_turns`
- A whitelisted set of `extra` keys (`permission_mode`, `cwd`,
  `disallowed_tools`, `add_dirs`, `max_budget_usd`, ...) pass through
  as-is. See `_PASSTHROUGH_EXTRA_KEYS`.
- `approval` callback adapts to `can_use_tool` via a wrapper that
  bridges `ApprovalRequest`/`ApprovalDecision` ↔
  `PermissionResultAllow`/`Deny`.

Run uses `ClaudeSDKClient` (the streaming, multi-turn client) so
`cancel()` maps to `client.interrupt()`. Sessions hold a long-lived
client; the original config is captured at first run -- subsequent
runs on the same session reuse the same client and ignore per-call
config differences (close + reopen to swap models).

Event mapping: `AssistantMessage.content` blocks → `MessageComplete` /
`ToolCall` / `Thinking`; `UserMessage` `ToolResultBlock`s →
`ToolResult` (with name looked up from a per-run
`tool_use_id → name` map); `SystemMessage(init)` → `RunStart`;
`ResultMessage` → `RunDone` with `stop_reason` / `cost_usd` mapped from
SDK subtypes (`error_max_turns` → `max_turns`, etc.).

Hard limitations:
- `cfg.tools` (Python `ToolSpec`) — not translated yet; would need an
  MCP server built via `create_sdk_mcp_server`. Raises explicitly so
  callers don't silently lose tools.

### `openai_sdk` — stub

`agent_sdk/backends/openai_sdk.py`. Raises on construction. To implement:
`pip install openai-agents`, translate `AgentConfig` → `Agent`
(`@function_tool`-wrap each `ToolSpec`, registry-map `BuiltinTool.name` →
`WebSearchTool` / `FileSearchTool` / `CodeInterpreterTool`), drive with
`Runner.run_streamed`, wrap the runner in an approval-loop that handles
`result.interruptions` → call user callback → `state.approve()` → resume,
and map `RunItemStreamEvent` / `RawResponsesStreamEvent` /
`AgentUpdatedStreamEvent` to our `Event` types. Full sketch including a
working `_to_events` is in `agent_sdk_comparison.md` §5.1.

The remaining stub satisfies the structural `AgentBackend` Protocol (its
`run`/`run_stream`/`open_session` exist with `# pragma: no cover` bodies)
so anyone catching the constructor's `BackendNotImplementedError` can still
type-check against `AgentBackend`.

## How to run things

Requires Python 3.11+ (PEP 604 union types, `match`, `from __future__ import
annotations`).

```bash
# One-time dev setup (from the tigerharness repo root):
uv sync --extra all                          # extras + the default dev group (pytest, pytest-asyncio, pytest-cov, coverage)

# Use the package (uv run puts the venv on PATH):
uv run python -m tigerharness.agent_sdk.examples.basic    # needs `claude` CLI on PATH
uv run python -m tigerharness.agent_sdk.examples.streaming
uv run python -m tigerharness.agent_sdk.examples.multi_turn
uv run python -m tigerharness.agent_sdk.examples.builtin_tools

# Test (pyproject.toml has testpaths = ["tests"]):
uv run pytest tests/agent_sdk -q             # the SDK alone
uv run pytest -q --cov=tigerharness --cov-report=term   # whole repo, 100% gate
uv run mypy --python-version 3.11 src/tigerharness/agent_sdk  # optional; mypy not in the dev group

# Sandbox-specific gotcha (not normally needed):
#   - mypy can't cache to read-only volumes; use --no-incremental --cache-dir=/tmp/...
```

The SDK ships inside the `tigerharness` distribution (`pip install
tigerharness`). The wheel packages the whole `src/tigerharness` tree —
this `docs/` folder included — and the tests live outside it at
`tests/`.

## Code conventions in this codebase

Match these when adding code so the package stays internally consistent:

- **Every module starts with `from __future__ import annotations`.** Keeps
  type annotations lazy so PEP 604 unions and forward refs work uniformly
  on 3.11.
- **No third-party imports in `agent_sdk/` source.** The package itself
  must remain stdlib-only. SDK backend dependencies (`claude-agent-sdk`,
  `openai-agents`) go inside the backend module and are imported lazily
  *inside* the function/method that needs them, so `import tigerharness.agent_sdk`
  doesn't fail when the optional dep is missing.
- **Dataclasses-everywhere** for value types; `Protocol` for behaviour
  types. No `class Foo: def __init__(self, ...)` boilerplate.
- **Async-only public API.** `run`, `run_stream`, `cancel`,
  `open_session`, tool handlers — all coroutines. Sync callers wrap with
  `asyncio.run(...)`. We do not provide sync helpers.
- **`# pragma: no cover`** is reserved for defensive cleanup paths that
  fire only on rare OS signals (already-dead processes, SIGINT timeouts).
  Don't use it to paper over untested logic.
- **Tests use `asyncio_test` from `tests/agent_sdk/_helpers.py`** — a thin
  `asyncio.run` wrapper. The repo also runs `pytest-asyncio` in
  `asyncio_mode = "auto"`; the two coexist, and either style is fine in
  new tests.
- **Public surface is re-exported from `agent_sdk/__init__.py`** and listed
  in `__all__`. New types should be added there.

## Concurrency and versioning

- **Concurrent `run_stream` calls from the same backend are safe.** Each
  call spawns its own subprocess (for `claude_p`) or its own SDK client
  (planned for the SDK backends), so no shared mutable state.
- **`Session` instances are not safe to share across concurrent runs.**
  Open one session per logical conversation.
- **Pre-1.0** (`__version__ = "0.1.0"`). Breaking interface changes are
  acceptable. There's no compat layer to maintain yet.
- **`pytest-asyncio` is installed** (dev group, `asyncio_mode = "auto"`).
  The older `asyncio_test` decorator wraps a *sync* function with
  `asyncio.run`, so pytest-asyncio never sees a coroutine there and the
  two do not compete.

## Conventions and gotchas (learned the hard way)

- **Cleanup contract for `StreamHandle`**: callers must EITHER consume the
  iterator to completion, OR call `cancel()`, OR use `async with`. Otherwise
  the subprocess lingers until SIGPIPE on its next stdout write.
- **Sessions are not portable across backends.** The `Session.id` populates
  *after* the first run (the CLI assigns it). `_set_id` on
  `_ClaudePSession` is idempotent — won't clobber an existing id.
- **`runtime_checkable` Protocol with `@property` will trigger the property
  getter during `isinstance(...)`** — and `BaseStreamHandle.result` raises
  before completion, so `isinstance(handle, StreamHandle)` blows up. Use
  `inspect.getattr_static` to verify the surface in tests.
- **Module names starting with a digit can't be imported** as
  `tigerharness.agent_sdk.examples.01_basic`. The example files are now plain names
  (`basic.py`, `streaming.py`, `multi_turn.py`, `builtin_tools.py`).
  `python -m` actually works via runpy's lenient lookup, but
  `import` doesn't.
- **stderr deadlock**: the CLI can block writing stderr if we don't drain it
  concurrently while reading stdout. There's an `asyncio.create_task` for a
  `_drain_stderr` coroutine in `_iter()`. The mutation test in
  `test_concurrent_stderr_drain_does_not_deadlock` deliberately emits ~2 MB
  of stderr interleaved with stdout to exercise the deadlock condition; if
  someone removes the drainer, that test fails.
- **`--tools` vs `--allowedTools`**: `--tools` restricts what the model
  *sees*, `--allowedTools` auto-approves them. We emit *both* with the same
  list so headless agents don't stall waiting for approval.
- **Caller's input prompt is seeded into the transcript** before iteration,
  because the CLI doesn't echo input back. This matches OpenAI's
  `to_input_list()` behaviour.
- **Coverage of defensive cleanup paths is `# pragma: no cover`** —
  specifically the SIGINT-on-dead-process and 3-second-timeout-then-terminate
  branches in `cancel()` and `_iter()` cleanup. They're hard to trigger
  reliably in tests.

## Sanity check on the test suite

The suite was mutation-tested against five deliberate bugs (drop tool name
lookup, invert max_turns mapping, skip the transcript seed, skip the
`--json-schema` flag, remove the concurrent stderr drainer). All five bugs
were caught by the existing tests. So the tests aren't just exercising lines
— they're asserting behaviour.

## Open work, in priority order

### Definition of done for a new backend

A backend is "complete" when:

1. It passes a parallel set of pipeline tests covering the same scenarios
   `claude_p` does today (success, tool roundtrip, max_turns, max_budget,
   generic error, non-zero exit, cancel, async-with cleanup,
   structured output, multi-turn session resume, transcript shape).
2. It additionally covers the features `claude_p` *can't*: user-defined
   `ToolSpec` execution and inline `ApprovalCallback`. Test both happy
   path and a denial.
3. The event-mapping function (`AssistantMessage` / `RunItemStreamEvent` →
   our `Event` union) is unit-tested against synthetic backend payloads.
4. `cfg.extra` keys are documented in the backend's module docstring; the
   shape mirrors the table above for `claude_p`.
5. Coverage of the new module is 100% (use `# pragma: no cover` only on
   defensive cleanup paths, like we did for `claude_p`).
6. Mypy clean: `mypy --python-version 3.11 src/tigerharness/agent_sdk` (mypy is not in the dev group; install it separately).

### Items

1. ~~**Implement the `anthropic_sdk` backend.**~~ — done. Wraps
   `claude-agent-sdk` via `ClaudeSDKClient`. Custom `ToolSpec` tools are
   still unsupported (would need an MCP server built via
   `create_sdk_mcp_server`); raises with a clear error rather than
   silently dropping them.

2. **Implement the `openai_sdk` backend.** Medium leverage. Look out for the
   approval-loop pattern (sketch in §5.1) — it's not just a `can_use_tool`
   callback; you must intercept `result.interruptions` and resume.

3. ~~**Add a `pyproject.toml`**~~ — done in the May 2026 reorg. The
   SDK now lives at `src/tigerharness/agent_sdk/` inside the
   tigerharness package; its dependencies are declared as optional
   extras (`tigerharness[anthropic]`) on the parent `pyproject.toml`.

4. **Wire `temperature` for `claude_p`.** Currently silently ignored — the
   CLI doesn't expose `--temperature`, but the user can set it via a
   settings file passed through `cfg.extra["settings"]`. Could detect a
   `temperature` and synthesise a temp settings file automatically, but
   that's invasive — leave as-is unless someone asks.

5. ~~**Move tests inside `agent_sdk/tests/`**~~ — superseded by the fold
   into tigerharness. Tests now live at `tests/agent_sdk/` in the repo
   (`testpaths = ["tests"]`), outside the wheel; `asyncio_test` is
   imported from `tests.agent_sdk._helpers`.

6. **Per-backend kwargs on `get_backend`.** Already supported (kwargs are
   forwarded), but no validation. If a typo is silently accepted by
   `ClaudePBackend(**kwargs)`, the user gets a confusing failure later.
   Consider a strict signature check.

## Iteration history

So future agents understand why the code looks the way it does:

1. **Compared OpenAI Agents SDK and Claude Agent SDK** — extracted the six
   shared concepts. Wrote `agent_sdk_comparison.md` v1 with an interface
   sketch.
2. **Critiqued v1, found 8 holes** (tool return types, approval semantics
   mismatch, prompt typing, transcript shape, session opacity, builtin-tool
   config, stream-handle ergonomics, free-string `stop_reason`). Rewrote as
   v2 in the same doc with verification against three concrete scenarios.
3. **Implemented `agent_sdk/`** with the v2 interface and a `claude_p`
   backend. Verified end-to-end with a stand-in CLI script.
4. **Critiqued the implementation, found 6 issues** — stderr deadlock,
   `output_schema` not wired, transcript missing input, async-with cleanup
   contract missing, session-id mutation leaking abstraction, untested
   error stop reasons. Fixed all six.
5. **Critiqued again, found 4 more** — example modules with digit prefixes,
   empty-prompt validation, mypy gaps, lack of `python -m` verification.
   Fixed.
6. **Wrote the test suite** — 158 tests across 7 modules, designed to run
   without `pytest-asyncio` (uses an `asyncio_test` decorator). Reached 99%
   coverage initially.
7. **Filled coverage gaps + critiqued the suite** — added 6 more tests
   (NotImplementedError-in-cancel, broken-aclose, blank-line skip,
   user-text-block, result-without-init, aclose-without-cancel). Marked
   defensive cleanup paths `# pragma: no cover`. Reached 100%.
8. **Final critique** — fragile try/finally registry mutation, no test
   instructions in README, mutation-test sanity check. Replaced try/finally
   with `isolated_registry` fixture, added Testing section to README,
   ran 5 deliberate bug injections — all caught except one: the
   stderr-drain test was too small. Fixed by bumping fake stderr to ~2 MB
   interleaved with stdout, plus an `asyncio.wait_for` so a regression fails
   fast instead of hanging.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `StreamNotConsumedError: Stream has not been fully consumed` | Reading `handle.result` before iteration finished, or after a `break` without `async with` | Iterate to `RunDone`, or wrap with `async with`, or call `await handle.cancel()` then keep iterating |
| `CLIError: \`claude\` not found on PATH` | Claude Code CLI not installed | install Claude Code (see anthropic.com/claude-code) and run `claude` once to log in |
| `BackendNotImplementedError: anthropic_sdk backend requires the claude-agent-sdk package` | `claude-agent-sdk` not installed | `pip install tigerharness[anthropic]` |
| `BackendNotImplementedError: openai_sdk backend is not yet implemented` | Trying to use the remaining stub | Use `claude_p`, `codex_exec` or `anthropic_sdk`; or implement the stub (see Open Work) |
| `BackendNotImplementedError: claude_p backend does not support user-defined ToolSpecs` | Passing `cfg.tools=[ToolSpec(...)]` to claude_p | Use `cfg.builtin_tools` instead. (`anthropic_sdk` also doesn't translate custom ToolSpecs yet -- would need an MCP server via `create_sdk_mcp_server`.) |
| Test suite hangs on `test_concurrent_stderr_drain_does_not_deadlock` | Concurrent stderr drainer was removed/broken; the `asyncio.wait_for(timeout=10)` should fail it fast | Inspect `_iter()` in `claude_p.py` — `stderr_task = asyncio.create_task(_drain_stderr())` must be present |
| `ModuleNotFoundError: No module named 'tigerharness.tigerharness.agent_sdk.examples.01_basic'` | Imported a digit-prefixed module name | Examples were renamed to `basic`, `streaming`, `multi_turn`, `builtin_tools`. Use those |
| `mypy: sqlite3.OperationalError: disk I/O error` | mypy can't write its cache to a read-only volume | `mypy --no-incremental --cache-dir=/tmp/.mypy_cache` |
| `isinstance(handle, StreamHandle)` raises `StreamNotConsumedError` | `runtime_checkable` Protocol invokes `@property` getters | Use `inspect.getattr_static` to verify the surface, or skip the isinstance check |
| Tests pass but a regression slips through | Coverage hits the line but the assertion is too loose | Run mutation testing — see "Sanity check on the test suite" |

## Quick orientation when picking this up

- Start by reading `agent_sdk_comparison.md` §1 (table) and §4 (the v2
  interface). Then skim `agent_sdk/types.py` — it's the source of truth.
- For implementation patterns, read `agent_sdk/backends/claude_p.py`
  end-to-end. The approval-loop and event-mapping sketches in
  `agent_sdk_comparison.md` §5 are the templates for the SDK-based
  backends.
- For test patterns, read `tests/agent_sdk/conftest.py` (the fake-CLI
  factory) and `tests/agent_sdk/_helpers.py` (`asyncio_test`), plus one of
  `tests/agent_sdk/test_claude_p.py`'s test classes; `test_codex_exec.py`
  is its twin for the Codex wire format.
- Run `uv run pytest tests/agent_sdk -q` first to confirm the baseline is
  green before changing anything.

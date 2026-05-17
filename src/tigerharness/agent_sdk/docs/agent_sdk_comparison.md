# Anthropic Agent SDK vs. OpenAI Agents SDK — Common Interface (v2)

A comparison of the two Python agent SDKs, plus a portable abstraction you can
drop into production code and back with whichever runtime you want
(`anthropic.claude-agent-sdk`, `openai-agents`, or `claude -p` as a subprocess).

This is the second pass of the design — the v1 sketch had real holes that
would have blown up in production. The fixes are spelled out in section 3.

Sources actually inspected for this writeup:
- `github.com/anthropics/claude-agent-sdk-python` — `query.py`, `client.py`,
  `types.py`, `__init__.py`.
- `github.com/openai/openai-agents-python` — `docs/agents.md`,
  `docs/running_agents.md`, `docs/results.md`, `docs/streaming.md`,
  `docs/tools.md`, plus `src/agents/stream_events.py`.

---

## 1. Side-by-side surface

| Concern | OpenAI Agents SDK | Claude Agent SDK |
|---|---|---|
| Agent definition | `Agent(name, instructions, model, model_settings, tools, handoffs, output_type, ...)` | `ClaudeAgentOptions(system_prompt, model, tools, allowed_tools, mcp_servers, max_turns, max_budget_usd, cwd, permission_mode, can_use_tool, hooks, ...)` |
| One-shot call | `Runner.run(agent, input, *, context, session, max_turns, run_config) -> RunResult` | `query(prompt, options) -> AsyncIterator[Message]` |
| Streaming call | `Runner.run_streamed(...) -> RunResultStreaming` (consume `result.stream_events()`) | `query(...)` is always streaming; each yielded `Message` is an event |
| Multi-turn, stateful | `Runner.run` plus a `Session` (e.g. `SQLiteSession`) or manual `result.to_input_list()` | `ClaudeSDKClient(options)` with `connect()`, `query(prompt)`, `receive_messages()` |
| Cancel mid-run | `result.cancel(mode="immediate"\|"after_turn")` | `await client.interrupt()` |
| Final output | `result.final_output` (str or `output_type` instance) | Last `AssistantMessage` + `ResultMessage.result` / `.structured_output` |
| Conversation items | `result.new_items` (typed RunItems); `result.to_input_list()` | Each yielded `Message` (`UserMessage`, `AssistantMessage`, `SystemMessage`, `ResultMessage`) |
| Function tool | `@function_tool` (schema inferred from signature + docstring) | `@tool(name, description, input_schema)` + `create_sdk_mcp_server(...)` wired through `options.mcp_servers` |
| Built-in tools | `WebSearchTool()`, `FileSearchTool()`, `CodeInterpreterTool()`, `ComputerTool()`, `ShellTool()`, `HostedMCPTool()`, `ImageGenerationTool()` — typed objects with config | String names of CC tools: `"Bash"`, `"Read"`, `"Edit"`, `"Write"`, `"WebSearch"`, `"WebFetch"`, ... |
| Human-in-the-loop | `ToolApprovalItem` → `result.to_state().approve(...)` → resume `Runner.run` with the state | `permission_mode` + `can_use_tool` callback (inline) + hooks |
| Sessions / resume | `Session` Protocol (`SQLiteSession`, custom); also OpenAI-managed `conversation_id` / `previous_response_id` | `session_id`, `resume`, `continue_conversation`; pluggable `SessionStore` Protocol |
| Structured output | `output_type=SomePydantic` (typed) | `ResultMessage.structured_output` (CLI-driven, JSON Schema) |
| Multi-agent | First-class `handoffs=[other_agent]`, `Agent.as_tool()` | Subagents via `AgentDefinition` and the `Task` tool |
| Custom transport | Single network transport (Responses HTTP/SSE/WebSocket) | `Transport` Protocol; default = `SubprocessCLITransport` over the `claude` CLI |

---

## 2. Conceptual mapping

Strip away the naming and both SDKs converge on the same six concepts:

1. **Agent config** — `system_prompt`, `model`, `tools`, run limits, plus
   backend-specific knobs.
2. **One-shot run** — prompt + config in, final answer + transcript + usage out.
3. **Streaming run** — same call, but events are delivered incrementally.
4. **Multi-turn session** — a handle that carries history forward.
5. **Tool definition** — name + description + JSON Schema + async callable.
6. **Permission / approval** — a policy that gates tool execution.

OpenAI's design is `Agent + Runner` (declarative description + loop driver).
Anthropic's design is `Options + (query | ClaudeSDKClient)` (one-shot vs.
interactive). The two are isomorphic if you map `Agent` ↔ `ClaudeAgentOptions`
and `Runner` ↔ `query`/`Client`.

What does **not** generalize cleanly:
- **Handoffs** — keep multi-agent orchestration outside the interface.
- **Permission modes** like `"plan"` / `"acceptEdits"` — CC-specific; expose
  the *callback*, not the mode.
- **Hosted tools** — provider-specific configuration; treat as named
  tools-with-config rather than typed objects.
- **Structured-output typing parity** — best-effort per backend.

---

## 3. Critique of the v1 sketch

Walking through v1 against actual SDK behavior surfaced eight problems. They're
listed below with the fix that rolls into v2.

**3.1 — `tool handler return type was `Any`.**
Claude's `@tool` requires the return value to be MCP-shaped
(`{"content": [{"type": "text", "text": "..."}]}`). OpenAI's `@function_tool`
JSON-encodes whatever Python value you return. If we keep `Any`, the user has
to know which backend they're targeting. **Fix:** define a `ToolOutput`
return type (`str` for plain text, or a structured `ToolOutput` dataclass) and
let each backend wrap it correctly.

**3.2 — approval callback elided OpenAI's interrupt-resume model.**
Claude's `can_use_tool` is invoked **inline** during the run. OpenAI's
approval flow **stops the run**, returns to the caller, and you must
re-invoke `Runner.run(...)` with the saved state. v1 hid this difference but
didn't say how the OpenAI backend should reconstitute it. **Fix:** the
OpenAI backend wraps `Runner.run` in a loop that, on `result.interruptions`,
calls the user's `approval` callback for each pending tool call, applies the
decisions to `result.to_state()`, and resumes — until there are no more
interruptions. Caller never sees the difference.

**3.3 — `prompt: str | list[dict[str, Any]]` was undefined cross-backend.**
A "list of dict items" means OpenAI Responses items in one SDK and CC
streaming-input items in the other. They're not interchangeable.
**Fix:** normalize to `str | list[InputMessage]` where `InputMessage` is our
own dataclass; let backends translate.

**3.4 — `RunResult.messages: list[dict[str, Any]]` had no shape.**
Same problem as the prompt. **Fix:** `transcript: list[NormalizedMessage]`
plus a `raw: Any` escape hatch for backend-native objects.

**3.5 — `Session.id` doesn't round-trip across SDKs.**
OpenAI's `SQLiteSession` is an *instance* you pass to `Runner.run`, not just a
string ID. v1's "id + close()" Protocol is too thin. **Fix:** `Session`
becomes a Protocol whose concrete subclasses are owned by each backend; the
Protocol exposes `id` and `close()` for inspection, plus a private
`_handle: Any` slot for the backend's native session object. Sessions are
**not portable** between backends; that limitation is now explicit.

**3.6 — `builtin_tools: list[str]` couldn't carry config.**
OpenAI's `WebSearchTool(filters=..., user_location=..., search_context_size=...)`
takes parameters; a bare string can't represent that. **Fix:** introduce a
`BuiltinTool(name: str, config: dict[str, Any])` dataclass.

**3.7 — Streaming events were too coarse and the iterator gave no
`final_output`.** v1 returned a bare `AsyncIterator[Event]`, but to also get a
`RunResult`, callers had to consume `RunDone` and reconstruct things.
**Fix:** `run_stream` returns a `StreamHandle` that *is* an async iterator
*and* exposes `.result` after iteration completes, plus `.cancel(...)`. This
matches OpenAI's `RunResultStreaming` shape and gives the Anthropic backend
somewhere to surface `interrupt()`.

**3.8 — `stop_reason` was a free string.** **Fix:** make it a `Literal` with
a fixed set of values.

---

## 4. Revised unified interface (v2)

```python
# agent_iface.py
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

# ---------- Content parts and messages ----------

@dataclass
class TextPart:
    text: str

@dataclass
class ToolUsePart:
    id: str
    name: str
    input: dict[str, Any]

@dataclass
class ToolResultPart:
    tool_use_id: str
    content: str | list[Any]
    is_error: bool = False

@dataclass
class ThinkingPart:
    text: str

ContentPart = TextPart | ToolUsePart | ToolResultPart | ThinkingPart

@dataclass
class InputMessage:
    """Caller-supplied input. Always normalized to this shape."""
    role: Literal["user", "assistant", "system", "tool"]
    content: str | list[ContentPart]

@dataclass
class NormalizedMessage:
    """Transcript entries returned by backends."""
    role: Literal["user", "assistant", "system", "tool"]
    content: list[ContentPart]


# ---------- Tools ----------

@dataclass
class ToolOutput:
    """Structured tool return. Use `ToolOutput.text("...")` for plain strings."""
    text: str | None = None
    data: Any = None              # arbitrary JSON-serializable structured payload
    is_error: bool = False

    @staticmethod
    def of(value: Any) -> "ToolOutput":
        if isinstance(value, ToolOutput): return value
        if isinstance(value, str):        return ToolOutput(text=value)
        return ToolOutput(data=value)

ToolHandler = Callable[[dict[str, Any]], Awaitable[ToolOutput | str | Any]]

@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]   # JSON Schema (object type)
    handler: ToolHandler
    needs_approval: bool = False

@dataclass
class BuiltinTool:
    """Provider-hosted tool. Backend looks `name` up in its registry."""
    name: str                       # e.g. "web_search", "code_interpreter", "Bash"
    config: dict[str, Any] = field(default_factory=dict)


# ---------- Approval / human-in-the-loop ----------

@dataclass
class ApprovalRequest:
    tool_call: "ToolCall"
    agent_name: str
    session_id: str | None

@dataclass
class ApprovalDecision:
    allow: bool
    reason: str | None = None
    updated_input: dict[str, Any] | None = None   # optional input rewrite

ApprovalCallback = Callable[[ApprovalRequest], Awaitable[ApprovalDecision]]


# ---------- Streaming events ----------

@dataclass
class RunStart:
    session_id: str | None
    model: str | None

@dataclass
class TextDelta:
    text: str

@dataclass
class MessageComplete:
    text: str
    role: Literal["assistant"] = "assistant"

@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

@dataclass
class ToolResult:
    id: str
    name: str
    output: ToolOutput

@dataclass
class Thinking:
    text: str

@dataclass
class AgentChanged:
    """Emitted when a backend swaps the active agent (e.g. OpenAI handoff)."""
    name: str

@dataclass
class ErrorEvent:
    """Non-terminal error. Terminal errors come through RunDone(stop_reason='error')."""
    message: str
    fatal: bool = False

StopReason = Literal[
    "end_turn", "max_turns", "max_budget", "tool_denied",
    "interrupted", "refusal", "error",
]

@dataclass
class RunDone:
    final_output: Any
    stop_reason: StopReason
    usage: dict[str, Any] | None
    cost_usd: float | None

Event = (RunStart | TextDelta | MessageComplete | ToolCall | ToolResult
         | Thinking | AgentChanged | ErrorEvent | RunDone)


# ---------- Run result (for non-streaming run()) ----------

@dataclass
class RunResult:
    final_output: Any
    transcript: list[NormalizedMessage]
    stop_reason: StopReason
    usage: dict[str, Any] | None
    cost_usd: float | None
    raw: Any                          # backend-native escape hatch


# ---------- Session ----------

@runtime_checkable
class Session(Protocol):
    """Opaque, multi-turn handle owned by a single backend.

    Sessions are NOT portable across backends. Pass the session you got from
    backend X back to backend X.
    """
    @property
    def id(self) -> str: ...
    async def close(self) -> None: ...


# ---------- Stream handle ----------

class StreamHandle(Protocol):
    """Async iterator over Events that also exposes the final RunResult."""
    def __aiter__(self) -> AsyncIterator[Event]: ...
    async def __anext__(self) -> Event: ...

    @property
    def result(self) -> RunResult: ...                          # raises if not complete
    @property
    def is_complete(self) -> bool: ...
    async def cancel(self, *, after_turn: bool = False) -> None: ...


# ---------- Agent config ----------

@dataclass
class AgentConfig:
    name: str
    instructions: str | None = None
    model: str | None = None
    tools: list[ToolSpec] = field(default_factory=list)
    builtin_tools: list[BuiltinTool] = field(default_factory=list)
    output_schema: type | dict[str, Any] | None = None         # pydantic model or JSON Schema
    max_turns: int | None = None
    temperature: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)        # backend-specific knobs


# ---------- The interface every backend implements ----------

class AgentBackend(Protocol):
    async def run(
        self,
        config: AgentConfig,
        prompt: str | list[InputMessage],
        *,
        session: Session | None = None,
        approval: ApprovalCallback | None = None,
    ) -> RunResult: ...

    def run_stream(
        self,
        config: AgentConfig,
        prompt: str | list[InputMessage],
        *,
        session: Session | None = None,
        approval: ApprovalCallback | None = None,
    ) -> StreamHandle: ...

    async def open_session(self, *, resume_id: str | None = None) -> Session: ...
```

Convention: **`run()` is implemented in terms of `run_stream()`** — accumulate
events, return `RunResult` from `RunDone`. This avoids two divergent code paths
in every backend.

```python
async def _run_via_stream(self, *args, **kwargs) -> RunResult:
    handle = self.run_stream(*args, **kwargs)
    async for _ in handle:
        pass
    return handle.result
```

---

## 5. Backend sketches (now with the hard parts shown)

### 5.1 OpenAI backend — real event mapper

The OpenAI side emits three event categories:
`RawResponsesStreamEvent` (token deltas), `RunItemStreamEvent` (semantic items
named `message_output_created`, `tool_called`, `tool_output`,
`reasoning_item_created`, `mcp_approval_requested`, ...), and
`AgentUpdatedStreamEvent`. Map them like this:

```python
from openai.types.responses import ResponseTextDeltaEvent
from agents import (
    Agent, Runner, function_tool, ItemHelpers, RunConfig,
    RawResponsesStreamEvent, RunItemStreamEvent, AgentUpdatedStreamEvent,
    SQLiteSession,
)

def _to_events(ev) -> list[Event]:
    if isinstance(ev, RawResponsesStreamEvent):
        if isinstance(ev.data, ResponseTextDeltaEvent):
            return [TextDelta(text=ev.data.delta)]
        return []                                                     # ignore other raw deltas

    if isinstance(ev, RunItemStreamEvent):
        item = ev.item
        if ev.name == "message_output_created":
            return [MessageComplete(text=ItemHelpers.text_message_output(item))]
        if ev.name == "tool_called":
            raw = item.raw_item                                       # ResponseFunctionToolCall
            return [ToolCall(id=raw.call_id, name=raw.name,
                             arguments=json.loads(raw.arguments))]
        if ev.name == "tool_output":
            raw = item.raw_item                                       # function_call_output
            out = ToolOutput.of(item.output)
            return [ToolResult(id=raw.call_id, name=getattr(raw, "name", ""), output=out)]
        if ev.name == "reasoning_item_created":
            return [Thinking(text=ItemHelpers.text_message_output(item))]
        # mcp_approval_requested is consumed by the approval-loop wrapper, not surfaced.
        return []

    if isinstance(ev, AgentUpdatedStreamEvent):
        return [AgentChanged(name=ev.new_agent.name)]
    return []


class OpenAIBackend:
    def _wrap_tool(self, t: ToolSpec):
        @function_tool(name_override=t.name, description_override=t.description)
        async def _tool(**kwargs):
            out = await t.handler(kwargs)
            return ToolOutput.of(out).text or ToolOutput.of(out).data
        return _tool

    def _wrap_builtin(self, b: BuiltinTool):
        from agents import WebSearchTool, FileSearchTool, CodeInterpreterTool
        return {                       # registry; extend as needed
            "web_search":      lambda: WebSearchTool(**b.config),
            "file_search":     lambda: FileSearchTool(**b.config),
            "code_interpreter":lambda: CodeInterpreterTool(**b.config),
        }[b.name]()

    def _agent(self, cfg: AgentConfig) -> Agent:
        tools = [self._wrap_tool(t) for t in cfg.tools]
        tools += [self._wrap_builtin(b) for b in cfg.builtin_tools]
        return Agent(
            name=cfg.name,
            instructions=cfg.instructions or "",
            model=cfg.model,
            tools=tools,
            output_type=cfg.output_schema,
            **cfg.extra,
        )

    def run_stream(self, cfg, prompt, *, session=None, approval=None) -> StreamHandle:
        agent = self._agent(cfg)
        # ApprovalLoop wraps Runner.run_streamed: on each interruption it calls
        # `approval(...)` for every pending ToolApprovalItem, mutates the
        # RunState, then resumes. Yields our Event stream throughout.
        return _OpenAIStreamHandle(agent, prompt, session, approval, cfg.max_turns)

    async def run(self, cfg, prompt, *, session=None, approval=None) -> RunResult:
        return await _run_via_stream(self, cfg, prompt, session=session, approval=approval)

    async def open_session(self, *, resume_id=None) -> Session:
        return _OpenAISession(SQLiteSession(resume_id or _uuid()))
```

`_OpenAIStreamHandle` does the approval-loop:

```python
class _OpenAIStreamHandle:
    def __init__(self, agent, prompt, session, approval, max_turns):
        self._agent, self._prompt = agent, prompt
        self._session = session._handle if session else None
        self._approval, self._max_turns = approval, max_turns
        self._result: RunResult | None = None
        self._gen = self._iter()

    async def _iter(self):
        sess_id = getattr(self._session, "id", None)
        yield RunStart(session_id=sess_id, model=self._agent.model)
        current_input = self._prompt
        last = None
        while True:
            stream = Runner.run_streamed(self._agent, current_input,
                                         session=self._session, max_turns=self._max_turns)
            async for ev in stream.stream_events():
                for out in _to_events(ev): yield out
            last = stream                                                  # finished or paused
            if not last.interruptions: break
            if self._approval is None:                                     # nothing to do
                yield RunDone(final_output=None, stop_reason="tool_denied",
                              usage=None, cost_usd=None); return
            state = last.to_state()
            for it in last.interruptions:
                req = ApprovalRequest(
                    tool_call=ToolCall(id=it.raw_item.call_id, name=it.raw_item.name,
                                       arguments=json.loads(it.raw_item.arguments)),
                    agent_name=self._agent.name, session_id=sess_id)
                decision = await self._approval(req)
                state.approve(it) if decision.allow else state.reject(it, reason=decision.reason)
            current_input = state                                          # resume from state

        self._result = RunResult(
            final_output=last.final_output,
            transcript=_to_normalized(last.to_input_list()),
            stop_reason="end_turn",
            usage=getattr(last.context_wrapper, "usage", None).__dict__ if last.context_wrapper else None,
            cost_usd=None,
            raw=last,
        )
        yield RunDone(self._result.final_output, "end_turn",
                      self._result.usage, self._result.cost_usd)

    def __aiter__(self): return self
    async def __anext__(self): return await self._gen.__anext__()
    @property
    def result(self):
        if self._result is None: raise RuntimeError("stream not consumed yet")
        return self._result
    @property
    def is_complete(self): return self._result is not None
    async def cancel(self, *, after_turn=False):
        # delegated to the underlying Runner streaming result
        ...
```

### 5.2 Anthropic backend

```python
from claude_agent_sdk import (
    ClaudeAgentOptions, ClaudeSDKClient, query, tool, create_sdk_mcp_server,
    AssistantMessage, UserMessage, ResultMessage,
    TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock,
    PermissionResultAllow, PermissionResultDeny,
)

def _from_claude_message(m) -> list[Event]:
    if isinstance(m, AssistantMessage):
        events = []
        text_parts = []
        for b in m.content:
            if isinstance(b, TextBlock):       text_parts.append(b.text)
            elif isinstance(b, ThinkingBlock): events.append(Thinking(text=b.thinking))
            elif isinstance(b, ToolUseBlock):
                events.append(ToolCall(id=b.id, name=b.name, arguments=b.input))
        if text_parts:
            events.append(MessageComplete(text="".join(text_parts)))
        return events
    if isinstance(m, UserMessage):
        out = []
        if isinstance(m.content, list):
            for b in m.content:
                if isinstance(b, ToolResultBlock):
                    out.append(ToolResult(id=b.tool_use_id, name="",
                                          output=ToolOutput.of(b.content)))
        return out
    if isinstance(m, ResultMessage):
        stop = "end_turn"
        if m.is_error:                      stop = "error"
        elif m.subtype == "error_max_turns": stop = "max_turns"
        elif m.subtype == "error_max_budget_usd": stop = "max_budget"
        return [RunDone(final_output=m.structured_output or m.result,
                        stop_reason=stop,
                        usage=m.usage, cost_usd=m.total_cost_usd)]
    return []


class AnthropicBackend:
    def _options(self, cfg: AgentConfig, approval) -> ClaudeAgentOptions:
        sdk_tools = [tool(t.name, t.description, t.input_schema)(_wrap(t)) for t in cfg.tools]
        servers = ({"app": create_sdk_mcp_server("app", tools=sdk_tools)} if sdk_tools else {})
        builtin_names = [b.name for b in cfg.builtin_tools]    # "Bash", "WebSearch", ...

        async def _can_use_tool(name, input_, ctx):
            if approval is None: return PermissionResultAllow()
            req = ApprovalRequest(
                tool_call=ToolCall(id=ctx.tool_use_id, name=name, arguments=input_),
                agent_name=cfg.name, session_id=ctx.session_id)
            d = await approval(req)
            if d.allow:
                return PermissionResultAllow(updated_input=d.updated_input or input_)
            return PermissionResultDeny(message=d.reason or "denied")

        return ClaudeAgentOptions(
            system_prompt=cfg.instructions,
            model=cfg.model,
            tools=builtin_names or None,
            mcp_servers=servers,
            max_turns=cfg.max_turns,
            can_use_tool=_can_use_tool if approval else None,
            **cfg.extra,
        )

    def run_stream(self, cfg, prompt, *, session=None, approval=None) -> StreamHandle:
        return _AnthropicStreamHandle(self._options(cfg, approval), prompt, session, cfg)
    # run() and open_session() omitted; same shape as OpenAI backend
```

`_AnthropicStreamHandle` uses `ClaudeSDKClient` (not `query()`) so that
`cancel()` can map to `await client.interrupt()`.

### 5.3 `claude -p` subprocess backend

```python
class ClaudePBackend:
    """Minimal backend that shells out to `claude -p --output-format stream-json`."""

    def __init__(self, cli: str = "claude"):
        self.cli = cli

    def run_stream(self, cfg, prompt, *, session=None, approval=None) -> StreamHandle:
        if approval is not None:
            raise NotImplementedError("approval requires --permission-prompt-tool plumbing")
        args = [self.cli, "-p", "--output-format", "stream-json"]
        if cfg.model:        args += ["--model", cfg.model]
        if cfg.max_turns:    args += ["--max-turns", str(cfg.max_turns)]
        if cfg.instructions: args += ["--system-prompt", cfg.instructions]
        if cfg.builtin_tools:
            args += ["--allowedTools", ",".join(b.name for b in cfg.builtin_tools)]
        if session:          args += ["--resume", session.id]
        return _ClaudePStreamHandle(args, prompt)
```

The subprocess backend is intentionally limited: no approval callback (would
need an MCP permission-prompt tool), no Python-defined tools (would need an
SDK MCP server, at which point use the Anthropic backend). It exists for
deployment scenarios where you can't or don't want to import the Python SDK.

---

## 6. Verification: trace three scenarios through all three backends

### Scenario A — simple Q&A, no tools

```python
cfg = AgentConfig(name="qa", instructions="Be concise.", model="claude-sonnet-4-6")
result = await backend.run(cfg, "What is 2+2?")
print(result.final_output)
```

- **OpenAI**: builds `Agent(name="qa", instructions="Be concise.", model=...)`,
  calls `Runner.run_streamed`, consumes events; `message_output_created`
  becomes `MessageComplete`; `result.final_output` populated; `RunDone` emitted
  with `stop_reason="end_turn"`.  ✔
- **Anthropic**: builds `ClaudeAgentOptions(system_prompt=..., model=...)`,
  iterates `query(prompt, options=...)`; `AssistantMessage` →
  `MessageComplete`; `ResultMessage` → `RunDone`.  ✔
- **`claude -p`**: spawns `claude -p --system-prompt "Be concise." --model ...`;
  stream-JSON lines map the same way as the Anthropic backend.  ✔

### Scenario B — function tool with approval

```python
cfg = AgentConfig(
    name="ops", instructions="Use tools when needed.",
    tools=[ToolSpec("delete_file", "Delete a file",
                    {"type": "object", "properties": {"path": {"type": "string"}},
                     "required": ["path"]},
                    handler=delete, needs_approval=True)],
)

async def gate(req: ApprovalRequest) -> ApprovalDecision:
    return ApprovalDecision(allow=req.tool_call.arguments["path"].startswith("/tmp/"))

result = await backend.run(cfg, "Clean up /tmp/old.log", approval=gate)
```

- **OpenAI**: `function_tool` wraps `delete`; the `_OpenAIStreamHandle`
  approval loop catches `result.interruptions`, calls `gate(...)` for each,
  applies `state.approve()`/`state.reject(...)`, resumes. `ToolCall` and
  `ToolResult` events appear in order. ✔
- **Anthropic**: `@tool` registers `delete` inside an SDK MCP server;
  `can_use_tool` calls `gate(...)` inline; `PermissionResultAllow/Deny`
  carries the decision back. The Claude side also lets `gate` rewrite the
  input via `updated_input`, which we already pipe through. ✔
- **`claude -p`**: not supported — backend raises. Caller picks a different
  backend or accepts the limitation. ✔ (documented)

### Scenario C — multi-turn with cancel

```python
session = await backend.open_session()
handle = backend.run_stream(cfg, "Start a long task", session=session)
async for ev in handle:
    if isinstance(ev, TextDelta): print(ev.text, end="", flush=True)
    if user_pressed_ctrl_c: await handle.cancel(after_turn=True); break
# Continue the same conversation:
result = await backend.run(cfg, "What did you find?", session=session)
```

- **OpenAI**: `open_session` returns an `_OpenAISession` wrapping a
  `SQLiteSession`. `run_stream` passes `session=self._handle` to
  `Runner.run_streamed`. `cancel(after_turn=True)` calls
  `result.cancel(mode="after_turn")` on the underlying streaming result.  ✔
- **Anthropic**: `open_session` returns an `_AnthropicSession` whose `id` is a
  UUID we plug into `options.session_id`. `cancel()` delegates to
  `client.interrupt()` on the held `ClaudeSDKClient`.  ✔
- **`claude -p`**: each call spawns a new subprocess; `session.id` becomes
  `--resume <id>` on the next invocation. `cancel()` sends `SIGINT` to the
  subprocess. Multi-turn is "fork a new process per turn, resume by ID"
  rather than "keep the same process alive."  ✔ (with the caveat documented)

All three scenarios go through the same caller-side code.

---

## 7. Remaining limitations and escape hatches

- **No multi-agent / handoff abstraction.** Compose `AgentBackend` calls in
  user code. Trying to unify handoffs would force the Anthropic backend to
  fake them via subagent tools, which is messier than just leaving them out.
- **No portable structured output guarantees.** Pydantic models are passed to
  OpenAI as `output_type`; for Claude the backend converts them to a JSON
  Schema and pipes them through `extra` to the CLI. Conversion can be lossy
  for unions and recursive types — when that matters, fall back to plain text
  + parse-yourself.
- **Sessions are not portable.** A session opened by `OpenAIBackend` cannot
  be passed to `AnthropicBackend`. The interface enforces this by making
  each backend return its own concrete `Session`.
- **Cost / usage shape is messy.** `usage: dict[str, Any]` is a pragmatic
  escape hatch; only `cost_usd` is reliable, and only for Claude.
- **Hosted-tool catalog drift.** OpenAI adds new hosted tools (image gen,
  file search, computer-use, ...) faster than this interface can name them.
  Treat the `BuiltinTool.name` registry as a source of truth your codebase
  owns; passing an unknown name surfaces a `KeyError` at construction time.
- **`extra: dict` and `raw: Any` are first-class.** Production code always
  ends up needing one provider-specific knob (sandbox config, reasoning
  budget, prompt template ID, hosted-tool params, hooks). Punching them
  through `extra` keeps the typed surface small without forcing a fork.
- **`claude -p` is best for stateless / batch / CI scenarios.** It can't host
  Python-defined tools or do inline approval. For interactive use, prefer the
  Anthropic backend with `ClaudeSDKClient` under the hood.

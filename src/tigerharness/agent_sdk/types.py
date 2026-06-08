"""Backend-agnostic types for the agent SDK.

This module is the single source of truth for the public interface.
Anything you import from `agent_sdk` ultimately comes from here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable


# ===== Content parts =====

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


# ===== Messages =====

Role = Literal["user", "assistant", "system", "tool"]


@dataclass
class InputMessage:
    """Caller-supplied input. Always normalized to this shape."""
    role: Role
    content: str | list[ContentPart]


@dataclass
class NormalizedMessage:
    """Transcript entries returned by backends."""
    role: Role
    content: list[ContentPart]


# ===== Tools =====

@dataclass
class ToolOutput:
    """Structured tool return value.

    Use ``ToolOutput.of(value)`` to wrap a string or arbitrary Python value.
    """
    text: str | None = None
    data: Any = None
    is_error: bool = False

    @staticmethod
    def of(value: Any) -> "ToolOutput":
        if isinstance(value, ToolOutput):
            return value
        if isinstance(value, str):
            return ToolOutput(text=value)
        if value is None:
            return ToolOutput()
        return ToolOutput(data=value)


ToolHandler = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass
class ToolSpec:
    """A user-defined tool. Backends that support tools wrap `handler` and
    expose it to the model under `name`.
    """
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    needs_approval: bool = False


@dataclass
class BuiltinTool:
    """Provider-hosted tool. Each backend maps `name` to its native object.

    Examples:
        BuiltinTool("Bash")                                     # Claude Code
        BuiltinTool("WebSearch", {"max_uses": 3})               # Claude Code
        BuiltinTool("web_search", {"search_context_size": "medium"})  # OpenAI
        BuiltinTool("file_search", {"vector_store_ids": [...]}) # OpenAI
    """
    name: str
    config: dict[str, Any] = field(default_factory=dict)


# ===== Approval / human-in-the-loop =====

@dataclass
class ApprovalRequest:
    tool_call: "ToolCall"
    agent_name: str
    session_id: str | None


@dataclass
class ApprovalDecision:
    allow: bool
    reason: str | None = None
    updated_input: dict[str, Any] | None = None


ApprovalCallback = Callable[[ApprovalRequest], Awaitable[ApprovalDecision]]


# ===== Streaming events =====

@dataclass
class RunStart:
    session_id: str | None
    model: str | None


@dataclass
class TextDelta:
    """Incremental assistant text token. Not all backends emit these;
    some only emit MessageComplete.
    """
    text: str


@dataclass
class MessageComplete:
    """A full assistant message has been produced."""
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
    """Reasoning trace, when the backend exposes one."""
    text: str


@dataclass
class AgentChanged:
    """The active agent changed (e.g. OpenAI handoff)."""
    name: str


@dataclass
class ErrorEvent:
    """Non-terminal error. Terminal errors come through RunDone(stop_reason='error')."""
    message: str
    fatal: bool = False


StopReason = Literal[
    "end_turn",
    "max_turns",
    "max_budget",
    "tool_denied",
    "interrupted",
    "refusal",
    "error",
]


@dataclass
class RunDone:
    final_output: Any
    stop_reason: StopReason
    usage: dict[str, Any] | None
    cost_usd: float | None


Event = (
    RunStart
    | TextDelta
    | MessageComplete
    | ToolCall
    | ToolResult
    | Thinking
    | AgentChanged
    | ErrorEvent
    | RunDone
)


# ===== Run result =====

@dataclass
class RunResult:
    final_output: Any
    transcript: list[NormalizedMessage]
    stop_reason: StopReason
    usage: dict[str, Any] | None
    cost_usd: float | None
    raw: Any = None  # backend-native escape hatch


# ===== Agent configuration =====

@dataclass
class AgentConfig:
    """Declarative agent definition. Backend-specific knobs live in `extra`."""

    name: str
    instructions: str | None = None
    model: str | None = None

    tools: list[ToolSpec] = field(default_factory=list)
    builtin_tools: list[BuiltinTool] = field(default_factory=list)

    output_schema: type | dict[str, Any] | None = None
    max_turns: int | None = None
    temperature: float | None = None

    # Free-form, backend-specific options. See each backend's docstring for
    # supported keys (e.g. claude_p reads "permission_mode", "add_dirs",
    # "cli_args", "max_budget_usd", and "env" -- a dict[str, str] of
    # per-call subprocess env additions).
    extra: dict[str, Any] = field(default_factory=dict)


# ===== Session =====

@runtime_checkable
class Session(Protocol):
    """Opaque, multi-turn handle owned by a single backend.

    Sessions are NOT portable across backends. A session opened by backend A
    can only be passed back to backend A.
    """

    @property
    def id(self) -> str: ...

    async def close(self) -> None: ...


# ===== Stream handle =====

@runtime_checkable
class StreamHandle(Protocol):
    """Async iterator over Events that also exposes the final RunResult.

    Cleanup contract: callers must EITHER fully consume the iterator,
    OR call ``cancel()``, OR use the handle as an async context manager.
    Otherwise resources held by the backend (subprocesses, sockets) may
    linger until the next operating-system signal.

    Usage:
        # Pattern 1 — consume to completion:
        handle = backend.run_stream(cfg, prompt)
        async for event in handle:
            ...
        result = handle.result

        # Pattern 2 — async context manager (guaranteed cleanup):
        async with backend.run_stream(cfg, prompt) as handle:
            async for event in handle:
                if some_condition:
                    break  # cleanup runs at __aexit__

        # Pattern 3 — explicit cancel:
        handle = backend.run_stream(cfg, prompt)
        try:
            async for event in handle: ...
        finally:
            await handle.cancel()
    """

    def __aiter__(self) -> AsyncIterator[Event]: ...
    async def __anext__(self) -> Event: ...

    async def __aenter__(self) -> "StreamHandle": ...
    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None: ...

    @property
    def result(self) -> RunResult:
        """The completed RunResult. Raises StreamNotConsumedError if the
        stream has not been fully iterated yet.
        """
        ...

    @property
    def is_complete(self) -> bool: ...

    async def cancel(self, *, after_turn: bool = False) -> None:
        """Cancel a running stream. `after_turn=True` is a best-effort hint
        to let the current turn finish before stopping.
        """
        ...


# ===== Backend protocol =====

@runtime_checkable
class AgentBackend(Protocol):
    """Every concrete backend (claude_p, anthropic_sdk, openai_sdk, ...)
    implements this Protocol.
    """

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

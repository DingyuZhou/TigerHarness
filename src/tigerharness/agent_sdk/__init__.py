"""Backend-agnostic agent SDK.

Public API entry points:

    from tigerharness.agent_sdk import (
        AgentConfig, ToolSpec, BuiltinTool, ToolOutput,
        InputMessage, ApprovalRequest, ApprovalDecision,
        get_backend, register_backend,
    )

    backend = get_backend("claude_p")              # `claude -p` subprocess
    # backend = get_backend("anthropic_sdk")       # future
    # backend = get_backend("openai_sdk")          # future

    cfg = AgentConfig(name="qa", instructions="Be concise.")
    result = await backend.run(cfg, "What is 2 + 2?")
    print(result.final_output)

The interface is designed so caller code stays identical when you switch
backends. See ``agent_sdk_comparison.md`` for the design rationale.
"""

from __future__ import annotations

from .errors import (
    AgentSDKError,
    BackendNotImplementedError,
    CLIError,
    StreamNotConsumedError,
    ToolApprovalDenied,
)
from .factory import get_backend, list_backends, register_backend
from .retry import run_with_retry
from .types import (
    # Backend Protocol
    AgentBackend,
    # Config
    AgentConfig,
    AgentChanged,
    # Approval
    ApprovalCallback,
    ApprovalDecision,
    ApprovalRequest,
    # Tools
    BuiltinTool,
    ContentPart,
    ErrorEvent,
    Event,
    InputMessage,
    MessageComplete,
    NormalizedMessage,
    Role,
    # Result
    RunDone,
    RunResult,
    RunStart,
    # Session / stream
    Session,
    StopReason,
    StreamHandle,
    # Events
    TextDelta,
    TextPart,
    Thinking,
    ThinkingPart,
    ToolCall,
    ToolHandler,
    ToolOutput,
    ToolResult,
    ToolResultPart,
    ToolSpec,
    ToolUsePart,
)


__version__ = "0.1.0"

__all__ = [
    # Version
    "__version__",
    # Errors
    "AgentSDKError",
    "BackendNotImplementedError",
    "CLIError",
    "StreamNotConsumedError",
    "ToolApprovalDenied",
    # Factory
    "get_backend",
    "list_backends",
    "register_backend",
    # Retry
    "run_with_retry",
    # Backend protocol
    "AgentBackend",
    # Config
    "AgentConfig",
    # Content
    "ContentPart",
    "InputMessage",
    "NormalizedMessage",
    "Role",
    "TextPart",
    "ThinkingPart",
    "ToolResultPart",
    "ToolUsePart",
    # Tools
    "BuiltinTool",
    "ToolHandler",
    "ToolOutput",
    "ToolSpec",
    # Approval
    "ApprovalCallback",
    "ApprovalDecision",
    "ApprovalRequest",
    # Events
    "AgentChanged",
    "ErrorEvent",
    "Event",
    "MessageComplete",
    "RunDone",
    "RunStart",
    "StopReason",
    "TextDelta",
    "Thinking",
    "ToolCall",
    "ToolResult",
    # Result, session, stream
    "RunResult",
    "Session",
    "StreamHandle",
]

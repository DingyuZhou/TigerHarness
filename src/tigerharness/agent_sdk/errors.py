"""Exceptions raised by the agent SDK."""

from __future__ import annotations


class AgentSDKError(Exception):
    """Base class for all agent SDK errors."""


class BackendNotImplementedError(AgentSDKError, NotImplementedError):
    """Raised when a backend doesn't support a requested feature."""


class StreamNotConsumedError(AgentSDKError):
    """Raised when `.result` is read on a stream handle before the stream
    has been fully iterated.
    """


class ToolApprovalDenied(AgentSDKError):
    """Raised when a tool call is denied by the approval callback and the
    backend signals a terminal denial.
    """


class CLIError(AgentSDKError):
    """Raised when the underlying CLI subprocess fails."""

    def __init__(
        self,
        message: str,
        *,
        returncode: int | None = None,
        stderr: str = "",
    ):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr

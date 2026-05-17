"""Stub backend that will use the ``openai-agents`` Python package.

Not yet implemented. To flesh this out:

1. ``pip install openai-agents``
2. Translate ``AgentConfig`` to ``Agent`` (instructions, model, tools wrapped
   via ``@function_tool``, builtin_tools mapped through a small registry to
   ``WebSearchTool`` / ``FileSearchTool`` / ``CodeInterpreterTool`` / ...).
3. Drive the run with ``Runner.run_streamed(agent, input, session=...)``.
4. Translate ``ApprovalCallback`` by wrapping the runner in a loop that
   inspects ``result.interruptions``, calls the user's callback for each
   pending ``ToolApprovalItem``, applies decisions to ``result.to_state()``,
   and resumes — until there are no more interruptions.
5. Map yielded ``RunItemStreamEvent`` / ``RawResponsesStreamEvent`` /
   ``AgentUpdatedStreamEvent`` to our ``Event`` types — the design doc has
   a working ``_to_events`` sketch.
"""

from __future__ import annotations

from typing import Any

from ..errors import BackendNotImplementedError


class OpenAISDKBackend:
    """Future backend over the ``openai-agents`` Python package."""

    def __init__(self, **kwargs: Any) -> None:
        raise BackendNotImplementedError(
            "openai_sdk backend is not yet implemented. "
            "Use get_backend('claude_p') for now, or contribute the impl "
            "(see comparison.md for the wiring sketch)."
        )

    async def run(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise BackendNotImplementedError("openai_sdk backend is not yet implemented.")

    def run_stream(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise BackendNotImplementedError("openai_sdk backend is not yet implemented.")

    async def open_session(self, **kwargs: Any) -> Any:  # pragma: no cover
        raise BackendNotImplementedError("openai_sdk backend is not yet implemented.")

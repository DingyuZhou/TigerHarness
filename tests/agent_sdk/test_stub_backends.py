"""Tests for the openai_sdk stub backend.

(The anthropic_sdk backend is no longer a stub — it now uses
``claude-agent-sdk`` for real. See test_anthropic_sdk.py.)
"""

from __future__ import annotations

import pytest

from tigerharness.agent_sdk import BackendNotImplementedError
from tigerharness.agent_sdk.backends.openai_sdk import OpenAISDKBackend


class TestOpenAISDKStub:
    def test_construction_raises(self) -> None:
        with pytest.raises(BackendNotImplementedError, match="openai_sdk"):
            OpenAISDKBackend()

    def test_construction_with_kwargs_raises(self) -> None:
        with pytest.raises(BackendNotImplementedError):
            OpenAISDKBackend(some_kw="value")


# The unreachable methods on the stub (run/run_stream/open_session) exist so
# the class structurally satisfies AgentBackend before construction blows up.
# Their bodies are guarded with ``# pragma: no cover`` and would only be
# reached if a future implementation accidentally reverted the constructor
# guard.

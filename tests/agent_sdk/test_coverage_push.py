"""Coverage-push tests for agent_sdk gaps.

Covers:
- backends/_base.py:49->exit (aclose branch when _gen is None or has no aclose)
- backends/anthropic_sdk.py: multiple branches (218->217, 267->269, 278->280,
  310->321, 319-320, 518->520, 531->492, 535)
- retry.py:124-125 (defensive unreachable code)
"""
from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tigerharness.agent_sdk import AgentConfig
from tigerharness.agent_sdk.types import (
    InputMessage,
    MessageComplete,
    NormalizedMessage,
    RunResult,
    TextPart,
    Thinking,
    ToolCall,
    ToolOutput,
    ToolResult,
    ToolResultPart,
)

from tests.agent_sdk._helpers import asyncio_test


# ---------------------------------------------------------------------------
# _base.py: 49->exit — _gen is None or no aclose attribute
# ---------------------------------------------------------------------------

class TestBaseStreamHandleAclose:
    """Cover the branch where _gen is None when __aexit__ runs."""

    @asyncio_test
    async def test_aexit_when_gen_is_none_and_complete(self):
        """_gen is None, is_complete → skip cancel and aclose."""
        from tigerharness.agent_sdk.backends._base import BaseStreamHandle
        from tigerharness.agent_sdk.types import RunResult

        handle = BaseStreamHandle()
        handle._gen = None
        # Mark as complete so cancel is skipped
        handle._result = RunResult(
            final_output="done", transcript=[], stop_reason="end_turn",
            usage=None, cost_usd=0.0, raw=None,
        )
        await handle.__aexit__(None, None, None)

    @asyncio_test
    async def test_aexit_when_not_complete_gen_is_none(self):
        """Not complete, _gen is None → cancel raises NotImplementedError, aclose skipped."""
        from tigerharness.agent_sdk.backends._base import BaseStreamHandle

        handle = BaseStreamHandle()
        handle._gen = None
        # _result is None → is_complete is False → cancel() called → raises NotImplementedError
        await handle.__aexit__(None, None, None)

    @asyncio_test
    async def test_aexit_when_gen_has_no_aclose(self):
        """_gen exists but has no aclose → skip aclose."""
        from tigerharness.agent_sdk.backends._base import BaseStreamHandle
        from tigerharness.agent_sdk.types import RunResult

        handle = BaseStreamHandle()
        handle._gen = "not-an-async-gen"  # type: ignore[assignment]
        handle._result = RunResult(
            final_output="done", transcript=[], stop_reason="end_turn",
            usage=None, cost_usd=0.0, raw=None,
        )
        await handle.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# Fake SDK setup — reused from test_anthropic_sdk.py patterns
# ---------------------------------------------------------------------------

@dataclass
class _TextBlock:
    text: str

@dataclass
class _ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]

@dataclass
class _ToolResultBlock:
    tool_use_id: str
    content: Any
    is_error: bool = False

@dataclass
class _ThinkingBlock:
    thinking: str

@dataclass
class _AssistantMessage:
    content: list[Any] = field(default_factory=list)
    model: str | None = None
    parent_tool_use_id: str | None = None
    error: Any = None
    usage: dict[str, Any] | None = None
    message_id: str | None = None
    stop_reason: str | None = None
    session_id: str | None = None
    uuid: str | None = None

@dataclass
class _UserMessage:
    content: list[Any] | str = field(default_factory=list)
    uuid: str | None = None
    parent_tool_use_id: str | None = None
    tool_use_result: Any = None

@dataclass
class _SystemMessage:
    subtype: str
    data: dict[str, Any] | None = None

@dataclass
class _ResultMessage:
    subtype: str = "success"
    duration_ms: int = 0
    duration_api_ms: int = 0
    is_error: bool = False
    num_turns: int = 1
    session_id: str | None = None
    stop_reason: str | None = "end_turn"
    total_cost_usd: float | None = None

@dataclass
class _PermissionResultAllow:
    behavior: str = "allow"
    updated_input: dict[str, Any] | None = None
    updated_permissions: Any = None

@dataclass
class _PermissionResultDeny:
    behavior: str = "deny"
    message: str = ""
    interrupt: bool = False

@dataclass
class _ClaudeAgentOptions:
    def __init__(self, **kwargs):
        known = {
            "system_prompt", "model", "allowed_tools", "disallowed_tools",
            "max_turns", "permission_mode", "cwd", "resume", "add_dirs",
            "max_budget_usd", "can_use_tool",
        }
        for k in known:
            setattr(self, k, kwargs.pop(k, None))
        self.extra_kwargs = kwargs


class _FakeClient:
    def __init__(self, *, options=None):
        self.options = options
        self.queries = []
        self.connected = False
        self.disconnected = False
        self.interrupted = False
        self.replay = []
        self.connect_error = None
        self.query_error = None
        self.disconnect_error = None

    async def connect(self):
        if self.connect_error:
            raise self.connect_error
        self.connected = True

    async def disconnect(self):
        if self.disconnect_error:
            raise self.disconnect_error
        self.disconnected = True

    async def interrupt(self):
        self.interrupted = True

    async def query(self, prompt, session_id=None):
        if self.query_error:
            raise self.query_error
        self.queries.append(prompt)

    async def receive_response(self):
        for msg in self.replay:
            yield msg


def _build_fake_sdk(client=None):
    fake = types.ModuleType("claude_agent_sdk")
    fake.TextBlock = _TextBlock
    fake.ToolUseBlock = _ToolUseBlock
    fake.ToolResultBlock = _ToolResultBlock
    fake.AssistantMessage = _AssistantMessage
    fake.UserMessage = _UserMessage
    fake.SystemMessage = _SystemMessage
    fake.ResultMessage = _ResultMessage
    fake.PermissionResultAllow = _PermissionResultAllow
    fake.PermissionResultDeny = _PermissionResultDeny
    fake.ClaudeAgentOptions = _ClaudeAgentOptions

    def _client_factory(options=None, **_):
        if client is not None:
            client.options = options
            return client
        return _FakeClient(options=options)

    fake.ClaudeSDKClient = _client_factory
    return fake


@pytest.fixture
def fake_sdk(monkeypatch):
    client = _FakeClient()
    sdk = _build_fake_sdk(client)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    return sdk, client


# ---------------------------------------------------------------------------
# anthropic_sdk.py: _extract_user_text — 218->217 (TextPart branch in loop)
# ---------------------------------------------------------------------------

class TestNormalizePrompt:
    def test_extracts_text_from_content_list_with_text_parts(self, fake_sdk):
        from tigerharness.agent_sdk.backends.anthropic_sdk import _normalize_prompt
        msgs = [
            InputMessage(role="user", content=[
                TextPart(text="hello"),
                TextPart(text="world"),
            ]),
        ]
        result = _normalize_prompt(msgs)
        assert "hello" in result
        assert "world" in result

    def test_skips_non_text_parts(self, fake_sdk):
        """Line 218 — isinstance check is False for non-TextPart items."""
        from tigerharness.agent_sdk.backends.anthropic_sdk import _normalize_prompt

        # Create a non-TextPart content item
        non_text = MagicMock()
        non_text.__class__ = type("SomethingElse", (), {})
        msgs = [
            InputMessage(role="user", content=[non_text]),
        ]
        result = _normalize_prompt(msgs)
        assert result == ""

    def test_skips_non_user_messages(self, fake_sdk):
        from tigerharness.agent_sdk.backends.anthropic_sdk import _normalize_prompt
        msgs = [
            InputMessage(role="assistant", content="hi"),
        ]
        result = _normalize_prompt(msgs)
        assert result == ""


# ---------------------------------------------------------------------------
# anthropic_sdk.py: _translate_block — ToolUseBlock with tool_names dict
# Lines 267->269 (tool_names is not None), 278->280 (tool_names is not None)
# ---------------------------------------------------------------------------

class TestTranslateBlock:
    def test_tool_use_block_records_name_in_tool_names(self, fake_sdk):
        """Cover 267->269: tool_names is not None with ToolUseBlock."""
        sdk_mod, _ = fake_sdk
        from tigerharness.agent_sdk.backends.anthropic_sdk import _translate_block
        tool_names: dict[str, str] = {}
        block = _ToolUseBlock(id="tu_1", name="Bash", input={"cmd": "ls"})
        ev = _translate_block(sdk_mod, block, tool_names)
        assert isinstance(ev, ToolCall)
        assert tool_names["tu_1"] == "Bash"

    def test_tool_use_block_without_tool_names(self, fake_sdk):
        """Cover 267 when tool_names is None."""
        sdk_mod, _ = fake_sdk
        from tigerharness.agent_sdk.backends.anthropic_sdk import _translate_block
        block = _ToolUseBlock(id="tu_1", name="Bash", input={"cmd": "ls"})
        ev = _translate_block(sdk_mod, block, None)
        assert isinstance(ev, ToolCall)
        assert ev.name == "Bash"

    def test_tool_result_block_uses_tool_names_for_name(self, fake_sdk):
        """Cover 278->280: tool_names has entry for tool_use_id."""
        sdk_mod, _ = fake_sdk
        from tigerharness.agent_sdk.backends.anthropic_sdk import _translate_block
        tool_names = {"tu_1": "Read"}
        block = _ToolResultBlock(tool_use_id="tu_1", content="data")
        ev = _translate_block(sdk_mod, block, tool_names)
        assert isinstance(ev, ToolResult)
        assert ev.name == "Read"

    def test_tool_result_block_without_tool_names(self, fake_sdk):
        """Cover 278 when tool_names is None."""
        sdk_mod, _ = fake_sdk
        from tigerharness.agent_sdk.backends.anthropic_sdk import _translate_block
        block = _ToolResultBlock(tool_use_id="tu_1", content="data")
        ev = _translate_block(sdk_mod, block, None)
        assert isinstance(ev, ToolResult)
        assert ev.name == ""

    def test_tool_result_block_with_missing_tool_id(self, fake_sdk):
        """Cover 278->280: tool_names doesn't have the id → defaults to ""."""
        sdk_mod, _ = fake_sdk
        from tigerharness.agent_sdk.backends.anthropic_sdk import _translate_block
        tool_names = {"other_id": "Bash"}
        block = _ToolResultBlock(tool_use_id="tu_1", content="data")
        ev = _translate_block(sdk_mod, block, tool_names)
        assert isinstance(ev, ToolResult)
        assert ev.name == ""


# ---------------------------------------------------------------------------
# anthropic_sdk.py: _to_normalized_message — UserMessage with list content
# Lines 310->321, 319-320 (TextBlock in UserMessage content list)
# ---------------------------------------------------------------------------

class TestToNormalizedMessage:
    def test_user_message_with_list_content_text_block(self, fake_sdk):
        """Cover 319-320: UserMessage content list with TextBlock."""
        sdk_mod, _ = fake_sdk
        from tigerharness.agent_sdk.backends.anthropic_sdk import _to_normalized_message
        msg = _UserMessage(content=[_TextBlock(text="hello user")])
        nm = _to_normalized_message(sdk_mod, msg)
        assert nm is not None
        assert nm.role == "user"
        assert len(nm.content) == 1
        assert isinstance(nm.content[0], TextPart)
        assert nm.content[0].text == "hello user"

    def test_user_message_with_list_tool_result_and_text(self, fake_sdk):
        """Cover 310->321: UserMessage with ToolResultBlock + TextBlock."""
        sdk_mod, _ = fake_sdk
        from tigerharness.agent_sdk.backends.anthropic_sdk import _to_normalized_message
        msg = _UserMessage(content=[
            _ToolResultBlock(tool_use_id="tu_1", content="output", is_error=True),
            _TextBlock(text="follow-up"),
        ])
        nm = _to_normalized_message(sdk_mod, msg)
        assert nm is not None
        assert nm.role == "user"
        assert len(nm.content) == 2
        assert isinstance(nm.content[0], ToolResultPart)
        assert nm.content[0].is_error is True
        assert isinstance(nm.content[1], TextPart)

    def test_user_message_with_str_content(self, fake_sdk):
        """UserMessage with plain string content."""
        sdk_mod, _ = fake_sdk
        from tigerharness.agent_sdk.backends.anthropic_sdk import _to_normalized_message
        msg = _UserMessage(content="just text")
        nm = _to_normalized_message(sdk_mod, msg)
        assert nm is not None
        assert nm.role == "user"
        assert len(nm.content) == 1
        assert isinstance(nm.content[0], TextPart)

    def test_user_message_with_empty_list(self, fake_sdk):
        """Cover 310->321: empty list → no parts, but still returns NormalizedMessage."""
        sdk_mod, _ = fake_sdk
        from tigerharness.agent_sdk.backends.anthropic_sdk import _to_normalized_message
        msg = _UserMessage(content=[])
        nm = _to_normalized_message(sdk_mod, msg)
        assert nm is not None
        assert nm.content == []


# ---------------------------------------------------------------------------
# anthropic_sdk.py: run_stream — nm is None (518->520), session_id (535),
# ResultMessage path (531->492)
# ---------------------------------------------------------------------------

class TestRunStreamCoverage:
    @asyncio_test
    async def test_normalized_message_is_none_skipped(self, fake_sdk):
        """Cover 518->520: _to_normalized_message returns None."""
        sdk_mod, client = fake_sdk
        from tigerharness.agent_sdk.backends.anthropic_sdk import AnthropicSDKBackend

        # SystemMessage is not mapped → returns None from _to_normalized_message
        client.replay = [
            _SystemMessage(subtype="init"),
            _AssistantMessage(content=[_TextBlock(text="hi")]),
            _ResultMessage(session_id="sess-1"),
        ]

        backend = AnthropicSDKBackend()
        cfg = AgentConfig(name="test")
        session = await backend.open_session()

        events = []
        async with backend.run_stream(cfg, "hello", session=session) as handle:
            async for ev in handle:
                events.append(ev)

        # Should have text event but no events from SystemMessage
        text_events = [e for e in events if isinstance(e, MessageComplete)]
        assert len(text_events) == 1

    @asyncio_test
    async def test_session_id_set_from_result_message(self, fake_sdk):
        """Cover 535: sess._id set from msg.session_id when sess exists."""
        sdk_mod, client = fake_sdk
        from tigerharness.agent_sdk.backends.anthropic_sdk import AnthropicSDKBackend

        client.replay = [
            _AssistantMessage(content=[_TextBlock(text="response")]),
            _ResultMessage(session_id="new-sess-id"),
        ]

        backend = AnthropicSDKBackend()
        cfg = AgentConfig(name="test")
        session = await backend.open_session()

        async with backend.run_stream(cfg, "hello", session=session) as handle:
            async for _ in handle:
                pass

        result = handle.result
        assert session.id == "new-sess-id"


# ---------------------------------------------------------------------------
# retry.py:124-125 — defensive unreachable code
# These lines are genuinely unreachable. Mark with pragma: no cover.
# ---------------------------------------------------------------------------
# The retry loop always either:
#   - returns (on success)
#   - raises (on final failure or CancelledError)
# Lines 124-125 can never execute. We'll document this rather than
# trying to cover unreachable defensive code.

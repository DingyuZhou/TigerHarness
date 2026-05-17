"""Tests for ``tigerharness.agent_sdk.backends.anthropic_sdk``.

We don't talk to a real ``claude`` CLI or the Anthropic API. Instead we
inject a fake ``claude_agent_sdk`` module that records the options we
build for it and replays canned message sequences back, so we can
verify the full event-translation pipeline end-to-end without external
state.
"""

from __future__ import annotations

import asyncio
import sys
import types
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from tigerharness.agent_sdk import (
    AgentConfig,
    ApprovalDecision,
    ApprovalRequest,
    BackendNotImplementedError,
    BuiltinTool,
    InputMessage,
    MessageComplete,
    RunDone,
    RunStart,
    TextPart,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from tigerharness.agent_sdk.types import RunResult


# ---------------------------------------------------------------------------
# Fake claude_agent_sdk module
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
    thinking: str  # duck-typed: anthropic_sdk introspects via getattr


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
    """Captures every kwarg we pass so tests can assert on translation."""
    system_prompt: str | None = None
    model: str | None = None
    allowed_tools: list[str] | None = None
    disallowed_tools: list[str] | None = None
    max_turns: int | None = None
    permission_mode: str | None = None
    cwd: str | None = None
    resume: str | None = None
    add_dirs: list[str] | None = None
    max_budget_usd: float | None = None
    can_use_tool: Any = None
    # Catch-all so unknown kwargs don't blow up.
    extra_kwargs: dict[str, Any] = field(default_factory=dict)

    def __init__(self, **kwargs):
        # We can't use the auto-generated dataclass __init__ because it'd
        # reject unknown kwargs. Use a permissive constructor.
        known = {
            "system_prompt", "model", "allowed_tools", "disallowed_tools",
            "max_turns", "permission_mode", "cwd", "resume", "add_dirs",
            "max_budget_usd", "can_use_tool",
        }
        for k in known:
            setattr(self, k, kwargs.pop(k, None))
        self.extra_kwargs = kwargs


class _FakeClient:
    """Recorder + replay for ``ClaudeSDKClient``.

    Tests preload ``replay`` with the message sequence the SDK should emit
    on ``receive_response()``. The client also records every call for
    later assertion.
    """

    def __init__(self, *, options: Any = None) -> None:
        self.options = options
        self.queries: list[str] = []
        self.connected: bool = False
        self.disconnected: bool = False
        self.interrupted: bool = False
        # Either preload via ``set_replay`` or by mutating ``replay`` directly.
        self.replay: list[Any] = []
        # If set, ``connect`` raises this instead.
        self.connect_error: Exception | None = None
        # If set, ``query`` raises this.
        self.query_error: Exception | None = None
        # If set, ``disconnect`` raises this.
        self.disconnect_error: Exception | None = None

    async def connect(self) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    async def disconnect(self) -> None:
        if self.disconnect_error is not None:
            raise self.disconnect_error
        self.disconnected = True

    async def interrupt(self) -> None:
        self.interrupted = True

    async def query(self, prompt: str, session_id: str | None = None) -> None:
        if self.query_error is not None:
            raise self.query_error
        self.queries.append(prompt)

    async def receive_response(self) -> AsyncIterator[Any]:
        for msg in self.replay:
            yield msg


def _build_fake_sdk(client: _FakeClient | None = None) -> types.ModuleType:
    """Construct a fake ``claude_agent_sdk`` module suitable for monkeypatch."""
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

    # Make ClaudeSDKClient return the preset client (so tests can pre-stage
    # the replay) — or a fresh one if no client is provided.
    def _client_factory(options: Any = None, **_) -> _FakeClient:
        if client is not None:
            client.options = options
            return client
        return _FakeClient(options=options)

    fake.ClaudeSDKClient = _client_factory
    return fake


@pytest.fixture
def fake_sdk(monkeypatch: pytest.MonkeyPatch):
    """Inject a fresh fake claude_agent_sdk into sys.modules.

    The fixture returns a (sdk_module, client) tuple so tests can both
    preload the replay AND assert on the recorder afterwards.
    """
    client = _FakeClient()
    sdk = _build_fake_sdk(client)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    return sdk, client


# ---------------------------------------------------------------------------
# Tests: construction + missing-dep error
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_raises_clear_error_when_sdk_missing(self, monkeypatch):
        # Force ImportError on `import claude_agent_sdk`.
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
        from tigerharness.agent_sdk.backends.anthropic_sdk import (
            AnthropicSDKBackend,
        )
        with pytest.raises(BackendNotImplementedError) as exc:
            AnthropicSDKBackend()
        assert "claude-agent-sdk" in str(exc.value)
        assert "tigerharness[anthropic]" in str(exc.value)

    def test_constructs_when_sdk_available(self, fake_sdk):
        from tigerharness.agent_sdk.backends.anthropic_sdk import (
            AnthropicSDKBackend,
        )
        backend = AnthropicSDKBackend()
        assert backend is not None

    def test_factory_returns_anthropic_backend(self, fake_sdk):
        from tigerharness.agent_sdk import get_backend
        backend = get_backend("anthropic_sdk")
        assert type(backend).__name__ == "AnthropicSDKBackend"


# ---------------------------------------------------------------------------
# Tests: config translation
# ---------------------------------------------------------------------------

class TestConfigTranslation:
    def test_basic_fields_map(self, fake_sdk):
        from tigerharness.agent_sdk.backends.anthropic_sdk import _build_options
        sdk, _ = fake_sdk
        cfg = AgentConfig(
            name="qa",
            instructions="Be concise.",
            model="claude-sonnet-4",
            max_turns=5,
        )
        opts = _build_options(sdk, cfg)
        assert opts.system_prompt == "Be concise."
        assert opts.model == "claude-sonnet-4"
        assert opts.max_turns == 5

    def test_builtin_tools_become_allowed_tools(self, fake_sdk):
        from tigerharness.agent_sdk.backends.anthropic_sdk import _build_options
        sdk, _ = fake_sdk
        cfg = AgentConfig(
            name="x",
            builtin_tools=[BuiltinTool("Bash"), BuiltinTool("Read")],
        )
        opts = _build_options(sdk, cfg)
        assert opts.allowed_tools == ["Bash", "Read"]

    def test_extra_overrides_builtin_allowed_tools(self, fake_sdk):
        """If the caller sets allowed_tools in extra, it wins over the
        builtin_tools list."""
        from tigerharness.agent_sdk.backends.anthropic_sdk import _build_options
        sdk, _ = fake_sdk
        cfg = AgentConfig(
            name="x",
            builtin_tools=[BuiltinTool("Bash")],
            extra={"allowed_tools": ["Read"]},
        )
        opts = _build_options(sdk, cfg)
        assert opts.allowed_tools == ["Read"]

    def test_known_extras_pass_through(self, fake_sdk):
        from tigerharness.agent_sdk.backends.anthropic_sdk import _build_options
        sdk, _ = fake_sdk
        cfg = AgentConfig(
            name="x",
            extra={
                "permission_mode": "plan",
                "cwd": "/proj",
                "disallowed_tools": ["Bash(rm:*)"],
                "add_dirs": ["/a", "/b"],
                "max_budget_usd": 1.50,
            },
        )
        opts = _build_options(sdk, cfg)
        assert opts.permission_mode == "plan"
        assert opts.cwd == "/proj"
        assert opts.disallowed_tools == ["Bash(rm:*)"]
        assert opts.add_dirs == ["/a", "/b"]
        assert opts.max_budget_usd == 1.50

    def test_unknown_extras_are_dropped(self, fake_sdk):
        """Keys not in _PASSTHROUGH_EXTRA_KEYS shouldn't appear in options."""
        from tigerharness.agent_sdk.backends.anthropic_sdk import _build_options
        sdk, _ = fake_sdk
        cfg = AgentConfig(
            name="x",
            extra={"bogus_key": "ignored"},
        )
        opts = _build_options(sdk, cfg)
        assert "bogus_key" not in opts.extra_kwargs

    def test_resume_id_threads_through(self, fake_sdk):
        from tigerharness.agent_sdk.backends.anthropic_sdk import _build_options
        sdk, _ = fake_sdk
        cfg = AgentConfig(name="x")
        opts = _build_options(sdk, cfg, session_id="sess-resume-abc")
        assert opts.resume == "sess-resume-abc"

    def test_custom_function_tools_raise(self, fake_sdk):
        """Custom ToolSpec isn't translated yet -- raise instead of
        silently dropping."""
        from tigerharness.agent_sdk.backends.anthropic_sdk import _build_options
        sdk, _ = fake_sdk
        async def _handler(args):
            return "x"
        cfg = AgentConfig(
            name="x",
            tools=[ToolSpec("foo", "desc", {}, _handler)],
        )
        with pytest.raises(BackendNotImplementedError, match="ToolSpec"):
            _build_options(sdk, cfg)

    @pytest.mark.asyncio
    async def test_approval_callback_wired_through(self, fake_sdk):
        from tigerharness.agent_sdk.backends.anthropic_sdk import _build_options
        sdk, _ = fake_sdk

        async def approver(req: ApprovalRequest) -> ApprovalDecision:
            return ApprovalDecision(allow=True, updated_input={"safe": True})

        cfg = AgentConfig(name="x")
        opts = _build_options(sdk, cfg, approval=approver)
        assert opts.can_use_tool is not None

        # Invoke the wrapper to verify the translation contract.
        class _Ctx:
            session_id = "sess-1"
            tool_use_id = "call-1"

        result = await opts.can_use_tool("Bash", {"command": "ls"}, _Ctx())
        assert isinstance(result, _PermissionResultAllow)
        assert result.updated_input == {"safe": True}

    @pytest.mark.asyncio
    async def test_approval_deny_path(self, fake_sdk):
        from tigerharness.agent_sdk.backends.anthropic_sdk import _build_options
        sdk, _ = fake_sdk

        async def denier(req: ApprovalRequest) -> ApprovalDecision:
            return ApprovalDecision(allow=False, reason="no thanks")

        opts = _build_options(sdk, AgentConfig(name="x"), approval=denier)

        class _Ctx:
            session_id = "sess-1"
            tool_use_id = ""

        result = await opts.can_use_tool("Bash", {"command": "rm -rf /"}, _Ctx())
        assert isinstance(result, _PermissionResultDeny)
        assert "no thanks" in result.message

    @pytest.mark.asyncio
    async def test_approval_deny_default_reason(self, fake_sdk):
        from tigerharness.agent_sdk.backends.anthropic_sdk import _build_options
        sdk, _ = fake_sdk

        async def denier(req: ApprovalRequest) -> ApprovalDecision:
            return ApprovalDecision(allow=False)  # no reason

        opts = _build_options(sdk, AgentConfig(name="x"), approval=denier)
        result = await opts.can_use_tool("X", {}, None)
        assert "denied by approval callback" in result.message


# ---------------------------------------------------------------------------
# Tests: prompt normalization
# ---------------------------------------------------------------------------

class TestPromptNormalization:
    def test_string_passes_through(self, fake_sdk):
        from tigerharness.agent_sdk.backends.anthropic_sdk import (
            _normalize_prompt,
        )
        assert _normalize_prompt("hello") == "hello"

    def test_input_message_list_concatenates_user_text(self, fake_sdk):
        from tigerharness.agent_sdk.backends.anthropic_sdk import (
            _normalize_prompt,
        )
        messages = [
            InputMessage(role="user", content="first"),
            InputMessage(role="user", content=[TextPart("second")]),
            InputMessage(role="assistant", content="ignored"),
        ]
        out = _normalize_prompt(messages)
        assert "first" in out
        assert "second" in out
        assert "ignored" not in out


# ---------------------------------------------------------------------------
# Tests: end-to-end run() with mocked SDK
# ---------------------------------------------------------------------------

class TestRunEndToEnd:
    @pytest.fixture
    def backend(self, fake_sdk):
        from tigerharness.agent_sdk.backends.anthropic_sdk import (
            AnthropicSDKBackend,
        )
        return AnthropicSDKBackend()

    @pytest.mark.asyncio
    async def test_simple_success(self, fake_sdk, backend):
        _, client = fake_sdk
        client.replay = [
            _SystemMessage(subtype="init", data={
                "session_id": "sess-1", "model": "claude-sonnet-4",
            }),
            _AssistantMessage(content=[_TextBlock("Hello there.")]),
            _ResultMessage(
                subtype="success", session_id="sess-1",
                stop_reason="end_turn", total_cost_usd=0.001,
            ),
        ]
        result = await backend.run(
            AgentConfig(name="qa", instructions="Be concise."),
            "What is 2+2?",
        )
        assert result.final_output == "Hello there."
        assert result.stop_reason == "end_turn"
        assert result.cost_usd == 0.001
        assert len(result.transcript) == 1
        assert client.queries == ["What is 2+2?"]
        assert client.connected and client.disconnected

    @pytest.mark.asyncio
    async def test_tool_roundtrip(self, fake_sdk, backend):
        _, client = fake_sdk
        client.replay = [
            _SystemMessage(subtype="init", data={"session_id": "s", "model": "m"}),
            _AssistantMessage(content=[
                _ToolUseBlock(id="call_1", name="Bash", input={"command": "ls"}),
            ]),
            _UserMessage(content=[
                _ToolResultBlock(
                    tool_use_id="call_1", content="a.py\nb.py", is_error=False,
                ),
            ]),
            _AssistantMessage(content=[_TextBlock("Two python files.")]),
            _ResultMessage(
                subtype="success", session_id="s",
                stop_reason="end_turn", total_cost_usd=0.005,
            ),
        ]
        result = await backend.run(AgentConfig(name="x"), "list files")
        assert result.final_output == "Two python files."
        # Transcript has assistant turn + user tool-result + assistant final.
        assert len(result.transcript) == 3

    @pytest.mark.asyncio
    async def test_streaming_events_in_order(self, fake_sdk, backend):
        _, client = fake_sdk
        client.replay = [
            _SystemMessage(subtype="init", data={"session_id": "s", "model": "m"}),
            _AssistantMessage(content=[
                _ToolUseBlock(id="c1", name="Bash", input={"command": "x"}),
            ]),
            _UserMessage(content=[
                _ToolResultBlock(tool_use_id="c1", content="ok"),
            ]),
            _AssistantMessage(content=[_TextBlock("done")]),
            _ResultMessage(
                subtype="success", session_id="s", stop_reason="end_turn",
            ),
        ]
        handle = backend.run_stream(AgentConfig(name="x"), "go")
        events = []
        async for ev in handle:
            events.append(type(ev).__name__)
        assert events == [
            "RunStart", "ToolCall", "ToolResult",
            "MessageComplete", "RunDone",
        ]

    @pytest.mark.asyncio
    async def test_thinking_block_emitted(self, fake_sdk, backend):
        _, client = fake_sdk
        client.replay = [
            _SystemMessage(subtype="init", data={"session_id": "s", "model": "m"}),
            _AssistantMessage(content=[
                _ThinkingBlock(thinking="hmm let me think"),
                _TextBlock("ok"),
            ]),
            _ResultMessage(subtype="success", session_id="s",
                           stop_reason="end_turn"),
        ]
        handle = backend.run_stream(AgentConfig(name="x"), "go")
        events = [type(ev).__name__ async for ev in handle]
        assert "Thinking" in events
        assert "MessageComplete" in events

    @pytest.mark.asyncio
    async def test_session_resume_populates_id(self, fake_sdk, backend):
        _, client = fake_sdk
        client.replay = [
            _SystemMessage(subtype="init", data={
                "session_id": "sess-new", "model": "m",
            }),
            _AssistantMessage(content=[_TextBlock("hi")]),
            _ResultMessage(subtype="success", session_id="sess-new",
                           stop_reason="end_turn"),
        ]
        session = await backend.open_session()
        assert session.id == ""
        await backend.run(AgentConfig(name="x"), "hi", session=session)
        assert session.id == "sess-new"

    @pytest.mark.asyncio
    async def test_session_reuses_client_across_calls(self, fake_sdk, backend):
        sdk, client = fake_sdk
        # Two runs share the session.
        common_result = _ResultMessage(
            subtype="success", session_id="s", stop_reason="end_turn",
        )
        session = await backend.open_session(resume_id="existing")

        # First call.
        client.replay = [
            _SystemMessage(subtype="init", data={"session_id": "existing", "model": "m"}),
            _AssistantMessage(content=[_TextBlock("a")]),
            common_result,
        ]
        await backend.run(AgentConfig(name="x"), "q1", session=session)
        first_disconnected = client.disconnected
        # Second call.
        client.replay = [
            _AssistantMessage(content=[_TextBlock("b")]),
            common_result,
        ]
        await backend.run(AgentConfig(name="x"), "q2", session=session)

        # The session held the client open across both calls. The first run
        # should NOT have disconnected (it's session-owned).
        assert not first_disconnected
        assert len(client.queries) == 2

        # Closing the session disconnects.
        await session.close()
        assert client.disconnected

    @pytest.mark.asyncio
    async def test_rejects_foreign_session(self, fake_sdk, backend):
        """Sessions are not portable: passing a non-_AnthropicSession raises."""

        class _ForeignSession:
            @property
            def id(self) -> str:
                return ""
            async def close(self) -> None:
                pass

        with pytest.raises(TypeError, match="sessions it created"):
            handle = backend.run_stream(
                AgentConfig(name="x"), "go", session=_ForeignSession(),
            )
            async for _ in handle:
                pass

    @pytest.mark.asyncio
    async def test_max_turns_stop_reason(self, fake_sdk, backend):
        _, client = fake_sdk
        client.replay = [
            _SystemMessage(subtype="init", data={"session_id": "s", "model": "m"}),
            _ResultMessage(subtype="error_max_turns", session_id="s",
                           stop_reason="max_turns"),
        ]
        result = await backend.run(AgentConfig(name="x"), "go")
        assert result.stop_reason == "max_turns"

    @pytest.mark.asyncio
    async def test_max_budget_stop_reason(self, fake_sdk, backend):
        _, client = fake_sdk
        client.replay = [
            _SystemMessage(subtype="init", data={"session_id": "s", "model": "m"}),
            _ResultMessage(subtype="error_max_budget_usd", session_id="s",
                           stop_reason=None),
        ]
        result = await backend.run(AgentConfig(name="x"), "go")
        assert result.stop_reason == "max_budget"

    @pytest.mark.asyncio
    async def test_cancel_calls_interrupt(self, fake_sdk, backend):
        _, client = fake_sdk
        # Don't include a ResultMessage so the stream stays open.
        client.replay = [
            _SystemMessage(subtype="init", data={"session_id": "s", "model": "m"}),
            _AssistantMessage(content=[_TextBlock("partial")]),
        ]
        handle = backend.run_stream(AgentConfig(name="x"), "go")
        # Drain to populate handle._client.
        events = []
        async for ev in handle:
            events.append(ev)
        # Now cancel.
        await handle.cancel()
        assert client.interrupted

    @pytest.mark.asyncio
    async def test_disconnect_failure_swallowed(self, fake_sdk, backend):
        _, client = fake_sdk
        client.disconnect_error = RuntimeError("network gone")
        client.replay = [
            _SystemMessage(subtype="init", data={"session_id": "s", "model": "m"}),
            _ResultMessage(subtype="success", session_id="s",
                           stop_reason="end_turn"),
        ]
        # Should NOT raise even though disconnect fails.
        result = await backend.run(AgentConfig(name="x"), "go")
        assert result.stop_reason == "end_turn"

    @pytest.mark.asyncio
    async def test_user_text_block_normalized(self, fake_sdk, backend):
        """A UserMessage with string content shouldn't crash transcript-building."""
        _, client = fake_sdk
        client.replay = [
            _SystemMessage(subtype="init", data={"session_id": "s", "model": "m"}),
            _UserMessage(content="some plain user note"),
            _ResultMessage(subtype="success", session_id="s",
                           stop_reason="end_turn"),
        ]
        result = await backend.run(AgentConfig(name="x"), "go")
        assert result.stop_reason == "end_turn"
        # Transcript should include the user-text message.
        user_msgs = [m for m in result.transcript if m.role == "user"]
        assert any(
            any(isinstance(p, TextPart) and "plain user note" in p.text
                for p in m.content)
            for m in user_msgs
        )

    @pytest.mark.asyncio
    async def test_session_close_idempotent(self, fake_sdk, backend):
        session = await backend.open_session()
        await session.close()  # no client yet -- should be a no-op
        await session.close()  # double close also fine


# ---------------------------------------------------------------------------
# Coverage: defensive branches
# ---------------------------------------------------------------------------

class TestDefensiveBranches:
    @pytest.mark.asyncio
    async def test_stream_without_result_message_falls_back_to_interrupted(
        self, fake_sdk,
    ):
        """If the SDK ends the stream without a ResultMessage, we still
        produce a RunResult (with stop_reason=interrupted) so callers
        don't get StreamNotConsumedError."""
        from tigerharness.agent_sdk.backends.anthropic_sdk import (
            AnthropicSDKBackend,
        )
        _, client = fake_sdk
        client.replay = [
            _SystemMessage(subtype="init", data={"session_id": "s", "model": "m"}),
            _AssistantMessage(content=[_TextBlock("partial")]),
            # No ResultMessage -- stream ends naturally.
        ]
        backend = AnthropicSDKBackend()
        result = await backend.run(AgentConfig(name="x"), "go")
        assert result.stop_reason == "interrupted"
        assert result.final_output == "partial"

    @pytest.mark.asyncio
    async def test_thinking_block_in_normalized_transcript(self, fake_sdk):
        """ThinkingPart should appear in NormalizedMessage.content when
        the AssistantMessage included a thinking block."""
        from tigerharness.agent_sdk.backends.anthropic_sdk import (
            AnthropicSDKBackend,
        )
        from tigerharness.agent_sdk.types import ThinkingPart
        _, client = fake_sdk
        client.replay = [
            _SystemMessage(subtype="init", data={"session_id": "s", "model": "m"}),
            _AssistantMessage(content=[_ThinkingBlock(thinking="reasoning")]),
            _ResultMessage(subtype="success", session_id="s",
                           stop_reason="end_turn"),
        ]
        backend = AnthropicSDKBackend()
        result = await backend.run(AgentConfig(name="x"), "go")
        thinking_parts = [
            p for m in result.transcript for p in m.content
            if isinstance(p, ThinkingPart)
        ]
        assert thinking_parts and thinking_parts[0].text == "reasoning"

    @pytest.mark.asyncio
    async def test_unknown_assistant_block_type_is_skipped(self, fake_sdk):
        """A content block that's neither TextBlock, ToolUseBlock,
        ToolResultBlock, nor thinking-shaped should yield no Event and
        not crash transcript-building."""
        from tigerharness.agent_sdk.backends.anthropic_sdk import (
            AnthropicSDKBackend,
        )
        @dataclass
        class _UnknownBlock:
            data: str = "wat"

        _, client = fake_sdk
        client.replay = [
            _SystemMessage(subtype="init", data={"session_id": "s", "model": "m"}),
            _AssistantMessage(content=[_UnknownBlock(), _TextBlock("ok")]),
            _ResultMessage(subtype="success", session_id="s",
                           stop_reason="end_turn"),
        ]
        backend = AnthropicSDKBackend()
        result = await backend.run(AgentConfig(name="x"), "go")
        assert result.final_output == "ok"

    @pytest.mark.asyncio
    async def test_user_message_tool_result_with_list_content(self, fake_sdk):
        """ToolResultBlock with list-typed content threads through cleanly."""
        from tigerharness.agent_sdk.backends.anthropic_sdk import (
            AnthropicSDKBackend,
        )
        _, client = fake_sdk
        client.replay = [
            _SystemMessage(subtype="init", data={"session_id": "s", "model": "m"}),
            _AssistantMessage(content=[
                _ToolUseBlock(id="c1", name="X", input={}),
            ]),
            _UserMessage(content=[
                _ToolResultBlock(
                    tool_use_id="c1",
                    content=[{"type": "text", "text": "structured"}],
                    is_error=False,
                ),
            ]),
            _ResultMessage(subtype="success", session_id="s",
                           stop_reason="end_turn"),
        ]
        backend = AnthropicSDKBackend()
        result = await backend.run(AgentConfig(name="x"), "go")
        # Find the tool-result event in the transcript.
        from tigerharness.agent_sdk.types import ToolResultPart
        results = [
            p for m in result.transcript for p in m.content
            if isinstance(p, ToolResultPart)
        ]
        assert results and isinstance(results[0].content, list)

    @pytest.mark.asyncio
    async def test_system_message_non_init_is_ignored(self, fake_sdk):
        """SystemMessage with subtype != 'init' should be a no-op."""
        from tigerharness.agent_sdk.backends.anthropic_sdk import (
            AnthropicSDKBackend,
        )
        _, client = fake_sdk
        client.replay = [
            _SystemMessage(subtype="other_subtype", data={"foo": "bar"}),
            _SystemMessage(subtype="init", data={"session_id": "s", "model": "m"}),
            _ResultMessage(subtype="success", session_id="s",
                           stop_reason="end_turn"),
        ]
        backend = AnthropicSDKBackend()
        handle = backend.run_stream(AgentConfig(name="x"), "go")
        events = [type(ev).__name__ async for ev in handle]
        # Exactly one RunStart from the init message; the 'other_subtype'
        # was skipped.
        assert events.count("RunStart") == 1

    def test_stop_reason_unknown_falls_back(self, fake_sdk):
        from tigerharness.agent_sdk.backends.anthropic_sdk import _stop_reason
        assert _stop_reason("totally_unknown_reason") == "end_turn"
        assert _stop_reason(None) == "end_turn"
        assert _stop_reason("error_some_obscure_thing") == "error"


# ---------------------------------------------------------------------------
# Regression: capture usage + populate ToolResult.name
# ---------------------------------------------------------------------------

class TestUsageAndToolName:
    @pytest.mark.asyncio
    async def test_usage_captured_from_assistant_message(self, fake_sdk):
        """The runner reads .usage off each AssistantMessage and threads
        it through to RunResult.usage. Pre-fix, RunResult.usage was
        always None."""
        from tigerharness.agent_sdk.backends.anthropic_sdk import (
            AnthropicSDKBackend,
        )
        _, client = fake_sdk
        client.replay = [
            _SystemMessage(subtype="init", data={"session_id": "s", "model": "m"}),
            _AssistantMessage(
                content=[_TextBlock("hi")],
                usage={"input_tokens": 12, "output_tokens": 5},
            ),
            _ResultMessage(subtype="success", session_id="s",
                           stop_reason="end_turn", total_cost_usd=0.001),
        ]
        backend = AnthropicSDKBackend()
        result = await backend.run(AgentConfig(name="x"), "hi")
        assert result.usage == {"input_tokens": 12, "output_tokens": 5}

    @pytest.mark.asyncio
    async def test_usage_uses_latest_non_empty_turn(self, fake_sdk):
        """When multiple assistant turns ship usage, the LAST one wins.
        Empty/None usage on a turn doesn't overwrite a real prior value."""
        from tigerharness.agent_sdk.backends.anthropic_sdk import (
            AnthropicSDKBackend,
        )
        _, client = fake_sdk
        client.replay = [
            _SystemMessage(subtype="init", data={"session_id": "s", "model": "m"}),
            _AssistantMessage(
                content=[_ToolUseBlock(id="c1", name="Bash", input={})],
                usage={"input_tokens": 10, "output_tokens": 3},
            ),
            _UserMessage(content=[
                _ToolResultBlock(tool_use_id="c1", content="ok"),
            ]),
            _AssistantMessage(
                content=[_TextBlock("done")],
                usage={"input_tokens": 25, "output_tokens": 8},  # cumulative
            ),
            _ResultMessage(subtype="success", session_id="s",
                           stop_reason="end_turn"),
        ]
        backend = AnthropicSDKBackend()
        result = await backend.run(AgentConfig(name="x"), "go")
        # The cumulative/final-turn usage wins, not the first turn's.
        assert result.usage == {"input_tokens": 25, "output_tokens": 8}

    @pytest.mark.asyncio
    async def test_usage_remains_none_when_sdk_omits_it(self, fake_sdk):
        """If no AssistantMessage carries usage (older SDK / stub), the
        result still has usage=None and doesn't crash."""
        from tigerharness.agent_sdk.backends.anthropic_sdk import (
            AnthropicSDKBackend,
        )
        _, client = fake_sdk
        client.replay = [
            _SystemMessage(subtype="init", data={"session_id": "s", "model": "m"}),
            _AssistantMessage(content=[_TextBlock("hi")]),  # no usage field
            _ResultMessage(subtype="success", session_id="s",
                           stop_reason="end_turn"),
        ]
        backend = AnthropicSDKBackend()
        result = await backend.run(AgentConfig(name="x"), "hi")
        assert result.usage is None

    @pytest.mark.asyncio
    async def test_tool_result_carries_originating_tool_name(self, fake_sdk):
        """Pre-fix, ToolResult.name was always "" because the
        ToolResultBlock only has tool_use_id. We now track
        {tool_use_id -> name} from earlier ToolUseBlock events so
        downstream consumers can read ev.name."""
        from tigerharness.agent_sdk.backends.anthropic_sdk import (
            AnthropicSDKBackend,
        )
        _, client = fake_sdk
        client.replay = [
            _SystemMessage(subtype="init", data={"session_id": "s", "model": "m"}),
            _AssistantMessage(content=[
                _ToolUseBlock(id="call_42", name="Bash", input={"command": "ls"}),
            ]),
            _UserMessage(content=[
                _ToolResultBlock(tool_use_id="call_42", content="a.py"),
            ]),
            _ResultMessage(subtype="success", session_id="s",
                           stop_reason="end_turn"),
        ]
        backend = AnthropicSDKBackend()
        handle = backend.run_stream(AgentConfig(name="x"), "go")
        events = []
        async for ev in handle:
            events.append(ev)

        tool_results = [e for e in events if isinstance(e, ToolResult)]
        assert len(tool_results) == 1
        assert tool_results[0].id == "call_42"
        assert tool_results[0].name == "Bash"  # <- was "" pre-fix

    @pytest.mark.asyncio
    async def test_tool_result_name_empty_when_no_prior_tool_use(self, fake_sdk):
        """If a ToolResultBlock arrives without a matching prior
        ToolUseBlock in the stream (unusual but possible on session
        resume), we degrade gracefully to name=""."""
        from tigerharness.agent_sdk.backends.anthropic_sdk import (
            AnthropicSDKBackend,
        )
        _, client = fake_sdk
        client.replay = [
            _SystemMessage(subtype="init", data={"session_id": "s", "model": "m"}),
            # Tool result with no preceding ToolUseBlock in THIS stream.
            _UserMessage(content=[
                _ToolResultBlock(tool_use_id="orphan", content="x"),
            ]),
            _ResultMessage(subtype="success", session_id="s",
                           stop_reason="end_turn"),
        ]
        backend = AnthropicSDKBackend()
        handle = backend.run_stream(AgentConfig(name="x"), "go")
        results = [e async for e in handle if isinstance(e, ToolResult)]
        assert len(results) == 1
        assert results[0].name == ""


# ---------------------------------------------------------------------------
# Coverage: tiny defensive branches
# ---------------------------------------------------------------------------

class TestTinyDefensiveBranches:
    def test_to_normalized_message_returns_none_for_unknown_type(self, fake_sdk):
        """Messages that aren't Assistant/User (e.g. SystemMessage) return
        None from _to_normalized_message -- defensive null."""
        sdk, _ = fake_sdk
        from tigerharness.agent_sdk.backends.anthropic_sdk import (
            _to_normalized_message,
        )
        assert _to_normalized_message(
            sdk, sdk.SystemMessage(subtype="init", data={}),
        ) is None

    @pytest.mark.asyncio
    async def test_handle_cancel_noop_when_no_client(self, fake_sdk):
        """Calling cancel() on a fresh handle (before run_stream wired
        up the client) is a safe no-op."""
        from tigerharness.agent_sdk.backends.anthropic_sdk import (
            _AnthropicStreamHandle,
        )
        handle = _AnthropicStreamHandle()
        await handle.cancel()  # no exception

    @pytest.mark.asyncio
    async def test_handle_cancel_swallows_interrupt_failure(self, fake_sdk):
        """If client.interrupt() raises, cancel() swallows it -- callers
        in a finally block shouldn't see an exception from cleanup."""
        from tigerharness.agent_sdk.backends.anthropic_sdk import (
            _AnthropicStreamHandle,
        )

        class _BadClient:
            async def interrupt(self) -> None:
                raise RuntimeError("interrupt broke")

        handle = _AnthropicStreamHandle()
        handle._client = _BadClient()
        await handle.cancel()  # no exception

    @pytest.mark.asyncio
    async def test_session_close_swallows_disconnect_failure(self, fake_sdk):
        """session.close() swallows a disconnect() exception (cleanup
        must never raise)."""
        from tigerharness.agent_sdk.backends.anthropic_sdk import (
            AnthropicSDKBackend,
        )
        backend = AnthropicSDKBackend()
        session = await backend.open_session()
        # Inject a client whose disconnect blows up.
        class _BadClient:
            async def disconnect(self) -> None:
                raise RuntimeError("network gone")
        session._client = _BadClient()
        session._connected = True
        await session.close()  # no exception
        assert session._client is None

    @pytest.mark.asyncio
    async def test_system_init_with_none_data_is_safe(self, fake_sdk):
        """A SystemMessage with data=None shouldn't crash the init path;
        we should still emit a RunStart (with session_id=None,
        model=None) and continue."""
        from tigerharness.agent_sdk.backends.anthropic_sdk import (
            AnthropicSDKBackend,
        )
        _, client = fake_sdk
        client.replay = [
            _SystemMessage(subtype="init", data=None),
            _AssistantMessage(content=[_TextBlock("ok")]),
            _ResultMessage(subtype="success", session_id="s",
                           stop_reason="end_turn"),
        ]
        backend = AnthropicSDKBackend()
        handle = backend.run_stream(AgentConfig(name="x"), "go")
        events = []
        async for ev in handle:
            events.append(ev)
        starts = [e for e in events if isinstance(e, RunStart)]
        assert len(starts) == 1
        assert starts[0].session_id is None
        assert starts[0].model is None

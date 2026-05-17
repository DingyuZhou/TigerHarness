"""Tests for ``agent_sdk.types`` — dataclasses, helpers, and Protocol shapes."""

from __future__ import annotations

from tigerharness.agent_sdk import (
    AgentBackend,
    AgentChanged,
    AgentConfig,
    ApprovalDecision,
    ApprovalRequest,
    BuiltinTool,
    ContentPart,
    ErrorEvent,
    Event,
    InputMessage,
    MessageComplete,
    NormalizedMessage,
    Role,
    RunDone,
    RunResult,
    RunStart,
    Session,
    StopReason,
    StreamHandle,
    TextDelta,
    TextPart,
    Thinking,
    ThinkingPart,
    ToolCall,
    ToolOutput,
    ToolResult,
    ToolResultPart,
    ToolSpec,
    ToolUsePart,
)


# ---------- ToolOutput.of -----------------------------------------------------


class TestToolOutputOf:
    def test_passes_through_existing(self) -> None:
        existing = ToolOutput(text="hi")
        assert ToolOutput.of(existing) is existing

    def test_wraps_string(self) -> None:
        out = ToolOutput.of("hello")
        assert out.text == "hello"
        assert out.data is None
        assert out.is_error is False

    def test_wraps_none(self) -> None:
        out = ToolOutput.of(None)
        assert out.text is None
        assert out.data is None
        assert out.is_error is False

    def test_wraps_int(self) -> None:
        out = ToolOutput.of(42)
        assert out.text is None
        assert out.data == 42

    def test_wraps_dict(self) -> None:
        out = ToolOutput.of({"k": "v"})
        assert out.text is None
        assert out.data == {"k": "v"}

    def test_wraps_list(self) -> None:
        out = ToolOutput.of([1, 2, 3])
        assert out.data == [1, 2, 3]


# ---------- Content parts -----------------------------------------------------


class TestContentParts:
    def test_text_part(self) -> None:
        p = TextPart(text="hi")
        assert p.text == "hi"

    def test_tool_use_part(self) -> None:
        p = ToolUsePart(id="x", name="t", input={"a": 1})
        assert p.id == "x" and p.name == "t" and p.input == {"a": 1}

    def test_tool_result_part_defaults(self) -> None:
        p = ToolResultPart(tool_use_id="x", content="out")
        assert p.is_error is False

    def test_tool_result_part_error(self) -> None:
        p = ToolResultPart(tool_use_id="x", content="bad", is_error=True)
        assert p.is_error is True

    def test_thinking_part(self) -> None:
        p = ThinkingPart(text="hmm")
        assert p.text == "hmm"

    def test_content_part_union_isinstance(self) -> None:
        # ContentPart is a runtime union — isinstance against the union works.
        assert isinstance(TextPart("a"), ContentPart)
        assert isinstance(ToolUsePart("a", "b", {}), ContentPart)


# ---------- Messages ----------------------------------------------------------


class TestMessages:
    def test_input_message_with_string_content(self) -> None:
        m = InputMessage(role="user", content="hi")
        assert m.role == "user"
        assert m.content == "hi"

    def test_input_message_with_list_content(self) -> None:
        parts = [TextPart("a"), TextPart("b")]
        m = InputMessage(role="user", content=parts)
        assert m.content == parts

    def test_normalized_message(self) -> None:
        m = NormalizedMessage(role="assistant", content=[TextPart("hi")])
        assert m.role == "assistant"
        assert len(m.content) == 1


# ---------- Tool spec / BuiltinTool ------------------------------------------


class TestToolSpec:
    def test_construction(self) -> None:
        async def handler(args: dict) -> str:
            return "ok"

        t = ToolSpec(name="t", description="d", input_schema={"type": "object"},
                     handler=handler)
        assert t.name == "t"
        assert t.needs_approval is False

    def test_needs_approval_flag(self) -> None:
        async def handler(args: dict) -> str:
            return "ok"

        t = ToolSpec(name="t", description="d", input_schema={},
                     handler=handler, needs_approval=True)
        assert t.needs_approval is True


class TestBuiltinTool:
    def test_default_config(self) -> None:
        b = BuiltinTool("Bash")
        assert b.name == "Bash"
        assert b.config == {}

    def test_with_config(self) -> None:
        b = BuiltinTool("WebSearch", {"max_uses": 3})
        assert b.config == {"max_uses": 3}


# ---------- Approval ----------------------------------------------------------


class TestApproval:
    def test_request(self) -> None:
        tc = ToolCall(id="1", name="Bash", arguments={"cmd": "ls"})
        req = ApprovalRequest(tool_call=tc, agent_name="x", session_id="s")
        assert req.tool_call.name == "Bash"

    def test_decision_defaults(self) -> None:
        d = ApprovalDecision(allow=True)
        assert d.reason is None
        assert d.updated_input is None

    def test_decision_with_reason(self) -> None:
        d = ApprovalDecision(allow=False, reason="nope",
                             updated_input={"x": 1})
        assert d.reason == "nope"
        assert d.updated_input == {"x": 1}


# ---------- Events ------------------------------------------------------------


class TestEvents:
    def test_run_start(self) -> None:
        e = RunStart(session_id="s", model="m")
        assert e.session_id == "s" and e.model == "m"

    def test_text_delta(self) -> None:
        e = TextDelta(text="hi")
        assert e.text == "hi"

    def test_message_complete_default_role(self) -> None:
        e = MessageComplete(text="hi")
        assert e.role == "assistant"

    def test_tool_call(self) -> None:
        e = ToolCall(id="1", name="Bash", arguments={})
        assert e.id == "1"

    def test_tool_result(self) -> None:
        e = ToolResult(id="1", name="Bash", output=ToolOutput(text="ok"))
        assert e.output.text == "ok"

    def test_thinking(self) -> None:
        e = Thinking(text="reasoning")
        assert e.text == "reasoning"

    def test_agent_changed(self) -> None:
        e = AgentChanged(name="other")
        assert e.name == "other"

    def test_error_event_default(self) -> None:
        e = ErrorEvent(message="oops")
        assert e.fatal is False

    def test_error_event_fatal(self) -> None:
        e = ErrorEvent(message="oops", fatal=True)
        assert e.fatal is True

    def test_run_done(self) -> None:
        e = RunDone(final_output="hi", stop_reason="end_turn",
                    usage=None, cost_usd=None)
        assert e.stop_reason == "end_turn"

    def test_event_union_membership(self) -> None:
        events = [
            RunStart(None, None), TextDelta("a"), MessageComplete("a"),
            ToolCall("1", "t", {}), ToolResult("1", "t", ToolOutput()),
            Thinking("x"), AgentChanged("x"), ErrorEvent("x"),
            RunDone(None, "end_turn", None, None),
        ]
        for e in events:
            assert isinstance(e, Event)


# ---------- Run result --------------------------------------------------------


class TestRunResult:
    def test_construction_with_defaults(self) -> None:
        r = RunResult(final_output="hi", transcript=[],
                      stop_reason="end_turn", usage=None, cost_usd=None)
        assert r.raw is None  # default

    def test_raw_field(self) -> None:
        r = RunResult(final_output=None, transcript=[],
                      stop_reason="error", usage=None, cost_usd=None,
                      raw={"native": "object"})
        assert r.raw == {"native": "object"}


# ---------- AgentConfig defaults ---------------------------------------------


class TestAgentConfig:
    def test_defaults(self) -> None:
        cfg = AgentConfig(name="x")
        assert cfg.instructions is None
        assert cfg.model is None
        assert cfg.tools == []
        assert cfg.builtin_tools == []
        assert cfg.output_schema is None
        assert cfg.max_turns is None
        assert cfg.temperature is None
        assert cfg.extra == {}

    def test_extra_independence(self) -> None:
        # field(default_factory=dict) creates a new dict per instance.
        a = AgentConfig(name="a")
        b = AgentConfig(name="b")
        a.extra["x"] = 1
        assert "x" not in b.extra


# ---------- Protocol shape sanity --------------------------------------------


class TestProtocolShapes:
    def test_role_literal_values(self) -> None:
        # Just exercise the type at runtime — Literal doesn't enforce.
        for r in ("user", "assistant", "system", "tool"):
            m = InputMessage(role=r, content="x")  # type: ignore[arg-type]
            assert m.role == r

    def test_stop_reason_values(self) -> None:
        # The full set we promise to emit.
        valid = {"end_turn", "max_turns", "max_budget", "tool_denied",
                 "interrupted", "refusal", "error"}
        # Exercise the type by constructing a RunDone for each.
        for sr in valid:
            r = RunDone(final_output=None, stop_reason=sr,  # type: ignore[arg-type]
                        usage=None, cost_usd=None)
            assert r.stop_reason == sr

    def test_session_protocol_runtime_check(self) -> None:
        # Anything with `id` property and async `close` satisfies Session.
        from tigerharness.agent_sdk.backends.claude_p import _ClaudePSession  # type: ignore[attr-defined]
        sess = _ClaudePSession()
        assert isinstance(sess, Session)

    def test_streamhandle_protocol_surface(self) -> None:
        # Protocol's runtime isinstance check evaluates @property attributes,
        # which would trigger StreamNotConsumedError on `result`. Verify the
        # surface via inspect.getattr_static which bypasses descriptors.
        import inspect

        from tigerharness.agent_sdk.backends._base import BaseStreamHandle
        handle = BaseStreamHandle()
        for attr in ("__aiter__", "__anext__", "__aenter__", "__aexit__",
                     "result", "is_complete", "cancel"):
            inspect.getattr_static(handle, attr)  # raises AttributeError if missing

    def test_agentbackend_protocol_runtime_check(self) -> None:
        from tigerharness.agent_sdk import get_backend
        b = get_backend("claude_p")
        assert isinstance(b, AgentBackend)

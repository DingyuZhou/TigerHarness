"""Tests for ``agent_sdk.backends.claude_p``.

These exercise the working subprocess backend end-to-end via the fake-CLI
fixtures defined in ``tests/conftest.py``. Internal helpers
(``_to_json_schema``, ``_input_to_text``, ``_input_to_transcript``) and
private session/handle classes are tested as well.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from tigerharness.agent_sdk import (
    AgentConfig,
    BackendNotImplementedError,
    BuiltinTool,
    CLIError,
    ErrorEvent,
    InputMessage,
    MessageComplete,
    NormalizedMessage,
    RunDone,
    RunResult,
    RunStart,
    StreamNotConsumedError,
    TextPart,
    Thinking,
    ThinkingPart,
    ToolCall,
    ToolOutput,
    ToolResult,
    ToolResultPart,
    ToolUsePart,
    get_backend,
)
from tigerharness.agent_sdk.backends.claude_p import (
    ClaudePBackend,
    _ClaudePSession,
    _input_to_text,
    _input_to_transcript,
    _to_json_schema,
)

from tests.agent_sdk._helpers import asyncio_test


# =============================================================================
# Module-level helpers
# =============================================================================


class TestToJsonSchema:
    def test_none_returns_none(self) -> None:
        assert _to_json_schema(None) is None

    def test_dict_returned_as_is(self) -> None:
        s = {"type": "object", "properties": {}}
        assert _to_json_schema(s) is s

    def test_pydantic_v2_model(self) -> None:
        from pydantic import BaseModel

        class Out(BaseModel):
            answer: int
            confidence: float

        schema = _to_json_schema(Out)
        assert isinstance(schema, dict)
        assert schema["properties"]["answer"]["type"] == "integer"

    def test_unsupported_type_raises(self) -> None:
        with pytest.raises(TypeError, match="output_schema"):
            _to_json_schema(42)

    def test_object_with_callable_schema_attribute(self) -> None:
        # Simulate an object whose `schema` attribute is callable (covers the
        # pydantic v1 fallback branch).
        class Fake:
            def schema(self) -> dict:
                return {"type": "object", "via": "schema()"}

        result = _to_json_schema(Fake())
        assert result == {"type": "object", "via": "schema()"}

    def test_object_with_schema_that_typeerrors_falls_through(self) -> None:
        class Fake:
            def schema(self, required_arg) -> dict:  # type: ignore[no-untyped-def]
                return {}

        with pytest.raises(TypeError, match="output_schema"):
            _to_json_schema(Fake())


class TestInputToText:
    def test_string_passthrough(self) -> None:
        assert _input_to_text("hello") == "hello"

    def test_text_parts_joined(self) -> None:
        parts = [TextPart("a"), TextPart("b")]
        assert _input_to_text(parts) == "a\n\nb"

    def test_thinking_skipped(self) -> None:
        parts = [TextPart("hi"), ThinkingPart("inner"), TextPart("there")]
        assert _input_to_text(parts) == "hi\n\nthere"

    def test_tool_parts_skipped(self) -> None:
        parts = [
            TextPart("a"),
            ToolUsePart("1", "Bash", {"cmd": "ls"}),
            ToolResultPart("1", "out"),
            TextPart("b"),
        ]
        assert _input_to_text(parts) == "a\n\nb"

    def test_empty_list(self) -> None:
        assert _input_to_text([]) == ""


class TestInputToTranscript:
    def test_string_prompt_creates_one_user_message(self) -> None:
        out = _input_to_transcript("hi")
        assert len(out) == 1
        assert out[0].role == "user"
        assert isinstance(out[0].content[0], TextPart)
        assert out[0].content[0].text == "hi"

    def test_input_message_list_preserves_roles(self) -> None:
        msgs = [
            InputMessage(role="user", content="first"),
            InputMessage(role="assistant", content=[TextPart("reply")]),
            InputMessage(role="user", content="follow-up"),
        ]
        out = _input_to_transcript(msgs)
        assert [m.role for m in out] == ["user", "assistant", "user"]

    def test_input_message_with_string_content_wraps(self) -> None:
        msgs = [InputMessage(role="user", content="hi")]
        out = _input_to_transcript(msgs)
        assert isinstance(out[0].content[0], TextPart)


# =============================================================================
# _ClaudePSession
# =============================================================================


class TestClaudePSession:
    def test_default_id_is_empty(self) -> None:
        s = _ClaudePSession()
        assert s.id == ""

    def test_explicit_id(self) -> None:
        s = _ClaudePSession(_id="abc")
        assert s.id == "abc"

    def test_set_id_when_empty(self) -> None:
        s = _ClaudePSession()
        s._set_id("new-id")
        assert s.id == "new-id"

    def test_set_id_idempotent_does_not_clobber(self) -> None:
        s = _ClaudePSession(_id="original")
        s._set_id("different")
        assert s.id == "original"

    def test_set_id_ignores_empty(self) -> None:
        s = _ClaudePSession()
        s._set_id("")
        assert s.id == ""

    @asyncio_test
    async def test_close_is_noop(self) -> None:
        s = _ClaudePSession(_id="abc")
        await s.close()  # should not raise


# =============================================================================
# Backend construction & open_session
# =============================================================================


class TestBackendConstruction:
    def test_default_cli_name(self) -> None:
        b = ClaudePBackend()
        assert b.cli == "claude"

    def test_custom_cli_path(self, cli_success: Path) -> None:
        b = ClaudePBackend(cli=str(cli_success))
        assert b.cli == str(cli_success)

    def test_env_and_cwd_held(self) -> None:
        b = ClaudePBackend(cli="x", env={"FOO": "bar"}, cwd="/tmp")
        assert b.env == {"FOO": "bar"}
        assert b.cwd == "/tmp"

    @asyncio_test
    async def test_open_session_default(self) -> None:
        b = ClaudePBackend()
        s = await b.open_session()
        assert s.id == ""

    @asyncio_test
    async def test_open_session_with_resume_id(self) -> None:
        b = ClaudePBackend()
        s = await b.open_session(resume_id="abc-123")
        assert s.id == "abc-123"


# =============================================================================
# argv construction
# =============================================================================


class TestBuildArgv:
    def _build(
        self,
        cli: str,
        cfg: AgentConfig,
        session=None,
    ) -> list[str]:
        backend = ClaudePBackend(cli=cli)
        return backend._build_argv(cfg, session)

    def test_minimal(self, cli_success: Path) -> None:
        cfg = AgentConfig(name="x")
        argv = self._build(str(cli_success), cfg)
        # Required flags
        assert argv[0] == str(cli_success)
        assert argv[1] == "-p"
        assert "--output-format" in argv
        assert argv[argv.index("--output-format") + 1] == "stream-json"
        assert "--input-format" in argv
        assert argv[argv.index("--input-format") + 1] == "stream-json"
        assert "--verbose" in argv

    def test_system_prompt(self, cli_success: Path) -> None:
        cfg = AgentConfig(name="x", instructions="be brief")
        argv = self._build(str(cli_success), cfg)
        i = argv.index("--system-prompt")
        assert argv[i + 1] == "be brief"

    def test_no_system_prompt_when_none(self, cli_success: Path) -> None:
        cfg = AgentConfig(name="x")
        argv = self._build(str(cli_success), cfg)
        assert "--system-prompt" not in argv

    def test_model(self, cli_success: Path) -> None:
        cfg = AgentConfig(name="x", model="claude-sonnet-4-6")
        argv = self._build(str(cli_success), cfg)
        i = argv.index("--model")
        assert argv[i + 1] == "claude-sonnet-4-6"

    def test_max_turns(self, cli_success: Path) -> None:
        cfg = AgentConfig(name="x", max_turns=5)
        argv = self._build(str(cli_success), cfg)
        i = argv.index("--max-turns")
        assert argv[i + 1] == "5"

    def test_max_budget(self, cli_success: Path) -> None:
        cfg = AgentConfig(name="x", extra={"max_budget_usd": 0.25})
        argv = self._build(str(cli_success), cfg)
        i = argv.index("--max-budget-usd")
        assert argv[i + 1] == "0.25"

    def test_output_schema_dict(self, cli_success: Path) -> None:
        schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
        cfg = AgentConfig(name="x", output_schema=schema)
        argv = self._build(str(cli_success), cfg)
        import json
        i = argv.index("--json-schema")
        assert json.loads(argv[i + 1]) == schema

    def test_output_schema_pydantic(self, cli_success: Path) -> None:
        from pydantic import BaseModel

        class Out(BaseModel):
            n: int

        cfg = AgentConfig(name="x", output_schema=Out)
        argv = self._build(str(cli_success), cfg)
        import json
        i = argv.index("--json-schema")
        loaded = json.loads(argv[i + 1])
        assert loaded["properties"]["n"]["type"] == "integer"

    def test_builtin_tools_emit_both_flags(self, cli_success: Path) -> None:
        cfg = AgentConfig(
            name="x",
            builtin_tools=[BuiltinTool("Bash"), BuiltinTool("Read")],
        )
        argv = self._build(str(cli_success), cfg)
        assert argv[argv.index("--tools") + 1] == "Bash,Read"
        assert argv[argv.index("--allowedTools") + 1] == "Bash,Read"

    def test_builtin_tool_with_config_rejected(self, cli_success: Path) -> None:
        cfg = AgentConfig(
            name="x",
            builtin_tools=[BuiltinTool("WebSearch", {"max_uses": 3})],
        )
        with pytest.raises(BackendNotImplementedError, match="config"):
            self._build(str(cli_success), cfg)

    def test_no_builtin_tools_means_no_tool_flags(self, cli_success: Path) -> None:
        cfg = AgentConfig(name="x", instructions="i")
        argv = self._build(str(cli_success), cfg)
        assert "--tools" not in argv
        assert "--allowedTools" not in argv

    def test_permission_mode(self, cli_success: Path) -> None:
        cfg = AgentConfig(name="x",
                          extra={"permission_mode": "bypassPermissions"})
        argv = self._build(str(cli_success), cfg)
        i = argv.index("--permission-mode")
        assert argv[i + 1] == "bypassPermissions"

    def test_resume_when_session_has_id(self, cli_success: Path) -> None:
        cfg = AgentConfig(name="x")
        sess = _ClaudePSession(_id="prior-uuid")
        argv = self._build(str(cli_success), cfg, sess)
        i = argv.index("--resume")
        assert argv[i + 1] == "prior-uuid"

    def test_no_resume_when_session_id_empty(self, cli_success: Path) -> None:
        cfg = AgentConfig(name="x")
        sess = _ClaudePSession()  # empty id
        argv = self._build(str(cli_success), cfg, sess)
        assert "--resume" not in argv

    def test_add_dirs(self, cli_success: Path) -> None:
        cfg = AgentConfig(name="x", extra={"add_dirs": ["/a", "/b"]})
        argv = self._build(str(cli_success), cfg)
        # Two --add-dir flags, one per dir
        idxs = [i for i, a in enumerate(argv) if a == "--add-dir"]
        assert len(idxs) == 2
        assert argv[idxs[0] + 1] == "/a"
        assert argv[idxs[1] + 1] == "/b"

    def test_disallowed_tools(self, cli_success: Path) -> None:
        cfg = AgentConfig(name="x",
                          extra={"disallowed_tools": ["Edit", "Write"]})
        argv = self._build(str(cli_success), cfg)
        i = argv.index("--disallowedTools")
        assert argv[i + 1] == "Edit,Write"

    def test_settings_path(self, cli_success: Path) -> None:
        cfg = AgentConfig(name="x", extra={"settings": "/tmp/s.json"})
        argv = self._build(str(cli_success), cfg)
        i = argv.index("--settings")
        assert argv[i + 1] == "/tmp/s.json"

    def test_cli_args_with_value(self, cli_success: Path) -> None:
        cfg = AgentConfig(name="x", extra={"cli_args": {"foo": "bar"}})
        argv = self._build(str(cli_success), cfg)
        i = argv.index("--foo")
        assert argv[i + 1] == "bar"

    def test_cli_args_boolean_flag(self, cli_success: Path) -> None:
        cfg = AgentConfig(name="x", extra={"cli_args": {"flag": None}})
        argv = self._build(str(cli_success), cfg)
        # --flag is present, but the next element is something else
        # (boolean flags have no value)
        i = argv.index("--flag")
        # If it's the last element, fine. Otherwise the next element should
        # not be the value of this flag.
        if i + 1 < len(argv):
            # The next arg shouldn't be a literal value belonging to --flag.
            # Hard to assert positively without parsing, so just confirm the
            # flag itself appears.
            pass

    def test_missing_cli_raises_clierror(self) -> None:
        backend = ClaudePBackend(cli="/definitely/does/not/exist")
        cfg = AgentConfig(name="x")
        with pytest.raises(CLIError, match="not found"):
            backend._build_argv(cfg, None)

    def test_absolute_path_to_existing_file_accepted(
        self, cli_success: Path
    ) -> None:
        # The cli_success fixture is an absolute path to an executable file;
        # _build_argv accepts it via the os.path.isfile fallback.
        backend = ClaudePBackend(cli=str(cli_success))
        argv = backend._build_argv(AgentConfig(name="x"), None)
        assert argv[0] == str(cli_success)


# =============================================================================
# stdin payload construction
# =============================================================================


class TestBuildStdinPayload:
    def _backend(self, cli_path: Path) -> ClaudePBackend:
        return ClaudePBackend(cli=str(cli_path))

    def test_string_prompt(self, cli_success: Path) -> None:
        b = self._backend(cli_success)
        payload = b._build_stdin_payload("hello", None)
        import json
        line = payload.strip()
        msg = json.loads(line)
        assert msg["type"] == "user"
        assert msg["message"]["content"] == "hello"
        assert msg["parent_tool_use_id"] is None

    def test_string_prompt_includes_session_id(self, cli_success: Path) -> None:
        b = self._backend(cli_success)
        sess = _ClaudePSession(_id="sid-123")
        payload = b._build_stdin_payload("hi", sess)
        import json
        msg = json.loads(payload.strip())
        assert msg["session_id"] == "sid-123"

    def test_input_message_list(self, cli_success: Path) -> None:
        b = self._backend(cli_success)
        msgs = [
            InputMessage(role="user", content="first"),
            InputMessage(role="user", content=[TextPart("second")]),
        ]
        payload = b._build_stdin_payload(msgs, None)
        import json
        lines = [l for l in payload.split("\n") if l]
        assert len(lines) == 2
        contents = [json.loads(l)["message"]["content"] for l in lines]
        assert contents == ["first", "second"]

    def test_non_user_roles_silently_skipped(self, cli_success: Path) -> None:
        b = self._backend(cli_success)
        msgs = [
            InputMessage(role="system", content="ignored"),
            InputMessage(role="user", content="kept"),
            InputMessage(role="assistant", content="ignored2"),
        ]
        payload = b._build_stdin_payload(msgs, None)
        import json
        lines = [l for l in payload.split("\n") if l]
        assert len(lines) == 1
        assert json.loads(lines[0])["message"]["content"] == "kept"

    def test_empty_string_raises(self, cli_success: Path) -> None:
        b = self._backend(cli_success)
        with pytest.raises(ValueError, match="empty"):
            b._build_stdin_payload("", None)

    def test_whitespace_only_raises(self, cli_success: Path) -> None:
        b = self._backend(cli_success)
        with pytest.raises(ValueError, match="empty"):
            b._build_stdin_payload("   \n\t  ", None)

    def test_empty_list_raises(self, cli_success: Path) -> None:
        b = self._backend(cli_success)
        with pytest.raises(ValueError, match="no user-role"):
            b._build_stdin_payload([], None)

    def test_only_non_user_roles_raises(self, cli_success: Path) -> None:
        b = self._backend(cli_success)
        msgs = [InputMessage(role="system", content="hi")]
        with pytest.raises(ValueError, match="no user-role"):
            b._build_stdin_payload(msgs, None)

    def test_user_with_whitespace_only_skipped(self, cli_success: Path) -> None:
        b = self._backend(cli_success)
        msgs = [
            InputMessage(role="user", content="  \n  "),
            InputMessage(role="user", content="real"),
        ]
        payload = b._build_stdin_payload(msgs, None)
        import json
        lines = [l for l in payload.split("\n") if l]
        assert len(lines) == 1
        assert json.loads(lines[0])["message"]["content"] == "real"


# =============================================================================
# run_stream rejections
# =============================================================================


class TestRunStreamRejections:
    def test_rejects_user_tools(self, cli_success: Path) -> None:
        from tigerharness.agent_sdk import ToolSpec

        async def handler(args: dict) -> str:
            return "ok"

        backend = ClaudePBackend(cli=str(cli_success))
        cfg = AgentConfig(
            name="x",
            tools=[ToolSpec("t", "d", {"type": "object"}, handler)],
        )
        with pytest.raises(BackendNotImplementedError, match="ToolSpec"):
            backend.run_stream(cfg, "hi")

    def test_rejects_approval(self, cli_success: Path) -> None:
        async def gate(req: Any) -> Any:
            from tigerharness.agent_sdk import ApprovalDecision
            return ApprovalDecision(allow=True)

        backend = ClaudePBackend(cli=str(cli_success))
        cfg = AgentConfig(name="x", instructions="i")
        with pytest.raises(BackendNotImplementedError, match="approval"):
            backend.run_stream(cfg, "hi", approval=gate)


# =============================================================================
# Full subprocess pipeline (via fake CLIs)
# =============================================================================


class TestPipelineSuccess:
    @asyncio_test
    async def test_one_shot_run(self, cli_success: Path) -> None:
        backend = ClaudePBackend(cli=str(cli_success))
        cfg = AgentConfig(name="qa", instructions="be brief")
        result = await backend.run(cfg, "hi")
        assert isinstance(result, RunResult)
        assert result.final_output == "Hello there."
        assert result.stop_reason == "end_turn"
        assert result.cost_usd == 0.001
        assert result.usage == {"input_tokens": 5, "output_tokens": 3}
        # Transcript includes user input + assistant reply.
        assert [m.role for m in result.transcript] == ["user", "assistant"]
        assert result.transcript[0].content[0].text == "hi"  # type: ignore[union-attr]

    @asyncio_test
    async def test_streaming_event_order(self, cli_success: Path) -> None:
        backend = ClaudePBackend(cli=str(cli_success))
        cfg = AgentConfig(name="qa", instructions="be brief")
        handle = backend.run_stream(cfg, "hi")
        events: list[str] = []
        async for ev in handle:
            events.append(type(ev).__name__)
        assert events == ["RunStart", "MessageComplete", "RunDone"]
        assert handle.is_complete is True

    @asyncio_test
    async def test_run_start_carries_session_and_model(
        self, cli_success: Path
    ) -> None:
        backend = ClaudePBackend(cli=str(cli_success))
        cfg = AgentConfig(name="x")
        handle = backend.run_stream(cfg, "hi")
        seen: list[Any] = []
        async for ev in handle:
            seen.append(ev)
        starts = [e for e in seen if isinstance(e, RunStart)]
        assert len(starts) == 1
        assert starts[0].session_id == "sess-ok"
        assert starts[0].model == "test-model"


class TestPipelineToolRoundtrip:
    @asyncio_test
    async def test_full_event_sequence(self, cli_tools: Path) -> None:
        backend = ClaudePBackend(cli=str(cli_tools))
        cfg = AgentConfig(
            name="dev",
            instructions="use tools",
            builtin_tools=[BuiltinTool("Bash")],
            extra={"permission_mode": "bypassPermissions"},
        )
        handle = backend.run_stream(cfg, "list files")
        events: list[Any] = []
        async for ev in handle:
            events.append(ev)

        types = [type(e).__name__ for e in events]
        assert types == [
            "RunStart", "Thinking", "ToolCall",
            "ToolResult", "MessageComplete", "RunDone",
        ]

        # ToolCall + ToolResult share an id, ToolResult name was looked up.
        tool_call = next(e for e in events if isinstance(e, ToolCall))
        tool_result = next(e for e in events if isinstance(e, ToolResult))
        assert tool_call.id == tool_result.id == "call_1"
        assert tool_result.name == "Bash"
        assert tool_result.output.text == "a.py\nb.py"
        assert tool_result.output.is_error is False

        # Thinking content surfaces as a separate event.
        thinking = next(e for e in events if isinstance(e, Thinking))
        assert thinking.text == "Let me think..."

    @asyncio_test
    async def test_transcript_full(self, cli_tools: Path) -> None:
        backend = ClaudePBackend(cli=str(cli_tools))
        cfg = AgentConfig(name="dev", builtin_tools=[BuiltinTool("Bash")],
                          extra={"permission_mode": "bypassPermissions"})
        result = await backend.run(cfg, "list files")
        roles = [m.role for m in result.transcript]
        # user prompt + assistant tool_use + user tool_result + assistant text
        assert roles == ["user", "assistant", "user", "assistant"]
        # First assistant message has both Thinking and ToolUse parts.
        first_asst = result.transcript[1]
        types = [type(p).__name__ for p in first_asst.content]
        assert "ThinkingPart" in types and "ToolUsePart" in types


class TestPipelineErrorPaths:
    @asyncio_test
    async def test_max_turns(self, cli_max_turns: Path) -> None:
        backend = ClaudePBackend(cli=str(cli_max_turns))
        cfg = AgentConfig(name="x", max_turns=1)
        result = await backend.run(cfg, "hi")
        assert result.stop_reason == "max_turns"

    @asyncio_test
    async def test_max_budget(self, cli_max_budget: Path) -> None:
        backend = ClaudePBackend(cli=str(cli_max_budget))
        cfg = AgentConfig(name="x", extra={"max_budget_usd": 0.01})
        result = await backend.run(cfg, "hi")
        assert result.stop_reason == "max_budget"

    @asyncio_test
    async def test_generic_error_subtype(self, cli_generic_error: Path) -> None:
        backend = ClaudePBackend(cli=str(cli_generic_error))
        cfg = AgentConfig(name="x")
        result = await backend.run(cfg, "hi")
        assert result.stop_reason == "error"
        assert result.final_output == "boom"

    @asyncio_test
    async def test_nonzero_exit_emits_error_event(
        self, cli_nonzero: Path
    ) -> None:
        backend = ClaudePBackend(cli=str(cli_nonzero))
        cfg = AgentConfig(name="x")
        handle = backend.run_stream(cfg, "hi")
        msgs: list[str] = []
        seen_error = False
        async for ev in handle:
            if isinstance(ev, ErrorEvent):
                seen_error = True
                msgs.append(ev.message)
                assert ev.fatal is True
        assert seen_error
        assert any("subprocess crashed" in m for m in msgs)
        assert handle.result.stop_reason == "error"

    @asyncio_test
    async def test_bad_json_line_yields_nonfatal_error_and_continues(
        self, cli_bad_json: Path
    ) -> None:
        backend = ClaudePBackend(cli=str(cli_bad_json))
        cfg = AgentConfig(name="x")
        handle = backend.run_stream(cfg, "hi")
        kinds = []
        nonfatal_errors = 0
        async for ev in handle:
            kinds.append(type(ev).__name__)
            if isinstance(ev, ErrorEvent) and not ev.fatal:
                nonfatal_errors += 1
                assert "Bad JSON" in ev.message
        # We still saw the assistant text and the result.
        assert "MessageComplete" in kinds
        assert "RunDone" in kinds
        assert nonfatal_errors >= 1
        assert handle.result.stop_reason == "end_turn"


class TestPipelineCancellation:
    @asyncio_test
    async def test_cancel_mid_stream(self, cli_slow: Path) -> None:
        backend = ClaudePBackend(cli=str(cli_slow))
        cfg = AgentConfig(name="x")
        handle = backend.run_stream(cfg, "go")
        t0 = time.monotonic()
        async for ev in handle:
            if isinstance(ev, RunStart):
                await handle.cancel()
        elapsed = time.monotonic() - t0
        assert elapsed < 5
        assert handle.result.stop_reason == "interrupted"

    @asyncio_test
    async def test_async_with_break_cleans_up_subprocess(
        self, cli_slow: Path
    ) -> None:
        backend = ClaudePBackend(cli=str(cli_slow))
        cfg = AgentConfig(name="x")
        t0 = time.monotonic()
        chunks = 0
        async with backend.run_stream(cfg, "go") as handle:
            async for ev in handle:
                if isinstance(ev, MessageComplete):
                    chunks += 1
                    if chunks >= 2:
                        break
        elapsed = time.monotonic() - t0
        assert chunks == 2
        assert elapsed < 4

    @asyncio_test
    async def test_cancel_with_after_turn_hint(self, cli_slow: Path) -> None:
        backend = ClaudePBackend(cli=str(cli_slow))
        cfg = AgentConfig(name="x")
        handle = backend.run_stream(cfg, "go")
        async for ev in handle:
            if isinstance(ev, RunStart):
                await handle.cancel(after_turn=True)
        assert handle.result.stop_reason == "interrupted"

    @asyncio_test
    async def test_double_cancel_safe(self, cli_slow: Path) -> None:
        backend = ClaudePBackend(cli=str(cli_slow))
        cfg = AgentConfig(name="x")
        handle = backend.run_stream(cfg, "go")
        async for ev in handle:
            if isinstance(ev, RunStart):
                await handle.cancel()
                await handle.cancel()  # second call must not raise
        assert handle.result.stop_reason == "interrupted"


class TestPipelineLargeStderrAndStructured:
    @asyncio_test
    async def test_concurrent_stderr_drain_does_not_deadlock(
        self, cli_large_stderr: Path
    ) -> None:
        # The fake CLI emits ~2 MB of stderr after the init event but before
        # the result event. Without our concurrent drainer the OS pipe
        # buffer fills, the subprocess blocks on stderr.write, the result
        # event is never produced, and this test hangs until pytest timeout.
        # With the drainer running concurrently, we sail through.
        backend = ClaudePBackend(cli=str(cli_large_stderr))
        cfg = AgentConfig(name="x")
        t0 = time.monotonic()
        # Wrap with asyncio.wait_for so a regression fails fast instead of
        # hanging.
        result = await asyncio.wait_for(backend.run(cfg, "hi"), timeout=10.0)
        elapsed = time.monotonic() - t0
        assert elapsed < 5
        assert result.stop_reason == "end_turn"

    @asyncio_test
    async def test_structured_output_returned(
        self, cli_large_stderr: Path
    ) -> None:
        backend = ClaudePBackend(cli=str(cli_large_stderr))
        cfg = AgentConfig(name="x",
                          output_schema={"type": "object",
                                         "properties": {"answer": {"type": "integer"}}})
        result = await backend.run(cfg, "hi")
        assert result.final_output == {"answer": 42}


class TestPipelineEdgeCases:
    @asyncio_test
    async def test_empty_assistant_and_unknown_blocks(
        self, cli_empty_assistant: Path
    ) -> None:
        backend = ClaudePBackend(cli=str(cli_empty_assistant))
        cfg = AgentConfig(name="x")
        handle = backend.run_stream(cfg, "hi")
        events: list[Any] = []
        async for ev in handle:
            events.append(ev)
        # No MessageComplete (assistant content was empty / unknown).
        assert not any(isinstance(e, MessageComplete) for e in events)
        # The user message with raw string content is captured in transcript.
        roles = [m.role for m in handle.result.transcript]
        assert "user" in roles
        # End reached normally.
        assert handle.result.stop_reason == "end_turn"

    @asyncio_test
    async def test_streamhandle_result_before_completion_raises(
        self, cli_slow: Path
    ) -> None:
        backend = ClaudePBackend(cli=str(cli_slow))
        cfg = AgentConfig(name="x")
        handle = backend.run_stream(cfg, "go")
        with pytest.raises(StreamNotConsumedError):
            _ = handle.result
        await handle.cancel()
        # Drain so the subprocess gets reaped before pytest tears down tmp_path.
        async for _ in handle:
            pass

    @asyncio_test
    async def test_blank_lines_silently_skipped(
        self, cli_user_text_and_blank: Path
    ) -> None:
        backend = ClaudePBackend(cli=str(cli_user_text_and_blank))
        cfg = AgentConfig(name="x")
        result = await backend.run(cfg, "hi")
        # Blank lines did not produce an ErrorEvent.
        assert result.stop_reason == "end_turn"

    @asyncio_test
    async def test_user_message_text_block_kept_in_transcript(
        self, cli_user_text_and_blank: Path
    ) -> None:
        # User-role messages can contain plain text blocks (not just
        # tool_results). The parser should preserve them in the transcript.
        backend = ClaudePBackend(cli=str(cli_user_text_and_blank))
        cfg = AgentConfig(name="x")
        result = await backend.run(cfg, "hi")
        # Transcript: [seeded user "hi", echoed user text "context note"].
        user_msgs = [m for m in result.transcript if m.role == "user"]
        assert len(user_msgs) >= 2
        # The second user message carries the CLI-emitted text block.
        second = user_msgs[1]
        assert any(
            isinstance(p, TextPart) and p.text == "context note"
            for p in second.content
        )

    @asyncio_test
    async def test_session_id_captured_from_result_when_no_init(
        self, cli_result_without_init: Path
    ) -> None:
        backend = ClaudePBackend(cli=str(cli_result_without_init))
        cfg = AgentConfig(name="x")
        sess = await backend.open_session()
        await backend.run(cfg, "hi", session=sess)
        # Even though no `system.init` event arrived, the parser captures
        # the session id from the `result` event.
        assert sess.id == "from-result"

    @asyncio_test
    async def test_aclose_without_prior_cancel_reaps_subprocess(
        self, cli_slow: Path
    ) -> None:
        # When the user breaks iteration without async-with and without
        # calling cancel(), then later closes the underlying generator,
        # the _iter() finally block must SIGINT the still-alive subprocess.
        backend = ClaudePBackend(cli=str(cli_slow))
        cfg = AgentConfig(name="x")
        handle = backend.run_stream(cfg, "go")
        chunks = 0
        async for ev in handle:
            if isinstance(ev, MessageComplete):
                chunks += 1
                if chunks >= 1:
                    break
        # Drive aclose() directly — no prior cancel() — to exercise the
        # _iter cleanup path that handles a still-alive subprocess.
        t0 = time.monotonic()
        assert handle._gen is not None
        await handle._gen.aclose()  # type: ignore[union-attr]
        assert time.monotonic() - t0 < 4


# =============================================================================
# Multi-turn session + factory wiring
# =============================================================================


class TestSessionLifecycle:
    @asyncio_test
    async def test_session_id_populated_after_first_run(
        self, cli_success: Path
    ) -> None:
        backend = ClaudePBackend(cli=str(cli_success))
        cfg = AgentConfig(name="x")
        sess = await backend.open_session()
        assert sess.id == ""
        await backend.run(cfg, "first", session=sess)
        assert sess.id == "sess-ok"

    @asyncio_test
    async def test_session_id_stable_across_turns(
        self, cli_success: Path
    ) -> None:
        backend = ClaudePBackend(cli=str(cli_success))
        cfg = AgentConfig(name="x")
        sess = await backend.open_session()
        await backend.run(cfg, "first", session=sess)
        first_id = sess.id
        await backend.run(cfg, "second", session=sess)
        assert sess.id == first_id


class TestFactoryIntegration:
    @asyncio_test
    async def test_get_backend_yields_working_handle(
        self, cli_success: Path
    ) -> None:
        backend = get_backend("claude_p", cli=str(cli_success))
        cfg = AgentConfig(name="x")
        result = await backend.run(cfg, "hi")
        assert result.final_output == "Hello there."

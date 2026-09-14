"""Tests for ``agent_sdk.backends.codex_exec``.

Exercised end-to-end through fake ``codex`` scripts (the ``make_cli``
fixture from ``tests/agent_sdk/conftest.py``) that emit Codex's JSONL
event vocabulary, so every parsing path runs without a real Codex binary.
"""

from __future__ import annotations

import json
import logging
import os
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
    RunDone,
    RunStart,
    TextPart,
    Thinking,
    ThinkingPart,
    ToolCall,
    ToolOutput,
    ToolResult,
    ToolResultPart,
    ToolSpec,
    ToolUsePart,
    get_backend,
    list_backends,
)
from tigerharness.agent_sdk.backends.codex_exec import (
    CodexExecBackend,
    _CodexSession,
    _error_text,
    _tool_view,
    _toml_string,
)

from tests.agent_sdk._helpers import asyncio_test


# =============================================================================
# Fake codex bodies
# =============================================================================

ARGV_DUMP_BODY = """
import json, os, sys
stdin = sys.stdin.read()
print(json.dumps({"type": "thread.started", "thread_id": "thr-argv"}), flush=True)
print(json.dumps({"type": "turn.started"}), flush=True)
print(json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message",
    "text": json.dumps({"argv": sys.argv[1:], "stdin": stdin,
                        "env": os.environ.get("TH_TEST_ENV")})}}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 2}}), flush=True)
"""

SUCCESS_BODY = """
import json, sys
sys.stdin.read()
print(json.dumps({"type": "thread.started", "thread_id": "thr-ok"}), flush=True)
print(json.dumps({"type": "turn.started"}), flush=True)
print(json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "Hello there."}}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 3}}), flush=True)
"""

TOOLS_BODY = """
import json, sys
sys.stdin.read()
def emit(o): print(json.dumps(o), flush=True)
emit({"type": "thread.started", "thread_id": "thr-tools"})
emit({"type": "thread.started", "thread_id": "thr-tools-dup"})
emit({"type": "turn.started"})
emit({"type": "item.completed", "item": {"id": "r1", "type": "reasoning", "text": "Let me think..."}})
emit({"type": "item.started", "item": {"id": "c1", "type": "command_execution", "command": "ls", "aggregated_output": "", "exit_code": None, "status": "in_progress"}})
emit({"type": "item.updated", "item": {"id": "c1", "type": "command_execution", "command": "ls", "aggregated_output": "a\\\\n", "exit_code": None, "status": "in_progress"}})
emit({"type": "item.completed", "item": {"id": "c1", "type": "command_execution", "command": "ls", "aggregated_output": "a\\\\nb\\\\n", "exit_code": 0, "status": "completed"}})
emit({"type": "item.completed", "item": {"id": "c2", "type": "command_execution", "command": "false", "aggregated_output": "", "exit_code": 1, "status": "failed"}})
emit({"type": "item.started", "item": {"id": "f1", "type": "file_change", "changes": [{"path": "a.py", "kind": "update"}], "status": "in_progress"}})
emit({"type": "item.completed", "item": {"id": "f1", "type": "file_change", "changes": [{"path": "a.py", "kind": "update"}], "status": "completed"}})
emit({"type": "item.completed", "item": {"id": "f2", "type": "file_change", "changes": []}})
emit({"type": "item.started", "item": {"id": "m1", "type": "mcp_tool_call", "server": "srv", "tool": "search", "arguments": {"q": "x"}, "status": "in_progress"}})
emit({"type": "item.completed", "item": {"id": "m1", "type": "mcp_tool_call", "server": "srv", "tool": "search", "arguments": {"q": "x"}, "status": "completed", "result": {"hits": 2}}})
emit({"type": "item.completed", "item": {"id": "m2", "type": "mcp_tool_call", "server": "srv", "tool": "boom", "arguments": "raw", "status": "failed", "error": {"message": "nope"}}})
emit({"type": "item.completed", "item": {"id": "w1", "type": "web_search", "query": "codex jsonl"}})
emit({"type": "item.completed", "item": {"id": "t1", "type": "todo_list", "items": []}})
emit({"type": "item.completed", "item": {"id": "e1", "type": "error", "message": "soft error"}})
emit({"type": "item.completed", "item": "not-a-dict"})
emit({"type": "item.completed", "item": {"id": "a1", "type": "agent_message", "text": "Done."}})
emit({"type": "turn.completed", "usage": {"input_tokens": 9, "output_tokens": 4}})
"""

TURN_FAILED_BODY = """
import json, sys
sys.stdin.read()
print(json.dumps({"type": "thread.started", "thread_id": "thr-fail"}), flush=True)
print(json.dumps({"type": "turn.failed", "error": {"message": "context window exceeded"}}), flush=True)
"""

TURN_FAILED_NO_ERROR_BODY = """
import json, sys
sys.stdin.read()
print(json.dumps({"type": "thread.started", "thread_id": "thr-fail2"}), flush=True)
print(json.dumps({"type": "turn.failed"}), flush=True)
"""

TOP_ERROR_EXIT_BODY = """
import json, sys
sys.stdin.read()
print(json.dumps({"type": "thread.started", "thread_id": "thr-err"}), flush=True)
print(json.dumps({"type": "error", "message": "usage limit reached"}), flush=True)
sys.exit(1)
"""

BAD_JSON_BODY = """
import json, sys
sys.stdin.read()
print(json.dumps({"type": "thread.started", "thread_id": "thr-bad"}), flush=True)
print("this is not json", flush=True)
print("[]", flush=True)
print("", flush=True)
print(json.dumps({"type": "something.unknown"}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": "not-a-dict"}), flush=True)
"""

NONZERO_BODY = """
import json, sys
sys.stdin.read()
print(json.dumps({"type": "thread.started", "thread_id": "thr-nz"}), flush=True)
sys.stderr.write("boom from codex")
sys.exit(3)
"""

NONZERO_SILENT_BODY = """
import sys
sys.stdin.read()
sys.exit(4)
"""

SLOW_BODY = """
import json, sys, time, signal
sys.stdin.read()
print(json.dumps({"type": "thread.started", "thread_id": "thr-slow"}), flush=True)
def _bye(*a):
    sys.exit(0)
signal.signal(signal.SIGINT, _bye)
for i in range(50):
    print(json.dumps({"type": "item.completed", "item": {"id": f"i{i}", "type": "agent_message", "text": f"chunk {i}"}}), flush=True)
    time.sleep(0.05)
print(json.dumps({"type": "turn.completed", "usage": {}}), flush=True)
"""

STRUCTURED_BODY = """
import json, sys
sys.stdin.read()
print(json.dumps({"type": "thread.started", "thread_id": "thr-schema"}), flush=True)
print(json.dumps({"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": json.dumps({"answer": 42})}}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {}}), flush=True)
"""

STRUCTURED_BAD_BODY = """
import json, sys
sys.stdin.read()
print(json.dumps({"type": "thread.started", "thread_id": "thr-schema-bad"}), flush=True)
print(json.dumps({"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "not json at all"}}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {}}), flush=True)
"""


@pytest.fixture
def codex_argv(make_cli) -> Path:  # type: ignore[no-untyped-def]
    return make_cli("codex-argv", ARGV_DUMP_BODY)


@pytest.fixture
def codex_ok(make_cli) -> Path:  # type: ignore[no-untyped-def]
    return make_cli("codex-ok", SUCCESS_BODY)


@pytest.fixture
def codex_tools(make_cli) -> Path:  # type: ignore[no-untyped-def]
    return make_cli("codex-tools", TOOLS_BODY)


@pytest.fixture
def codex_slow(make_cli) -> Path:  # type: ignore[no-untyped-def]
    return make_cli("codex-slow", SLOW_BODY)


async def _collect(handle: Any) -> list[Any]:
    events: list[Any] = []
    async for ev in handle:
        events.append(ev)
    return events


def _argv_of(result: Any) -> dict[str, Any]:
    return json.loads(result.final_output)


# =============================================================================
# Helpers
# =============================================================================

class TestHelpers:
    def test_toml_string_is_valid_toml_basic_string(self) -> None:
        text = 'Line "one"\nkey = value\tback\\slash é'
        rendered = _toml_string(text)
        assert rendered.startswith('"') and rendered.endswith('"')
        # A TOML basic string decodes with the JSON reader (same escapes).
        assert json.loads(rendered) == text
        assert "\n" not in rendered  # newlines are escaped, never literal

    def test_error_text_shapes(self) -> None:
        assert _error_text({"message": "m"}) == "m"
        assert _error_text({"code": 7}) == '{"code": 7}'
        assert _error_text("plain") == "plain"
        assert _error_text(5) == "5"

    def test_tool_view_unknown_kind(self) -> None:
        assert _tool_view("todo_list", {}) == (None, {}, None)
        assert _tool_view(None, {}) == (None, {}, None)

    def test_tool_view_command_execution_states(self) -> None:
        name, args, out = _tool_view("command_execution", {"command": "ls", "status": "in_progress"})
        assert (name, args, out) == ("command_execution", {"command": "ls"}, None)
        _, _, out = _tool_view("command_execution", {"command": "x", "status": "declined"})
        assert out is not None and out.is_error
        _, _, out = _tool_view("command_execution", {"command": "x", "exit_code": 0, "aggregated_output": "ok"})
        assert out == ToolOutput(text="ok", is_error=False)

    def test_tool_view_file_change_without_status(self) -> None:
        name, args, out = _tool_view("file_change", {})
        assert (name, args, out) == ("file_change", {"changes": []}, None)
        _, _, out = _tool_view("file_change", {"status": "failed"})
        assert out is not None and out.is_error

    def test_tool_view_mcp_shapes(self) -> None:
        name, args, out = _tool_view("mcp_tool_call", {"status": "in_progress"})
        assert (name, args, out) == ("mcp_tool_call", {"arguments": None}, None)
        name, _, out = _tool_view("mcp_tool_call", {"server": "s", "tool": "t", "result": "text-result"})
        assert name == "mcp:s.t"
        assert out == ToolOutput(text="text-result", is_error=False)
        _, _, out = _tool_view("mcp_tool_call", {"tool": "t", "status": "failed", "result": {"a": 1}})
        assert out == ToolOutput(data={"a": 1}, is_error=True)
        _, _, out = _tool_view("mcp_tool_call", {"error": "stringy"})
        assert out == ToolOutput(text="stringy", is_error=True)

    def test_tool_view_web_search(self) -> None:
        assert _tool_view("web_search", {"query": "q"}) == (
            "web_search", {"query": "q"}, ToolOutput(text="")
        )


class TestSession:
    def test_set_id_is_idempotent(self) -> None:
        s = _CodexSession()
        assert s.id == ""
        s._set_id("first")
        s._set_id("second")
        s._set_id("")
        assert s.id == "first"

    @asyncio_test
    async def test_open_and_close(self) -> None:
        backend = CodexExecBackend(cli="/nonexistent")
        s = await backend.open_session(resume_id="abc")
        assert s.id == "abc"
        await s.close()
        s2 = await backend.open_session()
        assert s2.id == ""


# =============================================================================
# argv / stdin construction (through the argv-dumping fake)
# =============================================================================

class TestArgv:
    def test_registered_in_factory(self) -> None:
        assert "codex_exec" in list_backends()
        assert isinstance(get_backend("codex_exec", cli="/x/codex"), CodexExecBackend)

    def test_missing_cli_raises(self, tmp_path: Path) -> None:
        backend = CodexExecBackend(cli=str(tmp_path / "no-such-codex"))
        with pytest.raises(CLIError, match="not found on PATH"):
            backend._build_argv(AgentConfig(name="x"), None)

    def test_tools_and_approval_unsupported(self, codex_ok: Path) -> None:
        backend = CodexExecBackend(cli=str(codex_ok))

        async def handler(_: dict) -> str:
            return "x"

        with pytest.raises(BackendNotImplementedError, match="ToolSpecs"):
            backend.run_stream(
                AgentConfig(name="x", tools=[ToolSpec("t", "d", {}, handler)]), "hi"
            )

        async def approve(_: Any) -> Any:  # pragma: no cover - never called
            return None

        with pytest.raises(BackendNotImplementedError, match="approval"):
            backend.run_stream(AgentConfig(name="x"), "hi", approval=approve)

    @asyncio_test
    async def test_new_session_argv_and_stdin(self, codex_argv: Path) -> None:
        backend = CodexExecBackend(cli=str(codex_argv), env={"TH_TEST_ENV": "backend"})
        cfg = AgentConfig(
            name="x",
            instructions='You are "Kogure".\nline two',
            model="gpt-6-astra",
            max_turns=1,
            builtin_tools=[BuiltinTool("Bash")],
            extra={
                "permission_mode": "bypassPermissions",
                "add_dirs": ["/a", Path("/b")],
                "max_budget_usd": 1.5,
                "disallowed_tools": ["Bash"],
                "settings": "/s.json",
                "cli_args": {"color": "never", "ephemeral": None},
                "env": {"TH_TEST_ENV": "call"},
            },
        )
        result = await backend.run(cfg, "hello codex")
        dump = _argv_of(result)
        argv = dump["argv"]
        assert argv[:3] == ["exec", "--json", "--skip-git-repo-check"]
        assert "resume" not in argv
        i = argv.index("-c")
        assert argv[i + 1] == "developer_instructions=" + json.dumps(cfg.instructions, ensure_ascii=False)
        assert argv[argv.index("-m") + 1] == "gpt-6-astra"
        assert "--dangerously-bypass-approvals-and-sandbox" in argv
        assert argv[argv.index("--add-dir") + 1] == "/a"
        assert argv.count("--add-dir") == 2
        assert "--color" in argv and argv[argv.index("--color") + 1] == "never"
        assert "--ephemeral" in argv
        assert argv[-1] == "-"
        assert "--output-schema" not in argv
        # stdin carried the prompt (newline-terminated); per-call env won.
        assert dump["stdin"] == "hello codex\n"
        assert dump["env"] == "call"
        assert result.stop_reason == "end_turn"
        assert result.cost_usd is None
        assert result.usage == {"input_tokens": 1, "output_tokens": 2}

    @asyncio_test
    async def test_resume_argv_skips_add_dirs_and_schema(
        self, codex_argv: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        backend = CodexExecBackend(cli=str(codex_argv))
        session = await backend.open_session(resume_id="thr-existing")
        cfg = AgentConfig(
            name="x",
            output_schema={"type": "object"},
            extra={"permission_mode": "acceptEdits", "add_dirs": ["/a"]},
        )
        with caplog.at_level(logging.INFO, logger="tigerharness.agent_sdk.backends.codex_exec"):
            result = await backend.run(cfg, "again\n", session=session)
        argv = _argv_of(result)["argv"]
        assert argv[:5] == ["exec", "resume", "thr-existing", "--json", "--skip-git-repo-check"]
        assert "--add-dir" not in argv
        assert "--output-schema" not in argv
        assert argv[argv.index("-c") + 1] == 'sandbox_mode="workspace-write"'
        assert 'approval_policy="never"' in argv
        assert "add_dirs ignored on a resumed thread" in caplog.text
        assert "output_schema ignored on a resumed thread" in caplog.text
        # The session keeps the id it was resumed with.
        assert session.id == "thr-existing"

    @asyncio_test
    async def test_plan_and_default_permission_modes(self, codex_argv: Path) -> None:
        backend = CodexExecBackend(cli=str(codex_argv))
        argv = _argv_of(await backend.run(
            AgentConfig(name="x", extra={"permission_mode": "plan"}), "p"
        ))["argv"]
        assert 'sandbox_mode="read-only"' in argv
        argv = _argv_of(await backend.run(
            AgentConfig(name="x", extra={"permission_mode": "default"}), "p"
        ))["argv"]
        assert "-c" not in argv and "--dangerously-bypass-approvals-and-sandbox" not in argv
        argv = _argv_of(await backend.run(
            AgentConfig(name="x", extra={"permission_mode": "dontAsk"}), "p"
        ))["argv"]
        assert "--dangerously-bypass-approvals-and-sandbox" in argv

    def test_unknown_permission_mode_raises(self, codex_argv: Path) -> None:
        backend = CodexExecBackend(cli=str(codex_argv))
        with pytest.raises(ValueError, match="unknown permission_mode 'yolo'"):
            backend._build_argv(
                AgentConfig(name="x", extra={"permission_mode": "yolo"}), None
            )

    @asyncio_test
    async def test_ignored_knobs_are_logged(
        self, codex_argv: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        backend = CodexExecBackend(cli=str(codex_argv))
        cfg = AgentConfig(
            name="x", max_turns=3, builtin_tools=[BuiltinTool("WebSearch")],
            extra={"max_budget_usd": 2, "settings": "/x"},
        )
        with caplog.at_level(logging.DEBUG, logger="tigerharness.agent_sdk.backends.codex_exec"):
            await backend.run(cfg, "p")
        assert "max_turns=3 has no Codex flag" in caplog.text
        assert "extra['max_budget_usd'] has no Codex flag" in caplog.text
        assert "extra['settings'] has no Codex flag" in caplog.text
        assert "builtin_tools ['WebSearch'] ignored" in caplog.text

    def test_stdin_payload_from_messages(self, codex_argv: Path) -> None:
        backend = CodexExecBackend(cli=str(codex_argv))
        msgs = [
            InputMessage(role="user", content="first"),
            InputMessage(role="assistant", content="ignored"),
            InputMessage(role="user", content=[TextPart("second"), ThinkingPart("skip")]),
            InputMessage(role="user", content="   "),
        ]
        assert backend._build_stdin_payload(msgs) == "first\n\nsecond\n"
        assert backend._build_stdin_payload("x\n") == "x\n"
        with pytest.raises(ValueError, match="no user-role content"):
            backend._build_stdin_payload("   ")
        with pytest.raises(ValueError, match="no user-role content"):
            backend._build_stdin_payload([InputMessage(role="assistant", content="a")])


# =============================================================================
# Streaming / parsing
# =============================================================================

class TestStream:
    @asyncio_test
    async def test_success_events_and_session_capture(self, codex_ok: Path) -> None:
        backend = CodexExecBackend(cli=str(codex_ok))
        session = await backend.open_session()
        handle = backend.run_stream(AgentConfig(name="x", model="m1"), "hi", session=session)
        events = await _collect(handle)
        assert isinstance(events[0], RunStart)
        assert events[0].session_id == "thr-ok" and events[0].model == "m1"
        assert isinstance(events[1], MessageComplete) and events[1].text == "Hello there."
        assert isinstance(events[-1], RunDone)
        assert events[-1].stop_reason == "end_turn"
        assert session.id == "thr-ok"
        result = handle.result
        assert result.final_output == "Hello there."
        assert result.usage == {"input_tokens": 5, "output_tokens": 3}
        assert [m.role for m in result.transcript] == ["user", "assistant"]
        assert result.transcript[0].content == [TextPart("hi")]
        assert result.transcript[1].content == [TextPart("Hello there.")]

    @asyncio_test
    async def test_tool_items_map_to_events(self, codex_tools: Path) -> None:
        backend = CodexExecBackend(cli=str(codex_tools))
        result_events = await _collect(backend.run_stream(AgentConfig(name="x"), "go"))
        kinds = [type(e).__name__ for e in result_events]
        # One RunStart despite the duplicate thread.started.
        assert kinds.count("RunStart") == 1
        thinking = [e for e in result_events if isinstance(e, Thinking)]
        assert thinking[0].text == "Let me think..."
        calls = {e.id: e for e in result_events if isinstance(e, ToolCall)}
        results = {e.id: e for e in result_events if isinstance(e, ToolResult)}
        # command c1: announced once on start, resolved on completion.
        assert calls["c1"].name == "command_execution"
        assert calls["c1"].arguments == {"command": "ls"}
        assert sum(1 for e in result_events if isinstance(e, ToolCall) and e.id == "c1") == 1
        assert results["c1"].output == ToolOutput(text="a\\nb\\n", is_error=False)
        # command c2: completed without a start -> call then result, error.
        assert "c2" in calls and results["c2"].output.is_error
        # file changes: f1 resolves; f2 (no status) is announced only.
        assert results["f1"].output == ToolOutput(text="completed", is_error=False)
        assert "f2" in calls and "f2" not in results
        # mcp: m1 result data; m2 error text, raw (non-dict) arguments wrapped.
        assert calls["m1"].name == "mcp:srv.search"
        assert results["m1"].output == ToolOutput(data={"hits": 2}, is_error=False)
        assert calls["m2"].arguments == {"arguments": "raw"}
        assert results["m2"].output == ToolOutput(text="nope", is_error=True)
        # web search is a one-shot call+result.
        assert results["w1"].output == ToolOutput(text="")
        # the soft error item is a non-fatal ErrorEvent.
        soft = [e for e in result_events if isinstance(e, ErrorEvent)]
        assert soft == [ErrorEvent(message="soft error", fatal=False)]
        # todo_list and the non-dict item are ignored; final message wins.
        assert "t1" not in calls
        done = result_events[-1]
        assert isinstance(done, RunDone) and done.final_output == "Done."
        assert done.stop_reason == "end_turn"

    @asyncio_test
    async def test_tool_transcript_shape(self, codex_tools: Path) -> None:
        backend = CodexExecBackend(cli=str(codex_tools))
        result = await backend.run(AgentConfig(name="x"), "go")
        parts = [p for m in result.transcript for p in m.content]
        assert any(isinstance(p, ThinkingPart) for p in parts)
        uses = [p for p in parts if isinstance(p, ToolUsePart)]
        assert {u.id for u in uses} >= {"c1", "c2", "f1", "f2", "m1", "m2", "w1"}
        res = {p.tool_use_id: p for p in parts if isinstance(p, ToolResultPart)}
        assert res["m1"].content == json.dumps({"hits": 2})
        assert res["c2"].is_error is True

    @asyncio_test
    async def test_turn_failed_is_fatal_error(self, make_cli) -> None:  # type: ignore[no-untyped-def]
        backend = CodexExecBackend(cli=str(make_cli("codex-turnfail", TURN_FAILED_BODY)))
        events = await _collect(backend.run_stream(AgentConfig(name="x"), "go"))
        errs = [e for e in events if isinstance(e, ErrorEvent)]
        assert errs == [ErrorEvent(message="context window exceeded", fatal=True)]
        assert events[-1].stop_reason == "error"

    @asyncio_test
    async def test_turn_failed_without_payload(self, make_cli) -> None:  # type: ignore[no-untyped-def]
        backend = CodexExecBackend(cli=str(make_cli("codex-turnfail2", TURN_FAILED_NO_ERROR_BODY)))
        result = await backend.run(AgentConfig(name="x"), "go")
        assert result.stop_reason == "error"

    @asyncio_test
    async def test_top_level_error_then_nonzero_exit(self, make_cli) -> None:  # type: ignore[no-untyped-def]
        backend = CodexExecBackend(cli=str(make_cli("codex-toperr", TOP_ERROR_EXIT_BODY)))
        events = await _collect(backend.run_stream(AgentConfig(name="x"), "go"))
        errs = [e for e in events if isinstance(e, ErrorEvent)]
        assert errs[0] == ErrorEvent(message="usage limit reached", fatal=True)
        # No stderr, so the exit message falls back to the recorded error.
        assert "exited with code 1: usage limit reached" in errs[1].message
        assert events[-1].stop_reason == "error"

    @asyncio_test
    async def test_bad_json_and_unknown_events(self, make_cli) -> None:  # type: ignore[no-untyped-def]
        backend = CodexExecBackend(cli=str(make_cli("codex-badjson", BAD_JSON_BODY)))
        events = await _collect(backend.run_stream(AgentConfig(name="x"), "go"))
        errs = [e for e in events if isinstance(e, ErrorEvent)]
        assert len(errs) == 1 and not errs[0].fatal and "Bad JSON" in errs[0].message
        done = events[-1]
        assert isinstance(done, RunDone)
        assert done.stop_reason == "end_turn" and done.usage is None

    @asyncio_test
    async def test_nonzero_exit_with_stderr(self, make_cli) -> None:  # type: ignore[no-untyped-def]
        backend = CodexExecBackend(cli=str(make_cli("codex-nz", NONZERO_BODY)))
        events = await _collect(backend.run_stream(AgentConfig(name="x"), "go"))
        errs = [e for e in events if isinstance(e, ErrorEvent)]
        assert errs[-1].fatal and "exited with code 3: boom from codex" in errs[-1].message
        assert events[-1].stop_reason == "error"

    @asyncio_test
    async def test_nonzero_exit_silent(self, make_cli) -> None:  # type: ignore[no-untyped-def]
        backend = CodexExecBackend(cli=str(make_cli("codex-nz2", NONZERO_SILENT_BODY)))
        result = await backend.run(AgentConfig(name="x"), "go")
        assert result.stop_reason == "error" and result.final_output is None

    @asyncio_test
    async def test_structured_output_parsed_and_schema_file_removed(
        self, make_cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        import tempfile
        tempfile.tempdir = None  # re-read TMPDIR
        backend = CodexExecBackend(cli=str(make_cli("codex-schema", STRUCTURED_BODY)))
        cfg = AgentConfig(name="x", output_schema={"type": "object"})
        handle = backend.run_stream(cfg, "go")
        schema_path = handle._schema_path  # type: ignore[attr-defined]
        assert schema_path is not None and schema_path.exists()
        assert json.loads(schema_path.read_text()) == {"type": "object"}
        await _collect(handle)
        assert handle.result.final_output == {"answer": 42}
        assert not schema_path.exists()
        tempfile.tempdir = None

    @asyncio_test
    async def test_structured_output_falls_back_to_text(self, make_cli) -> None:  # type: ignore[no-untyped-def]
        backend = CodexExecBackend(cli=str(make_cli("codex-schema-bad", STRUCTURED_BAD_BODY)))
        cfg = AgentConfig(name="x", output_schema={"type": "object"})
        result = await backend.run(cfg, "go")
        assert result.final_output == "not json at all"

    @asyncio_test
    async def test_result_before_consumption_raises(self, codex_ok: Path) -> None:
        from tigerharness.agent_sdk import StreamNotConsumedError

        backend = CodexExecBackend(cli=str(codex_ok))
        handle = backend.run_stream(AgentConfig(name="x"), "hi")
        with pytest.raises(StreamNotConsumedError):
            _ = handle.result
        await _collect(handle)
        assert handle.is_complete


class TestCancellation:
    @asyncio_test
    async def test_cancel_mid_stream(self, codex_slow: Path) -> None:
        backend = CodexExecBackend(cli=str(codex_slow))
        handle = backend.run_stream(AgentConfig(name="x"), "go")
        t0 = time.monotonic()
        async for ev in handle:
            if isinstance(ev, RunStart):
                await handle.cancel()
                await handle.cancel()  # second call must not raise
        assert time.monotonic() - t0 < 5
        assert handle.result.stop_reason == "interrupted"

    @asyncio_test
    async def test_cancel_before_start_and_after_exit(self, codex_ok: Path) -> None:
        backend = CodexExecBackend(cli=str(codex_ok))
        handle = backend.run_stream(AgentConfig(name="x"), "go")
        await handle.cancel()  # no process yet: no-op
        await _collect(handle)
        await handle.cancel()  # process gone: no-op
        assert handle.result.stop_reason == "interrupted"

    @asyncio_test
    async def test_async_with_break_cleans_up_subprocess(self, codex_slow: Path) -> None:
        backend = CodexExecBackend(cli=str(codex_slow))
        t0 = time.monotonic()
        chunks = 0
        async with backend.run_stream(AgentConfig(name="x"), "go") as handle:
            async for ev in handle:
                if isinstance(ev, MessageComplete):
                    chunks += 1
                    if chunks >= 2:
                        break
        assert chunks == 2
        assert time.monotonic() - t0 < 4
        assert handle._proc is not None and handle._proc.returncode is not None  # type: ignore[attr-defined]


# =============================================================================
# Remaining stream edges
# =============================================================================

STARTED_MESSAGES_BODY = """
import json, sys
sys.stdin.read()
def emit(o): print(json.dumps(o), flush=True)
emit({"type": "thread.started", "thread_id": "thr-started"})
emit({"type": "item.started", "item": {"id": "a", "type": "agent_message", "text": ""}})
emit({"type": "item.started", "item": {"id": "r", "type": "reasoning", "text": ""}})
emit({"type": "item.completed", "item": {"id": "r", "type": "reasoning", "text": "hm"}})
emit({"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "final"}})
emit({"type": "turn.completed", "usage": {}})
"""


class TestStreamEdges:
    @asyncio_test
    async def test_started_messages_are_deferred_until_completed(self, make_cli) -> None:  # type: ignore[no-untyped-def]
        backend = CodexExecBackend(cli=str(make_cli("codex-started", STARTED_MESSAGES_BODY)))
        events = await _collect(backend.run_stream(AgentConfig(name="x"), "go"))
        msgs = [e for e in events if isinstance(e, MessageComplete)]
        thinks = [e for e in events if isinstance(e, Thinking)]
        assert [m.text for m in msgs] == ["final"]
        assert [t.text for t in thinks] == ["hm"]

    @asyncio_test
    async def test_aclose_without_prior_cancel_reaps_subprocess(self, codex_slow: Path) -> None:
        backend = CodexExecBackend(cli=str(codex_slow))
        handle = backend.run_stream(AgentConfig(name="x"), "go")
        async for ev in handle:
            if isinstance(ev, MessageComplete):
                break
        t0 = time.monotonic()
        assert handle._gen is not None  # type: ignore[attr-defined]
        await handle._gen.aclose()  # type: ignore[union-attr,attr-defined]
        assert time.monotonic() - t0 < 4
        assert handle._proc is not None and handle._proc.returncode is not None  # type: ignore[attr-defined]

    @asyncio_test
    async def test_run_without_session_does_not_need_one(self, codex_ok: Path) -> None:
        backend = CodexExecBackend(cli=str(codex_ok))
        result = await backend.run(AgentConfig(name="x"), "hi")
        assert result.final_output == "Hello there."
        assert result.raw is None


class TestReviewPass:
    def test_missing_cli_leaves_no_schema_tempfile(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        import tempfile
        tempfile.tempdir = None
        backend = CodexExecBackend(cli=str(tmp_path / "no-such-codex"))
        with pytest.raises(CLIError):
            backend.run_stream(AgentConfig(name="x", output_schema={"type": "object"}), "hi")
        assert not list(tmp_path.glob("tigerharness-codex-schema-*"))
        tempfile.tempdir = None

    def test_toml_string_escapes_del(self) -> None:
        assert _toml_string("a\x7fb") == '"a\\u007Fb"'


class TestStdinClosedEarly:
    """A CLI that exits before reading its prompt must report its exit,
    not raise asyncio's "Connection lost" from the stdin flush (a race
    that only ever bit on fast CI runners)."""

    @asyncio_test
    async def test_drain_connection_lost_is_not_fatal(self, codex_ok: Path, monkeypatch) -> None:
        import asyncio as _asyncio

        async def lost(self):
            raise ConnectionResetError("Connection lost")
        monkeypatch.setattr(_asyncio.StreamWriter, "drain", lost)
        backend = CodexExecBackend(cli=str(codex_ok))
        result = await backend.run(AgentConfig(name="x"), "hi")
        # The fake still ran to completion (its stdin was closed, which it
        # reads as EOF) and the stream parsed normally.
        assert result.final_output == "Hello there."

    @asyncio_test
    async def test_real_early_exit_with_a_large_prompt(self, make_cli) -> None:  # type: ignore[no-untyped-def]
        cli = make_cli("codex-exit-fast", "import sys; sys.stderr.write('boom'); sys.exit(7)")
        backend = CodexExecBackend(cli=str(cli))
        events = await _collect(backend.run_stream(AgentConfig(name="x"), "x" * (4 * 1024 * 1024)))
        assert events[-1].stop_reason == "error"
        assert any(isinstance(e, ErrorEvent) and "exited with code 7" in e.message for e in events)

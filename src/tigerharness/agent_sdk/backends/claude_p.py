"""Backend that shells out to the Claude Code CLI in headless mode.

Implements ``AgentBackend`` by spawning ``claude -p`` per call, exchanging
newline-delimited JSON over stdin/stdout (the same wire format the official
``claude-agent-sdk`` Python package uses).

Capabilities
------------
- One-shot or multi-turn (via ``--resume <session-id>``).
- Built-in Claude Code tools (Bash, Read, Edit, WebSearch, ...) via
  ``--allowedTools``.
- Cancellation by SIGINT to the subprocess.
- Permission modes via ``cfg.extra["permission_mode"]``.
- Per-call subprocess env additions via ``cfg.extra["env"]`` (a
  ``dict[str, str]``), merged over ``os.environ`` and the backend's
  own ``env`` so a caller can pass turn-scoped context without touching
  the shared process environment.

Limitations
-----------
- User-defined Python tools (``AgentConfig.tools``) are NOT supported here.
  Routing them in would require running an in-process MCP server, which is
  what the ``anthropic_sdk`` backend is for.
- Approval callbacks are NOT supported here for the same reason.

Both unsupported features raise ``BackendNotImplementedError`` so callers
catch the limitation immediately rather than getting silent fallthrough.
"""

from __future__ import annotations

import logging

import asyncio
import json
import os
import shutil
import signal
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from ..errors import BackendNotImplementedError, CLIError
from ..types import (
    AgentConfig,
    ApprovalCallback,
    ContentPart,
    ErrorEvent,
    Event,
    InputMessage,
    MessageComplete,
    NormalizedMessage,
    RunDone,
    RunResult,
    RunStart,
    Session,
    StreamHandle,
    TextPart,
    Thinking,
    ThinkingPart,
    ToolCall,
    ToolOutput,
    ToolResult,
    ToolResultPart,
    ToolUsePart,
)
from ._base import BaseStreamHandle, run_via_stream

log = logging.getLogger("tigerharness.agent_sdk.backends.claude_p")


# ---------- Session ----------

@dataclass
class _ClaudePSession:
    """Session for the claude_p backend.

    A fresh session starts with an empty id; the CLI assigns a real UUID on
    the first turn and we capture it from the ``system.init`` event so that
    subsequent ``run(..., session=session)`` calls pass ``--resume <id>``.
    """

    _id: str = ""

    @property
    def id(self) -> str:
        return self._id

    def _set_id(self, new_id: str) -> None:
        # Internal — invoked by the stream handle once the CLI emits its
        # init event for a brand-new session. Idempotent.
        if not self._id and new_id:
            self._id = new_id

    async def close(self) -> None:
        # Sessions persist on disk under the user's HOME by default; nothing
        # to clean up locally.
        return


# ---------- Backend ----------

class ClaudePBackend:
    """``AgentBackend`` that drives ``claude -p`` as a subprocess."""

    def __init__(
        self,
        *,
        cli: str = "claude",
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self.cli = cli
        self.env = env
        self.cwd = cwd

    # ----- public API -----

    def run_stream(
        self,
        config: AgentConfig,
        prompt: str | list[InputMessage],
        *,
        session: Session | None = None,
        approval: ApprovalCallback | None = None,
    ) -> StreamHandle:
        if config.tools:
            raise BackendNotImplementedError(
                "claude_p backend does not support user-defined ToolSpecs. "
                "Use the 'anthropic_sdk' backend (it can host Python tools "
                "via MCP), or remove cfg.tools."
            )
        if approval is not None:
            raise BackendNotImplementedError(
                "claude_p backend does not support approval callbacks. "
                "Use cfg.extra={'permission_mode': 'acceptEdits'|'plan'|"
                "'bypassPermissions'|'dontAsk'} for coarse policy, or switch "
                "to the 'anthropic_sdk' backend for inline can_use_tool."
            )

        argv = self._build_argv(config, session)
        stdin_payload = self._build_stdin_payload(prompt, session)
        seed_transcript = _input_to_transcript(prompt)
        # Per-call env, most-specific last: process env < backend env <
        # this call's ``cfg.extra["env"]``. The per-call layer is how a
        # caller passes turn-scoped context (e.g. the slack bridge's
        # ``TIGERHARNESS_SLACK_THREAD_TS``) into the subprocess without
        # mutating the shared ``os.environ`` -- each subprocess gets its
        # own dict, so concurrent turns never race.
        call_env = config.extra.get("env") or {}
        return _ClaudePStreamHandle(
            argv=argv,
            stdin_payload=stdin_payload,
            env={**os.environ, **(self.env or {}), **call_env},
            cwd=self.cwd,
            session=session,
            seed_transcript=seed_transcript,
        )

    async def run(
        self,
        config: AgentConfig,
        prompt: str | list[InputMessage],
        *,
        session: Session | None = None,
        approval: ApprovalCallback | None = None,
    ) -> RunResult:
        return await run_via_stream(
            self.run_stream(
                config, prompt, session=session, approval=approval
            )
        )

    async def open_session(
        self, *, resume_id: str | None = None
    ) -> Session:
        return _ClaudePSession(_id=resume_id or "")

    # ----- argv / stdin construction -----

    def _build_argv(
        self, cfg: AgentConfig, session: Session | None
    ) -> list[str]:
        if shutil.which(self.cli) is None and not os.path.isfile(self.cli):
            raise CLIError(
                f"`{self.cli}` not found on PATH. Install Claude Code "
                "(https://docs.claude.com/en/docs/claude-code/quickstart) "
                "or pass `cli=` to ClaudePBackend()."
            )

        argv: list[str] = [
            self.cli,
            "-p",
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--verbose",  # required for stream-json output
        ]

        # System prompt
        if cfg.instructions is not None:
            argv += ["--system-prompt", cfg.instructions]

        # Model
        if cfg.model:
            argv += ["--model", cfg.model]

        # Limits
        if cfg.max_turns is not None:
            argv += ["--max-turns", str(cfg.max_turns)]
        max_budget = cfg.extra.get("max_budget_usd")
        if max_budget is not None:
            argv += ["--max-budget-usd", str(max_budget)]

        # Structured output (JSON Schema)
        schema = _to_json_schema(cfg.output_schema)
        if schema is not None:
            argv += ["--json-schema", json.dumps(schema)]

        # Built-in tools.
        # `--tools <names>` restricts the *available* set of tools.
        # `--allowedTools <names>` also auto-approves them so the headless
        # agent doesn't stall waiting for a permission prompt. We emit both
        # by default; for finer control, set
        #   cfg.extra["cli_args"] = {"allowedTools": "Bash"}
        # or use permission_mode.
        if cfg.builtin_tools:
            names = [b.name for b in cfg.builtin_tools]
            argv += ["--tools", ",".join(names)]
            argv += ["--allowedTools", ",".join(names)]
            for b in cfg.builtin_tools:
                if b.config:
                    raise BackendNotImplementedError(
                        f"BuiltinTool({b.name!r}, config=...) is not "
                        "supported by the claude_p backend. The Claude Code "
                        "CLI configures hosted tools through settings files, "
                        "not flags. Use the 'anthropic_sdk' backend or pass "
                        "your own --settings via cfg.extra['cli_args']."
                    )

        # Permission mode
        permission_mode = cfg.extra.get("permission_mode")
        if permission_mode:
            argv += ["--permission-mode", permission_mode]

        # Resume / session id
        if session is not None and session.id:
            argv += ["--resume", session.id]

        # Working-directory expansions
        for d in cfg.extra.get("add_dirs", []) or []:
            argv += ["--add-dir", str(d)]

        # Disallowed tools (advanced policy)
        disallowed = cfg.extra.get("disallowed_tools") or []
        if disallowed:
            argv += ["--disallowedTools", ",".join(disallowed)]

        # Settings file
        settings_path = cfg.extra.get("settings")
        if settings_path:
            argv += ["--settings", str(settings_path)]

        # Free-form CLI args escape hatch:  cfg.extra = {"cli_args": {"foo": "bar", "flag": None}}
        for flag, value in (cfg.extra.get("cli_args") or {}).items():
            argv.append(f"--{flag}")
            if value is not None:
                argv.append(str(value))

        return argv

    def _build_stdin_payload(
        self,
        prompt: str | list[InputMessage],
        session: Session | None,
    ) -> str:
        """Serialize prompt(s) into newline-delimited stream-json."""
        sid = session.id if session is not None and session.id else None
        lines: list[str] = []

        def _emit_user(text: str) -> None:
            msg = {
                "type": "user",
                "message": {"role": "user", "content": text},
                "parent_tool_use_id": None,
            }
            if sid:
                msg["session_id"] = sid
            lines.append(json.dumps(msg))

        if isinstance(prompt, str):
            if not prompt.strip():
                raise ValueError(
                    "prompt cannot be empty or whitespace-only"
                )
            _emit_user(prompt)
        else:
            # We only forward user-role content to the CLI on stdin. History
            # for other roles must come from `session` (resume).
            for m in prompt:
                if m.role != "user":
                    continue
                text = _input_to_text(m.content)
                if text.strip():
                    _emit_user(text)

        if not lines:
            raise ValueError(
                "prompt produced no user-role content to send. Pass a "
                "non-empty string, or include at least one InputMessage with "
                "role='user' and non-empty content."
            )
        return "\n".join(lines) + "\n"


def _input_to_text(content: str | list[ContentPart]) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for p in content:
        if isinstance(p, TextPart):
            parts.append(p.text)
        elif isinstance(p, ThinkingPart):
            # Don't forward thinking content to the model.
            continue
        else:
            # ToolUsePart / ToolResultPart in user input is unusual — skip.
            continue
    return "\n\n".join(parts)


def _input_to_transcript(
    prompt: str | list[InputMessage],
) -> list[NormalizedMessage]:
    """Normalize the caller-supplied prompt for inclusion in the transcript.

    The CLI doesn't echo the user's input back, so we seed the transcript
    here. This matches OpenAI's ``RunResult.to_input_list()`` behaviour
    (which includes the user input).
    """
    if isinstance(prompt, str):
        return [NormalizedMessage(role="user", content=[TextPart(text=prompt)])]

    seeded: list[NormalizedMessage] = []
    for m in prompt:
        if isinstance(m.content, str):
            content_list: list[ContentPart] = [TextPart(text=m.content)]
        else:
            content_list = list(m.content)
        seeded.append(NormalizedMessage(role=m.role, content=content_list))
    return seeded


def _to_json_schema(value: Any) -> dict[str, Any] | None:
    """Coerce ``output_schema`` to a JSON Schema dict."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    # Pydantic v2
    model_json_schema = getattr(value, "model_json_schema", None)
    if callable(model_json_schema):
        return model_json_schema()  # type: ignore[no-any-return]
    # Pydantic v1
    schema_json = getattr(value, "schema", None)
    if callable(schema_json):
        try:
            return schema_json()  # type: ignore[no-any-return]
        except TypeError:
            pass
    raise TypeError(
        f"AgentConfig.output_schema must be a JSON Schema dict or a pydantic "
        f"model; got {type(value).__name__}."
    )


# ---------- Stream handle ----------

class _ClaudePStreamHandle(BaseStreamHandle):
    """Async iterator over events emitted by `claude -p`."""

    def __init__(
        self,
        *,
        argv: list[str],
        stdin_payload: str,
        env: dict[str, str],
        cwd: str | None,
        session: Session | None,
        seed_transcript: list[NormalizedMessage],
    ) -> None:
        super().__init__()
        self._argv = argv
        self._stdin_payload = stdin_payload
        self._env = env
        self._cwd = cwd
        self._session = session
        self._seed_transcript = seed_transcript
        self._proc: asyncio.subprocess.Process | None = None
        self._cancelled = False
        self._start(self._iter())

    async def cancel(self, *, after_turn: bool = False) -> None:
        # `after_turn` is a hint; the CLI doesn't expose explicit turn-edge
        # cancellation. SIGINT lets it shut down cleanly either way.
        self._cancelled = True
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.send_signal(signal.SIGINT)
        except ProcessLookupError:  # pragma: no cover
            return
        # Escalate after a grace period if it doesn't exit.
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:  # pragma: no cover
            try:
                proc.terminate()
            except ProcessLookupError:
                pass

    async def _iter(self) -> AsyncIterator[Event]:
        # Raise the StreamReader buffer limit from the default 64 KB to
        # 10 MB.  claude -p emits one JSON object per line; large tool
        # results or image-heavy system prompts can easily exceed 64 KB.
        _STREAM_LIMIT = 10 * 1024 * 1024  # 10 MB

        log.info("spawning %s (cwd=%s)", self._argv[0], self._cwd)
        proc = await asyncio.create_subprocess_exec(
            *self._argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
            cwd=self._cwd,
            limit=_STREAM_LIMIT,
        )
        self._proc = proc

        # Drain stderr concurrently to avoid deadlock when the CLI emits
        # more than ~64 KB of stderr while we're still reading stdout.
        stderr_chunks: list[bytes] = []

        async def _drain_stderr() -> None:
            assert proc.stderr is not None
            try:
                while True:
                    chunk = await proc.stderr.read(4096)
                    if not chunk:
                        return
                    stderr_chunks.append(chunk)
            except (asyncio.CancelledError, Exception):  # pragma: no cover
                return

        stderr_task = asyncio.create_task(_drain_stderr())

        # Send the prompt and close stdin so the CLI knows we're done.
        try:
            assert proc.stdin is not None
            proc.stdin.write(self._stdin_payload.encode("utf-8"))
            await proc.stdin.drain()
        finally:
            try:
                proc.stdin.close()  # type: ignore[union-attr]
            except Exception:  # pragma: no cover
                pass

        # Seed the transcript with the caller's input so it doesn't get
        # silently dropped (the CLI only echoes back assistant + tool turns).
        transcript: list[NormalizedMessage] = list(self._seed_transcript)
        emitted_run_start = False
        session_id: str | None = None
        model: str | None = None
        final_output: Any = None
        stop_reason: str = "end_turn"
        usage: dict[str, Any] | None = None
        cost_usd: float | None = None
        # Track tool-call names so we can attach them to ToolResult events
        # (the `user` tool_result blocks only carry tool_use_id, not name).
        tool_call_names: dict[str, str] = {}

        try:
            assert proc.stdout is not None
            async for raw_line in proc.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError as exc:
                    yield ErrorEvent(
                        message=f"Bad JSON from claude CLI: {exc}",
                        fatal=False,
                    )
                    continue

                t = msg.get("type")

                if t == "system":  # pragma: no branch  # first msg is always system/init
                    if msg.get("subtype") == "init" and not emitted_run_start:  # pragma: no branch  # only one init per stream
                        session_id = msg.get("session_id") or session_id
                        model = msg.get("model") or model
                        if isinstance(self._session, _ClaudePSession) and session_id:
                            self._session._set_id(session_id)
                        emitted_run_start = True
                        yield RunStart(session_id=session_id, model=model)

                elif t == "assistant":
                    blocks = (msg.get("message") or {}).get("content") or []
                    text_parts: list[str] = []
                    follow_up: list[Event] = []
                    norm: list[ContentPart] = []
                    for blk in blocks:
                        bt = blk.get("type")
                        if bt == "text":
                            text_parts.append(blk.get("text", ""))
                            norm.append(TextPart(text=blk.get("text", "")))
                        elif bt == "thinking":
                            follow_up.append(Thinking(text=blk.get("thinking", "")))
                            norm.append(ThinkingPart(text=blk.get("thinking", "")))
                        elif bt == "tool_use":
                            call_id = blk.get("id", "")
                            call_name = blk.get("name", "")
                            call_args = blk.get("input") or {}
                            tool_call_names[call_id] = call_name
                            follow_up.append(
                                ToolCall(
                                    id=call_id,
                                    name=call_name,
                                    arguments=call_args,
                                )
                            )
                            norm.append(
                                ToolUsePart(
                                    id=call_id,
                                    name=call_name,
                                    input=call_args,
                                )
                            )
                    if text_parts:
                        yield MessageComplete(text="".join(text_parts))
                    for ev in follow_up:
                        yield ev
                    if norm:
                        transcript.append(
                            NormalizedMessage(role="assistant", content=norm)
                        )

                elif t == "user":
                    blocks = (msg.get("message") or {}).get("content") or []
                    user_norm: list[ContentPart] = []
                    if isinstance(blocks, str):
                        user_norm.append(TextPart(text=blocks))
                    else:
                        for blk in blocks:
                            bt = blk.get("type")
                            if bt == "tool_result":
                                tu_id = blk.get("tool_use_id", "")
                                content = blk.get("content")
                                is_err = bool(blk.get("is_error", False))
                                yield ToolResult(
                                    id=tu_id,
                                    name=tool_call_names.get(tu_id, ""),
                                    output=ToolOutput(
                                        text=content if isinstance(content, str) else None,
                                        data=content if not isinstance(content, str) else None,
                                        is_error=is_err,
                                    ),
                                )
                                user_norm.append(
                                    ToolResultPart(
                                        tool_use_id=tu_id,
                                        content=(
                                            content
                                            if isinstance(content, (str, list))
                                            else str(content)
                                        ),
                                        is_error=is_err,
                                    )
                                )
                            elif bt == "text":  # pragma: no branch  # CLI block types exhaustive
                                user_norm.append(TextPart(text=blk.get("text", "")))
                    if user_norm:  # pragma: no branch  # user msgs always have content blocks
                        transcript.append(
                            NormalizedMessage(role="user", content=user_norm)
                        )

                elif t == "result":
                    sub = msg.get("subtype") or "success"
                    if sub == "success":
                        stop_reason = "end_turn"
                    elif sub == "error_max_turns":
                        stop_reason = "max_turns"
                    elif sub == "error_max_budget_usd":
                        stop_reason = "max_budget"
                    else:
                        stop_reason = "error"
                    final_output = msg.get("structured_output", msg.get("result"))
                    usage = msg.get("usage")
                    cost_usd = msg.get("total_cost_usd")
                    if not session_id:
                        session_id = msg.get("session_id")
                        if isinstance(self._session, _ClaudePSession) and session_id:  # pragma: no branch  # session always set by init msg
                            self._session._set_id(session_id)

                # Other types ("stream_event", etc.) are ignored quietly.

            await proc.wait()

        except (asyncio.CancelledError, GeneratorExit):
            # Either the surrounding task was cancelled, or our generator's
            # `aclose()` was called (e.g. from `async with` cleanup).
            self._cancelled = True
            raise

        finally:
            # Always reap the subprocess, even on early break / cancel.
            if proc.returncode is None:
                try:
                    proc.send_signal(signal.SIGINT)
                except ProcessLookupError:  # pragma: no cover
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3.0)
                except asyncio.TimeoutError:  # pragma: no cover
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass
                    try:
                        await proc.wait()
                    except Exception:
                        pass
                except Exception:  # pragma: no cover
                    pass

            # Stop the stderr drainer.
            stderr_task.cancel()
            try:
                await stderr_task
            except (asyncio.CancelledError, Exception):  # pragma: no cover
                pass

        stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")

        # Fail loudly on non-zero exits unless we were cancelled (-2 = SIGINT).
        if proc.returncode not in (0, -2, None):
            log.warning("%s exited nonzero: code=%s stderr=%.500s",
                        self._argv[0], proc.returncode, stderr_text.strip())
            yield ErrorEvent(
                message=(
                    f"`{self._argv[0]}` exited with code {proc.returncode}: "
                    f"{stderr_text.strip()}"
                ),
                fatal=True,
            )
            stop_reason = "interrupted" if self._cancelled else "error"
        elif self._cancelled:
            stop_reason = "interrupted"

        self._result = RunResult(
            final_output=final_output,
            transcript=transcript,
            stop_reason=stop_reason,  # type: ignore[arg-type]
            usage=usage,
            cost_usd=cost_usd,
            raw=None,
        )
        yield RunDone(
            final_output=final_output,
            stop_reason=stop_reason,  # type: ignore[arg-type]
            usage=usage,
            cost_usd=cost_usd,
        )

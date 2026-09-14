"""Backend that shells out to OpenAI's Codex CLI in headless mode.

Implements ``AgentBackend`` by spawning ``codex exec --json`` per call
(``codex exec resume <thread-id> --json`` for a later turn of the same
session), sending the prompt on stdin and reading Codex's JSONL event
stream on stdout. It is the ``chatgpt`` vendor of
:mod:`tigerharness.vendors`: the same shape as ``claude_p`` -- a
subscription-billed agentic CLI driven as a subprocess -- so every
harness consumer (Slack bridge, autodrive, idle compaction) treats the
two interchangeably.

Capabilities
------------
- One-shot or multi-turn (via ``codex exec resume <thread-id>``; the id
  is captured from the ``thread.started`` event).
- ``AgentConfig.instructions`` becomes Codex's ``developer_instructions``
  config override (``-c developer_instructions=<TOML string>``), which
  Codex layers on top of its own base instructions -- the same role
  ``--system-prompt`` plays for ``claude -p``.
- ``AgentConfig.model`` -> ``-m <model>``.
- ``cfg.extra["permission_mode"]`` maps the Claude Code vocabulary the
  harness already speaks onto Codex's sandbox/approval knobs:
  ``bypassPermissions`` / ``dontAsk`` -> ``--dangerously-bypass-approvals-
  and-sandbox`` (the unattended mode every harness daemon runs in);
  ``acceptEdits`` -> ``workspace-write`` sandbox; ``plan`` -> ``read-only``
  sandbox; ``default`` / unset -> Codex's own ``exec`` defaults.
- ``cfg.extra["add_dirs"]`` -> ``--add-dir`` (new sessions only; a resumed
  thread keeps the roots it was opened with).
- ``AgentConfig.output_schema`` -> ``--output-schema <tempfile>`` (new
  sessions only); the final message is then parsed as JSON.
- ``cfg.extra["env"]`` per-call subprocess env additions, merged over
  ``os.environ`` and the backend's own ``env`` exactly as ``claude_p``.
- ``cfg.extra["cli_args"]`` free-form ``{flag: value}`` passthrough.
- Cancellation by SIGINT to the subprocess.

Ignored knobs (logged at DEBUG, never raised, so a config written for
``claude_p`` runs unchanged): ``max_turns``, ``max_budget_usd``,
``disallowed_tools``, ``settings``, and ``builtin_tools`` -- Codex has no
per-run flag for any of them (its tool surface is fixed; its spend is the
ChatGPT subscription).

Limitations
-----------
- User-defined Python tools (``AgentConfig.tools``) and approval callbacks
  raise ``BackendNotImplementedError``, as in ``claude_p``.
- Codex sessions live under ``$CODEX_HOME/sessions`` (``~/.codex`` by
  default); the tiger-memory ``claude_transcript`` source does not read
  them, so a Codex-driven persona's Slack turns are not yet ingested
  into its memory from the transcript rail (the journal worklog rail is
  unaffected).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
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
from .claude_p import _input_to_text, _input_to_transcript, _to_json_schema

log = logging.getLogger("tigerharness.agent_sdk.backends.codex_exec")


#: Claude Code permission-mode vocabulary -> the codex argv that means
#: the same thing. ``None`` entries add nothing (Codex's exec defaults).
_PERMISSION_ARGV: dict[str, list[str]] = {
    "bypassPermissions": ["--dangerously-bypass-approvals-and-sandbox"],
    "dontAsk": ["--dangerously-bypass-approvals-and-sandbox"],
    "acceptEdits": [
        "-c", 'sandbox_mode="workspace-write"',
        "-c", 'approval_policy="never"',
    ],
    "plan": [
        "-c", 'sandbox_mode="read-only"',
        "-c", 'approval_policy="never"',
    ],
    "default": [],
}

#: Knobs ``claude_p`` honours that Codex has no flag for. Each is logged
#: (at DEBUG -- the persona router sets two of them on every new thread,
#: so INFO would be noise) once per call when set, so an operator
#: raising the log level can see the setting was dropped rather than
#: silently applied.
_IGNORED_EXTRA_KEYS = ("max_budget_usd", "disallowed_tools", "settings")


def _toml_string(text: str) -> str:
    """Render *text* as a TOML basic string.

    ``codex -c key=value`` parses the value as TOML. JSON's string
    escaping is a strict subset of TOML's basic-string escapes (``\\n``,
    ``\\t``, ``\\"``, ``\\\\``, ``\\uXXXX``; JSON never emits ``\\/``), so
    ``json.dumps`` produces a valid TOML basic string for any input,
    including multi-paragraph persona prompts with quotes and ``=`` --
    with one gap closed here: TOML also forbids a raw DEL (U+007F), which
    JSON leaves unescaped.
    """
    return json.dumps(text, ensure_ascii=False).replace("\x7f", "\\u007F")


# ---------- Session ----------

@dataclass
class _CodexSession:
    """Session for the codex_exec backend.

    A fresh session starts with an empty id; Codex assigns a thread id on
    the first turn (the ``thread.started`` event) and later
    ``run(..., session=session)`` calls become ``codex exec resume <id>``.
    """

    _id: str = ""

    @property
    def id(self) -> str:
        return self._id

    def _set_id(self, new_id: str) -> None:
        if not self._id and new_id:
            self._id = new_id

    async def close(self) -> None:
        # Threads persist under $CODEX_HOME/sessions; nothing local to free.
        return


# ---------- Backend ----------

class CodexExecBackend:
    """``AgentBackend`` that drives ``codex exec`` as a subprocess."""

    def __init__(
        self,
        *,
        cli: str = "codex",
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
                "codex_exec backend does not support user-defined ToolSpecs. "
                "Codex exposes its own tool surface; remove cfg.tools."
            )
        if approval is not None:
            raise BackendNotImplementedError(
                "codex_exec backend does not support approval callbacks. "
                "Use cfg.extra={'permission_mode': 'acceptEdits'|'plan'|"
                "'bypassPermissions'} for coarse policy."
            )

        self._check_cli()  # before any temp file exists, so a missing CLI leaks nothing
        schema_path = self._write_schema(config, session)
        argv = self._build_argv(config, session, schema_path=schema_path)
        stdin_payload = self._build_stdin_payload(prompt)
        seed_transcript = _input_to_transcript(prompt)
        call_env = config.extra.get("env") or {}
        return _CodexStreamHandle(
            argv=argv,
            stdin_payload=stdin_payload,
            env={**os.environ, **(self.env or {}), **call_env},
            cwd=self.cwd,
            session=session,
            seed_transcript=seed_transcript,
            model=config.model,
            structured=schema_path is not None,
            schema_path=schema_path,
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
            self.run_stream(config, prompt, session=session, approval=approval)
        )

    async def open_session(self, *, resume_id: str | None = None) -> Session:
        return _CodexSession(_id=resume_id or "")

    # ----- argv / stdin construction -----

    @staticmethod
    def _is_resume(session: Session | None) -> bool:
        return session is not None and bool(session.id)

    def _write_schema(
        self, cfg: AgentConfig, session: Session | None
    ) -> Path | None:
        """Materialise ``output_schema`` for ``--output-schema``. Codex
        reads the file at startup; the stream handle unlinks it after
        the process exits. Resumed threads cannot change their schema,
        so none is written for them."""
        schema = _to_json_schema(cfg.output_schema)
        if schema is None:
            return None
        if self._is_resume(session):
            log.info(
                "codex_exec: output_schema ignored on a resumed thread "
                "(Codex fixes the schema when the thread is opened)"
            )
            return None
        fd, name = tempfile.mkstemp(prefix="tigerharness-codex-schema-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(schema, fh)
        return Path(name)

    def _check_cli(self) -> None:
        if shutil.which(self.cli) is None and not os.path.isfile(self.cli):
            raise CLIError(
                f"`{self.cli}` not found on PATH. Install the Codex CLI "
                "(https://github.com/openai/codex) and sign in with "
                "`codex login`, or pass `cli=` to CodexExecBackend()."
            )

    def _build_argv(
        self,
        cfg: AgentConfig,
        session: Session | None,
        *,
        schema_path: Path | None = None,
    ) -> list[str]:
        self._check_cli()

        resume = self._is_resume(session)
        argv: list[str] = [self.cli, "exec"]
        if resume:
            assert session is not None
            argv += ["resume", session.id]
        # `--json` is the event stream we parse; `--skip-git-repo-check`
        # because a team root or a scratch cwd is not necessarily a git
        # checkout and the daemon must never stall on that prompt.
        argv += ["--json", "--skip-git-repo-check"]

        if cfg.instructions is not None:
            argv += ["-c", f"developer_instructions={_toml_string(cfg.instructions)}"]

        if cfg.model:
            argv += ["-m", cfg.model]

        permission_mode = cfg.extra.get("permission_mode")
        if permission_mode:
            try:
                argv += _PERMISSION_ARGV[permission_mode]
            except KeyError:
                raise ValueError(
                    f"codex_exec: unknown permission_mode {permission_mode!r}; "
                    f"accepted: {', '.join(sorted(_PERMISSION_ARGV))}"
                ) from None

        if schema_path is not None:
            argv += ["--output-schema", str(schema_path)]

        add_dirs = cfg.extra.get("add_dirs", []) or []
        if add_dirs:
            if resume:
                log.info(
                    "codex_exec: add_dirs ignored on a resumed thread "
                    "(the thread keeps the roots it was opened with)"
                )
            else:
                for d in add_dirs:
                    argv += ["--add-dir", str(d)]

        # Knobs with no Codex equivalent: say so in the log, then carry on.
        if cfg.max_turns is not None:
            log.debug("codex_exec: max_turns=%s has no Codex flag; ignored", cfg.max_turns)
        for key in _IGNORED_EXTRA_KEYS:
            if cfg.extra.get(key):
                log.debug("codex_exec: extra[%r] has no Codex flag; ignored", key)
        if cfg.builtin_tools:
            log.debug(
                "codex_exec: builtin_tools %s ignored (Codex's tool surface is fixed)",
                [b.name for b in cfg.builtin_tools],
            )

        for flag, value in (cfg.extra.get("cli_args") or {}).items():
            argv.append(f"--{flag}")
            if value is not None:
                argv.append(str(value))

        # Read the prompt from stdin: keeps a long prompt out of argv and
        # off `ps`, exactly like the claude_p stream-json stdin payload.
        argv.append("-")
        return argv

    def _build_stdin_payload(self, prompt: str | list[InputMessage]) -> str:
        """The prompt text Codex reads from stdin. Only user-role content
        is forwarded; prior assistant turns come from the resumed thread."""
        if isinstance(prompt, str):
            text = prompt
        else:
            parts = [
                _input_to_text(m.content) for m in prompt if m.role == "user"
            ]
            text = "\n\n".join(p for p in parts if p.strip())
        if not text.strip():
            raise ValueError(
                "prompt produced no user-role content to send. Pass a "
                "non-empty string, or include at least one InputMessage with "
                "role='user' and non-empty content."
            )
        return text if text.endswith("\n") else text + "\n"


# ---------- Stream handle ----------

class _CodexStreamHandle(BaseStreamHandle):
    """Async iterator over the JSONL events emitted by ``codex exec --json``.

    Event vocabulary (Codex CLI 0.15x): ``thread.started`` (carries
    ``thread_id``), ``turn.started``, ``item.started`` / ``item.updated``
    / ``item.completed`` (each wrapping an ``item`` whose ``type`` is one
    of ``agent_message``, ``reasoning``, ``command_execution``,
    ``file_change``, ``mcp_tool_call``, ``web_search``, ``todo_list``,
    ``error``), ``turn.completed`` (carries ``usage``), ``turn.failed``
    (carries ``error``), and a top-level ``error``.
    """

    def __init__(
        self,
        *,
        argv: list[str],
        stdin_payload: str,
        env: dict[str, str],
        cwd: str | None,
        session: Session | None,
        seed_transcript: list[NormalizedMessage],
        model: str | None,
        structured: bool,
        schema_path: Path | None,
    ) -> None:
        super().__init__()
        self._argv = argv
        self._stdin_payload = stdin_payload
        self._env = env
        self._cwd = cwd
        self._session = session
        self._seed_transcript = seed_transcript
        self._model = model
        self._structured = structured
        self._schema_path = schema_path
        self._proc: asyncio.subprocess.Process | None = None
        self._cancelled = False
        self._start(self._iter())

    async def cancel(self, *, after_turn: bool = False) -> None:
        self._cancelled = True
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.send_signal(signal.SIGINT)
        except ProcessLookupError:  # pragma: no cover
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:  # pragma: no cover
            try:
                proc.terminate()
            except ProcessLookupError:
                pass

    async def _iter(self) -> AsyncIterator[Event]:
        _STREAM_LIMIT = 10 * 1024 * 1024  # 10 MB, as claude_p

        log.info("spawning %s exec (cwd=%s)", self._argv[0], self._cwd)
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

        try:
            assert proc.stdin is not None
            proc.stdin.write(self._stdin_payload.encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            # The CLI exited (or closed stdin) before reading the prompt --
            # a bad flag, a missing login, an instant crash. Not fatal here:
            # the nonzero exit and its stderr are reported below, and a
            # fast exit must not turn into a "Connection lost" traceback
            # that hides the real error (seen as a CI-only race).
            log.info(
                "%s closed stdin before the prompt was delivered",
                self._argv[0],
            )
        finally:
            try:
                proc.stdin.close()  # type: ignore[union-attr]
            except Exception:  # pragma: no cover
                pass

        transcript: list[NormalizedMessage] = list(self._seed_transcript)
        emitted_run_start = False
        session_id: str | None = None
        last_message: str | None = None
        stop_reason: str = "end_turn"
        usage: dict[str, Any] | None = None
        fatal_error: str | None = None
        # Item ids whose ToolCall has been emitted (so a completed item
        # that never showed a started event still gets its call first,
        # and one that did is not announced twice).
        announced: set[str] = set()

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
                        message=f"Bad JSON from codex CLI: {exc}", fatal=False
                    )
                    continue
                if not isinstance(msg, dict):
                    continue

                t = msg.get("type")

                if t == "thread.started":
                    session_id = msg.get("thread_id") or session_id
                    if isinstance(self._session, _CodexSession) and session_id:
                        self._session._set_id(session_id)
                    if not emitted_run_start:
                        emitted_run_start = True
                        yield RunStart(session_id=session_id, model=self._model)

                elif t in ("item.started", "item.updated", "item.completed"):
                    item = msg.get("item") or {}
                    if not isinstance(item, dict):
                        continue
                    itype = item.get("type")
                    item_id = str(item.get("id", ""))
                    completed = t == "item.completed"

                    if itype == "agent_message":
                        if completed:
                            text = item.get("text", "") or ""
                            last_message = text
                            yield MessageComplete(text=text)
                            transcript.append(
                                NormalizedMessage(
                                    role="assistant", content=[TextPart(text=text)]
                                )
                            )

                    elif itype == "reasoning":
                        if completed:
                            text = item.get("text", "") or ""
                            yield Thinking(text=text)
                            transcript.append(
                                NormalizedMessage(
                                    role="assistant",
                                    content=[ThinkingPart(text=text)],
                                )
                            )

                    elif itype == "error":
                        yield ErrorEvent(
                            message=str(item.get("message", "codex error")),
                            fatal=False,
                        )

                    else:
                        name, args, output = _tool_view(itype, item)
                        if name is None:
                            continue  # todo_list, context_compaction, ...
                        if item_id not in announced:
                            announced.add(item_id)
                            yield ToolCall(id=item_id, name=name, arguments=args)
                            transcript.append(
                                NormalizedMessage(
                                    role="assistant",
                                    content=[
                                        ToolUsePart(id=item_id, name=name, input=args)
                                    ],
                                )
                            )
                        if completed and output is not None:
                            yield ToolResult(id=item_id, name=name, output=output)
                            result_part: list[ContentPart] = [
                                ToolResultPart(
                                    tool_use_id=item_id,
                                    content=(
                                        output.text
                                        if output.text is not None
                                        else json.dumps(output.data)
                                    ),
                                    is_error=output.is_error,
                                )
                            ]
                            transcript.append(
                                NormalizedMessage(role="user", content=result_part)
                            )

                elif t == "turn.completed":
                    raw_usage = msg.get("usage")
                    usage = raw_usage if isinstance(raw_usage, dict) else None
                    stop_reason = "end_turn"

                elif t == "turn.failed":
                    err = msg.get("error")
                    message = _error_text(err) if err else "turn failed"
                    fatal_error = message
                    stop_reason = "error"
                    yield ErrorEvent(message=message, fatal=True)

                elif t == "error":
                    message = str(msg.get("message") or "codex error")
                    fatal_error = message
                    stop_reason = "error"
                    yield ErrorEvent(message=message, fatal=True)

                # thread.started handled above; turn.started and unknown
                # event types are ignored quietly.

            await proc.wait()

        except (asyncio.CancelledError, GeneratorExit):
            self._cancelled = True
            raise

        finally:
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

            stderr_task.cancel()
            try:
                await stderr_task
            except (asyncio.CancelledError, Exception):  # pragma: no cover
                pass

            if self._schema_path is not None:
                try:
                    self._schema_path.unlink()
                except OSError:  # pragma: no cover
                    pass

        stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")

        if proc.returncode not in (0, -2, None):
            log.warning("%s exited nonzero: code=%s stderr=%.500s",
                        self._argv[0], proc.returncode, stderr_text.strip())
            yield ErrorEvent(
                message=(
                    f"`{self._argv[0]}` exited with code {proc.returncode}: "
                    f"{stderr_text.strip() or fatal_error or ''}"
                ),
                fatal=True,
            )
            stop_reason = "interrupted" if self._cancelled else "error"
        elif self._cancelled:
            stop_reason = "interrupted"

        final_output: Any = last_message
        if self._structured and isinstance(last_message, str):
            try:
                final_output = json.loads(last_message)
            except json.JSONDecodeError:
                final_output = last_message

        self._result = RunResult(
            final_output=final_output,
            transcript=transcript,
            stop_reason=stop_reason,  # type: ignore[arg-type]
            usage=usage,
            cost_usd=None,  # subscription-billed; Codex reports no dollars
            raw=None,
        )
        yield RunDone(
            final_output=final_output,
            stop_reason=stop_reason,  # type: ignore[arg-type]
            usage=usage,
            cost_usd=None,
        )


def _error_text(err: Any) -> str:
    """A Codex error payload (a dict with ``message``, or anything
    else) as one line of text."""
    if isinstance(err, dict):
        msg = err.get("message")
        return str(msg) if msg is not None else json.dumps(err)
    return str(err)


def _tool_view(
    itype: str | None, item: dict[str, Any]
) -> tuple[str | None, dict[str, Any], ToolOutput | None]:
    """Project a Codex tool-shaped item onto ``(name, arguments, output)``.

    ``name`` is ``None`` for item kinds that are not tool calls (the
    caller skips them). ``output`` is ``None`` while the item is still in
    progress; on a completed item it is the ``ToolOutput`` to report.
    """
    if itype == "command_execution":
        args = {"command": item.get("command", "")}
        exit_code = item.get("exit_code")
        status = item.get("status")
        if status in ("completed", "failed", "declined") or exit_code is not None:
            output = ToolOutput(
                text=item.get("aggregated_output", "") or "",
                is_error=bool(exit_code) or status in ("failed", "declined"),
            )
        else:
            output = None
        return "command_execution", args, output

    if itype == "file_change":
        args = {"changes": item.get("changes") or []}
        status = item.get("status")
        output = (
            ToolOutput(text=str(status), is_error=status not in ("completed", None))
            if status is not None
            else None
        )
        return "file_change", args, output

    if itype == "mcp_tool_call":
        server = item.get("server", "")
        tool = item.get("tool", "")
        raw_args = item.get("arguments")
        args = raw_args if isinstance(raw_args, dict) else {"arguments": raw_args}
        status = item.get("status")
        if status in ("completed", "failed") or "result" in item or "error" in item:
            err = item.get("error")
            result = item.get("result")
            if err:
                output = ToolOutput(text=_error_text(err), is_error=True)
            elif isinstance(result, str):
                output = ToolOutput(text=result, is_error=status == "failed")
            else:
                output = ToolOutput(data=result, is_error=status == "failed")
        else:
            output = None
        return f"mcp:{server}.{tool}" if server or tool else "mcp_tool_call", args, output

    if itype == "web_search":
        return "web_search", {"query": item.get("query", "")}, ToolOutput(text="")

    return None, {}, None

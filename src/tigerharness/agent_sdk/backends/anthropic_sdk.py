"""Backend over Anthropic's official ``claude-agent-sdk`` Python package.

Installs as an optional extra:

    pip install tigerharness[anthropic]

Uses ``ClaudeSDKClient`` (the streaming, multi-turn client) under the
hood so we get cancellation (``interrupt()``) and session resume for
free. Translation between ``AgentConfig`` and ``ClaudeAgentOptions`` is
direct: ``instructions`` -> ``system_prompt``, ``model`` -> ``model``,
``builtin_tools`` -> ``allowed_tools``, ``max_turns`` -> ``max_turns``,
and any ``extra`` keys whose names match ``ClaudeAgentOptions`` fields
get passed through as-is (``permission_mode``, ``cwd``,
``disallowed_tools``, ``add_dirs``, ``max_budget_usd``, etc.).

Custom function tools (``ToolSpec``) are intentionally NOT wired yet --
``claude-agent-sdk`` exposes them through MCP servers built via
``create_sdk_mcp_server``, which is a more invasive translation. If
``cfg.tools`` is non-empty we raise so callers don't silently lose
their tools. Use ``builtin_tools`` for now or open a PR.
"""

from __future__ import annotations

import logging

from collections.abc import AsyncIterator
from typing import Any

from ..errors import BackendNotImplementedError
from ..types import (
    AgentConfig,
    ApprovalCallback,
    ApprovalDecision,
    ApprovalRequest,
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

log = logging.getLogger("tigerharness.agent_sdk.backends.anthropic_sdk")


# ---------------------------------------------------------------------------
# Lazy import: claude-agent-sdk is an optional dep
# ---------------------------------------------------------------------------

def _import_sdk():
    """Import claude_agent_sdk lazily so the rest of agent_sdk works
    without the optional extra installed.

    Raises BackendNotImplementedError with install instructions if the
    package is missing.
    """
    try:
        import claude_agent_sdk
    except ImportError as exc:
        raise BackendNotImplementedError(
            "anthropic_sdk backend requires the claude-agent-sdk package. "
            "Install with: pip install tigerharness[anthropic] "
            "(or: pip install claude-agent-sdk)."
        ) from exc
    return claude_agent_sdk


# ---------------------------------------------------------------------------
# Config translation
# ---------------------------------------------------------------------------

# Keys in AgentConfig.extra that map directly onto ClaudeAgentOptions fields.
# Anything passed through here is the caller's responsibility to spell right.
_PASSTHROUGH_EXTRA_KEYS = frozenset({
    "permission_mode",
    "cwd",
    "allowed_tools",
    "disallowed_tools",
    "add_dirs",
    "max_budget_usd",
    "fallback_model",
    "cli_path",
    "settings",
    "env",
    "extra_args",
    "max_buffer_size",
    "debug_stderr",
    "user",
    "include_partial_messages",
    "agents",
    "setting_sources",
    "skills",
    "sandbox",
    "plugins",
    "max_thinking_tokens",
    "thinking",
    "effort",
    "output_format",
    "enable_file_checkpointing",
    "betas",
    "permission_prompt_tool_name",
})


def _build_options(
    sdk: Any,
    config: AgentConfig,
    *,
    session_id: str | None = None,
    approval: ApprovalCallback | None = None,
) -> Any:
    """Translate ``AgentConfig`` -> ``ClaudeAgentOptions``.

    Caller-supplied ``approval`` becomes a ``can_use_tool`` callback that
    bridges between our ``ApprovalRequest``/``ApprovalDecision`` protocol
    and claude-agent-sdk's ``PermissionResultAllow``/``PermissionResultDeny``.
    """
    if config.tools:
        raise BackendNotImplementedError(
            "anthropic_sdk backend doesn't translate custom ToolSpec tools "
            "yet (would require building an MCP server via "
            "create_sdk_mcp_server). Use builtin_tools for now."
        )

    builtin_names = [t.name for t in config.builtin_tools]

    kwargs: dict[str, Any] = {}
    if config.instructions is not None:
        kwargs["system_prompt"] = config.instructions
    if config.model is not None:
        kwargs["model"] = config.model
    if config.max_turns is not None:
        kwargs["max_turns"] = config.max_turns
    if builtin_names:
        # If the caller hasn't explicitly set `allowed_tools` in extra,
        # use the builtin_tools list. Otherwise the extras take precedence.
        kwargs.setdefault("allowed_tools", builtin_names)

    # Pass through known extras.
    for key in _PASSTHROUGH_EXTRA_KEYS:
        if key in config.extra:
            kwargs[key] = config.extra[key]

    if session_id:
        kwargs["resume"] = session_id

    if approval is not None:
        kwargs["can_use_tool"] = _wrap_approval(sdk, approval, agent_name=config.name)

    return sdk.ClaudeAgentOptions(**kwargs)


def _wrap_approval(sdk: Any, approval: ApprovalCallback, *, agent_name: str):
    """Adapt our ``ApprovalCallback`` to claude-agent-sdk's ``can_use_tool``
    callback signature. The SDK calls this for each tool invocation that
    requires approval (depending on ``permission_mode``)."""

    from ..types import ToolCall as _ToolCall

    async def _can_use_tool(
        tool_name: str,
        input_data: dict[str, Any],
        context: Any,
    ) -> Any:
        sess_id = getattr(context, "session_id", None) if context is not None else None
        req = ApprovalRequest(
            tool_call=_ToolCall(
                id=getattr(context, "tool_use_id", "") or "",
                name=tool_name,
                arguments=dict(input_data),
            ),
            agent_name=agent_name,
            session_id=sess_id,
        )
        decision: ApprovalDecision = await approval(req)
        if decision.allow:
            return sdk.PermissionResultAllow(
                updated_input=decision.updated_input,
            )
        return sdk.PermissionResultDeny(
            message=decision.reason or "denied by approval callback",
        )

    return _can_use_tool


# ---------------------------------------------------------------------------
# Prompt normalization
# ---------------------------------------------------------------------------

def _normalize_prompt(prompt: str | list[InputMessage]) -> str:
    """Reduce our prompt type to the string the SDK accepts.

    For ``list[InputMessage]`` we concatenate user-role text contents.
    Non-user messages and non-text parts are skipped with no error -- the
    common usage is a single string prompt; lists are mostly a fan-in for
    multi-message inputs.
    """
    if isinstance(prompt, str):
        return prompt
    pieces: list[str] = []
    for msg in prompt:
        if msg.role != "user":
            continue
        if isinstance(msg.content, str):
            pieces.append(msg.content)
        else:
            for part in msg.content:
                if isinstance(part, TextPart):
                    pieces.append(part.text)
    return "\n\n".join(pieces).strip()


# ---------------------------------------------------------------------------
# Event translation: their message types -> our Event types
# ---------------------------------------------------------------------------

_STOP_REASON_MAP = {
    "end_turn": "end_turn",
    "max_turns": "max_turns",
    "max_budget_usd": "max_budget",
    "error": "error",
    "interrupted": "interrupted",
    "refusal": "refusal",
    "tool_denied": "tool_denied",
    "tool_use": "end_turn",  # mid-turn tool use; stream continues
}


def _stop_reason(raw: Any) -> str:
    """Map an SDK stop_reason / subtype to our StopReason literal."""
    if isinstance(raw, str) and raw.startswith("error_"):
        sub = raw[len("error_"):]
        return _STOP_REASON_MAP.get(sub, "error")
    if isinstance(raw, str):
        return _STOP_REASON_MAP.get(raw, "end_turn")
    return "end_turn"


def _translate_block(
    sdk: Any,
    block: Any,
    tool_names: dict[str, str] | None = None,
) -> Event | None:
    """Translate a single content block to one of our Events.

    ``tool_names`` is a caller-owned ``{tool_use_id -> tool_name}`` map
    populated as ``ToolUseBlock`` instances stream by. When a
    ``ToolResultBlock`` arrives we look up the originating tool's name
    in the map so ``ToolResult.name`` is populated. Without the map,
    ``ToolResult.name`` defaults to ``""`` (the block doesn't carry it).
    """
    if isinstance(block, sdk.TextBlock):
        # claude-agent-sdk delivers full assistant text blocks (not
        # partial deltas in the basic mode), so use MessageComplete.
        return MessageComplete(text=block.text, role="assistant")
    if isinstance(block, sdk.ToolUseBlock):
        if tool_names is not None:
            tool_names[block.id] = block.name
        return ToolCall(
            id=block.id,
            name=block.name,
            arguments=dict(block.input) if block.input else {},
        )
    if isinstance(block, sdk.ToolResultBlock):
        out = ToolOutput.of(block.content)
        out.is_error = bool(block.is_error)
        name = ""
        if tool_names is not None:
            name = tool_names.get(block.tool_use_id, "")
        return ToolResult(id=block.tool_use_id, name=name, output=out)
    # Thinking blocks: introspect duck-style so we also pick up future
    # block types with a `.thinking` payload.
    if getattr(block, "thinking", None) is not None:
        return Thinking(text=block.thinking)
    return None


def _to_normalized_message(sdk: Any, msg: Any) -> NormalizedMessage | None:
    """Build a transcript entry from one SDK message."""
    if isinstance(msg, sdk.AssistantMessage):
        parts: list[Any] = []
        for b in msg.content:
            if isinstance(b, sdk.TextBlock):
                parts.append(TextPart(text=b.text))
            elif isinstance(b, sdk.ToolUseBlock):
                parts.append(ToolUsePart(
                    id=b.id,
                    name=b.name,
                    input=dict(b.input) if b.input else {},
                ))
            elif getattr(b, "thinking", None) is not None:
                parts.append(ThinkingPart(text=b.thinking))
        return NormalizedMessage(role="assistant", content=parts)

    if isinstance(msg, sdk.UserMessage):
        parts = []
        content = msg.content
        if isinstance(content, str):
            parts.append(TextPart(text=content))
        elif isinstance(content, list):  # pragma: no branch  # SDK always returns str|list
            for b in content:
                if isinstance(b, sdk.ToolResultBlock):
                    parts.append(ToolResultPart(
                        tool_use_id=b.tool_use_id,
                        content=b.content if isinstance(b.content, (str, list))
                        else str(b.content),
                        is_error=bool(b.is_error),
                    ))
                elif isinstance(b, sdk.TextBlock):  # pragma: no branch  # SDK block types exhaustive
                    parts.append(TextPart(text=b.text))
        return NormalizedMessage(role="user", content=parts)

    return None


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

class _AnthropicSession:
    """Multi-turn handle backed by a long-lived ``ClaudeSDKClient``.

    The client is created lazily on the first ``run_stream`` call so an
    ``open_session()`` with a ``resume_id`` you never use is essentially
    free. Subsequent calls reuse the same client to preserve in-process
    conversation state.

    **Config immutability.** ``ClaudeAgentOptions`` (system_prompt, model,
    allowed_tools, max_turns, approval callback, ...) is captured once
    when the underlying client is created -- i.e. from the
    ``AgentConfig`` passed to the FIRST ``run`` / ``run_stream`` call
    on this session. Subsequent calls reuse the same client and ignore
    any per-call ``AgentConfig`` differences in those fields. If you
    need to swap models or instructions, ``close()`` the session and
    open a new one with the new config.
    """

    def __init__(self, sdk_module: Any, *, resume_id: str | None = None) -> None:
        self._sdk = sdk_module
        self._id: str = resume_id or ""
        self._client: Any = None
        self._connected: bool = False

    @property
    def id(self) -> str:
        return self._id

    async def close(self) -> None:
        if self._client is not None and self._connected:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        self._connected = False
        self._client = None


# ---------------------------------------------------------------------------
# Stream handle
# ---------------------------------------------------------------------------

class _AnthropicStreamHandle(BaseStreamHandle):
    """Stream handle for one ``run_stream`` invocation.

    Holds a reference to the underlying ``ClaudeSDKClient`` so
    ``cancel()`` can call ``client.interrupt()``.
    """

    def __init__(self) -> None:
        super().__init__()
        self._client: Any = None
        self._owns_client: bool = False

    async def cancel(self, *, after_turn: bool = False) -> None:
        if self._client is None:
            return
        try:
            await self._client.interrupt()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------

class AnthropicSDKBackend:
    """Backend over Anthropic's ``claude-agent-sdk``.

    Implements the ``AgentBackend`` Protocol. See module docstring for the
    translation contract.
    """

    def __init__(self, **_kwargs: Any) -> None:
        self._sdk = _import_sdk()

    async def open_session(self, *, resume_id: str | None = None) -> Session:
        return _AnthropicSession(self._sdk, resume_id=resume_id)

    async def run(
        self,
        config: AgentConfig,
        prompt: str | list[InputMessage],
        *,
        session: Session | None = None,
        approval: ApprovalCallback | None = None,
    ) -> RunResult:
        return await run_via_stream(self.run_stream(
            config, prompt, session=session, approval=approval,
        ))

    def run_stream(
        self,
        config: AgentConfig,
        prompt: str | list[InputMessage],
        *,
        session: Session | None = None,
        approval: ApprovalCallback | None = None,
    ) -> StreamHandle:
        handle = _AnthropicStreamHandle()
        handle._start(self._iter(handle, config, prompt, session, approval))
        return handle

    # ----- internals --------------------------------------------------------

    async def _iter(
        self,
        handle: _AnthropicStreamHandle,
        config: AgentConfig,
        prompt: str | list[InputMessage],
        session: Session | None,
        approval: ApprovalCallback | None,
    ) -> AsyncIterator[Event]:
        sdk = self._sdk
        prompt_text = _normalize_prompt(prompt)

        sess: _AnthropicSession | None = None
        if session is not None:
            if not isinstance(session, _AnthropicSession):
                raise TypeError(
                    "anthropic_sdk backend can only consume sessions it "
                    "created via open_session()."
                )
            sess = session

        # Build or reuse the client.
        client: Any
        if sess is not None and sess._client is not None:
            client = sess._client
            handle._client = client
            handle._owns_client = False
        else:
            options = _build_options(
                sdk, config,
                session_id=(sess.id if sess else None),
                approval=approval,
            )
            client = sdk.ClaudeSDKClient(options=options)
            handle._client = client
            await client.connect()
            if sess is not None:
                sess._client = client
                sess._connected = True
                handle._owns_client = False
            else:
                handle._owns_client = True

        # Run the query and translate events.
        transcript: list[NormalizedMessage] = []
        final_text: str = ""
        cost: float | None = None
        usage: dict[str, Any] | None = None
        stop: str = "end_turn"
        session_id_seen: str | None = None
        model_seen: str | None = None
        # Per-run map so ToolResult events can carry the originating
        # tool's name (ToolResultBlock only has tool_use_id).
        tool_names: dict[str, str] = {}

        try:
            await client.query(prompt_text)
            async for msg in client.receive_response():
                # SystemMessage on init -> RunStart.
                if isinstance(msg, sdk.SystemMessage):
                    if msg.subtype == "init":
                        data = msg.data or {}
                        session_id_seen = data.get("session_id")
                        model_seen = data.get("model")
                        if sess is not None and session_id_seen and not sess._id:
                            sess._id = session_id_seen
                        yield RunStart(
                            session_id=session_id_seen, model=model_seen,
                        )
                    continue

                # Assistant + user messages: emit per-block events.
                if isinstance(msg, (sdk.AssistantMessage, sdk.UserMessage)):
                    # Capture usage from assistant turns. We keep the
                    # latest non-None usage so RunResult.usage reflects
                    # the final turn (multi-turn tool loops aggregate
                    # input/output tokens on each call; the last is the
                    # cumulative view most callers want).
                    msg_usage = getattr(msg, "usage", None)
                    if msg_usage:
                        usage = dict(msg_usage) if isinstance(msg_usage, dict) \
                            else msg_usage
                    nm = _to_normalized_message(sdk, msg)
                    if nm is not None:  # pragma: no branch  # SDK msgs are always User|Assistant
                        transcript.append(nm)
                    content = msg.content if isinstance(msg.content, list) else []
                    for block in content:
                        ev = _translate_block(sdk, block, tool_names)
                        if ev is None:
                            continue
                        if isinstance(ev, MessageComplete):
                            final_text = ev.text
                        yield ev
                    continue

                # ResultMessage: terminal. Stop iterating.
                if isinstance(msg, sdk.ResultMessage):  # pragma: no branch  # only msg types are content+result
                    cost = msg.total_cost_usd
                    stop = _stop_reason(msg.stop_reason or msg.subtype)
                    if msg.session_id and sess is not None and not sess._id:
                        sess._id = msg.session_id
                    handle._result = RunResult(
                        final_output=final_text or None,
                        transcript=transcript,
                        stop_reason=stop,  # type: ignore[arg-type]
                        usage=usage,
                        cost_usd=cost,
                        raw=msg,
                    )
                    yield RunDone(
                        final_output=final_text or None,
                        stop_reason=stop,  # type: ignore[arg-type]
                        usage=usage,
                        cost_usd=cost,
                    )
                    break
        finally:
            if handle._owns_client and client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass

        # Safety net: stream ended without a ResultMessage (e.g. interrupt).
        if handle._result is None:
            handle._result = RunResult(
                final_output=final_text or None,
                transcript=transcript,
                stop_reason="interrupted",
                usage=usage,
                cost_usd=cost,
                raw=None,
            )

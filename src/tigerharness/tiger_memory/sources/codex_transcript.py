"""OpenAI Codex session ("rollout") JSONL source adapter.

Codex writes one JSONL per session under
``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<timestamp>-<thread-id>.jsonl``
(``~/.codex`` by default). Unlike Claude Code's per-project folders, the
tree is global, so a session is attributed to a team by the ``cwd`` its
``session_meta`` line records: with ``cwd`` set, only sessions opened in
that directory (the team root) are read.

Per file, this adapter derives:
    conversation_uuid = the thread id from ``session_meta.payload.id``
                        (falls back to the id in the filename)
    source            = "slack" if that id appears in the bridge's
                        threads.json reverse map (a ``codex_exec`` bridge
                        session); else "codex"
    source_id         = thread_ts (slack) or the thread id
    first/last_event_at = first/last ``timestamp`` in the file
    content           = chronological dump of user/assistant messages,
                        tool calls (by name) and tool outputs
    activity_mtime    = mtime of the JSONL (append-only)

Helper-session rollouts (a ``spawn_agent`` child: ``session_meta`` carries
``parent_thread_id``) are skipped -- they are the sub-agent's own context,
never the persona's conversation -- the counterpart of dropping Claude's
``isSidechain`` rows. Codex-injected context blocks in the first user
message (``<recommended_plugins>``, ``<environment_context>``) and the
``[bridge-context]`` trailer are stripped, and tool calls that only read
a memory briefing are dropped with their outputs, exactly as for Claude.

Everything about attribution, persona filtering, drive-session suppression
and the record shape is shared with the Claude adapter through
``_transcripts.TranscriptAdapterBase``.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterator

from ._transcripts import (
    ThreadMap,
    TranscriptAdapterBase,
    _extract_slack_channel,
    _iter_events,
    _load_drive_threads,
    _parse_ts,
    _path_is_briefing,
    _strip_bridge_context,
)
from .base import SourceRecord

log = logging.getLogger(__name__)

DEFAULT_SESSIONS_PATH = Path("~/.codex/sessions")

# Codex prepends machine context to a session's first user message as
# XML-ish blocks. They are not conversation and would otherwise lead
# every summary with a plugin catalogue.
_INJECTED_BLOCK_RE = re.compile(
    r"<(recommended_plugins|environment_context|user_instructions|"
    r"skills_instructions)>[\s\S]*?</\1>\s*"
)

_THREAD_ID_IN_NAME_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
)

_MESSAGE_ROLES = {"user", "assistant"}
_TOOL_CALL_TYPES = {"function_call", "custom_tool_call"}
_TOOL_OUTPUT_TYPES = {"function_call_output", "custom_tool_call_output"}


class CodexTranscriptAdapter(TranscriptAdapterBase):
    kind = "codex"
    local_source = "codex"

    def __init__(
        self,
        sessions_path: Path | str | None = None,
        threads_json: Path | None = None,
        *,
        cwd: Path | str | None = None,
        persona: str | None = None,
        team: str | None = None,
        include_unattributed: bool = False,
        max_age_days: int | None = 7,
        drive_sessions_json: Path | None = None,
    ) -> None:
        super().__init__(
            threads_json=threads_json,
            persona=persona,
            team=team,
            include_unattributed=include_unattributed,
            max_age_days=max_age_days,
            drive_sessions_json=drive_sessions_json,
        )
        self.sessions_path = Path(sessions_path or DEFAULT_SESSIONS_PATH).expanduser()
        # The team root the sessions must have been opened in; ``None``
        # reads every session under ``sessions_path`` (single-team hosts
        # with one Codex user -- or tests).
        self.cwd = Path(cwd).expanduser().resolve() if cwd else None

    # ---- public --------------------------------------------------------

    def discover(self) -> Iterator[SourceRecord]:
        thread_map = self._reverse_thread_map()
        drive_threads = _load_drive_threads(self.drive_sessions_json)
        if not self.sessions_path.exists():
            return
        cutoff = self._cutoff()
        for jsonl in sorted(self.sessions_path.rglob("rollout-*.jsonl")):
            if self._too_old(jsonl, cutoff):
                continue
            meta = _session_meta(jsonl)
            if meta is None or not self._session_is_ours(meta):
                log.debug("codex: skipping %s (not this team's top-level session)", jsonl.name)
                continue
            session_uuid = meta.get("id") or _id_from_name(jsonl)
            if not session_uuid:
                continue
            if not self._allowed(session_uuid, thread_map, drive_threads):
                continue
            rec = self._record_for(jsonl, session_uuid, thread_map)
            if rec is not None:
                yield rec

    # ---- helpers -------------------------------------------------------

    def _session_is_ours(self, meta: dict) -> bool:
        """A top-level session (no ``parent_thread_id``) opened in our
        ``cwd`` (when one is configured)."""
        if meta.get("parent_thread_id"):
            return False  # a helper session's own rollout
        if self.cwd is None:
            return True
        raw = meta.get("cwd")
        if not isinstance(raw, str) or not raw:
            return False
        try:
            return Path(raw).expanduser().resolve() == self.cwd
        except OSError:  # pragma: no cover - resolve() on an odd path
            return False

    def _record_for(
        self, jsonl: Path, session_uuid: str, thread_map: ThreadMap,
    ) -> SourceRecord | None:
        events = list(_iter_events(jsonl))
        ts_events = [e for e in events if isinstance(e.get("timestamp"), str)]
        if not ts_events:
            return None
        first_at = _parse_ts(ts_events[0]["timestamp"])
        last_at = _parse_ts(ts_events[-1]["timestamp"])

        content_parts: list[str] = []
        skipped_call_ids: set[str] = set()
        slack_channel = ""
        for e in events:
            if e.get("type") != "response_item":
                continue  # event_msg duplicates, token counts, turn context
            payload = e.get("payload")
            if not isinstance(payload, dict):
                continue
            ts = e.get("timestamp", "")
            ptype = payload.get("type")
            if ptype == "message":
                role = payload.get("role")
                if role not in _MESSAGE_ROLES:
                    continue  # developer / system instructions
                raw = _content_text(payload.get("content"))
                if raw and not slack_channel:
                    slack_channel = _extract_slack_channel(raw)
                text = _clean_message(raw)
                if text:
                    content_parts.append(f"[{ts}] {role}:\n{text}\n")
            elif ptype in _TOOL_CALL_TYPES:
                call_id = str(payload.get("call_id") or "")
                args = str(payload.get("input") or payload.get("arguments") or "")
                if _path_is_briefing(args):
                    if call_id:
                        skipped_call_ids.add(call_id)
                    continue
                content_parts.append(
                    f"[{ts}] assistant:\n[tool_use: {payload.get('name', '?')}]\n"
                )
            elif ptype in _TOOL_OUTPUT_TYPES:
                call_id = str(payload.get("call_id") or "")
                if call_id in skipped_call_ids:
                    continue
                out = _content_text(payload.get("output"))
                if out:
                    content_parts.append(f"[{ts}] user:\n[tool_result] {out}\n")
        if not content_parts:
            return None
        return self._make_record(
            session_uuid=session_uuid,
            path=jsonl,
            first_at=first_at,
            last_at=last_at,
            content="".join(content_parts),
            slack_channel=slack_channel,
            thread_map=thread_map,
        )


# ----- module-level helpers (kept testable) --------------------------------


def _session_meta(jsonl: Path) -> dict | None:
    """The ``session_meta`` payload (normally the first line), or ``None``
    when the file has none -- it is then not a session rollout."""
    for e in _iter_events(jsonl):
        if e.get("type") == "session_meta":
            payload = e.get("payload")
            return payload if isinstance(payload, dict) else {}
    return None


def _id_from_name(jsonl: Path) -> str:
    m = _THREAD_ID_IN_NAME_RE.search(jsonl.name)
    return m.group(1) if m else ""


def _content_text(content: object) -> str:
    """Join the ``text`` of Codex content blocks (``input_text`` /
    ``output_text``); a bare string passes through; encrypted blocks and
    anything else contribute nothing."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and isinstance(b.get("text"), str)
        ]
        return "\n".join(p for p in parts if p)
    return ""


def _clean_message(text: str) -> str:
    """Drop Codex's injected context blocks and the bridge trailer."""
    return _strip_bridge_context(_INJECTED_BLOCK_RE.sub("", text)).strip()


__all__ = [
    "CodexTranscriptAdapter",
    "DEFAULT_SESSIONS_PATH",
]

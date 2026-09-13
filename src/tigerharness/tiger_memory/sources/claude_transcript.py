"""Claude transcript JSONL source adapter.

Discovers ``*.jsonl`` files in the configured project_path. For each,
derives:
    conversation_uuid = JSONL filename UUID (full 36-char RFC 4122)
    source            = "slack" if its session_id appears in the
                        bridge's threads.json reverse map; else "claude_code"
    source_id         = thread_ts (slack) or session_uuid (claude_code)
    first/last_event_at = first/last timestamp in the JSONL events
    content           = chronological dump of user/assistant message text
    activity_mtime    = mtime of the JSONL (rock-solid: append-only)

The Slack reverse-lookup is optional -- if ``threads_json`` isn't
configured (or doesn't exist), all transcripts are classified
``claude_code``.

Per-persona filtering (added when the multi-bridge introduced N-persona
routing): if ``persona`` is set, the adapter only emits records owned
by that persona (per the bridge's threads.json). Sessions with no
attribution (local ``claude -p`` not via Slack, or pre-routing
threads.json entries) are excluded unless ``include_unattributed=True``.

threads.json schema compatibility: the post-routing schema is
``{thread_ts: {session_id, persona}}``; the pre-routing schema was
``{thread_ts: session_id}``. Both are tolerated; pre-routing entries
have ``persona=None`` and are filtered out under strict mode.

The attribution / filtering / record-shaping half is shared with the
Codex adapter (``codex_transcript``) through
``_transcripts.TranscriptAdapterBase``; this module keeps the Claude
Code line format. The helpers below are re-exported from
``_transcripts`` under their historical names so existing imports and
monkeypatches keep working.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator

from ._transcripts import (  # noqa: F401 -- re-exported for callers/tests
    _BRIDGE_CTX_RE,
    _SLACK_CHANNEL_RE,
    ThreadMap,
    TranscriptAdapterBase,
    _extract_slack_channel,
    _iter_events,
    _load_drive_threads,
    _normalize_owner,
    _parse_ts,
    _path_is_briefing,
    _strip_bridge_context,
)
from .base import SourceRecord

log = logging.getLogger(__name__)


# JSONL row types we extract content from. The append-only file also
# carries queue-operation / system / tool_use rows that aren't part
# of the conversation; we skip them for the summarizer's content
# string but use them for timestamps.
_CONTENT_TYPES = {"user", "assistant"}


class ClaudeTranscriptAdapter(TranscriptAdapterBase):
    kind = "claude_code"  # umbrella; resolves to "slack" per-transcript
    local_source = "claude_code"

    def __init__(
        self,
        project_path: Path,
        threads_json: Path | None = None,
        *,
        persona: str | None = None,
        team: str | None = None,
        include_unattributed: bool = False,
        max_age_days: int | None = 7,
        drive_sessions_json: Path | None = None,
    ):
        super().__init__(
            threads_json=threads_json,
            persona=persona,
            team=team,
            include_unattributed=include_unattributed,
            max_age_days=max_age_days,
            drive_sessions_json=drive_sessions_json,
        )
        self.project_path = Path(project_path).expanduser()

    # ---- public --------------------------------------------------------

    def discover(self) -> Iterator[SourceRecord]:
        # session_id -> (thread_ts, persona | None)
        thread_map = self._reverse_thread_map()
        # thread_ts of journal *drive* sessions to suppress (read once).
        drive_threads = _load_drive_threads(self.drive_sessions_json)
        if not self.project_path.exists():
            return
        cutoff = self._cutoff()
        for jsonl in sorted(self.project_path.glob("*.jsonl")):
            if self._too_old(jsonl, cutoff):
                continue
            session_uuid = jsonl.stem
            if not self._allowed(session_uuid, thread_map, drive_threads):
                continue
            rec = self._record_for(jsonl, thread_map)
            if rec is not None:
                yield rec

    # ---- helpers -------------------------------------------------------

    def _record_for(
        self,
        jsonl: Path,
        thread_map: ThreadMap,
    ) -> SourceRecord | None:
        session_uuid = jsonl.stem  # filename minus .jsonl
        # Parse JSONL — gather timestamps + content. B7: drop sub-agent
        # (sidechain) rows up front — they are the helper session's /
        # summarizer's own context, never the persona's conversation. This
        # handles both shapes: nested sidechain rows fall out of a parent
        # session, and an all-sidechain file collapses to empty -> skipped
        # just below.
        events = [
            e for e in _iter_events(jsonl) if e.get("isSidechain") is not True
        ]
        if not events:
            return None

        # Timestamps: first and last event with a timestamp
        ts_events = [e for e in events if e.get("timestamp")]
        if not ts_events:
            return None
        first_at = _parse_ts(ts_events[0]["timestamp"])
        last_at = _parse_ts(ts_events[-1]["timestamp"])

        # Content dump from user/assistant rows only. Carry the skipped
        # tool_use_id set across events so a briefing-read tool_use in
        # one event drops its tool_result in the next event too.
        # Also capture slack_channel from the bridge-context block
        # (which we'll then strip) so `tiger-memory raw` can compose
        # a Slack thread URL.
        content_parts = []
        skipped_tool_use_ids: set[str] = set()
        slack_channel = ""
        for e in events:
            if e.get("type") not in _CONTENT_TYPES:
                continue
            raw_text = _raw_text(e)
            if raw_text and not slack_channel:
                slack_channel = _extract_slack_channel(raw_text)
            text = _extract_text(e, skipped_tool_use_ids=skipped_tool_use_ids)
            if not text:
                continue
            ts = e.get("timestamp", "")
            role = e.get("type", "")
            content_parts.append(f"[{ts}] {role}:\n{text}\n")
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


def _raw_text(event: dict) -> str:
    """Return the raw concatenated text of a user/assistant event,
    *without* the briefing/bridge-context filtering. Used only for
    metadata extraction (e.g., slack_channel from bridge-context)."""
    msg = event.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return ""


def _extract_text(event: dict, *, skipped_tool_use_ids: set[str] | None = None) -> str:
    """Extract the human-readable text from a user/assistant event.

    Filters out briefing-read tool calls — when the agent reads
    `memory/briefing/*` files at session start, those reads land in
    the JSONL but are pure boilerplate from a summarization standpoint.
    Including them would cause every short summary to re-summarize the
    briefing instead of the actual conversation.

    Conservative filter: only Read/View tool calls whose path contains
    ``/memory/`` AND ``briefing`` are dropped. All other tool calls
    (Bash, Edit, Write, etc.) and their results pass through —
    "Sai ran the backtest" IS signal for the summary.

    The caller passes a mutable ``skipped_tool_use_ids`` set so a
    tool_use in event N drops its matching tool_result in event N+1.
    """
    msg = event.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return _strip_bridge_context(content)
    if isinstance(content, list):
        # Anthropic-style content blocks
        if skipped_tool_use_ids is None:
            skipped_tool_use_ids = set()
        parts = []
        for block in content:
            if isinstance(block, dict):
                btype = block.get("type")
                if btype == "text":
                    parts.append(_strip_bridge_context(block.get("text", "")))
                elif btype == "tool_use":
                    if _is_briefing_read(block):
                        # Remember the id so we can drop its result too.
                        if isinstance(block.get("id"), str):
                            skipped_tool_use_ids.add(block["id"])
                        continue
                    parts.append(
                        f"[tool_use: {block.get('name', '?')}]"
                    )
                elif btype == "tool_result":
                    tu_id = block.get("tool_use_id", "")
                    if tu_id in skipped_tool_use_ids:
                        continue
                    res = block.get("content", "")
                    if isinstance(res, list):
                        res = "\n".join(
                            b.get("text", "") for b in res if isinstance(b, dict)
                        )
                    parts.append(f"[tool_result] {res}")
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    return ""


def _is_briefing_read(tool_use_block: dict) -> bool:
    """True iff this tool call's purpose is reading a briefing file.

    Covers multiple paths the agent might use to access the briefing:
    Read/View/Open, Bash (cat/head/less/tail), Glob, and Grep — any
    invocation whose input mentions a path inside ``memory/.../briefing``.

    Conservative: we require both "/memory/" and "briefing" substrings
    to coexist in some input field. This avoids false-positive drops
    of reads of unrelated paths that happen to contain "memory" or
    "briefing" separately (e.g., `docs/019_sai_memory_system.md`).
    """
    name = (tool_use_block.get("name") or "").lower()
    inp = tool_use_block.get("input") or {}

    # Direct file readers — check the path/file_path field.
    if name in {"read", "view", "open"}:
        path = str(inp.get("file_path") or inp.get("path") or "")
        return _path_is_briefing(path)

    # Bash — inspect the command for cat/head/less/tail of briefing paths.
    if name == "bash":
        cmd = str(inp.get("command") or "")
        return _path_is_briefing(cmd)

    # Glob / Grep — surface the pattern + path fields.
    if name in {"glob", "grep"}:
        for k in ("pattern", "path", "include"):
            v = str(inp.get(k) or "")
            if _path_is_briefing(v):
                return True
        return False

    return False

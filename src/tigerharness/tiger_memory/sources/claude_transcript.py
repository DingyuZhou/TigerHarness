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
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .base import SourceAdapter, SourceRecord


# JSONL row types we extract content from. The append-only file also
# carries queue-operation / system / tool_use rows that aren't part
# of the conversation; we skip them for the summarizer's content
# string but use them for timestamps.
_CONTENT_TYPES = {"user", "assistant"}


class ClaudeTranscriptAdapter(SourceAdapter):
    kind = "claude_code"  # umbrella; resolves to "slack" per-transcript

    def __init__(
        self,
        project_path: Path,
        threads_json: Path | None = None,
        *,
        persona: str | None = None,
        include_unattributed: bool = False,
        max_age_days: int | None = 7,
    ):
        self.project_path = Path(project_path).expanduser()
        self.threads_json = (
            Path(threads_json).expanduser() if threads_json else None
        )
        # When set, only sessions owned by *persona* (per threads.json)
        # are emitted. ``None`` preserves the legacy "emit everything"
        # behavior.
        self.persona = persona
        # When persona-filtering, controls whether sessions with NO
        # persona attribution (local claude -p, or pre-routing entries)
        # are also emitted. Default False == strict.
        self.include_unattributed = include_unattributed
        # Hard upper bound on how far back discovery looks, measured by
        # the JSONL's mtime. Defense-in-depth against runaway rebuilds:
        # even if the source directory accumulates years of transcripts,
        # only the recent ``max_age_days`` worth are ever considered for
        # summarization. ``None`` disables the cutoff (legacy behavior).
        self.max_age_days = max_age_days

    # ---- public --------------------------------------------------------

    def discover(self) -> Iterator[SourceRecord]:
        # session_id -> (thread_ts, persona | None)
        thread_map = self._reverse_thread_map()
        if not self.project_path.exists():
            return
        cutoff = (
            time.time() - self.max_age_days * 86400
            if self.max_age_days is not None
            else None
        )
        for jsonl in sorted(self.project_path.glob("*.jsonl")):
            if cutoff is not None:
                try:
                    if jsonl.stat().st_mtime < cutoff:
                        continue
                except OSError:
                    continue
            session_uuid = jsonl.stem
            if not self._allowed(session_uuid, thread_map):
                continue
            rec = self._record_for(jsonl, thread_map)
            if rec is not None:
                yield rec

    # ---- helpers -------------------------------------------------------

    def _allowed(
        self,
        session_uuid: str,
        thread_map: dict[str, tuple[str, str | None]],
    ) -> bool:
        """Apply the per-persona filter.

        - No filter set (legacy / single-tenant): everything passes.
        - Filter set + session in threads.json with matching persona: pass.
        - Filter set + session in threads.json with different persona: drop.
        - Filter set + session NOT in threads.json (local claude_p)
          OR persona=None (pre-routing): pass iff include_unattributed.
        """
        if self.persona is None:
            return True
        entry = thread_map.get(session_uuid)
        if entry is None:
            # Local claude_p session, never went through the bridge.
            return self.include_unattributed
        _thread_ts, owner = entry
        if owner is None:
            # Pre-routing threads.json entry (bare session_id schema).
            return self.include_unattributed
        return owner == self.persona

    def _reverse_thread_map(self) -> dict[str, tuple[str, str | None]]:
        """Build session_id -> (thread_ts, persona | None) from the
        bridge's threads.json.

        Tolerates both schemas:

        Pre-PR4: ``{thread_ts: "session_id"}`` -> persona is ``None``.
        Post-PR4: ``{thread_ts: {session_id, persona}}`` -> persona
        preserved (or ``None`` if missing/empty).
        """
        if not self.threads_json or not self.threads_json.exists():
            return {}
        try:
            data = json.loads(self.threads_json.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        result: dict[str, tuple[str, str | None]] = {}
        for tts, val in data.items():
            if isinstance(val, str) and val:
                # Pre-routing: bare session_id string.
                result[val] = (str(tts), None)
            elif isinstance(val, dict):
                sid = val.get("session_id")
                if not isinstance(sid, str) or not sid:
                    continue
                persona = val.get("persona")
                result[sid] = (
                    str(tts),
                    persona if isinstance(persona, str) and persona else None,
                )
        return result

    def _record_for(
        self,
        jsonl: Path,
        thread_map: dict[str, tuple[str, str | None]],
    ) -> SourceRecord | None:
        session_uuid = jsonl.stem  # filename minus .jsonl
        # Parse JSONL — gather timestamps + content
        events = list(_iter_events(jsonl))
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

        is_slack = session_uuid in thread_map
        if is_slack:
            thread_ts, _owner = thread_map[session_uuid]
            source_id = (
                f"{thread_ts}@{slack_channel}" if slack_channel else thread_ts
            )
        else:
            source_id = session_uuid
        return SourceRecord(
            conversation_uuid=session_uuid,
            source="slack" if is_slack else "claude_code",
            source_id=source_id,
            first_event_at=first_at,
            last_event_at=last_at,
            activity_mtime=jsonl.stat().st_mtime,
            content="".join(content_parts),
            raw_path=jsonl,
        )


# ----- module-level helpers (kept testable) --------------------------------


def _iter_events(jsonl: Path) -> Iterator[dict]:
    """Yield parsed JSON objects from a JSONL file. Tolerates malformed lines."""
    try:
        with jsonl.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        import logging
        logging.getLogger("tigerharness.tiger_memory.sources").warning(
            "unreadable JSONL %s: %s", jsonl, exc,
        )
        return


def _parse_ts(ts: str) -> datetime:
    """Parse an ISO timestamp. Falls back to epoch on failure."""
    try:
        # Claude Code uses 'Z' suffix; Python 3.11+ accepts it directly.
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except (ValueError, AttributeError):
        return datetime.fromtimestamp(0, tz=timezone.utc)


# Slack bridge appends a `[bridge-context]` block to every user message
# (thread_ts + channel id) as machine metadata. It's not conversation
# content and shouldn't pollute summaries. Pattern matches the block
# to end-of-string (since the bridge appends it last).
_BRIDGE_CTX_RE = __import__("re").compile(r"\n*\[bridge-context\][\s\S]*$")


def _strip_bridge_context(text: str) -> str:
    """Remove the slack-bridge metadata block from user message text."""
    if "[bridge-context]" not in text:
        return text
    return _BRIDGE_CTX_RE.sub("", text).rstrip()


_SLACK_CHANNEL_RE = __import__("re").compile(r"slack_channel:\s*([A-Z0-9]+)")


def _extract_slack_channel(text: str) -> str:
    """Pull the channel id from the bridge-context block (empty if none)."""
    m = _SLACK_CHANNEL_RE.search(text)
    return m.group(1) if m else ""


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


def _path_is_briefing(text: str) -> bool:
    """Return True iff *text* references a path under any memory briefing dir.

    Pattern: must contain ``memory/`` AND ``/briefing`` somewhere.
    Examples:
        memory/sai/briefing/MANIFEST.md         → True
        /abs/memory/sai/briefing/recent/x.md    → True
        services/tiger-memory/briefing.py       → False  (no `/briefing` *dir*)
        services/tiger-memory/tiger_memory/...  → False  (no /briefing path)
        docs/019_sai_memory_system.md            → False  (no memory/ token)
    """
    if not text:
        return False
    return "memory/" in text and "/briefing" in text

"""Shared machinery for the transcript-shaped source adapters.

Two vendors write per-session JSONL transcripts that tiger-memory reads:
Claude Code (``~/.claude/projects/<slug>/<uuid>.jsonl``) and OpenAI Codex
(``~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl``). The *line
formats* differ; everything around them is the same -- which session
belongs to which persona (the Slack bridge's ``threads.json`` reverse
map), which sessions to suppress (journal drive sessions, whose worklog
already owns the content), the age cutoff, the ``[bridge-context]``
stripping, and the ``SourceRecord`` shape the summarizer ingests. That
shared half lives here once; each adapter keeps only "find my files" and
"turn one file into text".

Module-level helpers keep their historical names (``_iter_events``,
``_parse_ts``, ...) and are re-exported by ``claude_transcript`` so
existing imports and monkeypatches keep working.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .base import SourceAdapter, SourceRecord

log = logging.getLogger(__name__)


def _normalize_owner(persona_value) -> "tuple[str | None, str] | None":
    """Normalize a threads.json ``persona`` value into ``(team|None, name)``.

    Accepts three shapes for forward/backward compatibility (B4):
      - ``{"team": "shohoku", "name": "ayako"}`` — self-describing
      - ``"shohoku/ayako"`` — flattened team/name
      - ``"ayako"`` — today's bare name (team unknown -> ``None``)
    Returns ``None`` for anything empty / unrecognized (unattributed).
    """
    if isinstance(persona_value, dict):
        name = persona_value.get("name")
        if not isinstance(name, str) or not name:
            return None
        team = persona_value.get("team")
        return (team if isinstance(team, str) and team else None, name)
    if isinstance(persona_value, str) and persona_value:
        if "/" in persona_value:
            team, _, name = persona_value.partition("/")
            if not name:
                return None
            return (team or None, name)
        return (None, persona_value)
    return None


def _load_drive_threads(path: "Path | None") -> frozenset[str]:
    """Read the journal drive-session registry's ``thread_ts`` keys.

    A consumer-side mirror of ``journal.drive_sessions.registered_threads``
    -- kept inline (rather than imported) so the adapters stay
    independent of the journal package at import time, exactly as they
    read the bridge's ``threads.json`` inline rather than importing the
    bridge. ``test_drive_sessions`` cross-checks that the two readers
    agree.

    Tolerant by design: ``None`` path (no registry configured), a missing
    / unreadable file, corrupt JSON, or a non-object top level all yield
    the empty set -- "suppress nothing", the safe direction (a degraded
    registry causes at worst a double-counted driver transcript, never
    lost persona memory)."""
    if path is None:
        return frozenset()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return frozenset()
    if not isinstance(data, dict):
        return frozenset()
    return frozenset(data)


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
        log.warning("unreadable JSONL %s: %s", jsonl, exc)
        return


def _parse_ts(ts: str) -> datetime:
    """Parse an ISO timestamp. Falls back to epoch on failure."""
    try:
        # Claude Code and Codex use a 'Z' suffix; Python 3.11+ accepts it directly.
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except (ValueError, AttributeError):
        return datetime.fromtimestamp(0, tz=timezone.utc)


# Slack bridge appends a `[bridge-context]` block to every user message
# (thread_ts + channel id) as machine metadata. It's not conversation
# content and shouldn't pollute summaries. Pattern matches the block
# to end-of-string (since the bridge appends it last).
_BRIDGE_CTX_RE = re.compile(r"\n*\[bridge-context\][\s\S]*$")


def _strip_bridge_context(text: str) -> str:
    """Remove the slack-bridge metadata block from user message text."""
    if "[bridge-context]" not in text:
        return text
    return _BRIDGE_CTX_RE.sub("", text).rstrip()


_SLACK_CHANNEL_RE = re.compile(r"slack_channel:\s*([A-Z0-9]+)")


def _extract_slack_channel(text: str) -> str:
    """Pull the channel id from the bridge-context block (empty if none)."""
    m = _SLACK_CHANNEL_RE.search(text)
    return m.group(1) if m else ""


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


ThreadMap = dict[str, tuple[str, tuple[str | None, str] | None]]


class TranscriptAdapterBase(SourceAdapter):
    """The vendor-independent half of a transcript source adapter.

    Subclasses set ``kind`` and ``local_source`` (the ``SourceRecord.source``
    for a session that never went through the Slack bridge), implement
    :meth:`discover`, and build records through :meth:`_make_record`.
    """

    kind = "transcript"
    local_source = "transcript"

    def __init__(
        self,
        *,
        threads_json: Path | None = None,
        persona: str | None = None,
        team: str | None = None,
        include_unattributed: bool = False,
        max_age_days: int | None = 7,
        drive_sessions_json: Path | None = None,
    ) -> None:
        self.threads_json = (
            Path(threads_json).expanduser() if threads_json else None
        )
        # Journal drive-session registry (``.drive-sessions.json`` under a
        # co-configured journal_worklog source's journal root). When set,
        # transcripts whose thread_ts is registered are skipped: the drive's
        # per-persona worklog already owns that content, so folding the fat
        # transcript here would double-count the driver. ``None`` (no
        # registry) disables suppression. See
        # ``docs/per-persona-journal-memory.md`` section 4.
        self.drive_sessions_json = (
            Path(drive_sessions_json).expanduser()
            if drive_sessions_json else None
        )
        # When set, only sessions owned by *persona* (per threads.json)
        # are emitted. ``None`` preserves the legacy "emit everything"
        # behavior.
        self.persona = persona
        # B4 — team-qualified identity. When set, a threads.json entry that
        # *also* carries a team must match it (so two teams' "Michael"
        # records never collide). A bare-name entry, or a team-less
        # adapter, falls back to name-only matching — fully backward
        # compatible with today's bare-string attributions.
        self.team = team
        # When persona-filtering, controls whether sessions with NO
        # persona attribution (a local headless session that never went
        # through the bridge, or pre-routing entries) are also emitted.
        # Default False == strict.
        self.include_unattributed = include_unattributed
        # Hard upper bound on how far back discovery looks, measured by
        # the JSONL's mtime. Defense-in-depth against runaway rebuilds:
        # even if the source directory accumulates years of transcripts,
        # only the recent ``max_age_days`` worth are ever considered for
        # summarization. ``None`` disables the cutoff (legacy behavior).
        self.max_age_days = max_age_days

    # ---- shared helpers ----------------------------------------------

    def _cutoff(self) -> float | None:
        return (
            time.time() - self.max_age_days * 86400
            if self.max_age_days is not None
            else None
        )

    @staticmethod
    def _too_old(path: Path, cutoff: float | None) -> bool:
        """True when *path* is older than the cutoff -- or unreadable, which
        is treated as 'skip' so one bad stat cannot abort discovery."""
        if cutoff is None:
            return False
        try:
            return path.stat().st_mtime < cutoff
        except OSError:
            return True

    def _allowed(
        self,
        session_uuid: str,
        thread_map: ThreadMap,
        drive_threads: frozenset[str] = frozenset(),
    ) -> bool:
        """Apply the drive-session suppression + per-persona filter.

        - Session whose thread_ts is a registered journal *drive*: drop
          (regardless of persona) -- its per-persona worklog owns that
          content; folding the fat transcript would double-count the
          driver.
        - No filter set (no per-persona routing): everything else passes.
        - Filter set + session in threads.json with matching persona: pass.
        - Filter set + session in threads.json with different persona: drop.
        - Filter set + session NOT in threads.json (a local headless
          session) OR persona=None (pre-routing): pass iff
          include_unattributed.

        Team match (B4) is enforced only when BOTH the adapter and the
        record carry a team; otherwise the name alone decides, so a
        bare-name entry stays compatible with a team-aware adapter.
        """
        entry = thread_map.get(session_uuid)
        if entry is not None and entry[0] in drive_threads:
            # Journal drive session: the worklog owns this content.
            return False
        if self.persona is None:
            return True
        if entry is None:
            # Local session, never went through the bridge.
            return self.include_unattributed
        _thread_ts, owner = entry
        if owner is None:
            # Pre-routing threads.json entry (bare session_id schema).
            return self.include_unattributed
        owner_team, owner_name = owner
        if owner_name != self.persona:
            return False
        if self.team is not None and owner_team is not None:
            return owner_team == self.team
        return True

    def _reverse_thread_map(self) -> ThreadMap:
        """Build session_id -> (thread_ts, owner) from the bridge's
        threads.json, where ``owner`` is ``(team|None, name)`` or ``None``.

        Tolerates every attribution schema:

        Pre-PR4: ``{thread_ts: "session_id"}`` -> owner ``None``.
        Post-PR4: ``{thread_ts: {session_id, persona}}`` where ``persona``
        is a bare name, a ``team/name`` string, or a ``{team, name}`` dict
        (B4). See ``_normalize_owner``.
        """
        if not self.threads_json or not self.threads_json.exists():
            return {}
        try:
            data = json.loads(self.threads_json.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        result: ThreadMap = {}
        for tts, val in data.items():
            if isinstance(val, str) and val:
                # Pre-routing: bare session_id string.
                result[val] = (str(tts), None)
            elif isinstance(val, dict):
                sid = val.get("session_id")
                if not isinstance(sid, str) or not sid:
                    continue
                result[sid] = (str(tts), _normalize_owner(val.get("persona")))
        return result

    def _make_record(
        self,
        *,
        session_uuid: str,
        path: Path,
        first_at: datetime,
        last_at: datetime,
        content: str,
        slack_channel: str,
        thread_map: ThreadMap,
    ) -> SourceRecord:
        """The one ``SourceRecord`` shape both vendors produce: ``source``
        is ``"slack"`` when the bridge opened the session (its
        ``source_id`` then names the thread, with the channel when
        known), else the adapter's ``local_source``."""
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
            source="slack" if is_slack else self.local_source,
            source_id=source_id,
            first_event_at=first_at,
            last_event_at=last_at,
            activity_mtime=path.stat().st_mtime,
            content=content,
            raw_path=path,
        )

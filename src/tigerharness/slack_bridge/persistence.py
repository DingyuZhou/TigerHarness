"""Persistent map: Slack ``thread_ts`` -> claude session_id + persona.

Without this, every restart wipes the in-memory thread->session map
and any reply-in-thread after the restart starts a fresh Claude session.
File lives under ``$XDG_STATE_HOME/slack-bridge/threads.json`` (default
``~/.local/state/slack-bridge/threads.json``).

Schema
------

Each entry on disk::

    "<thread_ts>": {
        "session_id": "abc-def-...",
        "persona": "ayako"            // may be null for pre-routing records
    }

For backward compatibility with the pre-routing schema, a bare string
value (``"<thread_ts>": "abc-def"``) is read as a record with
``persona=None``. Callers handle ``persona=None`` by falling back to the
team's ``default_persona``. All writes use the new dict shape.

Writes are atomic via ``tmp + os.replace``. Read errors fall back to an
empty map with a warning rather than failing the bridge to start.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


log = logging.getLogger("tigerharness.slack_bridge.persistence")


def default_state_path() -> Path:
    """XDG-correct default location for the threads file."""
    override = os.environ.get("TIGERHARNESS_SLACK_STATE_DIR", "").strip()
    if override:
        return Path(override) / "threads.json"
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "slack-bridge" / "threads.json"


@dataclass(frozen=True)
class ThreadRecord:
    """One thread's persisted state: which claude session, which persona.

    *persona* is ``None`` only for records read from the pre-routing
    on-disk schema (a bare session-id string). Callers must resolve it
    to the team's ``default_persona`` before dispatch.
    """
    session_id: str
    persona: str | None = None


class ThreadStore:
    """File-backed ``thread_ts -> ThreadRecord`` mapping.

    Reads once at construction. Writes synchronously on every ``set``
    that actually changes a value.

    The ``get()`` / ``set()`` API is backward compatible with the
    pre-routing single-persona bridge (returns session_id; set takes a
    bare session_id). Routing-aware callers use ``get_record()`` and
    ``set(persona=...)`` to read/write the persona.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._map: dict[str, ThreadRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning(
                "could not read threads file %s (%s); starting fresh",
                self._path, exc,
            )
            return
        if not isinstance(data, dict):
            log.warning(
                "threads file %s is not a JSON object; starting fresh",
                self._path,
            )
            return
        loaded: dict[str, ThreadRecord] = {}
        for k, v in data.items():
            key = str(k)
            if isinstance(v, str) and v:
                # Pre-routing schema: bare session_id string.
                loaded[key] = ThreadRecord(session_id=v, persona=None)
            elif isinstance(v, dict):
                sid = v.get("session_id")
                if not isinstance(sid, str) or not sid:
                    continue
                persona = v.get("persona")
                loaded[key] = ThreadRecord(
                    session_id=sid,
                    persona=persona if isinstance(persona, str) and persona else None,
                )
        self._map = loaded
        log.info(
            "loaded %d thread record(s) from %s", len(self._map), self._path
        )

    def get(self, thread_ts: str) -> str | None:
        """Backward-compatible accessor: returns just the ``session_id``.

        Single-persona callers (the existing single-tenant bridge and
        all of its tests) only need the session_id; this preserves
        their API. Routing-aware callers should use ``get_record()``.
        """
        rec = self._map.get(thread_ts)
        return rec.session_id if rec is not None else None

    def get_record(self, thread_ts: str) -> ThreadRecord | None:
        """Full record including the persona name (or ``None`` for
        pre-routing entries)."""
        return self._map.get(thread_ts)

    def set(
        self,
        thread_ts: str,
        session_id: str,
        *,
        persona: str | None = None,
    ) -> None:
        """Persist a thread's session + persona. Empty session_id is a
        no-op (matches pre-routing behavior). Re-writing the same
        ``(session_id, persona)`` is also a no-op."""
        if not session_id:
            return
        existing = self._map.get(thread_ts)
        new = ThreadRecord(session_id=session_id, persona=persona)
        if existing == new:
            return
        self._map[thread_ts] = new
        self._save()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(self._path.parent),
            delete=False,
            prefix=".threads.",
            suffix=".tmp",
        )
        try:
            with fd as tf:
                serializable = {
                    k: {"session_id": v.session_id, "persona": v.persona}
                    for k, v in self._map.items()
                }
                json.dump(serializable, tf, indent=2, sort_keys=True)
                tf.write("\n")
            os.replace(fd.name, self._path)
        except Exception:
            try:
                os.unlink(fd.name)
            except OSError:
                pass
            raise

"""Persistent map: Slack ``thread_ts`` -> ``claude -p`` session id.

Without this, every restart wipes the in-memory thread->session map
and any reply-in-thread after the restart starts a fresh Claude session.
File lives under ``$XDG_STATE_HOME/slack-bridge/threads.json`` (default
``~/.local/state/slack-bridge/threads.json``).

Writes are atomic via ``tmp + os.replace``. Read errors fall back to an
empty map with a warning rather than failing the bridge to start.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
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


class ThreadStore:
    """File-backed ``thread_ts -> session_id`` mapping.

    Reads once at construction. Writes synchronously on every ``set`` that
    actually changes a value.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._map: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning(
                "could not read threads file %s (%s); starting fresh",
                self._path,
                exc,
            )
            return
        if not isinstance(data, dict):
            log.warning(
                "threads file %s is not a JSON object; starting fresh",
                self._path,
            )
            return
        self._map = {
            str(k): str(v)
            for k, v in data.items()
            if isinstance(v, str) and v
        }
        log.info(
            "loaded %d thread session(s) from %s", len(self._map), self._path
        )

    def get(self, thread_ts: str) -> str | None:
        return self._map.get(thread_ts)

    def set(self, thread_ts: str, session_id: str) -> None:
        if not session_id:
            return
        if self._map.get(thread_ts) == session_id:
            return
        self._map[thread_ts] = session_id
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
                json.dump(self._map, tf, indent=2, sort_keys=True)
                tf.write("\n")
            os.replace(fd.name, self._path)
        except Exception:
            try:
                os.unlink(fd.name)
            except OSError:
                pass
            raise

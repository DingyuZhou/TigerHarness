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
        "persona": "ayako",           // may be null for pre-routing records
        "team": "Shohoku",            // lane/team name; null on old records
        "last_usage": {...},          // final turn's usage payload, or null
        "last_turn_at": "2026-...Z",  // ISO time of last completed turn
        "in_flight": false            // a bridge turn is running right now
    }

For backward compatibility with the pre-routing schema, a bare string
value (``"<thread_ts>": "abc-def"``) is read as a record with
``persona=None``. Callers handle ``persona=None`` by falling back to the
team's ``default_persona``. All writes use the new dict shape. The
``team`` / ``last_usage`` / ``last_turn_at`` / ``in_flight`` fields exist
for the external idle-compaction pass (``slack-bridge compact-idle``):
the bridge stamps them at its turn boundary, and the pass reads them to
find heavy, quiet lanes it may safely ``/compact``. Records missing them
(written by an older bridge) simply read as "unknown" and are skipped by
that pass.

Writes are atomic via ``tmp + os.replace``. Read errors fall back to an
empty map with a warning rather than failing the bridge to start.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
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


#: Sentinel for ThreadStore.set keyword arguments: "leave the stored
#: value as it is" (an explicit ``None`` means "clear it").
_UNSET: object = object()

#: The only usage fields the store persists -- exactly what
#: ``idle_compact.context_fraction`` reads. Stamping is sanitized to
#: this allowlist so a backend returning an exotic (non-JSON-serializable)
#: usage payload can never poison the store's save path.
_USAGE_KEYS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _sanitize_usage(usage: object) -> dict | None:
    """Reduce a turn's usage payload to the persisted allowlist.

    Non-dict input, or a dict with no positive numeric allowlisted
    fields, reads as None (nothing worth stamping)."""
    if not isinstance(usage, dict):
        return None
    out: dict[str, int] = {}
    for key in _USAGE_KEYS:
        value = usage.get(key)
        if isinstance(value, (int, float)) and value > 0:
            out[key] = int(value)
    return out or None


@dataclass(frozen=True)
class ThreadRecord:
    """One thread's persisted state: which claude session, which persona.

    *persona* is ``None`` only for records read from the pre-routing
    on-disk schema (a bare session-id string). Callers must resolve it
    to the team's ``default_persona`` before dispatch.

    *team* / *last_usage* / *last_turn_at* / *in_flight* are the turn
    metadata the external ``compact-idle`` pass reads; all default to
    the "unknown" values an older on-disk record implies.
    """
    session_id: str
    persona: str | None = None
    team: str | None = None
    last_usage: dict | None = None
    last_turn_at: str | None = None
    in_flight: bool = False


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
        self._map = self._read_disk()
        if self._map:
            log.info(
                "loaded %d thread record(s) from %s", len(self._map), self._path
            )

    def _read_disk(self) -> dict[str, ThreadRecord]:
        """Tolerant parse of the on-disk map (empty on any read error).

        Every write path re-reads through this under the file lock, so a
        write only ever publishes the freshest disk state plus its own
        one-record delta -- never a stale in-memory snapshot of the whole
        map (two processes share this file: the bridge daemon and the
        ``compact-idle`` CLI)."""
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning(
                "could not read threads file %s (%s); starting fresh",
                self._path, exc,
            )
            return {}
        if not isinstance(data, dict):
            log.warning(
                "threads file %s is not a JSON object; starting fresh",
                self._path,
            )
            return {}
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
                team = v.get("team")
                usage = v.get("last_usage")
                turn_at = v.get("last_turn_at")
                loaded[key] = ThreadRecord(
                    session_id=sid,
                    persona=persona if isinstance(persona, str) and persona else None,
                    team=team if isinstance(team, str) and team else None,
                    last_usage=usage if isinstance(usage, dict) else None,
                    last_turn_at=(
                        turn_at if isinstance(turn_at, str) and turn_at else None
                    ),
                    in_flight=bool(v.get("in_flight", False)),
                )
        return loaded

    @contextmanager
    def _locked(self):
        """Exclusive cross-process write lock (flock on a sidecar file).

        Held only around read-merge-write critical sections -- never
        around anything slow (the compact-idle pass sends its ``/compact``
        turns outside the lock)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_name(self._path.name + ".lock")
        with open(lock_path, "w", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def get(self, thread_ts: str) -> str | None:
        """Backward-compatible accessor: returns just the ``session_id``.

        Single-persona callers (the one-persona ``build_bridge`` path
        and its tests) only need the session_id; this preserves
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
        persona: str | None | object = _UNSET,
        team: str | object = _UNSET,
        last_usage: dict | None | object = _UNSET,
        last_turn_at: str | None | object = _UNSET,
        in_flight: bool | object = _UNSET,
    ) -> None:
        """Persist a thread's session + persona (+ turn metadata).

        Empty session_id is a no-op (matches pre-routing behavior).
        Writing an identical record is also a no-op. All keyword fields
        default to *leave the stored value unchanged*; pass an explicit
        ``None`` to clear one. The merge base is the CURRENT ON-DISK
        record (read under the write lock), so concurrent writers -- the
        bridge daemon and the compact-idle CLI -- each publish only their
        own one-record delta and can never clobber the other's writes or
        resurrect values from a stale snapshot. ``last_usage`` is
        sanitized to the token-count allowlist before storing.
        """
        if not session_id:
            return
        if last_usage is not _UNSET and last_usage is not None:
            last_usage = _sanitize_usage(last_usage)

        def _keep(value: object, current: object) -> object:
            return current if value is _UNSET else value

        with self._locked():
            disk = self._read_disk()
            cur = disk.get(thread_ts, ThreadRecord(session_id=""))
            new = ThreadRecord(
                session_id=session_id,
                persona=_keep(persona, cur.persona),   # type: ignore[arg-type]
                team=_keep(team, cur.team),            # type: ignore[arg-type]
                last_usage=_keep(last_usage, cur.last_usage),  # type: ignore[arg-type]
                last_turn_at=_keep(last_turn_at, cur.last_turn_at),  # type: ignore[arg-type]
                in_flight=bool(_keep(in_flight, cur.in_flight)),
            )
            if disk.get(thread_ts) != new:
                disk[thread_ts] = new
                self._write_map(disk)
            self._map = disk

    def mark_in_flight(self, thread_ts: str, flag: bool) -> None:
        """Flip the ``in_flight`` marker on an existing record.

        A thread with no record yet (its first turn is still running)
        is a silent no-op -- the external compact-idle pass cannot see
        that thread either way, so there is nothing to guard. Same
        lock-protected read-merge-write discipline as :meth:`set`."""
        with self._locked():
            disk = self._read_disk()
            existing = disk.get(thread_ts)
            if existing is not None and existing.in_flight != flag:
                disk[thread_ts] = replace(existing, in_flight=flag)
                self._write_map(disk)
            self._map = disk

    def clear_in_flight_all(self) -> None:
        """Bridge-startup sanitization: at process start no turn can be
        running, so any persisted ``in_flight`` marker is a leftover from
        a crash (SIGKILL / host reboot mid-turn). Without this, a
        crash-stuck marker would make the compact-idle pass skip exactly
        the abandoned-heavy lane it exists for, forever."""
        with self._locked():
            disk = self._read_disk()
            changed = False
            for key, rec in disk.items():
                if rec.in_flight:
                    disk[key] = replace(rec, in_flight=False)
                    changed = True
            if changed:
                self._write_map(disk)
            self._map = disk

    def records(self) -> dict[str, ThreadRecord]:
        """Snapshot of all records (read-only copy) -- the compact-idle
        pass iterates this."""
        return dict(self._map)

    def _write_map(self, mapping: dict[str, ThreadRecord]) -> None:
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
                    k: {
                        "session_id": v.session_id,
                        "persona": v.persona,
                        "team": v.team,
                        "last_usage": v.last_usage,
                        "last_turn_at": v.last_turn_at,
                        "in_flight": v.in_flight,
                    }
                    for k, v in mapping.items()
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

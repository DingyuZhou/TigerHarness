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
        "channel": "D0B4L5V7RFG",     // where the thread lives; null on old records
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

Delivery ledger (``SeenLedger``)
--------------------------------

:class:`SeenLedger` lives in this module and shares the store's locking
and atomic-write discipline, but keeps its own sidecar file
(``threads.seen.json``) rather than extending the thread map. Two
reasons: the thread map's on-disk shape is read by an older bridge and
by the ``compact-idle`` CLI, and its records are keyed by *thread*
while delivery has to be tracked for messages that do not have a thread
record yet (the first message of a brand-new DM). Its per-channel entry
is::

    "<channel_id>": {
        "watermark": "1786900916.787969",   // newest ts ever accepted
        "seen": ["1786900916.787969", ...]  // bounded ring, newest last
    }

The ring -- not the watermark -- is the dedup authority: an explicit
per-message marker cannot be fooled by equal timestamps or by messages
arriving out of order, which "is it newer than the watermark?" can. The
watermark exists only to bound how far back a catch-up has to fetch.
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

    *channel* is the Slack conversation the thread lives in. It is what
    lets the reconnect catch-up ask ``conversations.replies`` for the
    replies it missed; a record written before this field existed reads
    as ``None`` and is simply skipped by that pass.
    """
    session_id: str
    persona: str | None = None
    team: str | None = None
    channel: str | None = None
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
                channel = v.get("channel")
                usage = v.get("last_usage")
                turn_at = v.get("last_turn_at")
                loaded[key] = ThreadRecord(
                    session_id=sid,
                    persona=persona if isinstance(persona, str) and persona else None,
                    team=team if isinstance(team, str) and team else None,
                    channel=(
                        channel if isinstance(channel, str) and channel else None
                    ),
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
        channel: str | None | object = _UNSET,
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
                channel=_keep(channel, cur.channel),   # type: ignore[arg-type]
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

    def seen_ledger(self) -> "SeenLedger":
        """The delivery ledger sitting beside this store's file."""
        return SeenLedger(self._path.with_name(self._path.stem + ".seen.json"))

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
                        "channel": v.channel,
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


#: How many recently-delivered message timestamps to retain per channel.
#: The ring only has to outlive the catch-up window: a replay never
#: offers more than ``CatchupConfig.max_messages`` (50) per run, so
#: anything the ring has forgotten is also something the replay will not
#: re-offer. Kept modest on purpose -- this file is read and rewritten
#: once per inbound message, so ``SEEN_RING_MAX * SEEN_CHANNELS_MAX`` is
#: the size of that per-message cost.
SEEN_RING_MAX = 200

#: How many channels the ledger tracks. Far above any realistic bridge
#: (this is DMs plus channels the bot is mentioned in), but a hostile or
#: runaway workspace cannot grow the file without bound.
SEEN_CHANNELS_MAX = 200


@dataclass(frozen=True)
class ChannelDelivery:
    """One channel's delivery state: how far we have got, and exactly
    which messages we handled recently.

    ``channel_type`` is Slack's own routing hint (``"im"`` for a DM),
    copied verbatim off a live event. The replay path re-attaches it
    instead of inferring it from the channel id, so a recovered message
    is routed by the same rule as the one that arrived normally.
    """
    watermark: str | None = None
    seen: tuple[str, ...] = ()
    channel_type: str | None = None


def _ts_sort_key(ts: str) -> float:
    """Slack timestamps sort numerically, not lexically -- ``"9.0" >
    "10.0"`` as strings. A value that is not a Slack ts at all sorts
    oldest so it can never win a watermark comparison."""
    try:
        return float(ts)
    except (TypeError, ValueError):
        return float("-inf")


class SeenLedger:
    """Per-channel record of which inbound messages were already handled.

    The bridge consults this **before** dispatching anything, on the
    normal path as well as the replay path, so the two cannot disagree
    about what "already delivered" means. :meth:`mark` is the whole
    contract: it is a compare-and-set that returns ``True`` exactly once
    per message ts, and it is durable before it returns.

    Shares :class:`ThreadStore`'s discipline -- ``flock`` around
    read-merge-write, atomic ``tmp + os.replace`` publish, tolerant
    parse -- because the same two processes (the bridge daemon and the
    ``compact-idle`` CLI) may hold the file open at once.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    # ----- reads -----

    def _read_disk(self) -> dict[str, ChannelDelivery]:
        """Tolerant parse; an unreadable ledger reads as empty.

        Failing open matters more here than anywhere else in the bridge:
        an empty ledger costs at most one duplicate replayed message,
        while refusing to start costs every message.
        """
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning(
                "could not read delivery ledger %s (%s); starting fresh",
                self._path, exc,
            )
            return {}
        if not isinstance(data, dict):
            log.warning(
                "delivery ledger %s is not a JSON object; starting fresh",
                self._path,
            )
            return {}
        out: dict[str, ChannelDelivery] = {}
        for channel, entry in data.items():
            if not isinstance(entry, dict):
                continue
            watermark = entry.get("watermark")
            ctype = entry.get("channel_type")
            raw_seen = entry.get("seen")
            seen = (
                tuple(t for t in raw_seen if isinstance(t, str) and t)
                if isinstance(raw_seen, list)
                else ()
            )
            out[str(channel)] = ChannelDelivery(
                watermark=(
                    watermark
                    if isinstance(watermark, str) and watermark
                    else None
                ),
                seen=seen[-SEEN_RING_MAX:],
                channel_type=(
                    ctype if isinstance(ctype, str) and ctype else None
                ),
            )
        return out

    def watermark(self, channel: str) -> str | None:
        """Newest ts ever accepted for *channel*, or ``None`` if the
        bridge has never handled a message there."""
        entry = self._read_disk().get(channel)
        return entry.watermark if entry is not None else None

    def channels(self) -> list[tuple[str, str | None]]:
        """``(channel, channel_type)`` for everywhere the bridge has ever
        delivered a message from.

        This is the catch-up's coverage set, and it is a real limit: the
        bridge can only replay conversations it already knows exist. A
        DM opened for the very first time while the socket was down has
        no entry here and is not recovered -- see ``docs/slack-bridge.md``.
        """
        return sorted(
            (channel, entry.channel_type)
            for channel, entry in self._read_disk().items()
        )

    def was_seen(self, channel: str, message_ts: str) -> bool:
        """Read-only probe. :meth:`mark` is the one that decides."""
        entry = self._read_disk().get(channel)
        return entry is not None and message_ts in entry.seen

    # ----- the write that gates dispatch -----

    def mark(
        self,
        channel: str,
        message_ts: str,
        channel_type: str | None = None,
    ) -> bool:
        """Claim *message_ts* for delivery. ``True`` iff this call won.

        A second call with the same ``(channel, message_ts)`` returns
        ``False``, whether it comes from the live socket or from a
        reconnect replay, and whether or not the bridge restarted in
        between -- the claim is on disk before this returns. Callers
        must treat ``False`` as "someone already has this; do nothing".
        """
        if not channel or not message_ts:
            # Nothing to key on. Refusing (rather than dispatching
            # unguarded) keeps "the ledger said yes" honest -- a caller
            # that cannot be deduped must not be replayed either.
            log.warning(
                "delivery ledger refusing an unkeyable message "
                "(channel=%r ts=%r)", channel, message_ts,
            )
            return False
        with self._locked():
            disk = self._read_disk()
            entry = disk.get(channel, ChannelDelivery())
            if message_ts in entry.seen:
                return False
            seen = (entry.seen + (message_ts,))[-SEEN_RING_MAX:]
            watermark = entry.watermark
            if watermark is None or _ts_sort_key(message_ts) > _ts_sort_key(watermark):
                watermark = message_ts
            disk[channel] = ChannelDelivery(
                watermark=watermark,
                seen=seen,
                channel_type=channel_type or entry.channel_type,
            )
            self._write(self._evict(disk))
        return True

    @staticmethod
    def _evict(disk: dict[str, ChannelDelivery]) -> dict[str, ChannelDelivery]:
        """Keep the ledger bounded by dropping the least recently active
        channels. Evicting a channel only costs a re-replay of messages
        the bridge already handled there, which the watermark then
        re-anchors on the next delivery."""
        if len(disk) <= SEEN_CHANNELS_MAX:
            return disk
        ranked = sorted(
            disk.items(),
            key=lambda kv: _ts_sort_key(kv[1].watermark or ""),
            reverse=True,
        )
        dropped = [c for c, _ in ranked[SEEN_CHANNELS_MAX:]]
        log.warning(
            "delivery ledger over %d channels; dropping %d least-recent: %s",
            SEEN_CHANNELS_MAX, len(dropped), ", ".join(dropped),
        )
        return dict(ranked[:SEEN_CHANNELS_MAX])

    # ----- shared write discipline -----

    @contextmanager
    def _locked(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_name(self._path.name + ".lock")
        with open(lock_path, "w", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _write(self, mapping: dict[str, ChannelDelivery]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(self._path.parent),
            delete=False,
            prefix=".seen.",
            suffix=".tmp",
        )
        try:
            with fd as tf:
                json.dump(
                    {
                        channel: {
                            "watermark": entry.watermark,
                            "seen": list(entry.seen),
                            "channel_type": entry.channel_type,
                        }
                        for channel, entry in mapping.items()
                    },
                    tf, indent=2, sort_keys=True,
                )
                tf.write("\n")
            os.replace(fd.name, self._path)
        except Exception:
            try:
                os.unlink(fd.name)
            except OSError:
                pass
            raise

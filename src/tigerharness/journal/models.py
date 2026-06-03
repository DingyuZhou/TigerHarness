"""``status.json`` data model: ``Status`` dataclass + ``State`` enum.

The full schema and state-transition rules live in
``docs/subscription-backend.md`` under "status.json -- the heart". The
short version:

- ``id`` / ``title`` / ``kind`` / ``persona`` are set once by the
  scaffolder; ``state`` advances per the transition table; ``sessions``
  is bumped by the driver on entry; ``updated_at`` is the heartbeat the
  driver refreshes on every progress.md append.
- Phase 1 only supports ``kind="task"``. ``kind="workflow"`` is a
  forward-compatible reservation; the scaffolder rejects it for now.
- There is no ``failed`` state in Phase 1; the human edits ``state=done``
  with a postmortem in ``next_action`` if a task is abandoned.

The dataclass is *deliberately* a plain dict on disk -- the journal
must be human- and AI-readable across vendors, so we ship plain JSON,
not pickle or yaml.
"""

from __future__ import annotations

import datetime as _dt
import enum
import json
from dataclasses import asdict, dataclass, field
from typing import Any


class State(str, enum.Enum):
    """Allowed values for ``Status.state``. Phase 1 omits ``failed`` (see
    module docstring)."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"


# Kinds the scaffolder will accept in Phase 1. ``"workflow"`` is reserved.
_SUPPORTED_KINDS_PHASE_1: frozenset[str] = frozenset({"task"})


class JournalModelError(ValueError):
    """Raised on invalid Status field values (bad state, malformed
    timestamp, unsupported kind, ...). Distinct from generic ValueError
    so callers can pattern-match the journal layer specifically."""


def _utcnow_iso() -> str:
    """ISO 8601 UTC timestamp with a trailing ``Z``. Used for
    ``created_at`` / ``updated_at``. Single source so all journal
    timestamps look identical (tests can monkeypatch this without
    touching ``datetime`` directly)."""
    return (
        _dt.datetime.now(_dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


@dataclass
class Status:
    """The ``status.json`` payload.

    Required fields are positional in the dataclass *order* but always
    set via keyword in practice -- the on-disk JSON is the canonical
    form. ``from_json`` is the only constructor you should use outside
    tests; ``new`` is the convenience for scaffolding fresh entries.
    """

    id: str
    title: str
    kind: str
    state: State
    persona: str
    sessions: int
    max_sessions: int
    created_at: str
    updated_at: str
    next_action: str = ""
    session_ref: str | None = None

    # ---- construction ----

    @classmethod
    def new(
        cls,
        *,
        id: str,
        title: str,
        persona: str,
        kind: str = "task",
        max_sessions: int = 5,
        next_action: str = "",
        now: str | None = None,
    ) -> "Status":
        """Build a freshly-scaffolded Status in ``state=pending``."""
        if kind not in _SUPPORTED_KINDS_PHASE_1:
            raise JournalModelError(
                f"unsupported kind {kind!r}; Phase 1 accepts only "
                f"{sorted(_SUPPORTED_KINDS_PHASE_1)}. "
                "kind=workflow is reserved for a later phase."
            )
        if not title.strip():
            raise JournalModelError("title is required and cannot be blank")
        if not persona.strip():
            raise JournalModelError(
                "persona is required for kind=task and cannot be blank"
            )
        if max_sessions < 1:
            raise JournalModelError(
                f"max_sessions must be >= 1; got {max_sessions}"
            )
        ts = now or _utcnow_iso()
        return cls(
            id=id,
            title=title.strip(),
            kind=kind,
            state=State.PENDING,
            persona=persona.strip(),
            sessions=0,
            max_sessions=max_sessions,
            created_at=ts,
            updated_at=ts,
            next_action=next_action,
            session_ref=None,
        )

    # ---- json round-trip ----

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # State is StrEnum; emit the plain string for forward-compatibility
        # with non-Python readers.
        d["state"] = self.state.value
        return d

    def to_json(self, *, indent: int = 2) -> str:
        """Serialise to JSON. Use ``write_atomic`` on the result."""
        return json.dumps(self.to_dict(), indent=indent) + "\n"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Status":
        """Build from a JSON-decoded dict. Validates required fields,
        the state enum, the kind enum (Phase 1 = task only), and
        non-negativity of ``sessions`` / ``max_sessions``. Forward-
        compat: unknown extra keys raise -- the schema is the contract.

        We mirror the constructor's validations here so a hand-edited
        or migrated-from-future status.json on disk cannot bypass the
        same gates ``Status.new`` enforces (Phase 1 = task only, sane
        session counts, real state enum)."""
        required = {
            "id", "title", "kind", "state", "persona",
            "sessions", "max_sessions",
            "created_at", "updated_at",
        }
        missing = required - set(data)
        if missing:
            raise JournalModelError(
                f"status.json missing required keys: {sorted(missing)}"
            )
        unknown = set(data) - (required | {"next_action", "session_ref"})
        if unknown:
            raise JournalModelError(
                f"status.json has unknown keys: {sorted(unknown)}"
            )
        try:
            state = State(data["state"])
        except ValueError as exc:
            raise JournalModelError(
                f"invalid state {data['state']!r}; allowed: "
                f"{[s.value for s in State]}"
            ) from exc
        kind = data["kind"]
        if kind not in _SUPPORTED_KINDS_PHASE_1:
            raise JournalModelError(
                f"unsupported kind {kind!r} on disk; Phase 1 accepts "
                f"only {sorted(_SUPPORTED_KINDS_PHASE_1)}. kind=workflow "
                "is reserved for a later phase."
            )
        try:
            sessions = int(data["sessions"])
            max_sessions = int(data["max_sessions"])
        except (TypeError, ValueError) as exc:
            raise JournalModelError(
                f"sessions / max_sessions must be ints; got "
                f"{data['sessions']!r} / {data['max_sessions']!r}"
            ) from exc
        if sessions < 0:
            raise JournalModelError(
                f"sessions must be >= 0; got {sessions}"
            )
        if max_sessions < 1:
            raise JournalModelError(
                f"max_sessions must be >= 1; got {max_sessions}"
            )
        return cls(
            id=data["id"],
            title=data["title"],
            kind=kind,
            state=state,
            persona=data["persona"],
            sessions=sessions,
            max_sessions=max_sessions,
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            next_action=data.get("next_action", "") or "",
            session_ref=data.get("session_ref"),
        )

    @classmethod
    def from_json(cls, text: str) -> "Status":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise JournalModelError(
                f"status.json is not valid JSON: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise JournalModelError(
                f"status.json must be a JSON object; got {type(data).__name__}"
            )
        return cls.from_dict(data)

    # ---- heartbeat / staleness ----

    def heartbeat_age_seconds(self, *, now: str | None = None) -> float:
        """Seconds elapsed since ``updated_at``. A negative value (clock
        skew, hand-edited timestamp) is clamped to 0 so the sweep does
        not flag a task as 'stale into the future'.

        Wraps both ``ValueError`` (from ``_parse_iso`` on malformed
        strings) and ``TypeError`` (naive-vs-aware subtraction) as
        ``JournalModelError`` so the sweep's malformed-entry handling
        is the single failure path -- one bad ``updated_at`` should
        flag *that* task as malformed, never abort classification of
        every other task in ``active/``.
        """
        now_str = now or _utcnow_iso()
        try:
            now_dt = _parse_iso(now_str)
            then_dt = _parse_iso(self.updated_at)
            delta = (now_dt - then_dt).total_seconds()
        except (ValueError, TypeError) as exc:
            raise JournalModelError(
                f"cannot compute heartbeat age from updated_at "
                f"{self.updated_at!r}: {exc}"
            ) from exc
        return max(0.0, delta)

    def is_stale(self, *, stuck_timeout_sec: int, now: str | None = None) -> bool:
        """A task is ``stale`` iff it is ``in_progress`` AND its
        heartbeat is older than ``stuck_timeout_sec``."""
        if self.state is not State.IN_PROGRESS:
            return False
        return self.heartbeat_age_seconds(now=now) > stuck_timeout_sec

    def is_fresh_in_progress(
        self, *, stuck_timeout_sec: int, now: str | None = None,
    ) -> bool:
        """The fresh-in-progress classification: ``in_progress`` AND
        heartbeat within ``stuck_timeout_sec``. Step 2 of the OPERATING
        decision procedure must NOT pick these (another session owns
        them right now). The heartbeat is the soft lease."""
        if self.state is not State.IN_PROGRESS:
            return False
        return self.heartbeat_age_seconds(now=now) <= stuck_timeout_sec


def _parse_iso(ts: str) -> _dt.datetime:
    """Parse an ISO 8601 string into an aware datetime. We always emit
    the trailing ``Z``; this helper accepts that *and* the explicit
    ``+00:00`` form so a hand-edited timestamp doesn't crash the sweep.

    Rejects naive timestamps (no ``Z`` and no offset) as ValueError so
    ``heartbeat_age_seconds`` can convert the failure into a
    ``JournalModelError`` and the sweep flags the single bad task as
    malformed instead of aborting. The schema requires UTC ISO 8601 --
    we don't silently treat naive input as UTC because that would mask
    real bugs in a future writer.
    """
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    parsed = _dt.datetime.fromisoformat(ts)
    if parsed.tzinfo is None:
        raise ValueError(
            f"timestamp {ts!r} is naive (no timezone offset); "
            "the schema requires UTC ISO 8601 with a Z or +00:00 suffix"
        )
    return parsed

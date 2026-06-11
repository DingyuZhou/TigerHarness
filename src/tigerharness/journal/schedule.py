"""Recurring task definitions materialized by the drive (T8).

A ``schedule/`` store sits beside ``active/``. Each definition is one
JSON file with a ``next_due`` gate; the lazy sweep materializes a due
definition into a normal pending task via the existing scaffolders.

Exactly-once is a two-phase intent protocol (the journal's claim CAS
applied to the definition file):

- **Phase A** (one atomic write): acquire the lease AND advance
  ``next_due`` AND record the intent (``materializing: {token,
  started_at, due}``). After A, this due can never fire twice.
- **Phase B**: scaffold the task (stamped with ``schedule_def`` +
  ``schedule_due``), then one atomic write recording ``last`` and
  clearing the intent.

Recovery (stale intent only): (a) an ``active/`` instance stamped
with this def-id and due means B's scaffold happened -- close the
intent; (b) ``last.due >= intent.due`` means a cleared crash
duplicate -- just clear; (c) a bounded ``done/`` scan; found = close,
not found = complete phase B now. A lost run is completed and a
completed run is never repeated.

Cadence semantics are WALL-CLOCK: "daily at 08:00" means the next
calendar day at 08:00 *local time* (DST-safe -- the advance recomputes
from the wall clock instead of adding seconds); ``next_due`` is stored
in UTC. Run-late-once: a definition due at 08:00 first swept at 15:00
materializes once, and missed days are never backfilled.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from tigerharness.journal.models import SUPPORTED_AUTONOMY, _utcnow_iso
from tigerharness.journal.paths import JournalPaths

import logging

log = logging.getLogger("tigerharness.journal.schedule")

_PERIODS = ("daily", "weekly")
_AT_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_KINDS = ("task", "workflow")


class ScheduleDefError(ValueError):
    """Invalid schedule definition (bad period/at/payload/JSON)."""


def _parse_iso_utc(value: str) -> _dt.datetime:
    return _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _to_iso_utc(value: _dt.datetime) -> str:
    return (
        value.astimezone(_dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def next_occurrence(
    after_utc: _dt.datetime,
    *,
    period: str,
    at: str,
    tz: _dt.tzinfo | None = None,
) -> _dt.datetime:
    """First wall-clock occurrence of ``at`` strictly after *after_utc*.

    Computed in *tz* (default: the machine's local timezone) so a DST
    shift keeps the wall-clock hour -- never ``+ 86400 seconds``.
    Weekly advances by calendar weeks from the first occurrence.
    Returns an aware UTC datetime.
    """
    m = _AT_RE.fullmatch(at)
    if period not in _PERIODS or not m:
        raise ScheduleDefError(
            f"invalid cadence: period={period!r} at={at!r} "
            f"(period one of {list(_PERIODS)}, at HH:MM)"
        )
    hour, minute = int(m.group(1)), int(m.group(2))
    local_tz = tz or _dt.datetime.now().astimezone().tzinfo
    local_after = after_utc.astimezone(local_tz)
    candidate = local_after.replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    step = _dt.timedelta(days=1 if period == "daily" else 7)
    while candidate <= local_after:
        # Advance by calendar date, then re-pin the wall-clock time in
        # the target tz (DST-safe: the hour is reasserted, not summed).
        next_date = (candidate + step).date()
        candidate = _dt.datetime(
            next_date.year, next_date.month, next_date.day,
            hour, minute, tzinfo=local_tz,
        )
    return candidate.astimezone(_dt.timezone.utc)


@dataclass
class ScheduleDef:
    """One recurring definition (``schedule/<id>.json``)."""

    id: str
    title: str
    period: str
    at: str
    next_due: str  # ISO UTC
    payload: dict[str, Any]
    enabled: bool = True
    materializing: dict[str, Any] | None = None
    last: dict[str, Any] | None = None

    # ---- validation ----

    @staticmethod
    def _validate_payload(payload: dict[str, Any]) -> None:
        kind = payload.get("kind")
        if kind not in _KINDS:
            raise ScheduleDefError(
                f"payload.kind must be one of {list(_KINDS)}; got {kind!r}"
            )
        if kind == "task":
            required = ("prd_text", "persona")
        else:
            required = ("brief_text", "playbook")
        missing = [k for k in required if not payload.get(k)]
        if missing:
            raise ScheduleDefError(
                f"payload for kind={kind} missing {missing}"
            )
        autonomy = payload.get("autonomy", "ask")
        if autonomy not in SUPPORTED_AUTONOMY:
            raise ScheduleDefError(
                f"payload.autonomy must be one of "
                f"{list(SUPPORTED_AUTONOMY)}; got {autonomy!r}"
            )
        # Fail at ADD time, not at the first 8am materialization: a
        # value the scaffolder would reject makes the definition a
        # permanent zombie (b2-sakuragi finding 1).
        max_sessions = payload.get("max_sessions")
        if max_sessions is not None and (
            not isinstance(max_sessions, int) or max_sessions < 1
        ):
            raise ScheduleDefError(
                f"payload.max_sessions must be a positive integer; "
                f"got {max_sessions!r}"
            )

    def validate(self) -> None:
        if not self.id.strip() or not self.title.strip():
            raise ScheduleDefError("id and title are required")
        if self.period not in _PERIODS or not _AT_RE.fullmatch(self.at):
            raise ScheduleDefError(
                f"invalid cadence: period={self.period!r} at={self.at!r}"
            )
        try:
            _parse_iso_utc(self.next_due)
        except ValueError as exc:
            raise ScheduleDefError(
                f"next_due is not ISO-8601: {self.next_due!r}"
            ) from exc
        self._validate_payload(self.payload)

    # ---- json round-trip ----

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "enabled": self.enabled,
            "period": self.period,
            "at": self.at,
            "next_due": self.next_due,
            "payload": self.payload,
        }
        if self.materializing is not None:
            d["materializing"] = self.materializing
        if self.last is not None:
            d["last"] = self.last
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2) + "\n"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScheduleDef":
        if not isinstance(data, dict):
            raise ScheduleDefError("definition must be a JSON object")
        required = {"id", "title", "period", "at", "next_due", "payload"}
        missing = required - set(data)
        if missing:
            raise ScheduleDefError(
                f"definition missing keys: {sorted(missing)}"
            )
        unknown = set(data) - (
            required | {"enabled", "materializing", "last"}
        )
        if unknown:
            raise ScheduleDefError(
                f"definition has unknown keys: {sorted(unknown)}"
            )
        d = cls(
            id=str(data["id"]),
            title=str(data["title"]),
            period=data["period"],
            at=data["at"],
            next_due=data["next_due"],
            payload=dict(data["payload"]),
            enabled=bool(data.get("enabled", True)),
            materializing=data.get("materializing"),
            last=data.get("last"),
        )
        d.validate()
        return d

    @classmethod
    def from_json(cls, text: str) -> "ScheduleDef":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ScheduleDefError(f"not valid JSON: {exc}") from exc
        return cls.from_dict(data)


# ---------------------------------------------------------------------------
# Store helpers
# ---------------------------------------------------------------------------

def schedule_dir(paths: JournalPaths) -> Path:
    return paths.root / "schedule"


def def_path(paths: JournalPaths, def_id: str) -> Path:
    return schedule_dir(paths) / f"{def_id}.json"


def list_def_ids(paths: JournalPaths) -> list[str]:
    d = schedule_dir(paths)
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.json"))


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def save_def(paths: JournalPaths, d: ScheduleDef) -> Path:
    d.validate()
    p = def_path(paths, d.id)
    p.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(p, d.to_json())
    return p


# ---------------------------------------------------------------------------
# Materialization (called from the sweep)
# ---------------------------------------------------------------------------

@dataclass
class MaterializeResult:
    materialized: list[str] = field(default_factory=list)  # task ids
    malformed: list[str] = field(default_factory=list)  # "<def-id>: err"
    skipped_in_flight: list[str] = field(default_factory=list)  # def ids


def _intent_stale(
    materializing: dict[str, Any], now: _dt.datetime, timeout_sec: int,
) -> bool:
    try:
        started = _parse_iso_utc(str(materializing.get("started_at")))
    except ValueError:
        return True  # unreadable intent = stale; recovery owns it
    return (now - started).total_seconds() > timeout_sec


def _scan_for_instance(
    paths: JournalPaths, def_id: str, due: str, *, archived: bool,
) -> str | None:
    """Task id stamped with (def_id, due), in active/ or done/."""
    base = paths.done if archived else paths.active
    if not base.is_dir():
        return None
    for status_path in sorted(base.glob("*/status.json")):
        try:
            data = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (
            isinstance(data, dict)
            and data.get("schedule_def") == def_id
            and data.get("schedule_due") == due
        ):
            return status_path.parent.name
    return None


def _has_in_flight(paths: JournalPaths, def_id: str) -> bool:
    """Any active/ instance of this definition, regardless of due."""
    if not paths.active.is_dir():
        return False
    for status_path in sorted(paths.active.glob("*/status.json")):
        try:
            data = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("schedule_def") == def_id:
            return True
    return False


def _scaffold_from_payload(
    paths: JournalPaths, d: ScheduleDef, due: str,
) -> str:
    """Phase B's scaffold: reuse the existing scaffolders, stamped."""
    # Imported here to avoid a module cycle (scaffold imports models,
    # and the CLI imports both).
    from tigerharness.journal import scaffold as _scaffold

    payload = d.payload
    kind = payload["kind"]
    common = dict(
        max_sessions=int(payload.get("max_sessions") or
                         (3 if kind == "task" else 10)),
        early_exit=bool(payload.get("early_exit", False)),
        autonomy=payload.get("autonomy", "ask"),
        schedule_def=d.id,
        schedule_due=due,
    )
    if kind == "task":
        result = _scaffold.new_task(
            prd_text=payload["prd_text"],
            persona=payload["persona"],
            paths=paths,
            title=d.title,
            **common,
        )
    else:
        result = _scaffold.new_workflow_task(
            brief_text=payload["brief_text"],
            playbook_text=payload["playbook_text"],
            playbook_name=payload["playbook"],
            team_root=Path(payload["team_root"]),
            paths=paths,
            title=d.title,
            captain=payload.get("captain"),
            **common,
        )
    return result.task_id


def materialize_due(
    paths: JournalPaths,
    *,
    now: str | None = None,
    stuck_timeout_sec: int = 1800,
    tz: _dt.tzinfo | None = None,
    cas_hook: Callable[[str, str], None] | None = None,
) -> MaterializeResult:
    """Materialize every due definition, exactly once each.

    Never raises for a bad definition -- the sweep is the drive's
    front door and must stay unbreakable; malformed entries are
    reported in the result instead.

    ``cas_hook(phase, def_id)`` is a test-only seam invoked between
    the CAS read and write (``phase="A"``) and before phase B's
    scaffold (``phase="B"``), so a test can interleave two
    materializers deterministically.
    """
    result = MaterializeResult()
    ts_now = _parse_iso_utc(now or _utcnow_iso())

    for def_id in list_def_ids(paths):
        p = def_path(paths, def_id)
        try:
            d = ScheduleDef.from_json(p.read_text(encoding="utf-8"))
        except (ScheduleDefError, OSError) as exc:
            result.malformed.append(f"{def_id}: {exc}")
            continue

        try:
            # ---- recovery of a stale intent (rare path) ----
            if d.materializing is not None:
                if not _intent_stale(
                    d.materializing, ts_now, stuck_timeout_sec
                ):
                    continue  # a live sweep owns it
                due = str(d.materializing.get("due"))
                task_id = _scan_for_instance(
                    paths, d.id, due, archived=False,
                )
                if task_id is None and d.last and str(
                    d.last.get("due", "")
                ) >= due:
                    task_id = str(d.last.get("task_id"))
                if task_id is None:
                    task_id = _scan_for_instance(
                        paths, d.id, due, archived=True,
                    )
                if task_id is None:
                    # Lost run: complete phase B now.
                    if cas_hook:
                        cas_hook("B", d.id)
                    task_id = _scaffold_from_payload(paths, d, due)
                    result.materialized.append(task_id)
                d.last = {
                    "task_id": task_id, "due": due, "at": _to_iso_utc(ts_now),
                }
                d.materializing = None
                save_def(paths, d)
                continue

            # ---- normal path ----
            if not d.enabled:
                continue
            if _parse_iso_utc(d.next_due) > ts_now:
                continue
            if _has_in_flight(paths, d.id):
                # Prior instance still active: never queue a duplicate,
                # never advance -- the sweep after it finishes fires.
                result.skipped_in_flight.append(d.id)
                continue

            # Phase A: one CAS write -- lease + advance + intent.
            token = uuid.uuid4().hex
            due = d.next_due
            d.materializing = {
                "token": token,
                "started_at": _to_iso_utc(ts_now),
                "due": due,
            }
            d.next_due = _to_iso_utc(next_occurrence(
                ts_now, period=d.period, at=d.at, tz=tz,
            ))
            if cas_hook:
                cas_hook("A", d.id)
            # Precondition re-check (the CAS read half): if another
            # sweep advanced this due or holds a lease since our first
            # read, back off WITHOUT writing -- an unconditional write
            # here would clobber a completed materialization (roll back
            # `last`, resurrect the lease, double-advance next_due).
            fresh = ScheduleDef.from_json(p.read_text(encoding="utf-8"))
            if fresh.materializing is not None or fresh.next_due != due:
                continue  # lost the race; the winner owns this due
            save_def(paths, d)
            if cas_hook:
                cas_hook("POST_WRITE", d.id)
            reread = ScheduleDef.from_json(p.read_text(encoding="utf-8"))
            if (
                reread.materializing is None
                or reread.materializing.get("token") != token
            ):
                continue  # simultaneous write; last writer owns it

            # Phase B: scaffold, then close the intent.
            if cas_hook:
                cas_hook("B", d.id)
            task_id = _scaffold_from_payload(paths, reread, due)
            reread.last = {
                "task_id": task_id, "due": due, "at": _to_iso_utc(ts_now),
            }
            reread.materializing = None
            save_def(paths, reread)
            result.materialized.append(task_id)
        except Exception as exc:  # noqa: BLE001 -- front-door guarantee
            log.exception("schedule: definition %s failed", def_id)
            result.malformed.append(f"{def_id}: {exc}")

    return result

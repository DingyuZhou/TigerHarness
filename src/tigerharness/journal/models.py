"""``status.json`` data model: ``Status`` dataclass + ``State`` enum.

The full schema and state-transition rules live in
``docs/subscription-backend.md`` under "status.json -- the heart" and
``docs/journal-workflow-mode.md`` under "status.json schema". The
short version:

- ``id`` / ``title`` / ``kind`` are set once by the scaffolder.
- ``persona`` is required for ``kind=task``, optional captain (may be
  ``None``) for ``kind=workflow``; per-step personas come from the
  compiled graph in workflow mode.
- ``state`` advances per the transition table; ``sessions`` is bumped
  by the driver on entry; ``updated_at`` is the heartbeat the driver
  refreshes on every progress.md append.
- Phase 1.5 ships ``kind="task"`` AND ``kind="workflow"``. Workflow
  tasks carry two additional fields, ``compile_pending: bool`` and
  ``compile_phase: CompilePhase``, that track the in-session compile
  sub-state machine; both fields are required for workflows and
  rejected for tasks.
- There is no ``failed`` top-level state; a failed compile uses
  ``state=blocked`` paired with ``compile_phase=failed``.

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
    """Allowed values for ``Status.state``. There is no ``failed`` here;
    failed compiles use ``state=blocked`` paired with
    ``compile_phase=failed`` (see :class:`CompilePhase`)."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"


class CompilePhase(str, enum.Enum):
    """The compile-phase sub-machine for ``kind=workflow`` tasks.

    Required for ``kind=workflow``; rejected for ``kind=task``. The
    sub-machine progresses through these values as the in-session
    compile (Option C) runs:

    - ``pending``: just scaffolded; no driver session has picked it up.
    - ``drafting``: a session is producing / re-producing a step bundle
      via the Anzai drafter prompt.
    - ``tier1_pre``: the session is running Tier 1 mechanical
      validators on the current draft before critique.
    - ``critiquing``: the session is mid-critique (Akagi-then-Ayako
      verdict pairs). The hard floor of 3 rounds applies here.
    - ``tier1_post``: the session is running the final defensive Tier 1
      pass before landing.
    - ``complete``: orchestration.json + steps/ are trustworthy; the
      driver may now read them.
    - ``failed``: compile gave up; paired with ``state=blocked``.

    The sweep's classifier treats all in-flight compile sub-phases
    identically to a graph-walking ``in_progress`` task -- the
    soft-lease heartbeat rule applies the same way.
    """

    PENDING = "pending"
    DRAFTING = "drafting"
    TIER1_PRE = "tier1_pre"
    CRITIQUING = "critiquing"
    TIER1_POST = "tier1_post"
    COMPLETE = "complete"
    FAILED = "failed"


# Kinds the scaffolder + reader accept. Historical note: Phase 1
# accepted only "task" via a now-renamed ``_SUPPORTED_KINDS_PHASE_1``
# constant; Phase 1.5 added "workflow".
_SUPPORTED_KINDS: frozenset[str] = frozenset({"task", "workflow"})

# Allowed values for ``Status.autonomy``. "ask" keeps every
# judgment-call pause; "judgement" lets the working persona
# self-resolve yellow-light calls (logged as Decision: entries).
# Red-light rules are never overridable in any mode -- that boundary
# lives in the team charter, not here; this field only carries the
# task's declared level to the persona reading status.json.
SUPPORTED_AUTONOMY: tuple[str, ...] = ("ask", "judgement")

# CompilePhase values are required-and-validated for workflow tasks
# and rejected outright for task tasks. The bare strings are the
# on-disk form (JSON enum values).
_COMPILE_PHASE_VALUES: frozenset[str] = frozenset(
    p.value for p in CompilePhase
)


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
    tests; ``new`` and ``new_workflow`` are the conveniences for
    scaffolding fresh entries (single-persona task vs multi-persona
    workflow).

    Workflow tasks (``kind=workflow``) carry two additional fields
    (``compile_pending`` + ``compile_phase``) absent from task-mode
    statuses; we store them on the dataclass with neutral defaults
    and let ``to_dict`` / ``from_dict`` enforce presence/absence
    based on ``kind`` so the on-disk JSON shape is strict per kind.
    """

    id: str
    title: str
    kind: str
    state: State
    persona: str | None  # required for kind=task; optional captain for workflow
    sessions: int
    max_sessions: int
    created_at: str
    updated_at: str
    next_action: str = ""
    session_ref: str | None = None
    # When False (default), the driver runs the full ``max_sessions``
    # budget -- "N iterations means exactly N". When True, the driver may
    # stop early once the task is done per acceptance criteria (mirrors
    # the retired runner's opt-in ``--early-exit``).
    early_exit: bool = False
    # Detached-run autonomy level (both kinds). ``ask`` (default): the
    # persona pauses on judgment calls per its prompt/charter rules.
    # ``judgement``: the persona may self-resolve yellow-light calls,
    # logging each as a ``Decision:`` entry in progress.md and the
    # final work note. Red-light rules stay non-overridable in every
    # mode (charter-defined).
    autonomy: str = "ask"
    # Set iff this task was materialized from a schedule definition
    # (T8): the definition id and the due timestamp it satisfied.
    # Both kinds; suppressed from JSON when absent. schedule_due is
    # what makes crash recovery's duplicate-detection exact.
    schedule_def: str | None = None
    schedule_due: str | None = None
    # Workflow-only sub-state. Defaults are neutral so a task-mode
    # Status round-trips byte-identically; the JSON schema gate in
    # ``from_dict`` / ``to_dict`` is what makes the per-kind contract
    # strict.
    compile_pending: bool = False
    compile_phase: CompilePhase | None = None
    # Phase 2: the bare playbook name (e.g. ``"default"``) the workflow
    # was scaffolded from. Stored on disk so ``cmd_land_compile`` can
    # emit the truthful name into ``orchestration.json`` and so an
    # operator inspecting ``status.json`` can see which playbook this
    # task was bound to (helpful when a team ships multiple playbooks).
    # Workflow-only; the schema gate enforces presence for
    # ``kind=workflow`` and absence for ``kind=task``.
    playbook_name: str | None = None

    # ---- construction ----

    @classmethod
    def new(
        cls,
        *,
        id: str,
        title: str,
        persona: str,
        kind: str = "task",
        max_sessions: int = 3,
        next_action: str = "",
        early_exit: bool = False,
        autonomy: str = "ask",
        schedule_def: str | None = None,
        schedule_due: str | None = None,
        now: str | None = None,
    ) -> "Status":
        """Build a freshly-scaffolded Status in ``state=pending``.

        This constructor is for ``kind=task`` only. Use
        :meth:`new_workflow` to scaffold ``kind=workflow`` tasks --
        their persona/compile_pending/compile_phase semantics differ
        enough that a shared constructor would be confusing."""
        if kind != "task":
            raise JournalModelError(
                f"Status.new only builds kind=task; got {kind!r}. "
                "Use Status.new_workflow for kind=workflow."
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
        if autonomy not in SUPPORTED_AUTONOMY:
            raise JournalModelError(
                f"autonomy must be one of {list(SUPPORTED_AUTONOMY)}; "
                f"got {autonomy!r}"
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
            early_exit=early_exit,
            autonomy=autonomy,
            schedule_def=schedule_def,
            schedule_due=schedule_due,
            compile_pending=False,
            compile_phase=None,
        )

    @classmethod
    def new_workflow(
        cls,
        *,
        id: str,
        title: str,
        playbook_name: str,
        captain: str | None = None,
        max_sessions: int = 10,
        next_action: str = "",
        early_exit: bool = False,
        autonomy: str = "ask",
        schedule_def: str | None = None,
        schedule_due: str | None = None,
        now: str | None = None,
    ) -> "Status":
        """Build a freshly-scaffolded ``kind=workflow`` Status.

        State is ``pending``, ``compile_pending=True``,
        ``compile_phase=PENDING`` -- the canonical scaffold-time shape
        per ``docs/journal-workflow-mode.md``. The ``captain``
        argument is the optional accountable owner shown by
        ``journal list``; per-step personas come from the compiled
        graph and are unknown at scaffold time. ``captain=None`` is
        legitimate -- the workflow has no single owner.

        ``max_sessions`` defaults to ``10`` here (not ``3`` as for
        tasks) to give the in-session compile a reasonable budget.
        The design proposal called for ``len(steps) * 2 + 3`` but
        ``len(steps)`` is unknown at scaffold time (compile hasn't
        run); ``10`` is the static safe default and operators can
        override via ``--max-sessions``.

        ``playbook_name`` is the bare name of the playbook this
        workflow was scaffolded from (e.g. ``"default"``). It is
        required and stored on disk so ``cmd_land_compile`` can name
        the playbook truthfully in ``orchestration.json``."""
        if not title.strip():
            raise JournalModelError("title is required and cannot be blank")
        if not playbook_name or not playbook_name.strip():
            raise JournalModelError(
                "playbook_name is required for kind=workflow and "
                "cannot be blank"
            )
        if captain is not None and not captain.strip():
            raise JournalModelError(
                "captain must be a non-blank string or None; "
                "got an empty/whitespace string"
            )
        if max_sessions < 1:
            raise JournalModelError(
                f"max_sessions must be >= 1; got {max_sessions}"
            )
        if autonomy not in SUPPORTED_AUTONOMY:
            raise JournalModelError(
                f"autonomy must be one of {list(SUPPORTED_AUTONOMY)}; "
                f"got {autonomy!r}"
            )
        ts = now or _utcnow_iso()
        return cls(
            id=id,
            title=title.strip(),
            kind="workflow",
            state=State.PENDING,
            persona=(captain.strip() if captain else None),
            sessions=0,
            max_sessions=max_sessions,
            created_at=ts,
            updated_at=ts,
            next_action=next_action,
            session_ref=None,
            early_exit=early_exit,
            autonomy=autonomy,
            schedule_def=schedule_def,
            schedule_due=schedule_due,
            compile_pending=True,
            compile_phase=CompilePhase.PENDING,
            playbook_name=playbook_name.strip(),
        )

    # ---- json round-trip ----

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain JSON-friendly dict. Workflow-only fields
        are emitted iff ``kind=workflow``, so a task status's on-disk
        JSON does not gain unknown keys."""
        d = asdict(self)
        # State is a StrEnum; emit the plain string for forward-
        # compatibility with non-Python readers.
        d["state"] = self.state.value
        # Schedule stamps: emitted only when set (both kinds), so an
        # unscheduled task's JSON shape is unchanged.
        if self.schedule_def is None:
            d.pop("schedule_def", None)
        if self.schedule_due is None:
            d.pop("schedule_due", None)
        # Workflow-only fields: emit iff kind=workflow, suppress
        # otherwise so task-mode JSON stays Phase 1 byte-shape.
        if self.kind == "workflow":
            d["compile_phase"] = (
                self.compile_phase.value if self.compile_phase else None
            )
        else:
            d.pop("compile_pending", None)
            d.pop("compile_phase", None)
            d.pop("playbook_name", None)
        return d

    def to_json(self, *, indent: int = 2) -> str:
        """Serialise to JSON. Use ``write_atomic`` on the result."""
        return json.dumps(self.to_dict(), indent=indent) + "\n"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Status":
        """Build from a JSON-decoded dict. Validates required fields,
        the state enum, the kind enum, sane session counts, and the
        per-kind contract on the workflow-only fields
        (``compile_pending`` + ``compile_phase`` must be present for
        ``kind=workflow`` and absent for ``kind=task``).

        Forward-compat: unknown extra keys raise -- the schema is the
        contract. We mirror the constructor's validations here so a
        hand-edited or migrated-from-future status.json on disk cannot
        bypass the same gates ``Status.new`` / ``Status.new_workflow``
        enforce."""
        required = {
            "id", "title", "kind", "state",
            "sessions", "max_sessions",
            "created_at", "updated_at",
        }
        # `persona` is required for kind=task and *optional* (may be
        # null/absent) for kind=workflow; we check it after we know the
        # kind.
        missing = required - set(data)
        if missing:
            raise JournalModelError(
                f"status.json missing required keys: {sorted(missing)}"
            )
        optional_keys = {
            "persona", "next_action", "session_ref", "early_exit",
            "autonomy", "schedule_def", "schedule_due",
            "compile_pending", "compile_phase", "playbook_name",
        }
        unknown = set(data) - (required | optional_keys)
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
        if kind not in _SUPPORTED_KINDS:
            raise JournalModelError(
                f"unsupported kind {kind!r} on disk; allowed: "
                f"{sorted(_SUPPORTED_KINDS)}."
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

        # Per-kind validation of persona + workflow-only fields.
        persona = data.get("persona")
        compile_pending = data.get("compile_pending")
        compile_phase_raw = data.get("compile_phase")
        playbook_name_raw = data.get("playbook_name")
        if kind == "task":
            if persona is None or (
                isinstance(persona, str) and not persona.strip()
            ):
                raise JournalModelError(
                    "persona is required for kind=task and cannot be "
                    "blank / null"
                )
            if not isinstance(persona, str):
                raise JournalModelError(
                    f"persona must be a string for kind=task; got "
                    f"{type(persona).__name__}"
                )
            if compile_pending is not None:
                raise JournalModelError(
                    "compile_pending is rejected for kind=task; remove it"
                )
            if compile_phase_raw is not None:
                raise JournalModelError(
                    "compile_phase is rejected for kind=task; remove it"
                )
            if playbook_name_raw is not None:
                raise JournalModelError(
                    "playbook_name is rejected for kind=task; remove it"
                )
            compile_phase: CompilePhase | None = None
            compile_pending_val = False
            playbook_name: str | None = None
        else:  # kind == "workflow"
            if persona is not None and not isinstance(persona, str):
                raise JournalModelError(
                    f"persona must be a string or null for kind=workflow; "
                    f"got {type(persona).__name__}"
                )
            if isinstance(persona, str) and not persona.strip():
                raise JournalModelError(
                    "persona for kind=workflow must be a non-blank "
                    "captain name OR null; got an empty string"
                )
            if compile_pending is None:
                raise JournalModelError(
                    "compile_pending is required for kind=workflow"
                )
            if not isinstance(compile_pending, bool):
                raise JournalModelError(
                    f"compile_pending must be a bool; got "
                    f"{type(compile_pending).__name__}"
                )
            if compile_phase_raw is None:
                raise JournalModelError(
                    "compile_phase is required for kind=workflow"
                )
            try:
                compile_phase = CompilePhase(compile_phase_raw)
            except ValueError as exc:
                raise JournalModelError(
                    f"invalid compile_phase {compile_phase_raw!r}; "
                    f"allowed: {sorted(_COMPILE_PHASE_VALUES)}"
                ) from exc
            compile_pending_val = compile_pending
            # Phase 2: playbook_name is REQUIRED for kind=workflow.
            # On-disk statuses written before Phase 2 (i.e. by a
            # Phase 1.5 build) won't have the field; rather than
            # silently rebuilding them with a placeholder, we reject
            # so the operator notices and re-scaffolds. There were
            # no production workflow statuses on disk pre-Phase-2
            # per the Operator's note ("nothing important in them").
            if playbook_name_raw is None:
                raise JournalModelError(
                    "playbook_name is required for kind=workflow"
                )
            if not isinstance(playbook_name_raw, str) or \
                    not playbook_name_raw.strip():
                raise JournalModelError(
                    "playbook_name for kind=workflow must be a non-blank "
                    f"string; got {playbook_name_raw!r}"
                )
            playbook_name = playbook_name_raw

        autonomy_val = data.get("autonomy", "ask")
        if autonomy_val not in SUPPORTED_AUTONOMY:
            raise JournalModelError(
                f"autonomy must be one of {list(SUPPORTED_AUTONOMY)}; "
                f"got {autonomy_val!r}"
            )

        return cls(
            id=data["id"],
            title=data["title"],
            kind=kind,
            state=state,
            persona=persona,
            sessions=sessions,
            max_sessions=max_sessions,
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            next_action=data.get("next_action", "") or "",
            session_ref=data.get("session_ref"),
            early_exit=bool(data.get("early_exit", False)),
            autonomy=autonomy_val,
            schedule_def=data.get("schedule_def"),
            schedule_due=data.get("schedule_due"),
            compile_pending=compile_pending_val,
            compile_phase=compile_phase,
            playbook_name=playbook_name,
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
        heartbeat within ``stuck_timeout_sec``. The heartbeat is the soft
        lease. NOTE: with the attach signal (``session_ref``), a *fresh*
        heartbeat only means "do not touch" when a session is actually
        attached -- see :meth:`in_progress_class`, which is what the
        sweep and ``claim`` now use."""
        if self.state is not State.IN_PROGRESS:
            return False
        return self.heartbeat_age_seconds(now=now) <= stuck_timeout_sec

    def in_progress_class(
        self, *, stuck_timeout_sec: int, now: str | None = None,
    ) -> str:
        """Classify an ``in_progress`` task by whether a session is
        *attached* (``session_ref`` set) and, if so, whether its
        heartbeat is fresh.

        The attach signal is decoupled from the heartbeat: ``session_ref``
        answers "is a session driving this right now?"; the heartbeat is
        consulted *only* to catch a crashed owner. Returns one of:

        - ``"idle"``    -- detached (``session_ref is None``): cleanly
          handed off or never claimed. Resumable **immediately** -- no
          heartbeat wait. This is the instant-resume class.
        - ``"busy"``    -- attached + fresh heartbeat: a live session
          owns it right now. Do not touch.
        - ``"crashed"`` -- attached + stale heartbeat: the owning session
          went silent past ``stuck_timeout_sec``. Reclaimable (rescue).

        Raises ``JournalModelError`` if called on a non-``in_progress``
        task (the caller gates on ``state``)."""
        if self.state is not State.IN_PROGRESS:
            raise JournalModelError(
                f"in_progress_class called on state={self.state.value!r}"
            )
        if self.session_ref is None:
            return "idle"
        if self.heartbeat_age_seconds(now=now) > stuck_timeout_sec:
            return "crashed"
        return "busy"


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

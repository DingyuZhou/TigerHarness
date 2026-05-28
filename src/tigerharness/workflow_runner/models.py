"""Typed models for the on-disk workflow journal.

The project does not depend on pydantic (confirmed in
``pyproject.toml``), so we use plain ``@dataclass`` with explicit
``__post_init__`` validation. Every model exposes ``from_dict`` /
``to_dict`` for clean JSON round-trip, and raises
:class:`WorkflowModelError` on shape problems.

Models:

* :class:`StepFrontmatter` -- YAML frontmatter inside a compiled step
  file. Carries routing + per-step limits.
* :class:`StepEdges` -- per-step routing triple
  (``on_approve``/``on_revise``/``on_block``). Stored in
  :class:`Orchestration.edges` so the executor needn't re-parse step
  files at runtime.
* :class:`WorkflowConfig` -- knobs from the playbook's
  ``workflow_config`` HTML-comment block. Defaults match the spec.
* :class:`Orchestration` -- the full compiled plan for one task.
* :class:`StepHistoryEntry` / :class:`Status` -- runtime pointer and
  history; the single source of truth for "where is the task".
* :class:`SessionMap` -- ``{persona: claude_session_id}`` wrapper.
* :class:`Event` -- one structured record from ``events.jsonl``.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Any, ClassVar

# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #


class WorkflowModelError(ValueError):
    """Raised when a model fails validation or fails ``from_dict``.

    Subclasses :class:`ValueError` so callers may also catch it as the
    more familiar built-in if they prefer.
    """


# Step-id sentinels recognised by the routing fields.
_SENTINELS = frozenset({"__done__", "__escalate__"})

# Verdict literals used in step history and trailers.
_VERDICTS = frozenset({"APPROVE", "REVISE", "BLOCK"})

# Phase literals used in Status.
_PHASES = frozenset(
    {"compile", "execute", "paused", "done", "escalated", "cancelled"}
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _require_str(value: Any, field_name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise WorkflowModelError(
            f"{field_name!r} must be a string, got {type(value).__name__}"
        )
    if not allow_empty and not value.strip():
        raise WorkflowModelError(f"{field_name!r} must be non-empty")
    return value


def _require_positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorkflowModelError(
            f"{field_name!r} must be an int, got {type(value).__name__}"
        )
    if value <= 0:
        raise WorkflowModelError(f"{field_name!r} must be > 0, got {value}")
    return value


def _require_non_negative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorkflowModelError(
            f"{field_name!r} must be an int, got {type(value).__name__}"
        )
    if value < 0:
        raise WorkflowModelError(
            f"{field_name!r} must be >= 0, got {value}"
        )
    return value


def _require_non_negative_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WorkflowModelError(
            f"{field_name!r} must be a number, got {type(value).__name__}"
        )
    if value < 0:
        raise WorkflowModelError(
            f"{field_name!r} must be >= 0, got {value}"
        )
    return float(value)


def _require_list_of_str(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list):
        raise WorkflowModelError(
            f"{field_name!r} must be a list, got {type(value).__name__}"
        )
    out: list[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise WorkflowModelError(
                f"{field_name!r}[{i}] must be a non-empty string"
            )
        out.append(item)
    return out


# Looser than full RFC3339 -- we only need to reject "obviously bad" inputs.
_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r"(?:Z|[+-]\d{2}:\d{2})?$"
)


def _require_iso_ts(value: Any, field_name: str) -> str:
    s = _require_str(value, field_name)
    if not _ISO_RE.match(s):
        raise WorkflowModelError(
            f"{field_name!r} must look like ISO-8601 timestamp, got {s!r}"
        )
    return s


def now_iso() -> str:
    """UTC now as ``YYYY-MM-DDTHH:MM:SSZ`` (seconds precision).

    Centralised so all writers produce the same shape and tests can
    monkeypatch one place.
    """
    return (
        _dt.datetime.now(_dt.timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


# --------------------------------------------------------------------------- #
# StepFrontmatter
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class StepFrontmatter:
    """YAML frontmatter of a compiled step file.

    See the "Frontmatter contract" table in ``docs/workflow-runner.md``.
    """

    id: str
    persona: str
    role: str
    on_approve: str
    on_revise: str
    on_block: str
    max_iters: int
    timeout_sec: int
    parallel_with: list[str] = field(default_factory=list)

    REQUIRED_KEYS: ClassVar[tuple[str, ...]] = (
        "id",
        "persona",
        "role",
        "on_approve",
        "on_revise",
        "on_block",
        "max_iters",
        "timeout_sec",
    )

    def __post_init__(self) -> None:
        self.id = _require_str(self.id, "id")
        self.persona = _require_str(self.persona, "persona")
        self.role = _require_str(self.role, "role")
        self.on_approve = _require_str(self.on_approve, "on_approve")
        self.on_revise = _require_str(self.on_revise, "on_revise")
        self.on_block = _require_str(self.on_block, "on_block")
        self.max_iters = _require_positive_int(self.max_iters, "max_iters")
        self.timeout_sec = _require_positive_int(
            self.timeout_sec, "timeout_sec"
        )
        self.parallel_with = _require_list_of_str(
            self.parallel_with, "parallel_with"
        )

    # Convenience accessor used by the executor.
    @property
    def edges(self) -> "StepEdges":
        return StepEdges(
            on_approve=self.on_approve,
            on_revise=self.on_revise,
            on_block=self.on_block,
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "StepFrontmatter":
        if not isinstance(raw, dict):
            raise WorkflowModelError(
                "StepFrontmatter requires a dict, "
                f"got {type(raw).__name__}"
            )
        missing = [k for k in cls.REQUIRED_KEYS if k not in raw]
        if missing:
            raise WorkflowModelError(
                f"StepFrontmatter missing keys: {sorted(missing)}"
            )
        # ``parallel_with`` is optional; pass the raw value straight to
        # the dataclass so that ``_require_list_of_str`` can reject a
        # string (which would otherwise be silently coerced to a list
        # of characters by ``list(...)``).
        pw = raw.get("parallel_with", [])
        if pw is None:
            pw = []
        return cls(
            id=raw["id"],
            persona=raw["persona"],
            role=raw["role"],
            on_approve=raw["on_approve"],
            on_revise=raw["on_revise"],
            on_block=raw["on_block"],
            max_iters=raw["max_iters"],
            timeout_sec=raw["timeout_sec"],
            parallel_with=pw,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "persona": self.persona,
            "role": self.role,
            "on_approve": self.on_approve,
            "on_revise": self.on_revise,
            "on_block": self.on_block,
            "max_iters": self.max_iters,
            "timeout_sec": self.timeout_sec,
            "parallel_with": list(self.parallel_with),
        }


# --------------------------------------------------------------------------- #
# StepEdges
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class StepEdges:
    """Routing triple for a single step."""

    on_approve: str
    on_revise: str
    on_block: str

    def __post_init__(self) -> None:
        _require_str(self.on_approve, "on_approve")
        _require_str(self.on_revise, "on_revise")
        _require_str(self.on_block, "on_block")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "StepEdges":
        if not isinstance(raw, dict):
            raise WorkflowModelError(
                "StepEdges requires a dict, "
                f"got {type(raw).__name__}"
            )
        try:
            return cls(
                on_approve=raw["on_approve"],
                on_revise=raw["on_revise"],
                on_block=raw["on_block"],
            )
        except KeyError as exc:
            raise WorkflowModelError(
                f"StepEdges missing key {exc.args[0]!r}"
            ) from exc

    def to_dict(self) -> dict[str, str]:
        return {
            "on_approve": self.on_approve,
            "on_revise": self.on_revise,
            "on_block": self.on_block,
        }


# --------------------------------------------------------------------------- #
# WorkflowConfig
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class WorkflowConfig:
    """``workflow_config`` knobs (spec defaults baked in)."""

    human_gate: bool = True
    max_compile_iters: int = 8
    max_cost_usd: float = 10.0
    max_loop_iters: int = 5
    step_timeout_sec: int = 1800
    max_task_wall_sec: int = 86400
    allow_parallel: bool = False
    human_gate_approvers: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.human_gate, bool):
            raise WorkflowModelError("'human_gate' must be a bool")
        if not isinstance(self.allow_parallel, bool):
            raise WorkflowModelError("'allow_parallel' must be a bool")
        self.max_compile_iters = _require_positive_int(
            self.max_compile_iters, "max_compile_iters"
        )
        self.max_cost_usd = _require_non_negative_number(
            self.max_cost_usd, "max_cost_usd"
        )
        self.max_loop_iters = _require_positive_int(
            self.max_loop_iters, "max_loop_iters"
        )
        self.step_timeout_sec = _require_positive_int(
            self.step_timeout_sec, "step_timeout_sec"
        )
        self.max_task_wall_sec = _require_positive_int(
            self.max_task_wall_sec, "max_task_wall_sec"
        )
        self.human_gate_approvers = _require_list_of_str(
            self.human_gate_approvers, "human_gate_approvers"
        )
        # Spec invariant (docs/workflow-runner.md, "Human gate"):
        # the allowlist is mandatory when human_gate=True. Compile must
        # fail loudly rather than silently produce a config that the
        # runtime would later refuse to honour.
        if self.human_gate and not self.human_gate_approvers:
            raise WorkflowModelError(
                "'human_gate_approvers' must be non-empty when "
                "'human_gate' is True"
            )

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "WorkflowConfig":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise WorkflowModelError(
                "WorkflowConfig requires a dict, "
                f"got {type(raw).__name__}"
            )
        kwargs: dict[str, Any] = {}
        for key in (
            "human_gate",
            "max_compile_iters",
            "max_cost_usd",
            "max_loop_iters",
            "step_timeout_sec",
            "max_task_wall_sec",
            "allow_parallel",
        ):
            if key in raw:
                kwargs[key] = raw[key]
        if "human_gate_approvers" in raw:
            kwargs["human_gate_approvers"] = list(
                raw["human_gate_approvers"] or []
            )
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "human_gate": self.human_gate,
            "max_compile_iters": self.max_compile_iters,
            "max_cost_usd": self.max_cost_usd,
            "max_loop_iters": self.max_loop_iters,
            "step_timeout_sec": self.step_timeout_sec,
            "max_task_wall_sec": self.max_task_wall_sec,
            "allow_parallel": self.allow_parallel,
            "human_gate_approvers": list(self.human_gate_approvers),
        }


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Orchestration:
    """Compiled plan for one task -- the steps list + edge map + config.

    ``steps`` is append-only after compile (spec rule), but this model
    does not enforce that on its own; the caller (executor /
    workflow-append-steps skill) is responsible for the rule.
    """

    task_id: str
    team: str
    playbook: str
    playbook_sha256: str
    steps: list[str]
    entrypoint: str
    compiled_at: str
    compiled_by: str
    edges: dict[str, StepEdges] = field(default_factory=dict)
    workflow_config: WorkflowConfig = field(default_factory=WorkflowConfig)
    compile_critique_iters: int = 0

    def __post_init__(self) -> None:
        self.task_id = _require_str(self.task_id, "task_id")
        self.team = _require_str(self.team, "team")
        self.playbook = _require_str(self.playbook, "playbook")
        self.playbook_sha256 = _require_str(
            self.playbook_sha256, "playbook_sha256"
        )
        self.steps = _require_list_of_str(self.steps, "steps")
        if len(set(self.steps)) != len(self.steps):
            raise WorkflowModelError("'steps' contains duplicate ids")
        self.entrypoint = _require_str(self.entrypoint, "entrypoint")
        if self.entrypoint not in self.steps:
            raise WorkflowModelError(
                f"entrypoint {self.entrypoint!r} not in steps list"
            )
        self.compiled_at = _require_iso_ts(self.compiled_at, "compiled_at")
        self.compiled_by = _require_str(self.compiled_by, "compiled_by")
        if not isinstance(self.edges, dict):
            raise WorkflowModelError("'edges' must be a dict")
        # Edges' targets may be other step ids OR sentinels; we don't
        # enforce ref-resolution here -- that's the Tier 1 'ref'
        # validator's job in Phase 2.
        for step_id, edge in self.edges.items():
            if not isinstance(edge, StepEdges):
                raise WorkflowModelError(
                    f"edges[{step_id!r}] must be a StepEdges"
                )
            if step_id not in self.steps:
                raise WorkflowModelError(
                    f"edges has entry {step_id!r} not present in steps"
                )
        self.compile_critique_iters = _require_non_negative_int(
            self.compile_critique_iters, "compile_critique_iters"
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Orchestration":
        if not isinstance(raw, dict):
            raise WorkflowModelError(
                "Orchestration requires a dict, "
                f"got {type(raw).__name__}"
            )
        edges_raw = raw.get("edges") or {}
        if not isinstance(edges_raw, dict):
            raise WorkflowModelError("'edges' must be a dict")
        edges = {
            sid: StepEdges.from_dict(val)
            for sid, val in edges_raw.items()
        }
        return cls(
            task_id=raw.get("task_id"),  # type: ignore[arg-type]
            team=raw.get("team"),  # type: ignore[arg-type]
            playbook=raw.get("playbook"),  # type: ignore[arg-type]
            playbook_sha256=raw.get("playbook_sha256"),  # type: ignore[arg-type]
            steps=list(raw.get("steps") or []),
            entrypoint=raw.get("entrypoint"),  # type: ignore[arg-type]
            compiled_at=raw.get("compiled_at"),  # type: ignore[arg-type]
            compiled_by=raw.get("compiled_by"),  # type: ignore[arg-type]
            edges=edges,
            workflow_config=WorkflowConfig.from_dict(
                raw.get("workflow_config")
            ),
            compile_critique_iters=int(
                raw.get("compile_critique_iters", 0)
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "team": self.team,
            "playbook": self.playbook,
            "playbook_sha256": self.playbook_sha256,
            "steps": list(self.steps),
            "entrypoint": self.entrypoint,
            "compiled_at": self.compiled_at,
            "compiled_by": self.compiled_by,
            "edges": {
                sid: edge.to_dict() for sid, edge in self.edges.items()
            },
            "workflow_config": self.workflow_config.to_dict(),
            "compile_critique_iters": self.compile_critique_iters,
        }


# --------------------------------------------------------------------------- #
# Status + history
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class StepHistoryEntry:
    """One row of ``status.step_history``: a single completed iter."""

    step: str
    iter: int
    persona: str
    started_at: str
    ended_at: str | None = None
    verdict: str | None = None
    reason: str | None = None
    cost_usd: float = 0.0

    def __post_init__(self) -> None:
        self.step = _require_str(self.step, "step")
        self.iter = _require_positive_int(self.iter, "iter")
        self.persona = _require_str(self.persona, "persona")
        self.started_at = _require_iso_ts(self.started_at, "started_at")
        if self.ended_at is not None:
            self.ended_at = _require_iso_ts(self.ended_at, "ended_at")
        if self.verdict is not None:
            if self.verdict not in _VERDICTS:
                raise WorkflowModelError(
                    f"verdict must be one of {sorted(_VERDICTS)}, "
                    f"got {self.verdict!r}"
                )
        if self.reason is not None and not isinstance(self.reason, str):
            raise WorkflowModelError("'reason' must be a string or None")
        self.cost_usd = _require_non_negative_number(
            self.cost_usd, "cost_usd"
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "StepHistoryEntry":
        if not isinstance(raw, dict):
            raise WorkflowModelError(
                "StepHistoryEntry requires a dict, "
                f"got {type(raw).__name__}"
            )
        return cls(
            step=raw.get("step"),  # type: ignore[arg-type]
            iter=raw.get("iter"),  # type: ignore[arg-type]
            persona=raw.get("persona"),  # type: ignore[arg-type]
            started_at=raw.get("started_at"),  # type: ignore[arg-type]
            ended_at=raw.get("ended_at"),
            verdict=raw.get("verdict"),
            reason=raw.get("reason"),
            cost_usd=raw.get("cost_usd", 0.0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "iter": self.iter,
            "persona": self.persona,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "verdict": self.verdict,
            "reason": self.reason,
            "cost_usd": self.cost_usd,
        }


@dataclass(slots=True)
class Status:
    """Runtime pointer + counters + verdict history.

    See ``docs/workflow-runner.md`` -- the executor writes this
    atomically on every state transition (write-tmp-then-rename) and
    treats it as the single source of truth for "where the task is".
    """

    task_id: str
    phase: str
    started_at: str
    current_step: str | None = None
    current_iter: int = 0
    step_started_at: str | None = None
    iter_counts: dict[str, int] = field(default_factory=dict)
    cost_usd_total: float = 0.0
    cost_usd_per_step: dict[str, float] = field(default_factory=dict)
    step_history: list[StepHistoryEntry] = field(default_factory=list)
    phase_state: dict[str, Any] = field(default_factory=dict)
    last_heartbeat: str | None = None
    escalation: str | None = None

    def __post_init__(self) -> None:
        self.task_id = _require_str(self.task_id, "task_id")
        if self.phase not in _PHASES:
            raise WorkflowModelError(
                f"phase must be one of {sorted(_PHASES)}, "
                f"got {self.phase!r}"
            )
        self.started_at = _require_iso_ts(self.started_at, "started_at")
        if self.current_step is not None:
            self.current_step = _require_str(
                self.current_step, "current_step"
            )
        self.current_iter = _require_non_negative_int(
            self.current_iter, "current_iter"
        )
        if self.step_started_at is not None:
            self.step_started_at = _require_iso_ts(
                self.step_started_at, "step_started_at"
            )
        if not isinstance(self.iter_counts, dict):
            raise WorkflowModelError("'iter_counts' must be a dict")
        for k, v in self.iter_counts.items():
            _require_str(k, f"iter_counts key {k!r}")
            _require_non_negative_int(v, f"iter_counts[{k!r}]")
        self.cost_usd_total = _require_non_negative_number(
            self.cost_usd_total, "cost_usd_total"
        )
        if not isinstance(self.cost_usd_per_step, dict):
            raise WorkflowModelError("'cost_usd_per_step' must be a dict")
        for k, v in self.cost_usd_per_step.items():
            _require_str(k, f"cost_usd_per_step key {k!r}")
            _require_non_negative_number(v, f"cost_usd_per_step[{k!r}]")
        if not isinstance(self.step_history, list):
            raise WorkflowModelError("'step_history' must be a list")
        for i, entry in enumerate(self.step_history):
            if not isinstance(entry, StepHistoryEntry):
                raise WorkflowModelError(
                    f"step_history[{i}] must be a StepHistoryEntry"
                )
        if not isinstance(self.phase_state, dict):
            raise WorkflowModelError("'phase_state' must be a dict")
        if self.last_heartbeat is not None:
            self.last_heartbeat = _require_iso_ts(
                self.last_heartbeat, "last_heartbeat"
            )
        if self.escalation is not None and not isinstance(
            self.escalation, str
        ):
            raise WorkflowModelError(
                "'escalation' must be a string or None"
            )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Status":
        if not isinstance(raw, dict):
            raise WorkflowModelError(
                "Status requires a dict, "
                f"got {type(raw).__name__}"
            )
        history = [
            StepHistoryEntry.from_dict(e)
            for e in (raw.get("step_history") or [])
        ]
        return cls(
            task_id=raw.get("task_id"),  # type: ignore[arg-type]
            phase=raw.get("phase"),  # type: ignore[arg-type]
            started_at=raw.get("started_at"),  # type: ignore[arg-type]
            current_step=raw.get("current_step"),
            current_iter=raw.get("current_iter", 0),
            step_started_at=raw.get("step_started_at"),
            iter_counts=dict(raw.get("iter_counts") or {}),
            cost_usd_total=raw.get("cost_usd_total", 0.0),
            cost_usd_per_step=dict(raw.get("cost_usd_per_step") or {}),
            step_history=history,
            phase_state=dict(raw.get("phase_state") or {}),
            last_heartbeat=raw.get("last_heartbeat"),
            escalation=raw.get("escalation"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "phase": self.phase,
            "started_at": self.started_at,
            "current_step": self.current_step,
            "current_iter": self.current_iter,
            "step_started_at": self.step_started_at,
            "iter_counts": dict(self.iter_counts),
            "cost_usd_total": self.cost_usd_total,
            "cost_usd_per_step": dict(self.cost_usd_per_step),
            "step_history": [e.to_dict() for e in self.step_history],
            "phase_state": dict(self.phase_state),
            "last_heartbeat": self.last_heartbeat,
            "escalation": self.escalation,
        }


# --------------------------------------------------------------------------- #
# SessionMap
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class SessionMap:
    """Thin wrapper around ``{persona: claude_session_id}``.

    Kept as a class (not a bare dict) so we have a clear name in the
    public API and a single place to add validation if the schema
    grows (e.g. per-persona session metadata).
    """

    sessions: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.sessions, dict):
            raise WorkflowModelError("'sessions' must be a dict")
        for k, v in self.sessions.items():
            _require_str(k, f"sessions key {k!r}")
            _require_str(v, f"sessions[{k!r}]")

    def get(self, persona: str) -> str | None:
        return self.sessions.get(persona)

    def set(self, persona: str, session_id: str) -> None:
        _require_str(persona, "persona")
        _require_str(session_id, "session_id")
        self.sessions[persona] = session_id

    def __contains__(self, persona: object) -> bool:
        return persona in self.sessions

    def __len__(self) -> int:
        return len(self.sessions)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "SessionMap":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise WorkflowModelError(
                "SessionMap requires a dict, "
                f"got {type(raw).__name__}"
            )
        return cls(sessions=dict(raw))

    def to_dict(self) -> dict[str, str]:
        return dict(self.sessions)


# --------------------------------------------------------------------------- #
# Event
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Event:
    """One record from ``events.jsonl``.

    The spec deliberately keeps event shape heterogeneous (every kind
    has its own ad-hoc payload), so we model it as a required
    ``(ts, kind)`` plus a free-form ``extra`` dict. ``to_dict`` flattens
    ``extra`` into the top level for back-compat with the spec's
    examples; ``from_dict`` pops ``ts`` / ``kind`` and stuffs the rest
    back into ``extra``.

    Reserved-key collisions (an entry in ``extra`` named ``"ts"`` or
    ``"kind"``) are rejected at validation time.
    """

    ts: str
    kind: str
    extra: dict[str, Any] = field(default_factory=dict)

    _RESERVED: ClassVar[frozenset[str]] = frozenset({"ts", "kind"})

    def __post_init__(self) -> None:
        self.ts = _require_iso_ts(self.ts, "ts")
        self.kind = _require_str(self.kind, "kind")
        if not isinstance(self.extra, dict):
            raise WorkflowModelError("'extra' must be a dict")
        clash = self._RESERVED & set(self.extra.keys())
        if clash:
            raise WorkflowModelError(
                f"extra cannot redefine reserved keys: {sorted(clash)}"
            )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Event":
        if not isinstance(raw, dict):
            raise WorkflowModelError(
                "Event requires a dict, "
                f"got {type(raw).__name__}"
            )
        d = dict(raw)
        try:
            ts = d.pop("ts")
            kind = d.pop("kind")
        except KeyError as exc:
            raise WorkflowModelError(
                f"Event missing required key {exc.args[0]!r}"
            ) from exc
        return cls(ts=ts, kind=kind, extra=d)

    def to_dict(self) -> dict[str, Any]:
        # Flatten extra into the top-level dict to match the on-disk
        # shape from the spec ("ts" + "kind" + per-kind fields).
        out: dict[str, Any] = {"ts": self.ts, "kind": self.kind}
        out.update(self.extra)
        return out

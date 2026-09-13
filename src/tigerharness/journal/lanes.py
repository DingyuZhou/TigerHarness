"""Drive lanes: which vendor/model a piece of journal work must run on.

A **lane** is one resolved model policy -- a vendor plus a model id
(``tigerharness.vendors``). Every persona resolves to exactly one lane
through ``configs/personas.yaml``; a drive session runs on exactly one
lane (the one its ``--driver`` persona resolves to) and may only take
work whose *owner* persona resolves to the same lane. A team whose
personas all share one policy has one lane and never notices any of
this -- which is the point: the mechanism is invisible until a roster
mixes vendors (ADR 0012).

Who owns a unit of work
-----------------------
- ``kind=task``: the assigned persona (``status.persona``).
- ``kind=workflow``, compile not yet complete: the captain
  (``status.persona``), else the team default. The compile loop adopts
  its drafter/critic roles in one session; it is coordination work and
  runs on the captain's lane as a unit.
- ``kind=workflow``, walk in progress: the persona of the step the walk
  is *at* (``walk.json`` -> ``steps/<id>.md``); before the first
  ``step-done`` that is the graph's entrypoint step.
- ``kind=workflow``, walk terminal: the captain (only ``release`` is
  left).
- a ``deferred/`` inbox entry: the entry's persona, else the team
  default.

The resolver is deliberately tolerant about *reading* (a missing step
file or orchestration falls back to the captain, then the team default)
and deliberately strict about *vendor names* (a typo in personas.yaml
raises, as everywhere else in ``vendors``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tigerharness.vendors import ModelPolicy, describe, resolve_model_policy

log = logging.getLogger("tigerharness.journal.lanes")


@dataclass(frozen=True)
class Lane:
    """One resolved policy, hashable and printable (``key``)."""

    vendor: str
    backend: str
    model: str | None

    @property
    def key(self) -> str:
        return describe(ModelPolicy(self.vendor, self.backend, self.model))

    @classmethod
    def from_policy(cls, policy: ModelPolicy) -> "Lane":
        return cls(vendor=policy.vendor, backend=policy.backend, model=policy.model)


@dataclass(frozen=True)
class LaneCheck:
    """The answer to "may this driver take this work?"."""

    same: bool
    driver: str | None
    driver_lane: Lane
    owner: str | None
    owner_lane: Lane

    def refusal(self, task_id: str) -> str:
        who = self.owner or "the team default persona"
        return (
            f"lane mismatch: {task_id} belongs to {who} on lane "
            f"{self.owner_lane.key}; this drive runs {self.driver} on lane "
            f"{self.driver_lane.key}. Leave it for a drive on that lane "
            f"(autodrive fires one per lane with work), or pass "
            f"--any-lane to take it on this vendor deliberately."
        )


def team_root_for(journal_root: Path) -> Path | None:
    """``<team>/journal`` -> ``<team>`` when that folder is a team root
    (has ``configs/personas.yaml``); ``None`` for a personal journal."""
    parent = Path(journal_root).resolve().parent
    if (parent / "configs" / "personas.yaml").is_file():
        return parent
    return None


def lane_of(
    team_root: Path | None, persona: str | None, *, data: dict[str, Any] | None = None
) -> Lane:
    """The lane *persona* runs on (the team default for ``None`` or an
    unknown name). Raises ``ValueError`` on a malformed vendor."""
    return Lane.from_policy(resolve_model_policy(team_root, persona, data=data))


def _step_persona(task_dir: Path, step_id: str) -> str | None:
    """The persona named in ``steps/<step_id>.md``'s frontmatter, or
    ``None`` when the file cannot be read/parsed (the caller falls back).
    Uses the compile machinery's reader so the parse rules stay single-
    sourced; the import is lazy so kind=task paths never pull wfcore in."""
    try:
        from tigerharness.journal.compile_cli import _read_existing_step

        return _read_existing_step(task_dir, step_id).persona or None
    except Exception as exc:  # ValueError / OSError / ImportError / yaml
        log.debug("lanes: no persona for step %s in %s (%s)", step_id, task_dir, exc)
        return None


def _entrypoint_persona(task_dir: Path) -> str | None:
    """The persona of the compiled graph's entrypoint step, or ``None``."""
    try:
        data = json.loads((task_dir / "orchestration.json").read_text(encoding="utf-8"))
        entry = data.get("entrypoint")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(entry, str) or not entry:
        return None
    return _step_persona(task_dir, entry)


def step_owner(paths: Any, task_id: str, step_id: str) -> str | None:
    """The persona a compiled step names, or ``None`` when unreadable."""
    return _step_persona(paths.task_dir(task_id), step_id)


def work_owner(paths: Any, status: Any) -> str | None:
    """The persona whose lane the next unit of work on *status* needs
    (module docstring rules). ``None`` means "the team default"."""
    captain = status.persona or None
    if status.kind != "workflow":
        return captain
    from tigerharness.journal import walk
    from tigerharness.journal.models import CompilePhase

    if status.compile_phase != CompilePhase.COMPLETE:
        return captain
    task_dir = paths.task_dir(status.id)
    try:
        state = walk.read(paths, status.id)
    except (OSError, ValueError):
        state = None
    if state is None:
        return _entrypoint_persona(task_dir) or captain
    if state.current in walk.SENTINELS:
        return captain
    return _step_persona(task_dir, state.current) or captain


def lane_check(
    team_root: Path | None,
    driver: str | None,
    owner: str | None,
    *,
    data: dict[str, Any] | None = None,
) -> LaneCheck:
    """Compare the driver's lane with the work owner's lane.

    With no team root there are no lanes (a personal journal), and with
    no driver there is no identity to compare -- both read as ``same``.
    """
    driver_lane = lane_of(team_root, driver, data=data)
    owner_lane = lane_of(team_root, owner, data=data)
    same = team_root is None or driver is None or driver_lane == owner_lane
    return LaneCheck(
        same=same, driver=driver, driver_lane=driver_lane,
        owner=owner, owner_lane=owner_lane,
    )


def deferred_owner(paths: Any, entry_id: str) -> str | None:
    """The persona a ``deferred/`` inbox entry names, else ``None``
    (team default). Tolerant: an unreadable entry reads as unowned."""
    try:
        from tigerharness.journal.deferred import read_entry

        return read_entry(paths, entry_id).persona or None
    except Exception as exc:  # noqa: BLE001 -- a broken entry is the sweep's problem
        log.debug("lanes: cannot read deferred entry %s (%s)", entry_id, exc)
        return None

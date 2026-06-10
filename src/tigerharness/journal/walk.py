"""Walk state: the graph-walk cursor for a kind=workflow journal task.

A workflow's compiled graph (``orchestration.json``) is walked one step
at a time during a drive. ``journal step-done`` is the only way to
advance the cursor: it writes the step's persona-attributed worklog
entry AND moves ``current`` to the edge target for the reported verdict.
The cursor lives in a sidecar ``walk.json`` under the task dir, so it
survives ``release``/resume across sessions.

Why a sidecar, enforced in code
-------------------------------
"The note is the ticket to advance." A session cannot skip a step or
fabricate progress: ``step-done`` validates the reported ``--step``
equals the cursor's ``current`` and refuses otherwise, and the cursor
only moves once a worklog entry has been written. The release
completion-check then requires ``current == "__done__"`` before a
workflow can be marked done, so no step's memory can be missed. The
cursor cannot live in status.json -- ``Status.from_dict`` rejects
unknown keys -- so it gets its own file.

yaml-free, like worklog
-----------------------
``walk.json`` is plain JSON (stdlib ``json``), so the core journal
install works without the ``[memory]`` extra. (The step *files* the
walk reads are yaml, but those only exist for kind=workflow tasks,
which already require the compile path.)
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from tigerharness.journal.models import _utcnow_iso
from tigerharness.journal.paths import JournalPaths


# Routing sentinels recognised as terminal walk targets. Mirrors
# ``wfcore.models._SENTINELS`` -- duplicated here (rather than
# imported) to keep the core journal walk independent of wfcore
# at import time; the two literals are a frozen part of the protocol.
DONE = "__done__"
ESCALATE = "__escalate__"
SENTINELS = frozenset({DONE, ESCALATE})


@dataclass(frozen=True)
class WalkStep:
    """One advance in the walk: a step's reported verdict + where it
    routed. The ``history`` is an audit trail (REVISE self-loops show as
    repeated entries); the authoritative per-step memory is the worklog."""

    step: str
    verdict: str
    next: str
    at: str


@dataclass(frozen=True)
class WalkState:
    """The graph-walk cursor for one workflow task.

    ``current`` is the step the walk is *at* -- the next step a turn must
    drive, or a terminal sentinel (``__done__`` / ``__escalate__``) once
    the walk ends.
    """

    task_id: str
    current: str
    history: tuple[WalkStep, ...] = ()
    started_at: str | None = None
    updated_at: str | None = None


# ---------------------------------------------------------------------------
# render / parse
# ---------------------------------------------------------------------------

def to_dict(state: WalkState) -> dict:
    """Plain-dict view of a ``WalkState`` for JSON serialisation."""
    return {
        "task_id": state.task_id,
        "current": state.current,
        "started_at": state.started_at,
        "updated_at": state.updated_at,
        "history": [
            {
                "step": h.step,
                "verdict": h.verdict,
                "next": h.next,
                "at": h.at,
            }
            for h in state.history
        ],
    }


def render(state: WalkState) -> str:
    """Render *state* as pretty JSON text (trailing newline)."""
    return json.dumps(to_dict(state), indent=2, ensure_ascii=False) + "\n"


def parse(text: str) -> WalkState:
    """Parse ``walk.json`` text into a ``WalkState``. Tolerates missing
    optional fields so a hand-edited file still loads."""
    data = json.loads(text)
    history = tuple(
        WalkStep(
            step=str(h.get("step", "")),
            verdict=str(h.get("verdict", "")),
            next=str(h.get("next", "")),
            at=str(h.get("at", "")),
        )
        for h in (data.get("history") or [])
    )
    return WalkState(
        task_id=str(data.get("task_id", "")),
        current=str(data.get("current", "")),
        history=history,
        started_at=data.get("started_at"),
        updated_at=data.get("updated_at"),
    )


# ---------------------------------------------------------------------------
# read / write
# ---------------------------------------------------------------------------

def _write_atomic(path: Path, content: str) -> None:
    """Write *content* to *path* via a same-dir temp file + rename, so a
    reader never sees a half-written file. Mirrors
    ``worklog._write_atomic`` (the journal package keeps a per-module
    copy rather than a shared util)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, path)
    finally:
        try:
            os.unlink(tmp.name)
        except FileNotFoundError:
            pass  # replaced successfully


def read(
    paths: JournalPaths,
    task_id: str,
    *,
    archived: bool = False,
) -> WalkState | None:
    """Read a task's walk cursor. Returns ``None`` if no ``walk.json``
    exists yet (the walk hasn't started). A corrupt file raises
    ``json.JSONDecodeError`` -- the caller decides how to surface it."""
    path = paths.walk_json(task_id, archived=archived)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return parse(text)


def write(
    paths: JournalPaths,
    state: WalkState,
    *,
    archived: bool = False,
) -> None:
    """Persist *state* to the task's ``walk.json`` atomically."""
    _write_atomic(
        paths.walk_json(state.task_id, archived=archived),
        render(state),
    )


# ---------------------------------------------------------------------------
# init / advance
# ---------------------------------------------------------------------------

def initial(task_id: str, entrypoint: str) -> WalkState:
    """A fresh cursor positioned at the graph's entrypoint."""
    now = _utcnow_iso()
    return WalkState(
        task_id=task_id,
        current=entrypoint,
        history=(),
        started_at=now,
        updated_at=now,
    )


def advance(
    state: WalkState,
    *,
    step: str,
    verdict: str,
    next_step: str,
) -> WalkState:
    """Return *state* moved to ``next_step`` with the advance recorded in
    history. Does not write -- the caller persists via :func:`write`."""
    now = _utcnow_iso()
    return replace(
        state,
        current=next_step,
        history=state.history + (
            WalkStep(step=step, verdict=verdict, next=next_step, at=now),
        ),
        updated_at=now,
    )

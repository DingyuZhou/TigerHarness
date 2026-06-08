"""Worklog: per-turn, persona-attributed records under a task directory.

Each persona turn during a journal drive leaves one markdown file:

    active/<task-id>/worklog/NNNN-<persona>-<step>.md   (-> done/ on archive)

That file is the unit tiger-memory ingests so each persona gets its own
memory of the journal work it personally did -- not the driver's. See
``docs/per-persona-journal-memory.md``. The frontmatter carries the
authoritative attribution (``persona``); the filename is a
human-friendly handle.

Why a hand-rolled, yaml-free reader/writer
------------------------------------------
The journal package is part of the *core* install and must keep working
without the ``[memory]`` extra (the only thing that pulls in PyYAML).
The other journal modules already import ``yaml`` lazily and degrade if
it is missing; this module avoids the dependency entirely. It renders
frontmatter as one ``key: <json-scalar>`` line per field -- a strict
subset of YAML whose values are also valid JSON, so they round-trip
with the stdlib ``json`` module alone. ``test_worklog`` additionally
parses the output with the real (yaml-backed)
``tiger_memory.frontmatter`` to prove an external YAML reader sees
identical values.

Concurrency
-----------
A task's worklog directory has a single writer at a time: only the
session currently holding the task's ``session_ref`` (the claim) writes
entries, and the claim is a compare-and-set hand-off. Sequence numbers
are therefore allocated ``max(existing)+1`` without a lock; the
``os.replace`` write is still crash-atomic so a reader never observes a
half-written file.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from tigerharness.journal.ids import slugify
from tigerharness.journal.paths import JournalPaths


_FM_DELIM = "---"
# A worklog filename starts with a zero-padded 4-digit sequence number.
_SEQ_RE = re.compile(r"^(\d{4})-")

# Frontmatter keys, in render order. ``None`` values are omitted so a
# thin driver entry doesn't carry empty ``verdict``/``role`` lines.
_FRONTMATTER_FIELDS: tuple[str, ...] = (
    "task_id",
    "kind",
    "persona",
    "role",
    "step",
    "objective",
    "verdict",
    "reason",
    "started_at",
    "ended_at",
)


@dataclass(frozen=True)
class WorklogEntry:
    """One worklog record: frontmatter fields + a markdown body.

    ``persona`` is the authoritative attribution -- it is stamped by the
    CLI gate from ``status.json`` (kind=task) or ``orchestration.json``
    (kind=workflow), never typed free-hand, so a turn cannot mis-file
    its own memory. ``seq`` and ``path`` are populated on read or after a
    write; they are not part of the frontmatter.
    """

    task_id: str
    persona: str
    step: str
    kind: str = "task"
    role: str | None = None
    objective: str | None = None
    verdict: str | None = None
    reason: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    body: str = ""
    seq: int | None = None
    path: Path | None = None


# ---------------------------------------------------------------------------
# render / parse  (yaml-free; JSON-scalar-per-line subset of YAML)
# ---------------------------------------------------------------------------

def _render_frontmatter(entry: WorklogEntry) -> str:
    lines = [_FM_DELIM]
    for key in _FRONTMATTER_FIELDS:
        value = getattr(entry, key)
        if value is None:
            continue
        # ``ensure_ascii=False`` keeps unicode legible; the result is a
        # valid YAML double-quoted (string) or bare (number) scalar.
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append(_FM_DELIM)
    return "\n".join(lines) + "\n"


def render(entry: WorklogEntry) -> str:
    """Render *entry* as a frontmatter + body markdown string."""
    header = _render_frontmatter(entry)
    body = entry.body or ""
    if body and not body.endswith("\n"):
        body += "\n"
    return header + body


def _parse_value(raw: str):
    raw = raw.strip()
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        # Tolerate a hand-edited bare/quoted scalar that isn't strict
        # JSON (e.g. a single-quoted YAML string). Strip matching outer
        # quotes; otherwise return the raw text.
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
            return raw[1:-1]
        return raw


def parse(text: str) -> tuple[dict, str]:
    """Split frontmatter from body. Returns ``({}, text)`` if the text
    has no leading ``---`` frontmatter block (mirrors
    ``tiger_memory.frontmatter.parse`` so callers can treat them
    interchangeably)."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != _FM_DELIM:
        return {}, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == _FM_DELIM:
            end = i
            break
    if end is None:
        return {}, text
    fm: dict = {}
    for line in lines[1:end]:
        stripped = line.rstrip("\r\n")
        if not stripped.strip() or ":" not in stripped:
            continue
        key, _, raw = stripped.partition(":")
        fm[key.strip()] = _parse_value(raw)
    body = "".join(lines[end + 1:])
    if body.startswith("\n"):
        body = body[1:]
    return fm, body


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------

def _write_atomic(path: Path, content: str) -> None:
    """Write *content* to *path* via a same-dir temp file + rename, so a
    reader never sees a half-written file even if we are SIGKILLed
    mid-write. Mirrors ``scaffold._write_atomic`` (the journal package
    keeps a per-module copy rather than a shared util)."""
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


def _next_seq(worklog_dir: Path) -> int:
    """Next sequence number = max existing + 1 (1 for an empty dir)."""
    max_seq = 0
    if worklog_dir.is_dir():
        for child in worklog_dir.iterdir():
            m = _SEQ_RE.match(child.name)
            if m:
                max_seq = max(max_seq, int(m.group(1)))
    return max_seq + 1


def write_entry(
    paths: JournalPaths,
    entry: WorklogEntry,
    *,
    archived: bool = False,
) -> WorklogEntry:
    """Write *entry* as the next worklog file for its task and return a
    copy stamped with the allocated ``seq`` and ``path``.

    Sequence is auto-allocated unless ``entry.seq`` is already set (the
    caller takes responsibility for collisions in that case). The
    filename embeds slugified ``persona`` and ``step`` as a readable
    handle; the frontmatter holds the authoritative values.
    """
    worklog_dir = paths.worklog(entry.task_id, archived=archived)
    worklog_dir.mkdir(parents=True, exist_ok=True)
    seq = entry.seq if entry.seq is not None else _next_seq(worklog_dir)
    filename = f"{seq:04d}-{slugify(entry.persona)}-{slugify(entry.step)}.md"
    target = worklog_dir / filename
    stamped = replace(entry, seq=seq, path=target)
    _write_atomic(target, render(stamped))
    return stamped


# ---------------------------------------------------------------------------
# read / inspect
# ---------------------------------------------------------------------------

def read_entry(path: Path | str) -> WorklogEntry:
    """Read a single worklog file into a ``WorklogEntry``."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    fm, body = parse(text)
    m = _SEQ_RE.match(path.name)
    seq = int(m.group(1)) if m else None
    return WorklogEntry(
        task_id=str(fm.get("task_id", "")),
        persona=str(fm.get("persona", "")),
        step=str(fm.get("step", "")),
        kind=str(fm.get("kind", "task")),
        role=fm.get("role"),
        objective=fm.get("objective"),
        verdict=fm.get("verdict"),
        reason=fm.get("reason"),
        started_at=fm.get("started_at"),
        ended_at=fm.get("ended_at"),
        body=body,
        seq=seq,
        path=path,
    )


def list_entries(
    paths: JournalPaths,
    task_id: str,
    *,
    archived: bool = False,
) -> list[WorklogEntry]:
    """All worklog entries for a task, ordered by filename (== sequence).
    Returns ``[]`` if the task has no worklog directory yet."""
    worklog_dir = paths.worklog(task_id, archived=archived)
    if not worklog_dir.is_dir():
        return []
    out: list[WorklogEntry] = []
    for child in sorted(worklog_dir.iterdir()):
        if child.suffix != ".md" or not child.is_file():
            continue
        if not _SEQ_RE.match(child.name):
            continue
        out.append(read_entry(child))
    return out


def personas_with_entries(
    paths: JournalPaths,
    task_id: str,
    *,
    archived: bool = False,
) -> set[str]:
    """Set of personas that have at least one worklog entry for a task.
    Used by the ``release``/sweep completion-check backstop."""
    return {
        e.persona
        for e in list_entries(paths, task_id, archived=archived)
        if e.persona
    }


def steps_with_entries(
    paths: JournalPaths,
    task_id: str,
    *,
    archived: bool = False,
) -> set[str]:
    """Set of step ids that have at least one worklog entry for a task.
    Used by the graph-walk completion-check backstop."""
    return {
        e.step
        for e in list_entries(paths, task_id, archived=archived)
        if e.step
    }


def has_entry_for_persona(
    paths: JournalPaths,
    task_id: str,
    persona: str,
    *,
    archived: bool = False,
) -> bool:
    """True if *persona* has any worklog entry for the task."""
    return persona in personas_with_entries(paths, task_id, archived=archived)

"""``new_task``: create a fresh journal task from a PRD.

Called by the ``tigerharness journal new`` CLI and the ``journal-new``
skill. Produces:

- ``active/<task-id>/task.md`` -- the PRD content verbatim.
- ``active/<task-id>/status.json`` -- the seeded ``Status`` in
  ``state=pending``, written atomically.
- ``active/<task-id>/progress.md`` -- empty starter file with a single
  H1 so the driver can append to a real file rather than create it.
- ``active/<task-id>/artifacts/`` -- empty subdirectory the task can
  fill at will.

Also lands ``OPERATING.md`` at the journal root on first use so the
driver can read the vendor-neutral protocol.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from tigerharness.journal.ids import JournalIdError, new_task_id
from tigerharness.journal.models import JournalModelError, Status
from tigerharness.journal.operating_template import OPERATING_MD
from tigerharness.journal.paths import JournalPaths


class JournalScaffoldError(ValueError):
    """Raised when the scaffolder cannot create a task (collision after
    retry, unreadable PRD, ...). Distinct from generic ValueError so
    callers can pattern-match the journal layer specifically."""


@dataclass(frozen=True)
class ScaffoldResult:
    """What the scaffolder produced. Returned to the CLI for the human-
    readable summary."""

    task_id: str
    task_dir: Path
    status: Status


def _first_h1(text: str) -> str:
    """Extract the first H1 heading line from a markdown PRD, or the
    empty string if none. Used to seed ``title`` when ``--title`` is
    not provided."""
    for raw in text.splitlines():
        m = re.match(r"^\s*#\s+(.+?)\s*$", raw)
        if m:
            return m.group(1).strip()
    return ""


def _write_atomic(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` via a same-directory temp file +
    rename. Guarantees a reader never sees a half-written file even if
    the writer is SIGKILLed mid-write."""
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


def _ensure_operating_md(paths: JournalPaths) -> None:
    """Write OPERATING.md at the journal root if it's not there yet.
    Idempotent. Once written, the file is the contract -- subsequent
    scaffolder runs leave it alone so a human edit isn't overwritten."""
    if paths.operating_md.is_file():
        return
    paths.root.mkdir(parents=True, exist_ok=True)
    _write_atomic(paths.operating_md, OPERATING_MD)


def new_task(
    *,
    prd_text: str,
    persona: str,
    paths: JournalPaths,
    title: str | None = None,
    kind: str = "task",
    max_sessions: int = 5,
    slug: str | None = None,
) -> ScaffoldResult:
    """Create a new task in ``paths.active``. Returns ``ScaffoldResult``.

    Workflow:

    1. Derive ``title`` from the ``--title`` arg, else first H1 of the
       PRD, else fall back to ``"task"``.
    2. Mint a task-id via :func:`new_task_id`; collision-check against
       both ``active/`` and ``done/`` so a recently-archived task
       cannot collide.
    3. Build a fresh ``Status`` (validates ``kind``, ``persona``,
       ``max_sessions``).
    4. Atomically write ``task.md`` (the PRD verbatim) and
       ``status.json`` (the seeded Status). Create ``progress.md`` with
       a single H1 + ``artifacts/`` empty.
    5. First-use only: write the canonical ``OPERATING.md`` at the
       journal root.
    """
    if not prd_text.strip():
        raise JournalScaffoldError("PRD is empty; nothing to scaffold")

    paths.ensure()

    effective_title = (title or "").strip() or _first_h1(prd_text) or "task"

    def _exists(candidate: str) -> bool:
        # A candidate id is "taken" if it's in active/ OR done/. Either
        # would create human confusion (re-archival collision later) or
        # a hard-error in JournalPaths.archive.
        return (
            paths.task_exists(candidate, archived=False)
            or paths.task_exists(candidate, archived=True)
        )

    try:
        task_id = new_task_id(
            effective_title,
            slug_overrider=(slug.strip() if slug else None),
            exists_check=_exists,
        )
    except JournalIdError as exc:
        raise JournalScaffoldError(
            f"could not mint a task id: {exc}"
        ) from exc

    try:
        status = Status.new(
            id=task_id,
            title=effective_title,
            persona=persona,
            kind=kind,
            max_sessions=max_sessions,
        )
    except JournalModelError as exc:
        raise JournalScaffoldError(
            f"could not build status.json: {exc}"
        ) from exc

    task_dir = paths.task_dir(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    paths.artifacts(task_id).mkdir(parents=True, exist_ok=True)

    # Write order matters: ``status.json`` must land LAST. The sweep's
    # visibility gate is ``status.json.is_file()`` (paths.list_active_ids),
    # so a SIGKILL or crash between writes must not leave a half-built
    # task visible to the driver. By the time status.json exists on
    # disk, task.md and progress.md already exist.
    _write_atomic(paths.task_md(task_id), prd_text)
    # progress.md is a single H1 starter; not atomic because torn write
    # is harmless (worst case: empty file), and it lands before
    # status.json so the driver's later append targets a real file.
    paths.progress_md(task_id).write_text(
        f"# Progress: {task_id}\n\n", encoding="utf-8",
    )
    # OPERATING.md is at the journal root, not the task dir -- order
    # vs. status.json doesn't matter for task visibility, but it should
    # exist before any drive-journal session reads it.
    _ensure_operating_md(paths)
    # Finally: the status.json that makes the task visible to the sweep.
    _write_atomic(paths.status_json(task_id), status.to_json())

    return ScaffoldResult(task_id=task_id, task_dir=task_dir, status=status)

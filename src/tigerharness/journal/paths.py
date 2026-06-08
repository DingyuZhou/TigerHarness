"""``JournalPaths``: filesystem layout for a journal.

Mirrors ``workflow_runner.paths.TaskPaths`` in spirit. Resolution
priority for the journal root:

1. ``$TIGERHARNESS_JOURNAL_DIR`` (env override)
2. ``<cwd>/journal/`` if ``cwd`` looks like a team directory
   (``configs/personas.yaml`` present) -- the convention scaffolded by
   ``tigerharness init``
3. ``$XDG_STATE_HOME/tigerharness-journal``
4. ``~/.local/state/tigerharness-journal``

Falling back to user-state means a freshly-cloned repo with no team
config still works (you get a per-user journal). The path-layer is
responsible for rejecting unsafe task ids before touching disk so a
malicious or corrupted ``status.json`` cannot make us write outside
the journal root.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from tigerharness.journal.ids import is_safe_task_id


_STATE_DIR_NAME = "tigerharness-journal"


def _is_team_dir(p: Path) -> bool:
    """A team root is any directory that has ``configs/personas.yaml``.
    The check is wrapped in a broad except so an unreadable directory
    (permission error, missing mount) is treated as not-a-team rather
    than crashing the resolver."""
    try:
        return (p / "configs" / "personas.yaml").is_file()
    except OSError:
        return False


def default_journal_root() -> Path:
    """Resolve the journal root per the priority above. Pure function:
    no filesystem mutation, no ``mkdir``. ``~`` is expanded in the env
    override so ``TIGERHARNESS_JOURNAL_DIR=~/journal`` works as a
    well-known shell convention rather than creating a literal ``~/``
    directory."""
    override = os.environ.get("TIGERHARNESS_JOURNAL_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    cwd = Path.cwd()
    if _is_team_dir(cwd):
        return cwd / "journal"
    base = os.environ.get("XDG_STATE_HOME") or str(
        Path.home() / ".local" / "state"
    )
    return Path(base) / _STATE_DIR_NAME


class JournalPathError(ValueError):
    """Raised on unsafe task ids reaching the path layer. Distinct from
    generic ValueError so callers can pattern-match the journal layer."""


@dataclass(frozen=True)
class JournalPaths:
    """File layout for a single journal root.

    ``root`` is the canonical ``journal/`` directory; ``active`` and
    ``done`` hang off of it. Per-task accessors take ``task_id`` and an
    ``archived`` flag (``done/<id>`` vs ``active/<id>``).
    """

    root: Path

    # ---- top-level ----

    @property
    def active(self) -> Path:
        return self.root / "active"

    @property
    def done(self) -> Path:
        return self.root / "done"

    @property
    def operating_md(self) -> Path:
        return self.root / "OPERATING.md"

    @property
    def drive_sessions_json(self) -> Path:
        """Registry of journal *drive* sessions' Slack ``thread_ts`` values,
        for tiger-memory double-count suppression. A top-level dotfile
        under the journal root -- not per-task, because a single drive
        session spans many tasks. Written best-effort at ``journal claim``;
        read by the ``claude_transcript`` source adapter to skip a drive's
        own transcript (the worklog already owns that content). See
        ``journal.drive_sessions`` and
        ``docs/per-persona-journal-memory.md`` (section 4)."""
        return self.root / ".drive-sessions.json"

    # ---- per-task ----

    def task_dir(self, task_id: str, *, archived: bool = False) -> Path:
        if not is_safe_task_id(task_id):
            raise JournalPathError(
                f"unsafe task id {task_id!r}; refusing to compute path"
            )
        return (self.done if archived else self.active) / task_id

    def status_json(self, task_id: str, *, archived: bool = False) -> Path:
        return self.task_dir(task_id, archived=archived) / "status.json"

    def task_md(self, task_id: str, *, archived: bool = False) -> Path:
        return self.task_dir(task_id, archived=archived) / "task.md"

    def progress_md(self, task_id: str, *, archived: bool = False) -> Path:
        return self.task_dir(task_id, archived=archived) / "progress.md"

    def artifacts(self, task_id: str, *, archived: bool = False) -> Path:
        return self.task_dir(task_id, archived=archived) / "artifacts"

    def worklog(self, task_id: str, *, archived: bool = False) -> Path:
        """Per-turn worklog directory for a task. Holds the
        persona-attributed ``NNNN-<persona>-<step>.md`` entries that
        tiger-memory ingests (see ``journal.worklog`` and
        ``docs/per-persona-journal-memory.md``). Survives archival
        because it hangs off ``task_dir``."""
        return self.task_dir(task_id, archived=archived) / "worklog"

    def walk_json(self, task_id: str, *, archived: bool = False) -> Path:
        """Graph-walk cursor sidecar for a kind=workflow task. Tracks the
        step the walk is currently at so ``journal step-done`` can enforce
        in-order advancement and the release completion-check can require
        the walk reached ``__done__`` before a workflow is marked done.

        A sidecar -- *not* status.json, whose schema rejects unknown keys
        (see ``journal.models.Status.from_dict``). Survives archival
        because it hangs off ``task_dir``. See ``journal.walk``."""
        return self.task_dir(task_id, archived=archived) / "walk.json"

    # ---- ensure / inspect ----

    def ensure(self) -> "JournalPaths":
        """Create ``active/`` and ``done/`` under root. Idempotent."""
        self.active.mkdir(parents=True, exist_ok=True)
        self.done.mkdir(parents=True, exist_ok=True)
        return self

    def task_exists(self, task_id: str, *, archived: bool = False) -> bool:
        if not is_safe_task_id(task_id):
            return False
        return self.status_json(task_id, archived=archived).is_file()

    def list_active_ids(self) -> list[str]:
        """Every directory in ``active/`` whose name parses as a safe
        task id and whose ``status.json`` exists. Sorted for determinism."""
        if not self.active.is_dir():
            return []
        out: list[str] = []
        for entry in sorted(self.active.iterdir()):
            if not entry.is_dir():
                continue
            if not is_safe_task_id(entry.name):
                continue
            if not (entry / "status.json").is_file():
                continue
            out.append(entry.name)
        return out

    # ---- archive ----

    def archive(self, task_id: str) -> Path:
        """Move ``active/<id>/`` to ``done/<id>/``. Atomic on the same
        filesystem (single ``rename``). Returns the new (archived) path.

        Raises ``JournalPathError`` if the task is not in ``active/`` or
        if a same-id directory already exists in ``done/`` (re-archival
        without an explicit overwrite would silently lose history)."""
        if not is_safe_task_id(task_id):
            raise JournalPathError(
                f"unsafe task id {task_id!r}; refusing to archive"
            )
        src = self.active / task_id
        if not src.is_dir():
            raise JournalPathError(
                f"cannot archive task {task_id!r}: not in active/"
            )
        dest = self.done / task_id
        if dest.exists():
            raise JournalPathError(
                f"cannot archive task {task_id!r}: done/{task_id} already "
                "exists (refusing to overwrite history)"
            )
        self.done.mkdir(parents=True, exist_ok=True)
        # ``shutil.move`` falls back to a copy+delete across filesystems;
        # plain ``rename`` would only work on the same FS. We accept the
        # cost trade-off for portability.
        shutil.move(str(src), str(dest))
        return dest

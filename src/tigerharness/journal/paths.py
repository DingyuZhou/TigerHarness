"""``JournalPaths``: filesystem layout for a journal.

Mirrors the retired api runner's TaskPaths in spirit. Resolution
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


class JournalRootRefusal(RuntimeError):
    """Team context exists, but resolution would land the work outside
    that team's own ``journal/``. Loud by design: silently falling back
    to the per-user XDG journal is how team-scheduled tasks got lost
    (2026-06-12 incident). Callers map this to exit 2 with the message.
    """

    def __init__(self, *, cwd: Path, expected_root: Path, fix_hint: str):
        self.cwd = cwd
        self.expected_root = expected_root
        self.fix_hint = fix_hint
        super().__init__(
            f"refusing to schedule outside the team's journal: cwd is "
            f"{cwd}, the team's journal root is {expected_root}. "
            f"{fix_hint}"
        )


def resolve_team_journal_root(
    team: str | None = None,
    team_dir: Path | None = None,
) -> Path:
    """The ONE team-pinning resolver (this task's plan, b0): given team
    context, return the pinned ``<team-root>/journal`` -- or raise
    :class:`JournalRootRefusal`. Never silently falls back to the XDG
    state dir; cwd is evidence for error messages only.

    ``team_dir`` (an explicit team root) wins over ``team`` (a name
    resolved per the scaffolder's team-root rules). With NEITHER, this
    function does not apply -- plain personal use keeps
    :func:`default_journal_root`.
    """
    if team_dir is not None:
        root = team_dir.expanduser()
        if not _is_team_dir(root):
            raise JournalRootRefusal(
                cwd=Path.cwd(),
                expected_root=root / "journal",
                fix_hint=(
                    "the --team-dir you passed has no "
                    "configs/personas.yaml -- point it at a real team "
                    "root."
                ),
            )
        return root / "journal"
    if team is None:
        raise ValueError(
            "resolve_team_journal_root needs team or team_dir"
        )
    # Late import: scaffold imports paths at module level; the reverse
    # edge stays function-local to avoid the cycle.
    from tigerharness.journal.scaffold import resolve_team_root
    root = resolve_team_root(team)
    cwd = Path.cwd()
    if _is_team_dir(root) and root.resolve().name != team:
        # The resolved directory's name doesn't match the requested
        # team. Two distinct causes deserve two truthful messages
        # (b2 defense finding: the old single message claimed a false
        # cwd fact): either resolve_team_root's "cwd is a team root"
        # convention matched a DIFFERENT team's folder, or the team
        # NAME itself resolved to a directory named something else
        # (e.g. a path-shaped name like "../outside/Victim" under
        # TIGERHARNESS_TEAMS_DIR). Both refuse; each says what
        # actually happened.
        if root.resolve() == cwd.resolve():
            hint = (
                f"cwd is team {root.resolve().name!r}'s root, not "
                f"{team!r}'s. Run from {team!r}'s own root, or pass "
                f"--team-dir <path-to-{team}>."
            )
        else:
            hint = (
                f"team name {team!r} resolves to a directory named "
                f"{root.resolve().name!r} ({root}) -- team names must "
                f"match their directory. Pass --team-dir for unusual "
                f"layouts."
            )
        raise JournalRootRefusal(
            cwd=cwd,
            expected_root=root / "journal",
            fix_hint=hint,
        )
    if not _is_team_dir(root):
        raise JournalRootRefusal(
            cwd=cwd,
            expected_root=root / "journal",
            fix_hint=(
                f"no team root found for {team!r} from {cwd} (looked "
                f"for configs/personas.yaml at {root}). Run from the "
                f"team's root, set TIGERHARNESS_TEAMS_DIR, or pass "
                f"--team-dir."
            ),
        )
    return root / "journal"


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
    def deferred(self) -> Path:
        """Deferred-task inbox (Phase 3 cheap Slack-side scheduling):
        each entry is a verbatim conversation payload + a small JSON
        sidecar, written by ``journal defer`` (API rail, LLM-free) and
        turned into a real task by ``journal materialize`` inside a
        drive (subscription rail)."""
        return self.root / "deferred"

    @property
    def needs_input(self) -> Path:
        """The ``needs_input/`` tray: tasks a driver parked because they
        need an Operator decision before proceeding. A task moves here
        (from ``active/``) via ``journal release --state needs_input``
        and back (to ``active/``) via ``journal answer``. Living in a
        separate tray keeps parked tasks out of the active queue --- the
        cascade physically cannot pick them up --- and makes them visible
        at a glance (``ls journal/needs_input/``) even with Slack off. See
        ``docs/journal-operator-questions.md``."""
        return self.root / "needs_input"

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

    def needs_input_dir(self, task_id: str) -> Path:
        """Per-task directory in the ``needs_input/`` tray. Same
        safe-id gate as ``task_dir``. Used by ``journal answer`` (read +
        reactivate) and the sweep's tray scan."""
        if not is_safe_task_id(task_id):
            raise JournalPathError(
                f"unsafe task id {task_id!r}; refusing to compute path"
            )
        return self.needs_input / task_id

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
        """Create ``active/``, ``done/`` and ``needs_input/`` under root.
        Idempotent."""
        self.active.mkdir(parents=True, exist_ok=True)
        self.done.mkdir(parents=True, exist_ok=True)
        self.needs_input.mkdir(parents=True, exist_ok=True)
        return self

    def task_exists(self, task_id: str, *, archived: bool = False) -> bool:
        if not is_safe_task_id(task_id):
            return False
        return self.status_json(task_id, archived=archived).is_file()

    def list_active_ids(self) -> list[str]:
        """Every directory in ``active/`` whose name parses as a safe
        task id and whose ``status.json`` exists. Sorted for determinism."""
        return self._list_ids_in(self.active)

    def list_needs_input_ids(self) -> list[str]:
        """Every directory in the ``needs_input/`` tray whose name parses
        as a safe task id and whose ``status.json`` exists. Sorted for
        determinism. Used by the sweep to count + surface parked tasks."""
        return self._list_ids_in(self.needs_input)

    @staticmethod
    def _list_ids_in(folder: Path) -> list[str]:
        """Shared scan: safe-id directories under ``folder`` that hold a
        ``status.json``, sorted."""
        if not folder.is_dir():
            return []
        out: list[str] = []
        for entry in sorted(folder.iterdir()):
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
        return self._move_task(
            task_id, self.active, self.done, verb="archive",
            src_label="active/", dest_label="done/",
        )

    def park(self, task_id: str) -> Path:
        """Move ``active/<id>/`` to ``needs_input/<id>/`` (the park).
        Returns the new tray path. Refuses if the task is not in
        ``active/`` or a same-id directory already exists in the tray."""
        return self._move_task(
            task_id, self.active, self.needs_input, verb="park",
            src_label="active/", dest_label="needs_input/",
        )

    def reactivate(self, task_id: str) -> Path:
        """Move ``needs_input/<id>/`` back to ``active/<id>/`` (the
        Operator's answer re-entry). Returns the new active path. Refuses
        if the task is not in the tray or a same-id directory already
        exists in ``active/``."""
        return self._move_task(
            task_id, self.needs_input, self.active, verb="reactivate",
            src_label="needs_input/", dest_label="active/",
        )

    def _move_task(
        self, task_id: str, src_root: Path, dest_root: Path, *,
        verb: str, src_label: str, dest_label: str,
    ) -> Path:
        """Shared tray-to-tray task move (archive / park / reactivate).
        Atomic on the same filesystem; refuses an unsafe id, a missing
        source, or a colliding destination (overwriting would silently
        lose history)."""
        if not is_safe_task_id(task_id):
            raise JournalPathError(
                f"unsafe task id {task_id!r}; refusing to {verb}"
            )
        src = src_root / task_id
        if not src.is_dir():
            raise JournalPathError(
                f"cannot {verb} task {task_id!r}: not in {src_label}"
            )
        dest = dest_root / task_id
        if dest.exists():
            raise JournalPathError(
                f"cannot {verb} task {task_id!r}: {dest_label}{task_id} "
                "already exists (refusing to overwrite history)"
            )
        dest_root.mkdir(parents=True, exist_ok=True)
        # ``shutil.move`` falls back to a copy+delete across filesystems;
        # plain ``rename`` would only work on the same FS. We accept the
        # cost trade-off for portability.
        shutil.move(str(src), str(dest))
        return dest

"""Per-task journal path layout for the workflow-runner.

The on-disk layout under ``<journal_root>/<task-id>/`` is the single
source of truth for an in-flight workflow task. Everything the
executor needs to resume after a crash lives here::

    <journal_root>/<task-id>/
        status.json            # current pointer + iter counts + history
        orchestration.json     # compiled step list + edges + config
        sessions.json          # {persona: claude_session_id}
        events.jsonl           # append-only machine-truth event stream
        steps/                 # compiled step markdown files
            01-...md
            ...
        logs/<step-id>/iter-NN/
            prompt.txt
            stdout.txt
            stderr.txt
            meta.json
        .lock                  # POSIX flock target (executor mutex)
        .pid                   # writer pid + start + last_heartbeat

The journal root is resolved via the same env / XDG conventions the
``task_runner`` module uses, so users with a customised state dir
get a consistent experience::

    1. $TIGERHARNESS_WORKFLOW_JOURNAL  (explicit override)
    2. $XDG_STATE_HOME/tigerharness-workflows
    3. ~/.local/state/tigerharness-workflows

The exec layer in later sub-steps may also point the env var at a
team-folder location (``teams/<Team>/workflow_journal/``) to satisfy
the discoverability requirement from the spec without hard-coding it
here.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from tigerharness.workflow_runner.ids import validate_step_id

_STATE_DIR_NAME = "tigerharness-workflows"


def default_journal_root() -> Path:
    """Return the default per-task journal root.

    Resolution order:

    1. ``$TIGERHARNESS_WORKFLOW_JOURNAL`` if set and non-empty.
    2. ``$XDG_STATE_HOME/tigerharness-workflows`` if ``XDG_STATE_HOME`` set.
    3. ``~/.local/state/tigerharness-workflows`` otherwise.

    The directory is not created here; call :meth:`TaskPaths.ensure` on
    the derived task directory when you are ready to write.
    """
    override = os.environ.get("TIGERHARNESS_WORKFLOW_JOURNAL", "").strip()
    if override:
        return Path(override)
    base = os.environ.get("XDG_STATE_HOME") or str(
        Path.home() / ".local" / "state"
    )
    return Path(base) / _STATE_DIR_NAME


_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def _slugify(text: str) -> str:
    """Cheap deterministic slug: lowercase, hyphenate, strip junk.

    Empty / all-junk input yields ``"task"`` (so task ids stay parseable).
    """
    cleaned = _SLUG_RE.sub("-", text.lower()).strip("-")
    return cleaned or "task"


def new_task_id(slug: str, *, now: _dt.datetime | None = None) -> str:
    """Mint a task id of the form ``YYYYMMDD-<slug>-<8hex>``.

    Parameters
    ----------
    slug:
        Free-text label; sanitised to ``[a-z0-9-]+``.
    now:
        Optional UTC datetime; defaults to ``datetime.now(UTC)``. Useful
        for deterministic tests.
    """
    when = now if now is not None else _dt.datetime.now(_dt.timezone.utc)
    return (
        f"{when.strftime('%Y%m%d')}-"
        f"{_slugify(slug)}-"
        f"{secrets.token_hex(4)}"
    )


@dataclass(frozen=True, slots=True)
class TaskPaths:
    """Resolved per-task filesystem paths.

    Construct once per task and pass it around. All attributes are
    pure ``pathlib.Path`` instances; nothing is created on disk until
    :meth:`ensure` is called.
    """

    root: Path
    task_id: str

    # ------------------------------------------------------------------ #
    # Computed paths
    # ------------------------------------------------------------------ #

    @property
    def task_dir(self) -> Path:
        return self.root / self.task_id

    @property
    def status_json(self) -> Path:
        return self.task_dir / "status.json"

    @property
    def orchestration_json(self) -> Path:
        return self.task_dir / "orchestration.json"

    @property
    def sessions_json(self) -> Path:
        return self.task_dir / "sessions.json"

    @property
    def events_jsonl(self) -> Path:
        return self.task_dir / "events.jsonl"

    @property
    def steps_dir(self) -> Path:
        return self.task_dir / "steps"

    @property
    def logs_dir(self) -> Path:
        return self.task_dir / "logs"

    @property
    def lock_file(self) -> Path:
        return self.task_dir / ".lock"

    @property
    def pid_file(self) -> Path:
        return self.task_dir / ".pid"

    @property
    def task_brief(self) -> Path:
        return self.task_dir / "task_brief.md"

    @property
    def playbook_snapshot(self) -> Path:
        return self.task_dir / "playbook_snapshot.md"

    @property
    def compile_trace(self) -> Path:
        return self.task_dir / "compile_trace.txt"

    @property
    def compile_critique(self) -> Path:
        return self.task_dir / "compile_critique.md"

    # ------------------------------------------------------------------ #
    # Derived per-iter paths
    # ------------------------------------------------------------------ #

    def step_log_dir(self, step_id: str) -> Path:
        """Return ``logs/<step_id>/`` (not created).

        Re-validates the id so a caller that bypasses model construction
        still fails closed instead of writing outside the task root.
        """
        validate_step_id(step_id)
        return self.logs_dir / step_id

    def iter_dir(self, step_id: str, iter_num: int) -> Path:
        """Return ``logs/<step_id>/iter-NN/`` for a given iter (1-based).

        ``iter_num`` must be >= 1; raises ``ValueError`` otherwise so a
        bad caller fails fast rather than silently creating ``iter-00``.
        """
        if iter_num < 1:
            raise ValueError(
                f"iter_num must be >= 1, got {iter_num!r}"
            )
        return self.step_log_dir(step_id) / f"iter-{iter_num:02d}"

    def step_file(self, step_id: str) -> Path:
        """Return ``steps/<step_id>.md``. Re-validates the id."""
        validate_step_id(step_id)
        return self.steps_dir / f"{step_id}.md"

    # ------------------------------------------------------------------ #
    # Filesystem creation
    # ------------------------------------------------------------------ #

    def ensure(self) -> "TaskPaths":
        """Create the task directory tree if absent.

        Idempotent. Creates ``task_dir``, ``steps_dir``, ``logs_dir``.
        Returns ``self`` for fluent use.
        """
        self.task_dir.mkdir(parents=True, exist_ok=True)
        self.steps_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        return self

    def ensure_iter_dir(self, step_id: str, iter_num: int) -> Path:
        """Create and return ``logs/<step>/iter-NN/``."""
        d = self.iter_dir(step_id, iter_num)
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ------------------------------------------------------------------ #
    # Factories
    # ------------------------------------------------------------------ #

    @classmethod
    def for_task(
        cls, task_id: str, *, root: Path | None = None
    ) -> "TaskPaths":
        """Convenience: use :func:`default_journal_root` when ``root`` is
        ``None``."""
        return cls(root=root if root is not None else default_journal_root(),
                   task_id=task_id)

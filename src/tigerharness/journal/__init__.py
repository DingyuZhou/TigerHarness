"""File-based, human-driven subscription backend (Phase 1).

See ``docs/subscription-backend.md`` for the design. Phase 1 ships:

- The on-disk journal layout: ``active/<task-id>/`` with ``task.md``,
  ``status.json``, ``progress.md``, ``artifacts/``; archived tasks in
  ``done/``; and ``OPERATING.md`` at the journal root.
- The ``Status`` dataclass + state machine.
- The scaffolder: create a task from a PRD.
- The lazy sweep: classify ``in_progress`` tasks as idle / busy /
  crashed (via the ``session_ref`` attach token + heartbeat), archive
  ``done`` tasks, summarise what's actionable.
- A small CLI: ``new`` / ``list`` / ``status`` / ``sweep``.

The driver (``drive-journal`` skill) is intentionally *not* a Python
function in this package -- it is markdown instructions in a SKILL.md
that drive the interactive Claude Code app. The skill instructs the
session to call ``tigerharness journal sweep`` and then follow the
decision procedure in OPERATING.md. See ``skills/drive-journal/``.
"""

from __future__ import annotations

from tigerharness.journal.ids import new_task_id, slugify
from tigerharness.journal.models import State, Status
from tigerharness.journal.paths import JournalPaths, default_journal_root
from tigerharness.journal.scaffold import new_task
from tigerharness.journal.sweep import SweepResult, sweep

__all__ = [
    "JournalPaths",
    "State",
    "Status",
    "SweepResult",
    "default_journal_root",
    "new_task",
    "new_task_id",
    "slugify",
    "sweep",
]

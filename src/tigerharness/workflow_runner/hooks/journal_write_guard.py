"""PreToolUse guard that blocks direct writes to workflow journal files.

Contract
--------
This module is wired into a persona team's ``.claude/settings.json`` as a
``PreToolUse`` hook matching ``Edit|Write|NotebookEdit``. Claude Code feeds
the hook the tool call as a JSON object on stdin::

    {"tool_name": "Edit", "tool_input": {"file_path": "/abs/path"}}

The guard resolves the workflow journal root the same way the executor does
(:func:`tigerharness.workflow_runner.paths.default_journal_root`, honouring
``$TIGERHARNESS_WORKFLOW_JOURNAL`` and the team-folder convention) and
**denies** any write whose target is one of the journal's truth-surface
files:

    <journal_root>/<task-id>/status.json
    <journal_root>/<task-id>/orchestration.json
    <journal_root>/<task-id>/sessions.json
    <journal_root>/<task-id>/events.jsonl
    <journal_root>/<task-id>/steps/<id>.md

A deny exits with code ``2`` and prints :data:`DENY_MESSAGE` to stderr --
the convention Claude Code uses to block a tool call and surface the reason
back to the model. Anything else exits ``0`` (allow): persona scratch under
``logs/``, the verbatim ``task_brief.md`` / ``playbook_snapshot.md`` copies,
the compile traces, and any path outside the journal entirely.

The guard **fails open**: missing / empty / unparseable stdin, a payload
that is not the expected shape, or a tool call carrying no file path all
exit ``0``. A broken hook must never wedge the agent's legitimate edits
everywhere else -- the journal is protected by the program that writes it
regardless, so failing open here only loses defense-in-depth, never
correctness.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tigerharness.workflow_runner.paths import default_journal_root

DENY_MESSAGE = (
    "Direct Edit of workflow journal files is forbidden. Use the\n"
    "workflow-append-steps skill (or another approved workflow skill) to\n"
    "mutate task state."
)

# Files that live directly under ``<journal_root>/<task-id>/`` and form the
# journal's machine-truth surface. Personas mutate these only through the
# workflow_runner module, never with a direct Edit/Write/NotebookEdit.
_PROTECTED_TASK_FILES = frozenset(
    {"status.json", "orchestration.json", "sessions.json", "events.jsonl"}
)


def _extract_target(tool_input: dict) -> str | None:
    """Return the write target from a ``tool_input`` payload, or None.

    ``Edit`` / ``Write`` carry the path in ``file_path``; ``NotebookEdit``
    carries it in ``notebook_path``. A blank or non-string value for one key
    falls through to the next, and exhausting both yields ``None`` so the
    guard fails open rather than crashing on a missing or malformed path.
    """
    for key in ("file_path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _is_protected(target: Path, journal_root: Path) -> bool:
    """Return True iff *target* is a journal truth-surface file.

    Both arguments must already be resolved to absolute, symlink-free form
    so the ``relative_to`` comparison is robust against ``..`` segments and a
    symlinked journal root.
    """
    try:
        rel = target.relative_to(journal_root)
    except ValueError:
        return False  # outside the journal entirely -> allow
    parts = rel.parts
    # parts[0] is the <task-id>; the truth surface lives one level below it.
    if len(parts) == 2 and parts[1] in _PROTECTED_TASK_FILES:
        return True
    if len(parts) == 3 and parts[1] == "steps" and parts[2].endswith(".md"):
        return True
    return False


def main() -> int:
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except ValueError:
        return 0  # fail open: empty / non-JSON stdin
    if not isinstance(data, dict):
        return 0  # fail open: JSON that isn't the expected object
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0  # fail open: no usable tool_input
    target_str = _extract_target(tool_input)
    if target_str is None:
        return 0  # fail open: no file path to guard
    target = Path(target_str).resolve()
    journal_root = default_journal_root().resolve()
    if _is_protected(target, journal_root):
        print(DENY_MESSAGE, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

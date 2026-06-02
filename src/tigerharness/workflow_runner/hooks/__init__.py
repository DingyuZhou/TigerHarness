"""Claude Code hooks that enforce workflow-runner invariants.

These modules are wired into a persona team's ``.claude/settings.json``
(see :func:`tigerharness.init._scaffold_claude_dir`) so they run inside the
persona ``claude -p`` subprocess, not inside the trusted executor. Today the
only hook is :mod:`journal_write_guard`, a ``PreToolUse`` guard that blocks
direct ``Edit`` / ``Write`` / ``NotebookEdit`` writes to the journal's
machine-truth files.
"""

from __future__ import annotations

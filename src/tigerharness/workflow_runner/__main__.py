"""Module entry point: ``python -m tigerharness.workflow_runner``.

Delegates straight to :func:`cli.main`. Mirrors the task-runner's
module entry point.
"""

from __future__ import annotations

import sys

from tigerharness.workflow_runner.cli import main


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

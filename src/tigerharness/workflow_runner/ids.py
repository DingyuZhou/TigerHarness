"""Step-id sanitizer.

Step ids are used as path segments (``logs/<id>/``, ``steps/<id>.md``)
and as argument-shaped tokens in CLI args and log lines, so they must
be filesystem-safe and never start with ``-`` (option-flag confusion)
or ``_`` (reserved for routing sentinels like ``__done__``).

The rules: ASCII alphanumeric or ``_``/``-``, first char alphanumeric,
length 1-64.

Separate module so ``models`` and ``paths`` can both import it without
introducing a cycle.
"""

from __future__ import annotations

import re

STEP_ID_PATTERN: str = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
_STEP_ID_RE = re.compile(STEP_ID_PATTERN)


def validate_step_id(step_id: object) -> None:
    """Raise :class:`WorkflowModelError` if ``step_id`` is not a safe id.

    "Safe" means it matches :data:`STEP_ID_PATTERN`. The exception type
    is imported lazily to keep this module a dependency leaf -- ``models``
    imports from here, not the other way around.
    """
    from tigerharness.workflow_runner.models import WorkflowModelError

    if not isinstance(step_id, str):
        raise WorkflowModelError(
            "step id must be a string, "
            f"got {type(step_id).__name__}"
        )
    if not _STEP_ID_RE.fullmatch(step_id):
        raise WorkflowModelError(
            f"step id {step_id!r} is not path-safe "
            "(need 1-64 chars, starts with [A-Za-z0-9], then [A-Za-z0-9_-])"
        )

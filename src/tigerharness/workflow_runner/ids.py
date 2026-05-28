"""Step-id sanitizer.

Step ids appear in two contexts that bite differently if untrusted:

1. As **path segments** -- ``logs/<step-id>/iter-NN/``,
   ``steps/<step-id>.md``. A step-id of ``..`` or ``foo/bar`` would
   escape the per-task journal and let an attacker (or a buggy
   compile-from-prose pass) clobber files outside the task root.
2. As **argument-shaped tokens** -- step ids show up in CLI args and
   log lines. A leading ``-`` could be misread as an option flag by a
   downstream shell-ish consumer (``rm <step-id>`` is the canonical
   example).

Phase 1 ingests pre-compiled step files written by hand, so the
executor *trusts* the author -- the sanitizer is defense in depth.
Phase 2 introduces an AI-driven compile phase, where untrusted prose
becomes step files; at that point the sanitizer is the active
attack-surface guard. Wiring it in now (and pinning it with tests) is
cheap; retrofitting it after Rukawa builds the executor is not.

The rules (single regex, anchored both ends):

* First char must be ASCII alphanumeric (no leading hyphen, no
  leading underscore -- the latter avoids accidental collision with
  the routing sentinels ``__done__`` / ``__escalate__``).
* Remaining chars: ASCII alphanumeric, ``_``, or ``-``.
* Length 1-64 inclusive.

Rationale for the upper bound: filesystems on every Linux we care
about cap a single path component at 255 bytes, but we also store
step ids in JSON keys (``iter_counts``, ``cost_usd_per_step``) and a
64-byte ceiling keeps the wire format tidy. The spec's example ids
(``01-7f2a-anzai-plan``) sit comfortably under 30 bytes.

Why a separate module: both ``models`` and ``paths`` need this, and a
dedicated leaf keeps the import graph acyclic
(``models``, ``paths`` -> ``ids``; ``ids`` depends on nothing).
"""

from __future__ import annotations

import re

# Mitsui's trailer parser uses ``[A-Za-z0-9_-]+`` for the ``target=``
# field (``trailer.py``). That regex is unanchored and lacks the
# leading-alphanumeric rule -- correct for that context (the parser
# brackets the match between ``target=`` and ``:``) but too lax for
# *minting* path segments. Keep the two regexes intentionally distinct;
# document the relationship here so they don't drift silently.
STEP_ID_PATTERN: str = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
_STEP_ID_RE = re.compile(STEP_ID_PATTERN)


def validate_step_id(step_id: object) -> None:
    """Raise :class:`WorkflowModelError` if ``step_id`` is not a safe id.

    "Safe" means it matches :data:`STEP_ID_PATTERN`: 1-64 chars,
    starts with ASCII alphanumeric, remaining chars are ASCII
    alphanumeric, ``_``, or ``-``.

    Imports :class:`WorkflowModelError` lazily to keep this module a
    true dependency leaf (``models`` imports from us, not the other
    way around).
    """
    # Lazy import to avoid a circular dep: ``models`` imports
    # ``validate_step_id``; this module deferred-imports the exception
    # type only on the failure path.
    from tigerharness.workflow_runner.models import WorkflowModelError

    if not isinstance(step_id, str):
        raise WorkflowModelError(
            "step id must be a string, "
            f"got {type(step_id).__name__}"
        )
    if not _STEP_ID_RE.match(step_id):
        raise WorkflowModelError(
            f"step id {step_id!r} does not match {STEP_ID_PATTERN} "
            "(1-64 chars, starts with [A-Za-z0-9], then [A-Za-z0-9_-])"
        )

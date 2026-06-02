"""Terminal compile-phase failures (Phase 2).

These exceptions are **raised by the compile pipeline**
(``compile/pipeline.py``) and **caught by the CLI** (``cli.cmd_start``),
which maps each tier to a ``compile_failed`` event and a non-zero exit
code.

They live in their own tiny module -- rather than inside
``pipeline.py`` -- on purpose. The pipeline lands in a parallel Phase 2
wave, so the CLI must be able to import and ``except`` these types
*before* the pipeline module exists on the branch. A leaf module with
no heavy imports is the cleanest shared contract: both sides depend on
``compile.errors`` and neither depends on the other.

Payload contract (mirrors ``docs/workflow-runner-phase2.md`` "Event
additions"):

* ``CompileTier1Error.errors`` -> ``compile_failed{tier:1, errors:[...]}``
  -- the structured Tier-1 validator-error list.
* ``CompileTier2Error.last_verdicts`` ->
  ``compile_failed{tier:2, last_verdicts:[...]}`` -- the final critique
  round's verdicts.
"""

from __future__ import annotations

from typing import Any


class CompileError(Exception):
    """Base class for terminal compile-phase failures."""


class CompileTier1Error(CompileError):
    """Tier 1 mechanical validation failed ``max_compile_iters`` times.

    Carries the structured validator-error list so the CLI can surface
    it verbatim in the ``compile_failed`` event without re-deriving it.
    """

    def __init__(
        self,
        message: str = "",
        *,
        errors: list[Any] | None = None,
    ) -> None:
        super().__init__(message or "tier 1 compile validation failed")
        # Defensive copy: the caller's list must not be aliased into the
        # exception (it may keep mutating it while unwinding the loop).
        self.errors: list[Any] = list(errors or [])


class CompileTier2Error(CompileError):
    """Tier 2 critique loop hit the ceiling without dual-APPROVE.

    Carries the final round's verdicts (whatever shape the critique
    loop produces -- a list or a mapping) so the CLI can attach them to
    the ``compile_failed`` event.
    """

    def __init__(
        self,
        message: str = "",
        *,
        last_verdicts: Any = None,
    ) -> None:
        super().__init__(message or "tier 2 critique did not converge")
        self.last_verdicts: Any = (
            last_verdicts if last_verdicts is not None else []
        )

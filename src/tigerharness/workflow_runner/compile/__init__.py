"""Compile phase for the workflow-runner (Phase 2).

Turns a freestyle playbook + a task brief into validated step files.
Wave 1 ships the step drafter (Mitsui) and the Tier 1 mechanical
validators (schema, ref, roster, cycle, dry-run trace -- Miyagi); Wave 2
adds the Tier 2 forced critique loop (Rukawa). The ``compile_playbook``
entrypoint lands alongside the pipeline (see
``docs/workflow-runner-phase2.md``).
"""

from __future__ import annotations

from tigerharness.workflow_runner.compile.critique import (
    CritiqueAbortedError,
    CritiqueParseError,
    CritiqueResult,
    CritiqueRound,
    CritiqueVerdict,
    run_critique_loop,
)
from tigerharness.workflow_runner.compile.drafter import (
    DrafterParseError,
    DrafterResult,
    draft_steps,
)
from tigerharness.workflow_runner.compile.validators import (
    SENTINELS,
    ValidationError,
    ValidationResult,
    build_dry_run_trace,
    validate_compile_output,
    validate_cycles,
    validate_refs,
    validate_roster,
    validate_schema,
)

__all__ = [
    # Tier 2 critique loop (Wave 2, Rukawa)
    "CritiqueAbortedError",
    "CritiqueParseError",
    "CritiqueResult",
    "CritiqueRound",
    "CritiqueVerdict",
    "run_critique_loop",
    # Step drafter (Wave 1, Mitsui)
    "DrafterParseError",
    "DrafterResult",
    "draft_steps",
    # Tier 1 validators (Wave 1, Miyagi)
    "SENTINELS",
    "ValidationError",
    "ValidationResult",
    "build_dry_run_trace",
    "validate_compile_output",
    "validate_cycles",
    "validate_refs",
    "validate_roster",
    "validate_schema",
]

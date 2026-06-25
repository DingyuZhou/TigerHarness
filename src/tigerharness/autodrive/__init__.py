"""``tigerharness autodrive`` -- periodic, vendor-agnostic journal driver.

Public surface is small: the CLI ``main`` and the runner core. See
``runner`` for the safety rationale (this is the Operator-authorized
exception to the journal's "no programmatic driver" rule).
"""

from __future__ import annotations

from .cli import main
from .runner import (
    AutodriveConfig,
    default_prompt,
    is_running,
    run_loop,
    run_one_drive,
    state_path,
)

__all__ = [
    "main",
    "AutodriveConfig",
    "default_prompt",
    "is_running",
    "run_loop",
    "run_one_drive",
    "state_path",
]

"""Shared fixtures + helpers for the compile-phase unit tests.

The drafter (and, in later waves, the critique loop) talk to the LLM
only through the :class:`SessionManager.invoke` seam. :class:`FakeSessionManager`
implements that seam in-memory: it records every call and returns a
single scripted :class:`InvocationResult`, so the tests are fully
deterministic and never spawn a ``claude`` subprocess.

The ``render_step`` / ``make_response`` helpers build the
``steps-bundle`` response shape the drafter parses, so each test can
describe its scenario declaratively instead of hand-writing fenced
markdown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from tigerharness.workflow_runner.sessions import InvocationResult


# --------------------------------------------------------------------------- #
# Fake SessionManager
# --------------------------------------------------------------------------- #


@dataclass
class RecordedCall:
    """One captured ``invoke`` call (for prompt / arg assertions)."""

    persona: str
    prompt: str
    timeout_sec: int
    log_dir: Optional[Path]


class FakeSessionManager:
    """In-memory stand-in for :class:`SessionManager`.

    Returns one scripted :class:`InvocationResult` per ``invoke`` call
    and records the call so tests can assert on the prompt and args.
    A single script is reused for every call (the drafter invokes once
    per :func:`draft_steps`).
    """

    def __init__(
        self,
        *,
        stdout: str = "",
        cost_usd: float = 0.0,
        error: Optional[str] = None,
        session_id: str = "sid-anzai",
        exit_code: int = 0,
    ) -> None:
        self._stdout = stdout
        self._cost_usd = cost_usd
        self._error = error
        self._session_id = session_id
        self._exit_code = exit_code
        self.calls: list[RecordedCall] = []

    def invoke(
        self,
        persona: str,
        prompt: str,
        *,
        timeout_sec: int,
        log_dir: Optional[Path] = None,
    ) -> InvocationResult:
        self.calls.append(
            RecordedCall(
                persona=persona,
                prompt=prompt,
                timeout_sec=timeout_sec,
                log_dir=log_dir,
            )
        )
        return InvocationResult(
            stdout=self._stdout,
            session_id=self._session_id,
            cost_usd=self._cost_usd,
            exit_code=self._exit_code,
            error=self._error,
            raw_envelope={},
        )


# --------------------------------------------------------------------------- #
# Response-rendering helpers
# --------------------------------------------------------------------------- #


@dataclass
class StepSpec:
    """Declarative description of one rendered step file."""

    id: str
    persona: str
    role: str
    on_approve: str
    on_revise: str
    on_block: str = "__escalate__"
    max_iters: int = 5
    timeout_sec: int = 1800
    parallel_with: str = "[]"
    body: str = "Do the thing.\n"
    # Extra raw frontmatter lines (e.g. an unknown ``bogus_field: foo``).
    extra_fm_lines: list[str] = field(default_factory=list)
    # When True, omit the closing ``---`` so the chunk is malformed.
    drop_close_delim: bool = False
    # When True, omit the opening ``---`` so the chunk is malformed.
    drop_open_delim: bool = False
    # When True, omit the ``## step:`` header for this step.
    drop_header: bool = False
    # Blank lines inserted between the header and the opening delimiter.
    blank_lines_after_header: int = 0


def render_step(spec: StepSpec) -> str:
    """Render one ``StepSpec`` into its ``## step:`` + frontmatter + body."""
    lines: list[str] = []
    if not spec.drop_header:
        lines.append(f"## step: {spec.id}")
    lines.extend([""] * spec.blank_lines_after_header)
    if not spec.drop_open_delim:
        lines.append("---")
    lines.extend(
        [
            f"id: {spec.id}",
            f"persona: {spec.persona}",
            f"role: {spec.role}",
            f"on_approve: {spec.on_approve}",
            f"on_revise: {spec.on_revise}",
            f"on_block: {spec.on_block}",
            f"max_iters: {spec.max_iters}",
            f"timeout_sec: {spec.timeout_sec}",
            f"parallel_with: {spec.parallel_with}",
        ]
    )
    lines.extend(spec.extra_fm_lines)
    if not spec.drop_close_delim:
        lines.append("---")
    lines.append("")
    lines.append(spec.body)
    return "\n".join(lines)


def make_response(
    steps: list[StepSpec],
    *,
    open_fence: bool = True,
    close_fence: bool = True,
    trailer: bool = True,
) -> str:
    """Wrap rendered steps in the ``steps-bundle`` fence + WORKFLOW trailer."""
    parts: list[str] = []
    if open_fence:
        parts.append("```steps-bundle")
    parts.extend(render_step(s) for s in steps)
    if close_fence:
        parts.append("```")
    if trailer:
        parts.append("")
        parts.append("WORKFLOW: APPROVE")
    return "\n".join(parts) + "\n"


def three_step_specs() -> list[StepSpec]:
    """A canonical valid 3-step plan: plan -> critique -> revise -> done."""
    return [
        StepSpec(
            id="01-anzai-plan",
            persona="anzai",
            role="planner",
            on_approve="02-akagi-critique",
            on_revise="01-anzai-plan",
        ),
        StepSpec(
            id="02-akagi-critique",
            persona="akagi",
            role="exec_critic",
            on_approve="03-anzai-revise",
            on_revise="01-anzai-plan",
        ),
        StepSpec(
            id="03-anzai-revise",
            persona="anzai",
            role="planner",
            on_approve="__done__",
            on_revise="01-anzai-plan",
        ),
    ]

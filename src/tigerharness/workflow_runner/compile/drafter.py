"""Anzai's step drafter for the workflow compile phase (Phase 2, Wave 1).

The drafter is the first stage of ``compile_playbook``. It drives the
LLM (the ``anzai`` persona, via :class:`SessionManager`) to turn a
freestyle markdown playbook plus a task brief into a list of typed,
parsed :class:`StepFrontmatter` objects — the candidates the Tier 1
validators (Miyagi's module) then check semantically.

Responsibilities, and the lines we do **not** cross:

* We assemble the prompt (playbook + brief + roster + frontmatter
  contract + output protocol + optional critic feedback) and invoke
  the session manager once.
* We parse the LLM's reply into ``StepFrontmatter`` objects, raising
  :class:`DrafterParseError` (with the raw response attached) on any
  structural problem.
* We pass the per-invocation ``cost_usd`` straight through so the
  pipeline can roll it into ``status.cost_usd_total`` (ADR D10).

We deliberately do **not** validate semantics here — ref resolution,
roster membership, cycle bounds and the dry-run trace are the Tier 1
validators' job. The drafter only guarantees "these are well-formed
``StepFrontmatter`` objects".

Output protocol
---------------

Anzai is asked to emit every step file inside a single fenced block
tagged ``steps-bundle``. Inside the block, each step file is introduced
by a ``## step: <id>`` header, followed by ``---``-delimited YAML
frontmatter and a markdown body::

    ```steps-bundle
    ## step: 01-anzai-plan
    ---
    id: 01-anzai-plan
    persona: anzai
    role: planner
    on_approve: 02-akagi-critique
    on_revise: 01-anzai-plan
    on_block: __escalate__
    max_iters: 5
    timeout_sec: 1800
    parallel_with: []
    ---
    <body the persona will receive>

    ## step: 02-akagi-critique
    ---
    ...
    ---
    <body>
    ```

The ``## step:`` headers are the split sentinel; the frontmatter is the
authoritative source of each step's id / persona / routing. Bodies must
not contain bare triple-backtick lines (they would collide with the
bundle's closing fence) — the prompt says so explicitly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

import yaml

from tigerharness.workflow_runner.models import (
    StepFrontmatter,
    WorkflowModelError,
)
from tigerharness.workflow_runner.sessions import SessionManager

__all__ = [
    "DrafterParseError",
    "DrafterResult",
    "draft_steps",
]


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Persona that runs the drafter prompt. ``compiled_by`` ends up being
#: this name in the orchestration the pipeline persists.
_DRAFTER_PERSONA = "anzai"

#: Opening fence the prompt asks Anzai to wrap every step file in.
_BUNDLE_FENCE = "```steps-bundle"

#: Any bare triple-backtick line closes the bundle.
_CLOSING_FENCE = "```"

#: YAML frontmatter delimiter inside each step file.
_FM_DELIM = "---"

#: ``## step: <id>`` split sentinel between step files in the bundle.
_STEP_HEADER_RE = re.compile(r"^##\s+step:\s*(?P<id>\S.*?)\s*$")

#: Frontmatter keys the drafter will accept. Anything else is a
#: hallucinated field and rejected before the value ever reaches the
#: model layer (``StepFrontmatter.from_dict`` ignores extras rather than
#: rejecting them, so the boundary check has to live here).
_ALLOWED_KEYS = frozenset(StepFrontmatter.REQUIRED_KEYS) | {"parallel_with"}


# --------------------------------------------------------------------------- #
# Public types
# --------------------------------------------------------------------------- #


class DrafterParseError(ValueError):
    """Raised when the LLM's reply cannot be parsed into step files.

    Carries the verbatim ``raw_response`` so the Tier 2 loop (and the
    compile transcript) can show exactly what the model produced when a
    draft fails to parse.
    """

    def __init__(self, message: str, *, raw_response: str) -> None:
        super().__init__(message)
        self.raw_response = raw_response


@dataclass(frozen=True)
class DrafterResult:
    """Outcome of one :func:`draft_steps` call.

    ``steps`` are parsed but not yet semantically validated; ``raw_response``
    is the model output verbatim (for the transcript); ``cost_usd`` is the
    invocation cost, passed through for the pipeline's cost roll-up.
    """

    steps: list[StepFrontmatter]
    raw_response: str
    cost_usd: float


# --------------------------------------------------------------------------- #
# Internal signal
# --------------------------------------------------------------------------- #


class _BundleFormatError(Exception):
    """Internal: a structural problem found while parsing the bundle.

    Helpers raise this with a human-readable message; :func:`_parse_response`
    is the single place that re-raises it as :class:`DrafterParseError`
    with the raw response attached.
    """


# --------------------------------------------------------------------------- #
# Public entrypoint
# --------------------------------------------------------------------------- #


def draft_steps(
    *,
    playbook_text: str,
    task_brief: str,
    roster: list[str],
    session_manager: SessionManager,
    feedback: Optional[str] = None,
    timeout_sec: int = 600,
) -> DrafterResult:
    """Drive Anzai to draft step files from a playbook + brief.

    Parameters
    ----------
    playbook_text:
        The freestyle playbook markdown, verbatim.
    task_brief:
        The task brief, verbatim.
    roster:
        Persona names from the team config. Given to the LLM so it only
        assigns personas that exist (Tier 1's ``roster`` validator is
        the enforcing check; this is the courtesy input).
    session_manager:
        The LLM seam. Tests inject a fake; production hits ``claude -p``.
    feedback:
        Tier 2 critic feedback for a re-draft. ``None`` on the initial
        call; a ``REVISE`` message string on subsequent loop turns.
    timeout_sec:
        Per-invocation wall-clock cap handed to the session manager.

    Returns
    -------
    DrafterResult
        Parsed steps, the verbatim response, and the invocation cost.

    Raises
    ------
    DrafterParseError
        If the invocation errored or the reply cannot be parsed into
        well-formed step frontmatters.
    """
    prompt = _build_prompt(
        playbook_text=playbook_text,
        task_brief=task_brief,
        roster=roster,
        feedback=feedback,
    )
    result = session_manager.invoke(
        _DRAFTER_PERSONA, prompt, timeout_sec=timeout_sec
    )
    if result.error is not None:
        raise DrafterParseError(
            f"drafter LLM invocation failed: {result.error}",
            raw_response=result.stdout,
        )
    steps = _parse_response(result.stdout)
    return DrafterResult(
        steps=steps,
        raw_response=result.stdout,
        cost_usd=result.cost_usd,
    )


# --------------------------------------------------------------------------- #
# Prompt assembly
# --------------------------------------------------------------------------- #


_FRONTMATTER_CONTRACT = """\
Required frontmatter fields (every step file MUST have all of these):
  - id:          globally-unique step id within this task; charset [A-Za-z0-9_-].
  - persona:     one of the roster names listed above (exact match).
  - role:        free-text label (planner, exec_critic, qa_critic,
                 developer, qa, doc_writer, ...).
  - on_approve:  next step id, or the sentinel __done__, or __escalate__.
  - on_revise:   step id to rewind to (typically the planning step).
  - on_block:    step id, or the sentinel __escalate__ (the usual default).
  - max_iters:   positive integer; hard cap on times this step runs.
  - timeout_sec: positive integer; per-iteration wall-clock cap.
Optional:
  - parallel_with: list of sibling step ids (default []). Reserved syntax;
                   keep it [].

Routing sentinels (the only two that exist — do NOT invent others):
  - __done__      : the task is complete once this step approves.
  - __escalate__  : hand control back to the Operator (human gate).
Every non-sentinel on_* target MUST be the id of a step you emit in this
same bundle."""


_OUTPUT_PROTOCOL = """\
Emit ALL step files inside a single fenced block tagged `steps-bundle`.
Inside the block, introduce each step file with a `## step: <id>` header,
then its `---`-delimited YAML frontmatter, then the body the persona will
receive. Shape:

```steps-bundle
## step: <step-id-1>
---
id: <step-id-1>
persona: <persona-from-roster>
role: <role>
on_approve: <target>
on_revise: <target>
on_block: <target>
max_iters: <int>
timeout_sec: <int>
parallel_with: []
---
<body: the instructions this persona will receive>

## step: <step-id-2>
---
...
---
<body>
```

Rules:
  - One `## step: <id>` header per step file; the header id MUST equal the
    `id:` frontmatter field.
  - Use ONLY the frontmatter fields listed in the contract above — no
    extra keys.
  - Do NOT use triple-backtick code fences anywhere inside the bundle;
    a bare ``` line would terminate the bundle early.
  - The terminal step's on_approve routes to __done__.

After the closing fence, end your whole reply with exactly:
    WORKFLOW: APPROVE"""


def _build_prompt(
    *,
    playbook_text: str,
    task_brief: str,
    roster: list[str],
    feedback: Optional[str],
) -> str:
    """Assemble the drafter prompt from its parts.

    The order is deliberate: identity/task, then the two verbatim inputs
    (playbook + brief), then the roster, then the machine-checked
    contract, then the output protocol, then any critic feedback last so
    it is the freshest thing in the model's context on a re-draft.
    """
    roster_block = "\n".join(f"  - {name}" for name in roster)
    sections = [
        (
            "You are compiling a team workflow. Read the freestyle "
            "playbook and the task brief below, then emit one step file "
            "per phase the playbook describes, with frontmatter matching "
            "the contract. Do not execute the task — only plan the steps."
        ),
        "## Playbook (verbatim)\n\n" + playbook_text,
        "## Task brief (verbatim)\n\n" + task_brief,
        "## Roster (assign only these personas)\n\n" + roster_block,
        "## Frontmatter contract\n\n" + _FRONTMATTER_CONTRACT,
        "## Output protocol\n\n" + _OUTPUT_PROTOCOL,
    ]
    if feedback is not None:
        sections.append("## Critic feedback to address\n\n" + feedback)
    return "\n\n".join(sections) + "\n"


# --------------------------------------------------------------------------- #
# Response parsing
# --------------------------------------------------------------------------- #


def _parse_response(raw_response: str) -> list[StepFrontmatter]:
    """Parse the LLM reply into ``StepFrontmatter`` objects.

    Single funnel for raw-response attachment: every structural failure
    surfaces as :class:`_BundleFormatError` from a helper and is
    re-raised here as :class:`DrafterParseError` carrying ``raw_response``.
    """
    try:
        bundle = _extract_bundle(raw_response)
        chunks = _split_steps(bundle)
        return [_parse_chunk(header_id, chunk) for header_id, chunk in chunks]
    except _BundleFormatError as exc:
        raise DrafterParseError(
            str(exc), raw_response=raw_response
        ) from exc


def _extract_bundle(text: str) -> str:
    """Return the content between the ``steps-bundle`` fence and its close.

    Line-based on purpose: a regex over the whole blob would be fooled by
    the ``---`` lines inside step files. We scan for the first opening
    fence, then the first bare ``` after it.
    """
    lines = text.splitlines()
    open_idx: Optional[int] = None
    for i, line in enumerate(lines):
        if line.strip() == _BUNDLE_FENCE:
            open_idx = i
            break
    if open_idx is None:
        raise _BundleFormatError(
            f"no opening {_BUNDLE_FENCE!r} fence found in drafter response"
        )
    close_idx: Optional[int] = None
    for j in range(open_idx + 1, len(lines)):
        if lines[j].strip() == _CLOSING_FENCE:
            close_idx = j
            break
    if close_idx is None:
        raise _BundleFormatError(
            "steps-bundle fence is never closed (missing ``` line)"
        )
    return "\n".join(lines[open_idx + 1 : close_idx])


def _split_steps(bundle: str) -> list[tuple[str, str]]:
    """Split the bundle into ``(header_id, chunk_text)`` pairs.

    The ``## step: <id>`` headers are the split points; any preamble
    before the first header is ignored. Each chunk is the text between
    one header and the next (or the end of the bundle).
    """
    lines = bundle.splitlines()
    headers: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        match = _STEP_HEADER_RE.match(line)
        if match:
            headers.append((i, match.group("id")))
    if not headers:
        raise _BundleFormatError(
            "steps-bundle contains no '## step: <id>' headers"
        )
    chunks: list[tuple[str, str]] = []
    for k, (idx, header_id) in enumerate(headers):
        start = idx + 1
        end = headers[k + 1][0] if k + 1 < len(headers) else len(lines)
        chunks.append((header_id, "\n".join(lines[start:end])))
    return chunks


def _parse_chunk(header_id: str, chunk: str) -> StepFrontmatter:
    """Parse one step chunk into a ``StepFrontmatter``.

    ``header_id`` is the id from the ``## step:`` line; it is used only to
    make error messages locatable. The authoritative id is the ``id:``
    frontmatter field consumed by ``StepFrontmatter.from_dict``.
    """
    data = _split_frontmatter(header_id, chunk)
    unknown = sorted(str(k) for k in set(data) - _ALLOWED_KEYS)
    if unknown:
        raise _BundleFormatError(
            f"step {header_id!r} has unknown frontmatter field(s): {unknown}"
        )
    try:
        return StepFrontmatter.from_dict(data)
    except WorkflowModelError as exc:
        raise _BundleFormatError(f"step {header_id!r}: {exc}") from exc


def _split_frontmatter(header_id: str, chunk: str) -> dict[str, Any]:
    """Extract the YAML frontmatter mapping from one step chunk.

    Tolerates blank lines between the ``## step:`` header and the opening
    ``---``. Raises :class:`_BundleFormatError` on a missing delimiter,
    invalid YAML, or a non-mapping payload.
    """
    lines = chunk.splitlines()
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or lines[i].strip() != _FM_DELIM:
        raise _BundleFormatError(
            f"step {header_id!r} is missing its opening '---' frontmatter "
            "delimiter"
        )
    open_i = i
    close_i: Optional[int] = None
    for j in range(open_i + 1, len(lines)):
        if lines[j].strip() == _FM_DELIM:
            close_i = j
            break
    if close_i is None:
        raise _BundleFormatError(
            f"step {header_id!r} is missing its closing '---' frontmatter "
            "delimiter"
        )
    fm_text = "\n".join(lines[open_i + 1 : close_i])
    try:
        data = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        raise _BundleFormatError(
            f"step {header_id!r} frontmatter is not valid YAML: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise _BundleFormatError(
            f"step {header_id!r} frontmatter must be a YAML mapping"
        )
    return data

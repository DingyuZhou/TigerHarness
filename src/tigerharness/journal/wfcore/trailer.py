"""Persona response trailer parser for the workflow-runner.

The workflow walk routes a graph of
personas through a playbook. Routing decisions are driven by a single
structured trailer line in each persona's reply:

    WORKFLOW: APPROVE
    WORKFLOW: REVISE: <reason>
    WORKFLOW: REVISE: target=<step-id>: <reason>
    WORKFLOW: BLOCK: <reason>

This module turns that final line into a typed verdict. It is a pure
function with no I/O and no logging -- the orchestrator owns the
re-prompt-once / route / escalate policy described in
``docs/workflow-runner.md``.

Grammar -- deliberately tight; this is the only place AI-generated text
meets deterministic routing:

* Verbs (``APPROVE`` / ``REVISE`` / ``BLOCK``) are case-sensitive.
* The trailer line must start with the literal prefix ``WORKFLOW:``;
  no leading whitespace, no ``workflow:``.
* Exactly one space between ``WORKFLOW:`` and the verb.
* Trailing whitespace on the trailer line is tolerated.
* If multiple lines start with ``WORKFLOW:``, the **last one wins**.
  If that final candidate is malformed, the whole parse fails; we do
  not silently fall back to an earlier valid one.
* ``REVISE`` and ``BLOCK`` require a non-empty reason.
* If a ``REVISE`` reason begins with ``target=``, it must be well-formed
  ``target=<step-id>: <reason-text>``. Step ids are restricted to
  ``[A-Za-z0-9_-]+`` -- anything else is a parse failure, not a
  silently-included literal reason.

A malformed final ``WORKFLOW:`` candidate intentionally produces
``ParseError`` even if an earlier line was well-formed: the
orchestrator's contract is to re-prompt the persona once on parse
failure, and that contract relies on us not papering over corrupt
trailers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Union

__all__ = [
    "Approve",
    "Block",
    "ParseError",
    "Revise",
    "Verdict",
    "parse_trailer",
]


# ---------------------------------------------------------------------------
# Verdict ADT -- frozen dataclasses with a Literal discriminator
# ---------------------------------------------------------------------------
#
# Why frozen dataclasses (not pydantic)? The rest of the project's typed
# value objects (``Persona``, registry rows, watchdog state) use
# ``@dataclass(frozen=True)`` and the package has no pydantic
# dependency. Staying consistent keeps the import surface tight.
#
# The ``kind`` Literal field gives callers a single discriminator they
# can branch on for JSON serialisation (``status.json`` step_history
# records) without having to ``isinstance``-walk the union.
#
# The discriminator values are deliberately UPPERCASE to match the
# wire protocol (the verb tokens in the trailer itself -- ``APPROVE``
# / ``REVISE`` / ``BLOCK``) and the on-disk shape enforced by
# :data:`tigerharness.journal.wfcore.models._VERDICTS`, which is the
# allowlist :class:`~tigerharness.journal.wfcore.models.StepHistoryEntry`
# validates ``verdict`` against. ``PARSE_ERROR`` mirrors the spec's
# ``verdict_parse_failed`` event terminology in uppercase form. A
# downstream executor can therefore use ``verdict.kind`` directly as
# the ``StepHistoryEntry.verdict`` value without re-casing.


@dataclass(frozen=True)
class Approve:
    """The reviewer approves; route via the step's ``on_approve``."""

    kind: Literal["APPROVE"] = "APPROVE"


@dataclass(frozen=True)
class Revise:
    """The reviewer wants another iteration.

    ``summary`` is the one-line human-readable reason. ``target`` is the
    optional rewind override extracted from a ``target=<step-id>:``
    prefix in the trailer; when ``None`` the orchestrator rewinds to
    the step's frontmatter ``on_revise``.
    """

    summary: str
    target: str | None = None
    kind: Literal["REVISE"] = "REVISE"


@dataclass(frozen=True)
class Block:
    """The reviewer cannot proceed; route via the step's ``on_block``."""

    summary: str
    kind: Literal["BLOCK"] = "BLOCK"


@dataclass(frozen=True)
class ParseError:
    """The trailer could not be parsed.

    The orchestrator's contract on receiving this is to re-prompt the
    persona **exactly once** with the canonical trailer reminder, then
    -- on a second parse failure -- emit ``verdict_parse_failed`` and
    route via ``on_block`` -> ``__escalate__``.
    """

    reason: str
    kind: Literal["PARSE_ERROR"] = "PARSE_ERROR"


Verdict = Union[Approve, Revise, Block, ParseError]
"""Union type alias so callers can write ``verdict: Verdict``."""


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

_WORKFLOW_PREFIX = "WORKFLOW:"

# Strict line shapes. Match against the line **after** trailing
# whitespace has been stripped.
_APPROVE_RE = re.compile(r"^WORKFLOW: APPROVE$")
_REVISE_RE = re.compile(r"^WORKFLOW: REVISE:\s+(.+)$")
_BLOCK_RE = re.compile(r"^WORKFLOW: BLOCK:\s+(.+)$")

# Optional ``target=<step-id>:`` prefix inside a REVISE reason.
# Step ids restricted to alphanumerics, underscore, hyphen -- enough for
# the orchestration ids we generate (e.g. ``06-3f1a-rukawa-implement``)
# and narrow enough to keep adversarial input out of routing keys.
_TARGET_RE = re.compile(r"^target=([A-Za-z0-9_-]+):\s+(.+)$")


def parse_trailer(text: str) -> Verdict:
    """Parse a persona's stdout into a typed :class:`Verdict`.

    Pure function -- no I/O, no logging, no side effects. Designed to
    be cheap enough to call on every iteration's stdout without
    measurement.
    """
    if not text.strip():
        return ParseError(reason="empty or whitespace-only input")

    # Find the LAST line that starts (strictly) with "WORKFLOW:".
    # ``splitlines()`` handles \n, \r\n, and \r uniformly and strips
    # the terminator -- we don't need to think about line endings
    # again below.
    candidate: str | None = None
    for line in text.splitlines():
        if line.startswith(_WORKFLOW_PREFIX):
            candidate = line

    if candidate is None:
        return ParseError(reason="no line starting with 'WORKFLOW:' found")

    # Trailing whitespace (spaces, tabs, stray \r left by mixed
    # endings) is tolerated. Leading whitespace is NOT -- that's
    # already handled by ``startswith`` above.
    stripped = candidate.rstrip()

    if _APPROVE_RE.match(stripped):
        return Approve()

    revise_match = _REVISE_RE.match(stripped)
    if revise_match:
        reason = revise_match.group(1)
        if reason.startswith("target="):
            target_match = _TARGET_RE.match(reason)
            if target_match is None:
                return ParseError(
                    reason=(
                        "REVISE reason begins with 'target=' but is not "
                        "well-formed 'target=<step-id>: <reason>'"
                    )
                )
            return Revise(
                summary=target_match.group(2),
                target=target_match.group(1),
            )
        return Revise(summary=reason, target=None)

    block_match = _BLOCK_RE.match(stripped)
    if block_match:
        return Block(summary=block_match.group(1))

    return ParseError(reason=f"unrecognized trailer form: {stripped!r}")

"""Tier 1 mechanical validators for the compile phase.

Pure Python -- no LLM, no subprocess, no I/O. These run after the
drafter emits a candidate set of step files (already parsed into
:class:`StepFrontmatter`) and again on every runtime ``append_steps``
call. They are the part of the compile defense-in-depth that *cannot*
be silently bypassed by a hallucinating critic (ADR 0002, D2).

Five validators, per ``docs/workflow-runner-phase2.md`` section
"Tier 1 -- mechanical validators":

* **schema** -- required fields / types (the model's ``__post_init__``
  already enforces most of this; we re-raise as a collectable
  :class:`ValidationError` instead of stopping at the first
  :class:`WorkflowModelError`) plus step-id uniqueness.
* **ref** -- every routing target is a real step id or a sentinel.
* **roster** -- every persona is in the team roster (case-sensitive).
* **cycle** -- no cycle is unbounded. In the current model every step
  carries a finite ``max_iters`` cap, so this is a *defensive* check
  (it guards against post-construction corruption and a future model
  where caps become optional); its real product is the cycle
  structure surfaced into the dry-run trace for the Tier 2 critics.
* **dry-run trace** -- a static graph walk rendered as human-readable
  text (happy path, branch points, loop annotations, escalation).

The orchestrating entrypoint :func:`validate_compile_output` runs all
five, collects *every* error (Tier 2 critics need the whole list to
suggest a coherent fix), and always builds the trace -- even on
failure, because the critics consume it as context.
"""

from __future__ import annotations

from dataclasses import dataclass

from tigerharness.workflow_runner.models import (
    StepFrontmatter,
    WorkflowModelError,
)

# Routing sentinels recognised by the ``on_*`` fields. Mirrors the
# private ``models._SENTINELS``; redeclared here to avoid importing a
# private name across module boundaries.
SENTINELS = frozenset({"__done__", "__escalate__"})

# Edge fields in a fixed order so error / trace output is deterministic.
_EDGE_FIELDS = ("on_approve", "on_revise", "on_block")


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ValidationError:
    """One mechanical-validation failure.

    ``message`` is shown verbatim to the Tier 2 LLM critic, so it must
    be self-contained and human-readable.
    """

    validator: str  # "schema" | "ref" | "roster" | "cycle" | "trace"
    step_id: str | None  # which step (None for graph-wide problems)
    message: str


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Aggregate outcome of a Tier 1 pass.

    ``trace`` is always populated (the dry-run walk), even when
    ``ok`` is ``False`` -- critics use it as context.
    """

    ok: bool
    errors: list[ValidationError]
    trace: str


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _is_bounded_cap(value: object) -> bool:
    """True iff ``value`` is a finite, executor-bounding iteration cap.

    A cap bounds re-entry iff it is a positive int (``bool`` excluded,
    matching the model's own ``_require_positive_int`` contract).
    """
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _bfs(adj: dict[str, set[str]], start: str) -> set[str]:
    """Set of nodes reachable from ``start`` in one or more steps."""
    seen: set[str] = set()
    stack = list(adj[start])
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(adj[node])
    return seen


# --------------------------------------------------------------------------- #
# 1. schema
# --------------------------------------------------------------------------- #


def validate_schema(steps: list[StepFrontmatter]) -> list[ValidationError]:
    """Re-validate each step's shape and enforce id uniqueness.

    ``StepFrontmatter.__post_init__`` already validates fields at
    construction, but the objects are mutable (``slots`` dataclasses,
    not frozen), so we defensively round-trip through
    ``from_dict(to_dict())`` to catch post-construction corruption and
    surface it as a collectable error rather than an exception.
    """
    errors: list[ValidationError] = []
    seen: dict[str, int] = {}
    for index, step in enumerate(steps):
        step_id = step.id if isinstance(step.id, str) else None
        try:
            StepFrontmatter.from_dict(step.to_dict())
        except WorkflowModelError as exc:
            errors.append(
                ValidationError(
                    "schema",
                    step_id,
                    f"step at index {index} fails schema: {exc}",
                )
            )
            continue
        if step_id in seen:
            errors.append(
                ValidationError(
                    "schema",
                    step_id,
                    f"duplicate step id {step_id!r} "
                    f"(first seen at index {seen[step_id]})",
                )
            )
        else:
            seen[step_id] = index
    return errors


# --------------------------------------------------------------------------- #
# 2. ref
# --------------------------------------------------------------------------- #


def validate_refs(
    steps: list[StepFrontmatter],
    *,
    entrypoint: str | None = None,
) -> list[ValidationError]:
    """Every routing target must be a real step id or a sentinel.

    When ``entrypoint`` is given it must itself resolve to a real step
    id (a sentinel is not a valid entrypoint).
    """
    errors: list[ValidationError] = []
    ids = {step.id for step in steps}
    valid_targets = ids | SENTINELS
    for step in steps:
        for field in _EDGE_FIELDS:
            target = getattr(step, field)
            if target not in valid_targets:
                errors.append(
                    ValidationError(
                        "ref",
                        step.id,
                        f"{field} target {target!r} is not a known step "
                        "id or sentinel (__done__/__escalate__)",
                    )
                )
    if entrypoint is not None and entrypoint not in ids:
        errors.append(
            ValidationError(
                "ref",
                None,
                f"entrypoint {entrypoint!r} is not a known step id",
            )
        )
    return errors


# --------------------------------------------------------------------------- #
# 3. roster
# --------------------------------------------------------------------------- #


def validate_roster(
    steps: list[StepFrontmatter],
    *,
    roster: list[str],
) -> list[ValidationError]:
    """Every step's ``persona`` must be in the team roster.

    Case-sensitive, consistent with ``configs/personas.yaml``.
    """
    allowed = set(roster)
    errors: list[ValidationError] = []
    for step in steps:
        if step.persona not in allowed:
            errors.append(
                ValidationError(
                    "roster",
                    step.id,
                    f"persona {step.persona!r} is not in the team roster "
                    f"{sorted(allowed)}",
                )
            )
    return errors


# --------------------------------------------------------------------------- #
# 4. cycle
# --------------------------------------------------------------------------- #


def _build_adjacency(
    id_to_step: dict[str, StepFrontmatter],
) -> tuple[dict[str, set[str]], set[str]]:
    """Routing graph over *real* step nodes (sentinels are sinks).

    Returns ``(adjacency, self_edge_nodes)``. Unknown targets are
    excluded here (the ref validator reports them); they cannot form a
    cycle through a node that does not exist.
    """
    adj: dict[str, set[str]] = {sid: set() for sid in id_to_step}
    self_edges: set[str] = set()
    for sid, step in id_to_step.items():
        for field in _EDGE_FIELDS:
            target = getattr(step, field)
            if target in adj:
                adj[sid].add(target)
                if target == sid:
                    self_edges.add(sid)
    return adj, self_edges


def _find_cycles(
    id_to_step: dict[str, StepFrontmatter],
) -> list[list[str]]:
    """Cycle groups in the routing graph.

    Each entry is a sorted list of member ids. A multi-node group is a
    strongly-connected component (size >= 2); a single-node group is a
    node with a direct self-edge. Both kinds are listed -- a self-edge
    inside a larger SCC is a distinct, tighter loop worth surfacing.
    SCCs come first (sorted by first member), then self-loops.
    """
    adj, self_edges = _build_adjacency(id_to_step)
    reach = {node: _bfs(adj, node) for node in adj}
    cycle_nodes = [node for node in id_to_step if node in reach[node]]

    sccs: list[list[str]] = []
    assigned: set[str] = set()
    for node in cycle_nodes:
        if node in assigned:
            continue
        component = sorted(
            other
            for other in cycle_nodes
            if other == node
            or (other in reach[node] and node in reach[other])
        )
        assigned.update(component)
        if len(component) >= 2:
            sccs.append(component)

    sccs.sort(key=lambda comp: comp[0])
    self_loops = [[node] for node in sorted(self_edges)]
    return sccs + self_loops


def validate_cycles(
    steps: list[StepFrontmatter],
) -> list[ValidationError]:
    """Reject any *unbounded* cycle.

    A cycle is bounded if at least one member carries a finite
    ``max_iters`` cap (the executor bounds re-entry by it). The model
    guarantees every step has such a cap, so this is a defensive check;
    it fires only if a cap was corrupted after construction or a future
    model relaxes the invariant.
    """
    id_to_step = {step.id: step for step in steps}
    errors: list[ValidationError] = []
    for members in _find_cycles(id_to_step):
        bounded = any(
            _is_bounded_cap(id_to_step[m].max_iters) for m in members
        )
        if not bounded:
            errors.append(
                ValidationError(
                    "cycle",
                    None,
                    f"cycle {{{', '.join(members)}}} is unbounded: "
                    "no member has a finite max_iters cap",
                )
            )
    return errors


# --------------------------------------------------------------------------- #
# 5. dry-run trace
# --------------------------------------------------------------------------- #


def _happy_path(id_to_step: dict[str, StepFrontmatter], start: str) -> str:
    """Follow ``on_approve`` from ``start`` until a terminal or a loop."""
    parts: list[str] = []
    seen: set[str] = set()
    cur = start
    while True:
        if cur in SENTINELS:
            parts.append(cur)
            break
        if cur not in id_to_step:
            parts.append(f"{cur} (unknown)")
            break
        if cur in seen:
            parts.append(f"{cur} (loops back)")
            break
        seen.add(cur)
        parts.append(cur)
        cur = id_to_step[cur].on_approve
    return " -> ".join(parts)


def _discover(
    id_to_step: dict[str, StepFrontmatter],
    start: str,
) -> tuple[list[str], set[str]]:
    """BFS over all edge types from ``start``; returns (order, reached)."""
    order: list[str] = []
    seen: set[str] = set()
    if start in id_to_step:
        seen.add(start)
        queue = [start]
    else:
        queue = []
    while queue:
        sid = queue.pop(0)
        order.append(sid)
        for field in _EDGE_FIELDS:
            target = getattr(id_to_step[sid], field)
            if target in id_to_step and target not in seen:
                seen.add(target)
                queue.append(target)
    return order, seen


def _edge_line(
    field: str,
    target: str,
    id_to_step: dict[str, StepFrontmatter],
    order_index: dict[str, int],
    cur_index: int,
) -> str:
    """Render one routing edge with an annotation.

    The "loop back" / "self-loop" annotation is a *readability* hint
    based on BFS discovery order: an edge to an already-or-equally
    discovered node (``order_index[target] <= cur_index``) is treated as
    a rewind. This deliberately is NOT a cycle-membership test, and it
    has a known soft edge: a fan-in edge into an earlier-discovered node
    that is itself a terminal (e.g. two branches merging into a shared
    finalizer that routes to ``__done__``) is labelled "loop back" even
    though no cycle exists. The authoritative loop structure lives in
    the ``loops:`` section (and :func:`validate_cycles`), which use
    rigorous mutual-reachability, so the annotation never drives a
    validation decision.

    Do not "fix" this by switching to reachability
    (``cur in reach[target]``): in REVISE-heavy graphs every step is
    mutually reachable, so that would mislabel the *forward* happy-path
    edge (``01 -> 02`` when ``02`` rewinds to ``01``) as a loop. The
    only strictly-correct upgrade is a DFS active-stack back-edge
    classifier; it is pin-compatible but out of scope for Wave 1.
    """
    label = f"{field:<10}"
    if target in SENTINELS:
        note = "done" if target == "__done__" else "escalate"
        return f"{label} -> {target} ({note})"
    if target not in id_to_step:
        return f"{label} -> {target} (unknown target)"
    if order_index[target] <= cur_index:
        cap = id_to_step[target].max_iters
        kind = "self-loop" if order_index[target] == cur_index else "loop back"
        return f"{label} -> {target} ({kind}; at most {cap} re-entries)"
    return f"{label} -> {target}"


def build_dry_run_trace(
    steps: list[StepFrontmatter],
    *,
    entrypoint: str | None = None,
) -> str:
    """Render a human-readable static walk of the step graph.

    Sections: happy path (all APPROVE), per-step routing detail (with
    inline loop / escalation annotations), unreachable steps (if any),
    and the cycle structure. Lines aim to stay within ~80 cols for
    typical step ids (the id pattern allows up to 64 chars, so a
    pathologically long id can still overflow; the trace is not hard
    wrapped). The output is saved to ``compile_trace.txt`` and fed to
    the Tier 2 critics.
    """
    lines = ["workflow dry-run trace", "======================"]
    id_to_step = {step.id: step for step in steps}
    if not steps:
        lines.append("(no steps to trace)")
        return "\n".join(lines)

    ep = entrypoint if entrypoint is not None else steps[0].id
    ids = list(id_to_step)
    lines.append(f"entrypoint: {ep}")
    lines.append(f"steps ({len(ids)}): {', '.join(ids)}")
    lines.append("")

    lines.append("happy path (all APPROVE):")
    lines.append("  " + _happy_path(id_to_step, ep))
    lines.append("")

    order, reachable = _discover(id_to_step, ep)
    order_index = {sid: i for i, sid in enumerate(order)}
    lines.append("routing detail:")
    for sid in order:
        step = id_to_step[sid]
        lines.append(
            f"  {sid} [{step.persona}/{step.role}] max_iters={step.max_iters}:"
        )
        for field in _EDGE_FIELDS:
            target = getattr(step, field)
            lines.append(
                "    "
                + _edge_line(field, target, id_to_step, order_index, order_index[sid])
            )

    unreachable = [sid for sid in ids if sid not in reachable]
    if unreachable:
        lines.append("")
        lines.append("unreachable from entrypoint:")
        lines.append("  " + ", ".join(unreachable))

    lines.append("")
    lines.append("loops:")
    cycles = _find_cycles(id_to_step)
    if not cycles:
        lines.append("  (none)")
    else:
        for members in cycles:
            bounded = any(
                _is_bounded_cap(id_to_step[m].max_iters) for m in members
            )
            tag = "bounded" if bounded else "UNBOUNDED"
            if len(members) == 1:
                lines.append(f"  self-loop on {members[0]} -- {tag}")
            else:
                lines.append(f"  {{{', '.join(members)}}} -- {tag}")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Orchestrating entrypoint
# --------------------------------------------------------------------------- #


def validate_compile_output(
    steps: list[StepFrontmatter],
    *,
    roster: list[str],
    entrypoint: str | None = None,
) -> ValidationResult:
    """Run all five Tier 1 validators and collect every error.

    Errors from all validators are gathered (the loop never stops at
    the first failure -- Tier 2 critics need the whole list). The trace
    is always built. ``ok`` is ``False`` iff any validator produced an
    error.
    """
    errors: list[ValidationError] = []
    if not steps:
        errors.append(
            ValidationError("schema", None, "workflow graph has no steps")
        )
    ep = entrypoint if entrypoint is not None else (steps[0].id if steps else None)

    errors.extend(validate_schema(steps))
    errors.extend(validate_refs(steps, entrypoint=ep))
    errors.extend(validate_roster(steps, roster=roster))
    errors.extend(validate_cycles(steps))

    trace = build_dry_run_trace(steps, entrypoint=ep)
    return ValidationResult(ok=not errors, errors=errors, trace=trace)

# ADR 0002 — workflow-runner Phase 2 (compile + step-append + skill + guardrail)

- **Status:** Accepted — implemented (see *Status update* below).
- **Date:** 2026-05-31.
- **Decision-makers:** Operator + Anzai.
- **Thread:** Claude Code session 2026-05-31 (Operator ↔ Anzai), following
  the Phase 1 audit + adversarial re-verification.

> **Status update — 2026-06-04.** D1–D12 shipped: the
> `workflow_runner.compile/` sub-package, the Tier 1 / Tier 2 split, the
> hard floor of 3 critique rounds, the two-critic loop, the
> `--playbook`/`--steps` flag rules, and compile-cost roll-up are all in
> the code. Two deviations from this record are worth flagging:
> - **D9 (human gate) was retired, not deferred.** The Phase-3
>   enforcement this decision left a clean integration point for "does
>   not port to the subscription model" and has been struck from the
>   roadmap — see
>   [`docs/journal-workflow-mode.md`](../journal-workflow-mode.md). The
>   anticipated ADR 0003 will not arrive in that form.
> - **D5 (runtime step-append) shipped in the journal backend** as
>   `tigerharness journal append-steps`, not as
>   `workflow_runner.compile.append_steps`. The Tier-1-only
>   re-validation contract is honored; only the module home changed.
>
> The decisions below are left intact as the original record.

## Context

Phase 1 of the workflow-runner shipped a working sequential executor
that drives **pre-compiled** step files to a terminal phase. The Phase 1
audit and the adversarial verification (66 agents across 13 findings)
confirmed that what was built matches the design *for the execution
half* but leaves the **authoring half** — the part the Operator
emphasized in the original design — unimplemented.

This ADR records the design decisions for Phase 2, which closes the
authoring-half gap, plus two adjacent fixes (write guardrail + spec
deviation) that the audit surfaced.

The full design spec was `docs/workflow-runner-phase2.md`, removed with
the runner ([ADR 0003](0003-remove-legacy-runners.md)).
This document is only the decision log — what we chose, what we
rejected, why.

## Decisions

### D1 — Compile phase belongs in `workflow_runner.compile/` as a sub-package

The compile phase is a self-contained subsystem (drafter, validators,
critique loop, pipeline) and is large enough to warrant its own
sub-package rather than living as a single `compile.py` next to
`executor.py`.

*Rejected:* a single `compile.py` module. The drafter + Tier 1
validators + Tier 2 critique loop are independently testable units
that each deserve their own file.

### D2 — Tier 1 validators are pure Python; Tier 2 is the AI loop

Mechanical checks (schema, ref, roster, cycle, dry-run trace) never
involve an LLM. They run cheaply on every compile attempt and on every
runtime `append_steps` call. The AI critique loop (Tier 2) is the
expensive part and is bounded by `max_compile_iters`.

*Rejected:* "have an AI judge the schema." The whole point of the
defense-in-depth was that Tier 1 cannot be silently bypassed by a
hallucinating critic.

### D3 — Tier 2 has a hard floor of 3 critique rounds even on early consensus

Per the Operator's original "force critique to look hard 3 times"
directive. The first three rounds run unconditionally; subsequent
rounds terminate the moment both critics return APPROVE.

*Rejected:* "terminate on first dual-APPROVE." This would let a
sycophantic round 1 short-circuit the whole defense.

### D4 — Two parallel critics, not a panel

Akagi (execution) and Ayako (QA) run in parallel each round. They are
*different lenses*, not redundant reviewers — both must APPROVE for
the round to count toward termination.

*Rejected:* a single critic. The single-critic shape both blurs the
exec/QA dimensions and removes the perspective-diversity that catches
failure modes redundancy can't (cf. the perspective-diverse verify
pattern that worked in the Phase 1 audit verification).

*Rejected:* a panel of N>2. Three or more critics has worse
coordination ergonomics and the marginal coverage from a third lens
(eg. cost) doesn't justify the complexity at this stage.

### D5 — Step-append re-runs Tier 1 (not Tier 2) on the appended graph

`append_steps` re-validates the *new full graph* against the
mechanical Tier 1 rules. It does **not** re-trigger the AI critique
loop. Rationale: runtime appends are typically small (planner injects
the next 2–3 concrete steps) and the round-trip cost of re-critiquing
the whole graph every time the planner thinks of a new step would be
prohibitive. The original compile already had the design-level
critique applied.

*Open question:* if an append substantially reshapes the graph (eg.
adds a whole new branch), should it re-trigger Tier 2? Phase 2 punts;
the running step is responsible for not over-reaching.

### D6 — `workflow start --steps <dir>` stays as the escape hatch

We *don't* deprecate the pre-compiled path. It's the only way to
re-run a known-good `steps/*.md` set without paying compile cost, and
it's the test surface the e2e suite relies on. `--playbook` becomes
the *recommended* path; `--steps` becomes the *power-user / repro*
path.

*Rejected:* removing `--steps`. That would break Phase 1 e2e tests
and the existing repro workflow for debugging shipped tasks.

### D7 — `--playbook` requires a brief; `--steps` forbids one

Mutually exclusive surfaces, validated at argparse time:

- `--playbook <name>` ⇒ requires `--task-brief <text>` xor `--brief-file <path>`.
- `--steps <dir>` ⇒ forbids `--playbook` / `--task-brief` / `--brief-file`.

*Rejected:* allowing `--steps` + `--task-brief`. There's no compile
phase to consume the brief in the escape-hatch path, so the brief
would silently be discarded — exactly the kind of trap the spec
discipline rules out.

### D8 — Write guardrail is a per-team `.claude/settings.json` PreToolUse hook

The mechanism is a hook on `Edit` / `Write` / `NotebookEdit` that
denies writes to `<journal_root>/<task-id>/{status,orchestration,sessions}.json`,
`events.jsonl`, and `steps/*.md`. Scoped per-team because the rule
travels with the team folder.

*Rejected:* a Python-level guard inside the workflow_runner module.
That would only block writes from code that imports
`workflow_runner` — it does nothing for a persona's `claude -p`
subprocess invoking the `Edit` tool, which is exactly the threat.

*Rejected:* read-only filesystem permissions. They block the executor
itself, which legitimately needs to write.

*Rejected:* a fence-of-shame "please don't edit these" comment.
Convention-only is what we have today and is what the audit flagged
as insufficient.

### D9 — Tier 3 (human gate) emits the event but does not block

Phase 2 inserts `human_gate_requested` into the event stream at the
right point (post-compile, when `workflow_config.human_gate=True`) so
Phase 3's enforcement work has a clean integration point. Compile
proceeds without waiting. This is the "design with the seam now,
enforce later" pattern.

*Rejected:* shipping the gate enforcement in Phase 2. The approval
mechanism (Slack reactions vs `workflow approve` CLI vs both) is its
own design decision worth its own ADR (0003).

### D10 — Compile cost rolls up into `status.cost_usd_total`

The Tier 2 critique loop runs through the same `SessionManager` as
the executor (different personas, same machinery). Cost accumulated
during compile counts against the same `max_cost_usd` ceiling. Users
see one cost number per task; the breakdown lives in
`compile_critique.md` for those who want it.

*Rejected:* a separate `compile_cost_usd_total` field in status.json.
Two ceilings is more complex than one; one user-visible cost
constraint matches the way the Operator framed the original "Maximum
cost" requirement.

### D11 — Drafter prompt lives in source (`drafter.py`) for now

Pragmatic: the prompt is short enough that maintaining it as Python
constants is fine. Promote to a versioned `.md` artifact under
`docs/prompts/` only when it grows large enough that prompt diffs
need their own review process.

*Rejected:* shipping the prompt in `docs/prompts/` from day one.
Premature optimization; adds a file-loading code path for no current
benefit.

### D12 — P0 fixes are independent of Phase 2 and ship separately

The parse-failure protocol fix (MAX_PARSE_FAILURES=2 + re-prompt
text) and the default-journal-under-team-folder change are shipped
on their own short-lived branch (`work/2026-05-31-workflow-runner-p0`)
and PR'd independently of Phase 2. They're correctness fixes on
Phase 1 code, not Phase 2 features, and shouldn't be gated on
Phase 2's much-larger landing.

## Consequences

**Positive:**

- The "ask Shohoku to use their default workflow on this task" UX
  from the original design becomes reachable end-to-end.
- Defense-in-depth on compile lives in code, not docs, so it can't be
  silently skipped.
- Step-append closes the "planning settles before exec steps are
  generated" requirement that's been outstanding since Phase 0.
- The write guardrail moves "program writes the journal" from
  persona-prompt convention to enforcement.

**Negative:**

- The compile phase materially extends the user-visible startup
  latency of `workflow start` (3+ rounds of AI critique). Tradeoff is
  inherent to the defense-in-depth model; partially mitigated by the
  `--steps` escape hatch for re-runs.
- More moving parts in the codebase — `workflow_runner` more than
  doubles in surface area.
- Adds a hard dependency on a working `SessionManager` (and thus a
  reachable `claude` binary) just to *start* a workflow, where Phase 1
  could initialise a task purely with `--no-run`. Mitigation:
  `--steps <dir>` still works without compile.

**Neutral:**

- Phase 3 (human gate enforcement), Phase 4 (stuck-watchdog +
  retry), and Phase 5 (parallel dispatch) remain unchanged in scope.
  Phase 2 lays clean integration points for Phase 3 (via the
  `human_gate_requested` event).

## Out of scope

- Resume-across-compile semantics (skipped per Phase 2 spec
  out-of-scope list).
- `workflow approve` CLI / Slack reaction approval (Phase 3 territory).
- Stuck-watchdog AI classifier (Phase 4 territory).

## References

- Original design doc: attached by the Operator in the Slack thread
  `1779962188.223389` on 2026-05-28; archived at
  `/tmp/slack-attachments/1779962188.223389/F0B6TGBEY6Q.markdown`.
- Phase 1 spec: `docs/workflow-runner.md`, removed with the runner
  ([ADR 0003](0003-remove-legacy-runners.md)).
- Phase 1 ADR: [`docs/adr/0001-workflow-runner.md`](0001-workflow-runner.md).
- Phase 1 final review report:
  `teams/Shohoku/task_journal/workflow-runner-phase1-akagi-final-review.md`
  (a team-private record, not an in-repo doc).
- Phase 1 adversarial audit verification: workflow run
  `wf_363a245e-a9d` (13 findings × 66 agents, 100% CONFIRMED at high
  confidence; 5 new gaps surfaced by the completeness critic, all
  addressed in this Phase 2 plan or queued as Phase 3+ work).

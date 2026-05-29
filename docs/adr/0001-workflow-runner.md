# ADR 0001 — workflow-runner

- **Status:** Accepted (spec phase; implementation in phases 1–5).
- **Date:** 2026-05-28.
- **Decision-makers:** Operator + Anzai.
- **Thread:** Slack DM 2026-05-28 (Anzai ↔ Operator).

## Context

The team needs a way to orchestrate multi-persona workflows above
the single-persona task-runner. The Operator proposed a per-team
workflow-folder model with multiple named playbooks (default,
planning, MVP, quality), a status-tracked task journal, a triggering
skill, and constraints to prevent runaway behavior.

This ADR records the design decisions we made in this thread before
any code is written. The full spec lives at
[`docs/workflow-runner.md`](../workflow-runner.md). This document is
only the decision log — what we chose, what we rejected, and why.

## Decisions

### D1 — Code lives in `tigerharness`; recipes live in teams

The workflow-runner is generic infrastructure and ships as
`src/tigerharness/workflow_runner/`. Team-specific playbooks live in
`teams/<Team>/workflow/<name>.md`.

*Rejected:* putting the orchestrator in `teams/Shohoku/` as a one-off.
Generic infra belongs in the project; only the playbooks are
team-specific.

### D2 — Playbooks are freestyle natural-language markdown

A playbook is human prose, no required schema. The orchestrator
compiles the playbook into concrete machine-readable step files at
task start.

*Rejected:* declarative YAML with `step`/`loop_until`/`parallel`
primitives. Too rigid for the way humans actually author team
playbooks; the Operator explicitly prefers natural-language markdown.

*Rejected:* Python files as workflows. Too much rope, couples the
recipe to code, makes it hard for non-engineers to read or
contribute.

### D3 — Compile-once, execute-deterministically

The natural-language playbook is compiled into concrete step files
*once* at task start. From that point on, the orchestrator is fully
deterministic. Each compiled step file is markdown with a YAML
frontmatter for routing (`id`, `persona`, `on_approve`, `on_revise`,
`on_block`, `max_iters`, `timeout_sec`, `parallel_with`).

*Rejected:* re-asking an AI for "what's next" on every step
boundary. Brittle, expensive, and non-deterministic in a way that
makes debugging painful.

### D4 — Compile-phase hardening: three tiers (defense in depth)

Because the compile step is a single point of failure that quietly
poisons everything downstream, we harden it with three layers:

1. **Tier 1 — mechanical validators (always on, unskippable):**
   schema, reference resolution, roster check, cycle bound check,
   dry-run trace.
2. **Tier 2 — forced AI critique loop, minimum 3 iterations.**
   Compiler persona ≠ critic personas. Each critic must list ≥ 2
   concrete issues or write `NO_ISSUES`. Cap at
   `max_compile_iters` (default 8).
3. **Tier 3 — human gate (default ON for v1).** Orchestrator
   slack-notifies the Operator with the compiled step list + dry-run
   trace and waits for `WORKFLOW: APPROVE`.

The `compile_and_validate(...)` primitive is reusable by any step
that produces a structured artifact (e.g., the docs step at the end
of the default workflow).

*Rejected:* relying on a single AI judge to validate the compile.
LLM judges LLM is fragile and gives a false sense of safety.

### D5 — Status and approval signals live in `status.json`, not in step files

Step files (compiled prompts) are write-once. All runtime state —
current pointer, iteration counts, per-iteration verdicts, cost
accumulation, step history — lives in `status.json`. Loops are
implemented as pointer rewinds, never as step rewrites.

*Rejected:* writing iteration outputs back into the step file. The
Operator explicitly required step files to remain untouched after
creation.

### D6 — Pointer rewind with feedback prologue

When a verdict routes to a previously-executed step, the
orchestrator rewinds the pointer (does *not* rewrite anything) and
injects a feedback prologue into that step's *next prompt only*. The
persona's Claude session memory carries the prior iteration's
context; the prologue reinforces the latest critique.

### D7 — Per-persona Claude session, shared across iterations within a task

`sessions.json` maps `persona -> claude_session_id`. A persona's
session persists across loop iterations within a single task, so
the persona remembers prior attempts. Different personas never
share a session.

### D8 — Persona response trailer protocol

Every step ends with a structured trailer the orchestrator parses:

```
WORKFLOW: APPROVE
WORKFLOW: REVISE: <one-line summary>
WORKFLOW: BLOCK: <one-line summary>
```

On parse failure: one re-prompt, then escalate. No silent fallback.

*Rejected:* a separate AI-judge call to interpret each persona's
output. Too expensive and introduces a second failure mode.

### D9 — Sequential by default, opt-in parallelism (Phase 5)

Steps run sequentially in Phase 1–4. The `parallel_with`
frontmatter field is reserved syntax (parsed and stored, but
ignored) until Phase 5. A playbook can opt into parallelism with
`allow_parallel: true` in its `workflow_config` header.

### D10 — Trigger via skill (`workflow-run`) as the primary path

The primary trigger is a Claude skill at
`teams/<Team>/.claude/skills/workflow-run/SKILL.md`. The skill is a
thin wrapper around `tigerharness workflow start ...`. The CLI
remains available for direct use. This makes the workflow
triggerable from Slack DMs (via slack-bridge) and from Claude Code
sessions, with no code duplication.

*Rejected:* CLI-only trigger. Would require the Operator to remember the
exact command; skill matching gives natural-language access.

### D11 — Logging: per-task `events.jsonl` + per-iteration full captures

Structured machine-truth event stream at
`workflow_journal/<task-id>/events.jsonl` (one JSON event per
line). Plus per-iteration full captures under
`logs/<step-id>/iter-NN/{prompt.txt, stdout.txt, stderr.txt,
meta.json}`. A `tigerharness workflow tail` CLI renders the event
stream human-friendly.

*Added at the Operator's request* during the design thread — logging was
missing from the original design doc.

### D12 — Constraints: cost, iters, timeouts, wall time

Hard kills, no silent retries:

- `max_cost_usd` (default 10.0)
- `max_loop_iters` (default 5 per step)
- `step_timeout_sec` (default 1800)
- `max_compile_iters` (default 8)
- `max_task_wall_sec` (default 86400)

Settable at playbook level, overridable per-step. Cost is parsed
from `claude -p --output-format json`.

### D13 — Folder name: `workflow_journal/`, separate from `task_journal/`

Per-task folder is `teams/<Team>/workflow_journal/<task-id>/`,
separate from the existing `task_journal/` used by the task-runner.
Clean separation; no risk of clobbering.

### D14 — Phased rollout (0 → 5)

| Phase | Scope |
|---|---|
| 0 | Spec, default playbook, ADR (this work). |
| 1 | Sequential executor; accepts pre-compiled step files. |
| 2 | Compile phase + Tier 1 validators + loop rewind. |
| 3 | Tier 2 critique + Tier 3 human gate + constraints + escalation. |
| 4 | `workflow-run` skill + end-to-end on Shohoku default. `workflow sweep` + `workflow diagnose` CLIs + matching skills + watchdog reuse. |
| 5 | Parallelism + scheduled sweep. Optional. |

Each phase ships with ≥ 95% line coverage on new code (the
project's 100% floor applies module-wide).

### D15 — Operator (not "CEO") in all public artifacts

The human running the harness is called the **Operator** (capital O)
in all docs, ADRs, playbooks, spec text, and code comments going
forward. tigerharness is open source; "CEO" reads as company-specific
and excludes everyone else who might run the tool.

*Rejected:* "owner" (less role-evocative), "maintainer" (skews
toward repo upkeep), "user" (too generic).

*Migration:* All public-facing references swept in this same change
across the tigerharness repo (code, tests, prompt templates, init
scaffolds, example team charter) and the Shohoku team folder
(persona prompts, charter, configs, knowledge). `memories/` journal
and archive entries left intact as historical records of what was
literally said at the time; future memories will use "Operator"
because the prompt templates now do.

### D16 — Sweep + diagnose for in-flight task care

Two AI-callable skills + matching CLIs, landing in Phase 4:

- `workflow-sweep` — list in-flight tasks across teams, flag stale
  ones, optionally auto-diagnose or auto-resume. Pure filesystem
  read; no claude calls. Cheap enough to cron.
- `workflow-diagnose` — classify a single task into one of seven
  diagnoses (`healthy_running`, `waiting_human_gate`,
  `process_dead_no_completion`, `persona_stuck_no_output`,
  `constraint_breached`, `parse_failure_loop`, `unknown`) and
  recommend a next action. Pure-Python by default; optional
  `--llm-fallback` on `unknown`.

Watchdog logic from `tigerharness.task_runner.stuck_watchdog` is
reused for the `persona_stuck_no_output` branch so automated
escalation and human-readable diagnosis agree.

Phase 5 adds a scheduled sweep (cron / systemd timer, opt-in
per-team) that posts a one-line per-task summary to the team's
`SLACK_NOTIFY_CHANNEL`.

*Rejected:* a single super-skill that conflates listing and
diagnosing. Cleaner to have `sweep` (cheap, frequent, list-mode)
separately from `diagnose` (per-task, deeper, optional LLM
fallback).

## Risks accepted

- **Cost runaway** — mitigated by `max_cost_usd` (mandatory, not
  optional).
- **Approval gaming** (a tired persona stamping `APPROVE`) —
  mitigated by enforcing compiler ≠ critic personas (Shohoku's
  roster already does this) and the minimum-3-iter rule on
  hardened compile / doc steps.
- **Skill-vs-direct-edit drift** (a persona edits `status.json`
  directly) — mitigated by prompt-level guards in v1, with hook-based
  enforcement as a Phase 1 TODO.
- **Shared folder with task-runner** — rejected; we use a separate
  `workflow_journal/` instead of sharing `task_journal/`.

## Non-decisions (deferred)

- **Per-task workflow_journal location** — team-folder vs state-dir.
  Lean team-folder, will confirm before Phase 1.
- **Hook-based protection** of status/event files — Phase 1 TODO.
- **Tier 3 default** — ON until ~10 clean compiles observed, then
  revisit.
- **Playbook discovery rules for the skill matcher** — Phase 4.
- **Log retention** — Phase 1+ TODO.
- **Cost data source** — `claude -p --output-format json` vs
  task-runner's existing path, settled before Phase 3.

## Self-critique 2x applied (Phase 0 draft)

Round 1 (correctness/completeness) caught:

- `workflow_config` HTML-comment block needed an explicit parser
  definition.
- Tier 1 validators must re-run on every `workflow-append-steps`
  call, not only at first compile.
- Phase D's `__phase_c_replay__` sentinel was too magical — replaced
  with `REVISE: target=<step-id>:` convention in the trailer protocol.
- Cancel / resume / mid-iteration semantics were undefined.
- Phase F's termination condition (who casts the final approving
  vote) was ambiguous.

Round 2 (safety/edge cases/security) caught:

- Human gate had no approver allowlist — anyone in the Slack thread
  could `APPROVE`. Added mandatory `human_gate_approvers` list.
- Concurrent `workflow start`/`resume` on the same task-id would
  race. Added `.lock` file with `flock` + pid heartbeat.
- Log retention not specified (deferred to Open Questions).

## Self-critique 2x applied (Phase 0 revision — sweep + Operator)

Round 1 (correctness/completeness) caught:

- The original spec had `workflow resume` but no story for *finding*
  tasks that need resuming. Added `workflow-sweep` skill + CLI.
- Diagnose output needed a stable, parseable schema so AI callers
  can branch on it; defined a 7-value `diagnosis` enum.
- "CEO" lingered in three docs after the public-OSS reframing.
  Swept all references to "Operator".

Round 2 (safety/edge cases/security) caught:

- Auto-resume from sweep must respect the `.lock` file or two
  sweepers could race-resume the same task. Sweep's `--auto-resume`
  goes through the normal `workflow resume` path which already
  acquires the lock — no extra mitigation needed.
- `--llm-fallback` on diagnose could leak persona output in the
  fallback prompt; the diagnose CLI is responsible for the same
  secret-redaction pass we'll apply to `events.jsonl`. Tracked
  under the existing log-retention Open Question.

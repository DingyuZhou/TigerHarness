# ADR 0003 — Remove the legacy `claude -p` runners

- Status: accepted
- Date: 2026-06-10
- Deciders: Operator (direction), Shohoku (execution; journal task
  `20260610-remove-legacy-runners`)

## Context

tigerharness shipped two API-billed execution rails before the
file-based subscription journal existed: `task_runner` (iterative
`claude -p` jobs) and `workflow_runner` (multi-persona orchestration
plus the `workflow` console script and a PreToolUse "journal write
guard" hook that `init` wired into team settings). The journal
backend (`tigerharness.journal`, kinds `task` and `workflow`)
replaced both for day-to-day work and was declared solid by the
Operator on 2026-06-10. Keeping the runners cost test surface,
docs that drifted, and a misleading story about how the harness is
meant to be driven.

## Decision

Remove both runner sub-packages, their tests, their docs, the
`workflow` console script, the `assign-task` bundled skill, and the
write-guard wiring in `init`.

**Relocation, not deletion, for the compile core.** The journal's
workflow mode is built on the compile machinery that lived inside
`workflow_runner`: step models, the drafter prompt/parser, the
critic prompt builders, the Tier 1 validators, and orchestration
assembly. That core moved to `tigerharness.journal.wfcore`
(history-preserving renames), trimmed of everything that needed the
api runner's `SessionManager`/`TaskPaths`. The journal owns its own
dependencies now; nothing under `journal/` imports a runner.

## Migration note — stranded write-guard hooks (action required per team)

Teams scaffolded before this removal may carry a PreToolUse hook in
`<team>/.claude/settings.json` that invokes the deleted module and
will fail every Edit/Write in those sessions once this version is
installed. Remove the entry whose fields match (copied from
`init.py:464-467` as shipped):

- matcher: `"Edit|Write|NotebookEdit"`
- command: ends with
  `python -m tigerharness.workflow_runner.hooks.journal_write_guard`

Delete that one hook object from the `PreToolUse` list (leave any
others). The guard only ever protected the legacy
`workflow_journal/` truth files; the journal backend never used it,
so removal loses nothing. An idempotent de-registration pass in
`tigerharness init` was considered and deliberately deferred as a
separate feature: one known stranded population, a one-line manual
fix, versus new init behavior carrying its own tests under the
100% coverage floor.

## Consequences

- The journal is the only execution path; `agent_sdk`,
  `slack_bridge`, `tiger_memory`, `init`, `dismiss`, and the
  remaining bundled skills are unchanged.
- Console scripts: `tigerharness` and `tiger-memory` remain; the
  `workflow` script is gone.
- The 100% line+branch coverage gate held through every commit of
  the removal (relocate, trim, delete) with no weakening.
- Historical references to the runners in ADR 0001/0002 and older
  design docs stay as history; this ADR is the tombstone.

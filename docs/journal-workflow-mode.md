# journal — workflow mode (Phase 1.5 design)

Extending the file-based subscription backend to support multi-persona
workflow tasks (`kind=workflow`). Single-persona task mode (`kind=task`)
shipped in Phase 1 (commit `7d6b9f8` on main).

> **Status:** Design-only. This document is a proposal. Sections
> describing unbuilt pieces are the plan, not a description of shipped
> behaviour. Phase 1.5 = this doc plus the corresponding implementation,
> targeted as one branch + PR.

## Why this exists

Phase 1's `drive-journal` skill drives a *single persona* through a
free-form PRD — the task-runner-style workload. The Operator's question
on 2026-06-03: *how do I trigger workflow-runner-style work through
this backend?*

Answer today: you can't. `Status.from_dict` rejects `kind=workflow` so
neither the scaffolder nor the driver will touch one. The api-backed
`workflow-runner` is still available, but it costs token billing from
~2026-06-15 onwards, which defeats the whole point of the subscription
backend.

Phase 1.5 closes that gap: multi-persona workflow work driven by the
interactive Claude Code app, using the same `journal/` folder, the
same `drive-journal` skill, the same lazy sweep, and (almost) the same
`status.json` schema. The compile pipeline that lands the workflow
graph is **reused verbatim** from Wave 2 (`workflow_runner.compile`) —
the only new code is the journal-side glue that creates the task and
the driver-side protocol that walks the graph one persona at a time.

## The one trade-off you'll want to weigh in on

**Compile uses `claude -p` (api-billed). Drive uses subscription.**

`compile_playbook(...)` (Sakuragi's pipeline) calls a Tier 2 critique
loop with ≥3 iterations of Akagi + Ayako critique. Those critique
calls go through `SessionManager`, which today wraps `claude -p` — i.e.
api-billed tokens. The compile happens **once per workflow task** at
scaffold time, so you pay for one compile per workflow you start, not
once per session.

Concretely: a typical compile runs ~6 short Opus calls (3 rounds × 2
critics), costing maybe ~$0.10–$0.30 per task at current prices. That's
your only api-billed cost in workflow mode; every subsequent
`drive-journal` invocation that walks the graph is pure subscription.

Three options for handling this:

| Option | What it does | When it makes sense |
|---|---|---|
| **A (proposed)** | Compile uses api-billed `claude -p`; drive uses subscription. The one-shot cost is acceptable. | If workflow tasks are infrequent (a few per week / month). Simple, ships fast. |
| **B** | Compile happens **inside** the interactive session. `drive-journal` is taught to compile playbooks itself (drafter prompt + Tier 1 validators in code + Tier 2 critique as in-session reasoning). | If you want strict zero-api-billing. Substantial additional skill markdown; doubles Phase 1.5 scope. |
| **C** | Defer compile until the first drive: scaffold writes only the playbook + brief and marks `compile_pending=true`; the driver compiles on its first invocation, then proceeds. Same billing shape as B but cleaner split: scaffold is cheap, compile is part of drive. | The cleanest long-term design but requires the skill to know the entire compile machinery. |

**My recommendation: A.** Ship it now; revisit if compile-time api
billing becomes a real budget issue. The Phase 2 of *this* subsystem
(if needed) is the migration to C.

The doc below assumes Option A.

## Architecture (Option A)

```
You: write a brief + pick a playbook
    |
    v
Scaffolder (CLI `journal new --kind workflow --playbook X --task-brief Y`)
    |-- imports compile_playbook from workflow_runner.compile.pipeline
    |-- runs it (api-billed; one-shot) -> writes:
    |     active/<task-id>/orchestration.json
    |     active/<task-id>/steps/<step-id>.md   (one per node)
    |     active/<task-id>/task_brief.md
    |     active/<task-id>/playbook_snapshot.md
    |     active/<task-id>/compile_trace.txt
    |     active/<task-id>/compile_critique.md
    |-- writes journal/status.json (kind=workflow, state=pending,
    |   persona=<captain or null>, max_sessions=<heuristic>)
    v
journal/ (passive file-based state machine)         <-- source of truth
    ^
    |
    v
Driver (drive-journal skill, interactive session)
  1. Lazy sweep of active/ (unchanged)
  2. Pick ONE actionable task -- now classifies by `kind`:
       - kind=task: existing Phase 1 flow (read task.md, work continuously)
       - kind=workflow: read orchestration.json + the step file pointed
         at by orchestration.current_node, ADOPT that step's persona,
         work the step body until `WORKFLOW: APPROVE/REVISE/BLOCK`
  3. Route per the step's on_approve/on_revise/on_block edges; bump
     orchestration.current_node; loop within the same invocation
     until the workflow reaches __done__/__escalate__ or stops.
  4. Cascade unchanged: sweep again, pick the next actionable task.
```

The interactive session does **all** the work: per-step persona
adoption, the routing logic, the per-step bodies. No `claude -p`
processes spawn at drive time. No background workers.

## File layout (per-task additions when `kind=workflow`)

`active/<task-id>/` for a `kind=workflow` task contains everything a
`kind=task` task has, **plus** the workflow-runner's compiled artifacts:

```
active/<task-id>/
  task.md                # the brief (same as kind=task)
  status.json            # journal's state machine (same shape;
                         # `kind=workflow`, `persona` = captain or null)
  progress.md            # append-only log (same as kind=task)
  artifacts/             # task outputs (same as kind=task)

  task_brief.md          # written by compile_playbook (== task.md content;
                         # workflow-runner convention -- keep both for
                         # downstream readers that expect task_brief.md)
  playbook_snapshot.md   # the playbook verbatim, written by compile
  orchestration.json     # the compiled graph (steps array, edges,
                         # current_node, workflow_config)
  steps/<step-id>.md     # per-step frontmatter + prompt body, one per node
  compile_trace.txt      # Tier 1 dry-run trace
  compile_critique.md    # Tier 2 critique transcript
```

Done tasks land at `done/<task-id>/` with all of this preserved.

## status.json schema for workflow tasks

Same shape as Phase 1, with three field semantics adjusted:

| Field | Phase 1 (`kind=task`) | Phase 1.5 (`kind=workflow`) |
|---|---|---|
| `kind` | `"task"` only | `"task"` or `"workflow"` |
| `persona` | required; the assigned persona | optional; the "captain" / accountable owner; can be `null` (the per-step persona is read from the graph) |
| `max_sessions` | default `5` | default `len(steps) * 2` (heuristic: ~2 sessions per step to allow for revision cycles); override with `--max-sessions N` |

All other fields (`id`, `title`, `state`, `sessions`, `updated_at`,
`next_action`, `created_at`, `session_ref`) keep their Phase 1
semantics. The state machine is identical: `pending → in_progress →
(blocked) → done`. There is **no** `failed` state in Phase 1.5 either.

`Status.from_dict` is relaxed: `kind=workflow` is accepted but
**requires** `orchestration.json` to exist in the task dir; otherwise
the load is rejected as malformed. The journal layer enforces this in
a new helper (`Status.is_well_formed_on_disk(paths)` or similar).

## Driver protocol additions

The `drive-journal` skill's existing protocol stays the same for
`kind=task`. For `kind=workflow`, after step 2 (pick one actionable
task), the inner loop changes:

```
2a. If status.kind == "task":
      Phase 1 procedure -- work the PRD freely, persona = status.persona.
2b. If status.kind == "workflow":
      Read active/<task-id>/orchestration.json.
      Identify orchestration.current_node (or the entrypoint if just picked up).
      Read steps/<current_node>.md -- this is the prompt body for THIS step.
      ADOPT that step's persona (named in the step frontmatter).
      Work the step until the response emits one of:
        WORKFLOW: APPROVE
        WORKFLOW: REVISE: <reasons>
        WORKFLOW: BLOCK: <reasons>
      Route per the step's on_approve / on_revise / on_block edges:
        APPROVE   -> orchestration.current_node = step.on_approve
        REVISE    -> orchestration.current_node = step.on_revise
        BLOCK     -> orchestration.current_node = step.on_block (often
                     `__escalate__`)
      Persist orchestration.json atomically.
      Append a one-paragraph entry to progress.md naming the step,
      the verdict, the reasons, and the next_node.

      If current_node == "__done__": state=done. Continue cascade.
      If current_node == "__escalate__": state=blocked with next_action
        naming the escalation reason. Continue cascade.
      Otherwise (more steps to go): loop within the same invocation
        and adopt the next step's persona. The whole workflow runs
        in one cascade if the queue (and the human's attention) allows.
```

The same stop conditions still apply: `max_sessions`, human-ends-session,
unrecoverable blocker. A blocker mid-graph moves the task to `blocked`
with `next_action` naming the offending step and reasons.

This adds ~50 lines of protocol to `OPERATING.md` (the on-disk
template) and ~50 lines to `drive-journal/SKILL.md`. The protocol
deliberately mirrors `WORKFLOW: APPROVE/REVISE/BLOCK` trailer parsing
from the Phase 1 workflow-runner executor — same vocabulary, same
routing semantics, so a future migration path between the two stays
clean.

## CLI surface additions

`tigerharness journal new` gains three flags:

```
tigerharness journal new \
    --kind workflow \
    --playbook <name>           # resolves to teams/<team>/workflow/<name>.md
    --task-brief "<text>"       # inline brief; mutually exclusive with --brief-file
    --brief-file <path>         # brief from a file
    [--persona <captain>]       # optional; the accountable owner
    [--max-sessions N]          # override the len(steps)*2 default
```

Validation rules:

- `--kind workflow` requires `--playbook` AND one of `--task-brief` /
  `--brief-file`. (Mirrors `workflow start --playbook` in the
  Phase 1 workflow-runner CLI.)
- `--prd` is rejected for `kind=workflow` (use `--task-brief` /
  `--brief-file` instead). Conversely `--playbook` is rejected for
  `kind=task` (the existing Phase 1 path).
- `--persona` is optional for `kind=workflow` (the per-step personas
  come from the graph).

The existing `kind=task` invocation is unchanged.

`tigerharness journal list` table format gains a `KIND` column so the
operator can tell task vs workflow at a glance.

`tigerharness journal status <task-id>` JSON output is unchanged —
just reflects the new field semantics.

## Out of scope for Phase 1.5

Deferred to a later phase if and when needed:

- **Step-append at runtime.** The Wave 3 workflow-runner feature
  (`workflow-append-steps` skill) lets a step add follow-up steps to
  the graph at runtime. Phase 1.5 ships static graphs only.
- **Human gate enforcement.** Wave 3 / Phase 3 of the original
  workflow-runner spec. Phase 1.5 emits the human-gate signal via
  `next_action` and `state=blocked` but doesn't wait for an explicit
  approval CLI / Slack reaction.
- **Multiple workflows per task.** A task has exactly one graph. If
  you want a different graph, scaffold a new task.
- **Workflow-step-level `sessions` counter.** The
  `sessions` field counts `drive-journal` invocations (already), not
  per-step iterations. Per-step iteration caps come from each step's
  `max_iters` frontmatter, as in the existing workflow-runner.
- **Interactive compile** (Options B / C above). Compile stays
  api-billed in Phase 1.5.

## Open questions for the Operator

1. **Compile billing — confirm Option A?** "Compile uses api-billed
   `claude -p`; drive is pure subscription" is what the doc proposes.
   If you want strict zero-api-billing, we move to Option B/C, which
   roughly doubles Phase 1.5 scope.
2. **What playbooks exist?** The teams under `/home/tigerleap/projects/teams/`
   currently have `Shohoku/workflow/default.md` — a single playbook
   per team. Phase 1.5 makes `--playbook default` actually
   subscription-drivable. Are there other playbooks you want shipped
   with the rollout, or is the default playbook enough for now?
3. **`persona` field on `kind=workflow` status** — make it `null`
   (per-step personas only) or store the captain so `journal list`
   shows a sensible name? I lean **captain** for display; the field
   semantics doc above reflects that.
4. **Backwards-compat with existing workflow-runner journals?** We
   have task_journals from Wave 1 + Wave 2 runs sitting under
   `~/.local/state/tigerharness-workflows/`. The Phase 1.5 journal
   does NOT import them. If you want a migration tool, that's a
   separate small CLI (`journal import --from workflow_runner`).
   Deferred unless you ask.

## Implementation plan

If you greenlight this design (with Option A), the implementation is
contained to one branch and likely one PR. Rough sizing:

| Piece | LOC est | Notes |
|---|---|---|
| Model layer (`models.py`) | ~30 | Relax `kind` enum; add `is_well_formed_on_disk` helper that checks orchestration.json presence for `kind=workflow`. Update tests. |
| Scaffolder (`scaffold.py`) | ~80 | New `new_workflow_task(...)` entry point that imports `compile_playbook`, wires a `workflow_runner.TaskPaths` pointing at the journal's task dir, writes the journal status.json after the compile pipeline lands its artifacts. Atomic and ordered: status.json last. |
| CLI (`cli.py`) | ~50 | New flag parsing + dispatch on `--kind`. Mutual-exclusion + helpful error messages. |
| Sweep (`sweep.py`) | ~5 | Trivial: classifier doesn't care about kind, but `to_summary` could show "(N workflow, M task)". Optional. |
| OPERATING.md template + drive-journal SKILL.md | ~100 lines | The new protocol section. Pinned by smoke tests so a future doc edit can't accidentally delete the load-bearing landmarks. |
| Tests | ~250 | Scaffolder integration tests with a mocked compile pipeline (don't actually call `claude -p` in tests); CLI tests for the new flags + validation; OPERATING.md template smoke tests for the new section. Aim for 100% line+branch as in Phase 1. |
| Doc updates | ~50 | Extend `docs/journal.md` with the workflow section; reference this doc from `subscription-backend.md`. |

Total: roughly 500–600 LOC of code + ~100 lines of protocol markdown.
ETA: ~4–6 hours of focused work, similar shape to Phase 1.

## Phasing

- **Phase 1.5 (this doc)** — `kind=workflow` on the journal via Option
  A. Compile = api-billed once at scaffold; drive = subscription.
- **Phase 2** (if compile cost ever bites) — Option C: defer compile
  to first-drive, taught entirely to the interactive session via
  protocol additions. Replaces Phase 1.5's scaffolder compile call
  with a `compile_pending=true` marker.
- **Phase 3** — step-append, human gate enforcement, workflow-graph
  migration tool from the existing workflow-runner journals.

## Non-goals

- **Replacing the existing api-backed workflow-runner.** It stays;
  Phase 1.5 just makes the journal a viable subscription-friendly
  alternative for the same shape of workload.
- **Inventing a new graph format.** We reuse
  `workflow_runner.compile`'s output verbatim — `orchestration.json` +
  `steps/<id>.md` per the existing schema.
- **Parallel persona work.** A single human drives serially. Concurrent
  drivers stay out of scope; the heartbeat-as-soft-lease handles the
  rare race (same answer as Phase 1).

## Related

- [`subscription-backend.md`](subscription-backend.md) — the design
  Phase 1 implements; this doc extends.
- [`journal.md`](journal.md) — Phase 1 operator docs.
- [`workflow-runner.md`](workflow-runner.md) — the api-backed
  multi-persona runner whose compile pipeline we reuse.
- [`workflow-runner-phase2.md`](workflow-runner-phase2.md) — the
  compile/drafter/critique pipeline this design depends on (shipped in
  Wave 2).

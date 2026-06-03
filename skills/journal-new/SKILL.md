---
name: journal-new
description: Scaffold a new journal task -- either kind=task (single persona from a PRD) or kind=workflow (multi-persona compiled from a team playbook). Use when the user asks to "create a task", "start a journal entry", "scaffold a PRD as a journal task", "compile a workflow", "scaffold a workflow run from a playbook", or hands over a markdown brief they want tracked in the file-based subscription backend. Wraps `tigerharness journal new ...`.
---

# journal-new

Skill for the **subscription backend's** scaffolder. Creates a new
journal task -- either single-persona (`kind=task`) or multi-persona
(`kind=workflow`) -- on disk under `journal/active/<task-id>/`. The
`drive-journal` skill then drives the task from there.

The subscription backend runs work through the *interactive* Claude
Code app (subscription-billed) instead of through `claude -p` (which
moves to API-token billing soon). See
[`docs/journal.md`](../../docs/journal.md) for the architecture and
[`docs/journal-workflow-mode.md`](../../docs/journal-workflow-mode.md)
for the workflow-mode details.

## When to use this skill

Trigger when the user asks anything like:

- "scaffold this PRD as a journal task"
- "create a journal entry for ..."
- "start a new task for <persona>"
- "compile this workflow" / "scaffold a workflow from the playbook"
- "run this playbook on <brief>"
- hands over a markdown brief and says "track this"

Decide between **task mode** and **workflow mode** by the user's
framing:

- One persona, one PRD, free-form work -> task mode (`--kind task`).
- Multi-persona work driven by a team playbook (`teams/<team>/workflow/<name>.md`)
  -> workflow mode (`--kind workflow`).

If the user already has a PRD or brief file on disk: read its path.
If they gave it inline in the message: write it to a temp file first
(e.g. `/tmp/<short-name>.md`), then pass that path -- OR for workflow
mode, use `--task-brief` to pass the text inline.

## How to invoke -- task mode (`kind=task`)

```bash
tigerharness journal new \
    --kind task \
    --prd <path-to-PRD.md> \
    --persona <persona-name> \
    [--title "<human label>"] \
    [--max-sessions 5]
```

`--kind task` is the default; you can omit it for backwards-compat.

Required args:

- `--prd` -- path to the PRD markdown file.
- `--persona` -- the persona this task is assigned to (must exist in
  the team's `configs/personas.yaml`).

Optional:

- `--title` -- explicit human label. Defaults to the first H1 of the
  PRD, then `"task"` if none.
- `--max-sessions` -- soft ceiling on `drive-journal` invocations
  before the task moves to `blocked` for human review. Default 5.
- `--slug` -- override the slug portion of the task id.

## How to invoke -- workflow mode (`kind=workflow`)

```bash
tigerharness journal new \
    --kind workflow \
    --playbook <bare-name> \
    --task-brief "<inline brief text>" \
    [--team Shohoku] \
    [--captain <persona-name>] \
    [--title "<human label>"] \
    [--max-sessions 10]
```

Or with a brief file:

```bash
tigerharness journal new \
    --kind workflow \
    --playbook <bare-name> \
    --brief-file <path-to-brief.md> \
    [--team Shohoku] \
    [--captain <persona-name>]
```

Required args:

- `--playbook` -- bare playbook name; resolves to
  `teams/<team>/workflow/<name>.md`.
- `--task-brief` OR `--brief-file` -- exactly one of these. Inline
  brief text or a path to a markdown brief file.

Optional:

- `--team` -- which team's playbook + persona registry to use.
  Default: `Shohoku`.
- `--captain` -- accountable owner shown in `journal list` (the
  per-step personas come from the compiled graph, not this field).
  Default: none.
- `--title` -- explicit human label. Defaults to the first H1 of the
  brief, then `"workflow"` if none.
- `--max-sessions` -- soft ceiling. Default 10 for workflow mode
  (vs 5 for task mode).
- `--slug` -- override the slug portion of the task id.

The scaffolder pre-flights the team's compile personas (by default:
Anzai, Akagi, Ayako -- configurable in
`teams/<team>/configs/workflow.yaml` under `compile_personas`) AND
every persona referenced in the playbook prose; it fails fast (exit
code 2) with a clear error if any prompt is missing.

The actual compile (drafter + critic loop) is **deferred to the
first `drive-journal` invocation** by design -- the scaffolder does
no LLM work and incurs no API billing. `compile_pending=true` on
status.json signals the driver to run the compile sub-protocol
described in `<journal>/OPERATING.md` before walking the graph.

## After scaffolding

The command prints the new task id, the task_dir path, and (for
workflow mode) the playbook + team. Report these back to the user so
they can `cd` to inspect.

Tell the user: invoke the `drive-journal` skill in their interactive
session to start working the task. (Don't drive it yourself unless
they ask -- the scaffolder's job ends at task creation.)

For workflow mode, also remind the user that the first drive will
run the in-session compile (drafter + critic loop) before any
graph-walking happens. Round-by-round progress is appended to
`compile/round-NN-*.md` files in the task directory.

## What gets created

For **task mode**, in `<journal>/active/<task-id>/`:

- `task.md` -- the PRD verbatim.
- `status.json` -- seeded with `state=pending`, `sessions=0`,
  `kind=task`, and the persona / title / max_sessions from the args.
- `progress.md` -- empty starter file with a single H1.
- `artifacts/` -- empty subdirectory the task fills as it works.

For **workflow mode**, in `<journal>/active/<task-id>/`:

- `task_brief.md` -- the brief verbatim.
- `playbook_snapshot.md` -- the team playbook as it was at scaffold
  time (frozen here so a later playbook edit doesn't invalidate the
  task mid-flight).
- `status.json` -- seeded with `state=pending`, `sessions=0`,
  `kind=workflow`, `compile_pending=true`, `compile_phase=pending`,
  the captain (if any) / title / max_sessions.
- `progress.md` -- empty starter file with a single H1.
- `artifacts/` -- empty subdirectory.

On first use the scaffolder also lands a canonical `OPERATING.md` at
the journal root -- the vendor-neutral protocol the driver reads.

## Out of scope

- **Editing existing tasks.** This skill only creates. Status updates
  are driven by `drive-journal`.
- **Compile-time API calls.** The scaffolder is pure Python; the
  compile is deferred to the in-session driver per `OPERATING.md`'s
  compile sub-protocol.
- **Step-append at runtime** (extending a workflow's graph after
  initial compile). That's a separate skill: `workflow-append-steps`.

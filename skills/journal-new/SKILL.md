---
name: journal-new
description: Scaffold a new journal task from a PRD. Use when the user asks to "create a task", "start a journal entry", "scaffold a PRD as a journal task", or hands over a markdown brief they want tracked in the file-based subscription backend. Wraps `tigerharness journal new --prd ...`.
---

# journal-new

Skill for the **subscription backend's** scaffolder. Creates a new
journal task from a PRD markdown file — establishes the on-disk state
machine the `drive-journal` skill will then drive.

The subscription backend runs work through the *interactive* Claude
Code app (subscription-billed) instead of through `claude -p` (which
moves to API-token billing soon). State lives in
`journal/active/<task-id>/`. See
[`docs/subscription-backend.md`](../../docs/subscription-backend.md).

## When to use this skill

Trigger when the user asks anything like:

- "scaffold this PRD as a journal task"
- "create a journal entry for ..."
- "start a new task for <persona>"
- hands over a markdown brief and says "track this"

If the user already has a PRD file on disk: read its path. If they
gave you the PRD inline in the message: write it to a temp file first
(e.g. `/tmp/<short-name>.md`), then pass that path.

## How to invoke

```bash
tigerharness journal new \
    --prd <path-to-PRD.md> \
    --persona <persona-name> \
    [--title "<human label>"] \
    [--max-sessions 5]
```

Required args:

- `--prd` — path to the PRD markdown file.
- `--persona` — the persona this task is assigned to (must exist in
  the team's `configs/personas.yaml`).

Optional:

- `--title` — explicit human label. Defaults to the first H1 of the
  PRD, then `"task"` if none.
- `--max-sessions` — soft ceiling on `drive-journal` invocations
  before the task moves to `blocked` for human review. Default 5.
- `--slug` — override the slug portion of the task id.

The command prints the new task id and the path to its `task_dir`.
Report both back to the user so they can `cd` to inspect.

## What gets created

In `<journal>/active/<task-id>/`:

- `task.md` — the PRD verbatim.
- `status.json` — seeded with `state=pending`, `sessions=0`,
  `kind=task`, and the persona / title / max_sessions from the args.
- `progress.md` — empty starter file with a single H1.
- `artifacts/` — empty subdirectory the task fills as it works.

On first use the scaffolder also lands a canonical `OPERATING.md` at
the journal root — the vendor-neutral protocol the driver reads.

## After scaffolding

Tell the user: invoke the `drive-journal` skill in their interactive
session to start working the task. (Don't drive it yourself unless
they ask — the scaffolder's job ends at task creation.)

## Out of scope

- `kind=workflow` (multi-persona pre-compiled graph) is reserved for a
  later phase. The CLI rejects `--kind=workflow` in v1; if the user
  asks for a workflow task, surface the limitation rather than
  inventing it.
- Editing existing tasks: this skill only creates. Status updates are
  driven by `drive-journal`.

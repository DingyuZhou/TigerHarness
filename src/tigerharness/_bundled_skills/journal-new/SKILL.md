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

## Slack-triggered? Stay lean (the API rail)

When triggered from Slack to schedule a journal task, you are on the
API-billed rail: do the minimum -- which since the deferred inbox
landed means **don't even scaffold**.

1. Collect the Operator's message VERBATIM (the whole conversation
   that defines the ask) plus a short title and the team name --
   nothing else.
2. Run exactly ONE command, the defer verb:

   tigerharness journal defer --title "..." --team <Team> \
       --payload-file <verbatim.md> [--thread-ts <ts>] [--requester <who>]

   (or pipe the verbatim text on stdin). It copies the conversation
   into the team journal's `deferred/` inbox -- no playbook read, no
   validation beyond team pinning, no LLM, strictly cheaper than
   `journal new`. The scaffold + persona preflight + compile all
   happen later, inside `drive-journal` on the subscription rail
   (`journal materialize` -- the driver runs it, not you).
3. Reply with the deferred id — one short message.
4. STOP.

`journal new` directly from Slack is still allowed when the Operator
explicitly asks for an immediate scaffold, but the defer verb is the
default — it is the cheapest possible Slack-side path.

Forbidden on this rail: repo exploration, file reads beyond this
skill's own needs, design work, journal sweeps, claiming, driving,
compile turns, materializing. All real work happens later, on the
subscription rail, via `drive-journal` in an interactive session --
and that is a hard rule, not a preference: Slack schedules, never
drives (`journal claim` refuses bridge sessions mechanically). Rails
and billing: `docs/subscription-backend.md`.

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

## The verbatim Operator-message section (MANDATORY)

Every PRD (`kind=task`) and every brief (`kind=workflow`) this skill
scaffolds MUST end with an "Operator's original message (verbatim)"
section. This is the relay-drift guard: the persona working the task
weeks later reads what the Operator actually said, not a paraphrase
of a paraphrase. If the PRD/brief you were handed lacks the section,
ADD it before scaffolding (appending to the file or the inline brief
text); do not scaffold without it.

Copy-paste template:

```markdown
## Operator's original message (verbatim)

> <the Operator's message, quoted verbatim -- no paraphrase, no
> translation, no typo fixes; keep the original language. Multi-turn
> context goes in as multiple quoted blocks in order.>

(<attribution: who, date, channel -- e.g. "Operator, 2026-06-11,
Slack DM thread 1781169418.787159" or "Operator, in-session
instruction to Ayako">. If the wording above was relayed or
translated by a persona rather than copied, say so here.)
```

Reading notes (clarifying nicknames, typos, or term mappings) may
follow the quote, clearly marked as interpretation -- never edit the
quote itself.

**When there is no Operator message** (self-initiated follow-up, a
scheduled/recurring task, or a task derived from another task's
artifacts), the section still appears, stating that explicitly so its
absence is informative rather than ambiguous:

```markdown
## Operator's original message (verbatim)

None -- this task was not initiated by a direct Operator message.
Origin: <e.g. "follow-up drafted in task <id>'s artifacts" /
"scheduled self-diagnosis run" / "scaffolded by <persona> under
standing authorization <where>">.
```

## How to invoke -- task mode (`kind=task`)

```bash
tigerharness journal new \
    --kind task \
    --prd <path-to-PRD.md> \
    --persona <persona-name> \
    [--title "<human label>"] \
    [--max-sessions 3]
```

`--kind task` is the default; you can omit it for backwards-compat.

Required args:

- `--prd` -- path to the PRD markdown file.
- `--persona` -- the persona this task is assigned to (must exist in
  the team's `configs/personas.yaml`). **Optional if the team
  declares `default_persona:` at the top of `configs/personas.yaml`**
  -- in that case omitting `--persona` uses the team default.
  `tigerharness init` seeds the first persona created on a team as
  the default automatically.

Optional:

- `--title` -- explicit human label. Defaults to the first H1 of the
  PRD, then `"task"` if none.
- `--max-sessions` -- soft ceiling on `drive-journal` invocations
  before the task moves to `blocked` for human review. Default 3; raise
  it with `--max-sessions N` for bigger tasks (e.g. "do this task in 10
  iterations" -> `--max-sessions 10`).
- `--early-exit` -- let the driver stop as soon as the task is done per
  its acceptance criteria. Default off: run the full `--max-sessions`
  budget ("N iterations = exactly N").
- `--autonomy ask|judgement` -- detached-run autonomy level (default
  `ask`). `judgement` lets the working persona self-resolve
  yellow-light judgment calls instead of releasing blocked, logging
  each as a `Decision:` entry in progress.md and the final work note.
  Red-light rules are never overridable; low-confidence decisions
  still escalate. Boundary defined in `charter/README.md` ("Autonomy
  levels for detached work"). Works for both kinds.
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
  Default: none. **Falls back to `default_captain:` in the playbook's
  HTML-comment YAML metadata block if present**, so a team-standard
  playbook can declare its owner once instead of every scaffold
  needing to pass `--captain`.
- `--title` -- explicit human label. Defaults to the first H1 of the
  brief, then `"workflow"` if none.
- `--max-sessions` -- soft ceiling. Default 10 for workflow mode
  (vs 3 for task mode).
- `--slug` -- override the slug portion of the task id.

The scaffolder pre-flights the team's compile-time personas AND
every persona referenced in the playbook prose; it fails fast (exit
code 2) with a clear error if any prompt is missing.

The compile-time personas play three roles: `drafter` (writes the
steps bundle), `akagi` (execution-mechanics critic), `ayako` (QA
critic). The default mapping is `drafter=Anzai`, `akagi=Akagi`,
`ayako=Ayako`. Teams can override this via
`teams/<team>/configs/workflow.yaml`:

```yaml
compile_personas:
  drafter: Sakuragi      # any persona from configs/personas.yaml
  akagi:   Rukawa
  ayako:   Mitsui
```

Partial overrides are fine -- unset roles fall back to the default.
Run `tigerharness journal validate-personas <team>` to confirm the
mapping resolves cleanly before scaffolding.

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

- `task.md` -- the PRD verbatim (including the mandatory
  "Operator's original message (verbatim)" section).
- `status.json` -- seeded with `state=pending`, `sessions=0`,
  `kind=task`, and the persona / title / max_sessions from the args.
- `progress.md` -- empty starter file with a single H1.
- `artifacts/` -- empty subdirectory the task fills as it works.

For **workflow mode**, in `<journal>/active/<task-id>/`:

- `task_brief.md` -- the brief verbatim (including the mandatory
  "Operator's original message (verbatim)" section).
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

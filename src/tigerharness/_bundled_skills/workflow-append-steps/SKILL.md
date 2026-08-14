---
name: workflow-append-steps
description: Append new step(s) to a kind=workflow journal task's already-compiled graph at runtime. Use when a step's output produces concrete follow-up work that wasn't in the original plan (e.g. Anzai's planning step discovers an extra QA pass is needed). Wraps `tigerharness journal append-steps`. ONLY use from inside an active drive-journal session on a workflow whose compile_phase is `complete`.
---

# workflow-append-steps

Phase 3 skill for extending a workflow task's step graph at runtime.
The journal compile is normally a one-shot at the top of the cascade
(see `OPERATING.md` -> "Compile sub-protocol"); this skill lets a step
discover new work and **append** it without re-scaffolding or
re-compiling from scratch.

The on-disk shape after a successful append:

- `orchestration.json` -- `steps` array extended with the new ids,
  `edges` map gains the new entries. All atomic via temp + rename.
- `steps/<new-id>.md` -- new frontmatter file per new step.
- `status.updated_at` -- heartbeat refreshed.

The skill **fails closed**: a new bundle that doesn't pass Tier 1
(ref-resolution, roster membership, cycle bound, dry-run trace) leaves
`orchestration.json` and `steps/` byte-identical. No partial append.

## When to use this skill

- A step's output identifies concrete follow-up work that was NOT in
  the original playbook plan -- e.g. Anzai's plan step calls for an
  extra QA pass before ship; the QA pass needs its own
  step-frontmatter so the executor can route to it.
- The follow-up work is **append-only** (never re-orders or rewrites
  earlier steps). The append-only invariant is enforced by
  `journal append-steps` rejecting any new step id that collides with
  the existing graph.
- The task is currently `kind=workflow` with `compile_phase=complete`
  (the graph is landed). Otherwise the CLI refuses with a clear error.

## How to invoke

1. Adopt the **drafter-role persona** (the one named under `drafter:`
   in the original `compile-context` bootstrap mapping; default
   `Anzai`). The append step is a mini-compile, so the same drafter
   discipline applies.

2. Emit a `steps-bundle` containing ONLY the new step file(s) -- the
   same drafter format as the original compile output:

   ```steps-bundle
   ## step: 03-mitsui-qa
   ---
   id: 03-mitsui-qa
   persona: Mitsui
   role: qa
   on_approve: __done__
   on_revise: 03-mitsui-qa
   on_block: __escalate__
   max_iters: 5
   timeout_sec: 1800
   parallel_with: []
   ---
   QA the implementation per the plan's acceptance criteria.
   ```

   A body that needs to show a literal `## step: ...` line (instructions
   that document this very format) must escape it with a leading
   backslash -- `\## step: <id>`. The backslash is stripped at parse
   time; an unescaped one starts a new step file instead. A fenced code
   block will not save you: the bundle ends at the first bare
   triple-backtick line.

3. Save the bundle to a file (e.g. `compile/append-NN.md` under the
   task directory; the suffix can be whatever -- the journal doesn't
   care).

4. Run:

   ```bash
   tigerharness journal append-steps --task <task-id> \
       --new-bundle <path-to-bundle.md>
   ```

5. Read the CLI's stdout:

   - **Success** (exit 0): prints `appended: N step(s) to <task-id>`
     and lists each new step's id / persona / role. The
     `orchestration.json` is now extended; the executor will route
     into the new step on the next `on_approve` that points at one of
     them.
   - **Tier 1 failure** (exit 1): prints a JSON envelope
     `{ok: false, errors: [...], trace: "..."}`. Treat the errors as
     feedback for a re-draft (same as in the original compile loop)
     and try again with a corrected bundle. The on-disk graph is
     unchanged.
   - **Operator error** (exit 2): bad task id / wrong phase /
     unreadable bundle / etc. The error message names the cause.

## Important: reachability is your responsibility

`append-steps` is **purely additive**: it adds new step ids + new
edges to `orchestration.json`, but it never **rewires** an existing
step's `on_approve` / `on_revise` / `on_block`. That means a newly
appended step is only reachable from the graph-walk if some EXISTING
edge already pointed at its id (which is impossible if you just
invented the id) OR you route into it from a NEW step that's itself
reachable.

In practice the useful pattern is:

1. The existing graph-walk reaches a step whose `on_approve` points
   at an id like `__pending_qa__` -- a "promise" the original
   drafter left for a later append to fulfill. (You'd have planned
   for this at compile time.)
2. You append a new step whose id IS `__pending_qa__`, satisfying
   the promise -- the existing step's edge now resolves to a real
   step in the graph.

If you didn't plan for a promise slot at compile time, the most
honest move is to surface the limitation to the human: "I want to
add step X, but the existing graph has no edge pointing at X, so the
new step would be unreachable. Either re-scaffold from a richer
playbook, or accept that the step is documented in the graph but not
executed by the walk."

A future Phase 3+ enhancement may add an `--after-step-id` argument
to rewire one existing edge. Today, `append-steps` only adds nodes.

## What NOT to do

- **Don't use this to fix bugs in already-executed steps.** This skill
  is append-only by design. To rewrite an existing step, you need to
  abort + re-scaffold (or, in extreme cases, hand-edit the step file
  -- which is outside the protocol).
- **Don't use it before compile is complete.** Until the original
  graph lands, you'd be appending to nothing. The CLI checks
  `status.compile_phase` and refuses if it's not `complete`.
- **Don't invoke from a `claude -p` / cron context.** Same rule as the
  rest of the journal driver: skill-only by design, because the
  subscription billing model depends on a human-driven interactive
  session.
- **Don't claim a step id that already exists.** The CLI's collision
  check will reject it, but you'll save yourself a round trip by
  picking fresh ids (`03-...`, `04-...` after the existing `01-/02-`).

## Recovery from a rejected append

A Tier 1 failure leaves `orchestration.json` and `steps/` untouched.
You can:

- Read the errors, redraft, and retry the same `journal append-steps`
  call with the new bundle.
- If the failure indicates a structural issue (e.g. you wanted to
  re-link an existing step), give up on append-steps for this case
  and surface the limitation to the human.

The skill has no notion of multi-round critique like the original
compile sub-protocol -- a single drafter -> Tier 1 -> commit cycle is
the contract. If you need critic review of a complicated append, do
that in-session (read the bundle aloud, reason about it) before
running `journal append-steps`.

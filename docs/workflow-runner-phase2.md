# workflow-runner — Phase 2 spec

> **Status:** Draft / design phase. Anchor for the Phase 2 team build.
> Companion to [`workflow-runner.md`](workflow-runner.md) (the canonical
> spec, which describes the full design across all phases) and to
> [`adr/0002-workflow-runner-phase2.md`](adr/0002-workflow-runner-phase2.md)
> (the decision log for this phase).

## Why a Phase 2

The Phase 1 MVP shipped the **execution half** — a deterministic Python
loop that drives a graph of pre-compiled step files through per-persona
Claude sessions, with constraint enforcement and a write-once journal.

The Phase 1 audit (and the adversarial re-verification) confirmed the
**authoring half** is the missing piece. Today, to actually run a
workflow you must hand-author every `steps/*.md` file with frontmatter
and point `workflow start --steps <dir>` at them. The original design's
"ask Shohoku to use their default workflow on this task" UX is
unreachable.

Phase 2 closes that gap with five connected deliverables:

1. **Compile phase** — turn a freestyle markdown playbook + a task brief
   into validated step files, with the defense-in-depth the Operator
   specifically asked for (Tier 1 mechanical validators + Tier 2 AI
   critique loop, ≥3 iterations).
2. **Step-append** — the running step can append new step files
   (closes the original "planning settles before exec steps are
   generated" requirement).
3. **CLI surface evolution** — `workflow start` accepts a playbook
   name and a task brief; `--steps <dir>` becomes the escape hatch
   for pre-compiled flows.
4. **`workflow-run` skill** — the agent-callable wrapper that closes
   the trigger-skill gap.
5. **Active write guardrail** — a hook that enforces "program writes
   the journal, not AI" instead of relying on persona-prompt convention.

The **human gate** (Tier 3) is *modeled* in `WorkflowConfig` already;
Phase 2 emits the necessary `human_gate_requested` event but leaves
**enforcement** to Phase 3. Stuck-watchdog (Phase 4) and parallelism
(Phase 5) remain out of scope.

---

## 1. Compile phase

### Inputs and outputs

```
Inputs:                         Outputs (under <task_dir>/):
  playbook .md (prose)            task_brief.md          (verbatim copy)
  task brief (str or file)        playbook_snapshot.md   (verbatim copy of playbook)
  team config (personas roster)   compile_trace.txt      (Tier 1 dry-run trace)
                                  compile_critique.md    (Tier 2 transcript)
                                  steps/<id>.md           (one per emitted step)
                                  orchestration.json     (real values, not placeholders)
```

The Phase 1 placeholders (`playbook="precompiled"`, `compiled_by="cli"`,
`compile_critique_iters=0`) are **replaced** with real values:
`playbook=<playbook-name>`, `compiled_by=<persona-name>` (typically
`"anzai"`), `compile_critique_iters=<n>`.

### High-level flow

```
1. Take playbook + task_brief + roster.
2. Anzai drafts an initial set of step files (prompt: "Read the
   playbook and brief; emit one step file per phase, with frontmatter
   matching docs/workflow-runner.md").
3. Tier 1 validators run mechanically over the draft.
   - On failure: surface validator errors back to Anzai; loop.
4. Tier 2 critique loop runs (min 3 iterations, capped at
   workflow_config.max_compile_iters):
   - Akagi (execution critic): APPROVE or REVISE with reasons.
   - Ayako (QA critic):        APPROVE or REVISE with reasons.
   - If either REVISEs: Anzai integrates the feedback, re-emits, loop.
   - If both APPROVE for the minimum number of iterations (≥3): done.
5. (Tier 3 placeholder.) Emit `human_gate_requested` event so the
   downstream Phase-3 work has a clean integration point. Phase 2
   does NOT wait for approval -- compilation completes immediately.
6. Persist artifacts (steps/, orchestration.json, traces).
7. Emit `compile_completed` event.
```

### Tier 1 — mechanical validators (pure code, can't be skipped)

| Validator | What it checks |
|---|---|
| **schema** | Every emitted step file parses as YAML frontmatter + body and has every required field (`id`, `persona`, `role`, `on_approve`, `on_revise`, `on_block`, `max_iters`, `timeout_sec`). |
| **ref** | Every `on_*` target points to a real step id or one of the sentinels `__done__` / `__escalate__`. |
| **roster** | Every `persona` named exists in the team's `configs/personas.yaml`. |
| **cycle** | The step graph has no unbounded cycle; every cycle in the graph contains at least one step with a finite `max_iters` cap. |
| **dry-run trace** | Walk the graph statically, emit a human-readable trace (`"happy path: A→B→C→__done__; if B revises: A→B→A→B…, capped at max_iters=5"`). Saved to `compile_trace.txt`. The trace is for humans and for the compile_critique transcript; it does not gate the compile by itself, but feeds Tier 2 critics. |

**Failure handling.** Tier 1 failures are fed back to Anzai as a single
structured error block; she re-emits. If Tier 1 fails
`max_compile_iters` times in a row, compile aborts with
`compile_failed{tier:1, errors:[...]}`.

### Tier 2 — forced AI critique loop (≥3 iterations, can't be skipped)

Two reviewer personas run in parallel each round; each independently
returns `APPROVE` or `REVISE: <reasons>`:

- **Akagi** reviews the *execution* dimensions: is the routing sound?
  Are responsibilities cleanly delegated? Is the dev/QA assignment
  fan-out coherent?
- **Ayako** reviews the *QA* dimensions: are testing gates present?
  Are critique loops sized realistically? Is the doc step actually
  going to land docs?

Both must return APPROVE on the same iteration to terminate the loop.
A REVISE from either restarts the round: Anzai integrates the feedback
and re-emits. The transcript (round number, each critic's verdict,
each critic's reasons, Anzai's response) is appended to
`compile_critique.md`.

**Hard floor:** the loop runs **at least 3 iterations** even if both
critics APPROVE on round 1 (the Operator's "force critique to look hard
3 times" rule). After the floor, the loop terminates the first round
both critics APPROVE.

**Hard ceiling:** `workflow_config.max_compile_iters` (default 8).
Reaching it aborts with `compile_failed{tier:2, last_verdicts:{...}}`.

### Tier 3 placeholder

Phase 2 emits `human_gate_requested` with the would-be approvers, then
**proceeds** without waiting. Phase 3 will add the wait + the
`workflow approve` CLI / Slack-reaction integration.

### Module layout (recommended)

```
src/tigerharness/workflow_runner/compile/
    __init__.py
    drafter.py          # Anzai's drafter prompt + JSON->steps parser
    validators.py       # Tier 1: schema/ref/roster/cycle/trace
    critique.py         # Tier 2: critic prompts, loop runner, transcript
    pipeline.py         # public entrypoint: compile_playbook(...)
```

### Public API

```python
from tigerharness.workflow_runner.compile import compile_playbook

result: CompileResult = compile_playbook(
    playbook_path=Path("teams/Shohoku/workflow/default.md"),
    task_brief="Add cache eviction to the redis layer.",
    team_root=Path("teams/Shohoku"),
    task_paths=TaskPaths(...),     # already minted by cmd_start
    *,
    session_manager=None,          # injected in tests
    max_compile_iters=8,
)

# CompileResult fields:
#   steps: list[StepFrontmatter]   # final, validated set
#   orchestration: Orchestration   # ready to persist
#   critique_iters: int            # number of Tier-2 rounds run
#   trace: str                     # dry-run trace string
#   transcript: str                # compile_critique.md content
```

---

## 2. Step-append at runtime

### The original design rule

> "The current executing task step is able to add task steps after it.
> When this happens, it also needs to modify the task orchestration
> file to reflect it. This is useful because a planning step could be
> already settled before real executing steps are generated."

### Mechanism

A new skill `workflow-append-steps` is exposed to persona subprocesses.

> **Shipped as:** runtime step-append landed in the **journal** backend,
> not in `workflow_runner` — there is no
> `workflow_runner.compile.append_steps` function. The shipped surface is
> the CLI `tigerharness journal append-steps --task <id> --new-bundle <path>`
> (driven by the `workflow-append-steps` skill); see
> [`journal-workflow-mode.md`](journal-workflow-mode.md). The original
> Phase-2 design (below) called a module function instead:

```python
# Original Phase-2 design -- NOT shipped (see note above):
from tigerharness.workflow_runner.compile import append_steps

append_steps(
    task_id="20260531-foo-7abc1234",   # from the current task
    new_steps=[                         # one or more step .md texts
        "---\nid: 05-rukawa-impl\npersona: rukawa\nrole: developer\n"
        "on_approve: 06-haruko-qa\non_revise: 04-anzai-revise\n"
        "on_block: __escalate__\nmax_iters: 10\ntimeout_sec: 1800\n---\n"
        "Implement the cache eviction policy per Anzai's plan."
    ],
    after_step_id="04-anzai-revise",    # insertion point in orchestration
)
```

### Re-validation contract

`append_steps` re-runs **Tier 1 validators** over the *new full graph*
(existing steps + appendices). This catches:

- Duplicate step ids.
- References to non-existent step ids (an appendix that points at a
  step the appender forgot to include).
- Cycles re-introduced after a previously-valid graph.
- Roster drift (a persona who was removed since the original compile).

**On failure:** the append is rejected, the new step files are deleted
from `steps/`, `orchestration.json` is left untouched, and an
`append_rejected{tier:1, errors:[...]}` event is emitted. The
currently-executing step gets the error in its next dispatch's
prologue (similar to the parse-failure reprompt pattern) and may retry
the append, bounded by its own `max_iters`.

**On success:** new step files are written under `steps/`, the
orchestration `steps` array is extended (atomic write), and an
`append_completed{added:[id1, id2, ...]}` event is emitted.

### Append-only invariant

`append_steps` only ever extends the `steps` array; it never removes,
re-orders, or rewrites previously-executed (or currently-executing)
step ids. A defensive check in `append_steps` itself enforces this
(the validator that confirms `existing_steps == new_steps[:len(existing)]`).

### Skill spec

```
File: /home/tigerleap/projects/tigerharness/skills/workflow-append-steps/SKILL.md

description: Append one or more pre-validated step files to the
currently-running workflow task. Use only from inside a workflow
step that is itself running, after planning produces new concrete
sub-tasks.
```

---

## 3. CLI surface evolution

### Argument additions

`workflow start` gains three new arguments and the existing `--steps`
becomes the escape hatch:

| Flag | New / kept | Meaning |
|---|---|---|
| `--team <name>` | kept | Team root resolution (unchanged). |
| `--playbook <name>` | **NEW** | Resolves to `teams/<Team>/workflow/<name>.md`. Default: `"default"`. |
| `--task-brief <text>` | **NEW** | Inline brief; mutually exclusive with `--brief-file`. |
| `--brief-file <path>` | **NEW** | Brief from a file (handy for long briefs piped from agents). |
| `--thread <slack_thread_ts>` | **NEW** | Optional; written into status.json so notifications + the human gate route back to the right Slack thread. |
| `--task-id <id>` | kept | Unchanged. |
| `--no-run` | kept | Unchanged. |
| `--steps <dir>` | kept (escape hatch) | Skips compile and uses the pre-compiled files as-is. **Mutually exclusive** with `--playbook` / `--task-brief` / `--brief-file`. |

### Argument validation rules

- `--playbook` requires either `--task-brief` or `--brief-file` (the
  compile phase needs a brief).
- `--steps` is incompatible with `--playbook` / `--task-brief` /
  `--brief-file` (you can't compile and use pre-compiled files
  simultaneously).
- `--thread` is purely metadata in Phase 2; Phase 3 (human gate)
  reads it.

### Compile-mode flow inside `cmd_start`

```
if args.steps:                          # legacy / escape hatch
    use pre-compiled steps (Phase 1 behaviour, unchanged).
else:
    playbook_path = teams/<team>/workflow/<args.playbook>.md
    brief = args.task_brief or read(args.brief_file)
    task_paths = mint + ensure()
    write(task_paths.task_brief, brief)
    write(task_paths.playbook_snapshot, playbook_path.read_text())
    result = compile_playbook(playbook_path, brief, team_root, task_paths)
    write_artifacts(task_paths, result)
    if args.no_run:
        print init summary; return 0
    return _run_task(task_paths, task_id)
```

---

## 4. `workflow-run` skill

A thin agent-callable wrapper so any persona / agent can launch a
workflow via natural language.

```
File: /home/tigerleap/projects/tigerharness/skills/workflow-run/SKILL.md

description: Launch a team workflow on a task brief. Use this when
the user (or another agent) asks the team to run a named workflow.
Examples:
  - "Ask Shohoku to use the default workflow to add cache eviction."
  - "Spin up the planning workflow on this brief."
  - "Have the team use the MVP playbook to ship a draft of X."
```

Skill body documents the invocation:

```bash
# From any team directory:
workflow start \
    --team <TeamName> \
    --playbook <name> \
    --task-brief "<the user's ask, verbatim or paraphrased>" \
    [--thread <slack_thread_ts>]    # iff invoked from a Slack thread
```

The skill teaches the agent the convention. The actual machinery is in
the CLI from #3.

---

## 5. Active write guardrail

### Problem

The "use a program to modify these files, instead of using AI to
directly modify them" rule from the original design is currently
**convention-only**. Nothing prevents a persona's `claude -p`
subprocess from `Edit`-ing `status.json` directly and silently
corrupting the journal.

### Mechanism

A Claude **PreToolUse hook** (configured in the persona's
`.claude/settings.json` via the existing `tigerharness init`
scaffolder) blocks `Edit` / `Write` / `NotebookEdit` calls against any
path that matches the protected glob:

```
<journal_root>/<task-id>/{status,orchestration,sessions}.json
<journal_root>/<task-id>/events.jsonl
<journal_root>/<task-id>/steps/*.md
```

(`<journal_root>` is resolved at hook time via the same `paths.py`
defaults the executor uses, plus any `TIGERHARNESS_WORKFLOW_JOURNAL`
override.)

A blocked tool call returns a deny message with the canonical
explanation:

> "Direct Edit of workflow journal files is forbidden. Use the
> `workflow-append-steps` skill (or another approved workflow skill)
> to mutate task state."

### What it does NOT block

- Reading any journal file (`Read`, `Grep`, `Glob`, `cat`) — personas
  may freely inspect the journal.
- Writing to `logs/<step>/iter-NN/` artifacts — those are persona
  scratch and not part of the truth surface.

### Scaffolding

`tigerharness init` already emits `.claude/settings.json` per team;
Phase 2 extends that template to include the hook block. Existing
teams need a one-line manual addition documented in
`docs/workflow-runner.md` under "Operating".

---

## Event additions

Three new event kinds Phase 2 emits, all spec-compliant additions to
the existing `events.jsonl` stream:

```json
{"ts":"...","kind":"compile_started","playbook":"default","task_brief_sha256":"..."}
{"ts":"...","kind":"compile_tier1_failed","iter":2,"errors":[{"validator":"ref","msg":"..."}]}
{"ts":"...","kind":"compile_critique_round","round":1,"akagi":"REVISE","ayako":"APPROVE","reasons":["..."]}
{"ts":"...","kind":"compile_completed","steps":12,"critique_iters":3}
{"ts":"...","kind":"compile_failed","tier":1,"errors":[...]}                              // alternative terminal
{"ts":"...","kind":"human_gate_requested","approvers":["operator"],"slack_thread_ts":"..."}  // Phase-3 hand-off
{"ts":"...","kind":"append_completed","added":["05-...","06-..."]}
{"ts":"...","kind":"append_rejected","tier":1,"errors":[...]}
```

Phase 1 event kinds are unchanged. The Phase-2 example block in
[`docs/workflow-runner.md`](workflow-runner.md) (currently labelled
"Phase 2+ (planned, not yet emitted)") becomes "Phase 2 (emitted)"
once this work lands.

---

## Test plan

### Unit tests (per module)

- `compile/validators.py`: each Tier-1 validator gets its own
  happy-path + failure-path + edge-case test.
- `compile/drafter.py`: prompt formatting + step-file parser; mock the
  LLM via the `SessionManager` test seam.
- `compile/critique.py`: mock the two critic personas to return scripted
  APPROVE/REVISE sequences; assert the loop terminates at the right
  iteration; assert transcript content.
- `compile/pipeline.py`: end-to-end compile against a known playbook +
  brief, asserting all artifacts written.
- `compile/append.py`: append happy/reject/append-only-invariant.

### Integration / e2e tests

- `tests/workflow_runner/e2e/compile/`: a canonical playbook + brief
  fed through the real compile pipeline (with a fake claude),
  producing a step set the executor can then run end-to-end.
- `cmd_start` with `--playbook` + `--task-brief` flows green to
  `done`.
- `cmd_start` with `--steps` (escape hatch) still flows green to
  `done` exactly as in Phase 1.
- Step-append: a workflow whose first step calls `workflow-append-steps`
  via the skill; assert the appended steps run after.

### Hook tests

- A direct `Edit` against `status.json` is rejected with the canonical
  message; the file is unchanged on disk.
- A `Read` against the same file succeeds.
- An `Edit` against `logs/.../stdout.txt` succeeds.

### Coverage floor

100% line + branch on every new module in `src/tigerharness/workflow_runner/compile/`
and on any cli.py changes — same standard as Phase 1.

---

## Roadmap and wave plan

Suggested team build, modeled on the Phase 1 wave pattern. Each wave
lands as its own branch and merges into a Phase 2 integration branch
before the next wave starts.

| Wave | Items | Personas |
|---|---|---|
| **W1** | Tier 1 validators (`compile/validators.py`) + step drafter (`compile/drafter.py`) + write-guardrail hook config | Miyagi, Mitsui, Sakuragi (one each) |
| **W2** | Tier 2 critique loop (`compile/critique.py`) + pipeline orchestration (`compile/pipeline.py`) + CLI surface evolution | Rukawa (critique loop), Sakuragi (pipeline), Mitsui (CLI) |
| **W3** | Step-append (`compile/append.py`) + `workflow-append-steps` skill + `workflow-run` skill | Rukawa (append), Miyagi (skills) |
| **W4** | End-to-end + integration tests | Haruko (e2e), Kogure (unit shoring) |
| **Integration** | Re-integrate waves; wire artifacts into cmd_start; update docs/workflow-runner.md (Phase 2+ → Phase 2 shipped) | Anzai |
| **Final review** | Chief review; SHIP-WITH-NITS or DO-NOT-SHIP verdict | Akagi |

The wave granularity is intentionally finer than Phase 1's (5 sub-steps
vs Phase 1's 6) because the compile phase has more independent
components that can land in parallel. Wave 1 has zero cross-deps;
Wave 2 needs validators (W1); Wave 3 needs the pipeline (W2).

---

## Out of scope (deferred to later phases)

- **Tier 3 human gate enforcement** → Phase 3. Phase 2 only emits the
  event so the integration point is clean.
- **Stuck-watchdog + AI-driven retry** → Phase 4. Per-step
  `timeout_sec` continues to be the only enforcement.
- **Parallel dispatch (`parallel_with` honored)** → Phase 5. Field
  continues to be parsed and ignored.
- **Cancel + resume across compile** → if compile is interrupted, the
  partially-written artifacts get cleaned up by `workflow cancel`; a
  fresh `workflow start` is required to retry (no `workflow resume
  --from-compile`).

---

## Open questions

1. **Drafter prompt source.** Should the drafter prompt (Anzai's
   instructions for converting playbook + brief into step files) live
   in source (`drafter.py`) or as a separate `.md` artifact under
   `docs/prompts/`? Recommendation: source for now; promote to .md
   artifact when it becomes long enough that diffing matters.
2. **Critic budget tracking.** Tier-2 critique loop costs LLM time +
   tokens. Should compile-phase cost roll up into
   `status.cost_usd_total` (counts against `max_cost_usd`) or be
   tracked separately? Recommendation: roll up — it's the same
   user-visible cost ceiling.
3. **Hook scope.** Should the guardrail hook be defined per-team (in
   `teams/<Team>/.claude/settings.json`) or globally (in the user's
   `~/.claude/settings.json`)? Recommendation: per-team, so the rule
   travels with the team folder.

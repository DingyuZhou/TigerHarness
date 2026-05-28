# workflow-runner

Team workflow orchestration for multi-persona tasks.

> **Status:** Phase 0 — spec / draft. No implementation yet. This
> document is the single source of truth for what we're going to
> build; it will be updated as decisions land.

## What it does

The task-runner loops *one* persona through N iterations of a single
prompt. The workflow-runner loops a *graph of personas* through a
defined playbook — plan, critique, develop, QA, doc — with explicit
approval gates, loop counters, and cost ceilings.

A *workflow* is a natural-language **playbook** authored once per
team. A *task* is a single execution of a playbook against a specific
brief, captured in its own self-contained folder under
`workflow_journal/`. The orchestrator is deterministic Python that
spawns `claude -p --resume` per persona, parses a structured trailer
from each output, and follows the playbook's compiled routing.

## Architecture

```
Trigger (skill or CLI)
    |
    v
Orchestrator (tigerharness.workflow_runner)
    |-- Compile phase: playbook.md  ->  steps/*.md  + orchestration.json
    |     (Tier 1 validators + Tier 2 critique loop + Tier 3 human gate)
    |
    |-- Execution phase: read status.json -> dispatch step ->
    |     parse persona trailer -> route on_approve/on_revise/on_block ->
    |     write events.jsonl + status.json -> loop
    |
    |-- Per-persona Claude sessions persisted in sessions.json
    |-- Per-task structured event log + per-step iteration logs
    |
    v
Result: workflow_journal/<task-id>/ (fully self-contained record)
```

The workflow-runner **sits above** the task-runner. It does *not*
replace it. Where the task-runner is the right fit (one persona, one
prompt, N iterations), use it directly.

## Folder layout

### Team-level (authored by humans)

```
teams/<Team>/workflow/
    default.md          # the default playbook
    planning.md         # planning/research-focused playbook
    mvp.md              # speed-first playbook
    quality.md          # quality-first playbook
    ...
```

Playbooks are **freestyle markdown prose**. They describe the
workflow in natural language: who does what, in what order, what
triggers iteration, when the team is done. There is **no required
schema** for playbooks — they are written for humans first. The
orchestrator compiles them into a machine-readable plan at task
start.

### Per-task (written by the orchestrator)

```
teams/<Team>/workflow_journal/<task-id>/
    task_brief.md             # the user's input, verbatim
    playbook_snapshot.md      # frozen copy of the playbook at task start
    compile_trace.txt         # dry-run trace from Tier 1 validators
    compile_critique.md       # transcript of Tier 2 critique loop
    orchestration.json        # ordered list of step ids
    status.json               # pointer + iteration counters + cost + history
    sessions.json             # {persona -> claude_session_id}
    events.jsonl              # structured event stream (machine truth)
    steps/
        01-7f2a-anzai-plan.md
        02-9c14-akagi-critique-exec.md
        03-d8e1-ayako-critique-qa.md
        ...
    logs/
        <step-id>/
            iter-01/
                prompt.txt
                stdout.txt
                stderr.txt
                meta.json     # cost, model, session_id, exit_code, timing
            iter-02/
                ...
```

`<task-id>` format: `<YYYYMMDD>-<short-slug>-<8-char-uuid>`, e.g.
`20260528-add-cache-eviction-7f2a9c14`.

## File formats

### Playbook (`teams/<Team>/workflow/<name>.md`)

Freestyle markdown. No frontmatter required. Example (extract):

```markdown
# Shohoku Default Workflow

Anzai reads the task brief and drafts a plan covering both
execution and QA. Akagi critiques the execution side; Ayako
critiques the QA side. Anzai revises until both approve. Then
Anzai assigns developers and QA from the roster. Developers
implement; QA tests in parallel where possible. Akagi reviews
both outputs; if either needs more work, that sub-team iterates.
Anzai does a final review; if anything needs change, Akagi
coordinates the fix. Once Anzai approves, Anzai writes the
project docs and updates team knowledge, critiquing and iterating
that step at least three times.
```

### Compiled step file (`steps/<NN>-<shortuuid>-<persona>-<slug>.md`)

Markdown body **with required YAML frontmatter**. Written by the
compile phase, never edited after.

```markdown
---
id: 03-d8e1-ayako-critique-qa
persona: ayako
role: qa_critic
on_approve: 04-anzai-revise-plan
on_revise: 04-anzai-revise-plan
on_block: __escalate__
max_iters: 5
timeout_sec: 1800
parallel_with: []
---

Critique Anzai's plan from a QA perspective. Are all behavior
changes covered by tests? Are edge cases identified? Are there
testing risks the plan doesn't address?

List at least 2 concrete issues, or write NO_ISSUES — but think
hard before NO_ISSUES.

End your reply with exactly one of:
    WORKFLOW: APPROVE
    WORKFLOW: REVISE: <one-line summary of what must change>
    WORKFLOW: BLOCK: <one-line summary of why we can't proceed>
```

**Frontmatter contract:**

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Globally-unique step id within this task. |
| `persona` | yes | Must exist in the team roster. |
| `role` | yes | Free-text label (`planner`, `exec_critic`, `qa_critic`, `developer`, `qa`, `doc_writer`, ...). |
| `on_approve` | yes | Next step id, or `__done__`, or `__escalate__`. |
| `on_revise` | yes | Step id to rewind to (typically the planning step). |
| `on_block` | yes | Step id, or `__escalate__` (default). |
| `max_iters` | yes | Hard cap on times this step can execute in one task. |
| `timeout_sec` | yes | Per-iteration wall-clock cap. |
| `parallel_with` | no | List of sibling step ids that may run concurrently (Phase 5 — reserved syntax, ignored in Phase 1). |

### `orchestration.json`

```json
{
    "task_id": "20260528-add-cache-eviction-7f2a9c14",
    "team": "Shohoku",
    "playbook": "default",
    "playbook_sha256": "<hex>",
    "steps": [
        "01-7f2a-anzai-plan",
        "02-9c14-akagi-critique-exec",
        "03-d8e1-ayako-critique-qa",
        "04-1abf-anzai-revise-plan",
        "05-..."
    ],
    "entrypoint": "01-7f2a-anzai-plan",
    "compiled_at": "2026-05-28T14:00:00Z",
    "compiled_by": "anzai",
    "compile_critique_iters": 3
}
```

**Mutability rule:** the `steps` array is append-only after compile.
The currently-executing step may append new step ids to the end (via
`workflow-append-steps` skill). Already-executed step ids are
immutable.

**Re-validation on append.** Every time `workflow-append-steps` is
called, Tier 1 validators (schema, ref, roster, cycle, dry-run)
re-run against the new full graph. If any validator fails, the
append is rejected, the new step files are deleted, and an
`append_rejected` event is emitted with the failure list. The
currently-executing step is notified via stderr injection and may
retry the append with a corrected payload (capped by `max_iters` on
that step).

### `status.json`

```json
{
    "task_id": "20260528-add-cache-eviction-7f2a9c14",
    "phase": "execute",
    "current_step": "03-d8e1-ayako-critique-qa",
    "current_iter": 2,
    "started_at": "2026-05-28T14:00:00Z",
    "step_started_at": "2026-05-28T14:42:11Z",
    "iter_counts": {
        "01-7f2a-anzai-plan": 2,
        "02-9c14-akagi-critique-exec": 2,
        "03-d8e1-ayako-critique-qa": 2
    },
    "cost_usd_total": 1.842,
    "cost_usd_per_step": {
        "01-7f2a-anzai-plan": 0.91,
        "02-9c14-akagi-critique-exec": 0.42,
        "03-d8e1-ayako-critique-qa": 0.51
    },
    "step_history": [
        {
            "step": "01-7f2a-anzai-plan",
            "iter": 1,
            "persona": "anzai",
            "started_at": "...",
            "ended_at": "...",
            "verdict": "APPROVE",
            "reason": null,
            "cost_usd": 0.51
        },
        {
            "step": "02-9c14-akagi-critique-exec",
            "iter": 1,
            "persona": "akagi",
            "verdict": "REVISE",
            "reason": "missing rollback plan for the cache migration",
            ...
        }
    ],
    "phase_state": {
        "compile_passed_tier1": true,
        "compile_critique_iters": 3,
        "human_gate_passed_at": "2026-05-28T14:01:42Z"
    }
}
```

`status.json` is the single source of truth for *where the task is*.
The orchestrator writes it atomically (write-temp-then-rename).

### `sessions.json`

```json
{
    "anzai": "claude-session-abc123...",
    "akagi": "claude-session-def456...",
    "ayako": "claude-session-789xyz..."
}
```

One Claude session per (task, persona). Persona memory persists
across loop iterations within the task. Different personas never
share a session.

### `events.jsonl`

One JSON object per line. The machine-truth event stream. Every
orchestrator decision, every step launch, every parsed verdict,
every cost delta, every error.

```json
{"ts":"2026-05-28T14:00:00Z","kind":"task_started","task_id":"..."}
{"ts":"...","kind":"compile_started","playbook":"default"}
{"ts":"...","kind":"compile_tier1_passed","validators":["schema","ref","roster","cycle"]}
{"ts":"...","kind":"compile_critique_iter","iter":1,"akagi_verdict":"REVISE","ayako_verdict":"APPROVE"}
{"ts":"...","kind":"compile_completed","steps":12,"critique_iters":3}
{"ts":"...","kind":"human_gate_requested","slack_thread_ts":"..."}
{"ts":"...","kind":"human_gate_passed","by":"ceo"}
{"ts":"...","kind":"step_started","step":"01-7f2a-anzai-plan","iter":1,"persona":"anzai"}
{"ts":"...","kind":"step_completed","step":"01-...","iter":1,"verdict":"APPROVE","cost_usd":0.51,"duration_sec":312}
{"ts":"...","kind":"step_rewind","from":"03-...","to":"01-...","reason":"REVISE: missing rollback plan"}
{"ts":"...","kind":"constraint_breach","kind_detail":"max_loop_iters","step":"02-...","value":5}
{"ts":"...","kind":"escalated","reason":"max_loop_iters exceeded","slack_thread_ts":"..."}
{"ts":"...","kind":"task_completed","verdict":"done","cost_usd_total":3.14}
```

A `tigerharness workflow tail <task-id>` CLI renders this
human-friendly (similar to `task_runner logs`).

## Persona response trailer protocol

Every step's prompt ends with:

```
End your reply with exactly one of:
    WORKFLOW: APPROVE
    WORKFLOW: REVISE: <one-line summary of what must change>
    WORKFLOW: BLOCK: <one-line summary of why we can't proceed>
```

**Parser rules:**

1. Scan the last 20 non-empty lines of the persona's stdout.
2. The trailer is the *last* line matching `^WORKFLOW: (APPROVE|REVISE|BLOCK)( *: *.+)?$`.
3. On match: record verdict + reason in `status.step_history`, route.
4. On no match: re-prompt **once** with `"I couldn't find your
   WORKFLOW: trailer. Please end your next reply with one of
   WORKFLOW: APPROVE / REVISE / BLOCK."`
5. Still no match after the second attempt: emit
   `verdict_parse_failed` event and route as `on_block` →
   `__escalate__`.

**Optional `REVISE` target override.** For critic steps that review
work from multiple prior steps, the `REVISE` reason may carry an
explicit rewind target:

```
WORKFLOW: REVISE: target=06-3f1a-rukawa-implement-cache: missing rollback
```

The orchestrator parses `target=<step-id>:` off the front of the
reason and rewinds to that id instead of the frontmatter
`on_revise`. The target must exist in `orchestration.json`; unknown
target → treated as a parse failure and re-prompted once.

## Loop semantics — rewind

When a step's verdict routes to a *previously executed* step id, the
orchestrator performs a **pointer rewind**, not a step rewrite:

1. Write a new entry to `status.step_history` with the verdict.
2. Set `current_step` to the target step.
3. Increment `current_iter` for that target.
4. Inject a feedback prologue into the target's *next prompt only*
   (does **not** mutate the step file):

   ```
   [Iteration <N> — previous attempts produced this feedback:]
   <verbatim REVISE reasons from the rewind chain, oldest first>
   ```

5. Re-run the target step in its existing Claude session (the
   persona remembers iter N−1 from session memory; the prologue
   reinforces the latest feedback).
6. If `current_iter > max_iters` for that step, route to
   `on_block` → `__escalate__`.

The step file itself is **never mutated** after compile. All
iteration state lives in `status.json` + `logs/<step>/iter-NN/`.

## Hardening AI-generated artifacts — the compile phase

The compile step (playbook prose → concrete step files) is a single
point of failure. Three layers of defense:

### Tier 1 — mechanical validators (always on, cannot be skipped)

Run after the AI produces the draft, before it's accepted:

| Validator | Rejects if |
|---|---|
| `schema` | Any step missing required frontmatter field. |
| `ref` | Any `on_approve`/`on_revise`/`on_block` points to a non-existent step id or unknown sentinel. |
| `roster` | Any `persona` not in the team's persona registry. |
| `cycle` | Any cycle in the step graph lacks a `max_iters` cap. |
| `dry_run` | Static graph walk produces a divergent or unbounded trace. |

Output: `compile_trace.txt` with the happy-path and revise-path
traces, plus pass/fail per validator.

Any failure → reject the draft, append the failure list to the
critique transcript, request another revision.

### Tier 2 — forced AI critique loop (minimum 3 iterations)

Even if Tier 1 passes on the first try, the critique loop runs at
least 3 times:

1. Anzai compiles → draft v1.
2. Akagi critiques execution viability (`role: exec_critic`).
3. Ayako critiques QA viability (`role: qa_critic`).
4. Anzai revises → draft v2.
5. Repeat until **both critics APPROVE** *and* `iter >= 3`.

The critic prompt is templated to enforce specificity:

> "List at least 2 concrete issues with this compiled plan, or
> write NO_ISSUES — but think hard before saying NO_ISSUES. Then
> end with WORKFLOW: APPROVE or WORKFLOW: REVISE: <summary>."

Critic personas **must differ** from the compiler persona. (Shohoku's
roster already enforces this.) The transcript is saved to
`compile_critique.md`.

Hard cap on critique iterations: `max_compile_iters` (default 8). On
breach: emit `compile_failed` event and route to human gate even if
`human_gate=false` was configured.

### Tier 3 — human gate (default ON for v1)

After Tier 1+2 pass, the orchestrator slack-notifies the Operator
with:

- Compiled step list (ids + roles, no prompt bodies)
- Dry-run trace summary
- One-line ACK request

The Operator replies `WORKFLOW: APPROVE` in thread → execution starts.
Anything else → orchestrator pauses; the task sits idle until
re-triggered or cancelled.

**Approver allowlist.** Only Slack user ids listed in the team's
`configs/workflow.yaml` under `human_gate_approvers` are accepted as
human-gate approvers. Replies from any other user in the thread are
logged as `human_gate_unauthorized_attempt` events and ignored. The
allowlist is mandatory (compile fails if it is empty when
`human_gate: true`).

Configurable per-workflow via the playbook header (optional):

```markdown
<!--
workflow_config:
  human_gate: true   # default; set to false to skip Tier 3
  max_compile_iters: 8
-->
```

The `compile_and_validate(...)` primitive is reusable: any step that
produces a structured artifact downstream code depends on (e.g.,
the final docs step) can opt into the same three tiers.

## Constraints

Set at the playbook level via the optional `workflow_config` HTML
comment, overridable per-step via frontmatter. Hard kills on breach;
no silent retries.

| Knob | Default | Where set | On breach |
|---|---|---|---|
| `max_cost_usd` | `10.0` | playbook | `escalated`, task paused, slack Operator |
| `max_loop_iters` | `5` per step | playbook | `escalated`, route `on_block` |
| `step_timeout_sec` | `1800` | playbook / per step | kill iteration, route `on_block` |
| `max_compile_iters` | `8` | playbook | escalate to human gate |
| `max_task_wall_sec` | `86400` | playbook | escalate |

Cost is parsed from `claude -p --output-format json`'s
`total_cost_usd` field per iteration and accumulated in
`status.cost_usd_total`.

## Parallelism (reserved for Phase 5)

`parallel_with: [step-id, step-id]` in step frontmatter marks a set
of siblings that *may* run concurrently. In Phase 1–4 this field is
parsed and stored but ignored — steps run strictly sequentially.
Phase 5 will implement the parallel dispatcher (process pool, joint
verdict aggregation).

A playbook can opt into parallel execution globally:

```html
<!--
workflow_config:
  allow_parallel: true   # default false in v1
-->
```

### How the `workflow_config` block is parsed

The orchestrator scans the playbook for HTML comment blocks of the
form:

```
<!--
workflow_config:
  <yaml-key>: <yaml-value>
  ...
-->
```

The block body (everything between `<!--` and `-->`, after the
`workflow_config:` header line) is parsed as YAML. Only one such
block per playbook; if multiple are present, the orchestrator
rejects the playbook and emits a `playbook_config_invalid` event.

## Trigger — the `workflow-run` skill

Primary entry point. Lives at
`teams/<Team>/.claude/skills/workflow-run/SKILL.md`. Triggers on
phrases like:

- "ask Shohoku to use the default workflow on this task"
- "run the planning workflow for X"
- "kick off Shohoku quality workflow"

The skill is a thin wrapper around the CLI:

```bash
tigerharness workflow start \
    --team Shohoku \
    --playbook default \
    --task-brief "<verbatim user request>" \
    --thread <slack_thread_ts>   # if invoked from Slack
```

Other skill commands:

```bash
tigerharness workflow list                  # list active tasks
tigerharness workflow show <task-id>        # current status
tigerharness workflow tail <task-id> -f     # follow events.jsonl
tigerharness workflow cancel <task-id>      # graceful stop
tigerharness workflow resume <task-id>      # after a pause
tigerharness workflow approve <task-id>     # pass the human gate
```

## Sweep & diagnose — caring for in-flight tasks

A workflow task can stall for many reasons: a persona's claude
subprocess died, a step's `step_timeout_sec` hasn't been reached but
the persona is genuinely stuck, the Operator hasn't acted on a human
gate, a host reboot left an orphaned lock. The team needs AI-callable
surfaces to *notice* and *act* on these.

### `workflow-sweep` skill

Lists all in-flight workflow tasks the calling agent can see and
flags anomalies. Backed by CLI:

```bash
workflow sweep                       # list in-flight tasks
workflow sweep --team Shohoku        # restrict to one team
workflow sweep --stale-after 1h      # mark tasks with no events in 1h as STALE
workflow sweep --status running      # filter by status
workflow sweep --auto-diagnose       # run diagnose on each STALE task
workflow sweep --auto-resume         # attempt resume on each STALE task (uses lock)
```

For each task, output includes: `task-id`, team, playbook, current
step + iter, wall age, time-since-last-event, total cost, and a
status flag (`RUNNING` / `STALE` / `BLOCKED` / `WAITING_HUMAN_GATE`
/ `ESCALATED`).

Implementation: walk `teams/*/workflow_journal/*/status.json` (and
the alternative state-dir if we end up there per Open Question 1).
No claude calls; pure filesystem read. Cheap enough to cron.

### `workflow-diagnose` skill

For a single task: reads `events.jsonl`, the tail of the current
step's `logs/<step>/iter-NN/{stdout,stderr,meta}`, and `status.json`.
Produces a one-page summary and a recommended action. Backed by CLI:

```bash
workflow diagnose <task-id-or-prefix>           # human-readable summary
workflow diagnose <task-id-or-prefix> --json    # structured output for AI consumption
```

Output structure (when `--json`):

```json
{
    "task_id": "...",
    "phase": "execute",
    "current_step": "06-...-rukawa-implement-cache",
    "current_iter": 2,
    "wall_age_sec": 7421,
    "since_last_event_sec": 3611,
    "last_event": {"kind": "step_started", "ts": "..."},
    "diagnosis": "process_dead_no_completion",
    "details": "Step started 1h ago; no step_completed or step_failed event; no claude pid visible in process tree; stale lock from pid 12345.",
    "recommended_action": "resume",
    "recommended_command": "tigerharness workflow resume 20260528-7f2a9c14"
}
```

The `diagnosis` field is a small enum that any caller can branch on:

| Diagnosis | Meaning | Typical action |
|---|---|---|
| `healthy_running` | Recent activity, no anomaly | none |
| `waiting_human_gate` | Tier 3 paused awaiting Operator | notify Operator |
| `process_dead_no_completion` | Orchestrator died mid-iter | `resume` |
| `persona_stuck_no_output` | Claude subprocess alive but silent past `step_timeout_sec` | escalate (uses task-runner stuck heuristic) |
| `constraint_breached` | Hit `max_cost_usd` / `max_loop_iters` / `max_task_wall_sec` | escalate |
| `parse_failure_loop` | Repeated trailer-parse failures | escalate |
| `unknown` | Heuristic can't classify | escalate to Operator |

The diagnose CLI is pure-Python (no claude calls in the default
path). An optional `--llm-fallback` flag invokes a small Claude call
*only* when the diagnosis is `unknown`, mirroring the task-runner's
stuck-watchdog two-stage hybrid.

### Watchdog integration (Phase 4)

Each workflow iteration runs under a per-step watchdog (reusing
`tigerharness.task_runner.stuck_watchdog`):

- Initial wait = step's `timeout_sec`.
- On STUCK verdict: SIGTERM the claude subtree, emit
  `iter_stuck` event, route via `on_block`.
- The diagnose CLI's `persona_stuck_no_output` branch shells out to
  the same heuristic so the human-facing diagnosis matches the
  automated watchdog's logic.

### Scheduled sweep (Phase 4–5)

A cron / systemd-timer trigger calls `workflow sweep --auto-diagnose`
every hour. Each STALE task with a non-`healthy_running` diagnosis
posts a one-liner to the team's `SLACK_NOTIFY_CHANNEL` (the same
channel slack-notify uses). The Operator wakes up to a short list:

```
[Shohoku] workflow sweep 09:00
  20260528-7f2a..  RUNNING  diagnose=healthy_running                 - no action
  20260527-4c1e..  STALE    diagnose=process_dead_no_completion      - resume?
  20260526-aa37..  BLOCKED  diagnose=waiting_human_gate              - approve?
  20260525-9f02..  ESCALATED diagnose=constraint_breached(max_cost)  - investigate
```

The cron driver is opt-in per team (configured via systemd timer);
no team gets it unless explicitly enabled.

## File-write skills (used by the orchestrator and by personas)

Personas never edit `orchestration.json`, `status.json`,
`events.jsonl`, or compiled step files directly. They call skills:

- `workflow-append-steps` — append new step files after the current
  step (used during planning). Validates frontmatter, updates
  `orchestration.json` atomically.
- `workflow-emit-trailer` — convenience; appends a parseable
  `WORKFLOW: ...` line. (Optional; personas can write the trailer by
  hand.)

The orchestrator owns all status / event writes. Tightening this
with hook-based blocks on `Edit` against status/event paths is a
TODO for Phase 1.

## CLI surface (`tigerharness workflow ...`)

```
workflow start    --team <T> --playbook <name> [--task-brief <text>|--brief-file <p>] [--thread <ts>]
workflow list     [--team <T>] [--status <s>]
workflow show     <task-id-or-prefix>
workflow tail     <task-id-or-prefix> [-f]
workflow cancel   <task-id-or-prefix>
workflow resume   <task-id-or-prefix>
workflow approve  <task-id-or-prefix>     # passes human gate
workflow validate <playbook-path>         # standalone Tier 1 dry-run
workflow sweep    [--team <T>] [--stale-after DUR] [--auto-diagnose] [--auto-resume]
workflow diagnose <task-id-or-prefix> [--json] [--llm-fallback]
```

`workflow validate` lets a playbook author dry-run their prose
against the validator suite before letting it loose on a real task.
`workflow sweep` and `workflow diagnose` back the eponymous skills
(see "Sweep & diagnose" above).

## Relationship to existing modules

| Module | Relationship |
|---|---|
| `task_runner` | The workflow-runner is a layer above. Each step is conceptually one task-runner job (one persona, prompt, single iteration per step turn). We may reuse `JobStore` / session-spawn code paths. |
| `slack_bridge` | The orchestrator slack-notifies via the existing bridge (DM threads, ops-log dual-post). |
| `tiger_memory` | Personas use their own memory unchanged. The workflow-runner does not write to persona memory. |

## Phasing

| Phase | Scope |
|---|---|
| **0** | This spec + Shohoku `default.md` playbook + ADR. **Current.** |
| **1** | Sequential executor: step files, `status.json`, per-persona sessions, file-write helpers, `events.jsonl`, persona-trailer parser, basic CLI (`start`/`show`/`list`/`tail`/`cancel`). **No compile phase yet — Phase 1 accepts pre-compiled step files only.** |
| **2** | Compile phase: prose-to-steps via Anzai + Tier 1 validators + `compile_and_validate` primitive. Loop rewind + feedback injection. |
| **3** | Tier 2 critique loop + Tier 3 human gate + `workflow approve` CLI. Constraint enforcement (cost / iters / timeouts). Escalation via slack-notify. |
| **4** | `workflow-run` skill + integration with Shohoku's default playbook end-to-end. Stuck-watchdog reuse from task-runner. `workflow sweep` + `workflow diagnose` CLIs + matching skills. |
| **5** | `parallel_with` implementation. Scheduled `workflow sweep` (cron / systemd timer). Optional. |

Each phase ends with a working demo and ≥95% line coverage on the
new code (the project's coverage floor applies).

## Cancel / resume / concurrency

- **Cancel.** `workflow cancel <task-id>` sets `phase=cancelling` in
  `status.json` and SIGTERMs the current iteration's claude
  subprocess. The orchestrator finalizes a `task_cancelled` event
  and exits. State on disk is left intact for inspection.
- **Resume.** `workflow resume <task-id>` reads `status.json` and
  dispatches the `current_step` at `current_iter`. If the most
  recent event for that (step, iter) is `step_started` with no
  matching `step_completed`, the iteration is re-run from scratch
  (the persona's session memory carries the partial work, so the
  cost is bounded).
- **Concurrent runs (same task-id).** The orchestrator acquires an
  exclusive file lock on `workflow_journal/<task-id>/.lock`
  (POSIX `flock`). A second `workflow start`/`resume` against the
  same task-id refuses with a clear error. Stale locks (orphaned by
  a hard kill) are detected by a writer-pid heartbeat in the lock
  file; > 5 minutes without heartbeat → safe to take over.

## Non-goals (for now)

- A general DAG editor / GUI. Playbooks are markdown; tools are CLIs.
- Dynamic team membership during a task. The team roster is fixed
  at compile time.
- Cross-team workflows. One task = one team.
- Resumable mid-iteration (rather than mid-task). If an iteration
  is killed mid-flight, the entire iteration re-runs.

## Open questions

1. **Per-task workflow_journal location.** Current proposal puts it
   under `teams/<Team>/workflow_journal/`. Alternative: under the
   tigerharness state dir
   (`~/.local/state/tigerharness-workflows/<task-id>/`). Team folder
   wins on discoverability; state dir wins on cleanliness. Lean
   team-folder; will confirm before Phase 1.
2. **Hook-based protection** of status/event files against persona
   edits. Cleaner than prompt-level guards but requires
   per-persona `settings.json` plumbing. TODO Phase 1.
3. **Tier 3 default.** Default ON until we've watched ~10 clean
   compiles, then revisit.
4. **What counts as a "playbook"** for the trigger skill matcher.
   Recommend: any `*.md` under `teams/<Team>/workflow/` that is not
   a README. Will confirm in Phase 4.
5. **Log retention.** `events.jsonl` and per-iter full captures can
   grow large on long tasks. Default: keep everything. Future:
   per-team retention policy (e.g., gzip iter logs older than 7
   days, archive completed tasks after 30 days). Phase 1+ TODO.
6. **Cost data source.** Plan A: parse `total_cost_usd` from
   `claude -p --output-format json`. Plan B: reuse whatever the
   task-runner uses (verify in Phase 1). Either way, settled before
   Phase 3 (when cost ceilings actually enforce).

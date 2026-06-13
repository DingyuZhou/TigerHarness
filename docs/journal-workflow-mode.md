# journal — workflow mode (Phase 1.5)

## At a glance
- **What:** how a `kind=workflow` task is compiled in-session (drafter +
  two critics over Tier-1 validators) and then walked step-by-step through
  gates that enforce order and write a persona-stamped note per step.
- **When you need it:** authoring/driving multi-persona workflows, the compile
  sub-protocol, or the graph edge/routing semantics.
- **Must-not-miss:** follow `journal step-done`'s next-step output — don't
  hand-follow edges (the gate is what records each persona's memory).

## Details

Extending the file-based subscription backend to support multi-persona
workflow tasks (`kind=workflow`). Single-persona task mode
(`kind=task`) shipped in Phase 1 (PR #25, commit `7d6b9f8` on main).

> **Status:** Phase 1.5 + Phase 2 + Phase 3 shipped on
> `work/2026-06-03-journal-closeout-phase2-phase3`:
>
> - `abf4cdb` Phase 1.5 closeout -- skills, doc reconciliation,
>   scripted compile driver, retire human-gate
> - `8ab2f30` Phase 2 model layer -- `playbook_name` on Status
> - `ba90ba7` Phase 2 -- `journal compile-retry` CLI
> - `31ebbe6` Phase 2 -- configurable compile-time persona roster
> - `fc2d2a0` Phase 3 -- `journal append-steps` + skill + OPERATING.md
>
> What this branch closes out: 100% line + branch coverage on the
> journal package; an end-to-end scripted compile driver suite; CLI
> surface = 10 commands (`new`, `compile-context`, `compile-prompts`,
> `validate-graph`, `land-compile`, `compile-fail`, `compile-retry`,
> `append-steps`, `abort`, `validate-personas`) +
> `list`/`status`/`sweep`. Internally referenced "Phase 3 human-gate
> enforcement" has been **retired** -- see the *Out of scope* +
> *Phasing* sections below for the structural reasoning.

## Why this exists

Phase 1's `drive-journal` skill drives a *single persona* through a
free-form PRD — the single-persona iterative workload. The Operator's question
on 2026-06-03: *how do I trigger workflow-runner-style work through
this backend?*

Answer today: you can't. `Status.from_dict` rejects `kind=workflow` so
neither the scaffolder nor the driver will touch one. The api-backed
`workflow-runner` is still available, but its `claude -p` runtime
moves to token billing in mid-June 2026, which defeats the whole
point of the subscription backend.

Phase 1.5 closes that gap: multi-persona workflow work driven by the
interactive Claude Code app, using the same `journal/` folder, the
same `drive-journal` skill, the same lazy sweep, and an extended
`status.json` schema. The compile pipeline that turns a playbook +
brief into a step graph is **reused from Wave 2** (the modules under
the compile core, now `journal.wfcore`); the new code is the journal-side glue
and the protocol additions that teach the interactive session to
walk the graph one persona at a time.

## Operator decisions already locked in (2026-06-03)

The four questions raised in the v1 of this doc were answered:

1. **Compile billing — Option C.** Defer compile to the first
   `drive-journal` invocation; run the entire compile pipeline as the
   interactive session's own reasoning. Zero api billing. "Plenty of
   time for doing it right."
2. **Playbooks — user-defined.** Going forward, playbooks are written
   by users / Operators per team, not pre-shipped. Shohoku's existing
   `workflow/default.md` is the development example.
3. **`status.persona` for `kind=workflow` — captain.** Store the
   captain (the accountable owner) for display in `journal list`
   even though per-step personas come from the graph.
4. **Missing persona at a step — error.** No silent default. Validate
   at scaffold time, re-validate at drive time, fail hard.

This doc is rewritten against those four decisions.

## The compile pipeline (Option C)

Phase 1.5 ships Option C: **compile is deferred to the first drive**.
Scaffold writes only the brief, a frozen playbook snapshot, and a
`status.json` with `compile_pending=true`. The first `drive-journal`
invocation runs the entire compile pipeline (Anzai drafter + Tier 1
validators + Akagi/Ayako Tier 2 critique loop) *as the interactive
session's own reasoning*, lands the orchestration + steps atomically,
flips `compile_pending=false`, and continues into the normal graph
walk — all inside one cascade. Zero api billing; full Phase 1
test/coverage discipline preserved.

### Why Option C over A and B

Option A bills the api for compile (a recurring cost on every workflow
task). Option B runs compile inside the scaffold CLI (no api billing,
but turns scaffold from a 1-second fileop into a multi-minute LLM
loop and re-introduces api-billing-shaped reasoning inside a CLI
process the user expected to be cheap). Option C ships A's
correctness with B's billing shape AND keeps scaffold cheap: scaffold
writes a few files and returns; the first drive picks up the compile
work as part of its session budget. Recoverable, cascading, journaled.

### Reused verbatim from Wave 2

The compile machinery already on main is reused as-is:

- `journal.wfcore.validators.validate_compile_output` — pure
  Python, no LLM, called by the session via a new `journal
  validate-graph` CLI shim.
- `journal.wfcore.pipeline._build_orchestration` — builds
  the `Orchestration` object from a parsed `StepFrontmatter` list.
  Called by the new `journal land-compile` CLI.
- `journal.wfcore.drafter._build_prompt` and the
  `critique.{AKAGI,AYAKO}_CRITIC_PROMPT_TEMPLATE` constants — the
  prompt text is the contract. A new `journal compile-prompts` CLI
  prints the assembled prompts on demand from the same Python
  constants, so the in-session compile is textually identical to
  what the api-billed pipeline would emit.
- `journal.wfcore.models.{Orchestration, StepFrontmatter}` and the
  `WORKFLOW: APPROVE/REVISE/BLOCK` trailer protocol — shared with
  Phase 1's graph-walk verbatim.

What the session does NOT reuse: `SessionManager`, `claude -p`,
`run_critique_loop`'s subprocess plumbing, `compile_playbook`'s
top-level orchestration. Those are replaced by the session's own
turn-by-turn reasoning, driven by OPERATING.md prose.

## Architecture

```
You: write a brief + pick a playbook
    |
    v
Scaffolder (CLI `journal new --kind workflow --playbook X --task-brief Y`)
    |-- validates personas at scaffold time (hard error if missing):
    |     Anzai, Akagi, Ayako must exist in teams/<team>/personas/
    |     plus every persona named in the playbook source
    |-- writes:
    |     active/<task-id>/task_brief.md
    |     active/<task-id>/playbook_snapshot.md
    |     active/<task-id>/status.json   (kind=workflow, state=pending,
    |                                     compile_pending=true,
    |                                     compile_phase="pending")
    |-- DOES NOT call any LLM. Returns in <1s.
    v
journal/ (passive file-based state machine)
    ^
    |
    v
Driver (drive-journal skill, interactive session)
  1. Lazy sweep (unchanged)
  2. Pick an actionable task -- classify by `kind` AND `compile_pending`:
       - kind=task: Phase 1 flow.
       - kind=workflow, compile_pending=true: COMPILE SUB-PROTOCOL (below).
       - kind=workflow, compile_pending=false: graph-walk, advanced ONE
         step at a time via the `tigerharness journal step-done --task
         <id> --step <id> --verdict <V> --output <note>` gate. The gate
         writes that step's persona-attributed `worklog/` entry, routes
         along the verdict's edge, and prints the next step -- the driver
         does NOT follow edges by hand. (Per-persona memory; see below.)
  3. Cascade after each task lands.
```

## The compile sub-protocol

When the driver picks a workflow task with `compile_pending=true`, it
branches into OPERATING.md's `## Compile sub-protocol` section
*before* any graph walk. The procedure:

1. **Atomic pickup** — done by the outer protocol's
   `tigerharness journal claim <task-id>` (the same pickup `kind=task`
   uses): it flips `state=pending → in_progress`, sets the `session_ref`
   attach token (the soft lease), and bumps `sessions`. It does **not**
   set an intermediate `compile_phase`. In the shipped backend
   `compile_phase` stays `pending` throughout the compile and is only
   advanced to `complete` by `land-compile` (or `failed` by
   `compile-fail`); the intermediate values
   (`drafting`/`tier1_pre`/`critiquing`/`tier1_post`) are **reserved and
   not written by any code path today**, so a rescuing session's resume
   signal is the `compile/round-NN-*.md` files, not `compile_phase`.
2. **Bootstrap.** Shell to `tigerharness journal compile-context
   <task-id>` to print playbook + brief + roster + the drafter
   prompt. One-shot context load — no path-fiddling inside the
   session.
3. **Drafter turn (Anzai).** Adopt Anzai per the uniform persona
   adoption preamble (see "Persona switching" below), draft the step
   bundle and save it to `compile/round-NN-draft.md`. Emit the
   `WORKFLOW:` trailer; bump heartbeat.
4. **Tier 1 gate.** Shell to `tigerharness journal validate-graph
   --task <id> --draft compile/round-NN-draft.md`. Exits 0 with the dry-run
   trace on stdout, or 1 with a JSON error list. On error, return to
   step 3 with the errors as drafter feedback. Consecutive Tier 1
   failures count against `max_compile_iters` (default 8);
   exhaustion routes to failure (see below).
5. **Critique round.** In fixed order **Akagi-then-Ayako** (preserves
   the verdict independence `critique.py` gets via subprocess
   parallelism in the api pipeline), the session adopts each critic
   in turn, reads the critic prompt via `compile-prompts --kind
   {akagi,ayako}`, emits a `WORKFLOW: APPROVE` or `WORKFLOW: REVISE:
   <reasons>` trailer. Each critic turn is saved to
   `compile/round-NN-akagi.md` / `compile/round-NN-ayako.md`; bump
   heartbeat. *These per-turn files are the recovery primitive.*
6. **Loop.** If both APPROVE *and* `round_num >= 3`, advance to
   step 7 (land). If both APPROVE but `round_num < 3`, force another
   round (matches `critique.py`'s hard-floor invariant). Otherwise
   merge REVISE feedback and return to step 3.
7. **Land.** Shell to `tigerharness journal land-compile --task <id>
   --draft compile/round-NN-draft.md --transcript compile/transcript.md
   --rounds <N>`. The CLI re-runs Tier 1 as defence-in-depth, builds
   the `Orchestration` object, writes step files +
   `orchestration.json` + `compile_critique.md` to
   `compile/final/`, atomically renames them into place, and flips
   `compile_pending=false`, `compile_phase="complete"` in
   `status.json` last (single transaction; `status.json` is the
   visibility gate).
8. **Continue.** The same `drive-journal` invocation falls through
   into the normal graph-walk protocol with `current_node =
   entrypoint`. Compile and the first graph steps share the session
   budget.

The session bumps the heartbeat at every named checkpoint (pickup,
each drafter turn, each Tier 1 call, each critic verdict, each round
completion, each land step). ~5–6 bumps per round, comfortably under
the 30-minute `stuck_timeout`.

## State machine

The four-state top-level machine (`pending / in_progress / blocked /
done`) is **unchanged**. Compile is a *sub-phase of `in_progress`*,
not a fifth top-level state. The sweep classifier is identical for
compile-phase and graph-walk-phase workflows: an `in_progress` task is
**idle** (detached `session_ref` → resumable now), **busy** (attached +
fresh heartbeat → a live session owns it), or **crashed** (attached +
stale heartbeat → reclaimable). See
[`journal-instant-resume.md`](journal-instant-resume.md).

A new `compile_phase: str` field on `status.json` tracks the
sub-phase (required for `kind=workflow`, rejected for `kind=task`):

| Value | Meaning |
|---|---|
| `pending` | Scaffolded **and throughout the compile** — the shipped backend leaves `compile_phase=pending` until landing. |
| `drafting` | *Reserved — not written by any current code path.* |
| `tier1_pre` | *Reserved — not written by any current code path.* |
| `critiquing` | *Reserved — not written by any current code path.* |
| `tier1_post` | *Reserved — not written by any current code path.* |
| `complete` | Compile landed (`land-compile`); `orchestration.json` is trustworthy. |
| `failed` | Compile gave up (`compile-fail`); paired with `state=blocked`. |

`Status.from_dict` enforces: `kind=workflow` requires `compile_phase`
present and one of the seven values; `kind=task` rejects the field
(preserves the strict-unknown-keys invariant). In the shipped backend
only `pending` → `complete`/`failed` are ever written (the four
intermediate values are reserved; an interrupted compile resumes by
reading the highest `compile/round-NN-*.md` on disk, not a phase value).
The graph-walker only reads `orchestration.json` when
`compile_phase=="complete"`; any other value means the file may be
absent or partial.

## Compile workspace on disk

During an in-flight compile, all scratch state lives under
`active/<task-id>/compile/`. The driver protocol's
`orchestration.json` and `steps/` paths are invariant during compile
— they only exist post-landing.

```
active/<task-id>/
  task_brief.md           # written at scaffold
  playbook_snapshot.md    # written at scaffold
  status.json             # state=in_progress, compile_phase=pending, ...
  progress.md             # narrative log (one entry on land/failure); NOT
                          #   ingested by tiger-memory -- the worklog is
  worklog/                # per-persona memory records (the INGESTED record)
    0001-anzai-compile-draft.md   # NNNN-<persona>-<step>.md, persona+step
    ...                           #   slugified. land-compile writes one per
                                  #   compile round/role (step=compile-<token>);
                                  #   step-done writes one per graph-walk step.
                                  #   tiger-memory's journal_worklog reads these

  compile/                # IN-FLIGHT scratch -- driver never reads it post-land
    round-01-draft.md     # drafter bundle for round 1 (StepFrontmatter list)
    round-01-akagi.md     # Akagi critic turn for round 1
    round-01-ayako.md     # Ayako critic turn for round 1
    round-02-draft.md     # round 2 ...
    ...                   # (per-turn markdown; highest round = latest)
    transcript.md         # human-readable running transcript
    final/                # staging area built just before land
      orchestration.json
      steps/<id>.md
    FAILED.md             # ONLY if compile failed; reason + last verdicts

  # post-land (driver-readable):
  orchestration.json      # the compiled graph
  steps/<id>.md           # per-step bodies
  compile_critique.md     # rendered final transcript (kept forever)
```

> **Per-persona memory.** `land-compile` normalises the saved
> `compile/round-NN-*.md` files into per-persona `worklog/` entries (one
> per round, attributed via the `compile_personas` role→persona map), and
> each graph-walk step writes its own `worklog/` entry via `journal
> step-done`. tiger-memory ingests **only** `worklog/`, never
> `progress.md`. See
> [`per-persona-journal-memory.md`](per-persona-journal-memory.md).

`compile/` is preserved verbatim on archival to `done/<task-id>/`
(small forensic value; <500KB worst-case). The per-round files are
the recovery primitive: highest-numbered file = last completed round;
round N+1 is where a rescuing session resumes.

## Persona switching (uniform mechanic)

One mechanic for both compile and graph-walk: each "turn" is a
self-contained role engagement where the session writes a four-line
preamble before doing role work and emits a `WORKFLOW:` trailer
after. The preamble:

```
PERSONA: <name>
ROLE: <role from the playbook or step bundle>
STEP: <step-id, or "compile-draft" / "compile-akagi" / "compile-ayako">
OBJECTIVE: <one sentence on what this turn must produce>
```

This is the exact preamble the shipped `OPERATING.md` (from
`journal/operating_template.py`) instructs the session to write; the
persona's prompt body (read from
`teams/<team>/personas/<name>/prompt.md`) follows the preamble.
Compile turns also reference a critic-lens prompt (Akagi or Ayako
critic prompt from `journal.wfcore.critique`); graph-walk step
turns reference the step body file. The audit-trail format is identical
across compile and graph-walk phases.

The compile-time critic prompts are pulled by the session via
`tigerharness journal compile-prompts --kind {drafter,akagi,ayako}`,
which prints the canonical text from `journal.wfcore.{drafter,
critique}` with playbook + brief + roster + feedback interpolated.
This is the single source of truth: no prompt text is embedded in
SKILL.md or OPERATING.md, and no separate "synced" copy of the
prompts exists on disk to drift.

There is no "consult" vs "adopt" distinction: every persona
engagement is a full adoption with preamble + trailer. This matches
the api pipeline's structural symmetry (every drafter/critic call is
a full claude-p invocation with the persona's prompt prepended).

## Failure modes and recovery

**Mid-compile clean stop / human-stop / context exhaustion.** The driver
`release`s the task, clearing `session_ref` → the task is **idle** and
the next `drive-journal` resumes it **immediately**, no wait.
**Mid-compile crash** (no `release`): the task stays attached, its
heartbeat ages out past `stuck_timeout`, and a later sweep classifies it
**crashed** → reclaimable. Either way recovery is to next-round
granularity, and the resume signal is the highest-numbered
`compile/round-NN-*.md` files (not `compile_phase`, which stays
`pending` mid-compile). Per-phase resume rules:

| `compile_phase` | Recovery action |
|---|---|
| `drafting` | Re-run drafter; the latest `round-NN-draft.md` is the prior attempt (may not exist on first interrupt). |
| `tier1_pre` | Read the latest `round-NN-draft.md`; run `validate-graph`; continue. |
| `critiquing` | Read the latest `round-NN-draft.md` + the highest round's `round-NN-akagi.md`/`round-NN-ayako.md`; resume at round N+1. The in-flight round (if any partial verdicts exist) is restarted from the drafter turn — critic verdicts are stateless given the same draft. |
| `tier1_post` | Read the latest `round-NN-draft.md`; run final `validate-graph` + `land-compile`. |

The hard floor (`rounds >= 3`) still applies after recovery: a
rescuing session that finds 2 completed rounds runs at least one
more.

**Tier 1 exhaustion** (drafter produced `max_compile_iters`
consecutively invalid graphs): `state=blocked`,
`compile_phase=failed`, `next_action="compile failed at tier1_pre:
<last error list>"`. The `compile/` workspace is preserved. A
consecutive-failures counter resets on success: a drafter that fails
twice then succeeds is fine.

**Tier 2 critique exhaustion** (rounds reach `max_compile_iters`
without dual-APPROVE): `state=blocked`, `compile_phase=failed`,
`next_action="compile failed at critiquing: <last round verdicts>"`.
Transcript and per-round files preserved.

**Playbook-roster typo** (user names a persona in the playbook that
doesn't exist in `personas.yaml`): caught at scaffold time by a
best-effort regex check. If it slips past scaffold and surfaces as a
Tier 1 `roster` error on round 1, the session **short-circuits** to
`compile_phase=failed` rather than burning the full Tier 1 retry
budget on an unfixable input error. Detection: if the offending
persona name is found verbatim in the playbook text, it's a
user-input typo, not drafter hallucination.

**Missing required persona at scaffold time.** Hard error from
`tigerharness journal new --kind workflow`. The scaffolder validates
(a) `teams/<team>/personas/{Anzai,Akagi,Ayako}/prompt.md` all exist
and are non-empty, (b) every persona regex-extracted from the
playbook exists, (c) `personas.yaml` parses. Failure exits with
code 2 and `MissingPersonaError: workflow compile requires personas
<names>; team <team> is missing: <list>`. No journal task is
created.

**Missing required persona at drive time** (personas.yaml edited
between scaffold and drive): the drive-time pre-compile check
re-validates Anzai/Akagi/Ayako presence before reading `compile/`
state. Failure transitions to `state=blocked`,
`compile_phase=failed`, `next_action` naming the missing persona.

**No-persona step at graph-walk time** (orchestration.json contains
a step with empty `persona` field, suggesting compile produced
malformed output or someone hand-edited): `state=blocked`,
`next_action="step <id> has no persona; orchestration.json malformed
-- compile invariant violated"`. Per the Operator's directive:
**error, don't silently default**. (The Tier 1 roster validator
should catch this at compile time; this is defense in depth.)

**Compile-failed recovery.** Phase 1.5 left this manual (operator
inspects `compile/` artifacts, either edits the playbook + re-scaffolds
or runs `journal abort`). **Phase 2 added** the
`tigerharness journal compile-retry <task-id>` CLI: clears
`compile/round-*` artifacts and the `failed` flags, flips
`compile_pending=true` + `compile_phase=pending` + `state=pending`,
and lets the next `drive-journal` invocation re-attempt the compile.
The brief + playbook snapshot are preserved across retries so the
human can use the audit trail to decide whether to retry vs. edit
vs. archive.

## CLI surface (compile-side)

All new CLIs are pure Python (no LLM calls). The interactive session
shells out to them between turns:

| Command | Purpose |
|---|---|
| `tigerharness journal new --kind workflow --playbook <name> --task-brief <text>\|--brief-file <path>` | Scaffold-time only. Writes brief + playbook snapshot + `status.json` (`compile_pending=true`). |
| `tigerharness journal compile-context <task-id>` | Prints playbook + brief + roster + drafter prompt. One-shot context bootstrap for the session. |
| `tigerharness journal compile-prompts --task <id> --kind {drafter\|akagi\|ayako} [--feedback <str>] [--draft <path>] [--trace <path>]` | Prints the assembled prompt for the requested role, pulling text from `journal.wfcore.{drafter,critique}` with all interpolations applied. For `--kind {akagi,ayako}`, `--draft` and `--trace` are **required** (exit 2 otherwise); for `drafter` they're unused. |
| `tigerharness journal validate-graph --task <id> --draft <path>` | Runs `validate_compile_output` on the draft; emits JSON `{ok, errors, trace}`. Exit 0/1. |
| `tigerharness journal land-compile --task <id> --draft <path> --transcript <path> --rounds <N>` | Atomic transition: re-runs Tier 1, builds `Orchestration`, writes step files + `orchestration.json` + `compile_critique.md` to `compile/final/`, promotes via `os.replace`, flips `compile_pending=false` + `compile_phase=complete` last. |
| `tigerharness journal compile-fail <task-id> --reason <str>` | Soft compile failure: sets `state=blocked` + `compile_phase=failed`, writes `--reason` as `next_action`, leaves the task in `active/` for human inspection. The driver invokes this on a `WORKFLOW: BLOCK` critic verdict or on cap exhaustion. |
| `tigerharness journal abort <task-id>` | Final cleanup: archives a (typically already-failed) task to `done/` with `state=done` + postmortem `next_action`; preserves `compile/` for forensics. |
| `tigerharness journal validate-personas <team>` | Pre-flight check; exit 0 if Anzai/Akagi/Ayako prompts all exist, non-zero with missing list otherwise. |
| `tigerharness journal compile-retry <task-id>` | *(Phase 2)* Reset a compile-failed task — wipes `compile/`, sets `state=pending` + `compile_pending=true` — so the next `drive-journal` retries the compile from scratch. |
| `tigerharness journal append-steps --task <id> --new-bundle <path>` | *(Phase 3)* Append new step(s) to an already-landed graph; re-runs Tier 1 over the combined graph, atomic on success. See the step-append sub-protocol. |

The CLI names are pinned by OPERATING.md landmark assertions so a
rename in code without updating prose is caught by CI.

## status.json schema (`kind=workflow`)

Same as Phase 1 with three additions:

| Field | Phase 1 | Phase 1.5 (`kind=workflow`) |
|---|---|---|
| `kind` | `"task"` | `"task"` or `"workflow"` |
| `persona` | required | optional captain; `null` allowed |
| `max_sessions` | default `3` | default `10` (static; see below) |
| `compile_pending` | not present | required `bool`; `true` at scaffold, `false` post-land |
| `compile_phase` | not present | required `str` enum (seven values above) |
| `playbook_name` | not present | required `str` (bare playbook name); rejected for `kind=task` |

`Status.from_dict` enforces both new fields' presence/absence based
on `kind`. The graph-walker gates on
`compile_pending=false AND compile_phase=="complete"` before reading
`orchestration.json`.

`max_sessions` defaults to a static `10` for workflows (vs `3` for
tasks). The original design proposal called for `len(steps) * 2 + 3`,
but `len(steps)` is unknown at scaffold time (the graph has not been
compiled yet), so the static default shipped instead (see
`journal/models.py`). Override with `--max-sessions N`.

## Test strategy

> **Design history — Phase 1.5 planning; superseded by the shipped implementation.** Kept for provenance, not a description of current behavior.

Phase 1.5 keeps Phase 1's discipline: 100% line+branch on new
Python, no live LLM in CI, no `claude -p` subprocess. The Option C
carve-out makes this tractable because every load-bearing decision
lives in a Python CLI the test suite can drive directly. The session
is a thin protocol layer pinned by ~25 landmark smoke assertions.

Test budget: +130 tests on top of Phase 1's 152 (target ~282 total):

- ~30 unit tests for the four new compile CLIs (`compile-context`,
  `compile-prompts`, `validate-graph`, `land-compile`).
- ~20 state-machine tests for `compile_phase` transitions, atomic
  write/rename ordering, and the `os.replace` directory-promotion
  invariant.
- ~15 scripted-compile-driver integration tests via a new
  `tests/journal/scripted_compile_driver.py` scaffold that replays
  canned drafter / critic responses through the CLIs against a
  tmpdir journal (equivalent of `FakeSessionManager` for Option C).
  Scenarios: happy-path 3-round, drafter-fixes-tier1-on-retry,
  critic-loop-converges-after-2-revises, max-rounds-exhaustion,
  BLOCK-mid-compile, kill-during-land-asserts-no-half-state,
  rescue-from-stale-mid-critique.
- ~25 OPERATING.md / SKILL.md landmark assertions covering: section
  headings, the seven CLI names, the persona-adoption preamble shape,
  the `WORKFLOW: APPROVE/REVISE/BLOCK` vocabulary, the `>= 3 rounds`
  floor, the `compile_pending` and `compile_phase` field names.
- ~15 model-layer tests for the new fields + relaxed `kind=workflow`
  validation.
- ~15 scaffold tests for `--kind workflow` (now cheap: writes files
  + validates personas, no LLM).
- ~10 backwards-compat tests asserting `kind=task` flows are
  byte-identical to Phase 1.

Explicitly out-of-scope: any test requiring a real Claude session.
"Does Claude follow the protocol" is verified manually by you during
the Phase 1.5 PR review (mirrors Phase 1's manual-verify discipline).

## Implementation plan (Option C)

> **Design history — Phase 1.5 planning; superseded by the shipped implementation.** Kept for provenance, not a description of current behavior.

| Piece | LOC est | Notes |
|---|---|---|
| Model layer (`models.py`) | ~50 | Relax `kind` enum; add `compile_pending` + `compile_phase` fields with strict validation. Update tests. |
| Scaffolder (`scaffold.py`) | ~60 | `new_workflow_task(...)` writes brief + snapshot + `status.json`. No compile call. Persona pre-flight check (Anzai/Akagi/Ayako + playbook-extracted refs). |
| Compile CLIs (`journal/compile_cli.py`) | ~250 | Seven subcommands (above). Wraps `journal.wfcore.{validators, pipeline._build_orchestration, drafter, critique}` constants. |
| Sweep + list (`sweep.py`, `cli.py`) | ~20 | Show `compile_pending` + `compile_phase` in summary line + list table. |
| `OPERATING.md` + `drive-journal/SKILL.md` | ~180 lines | Compile sub-protocol section (~80 lines), persona-adoption preamble spec (~20 lines), state-machine docs (~30 lines), failure recovery (~50 lines). Pinned by landmark smoke tests. |
| Tests | ~130 tests / ~600 LOC test code | Per the breakdown above. |
| Doc updates | ~80 | Extend `docs/journal.md` with the workflow + compile sub-protocol section; this doc moves from "design" to "shipped" status. |

Total: ~700–800 LOC of new Python + ~180 lines of protocol markdown
+ ~600 LOC of test code. **ETA: ~10–14 hours of focused work** (about
2x Phase 1 given the new state-machine surface and the seven new
CLIs). Risk: **MED** (the in-session compile state machine is novel;
recovery paths are non-obvious; the test budget is large but
tractable because every load-bearing decision is in a Python CLI).

## Migration

> **Design history — Phase 1.5 planning; superseded by the shipped implementation.** Kept for provenance, not a description of current behavior.

**None.** The retired api runner's task journals under
`~/.local/state/tigerharness-workflows/` and the retired iterative runner's jobs under
`~/.local/state/tigerharness-tasks/` are not imported. They get
deleted after Phase 1.5 ships (Operator decision: nothing important
in them). In-flight api-runner tasks at ship time were lost —
documented as a one-line release-note warning.

## Phasing

- **Phase 1.5 (this doc)** — `kind=workflow` via Option C. Compile
  in-session at first drive, graph-walk per the existing protocol,
  manual recovery from `compile_phase=failed`.
- **Phase 2 (shipped on the closeout branch)** — `journal compile-retry`
  CLI for one-shot recovery from `compile_phase=failed`; per-round
  draft snapshots (already on disk under `compile/round-NN-*.md`);
  configurable compile-time persona roster (via
  `teams/<team>/configs/workflow.yaml`'s `compile_personas` key, with
  Anzai / Akagi / Ayako as the default).
- **Phase 3 (shipped on the closeout branch)** — step-append at
  runtime via `journal append-steps` + the `workflow-append-steps`
  skill. The originally-planned "human-gate enforcement" item from
  this phase has been **retired** -- it does not port to the
  subscription model, see *Out of scope* below.

## Out of scope for Phase 1.5

Deferred to a later phase if and when needed:

- **Step-append at runtime.** **Shipped in Phase 3** (closeout
  branch). New `journal append-steps --task <id> --new-bundle <path>`
  CLI + `workflow-append-steps` skill + an OPERATING.md sub-protocol
  section. Append-only (no rewrite / reorder); Tier 1 re-validates
  over the combined graph; atomic promotion on success, no change
  on failure.
- **Human gate enforcement (RETIRED, not deferred).** The api-backed
  workflow-runner's Tier 3 human gate existed to brake a process
  that was otherwise running the LLM autonomously via `claude -p`
  -- a safety mechanism against unattended token burn. The
  subscription model is *structurally* a human gate: every persona
  turn happens inside a live interactive Claude Code session a human
  is sitting in, and the human can stop or redirect the cascade at
  any moment. The Slack-thread notification, the
  `human_gate_approvers` allowlist, and the
  `human_gate_unauthorized_attempt` events do not port -- there is
  no orchestrator daemon to wait on a thread, no third party who can
  race the gate, and no autonomous execution to block. The journal
  hardcodes `WorkflowConfig(human_gate=False)` and the original
  "Phase 3 human gate" item has been struck from the roadmap. The
  `captain` field on `status.json` carries the accountable owner for
  display purposes (`journal list`).
- **`journal compile-retry` CLI.** **Shipped in Phase 2** (closeout
  branch). Clears `compile/round-*` artifacts and resets
  `compile_pending=true` + `compile_phase=pending` + `state=pending`
  so the next `drive-journal` can re-attempt without a re-scaffold.
- **Configurable compile persona roster.** **Shipped in Phase 2**
  (closeout branch). `teams/<team>/configs/workflow.yaml` now reads
  a `compile_personas` key; Anzai / Akagi / Ayako remain the default
  when the key is absent.
- **Per-step iteration caps.** Per-step `max_iters` lives in
  `StepFrontmatter` already; we honour it during graph walk, but
  enforcement is Phase 1.5's responsibility only via the existing
  workflow-runner step-frontmatter validation.

## Open questions for the Operator (Phase 1.5-specific)

> **Design history — these were resolved during the Phase 1.5 build (the doc shipped).** Kept for provenance; the "leans" described below were adopted unless a later doc says otherwise.

Seven questions the design refinement surfaced; my lean on each
follows. Defaults below apply unless you flip them:

1. **Mid-walk persona-missing routing** — if `personas.yaml` is edited
   between compile-done and a later step's execution, route to
   `state=blocked` (re-add persona, then re-drive) or
   `compile_phase=failed` (compile-specific)?
   *My lean: `state=blocked`*, symmetric with Phase 1 blocker
   semantics. `compile_phase=failed` stays strictly for compile-phase
   failures.
2. **Mid-compile rescue granularity** — round-level checkpoints
   (current proposal; wastes one critic call per recovery) or
   verdict-level (more code, maximum work preservation)?
   *My lean: round-level* for protocol simplicity; verdict-level can
   land in Phase 2 if real compiles get long.
3. **`compile/final/` post-promotion** — delete after promotion
   (canonical copies have moved) or keep as a redundant snapshot?
   *My lean: delete*. Round-NN.json + transcript.md preserve audit.
4. **Compile sessions counter** — unified `sessions` counter (current
   proposal; `len(steps) * 2 + 3`) or separate `compile_sessions`?
   *My lean: unified* for simplicity; split makes failure modes more
   legible but adds a field.
5. **Cost-tracking in `round-NN.json`** — drop entirely (no api
   billing in Option C) or keep a token-count estimate for
   telemetry?
   *My lean: drop*. Implies api billing where there is none.
6. **Compile persona roster** — hard-code Anzai/Akagi/Ayako in
   Phase 1.5, or make team-configurable from the start?
   *My lean: hard-code* for Phase 1.5; configurable is Phase 2.
7. **`journal compile-retry` in Phase 1.5** — ship it, or defer?
   *My lean: defer*. Manual file surgery is fine for v1 given
   compile failures are expected to be rare and indicative of bad
   playbook input.

If you confirm "go with the leans" on all seven, I have everything
needed to implement Phase 1.5 the same shape as Phase 1.

## Non-goals

- **Replacing the existing api-backed workflow-runner.** It stays;
  Phase 1.5 just makes the journal a viable subscription-friendly
  alternative for the same shape of workload.
- **Inventing a new graph format.** We reuse
  the compile core's output verbatim — `orchestration.json`
  + `steps/<id>.md` per the existing schema.
- **Parallel persona work.** A single human drives serially.
  Concurrent drivers stay out of scope; the heartbeat-as-soft-lease
  handles the rare race (same answer as Phase 1).
- **Testing real Claude conformance to the protocol.** Test suite
  covers the Python CLIs at 100% line+branch; "does the model
  actually follow OPERATING.md" is verified manually during PR
  review.

## Related

- [`subscription-backend.md`](subscription-backend.md) — the design
  Phase 1 implements; this doc extends.
- [`journal.md`](journal.md) — Phase 1 operator docs.
- [`workflow-runner.md`](workflow-runner.md) — the api-backed
  multi-persona runner whose compile machinery we reuse.
- [`workflow-runner-phase2.md`](workflow-runner-phase2.md) — the
  compile / drafter / critique pipeline this design depends on
  (shipped in Wave 2).

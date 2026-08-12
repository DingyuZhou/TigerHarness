# journal

## At a glance
- **What:** the journal backend's concepts + task lifecycle — `kind=task`
  (single-persona) and `kind=workflow` (multi-persona), scheduled → driven →
  done on the subscription rail.
- **When you need it:** the big picture and the task lifecycle. For workflow
  compile/walk see [journal-workflow-mode.md](journal-workflow-mode.md); for
  rails/billing + `status.json` see [subscription-backend.md](subscription-backend.md);
  for crash/idle resume see [journal-instant-resume.md](journal-instant-resume.md).
- **Must-not-miss:** driving is the subscription rail; Slack only schedules.

File-based, human-driven subscription backend (Phase 1 + Phase 1.5 + Phase 2 + Phase 3).

> **Status:** Phase 1 + Phase 1.5 + Phase 2 (closeout) + Phase 3
> step-append shipped. Phase 1 covers single-persona tasks
> (`kind=task`, PR #25 / `7d6b9f8`). Phase 1.5 adds multi-persona
> workflow tasks (`kind=workflow`, PR #26 / `155128f`) compiled
> in-session from a team playbook -- zero API billing, the
> interactive session itself adopts the drafter and critic personas
> and shells out only to pure-Python validators. Phase 2 added
> `journal compile-retry`, a configurable compile-time persona
> roster, and an end-to-end scripted compile driver integration
> suite. Phase 3 added `journal append-steps` for runtime graph
> extension. Both kinds share the same scaffolder, sweep, list, and
> `OPERATING.md` protocol -- the protocol switches on `status.kind`
> at the work step. 100% line + branch coverage across the journal
> package.

## What it does

Runs agent work through the **interactive** Claude Code app so the
work counts against a monthly subscription instead of token-billed
API usage. Durable state lives on disk in a `journal/` folder; a
human-triggered skill drains the queue continuously per session.

See [`subscription-backend.md`](subscription-backend.md) for the full
design (push-vs-pull rationale, soft-lease semantics, cascade
behaviour, OPERATING.md protocol).

## Architecture

```
You: write a PRD
    |
    v
Scaffolder (CLI `tigerharness journal new` or `journal-new` skill)
    |-- creates journal/active/<task-id>/ from the PRD
    v
journal/ (passive file-based state machine)         <-- source of truth
    ^
    |
    v
Driver (`drive-journal` skill, interactive session)
  1. Lazy sweep of active/ (no AI, no cron, no daemon)
       - archive done/ tasks
       - classify in_progress as idle/busy/crashed via the `session_ref` attach signal (heartbeat is crash-only; see docs/journal-instant-resume.md)
       - summarise actionable counts in-session
       - cheap no-op fast path: if all in_progress are busy and nothing is idle/crashed/pending, stop immediately (a frequent loop is meant to no-op here)
  2. Pick ONE actionable task (finish-before-start: resumable in_progress first; a busy task defers a pending one), run it through its WHOLE max_sessions budget, session-to-session
       - reads OPERATING.md; claims via `journal claim` (in a drive: `--driver <persona>`)
       - appends progress.md as it goes
       - updates status.json (state, heartbeat, sessions, next_action)
  3. Cascade (the hard loop): re-sweep and pick the next back-to-back in the SAME turn; one drive drains the whole queue, never one-session-per-loop-fire
       - context pressure is not a stop reason: rely on auto-compaction and re-orient from progress.md; hand off only at the true context ceiling
```

## Folder layout

```
<journal>/
  OPERATING.md                # vendor-neutral protocol (installed by scaffolder)
  active/
    <task-id>/                # kind=task layout
      task.md                 # the PRD verbatim
      status.json             # the state machine (single source of truth)
      progress.md             # append-only log, human + AI readable
      artifacts/              # whatever the task produces

    <task-id>/                # kind=workflow layout
      task_brief.md           # the brief verbatim
      playbook_snapshot.md    # the team playbook at scaffold time
      status.json             # adds compile_pending + compile_phase
      progress.md
      artifacts/
      compile/                # in-flight compile workspace (round files, transcript)
      orchestration.json      # post-compile: the compiled graph (atomically promoted)
      steps/                  # post-compile: one frontmatter file per graph step
      compile_critique.md     # post-compile: full critic transcript
  done/
    <task-id>/                # finished tasks moved here by the next drive-journal sweep
  needs_input/
    <task-id>/                # tasks PARKED on an Operator question (questions.md);
                              # out of the actionable queue until `journal answer <id>`
                              # moves them back to active/. See journal-operator-questions.md
```

Task-id format: `<YYYYMMDD>-<HHmmSS>-<slug>-<uuid8>`.

- `YYYYMMDD-HHmmSS` — UTC date and time at creation (one clock
  reading). Same-day ids therefore sort in their creation order
  under a plain lexicographic sort — scheduling several tasks in one
  day preserves their scheduled sequence.
- Legacy ids minted before the time component
  (`<YYYYMMDD>-<slug>-<uuid8>`) stay fully valid: nothing parses the
  timestamp back out of an id, so old and new folders coexist in one
  journal with no migration. Relative order between a same-day legacy
  id and a new-format id is plain lexicographic (a digit sorts before
  a letter) — documented as-is, no machine distinguishability is
  claimed.
- `slug` — `slugify(--title or first H1 of PRD, max=40)` (ASCII
  lowercase, hyphen-separated; falls back to `"task"` if the source
  has no usable chars)
- `uuid8` — 8 hex chars from `secrets.token_hex(4)`
  (collision-rare; the scaffolder regenerates once on hit, then
  hard-errors)

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `TIGERHARNESS_JOURNAL_DIR` | resolver (below) | Override the journal root. |
| `TIGERHARNESS_JOURNAL_STUCK_TIMEOUT` | `1800` (30 min) | Heartbeat age past which an *attached* `in_progress` task (`session_ref` set) is treated as **crashed** and reclaimable. A detached task is **idle**/resumable regardless of age — the heartbeat is crash-detection only. |

Journal root resolution priority:

1. `TIGERHARNESS_JOURNAL_DIR` (env override)
2. `<cwd>/journal/` if cwd has `configs/personas.yaml`
   (the convention scaffolded by `tigerharness init`)
3. `$XDG_STATE_HOME/tigerharness-journal`
4. `~/.local/state/tigerharness-journal`

**Scheduling never falls back silently.** The XDG fallback (3/4) is
for *inspection* of a personal journal — but `journal new` and
`journal schedule add` REFUSE to run from a non-team cwd without an
explicit `--journal-dir` / `$TIGERHARNESS_JOURNAL_DIR` (exit 2, the
error names the cwd and the fix). A team-scheduled task can only land
in that team's own `journal/`; this is what prevents the
misplaced-task class (a task scheduled from the wrong directory
silently landing in the per-user state dir). Every scaffolded task
also records a `journal_root` provenance field (see status.json
schema); `journal sweep` flags any task whose recorded root doesn't
match where it sits as **MISPLACED**, and reports pre-provenance
tasks as placement-unknown rather than guessing.

**Migrating a misplaced task.** If a task did land in the wrong
journal (e.g. scheduled before this guard existed), move it while it
is detached (no live session): stop any drive, then
`mv <wrong-journal>/active/<task-id> <team-journal>/active/`. The next
`journal sweep` from the team root picks it up normally. Worked
example: a task scaffolded into `~/.local/state/tigerharness-journal/`
on 2026-06-12 was relocated this way into the team journal mid-drive
with no state edited.

**Cheap Slack-side scheduling (the deferred inbox).** Scheduling from
Slack bills API tokens, so the Slack side runs one dumb verb —
`journal defer --title ... --team <Team>` (payload on stdin or
`--payload-file`) — which copies the conversation verbatim into the
team journal's `deferred/` inbox (no playbook read, no compile, no
LLM). A later `drive-journal` session materializes it on the
subscription rail: `journal materialize <id>` turns the inbox entry
into a real `kind=workflow` task via the same scaffolder `journal new`
uses (persona preflight included), indistinguishable from a direct
scaffold. Malformed entries exit 1 with a JSON envelope and stay in
the inbox for repair; `journal sweep` surfaces the inbox so the driver
materializes the oldest when nothing else is actionable.

**Scheduling can start the driver (opt-in).** Per
[ADR 0010](adr/0010-self-driving-journal.md), `journal new`, `defer`,
`materialize`, and `answer` call `autodrive.ensure_running()` after the
queue write succeeds. It is a **no-op unless
`TIGERHARNESS_AUTODRIVE_AUTOSTART` is truthy** in the team's `configs/.env`,
and it **never fails the scheduling command** — a daemon that cannot start
logs a warning and the task is still queued. Combined with the daemon's
auto-stop, the effect is: scheduling work starts the driver, draining the
queue stops it, and nothing runs in between. The Slack rule is unchanged —
Slack schedules, never drives. See [autodrive.md](autodrive.md).

## Team-level defaults (`configs/personas.yaml`)

A team's `personas.yaml` carries two optional knobs that make the
scaffolder less typey:

- **`default_persona: <name>`** -- used by
  `journal new --kind task` when `--persona` is omitted. The first
  persona that `tigerharness init` adds becomes the default
  automatically; edit later as the team grows. A `default_persona:`
  that names a persona missing from disk is rejected at
  `journal new` time with a clear error pointing at the yaml.
- **Persona `aliases:` per entry** -- a list of alternate names
  (case- and separator-insensitive) that resolve to the canonical
  persona. Matches the personas registry's alias rules. Examples on a team:

  ```yaml
  default_persona: Ayako
  personas:
    - name: Kogure
      aliases: [Mumu, Kogure-senpai, 木暮]
  ```

  Then `journal new --persona Mumu` finds Kogure's prompt.md, the
  playbook prose "Mumu reviews" counts as a real persona reference
  for compile-time validation, and a `workflow.yaml` override like
  `compile_personas: { ayako: Mumu }` correctly resolves to
  Kogure.

  Conflict policy: canonical names always win over alias entries
  (regardless of file order); among colliding aliases, last-defined
  wins.

## Team-level workflow config (`configs/workflow.yaml`, optional)

For `kind=workflow` tasks a team may override which personas play
the three compile-time roles:

```yaml
compile_personas:
  drafter: Anzai     # default; the steps-bundle author
  akagi:   Akagi     # default; execution-mechanics critic
  ayako:   Mumu      # alias of Kogure on this team -- QA critic
```

All three keys are optional; absent keys fall back to
`Anzai`/`Akagi`/`Ayako`. Names resolve through the alias map above.
A missing yaml file means "all defaults."

## Playbook-level defaults (HTML-comment YAML)

A playbook can declare metadata in a multi-line HTML comment:

```markdown
# My playbook

<!--
default_captain: Mitsui
-->

## Roles
...
```

Recognised keys (whitelisted; unknown keys are dropped silently):

- **`default_captain: <name>`** -- accountable owner used when
  `journal new --kind workflow` omits `--captain`. Resolved through
  aliases.

Single-line comments like `<!-- foo: bar -->` are treated as
narrative prose and ignored.

## Usage

```bash
# Scaffold a new task from a PRD (kind=task -- single persona).
# --persona is optional when the team's personas.yaml declares
# `default_persona:` (see "Team-level defaults" above).
# --max-sessions defaults to 3 for kind=task, 10 for kind=workflow.
# --early-exit (default off) lets the driver stop the moment the task
# is done; with it off, the driver runs the full --max-sessions budget
# ("N iterations = exactly N").
tigerharness journal new \
    --prd brief.md \
    --persona Mitsui \
    --max-sessions 5 \
    --early-exit

# Scaffold a new workflow task (kind=workflow -- multi-persona,
# compiled from a team playbook). Either --task-brief or --brief-file
# supplies the brief. --captain is optional and falls back to the
# playbook's `default_captain:` HTML-comment YAML if present.
tigerharness journal new \
    --kind workflow \
    --playbook default \
    --task-brief "Ship the feature" \
    --captain Mitsui

# Compile-side CLIs (called from a drive-journal session per
# OPERATING.md's compile sub-protocol; pure Python, no API billing).
tigerharness journal compile-context <task-id>
tigerharness journal compile-prompts --task <id> --kind drafter|akagi|ayako ...
tigerharness journal validate-graph --task <id> --draft <path>
tigerharness journal land-compile   --task <id> --draft <path> --transcript <path> --rounds <N>
tigerharness journal compile-fail   <task-id> --reason "<postmortem>"
tigerharness journal compile-retry  <task-id>           # Phase 2: reset a failed compile + retry
tigerharness journal append-steps   --task <id> --new-bundle <path>  # Phase 3: extend a landed graph
tigerharness journal abort          <task-id>
tigerharness journal validate-personas <team>

# Quick read-only inspect (no archives, no flags).
tigerharness journal list           # table format -- new KIND column
tigerharness journal list --format json
tigerharness journal status <task-id>

# The lazy sweep -- side-effecting: archives `done` tasks, classifies
# the rest, prints a summary. The `drive-journal` skill calls this as
# its first action, but you can also run it ad-hoc.
tigerharness journal sweep
tigerharness journal sweep --format json
tigerharness journal sweep --stuck-timeout 600
```

The driver is **skill-only by design**: there is no
`tigerharness journal drive` CLI because a CLI driver would
reintroduce programmatic billing and defeat the subscription model.
Driving only happens inside an interactive Claude Code session.

## Scheduled (recurring) tasks -- DEPRECATED

> **Deprecated as of 2026-08-12**
> ([ADR 0010](adr/0010-self-driving-journal.md)). Definitions materialize
> **only inside a sweep**, and a sweep only happens while a daemon is awake.
> Now that autodrive correctly stops on an empty queue, a recurring
> definition's due time can pass with nothing running to notice it --
> auto-stop and recurring schedules are structurally incompatible as built.
>
> Per the Operator, recurring tasks are not load-bearing today.
> `journal schedule add` emits a deprecation warning naming the ADR; the
> code, the trays, and existing definitions keep working exactly as
> described below (the daemon still materializes due definitions while it is
> awake). Removal is a separate, announced step -- the ADR 0003 / ADR 0009
> pattern.
>
> If the feature comes back it needs a design that does not depend on a
> long-lived process: an OS-level timer that runs the materialization sweep
> and then calls `autodrive.ensure_running()` is the obvious shape.

Recurring definitions live in `schedule/` beside `active/`, one JSON
file each, and are materialized into normal pending tasks by the lazy
sweep -- the drive is the only clock; there is no daemon.

```bash
# Daily self-diagnosis, materialized by the first drive after 08:00:
tigerharness journal schedule add \
    --title "Morning self-diagnosis" \
    --period daily --at 08:00 \
    --kind workflow --playbook self-diagnosis \
    --task-brief "Run the morning bug hunt." \
    --autonomy judgement

tigerharness journal schedule list
tigerharness journal schedule rm morning-self-diagnosis
```

Semantics (deliberately small in v1):

- **Cadence** is `daily` or `weekly` at `HH:MM` local **wall clock**
  (DST-safe: occurrences are recomputed from the calendar, never
  `+86400s`); `next_due` is stored in UTC.
- **Run-late-once**: a definition due at 08:00 whose first drive
  happens at 15:00 materializes once, then `next_due` advances to the
  next *future* occurrence. Missed days are never backfilled.
- **Skip-if-in-flight**: while a prior instance of the same
  definition is still in `active/`, nothing new is materialized and
  `next_due` does not advance -- the first sweep after it finishes
  fires.
- **Exactly-once under concurrent drives**: materialization is a
  two-phase intent protocol on the definition file (CAS lease +
  `next_due` advance in one atomic write, then scaffold and close);
  a crashed materialization is recovered by the next sweep -- a lost
  run is completed and a completed run is never repeated.
- The prd/brief is **inlined into the definition at add time**, so a
  definition never dangles on a moved file. Materialized tasks are
  stamped with `schedule_def` + `schedule_due` in their status.json.
- v1 has **no disable verb**: `enabled: false` is honored if set by
  hand, but the CLI levers are `add` and `rm` only -- disable =
  `rm` now, re-`add` later.
- A malformed definition is reported in the sweep summary
  (`N malformed-definitions`) and skipped -- it never breaks the
  sweep.

## status.json schema

See the field-by-field table and state-transition rules in
[`subscription-backend.md` — "status.json — the heart"](subscription-backend.md).
Phase 1.5 adds three workflow-only fields to that schema:

| Field | Type | When | Meaning |
|---|---|---|---|
| `kind` | `"task"` \| `"workflow"` | always | Selects the protocol branch at step 4. |
| `compile_pending` | `bool` | workflow only | `true` until `land-compile` flips it. The driver runs the compile sub-protocol while this is `true`. |
| `compile_phase` | enum | workflow only | One of `pending`, `drafting`, `tier1_pre`, `critiquing`, `tier1_post`, `complete`, `failed`. |
| `journal_root` | str \| absent | both kinds | Provenance: the journal root the task was scaffolded into, stamped at every scheduling write point. The sweep flags a task whose recorded root != where it sits as MISPLACED; tasks predating the field (absent) are reported placement-unknown, never guessed. |

For workflows, `persona` becomes the optional `--captain` (the
accountable owner shown in `journal list`); per-step personas come
from the compiled `orchestration.json` graph. The full design lives
in [`journal-workflow-mode.md`](journal-workflow-mode.md).

## Skills

- `skills/journal-new/` — scaffolder skill (CLI form is the primary;
  the skill is a thin wrapper). Teaches both `kind=task` and
  `kind=workflow` modes.
- `skills/drive-journal/` — driver skill (skill-only, no CLI form).
- `skills/workflow-append-steps/` — Phase 3 step-append skill for
  extending a landed graph at runtime. Wraps `journal append-steps`.

The driver skill points the interactive session at the on-disk
`OPERATING.md` for the canonical protocol, which now includes the
compile sub-protocol (Phase 1.5) and the step-append sub-protocol
(Phase 3).

## OPERATING.md

The vendor-neutral contract. Lives at `<journal>/OPERATING.md` and is
installed by the scaffolder on first use. The shipped template is in
[`src/tigerharness/journal/operating_template.py`](../src/tigerharness/journal/operating_template.py).
The scaffolder will NOT overwrite a human-customised `OPERATING.md`
on subsequent runs — once you've edited it, it's yours.

## Non-goals

- **Parallelism.** A single human drives serially. Concurrent drivers
  are explicitly out of scope. The heartbeat acts as a soft lease;
  see [`subscription-backend.md` — "How serial execution is
  enforced"](subscription-backend.md) for the race-window discussion.
- **Replacing the api backend.** The two coexist (Phase 2 config
  switch); pick `subscription` (default) or `api` per task.
- **Automating the interactive app.** No keystroke automation. The
  human trigger is the design.

## Related

- [`subscription-backend.md`](subscription-backend.md) — the Phase 1
  design.
- [`journal-workflow-mode.md`](journal-workflow-mode.md) — the
  Phase 1.5 design: `kind=workflow`, the compile sub-protocol, the
  persona-switching mechanic.

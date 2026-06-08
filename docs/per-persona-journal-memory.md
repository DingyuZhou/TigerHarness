# Per-persona memory from journal-driven work

Status: **proposal / plan** (not yet implemented)
Author: Anzai (Shohoku)
Related: [`subscription-backend.md`](subscription-backend.md),
[`tiger-memory.md`](tiger-memory.md),
[`tiger-memory-sweep-protocol.md`](tiger-memory-sweep-protocol.md)

## Problem

As the team moves most work onto the journal driver (`drive-journal`),
all of that work happens inside **one** Claude session — the
human-triggered driver's. The driver adopts each persona *in-session*
via the four-line preamble (`PERSONA:` / `ROLE:` / `STEP:` /
`OBJECTIVE:`); no separate sessions, by design (the subscription
billing rule forbids `claude -p` for persona steps, and Task sub-agents
are dropped from memory by the sidechain filter, B7).

tiger-memory attributes a whole transcript to **one** owner via
`threads.json`. So every drive — Anzai planning, Rukawa implementing,
Mitsui testing — collapses into the **driver's** store. The specialists'
own stores get nothing. We want each persona to remember the work it
personally did.

## Goals

- A persona's journal work lands in **that persona's** memory store.
- Applies to **both** task kinds: `kind=task` (one assigned persona)
  and `kind=workflow` (many personas). Both are journal-driven.
- The driver keeps only a **very thin** trace — one line per task
  actually driven or rescued. No memory at all for no-op iterations
  (empty queue, busy-only exit) — those are cron-like noise.
- Attribution is **enforced in harness code**, not requested in a
  prompt. A persona turn cannot advance without leaving its record.

## Non-goals

- Quality of a note's prose. Code can guarantee a note **exists** and
  is **correctly attributed**; it cannot force it to be well-written.
  The worst case becomes a thin note, never a missing one.
- Re-architecting to per-persona sessions (forbidden by the billing
  model — this is the constraint we design around).

## Decisions (from the brainstorm)

1. **Scope:** both `kind=task` and `kind=workflow`.
2. **Driver share:** very thin — only real tasks driven/rescued;
   nothing for no-op sweeps.
3. **Protocol changes:** allowed where they make enforcement easier and
   more robust.
4. **Enforcement principle:** *the note is the ticket to advance.* The
   journal CLI that moves the workflow forward writes (and verifies)
   the note and refuses to advance without it.

## Architecture overview

Drive memory off the journal's **structured records**, not the raw
transcript. The journal already produces durable, persona-attributable
artifacts; reading those is robust, whereas parsing the transcript for
`PERSONA:` markers is brittle (a forgotten/misformatted preamble
mis-routes silently).

Three moving parts:

1. **Write path (harness code):** every persona turn produces a
   *worklog entry*, emitted by the journal CLI the turn must call to
   proceed. The driver writes a thin entry at `claim`/`release`.
2. **Ingestion path (memory code):** a new `JournalWorklogAdapter`
   source discovers worklog entries, attributes each by its `persona`
   frontmatter, and feeds the **existing** sweep → plan → summarize →
   ingest machinery unchanged.
3. **Double-count suppression:** the drive transcript is marked so the
   `claude_transcript` adapter skips it — otherwise the driver would
   *also* get a fat summary of the whole drive, defeating the "thin"
   goal.

Plus a **prerequisite:** every persona that does journal work needs a
tiger-memory config + store. Today only Anzai & Ayako have them.

## The enforcement principle (load-bearing)

> A persona turn ends by handing its output to a journal CLI gate.
> The gate writes the worklog entry — **stamping the persona itself,
> from the orchestration/compile mapping, so the name can never be
> wrong** — and only then returns permission / the next step. No
> output → the gate fails → the turn cannot advance.

Today this already exists for **compile** rounds: you cannot start the
next round without handing your written work to `validate-graph` /
`compile-prompts`. The gap is the **graph-walk** (routing is currently
"soft" — the session reads `orchestration.json` and follows edges
itself, with no per-step CLI) and **kind=task** work (only `claim` /
`release` gate it). We close both gaps.

A determined in-session agent could still bypass a soft gate by reading
`orchestration.json` directly, so we add a **backstop**: `release
--state done` (and the sweep's archival) verifies every executed step
has a worklog entry and refuses to mark the task done otherwise. Gate +
completion-check together make a missing note practically impossible.

## Components

### 1. Worklog format & location

One markdown file per turn under the task directory, surviving archival
to `done/`:

```
active/<task-id>/worklog/NNNN-<persona>-<step>.md
```

YAML frontmatter + body (the turn's narrative output):

```yaml
---
task_id: <id>
kind: task | workflow
persona: Rukawa          # stamped by code, from the step→persona map
role: <role>
step: <step-id | "task-work" | "compile-draft" | "compile-akagi" | ...>
objective: <one line>
verdict: APPROVE | REVISE | BLOCK   # workflow turns only
started_at / ended_at
---
<the turn's substantive output: what was done, decided, produced>
```

Rationale for files (vs a single `worklog.jsonl`): human-readable,
drill-down friendly, mirrors the existing `compile/round-NN-*.md`
style, trivial to write atomically per turn.

### 2. Write path (harness code)

- **`claim`** (`journal/cli.py:445`): when claiming a `pending` /
  `idle` / `crashed` task (i.e. a *real* drive or rescue), write a thin
  driver worklog entry — `task_id`, `kind`, `reason` (new / resume /
  rescue), attributed to the **driver persona**. No-op sweeps never
  call `claim`, so they leave no trace (satisfies decision #2). Driver
  persona supplied by the drive session via `--driver <name>` (the
  session knows its own persona), or derived from `thread_ts` →
  `threads.json` if registered (see §4).
- **`release`** (`journal/cli.py:552`): append the outcome to the
  driver's thin entry (done / handed-off-idle / blocked). For
  `kind=task`, **require** a worklog entry for this session before
  permitting `--state done`; refuse otherwise (the completion backstop).
- **Graph-walk gate — NEW CLI** `journal step-done`:
  `--task <id> --step <id> --output <path> --verdict <APPROVE|REVISE|BLOCK>`.
  It (a) validates the step is current, (b) reads `persona`/`role` from
  `orchestration.json` for that step, (c) writes the worklog entry
  stamped with that persona, (d) returns the next step id (follows
  `on_approve`/`on_revise`/`on_block`, or `__done__`/`__escalate__`),
  (e) refuses (non-zero) if `--output` is missing/empty. The graph-walk
  sub-protocol is updated so the session learns the next step **only**
  from this CLI.
- **Compile:** emit normalized worklog entries from the round outputs
  already saved (`compile/round-NN-<role>.md`), mapping role→persona via
  `compile_personas`. Likely folded into `compile-prompts` /
  `land-compile` so no extra step.

### 3. Ingestion path (memory code)

- **New source adapter** `JournalWorklogAdapter`
  (`tiger_memory/sources/journal_worklog.py`): discovers
  `*/worklog/*.md` under the journal root (both `active/` and `done/`),
  reads frontmatter, filters by `persona`, and emits `SourceRecord`s.
- **Grouping:** the summary unit is **per (task, persona)** — "Rukawa's
  memory of task X" — not per-turn. Fewer sub-agent calls, a natural
  recall unit. Individual turn files remain the drill-down detail.
  `conversation_uuid = uuid5("journal:" + team + "/" + task_id + "/" +
  persona)` (precedent: `docs.py` derives uuid5 from a path). Stable as
  the task grows; the existing addendum/growth path handles
  re-summarization.
- **Config wiring:** each persona's `tiger-memory.config.yaml` gains a
  source:

  ```yaml
  - kind: journal_worklog
    journal_root: /home/tigerleap/projects/teams/Shohoku/journal/
    persona: Rukawa
  ```

  and a matching branch in `lifecycle.py:_build_adapters`
  (alongside `claude_code` / `slack_thread` / `docs`).
- **Sweep staleness must see worklog activity.** A specialist (e.g.
  Rukawa) may have *no* direct Slack threads — only worklog entries from
  drives. The team-sweep's per-persona "due / stale" detection must
  count worklog-file activity as new activity, or that persona is never
  swept and its journal memory never materializes. Verify against
  `sweep.py` staleness logic during Phase 2.

### 4. Double-count suppression

Without this, the `claude_transcript` adapter would still fold the whole
drive into the driver's store (fat) — defeating decision #2.

- At `claim`, record the drive session's `thread_ts` (from
  bridge-context) to a registry, e.g. `journal/.drive-sessions.json`.
- `ClaudeTranscriptAdapter` reads the registry and **skips** transcripts
  whose `thread_ts` is a registered drive session; the worklog now owns
  that content (persona slices → specialists, thin trace → driver).

### 5. Roster rollout (prerequisite)

Give every journal-working persona a tiger-memory config + store +
briefing scaffold: **Akagi, Rukawa, Mitsui, Miyagi, Sakuragi, Kogure,
Haruko** (Anzai & Ayako already have them). Independent of the rest;
can run first/in parallel.

## Phasing & dependencies

- **Phase 0 — Roster rollout.** Configs + stores for the 7 missing
  personas. Independent; do first or in parallel.
- **Phase 1 — Write path.** Worklog format; `claim`/`release` thin
  driver entries; `step-done` gate + graph-walk routing through it;
  `kind=task` release gate; compile worklog normalization. Tests.
- **Phase 2 — Ingestion path.** `JournalWorklogAdapter`; config schema +
  `_build_adapters` branch; per-(task,persona) grouping + uuid5. Tests.
- **Phase 3 — Double-count suppression.** Drive-session registry at
  `claim`; `claude_transcript` skip. Tests.
- **Phase 4 — Protocol docs.** `operating_template.py` (so scaffolded
  journals teach the gates), `drive-journal` SKILL.md,
  `subscription-backend.md`, `tiger-memory*.md`.

Dependency order: 1 → 2 → 3. Phase 0 and Phase 4 run alongside.
100% line-coverage floor applies throughout.

## Decisions (locked 2026-06-08)

1. **Worklog format:** per-turn markdown files (one file per turn).
2. **Summary granularity:** per-(task, persona).
3. **Driver attribution:** `--driver <name>` passed by the session
   (simple; no coupling to the registry).
4. **`kind=task` assigned persona:** the task's work is filed under the
   task's **assigned persona** regardless of who drove it (the driver
   need not formally adopt it). The `step-done`/`release` gate stamps
   from `status.json`'s assigned-persona field — confirm the field
   exists; add it at scaffold time if missing.

## Known limitations (v1)

- **Mixed drive sessions:** if a human chats about something unrelated
  *during* a drive, that chatter is skipped (the whole drive transcript
  is skipped). Dedicated drive sessions are the norm — accepted for v1.
- **Note quality not enforced:** code guarantees existence + correct
  attribution, not richness.
- **Soft-gate residual:** an agent could bypass the graph-walk gate by
  reading `orchestration.json` directly; the `release --state done`
  completion-check is the backstop.

## Testing

- Unit: worklog write/parse; `step-done` routing + refusal on empty
  output; `release` completion-check; `JournalWorklogAdapter`
  discovery/attribution/grouping; registry skip in
  `claude_transcript`.
- Integration: a synthetic `kind=workflow` drive produces N persona
  stores + a thin driver trace; a no-op sweep produces nothing.

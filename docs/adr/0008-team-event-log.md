# ADR 0008 — Team event log: a lazy, self-compacting cross-persona activity ledger

- Status: accepted
- Date: 2026-08-01
- Driver: Operator directive (Dingyu Zhou), executed by Ayako

## Context

Persona memory (ADR 0007) is deliberately private: each persona's three
bounded stores hold what *that persona* learned and did. Nothing records
what the *rest of the team* did. A persona picking up unfamiliar work has
no cheap way to answer "who touched this before, and when?" — the answer
is buried in other personas' stores (which it must not load), in git
history, or nowhere.

Operator directive (2026-08-01, verbatim intent): add a **team-wide event
log** — a concise, dated ledger of the form

```
2026-08-01
- Anzai did xxx.
- Ayako did yyy 3 times.
- Sakuragi did QA for zzz.

2026-07-31
- Mitsui did aaa.
```

so a team member can figure out things they didn't work on before. The
log must be **lazy-loaded** — never part of any session-start briefing
load, read only when needed. And it needs a **compaction system**: the
latest 30 days stay uncompacted at daily granularity; older entries are
compacted to month summaries, and eventually to year summaries.

## Decision

### Store model

One **team-level** store, beside the per-persona stores and the sweep
state file, owned by no persona:

```
<team>/memories/team/
  events.md              # the ledger — durable store AND read surface
  .compact-staging/      # fold prompts/cards (transient)
  .events.lock           # cross-persona append lock (transient)
```

`events.md` is a single markdown file that is both the durable store and
what a persona reads — the period **heading granularity encodes the
compaction tier**, so no frontmatter substrate and no projection split
are needed: `## 2026-08-01` (raw daily entries), `## 2026-07` (a
compacted month), `## 2025` (a compacted year), newest first (period
strings order lexicographically across granularities). A day section
holds `- <Persona> <did thing>.` bullets; an exact repeat within a day
collapses to a `(xN)` count suffix — the "did yyy 3 times" form.
`month`/`year` sections hold the AI-folded summary bullets. Single
substrate — no dual-write problem, and git history preserves everything
compaction forgets.

### Write path: the sweep, contract v3

Events are extracted where the transcript is already being read: the
sweep's extraction sub-agent. The card contract gains a fourth section —
v3 is `@@SKILLS@@` / `@@MUST_REMEMBER@@` / `@@TOPICS@@` /
`@@TEAM_EVENTS@@`, in that order, each marker exactly once:

```
@@TEAM_EVENTS@@
EVENT: <one concise line, <= ~15 words, past tense, verb-first, no persona name>
```

Zero to three `EVENT:` lines per session (or `NONE`); the extractor is
told to record only *work done* (shipped, reviewed, QA'd, decided,
migrated…), not process noise. EVENT items are parsed **line-wise**
(consecutive `EVENT:` lines need no blank line between them — a
block-wise parse would silently keep only the last). Ingest
(`ingest-staged`, already the single-process glue) prefixes the persona
name, stamps the item's session **end date** (the plan manifest's
`last_event_at` — a backlog sweep must not pile old work onto today),
and appends into the team store under that day. Cross-persona
concurrency is real (personas may be processed in parallel), so the
append takes an O_EXCL file lock with retries; a held lock is logged
and skipped rather than failing the ingest — the log is awareness, not
the ledger of record.

The extraction prompt does **not** embed the event log (no routing
needed — events are append-only facts); prompts stay lean.

### Lazy load (a pointer, never a payload)

- No briefing file ever includes event content. `briefing/README.md`
  gains one pointer line: the team event log lives at
  `../../team/events.md`; open it **only** when the session actually
  needs cross-team awareness (unfamiliar work, "who did X?", handoffs).
- That is the whole read rail. Session bootstrap cost is unchanged: the
  three small indexes.

### Compaction: age-tiered, staged, deterministic convergence

Two forces bound the file: an **age policy** (the Operator's 30-day
window) and a **size bound** (`max` / `overflow_limit` hysteresis over
the rendered projection, like every other surface).

- **Day → month**: a `day` entry older than `recent_days` (default 30)
  belongs to a *closed* month; once a month has aged out entirely, its
  day entries are compaction-eligible. Staged like ADR 0007: a
  `team-events-compact-plan` (non-AI) writes one prompt per eligible
  month (embedding that month's day bullets) under
  `<team>/memories/team/.compact-staging/`; a Task sub-agent
  (subscription rail) writes a `<period>.card.md` with the month's
  summary bullets (target `month_max_chars`); `team-events-compact-apply`
  (non-AI) validates, replaces the day entries with one `month` entry
  atomically, and hard-trims an oversized card (oldest bullets first) —
  convergence is deterministic, a card is never accepted oversized.
- **Month → year**: same dance for `month` entries older than
  `year_after_days` (default 400 — a month stays readable for over a
  year before folding); a year's months collapse into one `year` entry
  (target `year_max_chars`).
- **Age, not size, is the trigger** for the tier folds (the directive's
  semantics); the size bound is a backstop. If the rendered projection
  is over `overflow_limit` *after* the age folds, the deterministic trim
  drops the oldest `year` bullets first — never the 30-day window, which
  is only ever bounded by real team activity.

### Sweep integration (who runs it, when)

Team-events compaction is **team-level, once per completed sweep**: the
driver runs it while still holding the claim, after every persona is
done and `remaining == 0`, immediately before `sweep-complete`. A
capped wake (`remaining > 0` → `sweep-release`) skips it — the wake
that finishes the roster does the fold. Holding the claim makes the
team-store mutation race-free by construction; `targets: []` (nothing
aged out) is the common case and costs one non-AI CLI call.

### Config

New `memory.team_events` block (all keys optional; team defaults
pattern applies):

```yaml
memory:
  team_events:
    recent_days: 30        # daily entries younger than this never compact
    year_after_days: 400   # month entries older than this fold into years
    month_max_chars: 700   # target size of one compacted month
    year_max_chars: 1000   # target size of one compacted year
    max_length: 8000       # rendered events.md bounds (backstop, hysteresis)
    overflow_limit: 12000
    enabled: true          # false: contract keeps the section, ingest drops it
```

Per-persona configs on one team must agree on these (they describe one
shared file); the team-defaults inheritance makes that the natural
state. The values used at plan time are the planning config's — the
sweep driver's own persona config.

## Consequences

- The card contract moves v2 → v3 (four markers). Staged v2
  `.extract.md` cards become unparseable: **quiesce sweeps before
  rollout** (complete or release, staging dirs drained) — same
  procedure as the ADR 0007 rollout. Un-swept transcripts are
  unaffected (cursors live outside the stores).
- Lockstep updates required: `extract_memory.md` prompt template, the
  sweep-protocol doc, the bundled `sweep-memory` skill, and
  team-installed skill copies.
- A brand-new team dir (`memories/team/`) appears on first ingest —
  no migration needed; absence of the dir simply means no events yet.
- Personas gain a cheap answer to "who worked on this before?" at zero
  session-start cost — the log is pointer-only until opened.
- The extractor writes events *about* a persona from that persona's own
  transcript, so attribution is structural (the sweep already knows
  whose session it is) — the summarizer never guesses names.

## Amendment (2026-08-02): audit hardening

A four-lens adversarial audit of the whole memory system (concurrency,
pipeline data-loss, bounds/convergence, protocol drift) caught two
design errors in the original decision, fixed as follows:

- **The size backstop measures the folded tiers only** (month + year
  sections). The original measured the whole rendered file while
  forbidding day-section drops — at ordinary activity (~3 bullets/day
  team-wide) the file permanently exceeded the bound, the backstop
  deleted *every* folded summary, and still reported over-bound forever.
  Scoped to the folds it is always convergent; the day window is bounded
  by real activity plus a **hard cap of 3 events per ingested card**
  (`MAX_EVENTS_PER_APPEND`, enforcing the contract's 0–3 ask).
- **Defaults resized for the real month inventory.** Up to ~26 month
  sections legitimately coexist before a year fold at
  `year_after_days=400`; at `month_max_chars=700` that needs ~19k chars,
  so `max_length`/`overflow_limit` moved 8000/12000 → **24000/30000**,
  and the loader now warns when a config's fold budget cannot fit under
  its `max_length`.
- Apply hardening: snapshot survival compares **normalized** keys (a
  `(xN)` count bump between plan and apply no longer resurrects a folded
  bullet); a re-apply after a crash **merges into** an existing
  same-period section (deduped) instead of appending a duplicate; a held
  lock skips just that target (`locked` in the report) instead of
  aborting the run; a fully-clean apply consumes the manifest so a blind
  re-apply gets the loud exit 2.
- Appends run with ~2s of lock retries (was 0.5s), and a contended
  append is still dropped by design — the log is awareness, not the
  ledger of record.

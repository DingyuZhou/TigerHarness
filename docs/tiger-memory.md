# tiger-memory

## At a glance
- **What:** per-persona persistent memory built on **three bounded,
  self-pruning stores** — `skills`, `must_remember`, `topics` — with an
  index-only session-start briefing, in-persona extraction, and a staged
  compaction flow (forgetting is first-class).
- **When you need it:** configuring or inspecting a persona's memory. For the
  team-wide refresh protocol see
  [tiger-memory-sweep-protocol.md](tiger-memory-sweep-protocol.md); for the
  rationale see [DESIGN-memory.md](DESIGN-memory.md) and
  [ADR 0007](adr/0007-topic-store-revamp.md).
- **Must-not-miss:** stores are **bounded** and forgetting is real (a
  forgotten topic leaves only git history / `retired/` copies behind). Never
  hand-edit a store file; let the sweep + compaction prune it. Length is
  measured in **characters**, never tokens. Session start loads **indexes
  only** — open a detail file only when its index line matters.

## What it does

Turns an agent's *finished* conversation sessions into entries in three
bounded per-persona stores, in-character, then assembles a session-start
briefing the agent reads at the start of each session. Because every
surface is bounded, crossing its `overflow_limit` ("must compact") makes it
a mandatory target for **staged compaction** on the next sweep, which
merges, tightens, and forgets to bring it back under `max` — so memory
self-prunes instead of growing forever.

## Architecture

```
Sources (Claude transcripts, Slack-bridge threads, journal worklogs)
    |
    v
Extraction (lifecycle.py): a finished session -> a strict
  @@SKILLS@@ / @@MUST_REMEMBER@@ / @@TOPICS@@ bundle, in-persona
  (the prompt embeds the persona's current topic routing list
   + must-remember items, so the bundle can TOUCH what it relied on)
    |
    v
Ingest (executor.py + lifecycle.py): bundle blocks -> the three stores
  (topics route to an existing slug or mint a NEW topic; TOUCH blocks
   refresh existing must-remember items' freshness)
    |
    v
Compaction (compaction.py), staged, ONLY for surfaces at/over their
  overflow_limit: compact-plan (non-AI) -> card sub-agents ->
  compact-apply (non-AI, deterministic convergence)
    |
    v
Stores (journal/skills.md + must_remember.md + topics.md)
    |
    v
Briefing (briefing.py): must_remember + skill INDEX + topic INDEX
  + detail files on demand + unprocessed-session notice
    |
    v
Agent reads the three small index files at session start
```

## The three bounded stores

Each surface has a **two-number bound** (`max` + `overflow_limit`, both in
characters) giving **hysteresis**: it may drift up to `overflow_limit`, and
only *then* is a compaction staged that brings it back under `max` —
preventing compact-every-session thrash.

| Store | Holds | Loaded at session start | Bounds (chars, defaults) |
|---|---|---|---|
| `skills` | learned, reusable lessons (name + trigger + procedure) | the **index only**; per-skill detail files (`briefing/skills/`) on demand | index 2000 / 3000; per-skill detail 4000 / 6000 |
| `must_remember` | external directives (`operator_explicit` / `preference` / `decision` / `incident`) | whole store (kept small) | 2000 / 3000; `forget_days` 30 |
| `topics` | durable project knowledge, filed by subject; dated detail bodies | the **index only** (freshest first); per-topic detail files (`briefing/topics/`) on demand | index 4000 / 6000; per-topic detail 4000 / 6000; `fresh_days` 7, `forget_days` 60 |

On-disk, all three are YAML-frontmatter entry stores under the persona
store's `journal/` dir (`skills.md`, `must_remember.md`, `topics.md`). The
indexes and detail files the persona actually reads are **projections**,
regenerated deterministically from the stores by `rebuild` — bounds are
measured over exactly those rendered strings. Don't hand-edit any of it.

Topic lifecycle in one paragraph: extraction **routes** new knowledge into
an existing topic (appending a dated bullet to its detail body, bumping its
touch count and freshness) or creates a new one; the index orders topics
most-recently-touched first; a topic touched within `fresh_days` is
protected from forget/merge; a topic untouched for `forget_days` is dropped
deterministically (oldest first, until the index is back at or under
`max`) once the index is at/over its `overflow_limit`; and
compaction may merge near-duplicate topics or tighten summaries.

## Key modules

| Module | Purpose |
|---|---|
| `cli.py` | CLI — see "CLI verbs" below |
| `config.py` | YAML config loading + validation (the `memory:` block) |
| `entries.py` | the three entry schemas (`SkillEntry` / `MustRememberEntry` / `TopicEntry`) + `topic_slug` + frontmatter bridge |
| `bounded_store.py` | crash-safe store I/O, index/detail/entry character measurement, overflow detection, per-store lock, the guarded `forget` |
| `indexes.py` | pure renderers: skill/topic index, detail files, the topic routing list — the single source of what a persona loads |
| `check.py` | the `check [--fix]` format gate + quarantine |
| `skills.py` | usage-based skill importance + skills keep-rank |
| `lifecycle.py` | extraction + routing ingest, in-session staging (plan / stacks / map-reduce), `rebuild`, `pin`, charter mission sourcing |
| `executor.py` | staged-card ingest glue (`IngestResult`: skills / must_remember / topics added + `touched` — must-remember items refreshed by `TOUCH:` blocks) |
| `compaction.py` | staged compaction: `compact-plan` / `compact-apply`, deterministic convergence, protections |
| `migrate_topics.py` | one-off `migrate-to-topics` (diary/fuzzy retirement) |
| `sweep.py` | team-sweep gating (claim / done / complete / release) |
| `cursor.py` | per-session incremental-sweep cursors (ADR 0006 Part 2) |
| `briefing.py` | assemble the session-start briefing (indexes + details + notice) |
| `sources/` | source adapters. Live on the sweep path: `ClaudeTranscriptAdapter` (both `claude_code` and, via the threads.json map, `slack_thread`) and `JournalWorklogAdapter`. `DocsAdapter` exists but `lifecycle._build_adapters` drops `docs` sources; `auto_memory` has no adapter at all |
| `summarizers/` | the prompt templates (`prompts/default/v1/`) + pluggable in-process backends |
| `state.py` | per-store JSON state snapshot |

## Configuration

A YAML config file (pointed to by `$TIGER_MEMORY_CONFIG` or `--config`).
The annotated reference copy is
[`examples/tiger-memory.config.yaml`](../examples/tiger-memory.config.yaml).

```yaml
agent:
  name: MyAgent
  role: A helpful assistant
  pronouns: they/them

store:
  root: ./memory/   # auto-appends the agent slug; the 3 stores live under journal/

sources:
  - kind: claude_code
    project_path: ~/.claude/projects/-my-project/
    # `~` is expanded. `project_path: auto` derives the transcripts dir
    # from the team root at runtime (standard layout: the config lives at
    # <team>/memories/<persona>/tiger-memory.config.yaml) -- use it to keep
    # per-persona configs free of machine-specific absolute paths.
    # Optional, for multi-persona Slack-bridge setups:
    # persona: ayako                    # only ingest sessions owned by this persona
    # include_unattributed: false        # opt-in to also include local claude-p sessions

summarizer:
  backend: anthropic
  model: claude-opus-4-7
  prompts: default/v1      # the prompt-template tree (extraction + compaction)

# The three bounded stores (every key optional — these are the defaults).
memory:
  length_unit: characters        # CONFIRMED: characters, never tokens
  skills:
    index_max_length: 2000
    index_overflow_limit: 3000
    detail_max_length: 4000
    detail_overflow_limit: 6000
  must_remember:
    max_length: 2000             # chars; loads whole, so kept small
    overflow_limit: 3000
    forget_days: 30              # untouched (no TOUCH) for => forget-eligible (>= 0)
  topics:
    index_max_length: 4000       # raised 2026-08-02: ~160 chars/topic
    index_overflow_limit: 6000
    detail_max_length: 4000
    detail_overflow_limit: 6000
    fresh_days: 7                # touched within => protected from forget/merge
    forget_days: 60              # untouched for => forget-eligible (>= fresh_days)
  team_events:                   # the team-wide event log (ADR 0008)
    enabled: true
    recent_days: 30              # daily sections younger than this never compact
    year_after_days: 400         # a month folds into its year this long after year end
    month_max_chars: 1200        # target size of one folded month section
    year_max_chars: 1000         # target size of one folded year section
    max_length: 40000            # size backstop over the FOLDED tiers (month+year)
    overflow_limit: 50000

memory_extract:                  # per-section word budgets for extraction
  skill_procedure_words: 120
  memo_words: 25
  topic_summary_words: 25
  topic_detail_words: 80
  team_event_words: 15
  max_output_words: 600

# Team-sweep gating (optional; these are the defaults). Tunable per
# team since 2026-08-02 -- e.g. floor_hours: 12 halves how stale
# memory is when a single-operator day starts.
sweep:
  floor_hours: 24
  max_personas: 3
  lease_seconds: 1800

rebuild:
  trigger: lazy
  idle_threshold_hours: 1
```

Validation is fail-fast at load: `length_unit` must be `characters` (token
units rejected); every bound pair must satisfy `0 < max < overflow_limit`;
`must_remember.forget_days >= 0`; for topics `fresh_days >= 0` and
`forget_days >= fresh_days`.

## Usage

```bash
# Initialize the memory store + validate config
tiger-memory --config my-config.yaml init

# Fresh-start rebuild: drop the retired legacy surface (first run), run the
# format gate, regenerate the session-start briefing (indexes + details)
tiger-memory --config my-config.yaml rebuild

# Pin a must_remember entry directly.
# NOTE: --kind defaults to operator_explicit — the most
# forget-protected kind. A bare `tiger-memory pin "..."` therefore writes an
# operator_explicit directive; pass `--kind preference` for an ordinary note.
tiger-memory --config my-config.yaml pin "Operator prefers tabular diffs" --kind preference
tiger-memory --config my-config.yaml pin "Never force-push main" --kind operator_explicit

# JSON snapshot of the three stores (count / chars / max / over_overflow)
tiger-memory --config my-config.yaml state
```

Extraction and compaction are driven by the team sweep, not by hand — see
the [sweep protocol](tiger-memory-sweep-protocol.md). The in-session
executor verbs (`plan` / `ingest-extraction` / `build-reduce-prompts` /
`ingest-staged` / `compact-plan` / `compact-apply`) are driven by the
`sweep-memory` skill, not run manually.

## CLI verbs (the live set)

| Verb | What it does |
|---|---|
| `init` | create the empty store + validate the config |
| `rebuild` | fresh-start: drop the retired legacy surface (first run), run `check --fix` as the per-persona format gate, regenerate the briefing |
| `pin <memo> --kind <k>` | write one `must_remember` entry. `--kind` **defaults to `operator_explicit`** (the most forget-protected kind), so a bare `pin` is a protected directive — pass `--kind preference` for an ordinary note |
| `migrate-to-topics [--apply]` | **one-off, idempotent** (ADR 0007): retire `diary.md` / `fuzzy.md` / `emotional.md` (+ `.rejected` sidecars) from `journal/` to `<root>/retired/` and create an empty `topics.md`. **Dry-run is the default** (preview only); `--apply` performs it |
| `state` | JSON snapshot of the three stores: per store `count`, `chars` (rendered-index chars for skills/topics, entry chars for must_remember), `max`, `over_overflow`, plus `details_over_overflow` for skills/topics |
| `plan [--max-sessions N]` | stage one extraction prompt per idle, unprocessed transcript + a manifest (items + stacks); a **still-active** session is also staged once its post-cursor prefiltered slice exceeds `budgets.active_slice_threshold_chars` (cut at a whole-turn boundary, holding back the live tail turn whenever a completed boundary exists; ADR 0006 Part 2). Each prompt embeds the persona's current topic routing list and must-remember item list (for `TOUCH:` blocks) |
| `ingest-extraction --uuid <u>` | write back ONE sub-agent's extraction bundle (stdin) for a planned uuid |
| `build-reduce-prompts` | reduce step (ADR 0006 Part 1): assemble `<uuid>.prompt.md` from a map_reduce item's staged chunk digests |
| `ingest-staged` | glue every staged `<uuid>.extract.md` card in ONE process (race-free). Exit 0 clean / 1 ≥1 malformed card / 2 no plan manifest |
| `compact-plan` | non-AI: run the deterministic stale-topic forget, then stage one compaction prompt per surface at/over its `overflow_limit` under `.compact-staging/` + `manifest.json` (empty `targets` = nothing to do) |
| `compact-apply` | non-AI: validate + apply every staged `<key>.card.md` in ONE process; deterministic convergence trim; protected content (operator-explicit, fresh topics) never force-dropped. Exit 0 clean / 1 ≥1 malformed card / 2 no compaction manifest |
| `card-check <card>` | non-AI, **read-only ruler for card authors**: resolve `<card>` through the `manifest.json` beside it, parse + merge through the exact `compact-apply` code path (post-merge store size for `must_remember` / `skills` / `topic_roster` against the live store, rendered detail for `topic_detail` / `skill_detail`, apply-time bullet accounting for team-events `month`/`year` fold cards), and report `{chars, max, over_by, fits}` — the pre-trim answer to "does my draft fit?". Exit 0 fits / 4 over-bound / 1 malformed card / 2 no card, no manifest, or not a staged target |
| `team-events-compact-plan` | non-AI, **team-level** (ADR 0008): run the size backstop, then stage one fold prompt per aged-out team-event period under `memories/team/.compact-staging/` (empty `targets` = nothing aged out) |
| `team-events-compact-apply` | non-AI: validate + apply every staged team-events fold card in ONE process (deterministic trim; post-plan appends survive). Exit 0 clean / 1 ≥1 malformed card / 2 no manifest |
| `sweep-plan` / `sweep-done` / `sweep-complete [--token]` / `sweep-release [--token]` | team-sweep gating (non-AI). `sweep-done` renews the claim lease and stamps the durable per-persona `done_at` map (the roster walk is least-recently-swept first); with `--token`, complete/release are refused (exit 3) when another session now owns the claim |
| `search <term> [--team] [--store S]` | case-insensitive content search over the stores (and the team event log); `--team` walks every roster persona. The read half of the Operator find-it/fix-it loop |
| `forget --store S (--id I \| --slug SLUG)` | Operator-authority removal of one entry (locked RMW; the removed block is archived to `journal/<store>.forgotten.md`, never silently lost; may drop even a fresh `operator_explicit` — the Operator IS the authority the protection serves). Rebuilds the briefing |
| `doctor [--json]` | team-wide health table: per-persona bounds/overflow, staged files, quarantines, sweep `done_at`, data-through, last still_over/malformed reports, cross-persona topic-slug collisions. Exit 1 when anything is flagged (cron-friendly) |
| `check [--fix]` | validate the 3 stores' on-disk format; exit non-zero if any invalid. `--fix` repairs mechanical drift + quarantines non-mechanical to `<store>.rejected.md` (no silent loss). Runs automatically inside every `rebuild` |

The retired verbs `import-legacy` and `migrate-emotional-to-diary` were
removed by ADR 0007 along with the diary/fuzzy stores; the older rollup-era
verbs (`bootstrap`, `search`, `drill`, `tree`, `raw`, `resummarize`,
`ingest-summary`) are long gone.

## The extraction contract

Extraction turns one finished session into a strict bundle with four
whole-line section markers, in order (contract v3 — `@@TEAM_EVENTS@@`
added by [ADR 0008](adr/0008-team-event-log.md); see
`summarizers/prompts/default/v1/extract_memory.md`):

```
@@SKILLS@@
<skill blocks, or NONE>
@@MUST_REMEMBER@@
<must-remember blocks, or NONE>
@@TOPICS@@
<topic blocks, or NONE>
@@TEAM_EVENTS@@
<EVENT: lines, or NONE>
```

A skill block is `NAME:` / `TRIGGER:` / `PROCEDURE:`; a must-remember block
is `KIND:` / `MEMO:`.

**Must-remember freshness (TOUCH).** The prompt also embeds the persona's
current must-remember items — one line per item: id, kind, memo (filled
into `{must_remember_index}`). If the session's work *related to* an
existing item (the extractor followed it, it constrained the work, the
subject came up again), the `@@MUST_REMEMBER@@` section carries a touch
block instead of a re-emitted memo:

```
TOUCH: <id from the embedded list>
```

Zero or more `TOUCH:` blocks, mixed freely with the `KIND:`/`MEMO:`
blocks. Ingest refreshes a touched item's `last_used` (and bumps its
`repeat_count`); unknown ids are ignored. Touches are counted in
`IngestResult.touched` (not in `total_added`). An item untouched for
`must_remember.forget_days` (default 30) becomes forget-eligible at
compaction.

A topic block routes durable project knowledge —
the prompt embeds the persona's existing topics (freshest first) and asks
the extractor to **route to an existing topic whenever one fits**:

```
TOPIC: <an existing slug from the embedded list, or exactly NEW>
NAME: <required when TOPIC is NEW>
SUMMARY: <required for NEW; for an existing topic only when the old summary no longer fits>
DETAIL: <the new durable facts from THIS session — always required>
```

Existing slug → the detail is appended (dated) to the topic body, its
freshness and touch count update. **All ingest dating is
source-dated** (2026-08-02): entry `created_at`/`last_used`, topic
section headings, and team-event days carry the session's END
timestamp, not the sweep's wall clock — a backlog sweep files
history under when the work happened, and an old session's touch
never moves an already-fresher entry backward. `NEW` → a topic is minted. A malformed
*bundle* (missing or out-of-order markers, or a **duplicated standalone
marker** — a bundle must emit each marker exactly once; a duplicate,
e.g. an echoed contract sample, makes the split ambiguous and the whole
bundle MALFORMED) is rejected before any write; an
individual malformed *block* is skipped. `NONE` for a store is a valid,
expected outcome — most sessions add little.

The `@@TEAM_EVENTS@@` section carries 0–3 `EVENT: <verb-first, past
tense, ≤ ~15 words>` lines — what the persona actually DID this session,
without its own name (ingest prefixes it — attribution is structural).
EVENT items are parsed line-wise (no blank line needed between them);
they feed the team-wide event log below, never a persona store.

## The team event log (ADR 0008) — lazy, team-wide, self-compacting

One file per team, beside the per-persona stores:
`<team>/memories/team/events.md` — a dated who-did-what ledger:

```
## 2026-08-01
- Anzai planned the QA pass for the topic store.
- Ayako reviewed PR #12. (x3)

## 2026-07
- Mitsui shipped the reliability fixes for the sweep planner.
```

- **Write path:** each persona's `ingest-staged` appends its bundle's
  `EVENT:` lines under the session's END day (name prefixed, exact
  same-day repeats collapse to `(xN)`), under a cross-persona file
  lock, capped at 3 events per card. A lock held through the ~2s
  retry window drops that append with a logged warning — the log is
  awareness, not the ledger of record (the persona stores captured
  the session either way).
- **Read path — LAZY only:** no briefing ever loads it; the briefing
  README carries a pointer. Open it only when a session needs
  cross-team awareness ("who touched X before?").
- **Compaction — age-tiered, staged:** day sections whose whole month
  is older than `recent_days` (default 30) fold into one `## YYYY-MM`
  section; month sections whose year ended more than `year_after_days`
  (default 400) ago fold into one `## YYYY` section. Folds are staged
  by `team-events-compact-plan` (non-AI), carded by Task sub-agents
  (subscription rail, strict `@@TEAM_EVENTS@@` bullet contract), and
  applied by `team-events-compact-apply` (non-AI; bullets appended
  between plan and apply survive; an oversized card is hard-trimmed).
  A deterministic size backstop (`max_length` / `overflow_limit`,
  measured over the FOLDED tiers only — month + year sections) drops
  the oldest year/month sections; the daily window is exempt from
  both the measurement and the drops (bounded by real activity plus
  the per-append cap), so the backstop is always convergent. The driver runs the fold once per completed sweep,
  before `sweep-complete`, while still holding the claim.
- **Config:** the `memory.team_events` block — `enabled`,
  `recent_days`, `year_after_days`, `month_max_chars`,
  `year_max_chars`, `max_length`, `overflow_limit` (all optional;
  keep them team-uniform via the team defaults file). The extraction
  word budget is `memory_extract.team_event_words` (default 15).

## Compaction (staged — replaces meditation)

When a surface crosses its `overflow_limit`, the sweep stages a compaction
(same subscription-rail shape as extraction; no in-process model call):

1. **`compact-plan`** (non-AI) first runs the deterministic stale-topic
   pre-pass — only once the topic index is **at/over its
   `overflow_limit`** (hysteresis), topics stale beyond `forget_days`
   drop oldest-first until the index is back at or under `max` (no AI
   needed for "not refreshed in months") — then writes one prompt per
   still-over surface under `.compact-staging/`: tighten `must_remember`
   (operator-explicit entries shown as protected, carried over verbatim),
   merge/forget `skills`, shrink the `topic_roster` (forget / merge /
   tighter summaries — fresh topics protected), or rewrite one
   `topic_detail` / `skill_detail` body under its `max`. In the
   must_remember prompt, an item untouched for more than
   `must_remember.forget_days` is annotated **`[forget-eligible]`** with
   its age — no sweep TOUCHed it — and the card is told to drop it unless
   it is still clearly valuable despite its age. **Deferral rule:** when
   the `skills` index or `topic_roster` target is staged, that surface's
   per-detail targets are deferred to the next sweep (a roster merge
   would clobber a same-run detail rewrite; a full index replacement
   would dangle it) — the oversized detail re-stages against the settled
   store.
2. **Card sub-agents** (Task tool, isolated context) each write one
   `<key>.card.md` per the prompt's embedded strict contract, then run
   `tiger-memory card-check <card>` — the deterministic, read-only ruler
   (same parse + merge code path as apply, incl. team-events fold cards)
   — and tighten the draft until it reports `fits`. This replaces the
   hand-counted generate→measure→trim loops that dominated sweep time.
3. **`compact-apply`** (non-AI) validates each card, applies it atomically
   to the entry store, and **guarantees convergence
   deterministically** — a surface still over `max` after its card is
   hard-trimmed by keep-rank/freshness, never accepted oversized. For
   must_remember the deterministic drop order is: stale normal entries
   first (oldest `last_used` first), then fresh normal entries by
   keep-rank (lowest recurrence/recency first), and only as the very
   last resort a *stale* `operator_explicit` directive (logged as a
   warning). A *fresh* `operator_explicit` is never dropped, and fresh
   topics are never force-dropped: a surface that cannot shrink without
   them is reported in `still_over` and retried next sweep. **Snapshot
   survival:** a replacement card (`must_remember` / `skills`) replaces
   only the plan-time snapshot its prompt saw (the manifest's
   `snapshot_ids`) — entries written between plan and apply survive; and
   a kept memo (matched by kind + normalized text) or kept skill
   (matched by name) inherits its predecessor's id and
   freshness/usage signals, so compaction never resets the TOUCH clock
   or keep-rank of a survivor. Malformed
   cards are reported and kept (exit 1); applied prompt+card files are
   deleted.

## Session start (the briefing)

`tiger-memory rebuild` assembles `briefing/`:

- `README.md` — the read order + rules (read this first);
- `UNPROCESSED.md` — the unprocessed-session notice: memory is built only
  after a session goes idle, so a still-active session may not be reflected
  yet. **Rule:** if the Operator references something you don't recognise,
  check this memory first, then check for unprocessed/active sessions,
  before claiming ignorance;
- `must_remember.md` — the whole (small) directives store;
- `skill_index.md` — one line-block per skill (name + trigger); the full
  procedure lives in `briefing/skills/<slug>-<id>.md`, read on demand;
- `topic_index.md` — one block per topic (name, slug, freshness, summary),
  **freshest first**; the dated detail lives in `briefing/topics/<slug>.md`,
  read on demand;
- `MANIFEST.md` — the inventory.

**Initial load = the three small files only** (`must_remember.md`,
`skill_index.md`, `topic_index.md`) plus the notice. Never load every
detail file "just in case" — the index tells you which ones matter. The
briefing is assembled atomically (temp-dir swap) with a fingerprint no-op
shortcut over the three journal stores.

## Migrating a persona from the 4-store (diary/fuzzy) model

ADR 0007 retired the `diary` and `fuzzy` stores. For each existing persona
store:

```bash
tiger-memory --config <persona-config> migrate-to-topics            # dry-run (preview)
tiger-memory --config <persona-config> migrate-to-topics --apply    # perform it
tiger-memory --config <persona-config> rebuild                      # regenerate briefing
```

- **Dry-run is the default**; `--apply` moves `diary.md` / `fuzzy.md` /
  `emotional.md` (+ `.rejected` sidecars) from `journal/` to
  `<root>/retired/` — nothing loads them any more, but the content stays on
  disk (and in git history) — and creates an empty `topics.md`. Idempotent:
  a re-run is a no-op.
- `must_remember.md` stays in place; a store near the old 8000-char bound
  will be aggressively compacted down to the new 2000/3000 on its first
  over-bound compaction — that is the intended shrink, and fresh
  `operator_explicit` entries keep their forget protection.
- Old config keys for the retired stores (`memory.diary.*`,
  `memory.fuzzy.*`) no longer do anything (unknown `memory:` keys are
  ignored by the loader) — remove them to keep configs honest.
- **Quiesce sweeps before rolling out the new code**: staged `.extract.md`
  cards in the old v1 (`@@DIARY@@`) format are unparseable by the new
  ingest. Complete or release any in-flight sweep and drain the staging
  dirs first; un-swept transcripts are unaffected (the next sweep stages
  v2 prompts).

`tigerharness init` already scaffolds new personas on the topic model, so
this migration only applies to stores created before ADR 0007.

## Per-persona filtering (multi-bridge integration)

When the slack-bridge runs in **multi-persona mode** (one Slack app routes
DMs to N team members), each persona has its own memory store and only wants
to extract **its own** conversations — Ayako's memory shouldn't include
Sakuragi's threads.

Tell the source adapter which persona owns the conversation by adding a
`persona:` field to the `claude_code` source:

```yaml
sources:
  - kind: claude_code
    project_path: ~/.claude/projects/-home-tigerleap-projects-teams-shohoku/
    persona: ayako               # only ingest sessions owned by Ayako
    # include_unattributed: false  # default: exclude local claude-p sessions
  - kind: slack_thread
    threads_json: ~/.local/state/slack-bridge/shohoku/threads.json
```

The bridge writes `{thread_ts: {session_id, persona}}` entries to
`threads.json` as users DM the bot. The adapter reads that mapping and only
emits records where the stored persona matches.

**Three sessions, three outcomes:**

| Session origin | `threads.json` entry | Strict mode (default) | `include_unattributed: true` |
|---|---|---|---|
| Slack DM addressed to Ayako | `{session_id, persona: "ayako"}` | ✅ in Ayako's memory | ✅ in Ayako's memory |
| Slack DM addressed to Sakuragi | `{session_id, persona: "sakuragi"}` | ❌ excluded | ❌ excluded |
| Local `claude -p` (not via bridge) | not in threads.json | ❌ excluded | ✅ in Ayako's memory |
| Pre-routing Slack thread (PR1-era) | `"session_id"` bare string | ❌ excluded | ✅ in Ayako's memory |

**Backward compat:** if `persona:` is omitted, the adapter emits every
session (legacy single-tenant behavior, unchanged). The threads.json reader
also accepts the pre-routing schema (bare `"session_id"` strings) so older
state files still work.

**`persona=None` semantics.** Both "not in `threads.json` at all" (local
`claude -p` session) and "present but with `persona=None`" (pre-routing
entry) are treated as *unattributed*. Under strict mode (default) they're
excluded from every persona's memory; `include_unattributed: true` brings
them in. There's no way to disambiguate "local work" from "stale pre-routing
thread" via config — migrate old `threads.json` entries by hand, or wait for
them to age out of the discovery window.

## Per-persona journal memory (journal_worklog source)

When the team runs work through the subscription backend's `drive-journal`
driver, all of it happens inside **one** Claude session (the driver's). The
driver adopts each persona in-session, so the raw transcript would collapse
every persona's work into the driver's store. The `journal_worklog` source
fixes that by ingesting the journal's **per-turn worklog records** instead —
each one stamped (by harness code) with the persona that actually did the
work. See [`per-persona-journal-memory.md`](per-persona-journal-memory.md)
and [`subscription-backend.md`](subscription-backend.md) for the write side.

Add the source to each journal-working persona's config:

```yaml
sources:
  - kind: journal_worklog
    journal_root: <team-root>/journal/   # e.g. teams/Shohoku/journal/
    persona: Rukawa             # only ingest worklog entries stamped Rukawa
    # team: Shohoku             # optional; defaults to the journal root's parent dir name
  - kind: claude_code
    project_path: ~/.claude/projects/-home-tigerleap-projects-teams-shohoku/
    persona: Rukawa
```

The adapter discovers `*/worklog/*.md` under `journal_root` (both `active/`
and `done/`), reads the frontmatter, keeps only entries whose `persona`
matches, and groups them **per `(task, persona)`** — "Rukawa's memory of
task X" — with a stable `uuid5("journal:<team>/<task>/<persona>")` so the
extraction grows in place as the task does. Individual turn files remain the
drill-down detail.

**Double-count suppression.** A drive session's own (fat) transcript would
otherwise be folded whole into the driver's store, double-counting work the
worklog already captured. At `journal claim`, the drive's Slack `thread_ts`
is recorded to `journal/.drive-sessions.json`; the `claude_code`
(`ClaudeTranscriptAdapter`) source reads that registry — wired in
automatically when a `journal_worklog` source is present in the same config
— and **skips** any session whose `thread_ts` is a registered drive. The
registry reader is tolerant: a missing/corrupt registry suppresses
**nothing** (the safe direction).

**Roster prerequisite.** Every persona that does journal work needs its own
tiger-memory config + store listing this source; otherwise the team-sweep
has nothing to extract for it. The team-sweep enumerates **all** roster
personas that have a store (it does not activity-gate per persona), so a
specialist with *only* worklog activity and no Slack threads is still swept —
its new worklog entries surface at `tiger-memory plan` time through this
source. See [`tiger-memory-sweep-protocol.md`](tiger-memory-sweep-protocol.md).

## The auto_memory and docs source kinds (config-only, no live adapter)

The config validator also accepts `auto_memory` and `docs` as source
kinds — **for forward-compatibility only**. Neither carries a live
adapter on the sweep path: `lifecycle._build_adapters` builds adapters
only for `claude_code` (which also covers `slack_thread` sessions via
the threads.json map) and `journal_worklog`; a configured `auto_memory`
or `docs` source is silently inert (a `DocsAdapter` class still exists
in `sources/` but nothing constructs it). Listing them does not break a
config, but they contribute nothing to extraction — use the live source
kinds above.

## Adding a new summarizer vendor

The production sweep path is model-free glue — its AI steps run as staged
Task sub-agents, so no summarizer backend is invoked there. The pluggable
summarizer registry remains for the in-process convenience path
(`extract_and_ingest`) and tests. The `anthropic` backend is pre-registered;
plug in any other vendor in three steps:

**1. Implement `Summarizer`** (see [`summarizers/base.py`](../src/tigerharness/tiger_memory/summarizers/base.py)):

```python
from tigerharness.tiger_memory.summarizers import Summarizer, SummarizerError

class OpenAISummarizer(Summarizer):
    name = "openai"
    version = "v1"

    def __init__(self, model: str, api_key: str):
        super().__init__()
        self.model = model
        self.api_key = api_key

    def summarize(self, *, prompt: str, max_words: int) -> str:
        # Call your vendor's API; return the markdown body.
        # Update self.cost_so_far from the API response if available.
        ...
```

**2. Register a factory:**

```python
from tigerharness.tiger_memory.summarizers import register_summarizer
from tigerharness.tiger_memory.config import SummarizerConfig

def _build_openai(cfg: SummarizerConfig) -> Summarizer:
    import os
    return OpenAISummarizer(model=cfg.model, api_key=os.environ["OPENAI_API_KEY"])

register_summarizer("openai", _build_openai)
```

Call `register_summarizer()` at import time — typically from your project's
top-level `__init__.py` or a startup hook.

**3. Use it in any persona's config:**

```yaml
summarizer:
  backend: openai
  model: gpt-4.1
  prompts: default/v1
```

If the backend name isn't registered, you get a `SummarizerError` listing
every registered backend. If your factory returns something that isn't a
`Summarizer` subclass, the registry catches that at lookup time and raises
`SummarizerError` with the actual type name.

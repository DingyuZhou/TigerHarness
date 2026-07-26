# ADR 0007 — Topic store revamp: indexes + detail files, diary/fuzzy retired

- Status: accepted
- Date: 2026-07-23
- Driver: Operator directive (Dingyu Zhou), executed by Anzai

## Context

The 4-store model (skills / must_remember / diary / fuzzy) kept persona
memory bounded, but it has two structural problems the Operator wants
fixed:

1. **Session-start load is too heavy.** The briefing loads full
   `must_remember.md` (8k chars), full `diary.md` (6k), `fuzzy.md` (4k)
   and the skill index — most of it irrelevant to the session at hand.
2. **The diary/fuzzy "personal feeling" rail earns less than it costs.**
   Weighted diary notes and the fuzzy coarsening pipeline add schema,
   scoring, migration and QA surface, while the useful residue —
   durable project knowledge — has no dedicated home.

Operator directive (2026-07-22, verbatim intent): shrink the skill
index and must_remember defaults to 1000/1500 chars; split each skill's
details into its own file; add a **topic** memory mode (index 1000/1500,
detail files 3000/4500) where the index alone is loaded at session
start; sweep routes new knowledge into existing topics or creates new
ones; recently-touched topics stay fresh; compaction shrinks index and
detail files at the must-compact bound, merges near-duplicate topics,
and forgets topics not refreshed for a while; **remove diary and fuzzy
and all related code**.

## Decision

### Store model (3 stores)

| Store | Durable state | Session-load surface | Default max / must-compact |
|---|---|---|---|
| `skills` | `journal/skills.md` (frontmatter entries) | rendered skill index (`briefing/skill_index.md`) + per-skill detail files (`briefing/skills/<slug>-<id>.md`) | index 2000 / 3000; detail 4000 / 6000 |
| `must_remember` | `journal/must_remember.md` (flat entry store) | loaded whole | 2000 / 3000 |
| `topics` | `journal/topics.md` (frontmatter entries; each entry's body is the topic's dated detail) | rendered topic index (`briefing/topic_index.md`) + per-topic detail files (`briefing/topics/<slug>.md`) | index 2000 / 3000; detail 4000 / 6000 |

(Bounds as amended 2026-07-23; the directive's original 1000/1500 +
3000/4500 proved too strict on the live roster.)

All bounds are characters (vendor-neutral, unchanged rule). `max` /
`overflow_limit` keep their existing hysteresis semantics: content may
drift above `max`; crossing `overflow_limit` ("must compact") makes the
surface a mandatory compaction target on the next sweep.

**Indexes and detail files are projections.** The three `journal/*.md`
frontmatter stores remain the only durable state — one entry substrate,
no dual-write consistency problem. The indexes and per-entry detail
files are rendered deterministically from them (`indexes.py`, single
source shared by the briefing and the bound measurement, so "the index
fits in N characters" means the same thing to the compactor, the
`state` snapshot, and the reader). An index over its bound means "too
many / too verbose entries", which only compaction (merge / forget /
tighten summaries) can fix.

- Skill index line: `- <name> — <trigger>` (+ detail file pointer).
  Ordered by importance (existing keep-rank).
- Topic index block: slug, `last:` date, touch count, one-to-two
  sentence summary. Ordered most-recently-touched first — freshness is
  the reader's default sort.

### Topic lifecycle

- **Ingest** (sweep card contract v2): the extraction prompt embeds the
  current topic index; the summarizer emits `@@TOPICS@@` blocks that
  either name an existing topic slug or `NEW`. Existing → the DETAIL
  text is appended (dated) to the topic's detail file, the SUMMARY (if
  given) replaces the index summary, `last_touched`/`touch_count`
  update. NEW → slug minted, detail file + index entry created.
- **Freshness**: `last_touched` orders the index; topics touched within
  `fresh_days` (default 7) are protected from forget/merge.
- **Forget**: a topic whose `last_touched` is older than `forget_days`
  (default 60) is eligible; when the index is over its bound, stale
  topics are dropped first (index entry and detail file together).
- **Merge**: compaction may fold two near-duplicate topics into one
  (union of details, one summary, newest `last_touched`, summed
  touches).

### Compaction is staged, like extraction (subscription rail)

The old `meditation.py` engine called an AI summarizer seam *in
process* — an API-billed path, which the team's billing model bans, and
which was in fact never wired into the CLI. It is replaced by a staged
flow mirroring plan/ingest-staged:

- `tiger-memory compact-plan` (non-AI): scans the three stores; for
  every surface over `overflow_limit` writes one prompt under
  `.compact-staging/` (rewrite this topic detail to ≤ max; shrink the
  topic roster by merge/forget/tighter summaries; tighten
  must_remember; merge/forget skills) plus a manifest. Deterministic
  pre-passes need no AI: topics stale beyond `forget_days` are dropped
  outright when the index is over `max`.
- Sub-agents (Task tool, subscription-billed) write one
  `<key>.card.md` card each (at the manifest's exact `card_path`), per
  the prompt's embedded strict
  contract.
- `tiger-memory compact-apply` (non-AI): validates each card, applies
  it atomically to the entry store, and **guarantees convergence
  deterministically** — a card still over `max` is hard-trimmed by
  keep-rank/recency, never accepted oversized. Malformed cards are
  reported and skipped (surface stays flagged; next sweep retries).
  compact-apply does **not** regenerate the briefing's rendered
  indexes — the sweep's subsequent `rebuild` step does.

The sweep skill's per-persona step becomes: `plan` → card sub-agents →
`ingest-staged` → `compact-plan` → compact sub-agents (if any) →
`compact-apply` → `rebuild` → `sweep-done`.

### Briefing / session-start load

`briefing/` now contains `README.md`, `UNPROCESSED.md`,
`must_remember.md` (small — bounded by the amended 2000/3000-char
must_remember limits), `skill_index.md`, `topic_index.md`,
and read-only copies of the detail files under `briefing/skills/` and
`briefing/topics/`. The README instructs: **initial load is the three
small files only**; open a detail file only when its index line is
relevant to the work at hand. That is the token-friendliness the
Operator asked for: bootstrap cost is O(indexes), not O(store).

### Removal

Deleted outright: `diary.py`, `diary_format.py`, `diary_finalize.py`,
`migrate_emotional_to_diary.py`, `regenerate_diary.py`,
`fuzzy_store.py`, `fuzzy_recompact.py`, `fuzz_select.py`,
`evocation.py`, `reinforce.py`, `meditation.py`, `import_legacy.py`
(legacy 2025-era import, diary-coupled, long since executed
everywhere), `DiaryEntry`, the `@@DIARY@@` contract section, the
`weight_cap` / decay / evocation config, and every diary/fuzzy branch
in `bounded_store` / `check` / `state` / `briefing` / `sweep` /
`lifecycle` / `cli`, plus their tests. The summarizer client seam
(`summarizers/anthropic.py`, `mock.py`, `base.py`) goes with them if
nothing else consumes it (the prompt-template loader stays).

### Migration (`tiger-memory migrate-to-topics`)

For each existing persona store: move `diary.md` and `fuzzy.md` (and
their quarantine sidecars, plus any legacy `emotional.md`) to
`<root>/retired/` (nothing loads them; git history and the retired
copies preserve the data); create an empty `topics.md`; leave
`skills.md` and `must_remember.md` in place — the entry format is
unchanged, the new index/detail surfaces are rendered projections, and
the tightened must_remember bound is enforced by the first over-bound
compaction, not by the migration. Then `rebuild`. Idempotent.

## Amendment (2026-07-23): must_remember freshness — the TOUCH mechanism

Operator follow-up after the first live rollout. must_remember needed the
same freshness signal topics get from routing:

- The extraction prompt embeds the persona's current must-remember items
  (id / kind / memo). A card's `@@MUST_REMEMBER@@` section may emit
  `TOUCH: <id>` blocks for items this session related to; ingest
  refreshes a touched item's `last_used` (and bumps `repeat_count`)
  instead of duplicating it.
- New `memory.must_remember.forget_days` (default 30): an item nobody
  touched for that long is **forget-eligible** when compaction needs the
  space. `compact-plan` annotates such items `[forget-eligible]` in the
  compaction prompt; the deterministic convergence trim drops stale
  normal items first (oldest untouched first), then fresh normal items
  by keep-rank, and a stale `operator_explicit` only as the very last
  resort (logged). A fresh `operator_explicit` is never dropped.

## Consequences

- Session bootstrap shrinks from ~4 full stores to ~3 small indexes.
- Project knowledge gets a durable, growing, self-pruning home
  (topics); the persona-voice diary rail is gone, per directive.
- The card contract changes (v2: `@@SKILLS@@` / `@@MUST_REMEMBER@@` /
  `@@TOPICS@@`); the sweep-protocol doc, the bundled sweep-memory
  skill (hash manifest!), and team-installed skill copies must move in
  lockstep.
- Old staged `.extract.md` cards in the v1 format become unparseable
  after deploy — sweeps must be quiesced (complete or released, staging
  dirs drained) before rollout. Un-swept transcripts are unaffected:
  cursors live outside the stores; the next sweep stages v2 prompts.
- must_remember stores currently near the old 8000-char bound will be
  aggressively compacted on first post-migration sweep; that is the
  intended shrink, and operator_explicit entries keep their existing
  forget-guard priority.

# tiger-memory

## At a glance
- **What:** per-persona persistent memory built on **four bounded,
  self-pruning stores** — `skills`, `must_remember`, `diary`, `fuzzy` — with a
  session-start briefing, in-persona extraction, and a meditation
  compaction engine (forgetting is first-class).
- **When you need it:** configuring or inspecting a persona's memory. For the
  team-wide refresh protocol see
  [tiger-memory-sweep-protocol.md](tiger-memory-sweep-protocol.md); for the
  rationale see [DESIGN-memory.md](DESIGN-memory.md).
- **Must-not-miss:** stores are **bounded** and **forgetting is
  irreversible** (no safety net). Never hand-edit a store file; let the
  sweep + meditation prune it. Length is measured in **characters**, never
  tokens.

Persistent memory management for Claude Code agents: four bounded stores,
a Python-rebuilt session-start briefing, and a meditation engine that
merges, relevance-downgrades, compacts, and forgets so memory stays focused
on what helps the team now.

## What it does

Turns an agent's *finished* conversation sessions into entries in three
bounded per-persona stores, in-character, then assembles a session-start
briefing the agent reads at the start of each session. Because every store
is bounded, crossing a store's `overflow_limit` triggers **meditation**,
which compacts and forgets to bring the store back under `max` — so memory
self-prunes instead of growing forever.

## Architecture

```
Sources (Claude transcripts, Slack threads, journal worklogs, docs)
    |
    v
Extraction (lifecycle.py): a finished session -> a strict
  @@SKILLS@@ / @@MUST_REMEMBER@@ / @@DIARY@@ bundle, in-persona
    |
    v
Ingest (bounded_store.py): bundle blocks -> the three stores
    |              \
    |               v
    |          Meditation (meditation.py), per store, ONLY when over
    |          overflow_limit: merge -> relevance-downgrade -> compact ->
    |          guarded-forget, back under max
    v
Stores (journal/skills.md + must_remember.md + diary.md)
    |
    v
Briefing (briefing.py): skill INDEX + full must_remember + full diary
  view + unprocessed-session notice
    |
    v
Agent reads briefing/ at session start
```

## The four bounded stores

Each store has a scalar, an ordering rule, and a **two-number bound**
(`max` + `overflow_limit`). The two numbers give **hysteresis**: a store may
drift up to `overflow_limit`, and only *then* does meditation fire and
compact it back under `max` — preventing meditate-every-session thrash.

| Store | Holds | Bound | Scalar / ordering |
|---|---|---|---|
| `skills` | learned, reusable lessons (name + trigger + procedure) | count (`max_count`) | `importance = log1p(usage_count)`; no time-decay; recency tie-break |
| `must_remember` | external directives (`operator_explicit` / `preference` / `decision` / `incident`) | characters (`max_length`) | importance = **reinforcement repeat-count** (recurrence); `operator_explicit` / `decision` protected until relevance-downgrade |
| `diary` | the persona's dated, weighted work-log | characters (`max_length`) | dated bullets `- (±N) note`; signed `weight` in `[-10, +10]` decays from the bullet date; ranked by `|weight|`; `fresh_days` window kept verbatim; **loaded whole** |
| `fuzzy` | coarsened, grouped older memory aged out of diary + must_remember | characters (`max_length`) | FREE TEXT (no entry schema); re-summarised each meditation so it **converges**; **loaded whole** |

On-disk, the three ENTRY stores are one markdown file each under the persona
store's `journal/` dir (`skills.md`, `must_remember.md`, `diary.md`), each entry a
YAML-frontmatter block + body. The **fuzzy** store (`fuzzy.md`) is free text, not
an entry list — it is written by meditation (steady state) and, one-off, by the
legacy→4-store **migration** that seeds it from the diary overflow. Don't
hand-edit these — meditation owns pruning, and **forgetting never deletes**: aged
items are coarsened into `fuzzy.md` (recoverable), not dropped.

The migration seed upholds the same *no-silent-loss* discipline. The dropped diary
overflow is ordered **newest-first** (so the fuzzy bound's tail-trim drops the
*oldest* gist, not the most recent). When the seed exceeds `fuzzy.max_length` and a
summarizer is supplied it is **coarsened** to fit (`fuzzy_recompact`, the
subscription-rail model call) — genuinely no-drop. The **default** (no summarizer)
is deterministic: it seeds raw, and if the seed still over-runs the bound the
residual IS trimmed — but **never silently**. `regenerate_store` records the
dropped-char count (`RegenResult.fuzzy_trimmed_chars`) and emits a per-persona
`WARNING`, so an operator always sees a partial loss at migration time instead of
discovering it later; a trimmed residual is recoverable from the migration task's
`regen` artifacts (it is gone from `fuzzy.md`). Net: with a summarizer the seed is
no-drop; without one it is never-silent. Verify by reading the seed tests in
`tests/tiger_memory/test_regenerate_diary.py`, or by running the migration and
grepping the logs for `... TRIMMED`.

### Verify the 4-store model

```bash
# 1. all four stores valid + bounded (skills, must_remember, diary, fuzzy):
tiger-memory --config <cfg> check          # exit 0 = green; --fix re-bounds fuzzy

# 2. the operator rename is complete (no stray legacy kind):
grep -rn owner_explicit src/tigerharness/tiger_memory   # only the legacy read-shim

# 3. fuzzy converges + nothing is hard-dropped: the named tests
#    (incl. the migration-seed no-silent-drop guarantee)
uv run python -m pytest tests/tiger_memory/test_meditate_persona.py \
    tests/tiger_memory/test_fuzzy_store.py \
    tests/tiger_memory/test_regenerate_diary.py
```

A store written before the `owner_explicit` -> `operator_explicit` rename still
loads: `entries.normalize_kind` maps the legacy value on read (no silent loss).

## Associative reinforcement (the recall-graph seed)

When a finished session produces a new **diary** note, the system can judge
whether that note *evokes* (联想 — "calls to mind") **0, 1, or at most 2**
existing memories — in any of the three sharp stores — and **reinforce** each
evoked old item so it is less likely to be forgotten, the way human memory
strengthens on associative recall. A **concise recall reference** to the evoked
item(s) is appended to the new note's text — a minimal, human-findable pointer
(`… ↪ recalls: skill "…"; diary 2026-06-19 "…"`), the seed of a memory-recall
graph. It is *not* a structured graph field and adds *no* id/schema/format change
to the compact diary store.

How an evoked item is reinforced:

| Evoked store | Reinforcement |
|---|---|
| `diary` | weight magnitude **+1 toward its existing sign**, clamped to `weight_cap` (a hub bullet saturates at ±cap), AND `last_used` reset to the evoking event's time — re-dating the bullet so its recency is restored |
| `must_remember` | `repeat_count += 1` (importance = repeat-count) |
| `skills` | `usage_count += 1` (importance re-derived, log-shaped — diminishing returns) |

The new note itself is **never** reinforced (no self-bump); only the old items it
evokes. The judgment is **one batched summarizer call per ingest** (all of that
ingest's new diary notes against the current stores as candidate context),
hooked between ingest and meditation — separate from meditation's merge (which
collapses near-duplicates; evocation keeps both and strengthens the old one).
Pure mutations live in `reinforce.py`; the pass + prompt/parse in `evocation.py`.

**Enabling it (a rail decision).** The pass is gated by
`memory.diary.evocation_enabled` (**default `false`**). Turning it on adds a
model call at ingest, so it is a deliberate, per-deployment choice: in the
in-process path (`extract_and_ingest`) it uses the session's summarizer; for the
staged production sweep, a summarizer must be threaded into `ingest_extraction`
(today `ingest-staged` is non-AI glue, so wiring the call there is the explicit
step that opts that path onto a model rail). With the flag off, ingest behaves
exactly as before.

The diary store's size was raised to `max_length` **6000** / `overflow_limit`
**8000** (from 4000/6000) alongside this feature, to give the references and
reinforced recency room before forgetting fires.

> Known limitation: a brand-new note byte-identical in text **and** weight **and**
> day to a pre-existing bullet can be mis-partitioned (the pass keys new-vs-old by
> that signature, since diary bullets have no id). The effect is benign
> misattribution, never a crash, and is vanishingly rare for free-text notes.

**Turning it on, concretely.** (1) Set `memory.diary.evocation_enabled: true`
(per-persona, or once in the team defaults so all personas inherit it). (2) Make a
summarizer reach the ingest path: the in-process path (`extract_and_ingest`)
already runs the pass when the flag is on; the **staged** production path
(`ingest-staged` → `ingest_extraction`) only runs it when a built summarizer is
passed in — wiring that call is the explicit opt-in that moves staged ingest onto
a model rail. With the flag off (default), neither path calls the model.

**Verifying.** The behaviour is unit-pinned in `tests/tiger_memory/test_reinforce.py`
(the mutations + reference), `test_evocation.py` (the batched pass + every
error/clamp branch), `test_evocation_wiring.py` (both drivers, flag on/off), and
`test_b2_evocation_qa.py` (the reference survives the format gate; counts stay
bounded). `tiger-memory check` stays exit-0 across all four stores (the diary
format is unchanged). Because the feature is default-off, a verifier sees **no**
change until they enable the flag and supply a summarizer (the real backend, or a
deterministic stub in tests).

## Key modules

| Module | Purpose |
|---|---|
| `cli.py` | CLI. Writers: `init`, `rebuild`, `pin`, `import-legacy` (one-off legacy seed). Reader: `state`. In-session executor (subscription rail): `plan` (stage extraction prompts + pack stacks), `ingest-extraction` (one bundle via stdin), `ingest-staged` (single-process glue of all `.extract.md` cards). Team-sweep gating: `sweep-plan`, `sweep-done`, `sweep-complete`, `sweep-release` |
| `import_legacy.py` | the one-off legacy import: reader (`read_legacy`), persona-driven re-author (`reauthor`), backdated seeding scorer (`score_seed_candidates`), seed-writer + idempotency guards (`seed_entries` / `already_imported` / `mark_imported`), orchestrator (`import_legacy_run`) |
| `config.py` | YAML config loading + validation (the `memory:` block) |
| `entries.py` | the three entry schemas + validation + frontmatter bridge |
| `bounded_store.py` | crash-safe store I/O, length/count, overflow detection, per-store lock, the guarded `forget` |
| `diary.py` | signed-weight clamp + decay + diary keep-rank |
| `diary_format.py` | the single dated-bullet serialize / parse / validate |
| `check.py` | the `check [--fix]` format gate + quarantine |
| `migrate_emotional_to_diary.py` | the one-off legacy -> diary migration |
| `skills.py` | usage-based skill importance + skills keep-rank |
| `meditation.py` | the compaction engine (merge → relevance-downgrade → compact → guarded-forget) |
| `reinforce.py` | pure associative-reinforcement mutations + the concise recall-reference builder |
| `evocation.py` | the batched evocation pass (gated): judge what each new diary note recalls, reinforce it, append the reference |
| `lifecycle.py` | extraction, in-session staging, fresh-start `rebuild`, `pin`, charter mission sourcing |
| `sweep.py` | team-sweep gating + post-ingest `meditate_all_stores` |
| `briefing.py` | assemble the session-start briefing from the four stores |
| `sources/` | source adapters (claude_code, slack_thread, docs, journal_worklog) |
| `summarizers/` | LLM summarization backends (anthropic, mock) — the only model call |
| `state.py` | per-store JSON state snapshot (count / chars / max / over_overflow) |

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
    # Optional, for multi-persona Slack-bridge setups:
    # persona: ayako                    # only ingest sessions owned by this persona
    # include_unattributed: false        # opt-in to also include local claude-p sessions

summarizer:
  # Pluggable — "anthropic" is the default. The ONLY model call: the
  # extraction judgement + meditation's similarity/staleness/compaction
  # judgements. See "Adding a new summarizer vendor" below.
  backend: anthropic
  model: claude-opus-4-7
  prompts: default/v1

# The four bounded stores (every key optional — these are the defaults).
memory:
  length_unit: characters        # CONFIRMED: characters, never tokens
  skills:
    max_count: 40
    overflow_limit: 50
  must_remember:
    max_length: 8000             # chars
    overflow_limit: 10000
  diary:
    max_length: 4000             # chars (loaded whole; kept small by forgetting)
    overflow_limit: 6000
    weight_cap: 10               # hard cap: |weight| <= 10
    decay:
      magnitude_per_day: 0.1

rebuild:
  trigger: lazy
  idle_threshold_hours: 1

# (diary loads whole — no top-N briefing knob)
```

Validation is fail-fast at load: `length_unit` must be `characters` (token
units rejected); every store bound must satisfy `0 < max < overflow_limit`;
`weight_cap > 0`; `magnitude_per_day >= 0`.

## Usage

```bash
# Initialize the memory store + validate config
tiger-memory --config my-config.yaml init

# Fresh-start rebuild: drop the retired surface (first run), regenerate the
# session-start briefing (skill index + must_remember + full diary + notice)
tiger-memory --config my-config.yaml rebuild

# Pin a must_remember entry directly.
# NOTE: --kind defaults to operator_explicit (importance 5.0) — the most
# forget-protected kind. A bare `tiger-memory pin "..."` therefore writes an
# operator_explicit directive; pass `--kind preference` for an ordinary note.
tiger-memory --config my-config.yaml pin "Operator prefers tabular diffs" --kind preference
tiger-memory --config my-config.yaml pin "Never force-push main" --kind operator_explicit

# JSON snapshot of the four stores (count / chars / max / over_overflow)
tiger-memory --config my-config.yaml state
```

Extraction itself is driven by the team sweep, not by hand — see the
[sweep protocol](tiger-memory-sweep-protocol.md). The in-session executor
verbs (`plan` / `ingest-extraction` / `ingest-staged`) are driven by the
`sweep-memory` skill, not run manually.

## CLI verbs (the live set)

| Verb | What it does |
|---|---|
| `init` | create the empty store + validate the config |
| `rebuild` | fresh-start: drop the retired legacy surface (first run), regenerate the briefing |
| `pin <memo> --kind <k>` | write one `must_remember` entry. `--kind` **defaults to `operator_explicit`** (importance 5.0, the most forget-protected kind), so a bare `pin` is a protected directive — pass `--kind preference` for an ordinary note |
| `import-legacy [--mock] [--force]` | **one-off, idempotent** seed of the three stores from the old `must_memorize.md` pins + the daily/weekly/monthly rollups (reads/snapshots only — never deletes). MUST run **before** `rebuild` (which drops the legacy files). `--mock` uses the deterministic mock summarizer (no live model); `--force` re-seeds (drops prior `import-legacy` entries first) |
| `state` | JSON snapshot of the four stores |
| `plan [--max-sessions N]` | stage one extraction prompt per idle, unprocessed transcript + a manifest (items + stacks) |
| `ingest-extraction --uuid <u>` | write back ONE sub-agent's extraction bundle (stdin) for a planned uuid |
| `ingest-staged` | glue every staged `<uuid>.extract.md` card in ONE process (race-free) |
| `sweep-plan` / `sweep-done` / `sweep-complete` / `sweep-release` | team-sweep gating (non-AI) |
| `check [--fix]` | validate the 3 stores' on-disk format; exit non-zero if any invalid. `--fix` repairs mechanical drift + quarantines non-mechanical to `<store>.rejected.md` (no silent loss — the quarantined block stays in the sidecar for the persona's next in-character meditation to re-author; it is not auto-restored). Runs as the per-persona gate at the end of every `rebuild` and in CI / pre-commit |
| `migrate-emotional-to-diary [--apply]` | one-off legacy `emotional.md` -> dated-bullet `diary.md`. **`--dry-run` is the default** (preview only); `--apply` snapshots `emotional.md.bak`, writes `diary.md`, marks done (idempotent), takes the diary store-lock and refuses if a sweep holds it |

The retired verbs `bootstrap`, `search`, `drill`, `tree`, `raw`,
`resummarize`, and `ingest-summary` no longer exist — they belonged to the
chronological-rollup / summary-RAG surface that was removed.

## The extraction contract

Extraction turns one finished session into a strict bundle with three
whole-line section markers, in order (see
`summarizers/prompts/default/v1/extract_memory.md`):

```
@@SKILLS@@
<skill blocks, or NONE>
@@MUST_REMEMBER@@
<must-remember blocks, or NONE>
@@DIARY@@
<diary blocks, or NONE>
```

A skill block is `NAME:` / `TRIGGER:` / `PROCEDURE:`; a must-remember block
is `KIND:` / `MEMO:`; a diary block is `WEIGHT:` / `TEXT:` (the
note is one line; its date is the session date). A malformed *bundle* (missing/out-of-order markers) is rejected
before any write; an individual malformed *block* is skipped. `NONE` for a
store is a valid, expected outcome — most sessions add little.

## Meditation (the compaction engine)

When a store crosses its `overflow_limit`, the sweep runs `meditate` over
it (per store, under a per-store lock), strictly in order:

1. **merge** near-duplicates — merging raises the survivor's scalar
   (importance / emotional magnitude), clamped;
2. *(must_remember only)* **relevance-check** each `operator_explicit`
   directive against the live charter Mission and **downgrade** stale ones to
   `decision` — this runs *before* any forget;
3. **compact** verbose survivors (the summarizer rewrites a body shorter);
4. **forget** the lowest-keep-ranked entries until under `max`, via the
   guarded `forget`.

Forgetting is irreversible and has no safety net, so the engine logs every
mutation at INFO, defaults the LLM judgement to the **safe** answer on a
garbled verdict, and **never** drops a still-relevant `operator_explicit`
directive (the forget-guard; if it cannot get under `max` without one, it
leaves the store intact and warns). The full design and the ratified
forget-guard semantics are in [DESIGN-memory.md](DESIGN-memory.md) §5.

The broad acceptance/verification suite for the revamp invariants (forget
order with nothing safe to drop, decay boundaries, relevance-downgrade
ordering, concurrent meditation, character-length edges, idempotency, and
malformed-input handling) is
`tests/tiger_memory/test_memory_revamp_qa_defense.py`.

## Session start (the briefing)

`tiger-memory rebuild` assembles `briefing/` from the four stores:

- the **full must_remember** store (highest-importance first);
- the **full diary** — loaded whole, strongest feelings first by `|weight|`
  (forgetting, not a display cap, keeps it bounded);
- the **skill index** — name + trigger + one line per skill; only the index
  loads, the persona reads the full skill on demand;
- the **unprocessed-session notice** (`UNPROCESSED.md`): memory is built
  only after a session goes idle, so a still-active session may not be
  reflected yet. **Rule:** if the Operator references something you don't
  recognise, check this memory first, then check for unprocessed/active
  sessions, before claiming ignorance.

## Migrating a roster from the old memory model

A persona config written for the **old** chronological-rollup model still
loads (unknown keys are ignored), but it carries retired keys and lacks the
`memory:` block, so it falls back to defaults. To migrate a persona's
`memories/<persona>/tiger-memory.config.yaml` (or the team-level
`configs/tiger-memory.defaults.yaml`) to the new model:

**1. Remove the retired keys** (they no longer have any effect):

- `budgets.must_memorize_rows` (and any rollup word budgets such as
  `short_summary_words` / `detailed_summary_words` / `*_rollup_*` /
  `longer_memory_words`)
- `budgets.repeat_detection_similarity`
- the whole `decay:` block (the per-kind `days_per_point` table)
- the whole `embedder:` block (RAG is retired)
- `rebuild.resummarize_window_days`
- `briefing.walking` (and `briefing.resident_layers`)

`budgets.max_prompt_content_chars` / `max_staged_content_chars` /
`sweep_stack_content_chars` / `sweep_stack_max_items` are **kept** (still
live for extraction + sweep stacking).

**2. Add the `memory:` block** (optional — omit it to take the defaults
in §7 of the design). To tune, add the keys you want:

```yaml
memory:
  skills:        { max_count: 40, overflow_limit: 50 }
  must_remember: { max_length: 8000, overflow_limit: 10000 }
  diary:
    max_length: 4000
    overflow_limit: 6000
    weight_cap: 10
    decay: { magnitude_per_day: 0.1 }
```

Optionally also add `memory_extract:` (per-section word budgets) and
(the diary now loads whole — no top-N knob).

**3. Seed from the old memory (one-off).** Before the fresh start drops the
legacy files, run the one-off import to carry the old memory forward:
`tiger-memory --config <persona-config> import-legacy`. It reads (and only
reads — no deletion) each persona's old `must_memorize.md` pins + the
daily/weekly/monthly rollups, re-authors them **in character** into the new
`skills` / `must_remember` / `diary` shapes, backdates them to their
source dates (an old diary note enters already-decayed; a skill's
`last_used` = its source date), and appends them to the three stores tagged
`source: import-legacy`. It is **idempotent** (a durable `.state.json`
`legacy_import` marker plus a detect-existing-seed fallback make a re-run a
no-op); pass `--force` to deliberately re-seed and `--mock` to run without a
live model. The verbose per-session `archive/` and `journal/` shorts are NOT
imported — only the pins + rollups.

**4. Fresh-start the store.** Migration is a **fresh start** (no converter,
design §10.6): run `tiger-memory --config <persona-config> rebuild` once.
The first rebuild drops the legacy on-disk surface (old rollup summaries,
`must_memorize.md`, `longer_memory.md`, the `archive/` dir) and regenerates
the briefing; the three new stores then (re)build incrementally as the team
sweep extracts new sessions.

> **Ordering (critical).** `import-legacy` must run **before** `rebuild` —
> `rebuild` deletes the legacy files, so running it first is irrecoverable
> (the old memory is gone before it can be seeded). On a team that tracks
> live persona configs in git, also do the roster-YAML migration **with the
> branch merge**, not before — editing a live config to the new schema while
> `main` still runs the old code would desync the config from the deployed
> code. The new code ignores the retired keys, and the old code ignores the
> `memory:` block. The full safe order is:
>
> **merge the code → migrate the configs → `import-legacy` (seed) →
> `rebuild` (fresh start) → ongoing sweep.**

### Migrating `emotional.md` → `diary.md` (3-store model)

A persona already on the 3-store model has an `emotional.md` (frontmatter:
`weight` + `reaction` + body). The diary redesign replaces it with the compact
dated-bullet `diary.md`. The one-off converter:

```bash
tiger-memory --config <persona-config> migrate-emotional-to-diary           # dry-run
tiger-memory --config <persona-config> migrate-emotional-to-diary --apply    # perform it
```

- **`--dry-run` is the default**: it parses + previews
  (`source_blocks` / `converted` / `kept` / `forgotten` / `no_loss`), writing
  nothing. Run it on every persona first, and **CONFIRM `no_loss: true`** (i.e. `source_blocks == kept + forgotten`) before `--apply`.
- **`--apply`** snapshots `emotional.md` → `emotional.md.bak`, writes the
  validated `diary.md`, removes `emotional.md`, and marks a durable
  `diary_migrated` state marker (**idempotent** — a re-run is a no-op). It
  takes the diary store-lock and **refuses if a live sweep holds it**.
- **Map**: each old entry's body → the bullet note (the `reaction`'s valence is
  folded into the weight sign); `weight` → `(±N)`; the entry date →
  the `## YYYY-MM-DD` header. A converted file over `max_length` is bounded by a
  forget pass (lowest `|weight|` first) — **no silent loss** (every source
  block is counted: `source_blocks == converted == kept + forgotten`).
- **Rollback**: restore `emotional.md.bak` → `emotional.md`, delete `diary.md`,
  remove the `diary_migrated` marker from `.state.json`.

Safe order: **dry-run all personas → review the previews → `--apply` per
persona when no sweep is running → `tiger-memory check` to confirm valid.** The
live `--apply` over a real roster is the Operator's call.

> **Verify forgetting kept it bounded.** After a sweep/meditation, a diary
> is in-bound iff its character length is `<= max_length` (4000) AND
> `tiger-memory check` exits 0 — the bound is enforced by forgetting, so
> this is the check a verifier runs, not an assumption.

`tigerharness init` already scaffolds new personas with the new model (no
retired keys), so this migration only applies to configs created before the
revamp.

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

## The auto_memory source (legacy)

One more source kind the config validator accepts is `auto_memory`. It
concatenates the `*.md` files under a `path:` directory into a single
synthetic record (Claude Code's auto-memory dir), extracted like any other
source. It is retained for backward compatibility; new setups should prefer
the explicit sources above.

```yaml
sources:
  - kind: auto_memory
    path: ~/.claude/projects/<slug>/memory/
```

## Adding a new summarizer vendor

Tiger-memory's summarizer is vendor-agnostic by design — it is the single
model touch point (the extraction judgement + meditation's
similarity/staleness/compaction judgements). The `anthropic` backend is
pre-registered; plug in any other vendor in three steps:

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
top-level `__init__.py` or a startup hook. The registration must run before
`tiger-memory rebuild`/`ingest-staged` is invoked.

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

**Future: entry-point-based registration.** Python's
`[project.entry-points."tigerharness.summarizers"]` mechanism would let
plugins register without anyone explicitly importing them — on the roadmap
if demand picks up. The in-process call is sufficient for single-org use.

# Memory system design — the four bounded stores (incl. fuzzy)

Status: **SHIPPED** (2026-06-17). This is the canonical design for the
tiger-memory bounded-store revamp, as built on
`work/2026-06-17-memory-revamp` (2600 tests, 100% branch coverage).
It supersedes the chronological-rollup memory model described in
`docs/history/tiger-memory-rework.md` (kept for historical rationale).
The shipped code under `src/tigerharness/tiger_memory/` is the ground
truth; this doc explains *why* it is shaped the way it is. Section
numbers are preserved from the original design draft so review notes that
cite "§4.2" / "§5" still resolve.

---

## 1. Why we did this

The old tiger-memory had three weaknesses:

1. **Unbounded growth.** Archived memory files grew forever; eventually a
   liability.
2. **Memorizing ≠ growing.** The agent recorded facts but did not get
   measurably better at future tasks because of them.
3. **Weak chemistry with the persona system.** Memory was the same
   generic store for everyone — it did not make a Sakuragi more Sakuragi.

The revamp answers all three: memory is **bounded and self-pruning**
(forgetting is first-class), **skill-forming** (learned lessons become
invokable skills that make future work easier), and **persona-coloured**
(diary logs carry each persona's dated work-notes with emotional weight, decayed over time).

## 2. Principles (the non-negotiables)

- **Forgetting is a feature, not a failure.** Every store is bounded.
  Crossing the bound triggers meditation, which compacts and forgets so the
  store stays focused on what helps the team now.
- **Memory must improve future task handling**, not just record the past.
- **Vendor-neutral mechanics.** All bookkeeping (weights, decay, forgetting,
  meditation orchestration, skill index) is pure Python with vendor-neutral
  data formats. The only LLM call sits behind the existing pluggable
  summarizer registry. No token-based units; no dependency on Claude's
  skill system.
- **The persona processes its own memory.** Both extraction (turning a
  finished session into memory) and meditation (compaction) run **as that
  persona, in character, with full context**, on the subscription rail (the
  sweep-memory pattern: constrained Task sub-agents) — never an inline
  `claude -p`, never a raw vendor API call. This is what makes the emotional
  layer genuinely persona-oriented.

## 3. What we retired (no safety net)

Ripped out entirely — raw sources stay on disk but are no longer maintained
as memory:

- Chronological rollups: daily / weekly / monthly summaries.
- `longer_memory.md` (the compressed pre-window history).
- `archive/` detailed summaries.
- Summary-RAG and drill-down: `rag.py`, the memory embedders, `drill.py`,
  the search-over-summaries path, and the `tiger-memory-search` /
  `tiger-memory-drill-down` skills.
- The lazy rollup lifecycle that produced the above.
- The old extraction contract: the `@@SHORT@@` / `@@DETAILED@@` /
  `@@MUST_MEMORIZE@@` bundle, `.summary.md` cards, the `ingest-summary`
  CLI verb, and the `must_memorize.md` table with `must_memorize_rows` +
  kind-decay.

The fresh-start migration is `tiger-memory rebuild`
(`lifecycle._drop_legacy_surface`): on its first run it removes the old
rollup `archive/` dir and the legacy journal files (chronological
summaries, `must_memorize.md`, `longer_memory.md`). The three new store
files survive; there is no one-time converter (resolved decision §10.6).

There is, however, a **one-off seeding import** (`tiger-memory
import-legacy`, `import_legacy.py`) that carries the old memory forward
*before* the fresh-start drop. It reads (never deletes) each persona's old
`must_memorize.md` pins + the daily/weekly/monthly rollups, re-authors them
in character into the new shapes, backdates them to their source dates, and
appends them tagged `source: import-legacy`. It is idempotent (a `.state.json`
`legacy_import` marker + a detect-existing-seed fallback) and must run
**before** `rebuild`. The full migration order is **merge → migrate configs
→ import-legacy → rebuild → sweep** — running `rebuild` before
`import-legacy` is irrecoverable, as it deletes the legacy files (§12).

Kept and adapted:

- **Source adapters** (`claude_code`, `slack_thread`, `journal_worklog`,
  `auto_memory`) as the input feed, plus multi-persona attribution and the
  drive-session double-count suppression.
- **The idle/summary trigger** (lazy / idle-threshold) — unchanged as the
  signal that a finished session is ready to be turned into memory.
- **The pin command**, reframed to write a must-remember entry.

## 4. The three bounded stores

> **Superseded by §11 (2026-06-19): the model is now FOUR stores** — a `fuzzy`
> store was added and `owner_explicit` renamed to `operator_explicit`. §4–§7
> below describe the original three; read §11 for the current model.

Memory is now exactly three stores per persona (`tiger_memory/entries.py`,
`bounded_store.py`). Each has a scalar, an ordering rule, and a two-number
bound (`max` + `overflow_limit`). The two numbers give hysteresis: a store
may drift up to `overflow_limit`, and only *then* does meditation fire and
compact it back below `max`. This prevents thrash (meditate every session).

On-disk layout: one markdown file per store under the persona's `journal/`
dir — `skills.md`, `must_remember.md`, `diary.md`. Each file is a
sequence of entries; every entry is a YAML-frontmatter block (the structured
fields) + a body, blocks separated by a sentinel line. `save_atomic`
rewrites the whole file (meditation operates store-wide). Length is measured
in **characters**, never tokens (§8).

### 4.1 Skills — learned, invokable, vendor-neutral

- A skill (`SkillEntry`) is a markdown entry capturing "something I learned
  to do better": a `name`, a *when-to-use* `trigger`, the `procedure`/lesson,
  provenance (`source`/`created_at`), a `usage_count`, and an `importance`
  scalar.
- **Importance grows with use** — `importance = log1p(usage_count)`
  (`skills.skill_importance`), monotonic non-decreasing in usage and
  **independent of elapsed time** (no continuous time-decay). The keep-rank
  factors in recency of use as a tie-break: an old, unused skill ranks lower
  and is forgotten first. Importance is re-derived at meditation time, not
  ticked down daily.
- **Session start:** Python rebuilds a **skill index** (name + trigger +
  one-line summary per skill, `briefing._render_skill_index`) and that index
  — *only the index* — is loaded into the persona's context. When a skill is
  relevant, the persona loads the full entry (its procedure) on demand from
  `skills.md`. This is our own progressive-disclosure mechanism, **not**
  Claude's `.claude/skills/`, and persona-private (not shared).
- **Bound:** count-based (`max_count` + `overflow_limit`). On overflow,
  meditation merges duplicate/near-duplicate skills and forgets the oldest,
  least-used ones.

### 4.2 Must-remember — external directives

- Reflects requirements from outside that make the work land better.
  `MustRememberEntry`: text, `kind` (`owner_explicit` / `preference` /
  `decision` / `incident`), an `importance` scalar, source + date.
- **Length-based** bound (replacing the old ~60-row cap): `max_length` +
  `overflow_limit`, measured in characters (§7).
- **Owner directives start elevated but are not immortal.** `pin --kind
  owner_explicit` writes importance 5.0; extracted directives start at 1.0.
  Meditation runs a **relevance check against the live team goal / project
  focus**: a directive that was tied to an old feature and no longer serves
  the current mission is **downgraded to a normal kind** (`decision`), after
  which it becomes forgettable like any other entry. Nothing is permanently
  locked; relevance to the live mission is the gate.
  **No time-decay here:** unlike the diary store, must_remember
  `importance` does not tick down over time — the keep-rank is simply
  `importance` + recency (`meditation.keep_rank`). Downgrading an
  `owner_explicit` directive to `decision` does not start a decay clock; it
  only drops the forget-guard, so the entry can be forgotten on the next
  overflow if its `importance` + recency rank low enough.
- On overflow, meditation: dedupe (merging bumps importance by 1.0),
  relevance-check and downgrade stale directives, compact verbose survivors,
  then guarded-forget old low-importance items until length < `max_length`.

### 4.3 Diary — the dated, weighted work-log (the "more Sakuragi" layer)

- `DiaryEntry`: a short **dated diary note** — "what I did / why / learned /
  could-do-better" — carrying a signed emotional `weight`. The feeling is
  folded into the note text plus the **sign** of the weight; there is no
  separate `reaction` field (dropped in the diary redesign).
- **Compact dated-bullet on-disk format** (this store ONLY — skills and
  must_remember keep YAML frontmatter): per-day sections headed
  `## YYYY-MM-DD` (ascending), each entry a bullet `- (±N) <note>` with the
  signed inline weight. The single `diary_format` module owns
  serialize / parse / validate (the note's whitespace is flattened to one
  line); the store's **validate-on-write round-trip** (serialize -> re-parse
  -> validate) REFUSES to persist a store that does not round-trip clean. The
  compact format intentionally drops per-entry `id`/`source` (only date /
  weight / note persist). Example `diary.md`:

  ```
  ## 2026-06-17
  - (+7) Drove the harness to true 100% — patient thoroughness, earned not declared.
  - (-5) Agents declaring success on near-misses; optimism that skips verification bugs me.

  ## 2026-06-18
  - (+4) Reframed the diary store with the Operator — simpler than what we shipped.
  ```
- **Signed scalar, hard cap [-10, +10]** (`diary.clamp_weight`): positive =
  *for* / liked, negative = *against* / disliked, `0` = neutral. Repeated
  merges can never inflate past ±10.
- **Decay toward 0 from either side** (`diary.decay_weight`), **anchored on
  each bullet's date**. Each day `|weight|` shrinks toward 0 by
  `magnitude_per_day * days`, sign preserved until the magnitude hits exactly
  0. Forgetting ranks by decayed `|weight|` plus recency
  (`diary.diary_keep_rank`), so strong feelings — positive *or* negative —
  survive while near-neutral / decayed items are forgotten first ("higher
  emotional weight = harder to forget").
- **Loaded WHOLE at session start** — there is no top-N display cap; the store
  is kept loadable by *forgetting*, not by a view limit. Bounds are tighter
  than the frontmatter stores for that reason: `max_length` 6000,
  `overflow_limit` 8000 chars (raised from 4000/6000 alongside associative
  reinforcement, §4.4, to give recall references + reinforced recency room).
- **Length-based** bound: `max_length` + `overflow_limit`. On overflow,
  meditation merges similar items (merging bumps magnitude toward the
  stronger feeling, clamped), then compacts/forgets the lowest-magnitude /
  oldest items until length < `max_length`.

### 4.4 Associative reinforcement — the recall-graph seed

A new diary note can *evoke* (联想) 0–2 existing memories across the three sharp
stores; each evoked **old** item is reinforced (the new note is never bumped — no
self-bump), and a concise recall reference to the evoked item(s) is appended to
the new note's text. The reference is a minimal human-findable pointer
(`↪ recalls: …`), deliberately **not** a structured graph field — so the compact,
id-less `diary_format` is unchanged (no schema/id/migration). It is the seed of a
memory-recall graph the operator asked for ("refer to other memory items … like a
recall graph sometimes").

- **Diary reinforcement = weight + recency:** magnitude +1 toward the bullet's
  existing sign, clamped to `weight_cap` (a hub saturates, no runaway), AND
  `last_used` reset to the evoking time (re-dates the bullet — recency restored).
- **Count stores = count:** must_remember `repeat_count += 1`; skills
  `usage_count += 1` (log-shaped importance, diminishing returns). Both mirror
  the meditation merge survivor-bump, so reinforcement and merge agree.
- **One model touch point, batched, separate from merge:** one summarizer call
  per ingest judges all that ingest's new diary notes against the current stores;
  it runs between ingest and meditation. Merge collapses near-duplicates;
  evocation keeps both and strengthens the old one.
- **Gated (`memory.diary.evocation_enabled`, default false):** enabling it adds a
  model call at ingest — a deliberate rail decision. Off ⇒ behaviour unchanged.
  Code: pure mutations + reference in `reinforce.py`; the pass in `evocation.py`.

## 5. Meditation — the compaction engine

> **See §11.2 for the current 4-store pipeline** (`meditate_persona`): this §5
> describes the original per-store engine, which still runs for skills; the diary
> + must_remember + fuzzy stores now meditate together (aged items route to
> `fuzzy.md`, never hard-dropped).

One engine (`meditation.meditate`), run per store, that turns "over the
overflow limit" back into "under max."

- **Trigger:** at session start (post-ingest), if any store is over its
  `overflow_limit` (`bounded_store.is_over_overflow` — the hysteresis
  trigger; `sweep.meditate_all_stores` gates on it). Meditation itself is a
  no-op under `max`.
- **Where it runs:** in-persona, on the subscription rail (sweep-memory's
  constrained Task sub-agent pattern), with the persona's full context **and
  the current team goal / project focus, read from the charter Mission**
  (`charter/README.md`, via `lifecycle.team_mission_text`). Never inline
  `claude -p`; never a raw vendor API call.
- **Per-store recipe** (`meditate` runs these strictly in order under the
  per-store lock):
  1. **merge** duplicate / near-duplicate entries; merging **raises** the
     surviving entry's scalar (importance, or emotional magnitude), clamped.
  2. *(must-remember only)* **relevance-check** each `owner_explicit`
     directive against the team goal; **downgrade** stale ones to `decision`.
  3. **compact** verbose survivors (skipped for the count-bounded skills
     store; the summarizer rewrites a body shorter, accepted only if
     strictly shorter).
  4. **forget** the lowest-keep-ranked entries until the store is below
     `max`, via the guarded `bounded_store.forget`.
- **Invariants:** steps run in order — never forget a still-relevant owner
  directive before the relevance-check has had its say (see §5.1). Meditation
  takes the per-store lock (concurrent sessions must not meditate the same
  store at once; a live foreign lock raises `StoreLockHeld` and the caller
  backs off — retried next sweep, it is not urgent). It is idempotent and
  **logged**: forgetting is irreversible with no safety net, so a changed
  pass emits an INFO record naming exactly what merged / downgraded /
  compacted / forgotten.
- **Vendor-neutral:** orchestration and ranking are Python; only the
  judgement (which items are "similar," "stale," "low value") goes through
  the pluggable summarizer/LLM backend. An unparseable verdict defaults to
  the **safe** answer (not-similar / still-relevant / keep-original), so a
  garbled backend never destroys data. CI runs the whole engine under a
  scripted mock — zero live-model calls.

### 5.1 Forget-guard semantics — RATIFIED (do not "fix" this)

The forget pass must never drop a still-relevant `owner_explicit` directive.
The mechanism is `bounded_store.forget(..., relevance_checked_ids=...)`: it
raises `ForgetGuardError` when asked to drop an `owner_explicit`
must_remember entry whose id is **not** in `relevance_checked_ids`.

The interface note that introduced the guard phrased it as "collect the set
of owner directives examined this cycle and pass them as
`relevance_checked_ids`." Taken **literally**, that would mark a
*still-relevant* directive as licensed-to-drop and reopen the data-loss hole
the guard exists to close.

**Decision (ratified, do not revert):** meditation's relevance pass
(`_relevance_pass`) adds to `relevance_checked` **only the directives it
judged stale and downgraded** (which are no longer `owner_explicit` anyway).
A still-relevant directive stays `owner_explicit` and is deliberately **not**
added, so the guard refuses to drop it. `_forget_pass` then **skips** a
guarded candidate (it does not force-drop) and, if the store still cannot get
under `max`, sets the terminal `over_max` warning and **leaves the store
intact**. This satisfies both the guard's letter and the design's intent: the
data-loss invariant wins over the interface-note phrasing.

A later editor must **not** "fix" `_relevance_pass` back to the literal
all-examined reading — that would silently reopen the data-loss hole. The
behavior is regression-locked by
`tests/tiger_memory/test_meditation.py::test_relevance_keeps_relevant_owner_then_terminal_overmax`
(seeds only still-relevant owner directives over `max`, asserts both survive
and `over_max is True`, `forgotten == []`).

## 6. Session-start protocol

The session-start working set (`briefing.rebuild_briefing`) is exactly:

- **the full must_remember store** (bounded, so cheap), highest-importance
  first;
- **the full diary** — loaded WHOLE, strongest feelings first by stored
  `|weight|` (forgetting, not a display cap, keeps it bounded; §4.3). It ranks
  on the raw weight as last written; decay is materialized at meditation time;
- **the skill index** — name + trigger + one-line summary per skill,
  Python-rebuilt; only the index loads, the persona pulls a full skill on
  demand;
- **the unprocessed/active-session notice** (`UNPROCESSED.md`): the idle
  trigger only fires once a session goes idle, so a *still-active* recent
  session may not be in memory yet. The rule for the agent: **if the Operator
  references something it does not recognise, check memory first, then check
  for unprocessed/active sessions before claiming ignorance.**

The briefing is assembled atomically (temp-dir folder swap) with a
fingerprint no-op shortcut. Meditation runs (per store) if any store is over
its overflow limit (§5).

## 7. Config schema (the `memory:` block)

`tiger_memory/config.py` parses and validates this block; every key is
optional (these are the defaults). See `examples/tiger-memory.config.yaml`
for an annotated copy.

```yaml
memory:
  length_unit: characters        # CONFIRMED: characters, never tokens
  skills:
    max_count: 40                # count bound
    overflow_limit: 50
  must_remember:
    max_length: 8000             # chars
    overflow_limit: 10000
  diary:
    max_length: 4000             # chars (loaded WHOLE; kept small by forgetting)
    overflow_limit: 6000
    weight_cap: 10               # hard cap: |weight| <= 10
    decay:
      magnitude_per_day: 0.1     # how fast |weight| -> 0

memory_extract:                  # per-section word budgets for extraction
  skill_procedure_words: 120
  memo_words: 25
  reaction_words: 40             # diary-note length hint
  max_output_words: 600

briefing: {}                     # diary loads whole — no top-N knob
```

Validation is fail-fast at load time: `length_unit` must be `characters`
(token units are rejected); each store bound must satisfy
`0 < max < overflow_limit` (the hysteresis band); `weight_cap > 0`;
`magnitude_per_day >= 0`. The numbers are sensible defaults approved for
tuning later; `length_unit` and `weight_cap` are confirmed final.

## 8. Vendor decoupling (explicit)

- Length measured in **characters**, never tokens (tokenization is
  vendor-specific and rejected at config load).
- Weights, decay, forgetting, the skill index, and meditation orchestration
  are pure Python over vendor-neutral markdown/frontmatter.
- The only model call is the judgement step inside extraction/meditation,
  which goes through the **existing pluggable summarizer registry** — swap
  Anthropic for any vendor without touching the memory mechanics.
- The skill system is **ours**, not Claude's `.claude/skills/`.

## 9. Code map

| Module | Role |
|---|---|
| `config.py` | the `memory:` / `memory_extract:` blocks + fail-fast validation |
| `entries.py` | the three entry schemas (`SkillEntry` / `MustRememberEntry` / `DiaryEntry`) + validation + frontmatter bridge |
| `bounded_store.py` | crash-safe store I/O, character-length / count, `is_over_overflow`, per-store lock, the guarded `forget` + `ForgetGuardError` / `StoreLockHeld` |
| `diary.py` | signed-weight clamp + decay + diary keep-rank |
| `diary_format.py` | the single dated-bullet serialize / parse / validate |
| `check.py` | `tiger-memory check [--fix]` format gate + quarantine |
| `migrate_emotional_to_diary.py` | one-off legacy `emotional.md` -> `diary.md` migration |
| `skills.py` | `log1p(usage)` importance + skills keep-rank |
| `ranking.py` | shared recency / date-math helpers |
| `meditation.py` | the compaction engine: `keep_rank`, `MeditationLog`, `meditate` (per-store), and `meditate_persona` (the 4-store pipeline: merge → relevance-downgrade → compact → fuzz-select → recompact → bound, no hard drop) |
| `fuzzy_store.py` | the 4th store I/O: `load_fuzzy` / `bound_fuzzy` (deterministic char-bound = convergence) / `save_fuzzy` |
| `fuzz_select.py` | pure fuzz-candidate selection: `select_diary_fuzz` (fresh-window-protected) / `select_mr_fuzz` (downgraded + low-repeat-count) |
| `fuzzy_recompact.py` | `recompact_fuzzy`: summarizer folds aging diary + must_remember + existing fuzzy into one coarse blob |
| `lifecycle.py` | extraction (`parse_extraction` / `extract_candidates` / `ingest_candidates`), the in-session staging (`plan_extraction`), fresh-start `rebuild`, `pin`, `team_mission_text` |
| `import_legacy.py` | the one-off legacy import (§12): reader (`read_legacy`), persona-driven re-author (`reauthor`), backdated seeding scorer (`score_seed_candidates`), seed-writer + idempotency guards (`seed_entries` / `already_imported` / `mark_imported`), orchestrator (`import_legacy_run`) |
| `sweep.py` | team-sweep gating + `meditate_all_stores` (post-ingest, over-overflow only) |
| `briefing.py` | session-start assembly: must_remember + full diary + skill index + unprocessed notice |
| `state.py` | the `tiger-memory state` JSON snapshot (`compute_state`): per-store count / chars / `max` / `over_overflow` — the programmatic hook to check a store's size vs its bound |
| `store.py` | on-disk store layout (`Paths`) + crash-safe serialization helpers (`atomic_write`, `atomic_swap_dir`, `write_state` / `read_state`) used by the bounded stores and the briefing swap |
| `cli.py` | `init` / `rebuild` / `pin` / `import-legacy` / `state` / `plan` / `ingest-extraction` / `ingest-staged` / `sweep-*` |

## 10. Resolved decisions (Operator, 2026-06-17)

1. **Length unit:** characters. (Confirmed.)
2. **Default bounds & decay rate:** sensible defaults (§7), tuned later.
   (Confirmed.)
3. **Skill importance:** no continuous time-decay, but old/unused skills are
   less important — recency of use feeds the keep-ranking and is recomputed
   at meditation. (Confirmed.)
4. **Emotional magnitude cap:** hard cap, range **[-10, +10]**. (Confirmed.)
5. **Team-goal reference for the relevance check:** read from the charter
   Mission. (Confirmed.)
6. **Migration:** fresh start — drop the existing stores and let the new
   system build from sources; no one-time converter. A one-off
   `import-legacy` seed (§12) carries the old `must_memorize.md` pins +
   rollups forward *before* the drop; order is merge → migrate configs →
   import-legacy → rebuild → sweep. (Confirmed.)
7. **Forget-guard semantics:** `relevance_checked_ids` carries only the
   downgraded directives, so a still-relevant owner directive is never
   licensed to drop (§5.1). (Ratified, do not revert.)

## 11. The 4-store evolution — fuzzy memory (Operator, 2026-06-19)

This supersedes the "three stores" framing of §4–§7 where they differ. The model
is now **four** length-bounded stores, all loaded whole at session start, plus a
richer meditation pipeline that **never hard-deletes** — older memory loses
granularity, not existence.

### 11.1 The four stores

1. **`skills.md`** — unchanged (count-bounded, importance = log1p(usage)).
2. **`must_remember.md`** — external directives. Importance is now a
   **reinforcement / repeat-count**: each time a fact recurs across sessions the
   meditation merge increments `repeat_count` (a fact seen N times outranks a
   one-off). Kinds: `operator_explicit` / `preference` / `decision` / `incident`
   (renamed from `owner_explicit`; see §11.5). `operator_explicit` + `decision`
   stay sharp until a **relevance-downgrade** demotes them.
3. **`diary.md`** — dated weighted bullets `## YYYY-MM-DD` / `- (±N) <note>`.
   EVERY item is written after summarization regardless of weight (incl. 0) —
   forgetting is a meditation concern, never an ingest-time filter. A
   `fresh_days` window (default 7) keeps recent bullets verbatim, any weight.
   The note carries *what I did + why this weight + what I learned / could do
   better* (richer than before; the on-disk `- (±N) <note>` format is unchanged).
4. **`fuzzy.md`** — **NEW.** Coarsened, grouped FREE TEXT aged out of BOTH diary
   and must_remember. Length-bounded; loaded whole so the persona keeps the gist.
   Not an entry list — `tiger_memory/fuzzy_store.py` owns it (load / hard-bound /
   atomic write). `tiger-memory check` validates it (over-overflow → `--fix`
   re-bounds).

### 11.2 The meditation pipeline (ordered) — `meditation.meditate_persona`

Per persona, under the diary + must_remember store locks:

1. **merge** near-duplicates (bumps must_remember `repeat_count`).
2. **decay** diary effective weight by age (existing `diary.decay_weight`).
3. **relevance-downgrade** (must_remember): each `operator_explicit` / `decision`
   directive is judged against the current team/project goal (charter mission);
   a stale one is downgraded to a normal kind AND routed to fuzzy. Guardrail:
   conservative + reversible — only when clearly off-goal; the demoted item goes
   to fuzzy (retained, recoverable), never deleted; it **re-sharpens** via
   repeat-count if it recurs. We never silently drop something the Operator said.
4. **select fuzz candidates** (`fuzz_select`): diary items past `fresh_days` with
   low decayed `|weight|`; must_remember items old + low repeat-count +
   relevance-downgraded. The fresh window is always kept verbatim.
5. **fuzzy re-compaction** (`fuzzy_recompact`): the summarizer compacts ONE bundle
   {aging must_remember, aging diary, the existing fuzzy.md} into fuzzy.md under
   its `max_length`. Aged content is re-summarised EVERY meditation, so it
   coarsens progressively, and the hard bound forces fuzzy.md to **converge**,
   never grow.
6. **bound the sharp stores**: persist the kept diary + must_remember. The ONLY
   items that leave a sharp store are the ones captured in fuzzy this cycle (or
   fresh-window items, which stay). The old forget-that-drops is gone.

### 11.3 The no-hard-drop invariant (the correctness anchor)

For every meditation: each input item ends up **kept-sharp**, within the
**fresh-window**, or present in the **fuzzy bundle** — `deleted == 0`. This is a
named test (`test_meditate_persona.py`). "No silent loss" is structural: fuzz,
never delete; demotions are recoverable; the live migration snapshots both
`emotional.md` AND `diary.md` to `.bak` before any overwrite.

### 11.4 Lifecycle: `sharp → fades → neutral → fuzzy-but-kept`

A memory enters sharp (full granularity in diary/must_remember). With age its
diary weight decays toward 0 / its must_remember repeat-count stays low; once
past `fresh_days` and low-value it is selected, folded into fuzzy.md (coarser),
and re-coarsened each subsequent meditation. It is never deleted; if it recurs it
re-sharpens (repeat-count). The two AI judgements (relevance-downgrade, fuzzy
re-compaction) go through the single summarizer seam — CI runs them mocked
(model-free); the deterministic phases are independently tested.

### 11.5 Terminology rename (Operator-mandated)

`owner` is legacy from another project; TigerHarness uses **`operator`**. The
must_remember kind `owner_explicit` → `operator_explicit` (and the human-role
"owner directive" → "operator directive" in code/prompts/docs). A **legacy-read
shim** (`entries.normalize_kind`) maps a stored `owner_explicit` → `operator_explicit`
on load, so stores written before the rename keep their elevated directives (no
silent loss). Unrelated technical `owner` (lock ownership, transcript thread
owner) is left untouched.

### 11.6 Config additions (extends §7)

```yaml
memory:
  diary:
    fresh_days: 7          # recents (<= N days) kept verbatim, never fuzzed
  fuzzy:
    max_length: 4000       # the 4th store (chars); converges under this bound
    overflow_limit: 6000   # hysteresis trigger, like the other length stores
```

Note: session-start load is now the SUM of four whole stores — keep the combined
size sane when tuning the bounds.

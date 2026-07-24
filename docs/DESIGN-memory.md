# Memory system design — the three bounded stores (topic model)

Status: **SHIPPED** (2026-07-23, [ADR 0007](adr/0007-topic-store-revamp.md)).
This is the canonical design for the tiger-memory **topic-store revamp**, as
built on `work/2026-07-23-topic-store-revamp`. It supersedes both earlier
models this doc used to describe — the original three-store model
(2026-06-17) and the 4-store diary/fuzzy evolution (2026-06-19) — see §11
(History). The shipped code under `src/tigerharness/tiger_memory/` is the
ground truth; this doc explains *why* it is shaped the way it is.

---

## 1. Why we did this

The 4-store model (skills / must_remember / diary / fuzzy) kept memory
bounded, but it had two structural problems (ADR 0007):

1. **Session-start load was too heavy.** The briefing loaded the full
   `must_remember.md` (8k chars), full `diary.md` (6k), `fuzzy.md` (4k) and
   the skill index — most of it irrelevant to the session at hand.
2. **The diary/fuzzy "personal feeling" rail earned less than it cost.**
   Weighted diary notes and the fuzzy coarsening pipeline added schema,
   scoring, migration and QA surface, while the useful residue — durable
   project knowledge — had no dedicated home.

The revamp answers both: session bootstrap is **O(indexes), not O(store)**
(only three small files load initially), and durable project knowledge gets
a first-class, self-pruning home — the **topics** store.

## 2. Principles (the non-negotiables)

- **Forgetting is a feature, not a failure.** Every surface is bounded.
  Crossing the must-compact bound makes it a mandatory compaction target on
  the next sweep, which merges, tightens, and forgets so memory stays
  focused on what helps the team now.
- **Session-start load is index-only.** The persona reads three small files
  at bootstrap (`must_remember.md`, `skill_index.md`, `topic_index.md`) and
  opens a detail file only when its index line is relevant to the work at
  hand.
- **Indexes are projections.** The rendered skill/topic indexes and detail
  files are regenerated deterministically from the entry stores by pure
  functions (`indexes.py`); there is no dual-write consistency problem.
  Bounds are measured over **exactly the rendered strings**, so "the index
  fits in N characters" means the same thing to the compactor, the `state`
  snapshot, and the reader.
- **Memory must improve future task handling**, not just record the past.
- **Vendor-neutral mechanics.** All bookkeeping (bounds, freshness,
  forgetting, index rendering, compaction orchestration) is pure Python
  over vendor-neutral markdown/frontmatter. Length is measured in
  **characters**, never tokens. No dependency on Claude's skill system.
- **The persona processes its own memory, on the subscription rail.** Both
  extraction (turning a finished session into memory) and compaction run
  **as that persona, in character**, via constrained Task sub-agents (the
  sweep-memory pattern) — never an inline `claude -p`, never an in-process
  vendor API call. The AI steps are *staged* as prompt files; the CLI verbs
  around them are non-AI glue.

## 3. What we retired (ADR 0007)

Deleted outright — the diary and fuzzy stores and everything coupled to
them:

- Modules: `diary.py`, `diary_format.py`, `diary_finalize.py`,
  `migrate_emotional_to_diary.py`, `regenerate_diary.py`, `fuzzy_store.py`,
  `fuzzy_recompact.py`, `fuzz_select.py`, `evocation.py`, `reinforce.py`,
  `meditation.py`, `import_legacy.py` (and their tests).
- `DiaryEntry`, the `@@DIARY@@` extraction-contract section, the
  `weight_cap` / decay / evocation config, and every diary/fuzzy branch in
  `bounded_store` / `check` / `state` / `briefing` / `sweep` / `lifecycle` /
  `cli`.
- The in-process **meditation** engine — replaced by the staged compaction
  flow (§6). Meditation called an AI summarizer seam in process (an
  API-billed path the team's billing model bans, never wired into the CLI).
- The CLI verbs `import-legacy` and `migrate-emotional-to-diary`.

Existing persona stores move over via `tiger-memory migrate-to-topics`
(`migrate_topics.py`): it retires `diary.md` / `fuzzy.md` / `emotional.md`
(+ their `.rejected` sidecars) from `journal/` to `<root>/retired/` —
nothing loads them any more, but the content stays on disk (plus git
history) — and creates an empty `topics.md`. Dry-run by default, `--apply`
to perform, idempotent.

## 4. The three bounded stores

Memory is exactly three stores per persona (`entries.py`,
`bounded_store.py`), all YAML-frontmatter entry stores under the persona's
`journal/` dir. What the persona *loads*, however, is not the store files —
it is the rendered surfaces (§7).

| Store | Entry store (source of truth) | Loaded at session start | Bounds (chars, `max` / must-compact) |
|---|---|---|---|
| `skills` | `journal/skills.md` | `skill_index.md` — index only; per-skill detail files on demand | index 2000 / 3000; per-skill detail 4000 / 6000 |
| `must_remember` | `journal/must_remember.md` | the whole (small) store | 2000 / 3000 |
| `topics` | `journal/topics.md` | `topic_index.md` — index only; per-topic detail files on demand | index 2000 / 3000; per-topic detail 4000 / 6000 |

Every bound is a two-number pair (`*_max_length` + `*_overflow_limit`)
giving **hysteresis**: a surface may drift above `max`; crossing the
overflow limit ("must compact") makes it a mandatory compaction target on
the next sweep. This prevents compact-every-session thrash.
`bounded_store.is_over_overflow` measures the **rendered index** for
skills/topics and the flat entry length for must_remember;
`is_detail_over_overflow` measures one entry's rendered detail.

### 4.1 Skills — learned, invokable, vendor-neutral

- A skill (`SkillEntry`) captures "something I learned to do better": a
  `name`, a *when-to-use* `trigger`, the `procedure`/lesson, provenance,
  a `usage_count`, and an `importance` scalar (`log1p(usage_count)`,
  `skills.skill_importance` — monotonic in usage, no continuous
  time-decay; recency is a keep-rank tie-break).
- **Index**: one compact block per skill (name, trigger, one-line lesson,
  pointer to its detail file), ordered importance-desc
  (`indexes.render_skill_index`). Bounded by `index_max_length` /
  `index_overflow_limit` — the skills store is now **index-length bounded,
  not count bounded** (the old `max_count` is gone).
- **Detail**: each skill's full procedure renders to its own briefing file,
  `briefing/skills/<slug(name)>-<id>.md` (the id disambiguates duplicate
  names until compaction merges them), bounded per-skill by
  `detail_max_length` / `detail_overflow_limit`.
- This is our own progressive-disclosure mechanism, **not** Claude's
  `.claude/skills/`, and persona-private.

### 4.2 Must-remember — external directives

- `MustRememberEntry`: text, `kind` (`operator_explicit` / `preference` /
  `decision` / `incident`), `importance`, `repeat_count` (a fact seen N
  times outranks a one-off), source + date.
- Loads whole at session start, so it must stay small: bounds tightened by
  ADR 0007 (from 8000/10000), now **2000 / 3000** chars (Operator-set
  2026-07-23).
- `pin --kind operator_explicit` writes importance 5.0; extracted entries
  start at 1.0.
- **Freshness — the TOUCH mechanism.** The extraction prompt embeds the
  persona's current must-remember items (one line each: id, kind, memo —
  `indexes.render_must_remember_touch_list`, filled into
  `{must_remember_index}`). The extraction card's `@@MUST_REMEMBER@@`
  section may contain `TOUCH: <id>` blocks — zero or more, mixed freely
  with the `KIND:`/`MEMO:` blocks — marking existing items this session
  *related to* (followed it, was constrained by it, subject came up again).
  Ingest refreshes a touched item's `last_used` and bumps its
  `repeat_count`; unknown ids are ignored (the item may have been
  compacted away between plan and ingest). Touches are reported in
  `IngestResult.touched` — they are refreshes, not additions. An item
  untouched for `must_remember.forget_days` (default 30, must be ≥ 0)
  becomes **forget-eligible** at compaction (§6).
- **Protection**: a *fresh* `operator_explicit` entry is never dropped by
  compaction — cards carry protected entries over verbatim (§6), and the
  store-level `bounded_store.forget` guard refuses to drop them. A *stale*
  `operator_explicit` (untouched past `forget_days`) may be dropped by the
  deterministic convergence only as the very last resort, and the drop is
  logged (§6.3) — never silently. A must_remember surface that cannot get
  under `max` without touching fresh protected content stays over-max and
  is reported (`still_over`), never silently violated.

### 4.3 Topics — durable project knowledge, filed by subject

- A topic (`TopicEntry`) is a named, growing body of knowledge about one
  subject (a subsystem, an ongoing effort, a recurring theme): a human
  `name`, a unique `slug` (auto-derived from the name, `topic_slug`), a
  one-to-two-sentence index `summary`, a `touch_count` (≥ 1), and the
  detail body — dated `## YYYY-MM-DD` sections of appended facts.
  `last_used` doubles as *last-touched*.
- **Index**: one compact block per topic (name, slug, freshness, summary),
  ordered **most-recently-touched first** (`indexes.render_topic_index`) —
  freshness is the reader's default sort.
- **Detail**: each topic's dated body renders to
  `briefing/topics/<slug>.md`, bounded per-topic by `detail_max_length` /
  `detail_overflow_limit`.

## 5. Topic lifecycle

- **Routing at ingest** (sweep card contract v2): the extraction prompt
  embeds the persona's current topic routing list
  (`indexes.render_topic_routing_list`, filled into `{topic_index}` by
  `plan_extraction`); the extractor emits `@@TOPICS@@` blocks that either
  name an existing slug or `NEW`:

  ```
  TOPIC: <existing-slug | NEW>
  NAME: <required for NEW>
  SUMMARY: <required for NEW; for an existing topic only when the old summary no longer fits>
  DETAIL: <the new durable facts from this session — always required>
  ```

  Existing slug → `ingest_candidates` appends the detail as a dated bullet
  under the right `## YYYY-MM-DD` section of the topic body, bumps
  `touch_count`, sets `last_used` to now, and refreshes the summary if one
  was given. `NEW` → a topic is minted (slug from name). An "existing" slug
  that no longer exists **revives** as a new topic. A malformed block is
  dropped, never the bundle.
- **Freshness** (`fresh_days`, default 7): a topic touched within the
  window is protected from forget/merge during compaction.
- **Forget** (`forget_days`, default 60; must be ≥ `fresh_days`, else
  `ConfigError`): a topic not touched for this long is forget-eligible.
  When the topic index is over `max`, `compact-plan`'s deterministic
  pre-pass drops stale topics **oldest-first** — no AI judgement needed for
  "not refreshed in two months".
- **Merge**: compaction may fold near-duplicate topics into one (union of
  details, one summary, newest `last_used`, summed touches) via the
  `@@TOPIC_ROSTER@@` card actions (§6).

## 6. Staged compaction — replaces meditation

Compaction (`compaction.py`) has the same subscription-rail shape as the
sweep's extraction: non-AI plan → Task sub-agents write cards → non-AI
apply. The bulky store content transits only the sub-agent's context, never
the driver's.

### 6.1 `compact-plan` (non-AI)

Scans the three stores. First the **deterministic pre-passes**: exact
duplicate memos (same kind + whitespace/case-normalized text) and exact
duplicate skills are merged loss-free into their first occurrence (repeat
and usage counters summed — sweeps re-capture the same directive verbatim,
so this alone is often enough); then topics stale beyond `forget_days`
are dropped oldest-first while the topic index is over `index_max_length`
(never touching a topic inside the window). Then, for
every surface still **at/over its overflow limit**, it writes one prompt
under `<root>/.compact-staging/` (templates `compact_*.md` in
`summarizers/prompts/default/v1/`) plus `manifest.json`. In the
must_remember prompt, an item untouched for more than
`must_remember.forget_days` is annotated **`[forget-eligible]`** with its
age — the sweep's TOUCH mechanism has not refreshed it, so the card is
told to drop it unless it is still clearly valuable despite its age. The
manifest:

```json
{"generated_at": "...", "dropped_stale_topics": ["..."],
 "deduped_skills": 0, "deduped_must_remember": 0,
 "targets": [{"kind": "...", "key": "...", "prompt_path": "...", "card_path": "..."}]}
```

Target kinds: `must_remember`, `skills` (the index roster), `topic_roster`,
`topic_detail` (carries `slug`), `skill_detail` (carries `entry_id`). An
empty `targets` list means nothing needs compacting.

### 6.2 Card sub-agents (Task tool, subscription-billed)

One sub-agent per staged prompt reads `<key>.prompt.md` and writes
`<key>.card.md` — the compacted replacement, per the prompt's embedded
strict marker contract:

- `@@MUST_REMEMBER@@` — `KIND:` / `MEMO:` blocks, plus optional
  `STALE: <id>` blocks. The prompt shows the protected
  `operator_explicit` entries separately (with their ids) alongside the
  team mission; the card may mark one `STALE:` when it fails that
  relevance check, which DOWNGRADES it to `decision` (rejoining the
  normal pool) — never a direct drop. Any `operator_explicit`
  `KIND:` block in a *card* is ignored (a compaction cannot mint
  operator directives), and the still-protected entries are carried
  over verbatim by the apply.
- `@@SKILLS@@` — `NAME:` / `TRIGGER:` / `PROCEDURE:` blocks; a **full
  replacement roster** (merge/forget by rewriting). `usage_count` /
  `created_at` / `last_used` are carried over by case-insensitive name
  match.
- `@@TOPIC_ROSTER@@` — `ACTION:` blocks: `forget`, `merge`, or `summary`
  (tighten a summary). Fresh topics are protected from forget/merge — a
  violating action logs a warning and is skipped.
- `@@TOPIC_DETAIL@@` — the marker, then the full rewritten dated body.

### 6.3 `compact-apply` (non-AI, deterministic convergence)

Validates each card (`CompactionParseError` on a bad one) and applies it
atomically to the entry store (the briefing's rendered indexes follow at
the sweep's subsequent `rebuild`). Returns an `ApplyReport`
(`applied`, `skipped_no_card`, `malformed`, `forced_trims`, `still_over`):

- **Convergence is guaranteed deterministically**: after each apply, a
  surface still over its `max` is hard-trimmed by keep-rank / freshness
  rules (`forced_trims`) — a card is never accepted oversized. For
  must_remember the drop order (`_mr_drop_order`) is: **(1)** stale normal
  entries (untouched past `forget_days`), oldest `last_used` first —
  the TOUCH mechanism kept everything that still comes up fresh; **(2)**
  fresh normal entries, lowest (importance, recency) first; **(3)** only
  as the very last resort, a *stale* `operator_explicit` directive,
  oldest first — each such drop is logged as a warning, never silent.
- **Protections beat convergence**: content that may not be trimmed
  (*fresh* operator-explicit directives, fresh topics) is never
  force-dropped; a surface that cannot shrink without touching it lands
  in `still_over` and stays flagged for the next sweep.
- Applied targets' prompt + card files are deleted; **malformed cards are
  kept** (reported, surface stays flagged, next sweep retries). A
  `skipped_no_card` target simply re-stages next sweep. Missing manifest →
  `FileNotFoundError` (CLI exit 2); ≥ 1 malformed card → exit 1.

The sweep's per-persona step is therefore: `plan` → extraction sub-agents →
`ingest-staged` → `compact-plan` → compaction sub-agents (if any targets) →
`compact-apply` → `rebuild` → `sweep-done`
(see [tiger-memory-sweep-protocol.md](tiger-memory-sweep-protocol.md)).

## 7. Session-start protocol (the briefing)

`briefing.rebuild_briefing` writes `briefing/` atomically (temp-dir swap,
with a `.fingerprint` no-op shortcut over the three journal store files):

- `README.md` — the read-order + usage rules (generated from
  `templates/briefing_readme.md`);
- `UNPROCESSED.md` — the unprocessed/active-session notice: memory is built
  only after a session goes idle, so **if the Operator references something
  you don't recognise, check memory first, then check for
  unprocessed/active sessions, before claiming ignorance**;
- `must_remember.md` — the whole (≤ ~2000 chars) store, rendered;
- `skill_index.md` + read-only detail copies under `briefing/skills/`;
- `topic_index.md` + read-only detail copies under `briefing/topics/`;
- `MANIFEST.md` — the inventory.

**The initial load is the three small files only** (`must_remember.md`,
`skill_index.md`, `topic_index.md`, plus the notice); a detail file is
opened only when its index line is relevant. Bootstrap cost is O(indexes),
not O(store).

## 8. Config schema (the `memory:` block)

`tiger_memory/config.py` parses and validates this block; every key is
optional (these are the defaults, Operator-set 2026-07-23).

```yaml
memory:
  length_unit: characters        # CONFIRMED: characters, never tokens
  skills:
    index_max_length: 2000       # the rendered skill index (chars)
    index_overflow_limit: 3000
    detail_max_length: 4000      # each skill's detail file (chars)
    detail_overflow_limit: 6000
  must_remember:
    max_length: 2000             # chars; loads whole, so kept small
    overflow_limit: 3000
    forget_days: 30              # untouched (no TOUCH) for => forget-eligible (>= 0)
  topics:
    index_max_length: 2000       # the rendered topic index (chars)
    index_overflow_limit: 3000
    detail_max_length: 4000      # each topic's detail file (chars)
    detail_overflow_limit: 6000
    fresh_days: 7                # touched within => protected from forget/merge
    forget_days: 60              # untouched for => forget-eligible (>= fresh_days)

memory_extract:                  # per-section word budgets for extraction
  skill_procedure_words: 120
  memo_words: 25
  topic_summary_words: 25       # a topic's one-line index summary
  topic_detail_words: 80        # the per-session detail appended to a topic
  max_output_words: 600
```

Validation is fail-fast at load time: `length_unit` must be `characters`
(token units are rejected); every bound pair must satisfy
`0 < max < overflow_limit` (the hysteresis band);
`must_remember.forget_days >= 0`; for topics `fresh_days >= 0` and
`forget_days >= fresh_days` (a topic cannot be simultaneously
protected-fresh and forget-eligible).

## 9. Vendor decoupling (explicit)

- Length measured in **characters**, never tokens (tokenization is
  vendor-specific and rejected at config load).
- Bounds, freshness, forgetting, index rendering, and compaction
  orchestration are pure Python over vendor-neutral markdown/frontmatter.
- The AI judgement steps (extraction, compaction cards) are **staged as
  prompt files** and executed by whatever agent runs the sweep — no
  in-process model call sits on the sweep path at all. The pluggable
  summarizer registry remains for the in-process `extract_and_ingest`
  convenience path and the prompt-template loader.
- The skill system is **ours**, not Claude's `.claude/skills/`.

## 10. Code map

| Module | Role |
|---|---|
| `config.py` | the `memory:` / `memory_extract:` blocks + fail-fast validation |
| `entries.py` | the three entry schemas (`SkillEntry` / `MustRememberEntry` / `TopicEntry`), `topic_slug`, validation + frontmatter bridge |
| `frontmatter.py` | the YAML-frontmatter parser/writer under the entry stores |
| `bounded_store.py` | crash-safe store I/O, `index_chars` / `detail_chars` / `length_chars`, `is_over_overflow` / `is_detail_over_overflow`, per-store lock, the guarded `forget` (`ForgetGuardError` / `StoreLockHeld`) |
| `indexes.py` | pure renderers — the single source of what a persona loads: skill/topic index, detail bodies, detail filenames, the topic routing list, the must-remember touch list |
| `skills.py` | `log1p(usage)` importance + skills keep-rank |
| `ranking.py` | shared recency / date-math helpers |
| `lifecycle.py` | extraction (`parse_extraction` / `extract_candidates` / `ingest_candidates` with topic routing), staging (`plan_extraction`, stacks, chunk map/reduce), `rebuild` (drop legacy surface → `check_all --fix` → briefing), `pin`, `team_mission_text` |
| `executor.py` | staged-card ingest glue (`ingest_extraction` → `IngestResult(skills_added, must_remember_added, topics_added, touched)` — `touched` counts must-remember items whose freshness the bundle's `TOUCH:` blocks refreshed) |
| `compaction.py` | staged compaction (§6): `compact_plan` (deterministic stale-topic forget + prompt staging) and `compact_apply` (card validation, deterministic convergence, `ApplyReport`) |
| `migrate_topics.py` | one-off, idempotent `migrate-to-topics`: retire diary/fuzzy/emotional files to `<root>/retired/`, create `topics.md` |
| `check.py` | `tiger-memory check [--fix]` format gate + quarantine, three frontmatter stores |
| `state.py` | the `tiger-memory state` JSON snapshot: per store `count` / `chars` (index chars for skills/topics, entry length for must_remember) / `max` / `over_overflow` / `bound_unit` + `details_over_overflow` for skills/topics |
| `briefing.py` | session-start assembly (§7): README, notice, must_remember, indexes, detail copies, MANIFEST, fingerprint |
| `sweep.py` | team-sweep gating (claim / done / complete / release) |
| `cursor.py` | per-session high-water-mark cursors (`.sweep-cursors.json`, ADR 0006 Part 2) |
| `prefilter.py` | transcript pre-filter (drop tool results / system reminders before staging) |
| `metrics.py` | lightweight rebuild instrumentation stamped into `state.json` |
| `store.py` | on-disk layout (`Paths`) + crash-safe serialization (`atomic_write`, `atomic_swap_dir`, state helpers) |
| `sources/` | source adapters (claude_code, slack_thread, docs, journal_worklog, auto_memory) |
| `summarizers/` | the prompt-template tree (`prompts/default/v1/` — `extract_memory.md`, `chunk_condense.md`, the `compact_*.md` set) + the pluggable in-process backends |
| `cli.py` | `init` / `rebuild` / `pin` / `migrate-to-topics` / `state` / `plan` / `ingest-extraction` / `build-reduce-prompts` / `ingest-staged` / `compact-plan` / `compact-apply` / `sweep-*` / `check` |

## 11. History

- **2026-06-17** — the original bounded-store revamp: three stores
  (skills / must_remember / emotional-then-diary) + the in-process
  meditation engine, replacing the chronological-rollup model
  (`docs/history/tiger-memory-rework.md`).
- **2026-06-19** — the 4-store diary/fuzzy evolution: weighted dated diary
  bullets, the fuzzy coarsening store, associative reinforcement
  (evocation), `owner_explicit` → `operator_explicit`.
- **2026-07-23** — **ADR 0007 (this doc)**: the diary/fuzzy rail and
  meditation were retired wholesale; topics + staged compaction replaced
  them. Earlier revisions of this file describe the retired models; consult
  git history if you need the diary/fuzzy rationale. The forget-guard
  data-loss invariant survives the transition: operator-explicit
  directives are still never silently dropped (now enforced by the
  compaction protections *and* the store-level guard).

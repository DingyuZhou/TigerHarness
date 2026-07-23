---
name: sweep-memory
description: Keep the whole team's tiger-memory fresh on the subscription rail. Run at persona-session bootstrap (or when the user says "sweep memory", "refresh team memory", "rebuild memories"). Claims a team-wide sweep, then extracts each stale persona's new transcripts into their three bounded memory stores via constrained Task-tool sub-agents (subscription-billed) -- never an inline `claude -p`. A cheap no-op inside the staleness floor, so triggering every session start is safe.
---

# sweep-memory

The **in-session, subscription-billed** memory refresh. One invocation
keeps the *whole roster* fresh: any human contact with any teammate is
the heartbeat. The bulky extraction work runs in isolated **Task-tool
sub-agents** so it bills to the **subscription**, regardless of which
conversation triggered it.

This skill drives the bounded-store memory model (design
`docs/DESIGN-memory.md`): each persona has **three** bounded stores --
`skills`, `must_remember`, `diary` -- and a sweep turns a persona's
finished transcripts into entries in those stores. The canonical
runtime contract is `docs/tiger-memory-sweep-protocol.md` in the
tigerharness package. If anything here seems to contradict it, that doc
wins. This skill is the driver over the non-AI gating CLIs (`sweep-plan` /
`sweep-done` / `sweep-complete` / `sweep-release`) plus the per-persona
`plan` / `ingest-staged` / `rebuild` CLIs.

## When to use this skill

- At **persona-session bootstrap** -- any persona conversation start. It
  is a cheap no-op inside the staleness floor (default 24h), so firing on
  every session start is safe and correct.
- When the user explicitly asks to "sweep memory", "refresh team
  memory", or "rebuild memories".
- From a **drive's idle-maintenance tail** (the `drive-journal` skill):
  when a drive — including an Operator-sanctioned autodrive `claude -p`
  fire — ends with nothing actionable and nothing busy, it invokes this
  skill before stopping. Same cheap no-op guarantee applies.

The executor must be a **Task-tool sub-agent** — only an *agent session*
(interactive, or a sanctioned agentic drive such as an autodrive fire)
can spawn one. A plain daemon process (the slack-bridge) cannot, and
shelling out to `claude -p` as the executor is banned (see billing,
below) -- that is exactly why this lives in the session, not the daemon.

## The billing model (load-bearing -- do not get this wrong)

- A programmatic `claude -p` bills **API tokens**. NEVER extract by
  shelling out to `claude -p`.
- A **Task-tool sub-agent** runs in isolated context, writes files,
  returns a short message, and bills to the **subscription** regardless
  of the triggering context. THAT is the executor for every extraction.
- The bulky transcript and the bulky extraction bundle must live in the
  **sub-agent's** context, never yours -- you only ever see short
  confirmations.

## Paths and the `tiger-memory` invocation

The persona session's cwd is the team root. The team-level gating CLIs
derive `team_memories_dir = cfg.store.root.parent`, so drive them with
**any** persona's config -- use **your own** persona's config:

```bash
DRIVER=memories/<your-persona>/tiger-memory.config.yaml   # e.g. memories/Anzai/...

# `tiger-memory` is the CLI from the package's [memory] extra. If it is on
# PATH, use it directly; define it once and reuse:
TM="tiger-memory"
# If your team WORKS ON the tigerharness checkout itself (so the CLI is not
# pip-installed), run it from that checkout instead, e.g.:
#   TM="uv run --project <path-to-tigerharness> tiger-memory"
```

The per-target `plan` / `ingest-staged` / `rebuild` use
`<target.config_path>` from the sweep-plan manifest, not `$DRIVER`. Every
`tiger-memory` invocation below is written as `$TM`.

**Sub-agent caveat:** a Task sub-agent runs in a *fresh* shell, so the
`$TM` you exported in the driver shell is NOT inherited. In each
sub-agent's brief, spell out the full invocation form literally -- but
note the extraction sub-agents below do **not** run any `tiger-memory`
command at all (they only write card files); the driver runs the single
glue command.

## The procedure

### 1. Claim the team sweep

```bash
$TM --config "$DRIVER" sweep-plan --token <stable-token> --max-personas 3
```

`--max-personas 3` caps how many stale personas this wake processes, so a
big backlog spreads across several new-thread triggers instead of making
one user wait. When `remaining > 0`, the rest resume on the next wake.

For `<stable-token>`, prefer a durable id tied to this session/thread (so
a resume can re-steal its own claim). If you have none, omit `--token` --
a random uuid is minted; a crashed claim is reclaimable after the lease
anyway.

It prints JSON:

```json
{"ran": true, "reason": "claimed", "token": "...",
 "targets": [{"name": "Anzai", "config_path": ".../tiger-memory.config.yaml"}],
 "remaining": 0, "all_personas": 5}
```

- `ran == false` (`reason` is `not_due` or `busy`) -> **STOP. No work.**
- `ran == true` -> you hold the claim. `targets` is the persona list to
  process **this wake** (at most `--max-personas`).

### 2. Per target persona: stage -> extract in stacks -> glue

For each `target` in `targets`:

a. **Stage the work** (non-AI; bulky content stays out of your context):

   ```bash
   $TM --config "<target.config_path>" plan        # optionally: --max-sessions N
   ```

   It writes one prompt per idle, unprocessed transcript under
   `<store>/.sweep-staging/<uuid>.prompt.md` and prints a manifest with
   two keys: `items` (per-uuid metadata) and **`stacks`** -- a list of
   uuid-lists, **one stack per extraction sub-agent**. The packer keeps
   each stack within a content budget and an item cap
   (`sweep_stack_content_chars` / `sweep_stack_max_items`), and gives an
   oversized transcript its own solo stack. This is the cost fix: a
   backlog fans out across many small fresh contexts instead of one agent
   looping over -- and re-reading -- every transcript.

b. **Spawn ONE Task sub-agent per stack** (the trust boundary + the
   fresh-window). Stacks are independent, so run the sub-agents **in
   parallel** (a sane cap, e.g. ~6 concurrent). Each sub-agent's brief:
   - **Read**: each `<uuid>.prompt.md` in its assigned stack (the prompt
     already embeds the prefiltered transcript and the full output
     contract) and, for context, the persona's current stores
     (`skills.md` / `must_remember.md` / `diary.md` under the store's
     `journal/` dir). Nothing else.
   - **Do**: for each uuid in the stack, in order -- read the prompt and
     act **as that persona, in character**; emit ONLY the
     `@@SKILLS@@` / `@@MUST_REMEMBER@@` / `@@DIARY@@` bundle per the
     prompt's output contract; **self-validate** (all three markers
     present, each on its own line, in that order; a section is either
     well-formed blocks or the literal `NONE`); and **write that bundle
     to a card file** `<store>/.sweep-staging/<uuid>.extract.md`. **Do
     NOT ingest** and do NOT run any `tiger-memory` command -- the driver
     glues all cards in one step. Return only a short confirmation (e.g.
     `carded 5 uuids`).
   - The bulky bundle must NEVER be returned to you -- it lives only in
     the card file.
   - **It is correct to emit `NONE` for a store.** Most sessions add
     little; the stores are bounded and forgetting is first-class, so the
     sub-agent should be selective -- only durable, reusable lessons
     (skills), genuine external directives (must_remember), and real
     diary notes (diary). A card that is `NONE` / `NONE` / `NONE` is a
     valid, expected outcome.

b'. **Oversized transcripts -- the map->reduce two-phase.** Most items are
   `kind="single"` (one `<uuid>.prompt.md`, the 2b flow above). An item
   whose prefiltered content exceeds `max_staged_content_chars` is staged
   by `plan` as `kind="map_reduce"` instead: in the `items` manifest it
   carries explicit **`chunk_prompts`** and **`digest_paths`** lists and
   has **no** `prompt_path`, so the oversized middle is split losslessly
   rather than silently clipped. If `plan`'s `items` shows ANY
   `map_reduce` uuid, run this extra hop for those uuids **between 2b and
   2c** (skipping it silently drops the oversized content -- the exact
   data-loss this path closes). It is a **three-hop dance**: a map
   sub-agent, then the driver's reduce CLI, then a second (carding)
   sub-agent -- and only then does 2c run.
   - **(map -- sub-agent round 1)** the stack's Task sub-agent reads the
     **exact `chunk_prompts` paths from the manifest** (each a
     `chunk_condense` prompt -- do NOT glob the staging dir or guess
     names) and writes one digest per chunk **at the matching
     `digest_paths` path from the manifest**, as **plain neutral prose --
     NOT** the `@@SKILLS@@ / @@MUST_REMEMBER@@ / @@DIARY@@` contract (that
     stays single-sourced in the reduce). **Hand the sub-agent the
     manifest's `chunk_prompts` and `digest_paths` literally** -- a digest
     written to a path that does not match `digest_paths` makes the reduce
     below see a *missing* digest and the uuid wedges in `pending` forever.
     Same brief as 2b (read only its assigned files; return a short
     confirmation); it writes digests, **not a card**, for these uuids.
   - **(reduce -- non-AI glue, the driver runs it; no sub-agent)** once a
     uuid's digests are **all** written: `$TM --config
     "<target.config_path>" build-reduce-prompts`. It concatenates that
     uuid's digests (a bounded last-resort clip only if they *still*
     overflow), fills the single-sourced contract over them, and writes
     `<uuid>.prompt.md` -- the **same** filename and shape a single item
     has. It prints `{"built": [...], "pending": [...]}` and exits `0`
     (pending is not an error) or `2` (no manifest). **Read this as your
     health signal:** a uuid in `built` is ready to card; a uuid in
     `pending` is not fully mapped yet. A uuid that **stays in `pending`
     across more than one pass** is the symptom of a missing or **misnamed
     digest** (round 1 wrote to the wrong path) -- investigate that uuid,
     do not just re-run. Its cursor never advances until it builds, so it
     cannot silently skip, but it also never ingests until the digest is
     fixed. Never force it.
   - **(card -- sub-agent round 2)** for each now-`built` uuid the
     `<uuid>.prompt.md` is **indistinguishable from a single item**: spawn
     a 2b-style sub-agent to turn it into the `<uuid>.extract.md` card.
     **Only after this second round has carded the built uuids do you run
     2c (`ingest-staged`)** -- running `ingest-staged` straight after
     `build-reduce-prompts`, before the carding round, would skip the card
     (`skipped_no_card`) and leave the cursor unmoved.

   **To verify the oversized path end to end:** force an item over
   `max_staged_content_chars`, confirm `plan` stages it as
   `kind="map_reduce"`, walk map -> `build-reduce-prompts` (uuid lands in
   `built`) -> card -> `ingest-staged`, and confirm the uuid appears in
   `ingest-staged`'s **`ingested`** list. A uuid stuck in `pending`, or
   surfacing in `skipped_no_card`, means the dance broke at the hop named
   above.

c. **Glue the cards** (non-AI, deterministic, race-free) once **all** of
   this persona's stacks have finished carding (for a `map_reduce` uuid,
   "carded" means its reduce-built `<uuid>.prompt.md` has been turned into
   an `<uuid>.extract.md`):

   ```bash
   $TM --config "<target.config_path>" ingest-staged
   ```

   It ingests every `<uuid>.extract.md` card in **one process**, merging
   each bundle's blocks into the persona's three bounded stores, so the
   per-persona store writes are **serialized by construction** -- there
   is no ingest race to coordinate by hand. It prints
   `{"ingested": N, "malformed": [...], "skipped_no_card": M}`. Exit
   codes: `0` clean; `1` at least one **malformed** card (its uuids are
   listed -- re-extract just those with a fresh sub-agent, or leave them
   to re-stage next wake); `2` no manifest (a planning bug -- surface
   it). A skipped (no-card) item simply was not extracted this wake; the
   store, not the card, is the durable ledger, so it re-stages next wake.
   For a `map_reduce` uuid, `ingest-staged` drops its chunk prompts +
   digests alongside the card, so the staging dir is clean next pass.

d. **Finalize the persona**, then record it done:

   ```bash
   $TM --config "<target.config_path>" rebuild   # fresh-start drop + briefing
   $TM --config "$DRIVER" sweep-done --persona "<target.name>"
   ```

   `rebuild` is the **fresh-start** finalize: it drops the retired
   legacy on-disk surface (old rollup summaries, `must_memorize.md`,
   `longer_memory.md`, the `archive/` dir) the very first time, then
   regenerates the session-start briefing (the **skill index** + the full
   `must_remember` view + the top-by-magnitude `diary` view + the
   unprocessed-session notice) from the three stores. It does NOT
   re-extract -- `ingest-staged` already wrote the entries.

   **Meditation** (the bounded-store compaction: merge near-duplicates ->
   relevance-check + downgrade stale owner directives -> compact ->
   guarded-forget) runs automatically inside the sweep, per store, **only
   when that store is over its `overflow_limit`** (the hysteresis
   trigger). You do not invoke it by hand; it is a no-op when every store
   is under bound. Its only model calls (similarity / staleness /
   compaction judgements) go through the persona's summarizer, so they
   also stay on the subscription rail.

Different personas have separate stores and are independent -- you may
process several `targets` concurrently (each its own plan -> stacks ->
`ingest-staged`).

### 3. Close the run

- **All due personas processed** (`remaining == 0`) -> advance the
  watermark and clear the claim:

  ```bash
  $TM --config "$DRIVER" sweep-complete
  ```

  This **includes the empty case**: if `sweep-plan` returned `ran=true`
  with `targets == []` and `remaining == 0`, call `sweep-complete`
  **immediately** -- do not leave the claim dangling.

- **Per-wake cap hit, more remain** (`remaining > 0`) -> drop the claim
  but keep progress + the stale watermark so the next wake resumes:

  ```bash
  $TM --config "$DRIVER" sweep-release
  ```

## The three bounded stores (what a sweep is feeding)

- **`skills`** -- learned, reusable lessons (name + when-to-use trigger +
  procedure). Count-bounded. Importance grows with use; the session-start
  briefing loads only the **skill index** (name + trigger + one line),
  and the persona reads the full skill on demand.
- **`must_remember`** -- external directives (`owner_explicit` /
  `preference` / `decision` / `incident`). Length-bounded (characters).
  `owner_explicit` directives start elevated and are protected from
  forgetting until meditation's relevance-check downgrades a stale one.
- **`diary`** -- the persona's signed diary notes (`weight` in
  `[-10, +10]`, decaying toward 0). Length-bounded. Strong feelings (for
  or against) survive; near-neutral / decayed items are forgotten first.

## Guardrails (already enforced by the package)

- **Staleness floor** (default 24h) + **team watermark** -> most triggers
  are a no-op.
- **Soft-lease claim** -> one session sweeps per window; a crashed owner's
  stale claim is stolen after the lease.
- **Per-wake cap** (`--max-personas`) -> a big backlog spreads across
  wakes; `sweep-done` makes progress resumable.
- **Serialization is structural, not manual.** The extraction sub-agents
  only WRITE cards; `ingest-staged` performs the single-process merge, so
  you never hand-coordinate per-persona ingest ordering.
- **Stacks bound per-sub-agent context.** The plan packs transcripts into
  stacks so a backlog runs as many fresh contexts, not one ballooning
  one.
- **Bounded + self-pruning.** Each store has a `max` + `overflow_limit`
  (hysteresis); meditation only fires over the overflow limit and never
  drops a still-relevant `owner_explicit` directive.
- **Subscription-safe** -> the executor is always the Task sub-agent.

## What NOT to do

- **Never** extract via `claude -p` -- that bills API. The executor is
  always a Task-tool sub-agent.
- **Never** let the bulky transcript or extraction bundle into your own
  context -- a sub-agent reads the prompt files and writes card files; you
  see only short confirmations and the `ingest-staged` JSON summary.
- **Never** ingest a card by hand mid-fan-out, and never call
  `ingest-extraction` per uuid in this flow -- `ingest-staged` owns the
  merge so it stays a single, race-free process. (`ingest-extraction` is
  the one-bundle-over-stdin path for a single uuid; the stacked sweep uses
  `ingest-staged`.)
- **Never** leave a claim dangling. Every `ran=true` path ends in exactly
  one of `sweep-complete` (done / empty) or `sweep-release` (cap hit).
- **Never** hand-edit the sweep-state JSON or any store file -- drive
  state only through the CLIs above. Forgetting is irreversible with no
  safety net; let meditation, not a hand-edit, prune a store.

## If you get confused

`docs/tiger-memory-sweep-protocol.md` in the tigerharness package is the
runtime contract, and `docs/DESIGN-memory.md` is the canonical design.
Re-read them. This skill is the driver; those docs are the source of
truth.

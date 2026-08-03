---
name: sweep-memory
description: Keep the whole team's tiger-memory fresh. Runs at every sweep trigger -- the first Slack message of a new thread (the bridge's Slack-bootstrap flow), persona-session bootstrap, a drive's idle-maintenance tail, the autodrive idle path, or an explicit "sweep memory" / "refresh team memory" / "rebuild memories" ask. Claims a team-wide sweep under the split gate (the calling persona's completed-but-un-swept transcripts bypass the staleness floor; every other persona keeps it), then extracts each target persona's new transcripts into their three bounded memory stores via constrained Task-tool sub-agents -- never an inline `claude -p`. A cheap no-op when nothing is pending, so firing on every trigger is safe.
---

# sweep-memory

The **in-session** memory refresh. One invocation keeps the *whole
roster* fresh: any human contact with any teammate is the heartbeat. The
bulky extraction work runs in isolated **Task-tool sub-agents**, so the
transcripts and extraction bundles never enter the triggering
conversation's context -- you only ever see short confirmations.

This skill drives the topic-store memory model (design
`docs/DESIGN-memory.md`, ADR 0007): each persona has **three** bounded
stores -- `skills`, `must_remember`, `topics` -- and a sweep turns a
persona's finished transcripts into entries in those stores, then
compacts any surface that outgrew its bound. The canonical runtime
contract is `docs/tiger-memory-sweep-protocol.md` in the tigerharness
package. If anything here seems to contradict it, that doc wins. This
skill is the driver over the non-AI gating CLIs (`sweep-plan` /
`sweep-done` / `sweep-complete` / `sweep-release`) plus the per-persona
`plan` / `ingest-staged` / `compact-plan` / `compact-apply` / `rebuild`
CLIs.

## When to use this skill (the five triggers)

One gate rule serves every trigger (see "The split gate" below); what
differs per trigger is only the UX:

- **First Slack message of a NEW thread** -- the bridge's first turn of
  a new session. The bridge's first-turn injection names your persona
  and points here; a resumed thread/session gets no injection and no
  bootstrap sweep -- by design. Notify-first + split-mode: see "The
  Slack-bootstrap flow" below.
- **Persona-session bootstrap** -- any persona conversation start. No
  notify needed (the terminal shows the work); split-mode is optional
  when requested work is waiting.
- **A drive's idle-maintenance tail** (the `drive-journal` skill): when
  a drive -- including an Operator-sanctioned autodrive fire -- ends
  with nothing actionable and nothing busy, it invokes this skill
  before stopping. No notify; sequential processing is fine.
- **The autodrive idle path** -- same as the drive tail (autodrive
  reaches it through the drive-journal skill's prompt).
- **An explicit ask** -- "sweep memory", "refresh team memory",
  "rebuild memories". The split gate still applies by default; a forced
  full-team sweep regardless of the floor is a separate, explicit ask.

## The split gate (own persona vs. the rest of the team)

The gate rule, identical at every trigger (the CLI enforces it; you
supply the inputs):

- **Own persona -- NO staleness floor.** Whenever the calling persona
  has completed-but-un-swept transcripts (its live session never counts
  itself), a sweep of THAT persona runs now, however recently the team
  swept.
- **Other personas -- the team floor.** Everyone else stays gated by
  the team watermark + `sweep.floor_hours` exactly as before.
- **Silent cases.** Own persona has nothing pending AND the team is
  inside the floor (`reason: "not_due"`), or another session holds the
  lease (`reason: "busy"`) -> proceed straight to the requested work.
  No notice, no sweep.

**Own-persona resolution, per trigger** (pass it as `--own-persona`):

- Slack bridge -> the persona the bridge injection names
  (`state.persona`).
- drive-journal -> the drive's `--driver` persona.
- autodrive -> the driven session's persona (its `--driver`).
- Interactive persona session -> the adopted persona.
- A session with NO persona identity -> omit `--own-persona`: the
  sweep is the plain team-floor sweep exactly as before (the
  no-identity fallback).

**Your own live session never counts as pending.** Pass
`--exclude-session <your session uuid>` when you can determine it (a
drive knows its session id; an interactive session can read its own).
Omitting it is also safe: an actively-written transcript has a fresh
mtime, inside the pipeline's idle threshold, so it is never counted --
but pass the uuid when you have it.

## The Slack-bootstrap flow (notify-first)

For the Slack trigger only, the in-thread UX is part of the contract:

1. Run `sweep-plan` (step 1 below) with `--own-persona <the persona
   the bridge injection names>`.
2. `ran == false` -> say nothing about sweeping; just do the requested
   work (the silent cases above).
3. `ran == true` -> post ONE short in-thread heads-up BEFORE any sweep
   work runs (notify-first), then follow split-mode ordering (step 1's
   ordering rule): own persona blocking, others in the background
   behind the requested work. Branch the wording on the JSON's `scope`:
   - `"own-only"` -> e.g. "Catching up my memory from recent sessions
     before I answer -- one moment."
   - `"team"` -> e.g. "Refreshing team memory (mine first, the rest in
     the background) -- one moment."
   One message; no progress spam afterwards.

## The executor rule (load-bearing -- do not get this wrong)

- The executor for every extraction is a **Task-tool sub-agent**: it
  runs in an isolated context window, writes card files, and returns a
  short confirmation, so the bulky transcript and extraction bundle
  live in the **sub-agent's** context, never yours.
- NEVER extract by shelling out to `claude -p`: a shelled-out model
  process runs outside the session's supervision and context
  management -- no isolation guarantee, no oversight, no resumability.
- Only an *agent session* (interactive, or a sanctioned agentic drive
  such as an autodrive fire) can spawn a Task sub-agent. A plain daemon
  process (e.g. the slack-bridge itself) cannot -- which is why every
  trigger routes the sweep INTO a session: the bridge injects the
  bootstrap instruction into the persona session's first turn rather
  than sweeping in the daemon.

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
$TM --config "$DRIVER" sweep-plan --token <stable-token> --max-personas 3 \
    --own-persona <your-persona> --exclude-session <your-session-uuid>
```

`--own-persona` is the split gate's input -- your persona per the
resolution list above (`$DRIVER` must be that persona's config; omit the
flag entirely in the no-identity fallback). `--exclude-session` is your
current session's conversation uuid (omit if unknown -- safe, see
above). `--max-personas 3` caps how many stale OTHER personas this wake
processes, so a big backlog spreads across several triggers instead of
making one user wait -- your own persona, when pending, is never counted
against the cap. When `remaining > 0`, the rest resume on the next wake.

For `<stable-token>`, prefer a durable id tied to this session/thread (so
a resume can re-steal its own claim). If you have none, omit `--token` --
a random uuid is minted; a crashed claim is reclaimable after the lease
anyway.

It prints JSON:

```json
{"ran": true, "reason": "claimed", "token": "...", "scope": "team",
 "own": {"persona": "Anzai", "pending": true},
 "targets": [{"name": "Anzai", "config_path": ".../tiger-memory.config.yaml"}],
 "remaining": 0, "all_personas": 5}
```

- `ran == false` (`reason` is `not_due` or `busy`) -> **STOP. No work.**
  `not_due` now means "no own pending AND team inside the floor". While
  another session's own-only run holds the lease, a concurrently due
  team claim gets `busy` too -- accepted, the lease is short and the
  next trigger retries.
- `ran == true` -> you hold the claim. `targets` is the persona list to
  process **this wake**. Branch notify wording and split-mode dispatch
  on **`scope`**, never on inferred target composition:
  - `scope: "team"` -- the team floor was due. If `own.pending` is
    true, your own persona is `targets[0]` (floor-exempt); the
    `--max-personas` cap applied to the others.
  - `scope: "own-only"` -- only your own persona had pending work;
    `targets` is exactly your persona and the rest of the team keeps
    its floor. Drive the run exactly the same; at close,
    `sweep-complete` knows not to advance the team watermark (the CLI
    handles it -- nothing extra to do).
  - `own` is `null` when you omitted `--own-persona` (the no-identity
    fallback -- plain team-floor behavior).

  **Ordering vs. the requested work (split-mode).** When the trigger
  fired inside a conversation with real requested work waiting (a Slack
  thread, an interactive ask): process YOUR OWN persona's target first,
  BLOCKING, before the requested work -- your own un-swept transcripts
  are exactly the memory that work needs. Dispatch the OTHER targets'
  extraction sub-agents with `run_in_background: true`, start the
  requested work while they card in parallel, then glue + finalize each
  persona as its cards land and close the run (step 3). With no
  requested work waiting (idle tail, explicit ask), plain sequential
  processing is fine. The lease is renewed by every `sweep-done`, and a
  ~30-min claim is stealable only when you go silent.

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
     already embeds the prefiltered transcript, the persona's current
     topic routing list, and the full output contract). Nothing else.
   - **Do**: for each uuid in the stack, in order -- read the prompt;
     emit ONLY the `@@SKILLS@@` / `@@MUST_REMEMBER@@` / `@@TOPICS@@` /
     `@@TEAM_EVENTS@@` bundle (contract v3, ADR 0008) per the prompt's
     output contract; **self-validate** (all
     four markers present, each on its own line, in that order; a
     section is either well-formed blocks or the literal `NONE`; every
     `@@TOPICS@@` block routes to an existing slug from the embedded
     list or is `TOPIC: NEW` with NAME + SUMMARY + DETAIL; the
     `@@MUST_REMEMBER@@` section may also carry `TOUCH: <id>` blocks
     marking embedded existing items this session relied on -- touching
     keeps them fresh, and an item untouched past its forget window
     becomes forgettable; the `@@TEAM_EVENTS@@` section is 0-3
     `EVENT: <verb-first past-tense clause, no persona name>` lines of
     real work DONE -- ingest prefixes the name and files them, dated,
     into the team-wide event log); and **write that bundle to a card file**
     `<store>/.sweep-staging/<uuid>.extract.md`. **Do NOT ingest** and do
     NOT run any `tiger-memory` command -- the driver glues all cards in
     one step. Return only a short confirmation (e.g. `carded 5 uuids`).
   - The bulky bundle must NEVER be returned to you -- it lives only in
     the card file.
   - **It is correct to emit `NONE` for a store.** Most sessions add
     little; the stores are bounded and forgetting is first-class, so the
     sub-agent should be selective -- only durable, reusable lessons
     (skills), genuine external directives (must_remember), and durable
     project knowledge filed by topic (topics; prefer routing into an
     existing topic over minting a new one). A card that is `NONE` /
     `NONE` / `NONE` is a valid, expected outcome.

b'. **Oversized transcripts (kind="map_reduce") -- three-hop dance.**
   If `plan`'s `items` shows ANY `map_reduce` uuid (it carries
   `chunk_prompts` + `digest_paths`, no `prompt_path`), run, between
   2b and 2c: (map) a sub-agent writes one plain-prose digest per
   chunk at the manifest's EXACT `digest_paths` (never the 4-marker
   contract, never guessed paths); (reduce, non-AI)
   `$TM --config "<target.config_path>" build-reduce-prompts` -- a
   uuid in `built` is ready, one stuck in `pending` across passes
   means a misnamed digest (investigate, never force); (card) a
   2b-style sub-agent turns the built `<uuid>.prompt.md` into its
   `.extract.md` card. Only then run 2c. Full contract + acceptance
   walkthrough: `docs/tiger-memory-sweep-protocol.md`.

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
   `{"ingested": N, "malformed": [...], "locked": [...],
   "skipped_no_card": M}`. Exit
   codes: `0` clean; `1` at least one **malformed** card (its uuids are
   listed -- re-extract just those with a fresh sub-agent, or leave them
   to re-stage next wake); `2` no manifest (a planning bug -- surface
   it). A skipped (no-card) item simply was not extracted this wake; the
   store, not the card, is the durable ledger, so it re-stages next wake.
   A `locked` uuid hit a stuck store lock -- it also re-stages next wake.
   **Anomaly check:** after a COMPLETED fan-out, `ingested: 0` with
   `skipped_no_card > 0` means misnamed/missing cards (or a premature
   glue), not a quiet team -- the CLI now warns on stderr; check the
   staging dir before moving on instead of treating exit 0 as success.
   For a `map_reduce` uuid, `ingest-staged` drops its chunk prompts +
   digests alongside the card, so the staging dir is clean next pass.

d. **Compact what outgrew its bound** (staged, same sub-agent shape as
   extraction):

   ```bash
   $TM --config "<target.config_path>" compact-plan
   ```

   Non-AI. It first runs the deterministic stale-topic forget (only once
   the topic index is at/over its `overflow_limit`, topics not refreshed
   within `forget_days` drop oldest-first until the index is back at or
   under its `max`), then stages one prompt per surface still
   at/over its `overflow_limit` under `<store>/.compact-staging/` and
   prints a manifest (`targets`: kind / key / `prompt_path` /
   `card_path`). **`targets: []` -> skip straight to step e** (the
   common case).

   Otherwise, spawn **ONE Task sub-agent per target** (parallel, same
   cap). Each sub-agent's brief: read its `prompt_path` (the prompt
   embeds the store content and the strict output contract), emit ONLY
   the contracted replacement, **write it to exactly `card_path`**, run
   no `tiger-memory` command, and return a one-line confirmation. Then
   glue:

   ```bash
   $TM --config "<target.config_path>" compact-apply
   ```

   Non-AI, one process. It validates every card, applies them atomically
   (fresh operator-explicit directives are carried over verbatim -- a
   card cannot drop them, only mark one STALE for a downgrade; a stale
   one, untouched past `forget_days`, may be forgotten by the
   deterministic trim as a logged last resort; fresh topics are
   protected from forget/merge), and
   deterministically trims any surface a card left over its `max`. It
   prints `{"applied": [...], "skipped_no_card": [...], "malformed":
   [...], "forced_trims": [...], "still_over": [...], "locked": [...]}`;
   exit `1` means a
   malformed card (re-run just that target with a fresh sub-agent, or
   leave it -- the surface re-stages next wake), `2` means no manifest.
   A `locked` target hit a live lock and was skipped (it re-stages next
   wake). A fully-clean apply consumes the manifest, so a mistaken
   re-apply gets the loud exit `2` instead of a silent "clean" no-op.

e. **Finalize the persona**, then record it done:

   ```bash
   $TM --config "<target.config_path>" rebuild   # format gate + briefing
   $TM --config "$DRIVER" sweep-done --persona "<target.name>"
   ```

   `rebuild` drops any retired legacy surface, format-checks the three
   stores (`check --fix` semantics: mechanical repair + quarantine), and
   regenerates the session-start briefing: `must_remember.md`,
   `skill_index.md`, `topic_index.md` (the ONLY files a persona loads at
   bootstrap) plus the per-skill and per-topic detail files under
   `briefing/skills/` and `briefing/topics/`. It does NOT re-extract --
   `ingest-staged` already wrote the entries.

Different personas have separate stores and are independent -- you may
process several `targets` concurrently (each its own plan -> stacks ->
`ingest-staged`).

### 3. Close the run

- **All due personas processed** (`remaining == 0`) -> first fold the
  **team event log** (ADR 0008; team-level, once per completed sweep,
  while you still hold the claim):

  ```bash
  $TM --config "$DRIVER" team-events-compact-plan
  ```

  `targets: []` (the common case) -> move on. Otherwise spawn ONE Task
  sub-agent per staged prompt (read its `prompt_path`; write the folded
  bullets to exactly `card_path` per the prompt's strict
  `@@TEAM_EVENTS@@` contract; run no `tiger-memory` command; return a
  one-line confirmation), then glue:

  ```bash
  $TM --config "$DRIVER" team-events-compact-apply
  ```

  Exit `1` = a malformed card (leave it; the fold re-stages next
  sweep); `2` = no manifest (a sequencing bug -- surface it).

  Then close the run, passing YOUR claim token (from `sweep-plan`'s
  JSON) so a stale driver whose lease was stolen can never clobber the
  live owner's run (exit `3` = refused, another session owns the sweep
  now -- stop, do not force). A `scope: "team"` run advances the team
  watermark; a `scope: "own-only"` run completes WITHOUT advancing it
  (the team's floor window is untouched -- the CLI reads the claim's
  scope, nothing extra to pass):

  ```bash
  $TM --config "$DRIVER" sweep-complete --token <token>
  ```

  This **includes the empty case**: if `sweep-plan` returned `ran=true`
  with `targets == []` and `remaining == 0`, still run the team-events
  fold above, then call `sweep-complete`
  **immediately** -- do not leave the claim dangling.

- **Per-wake cap hit, more remain** (`remaining > 0`) -> drop the claim
  but keep progress + the stale watermark so the next wake resumes:

  ```bash
  $TM --config "$DRIVER" sweep-release --token <token>
  ```

  The roster walk orders personas least-recently-swept first (durable
  `done_at` map), so repeated capped wakes rotate through the whole
  roster instead of re-sweeping the same head. If you have time in this
  session, you MAY immediately re-run `sweep-plan --token <same>` after
  the release and keep draining the backlog in 3-persona chunks --
  re-claiming your own token is allowed while the watermark is stale.

## The three bounded stores (what a sweep is feeding)

- **`skills`** -- learned, reusable lessons (name + when-to-use trigger +
  procedure). The rendered **skill index** is length-bounded
  (characters); the session-start briefing loads only that index, and
  each skill's procedure is a separate detail file read on demand.
- **`must_remember`** -- external directives (`operator_explicit` /
  `preference` / `decision` / `incident`). Length-bounded (characters).
  `operator_explicit` directives are protected: compaction carries
  **fresh** ones over verbatim. One untouched past `forget_days` may be
  relevance-downgraded (STALE -> `decision`) or, as a logged last
  resort, forgotten by the deterministic trim -- sweeps TOUCH items that
  come up, which is what keeps live directives fresh.
- **`topics`** -- named, growing bodies of durable project knowledge.
  The rendered **topic index** (slug + freshness + one-line summary,
  freshest first) is length-bounded and is the only topic surface loaded
  at session start; each topic's dated detail body is a separate file,
  itself bounded per-topic. Sweeps route new facts into existing topics
  (or mint new ones); compaction merges near-duplicate topics and
  forgets ones not refreshed within `forget_days`.

## Guardrails (already enforced by the package)

- **Split gate**: the team floor (default 24h) + **team watermark**
  make most triggers a no-op; the own-persona bypass fires only on real
  pending work, and an own-only run never advances the team watermark.
- **Lease renewal** -> every `sweep-done` refreshes the claim lease, so
  a healthy long run (many personas, big fan-outs) is never stolen
  mid-flight; only a genuinely silent driver loses the claim.
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
- **Bounded + self-pruning.** Every surface has a `max` +
  `overflow_limit` (hysteresis); compaction only stages over the
  overflow limit, never drops a *fresh* `operator_explicit` directive
  (a stale one goes only as a logged last resort), and
  never forgets/merges a fresh topic.
- **Context-safe** -> the executor is always the Task sub-agent
  (extraction AND compaction); bulky content stays in sub-agent
  windows.

## What NOT to do

- **Never** extract via `claude -p` -- the executor is always a
  Task-tool sub-agent (the executor rule above: isolation, oversight,
  resumability).
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
  safety net; let the staged compaction, not a hand-edit, prune a store.

## If you get confused

`docs/tiger-memory-sweep-protocol.md` in the tigerharness package is the
runtime contract, and `docs/DESIGN-memory.md` is the canonical design.
Re-read them. This skill is the driver; those docs are the source of
truth.

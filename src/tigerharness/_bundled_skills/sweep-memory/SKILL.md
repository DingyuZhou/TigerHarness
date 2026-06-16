---
name: sweep-memory
description: Keep the whole team's tiger-memory fresh on the subscription rail. Run at persona-session bootstrap (or when the user says "sweep memory", "refresh team memory", "rebuild memories"). Claims a team-wide sweep, then summarizes each stale persona's new transcripts via constrained Task-tool sub-agents (subscription-billed) -- never an inline `claude -p`. A cheap no-op inside the staleness floor, so triggering every session start is safe.
---

# sweep-memory

The **in-session, subscription-billed** memory rebuild. One invocation
keeps the *whole roster* fresh: any human contact with any teammate is
the heartbeat. The bulky summarize work runs in isolated **Task-tool
sub-agents** so it bills to the **subscription**, regardless of which
conversation triggered it.

The canonical contract is `docs/tiger-memory-sweep-protocol.md` in the
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

Do NOT drive this from a non-interactive `claude -p` / cron / API
context. The executor must be a **Task-tool sub-agent**, which only a
running interactive session can spawn. A daemon (the slack-bridge) cannot
-- that is exactly why this lives in the session, not the daemon.

## The billing model (load-bearing -- do not get this wrong)

- A programmatic `claude -p` bills **API tokens**. NEVER summarize by
  shelling out to `claude -p`.
- A **Task-tool sub-agent** runs in isolated context, writes files,
  returns a short message, and bills to the **subscription** regardless
  of the triggering context. THAT is the executor for every summary.
- The bulky transcript and the bulky summary bundle must live in the
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
note the summarize sub-agents below do **not** run any `tiger-memory`
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

### 2. Per target persona: stage -> summarize in stacks -> glue

For each `target` in `targets`:

a. **Stage the work** (non-AI; bulky content stays out of your context):

   ```bash
   $TM --config "<target.config_path>" plan        # optionally: --max-sessions N
   ```

   It writes one prompt per flagged transcript under
   `<store>/.sweep-staging/<uuid>.prompt.md` and prints a manifest with
   two keys: `items` (per-uuid metadata) and **`stacks`** -- a list of
   uuid-lists, **one stack per summarize sub-agent**. The packer keeps
   each stack within a content budget and an item cap
   (`sweep_stack_content_chars` / `sweep_stack_max_items`), and gives an
   oversized transcript its own solo stack. This is the cost fix: a
   backlog fans out across many small fresh contexts instead of one agent
   looping over -- and re-reading -- every transcript.

b. **Spawn ONE Task sub-agent per stack** (the trust boundary + the
   fresh-window). Stacks are independent, so run the sub-agents **in
   parallel** (a sane cap, e.g. ~6 concurrent). Each sub-agent's brief:
   - **Read**: each `<uuid>.prompt.md` in its assigned stack (the prompt
     already embeds the prefiltered transcript) and the persona's store.
     Nothing else.
   - **Do**: for each uuid in the stack, in order -- read the prompt;
     emit ONLY the `@@SHORT@@` / `@@DETAILED@@` / `@@MUST_MEMORIZE@@`
     bundle per the prompt's output contract; **self-validate** (all
     three markers present, in order; short + detailed non-empty); and
     **write that bundle to a card file**
     `<store>/.sweep-staging/<uuid>.summary.md`. **Do NOT ingest** and do
     NOT run any `tiger-memory` command -- the driver glues all cards in
     one step. Return only a short confirmation (e.g. `carded 5 uuids`).
   - The bulky bundle must NEVER be returned to you -- it lives only in
     the card file.

c. **Glue the cards** (non-AI, deterministic, race-free) once **all** of
   this persona's stacks have finished carding:

   ```bash
   $TM --config "<target.config_path>" ingest-staged
   ```

   It ingests every `<uuid>.summary.md` card in **one process**, so the
   per-persona must-memorize merge is **serialized by construction** --
   there is no ingest race to coordinate by hand. It prints
   `{"ingested": N, "malformed": [...], "skipped_no_card": M}`. Exit
   codes: `0` clean; `1` at least one **malformed** card (its uuids are
   listed -- re-summarize just those with a fresh sub-agent, or leave
   them to re-stage next wake); `2` no manifest (a planning bug -- surface
   it). A skipped (no-card) item simply was not summarized this wake; the
   store, not the card, is the durable ledger, so it re-stages next wake.

d. **Finalize the persona**, then record it done:

   ```bash
   $TM --config "<target.config_path>" rebuild   # rollups, decay, briefing
   $TM --config "$DRIVER" sweep-done --persona "<target.name>"
   ```

   (`ingest-staged` already wrote the summaries, so `rebuild`'s summarize
   step is a clean no-op; it just runs the finalize tail.)

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

## Guardrails (already enforced by the package)

- **Staleness floor** (default 24h) + **team watermark** -> most triggers
  are a no-op.
- **Soft-lease claim** -> one session sweeps per window; a crashed owner's
  stale claim is stolen after the lease.
- **Per-wake cap** (`--max-personas`) -> a big backlog spreads across
  wakes; `sweep-done` makes progress resumable.
- **Serialization is structural, not manual.** The summarize sub-agents
  only WRITE cards; `ingest-staged` performs the single-process merge, so
  you never hand-coordinate per-persona ingest ordering.
- **Stacks bound per-sub-agent context.** The plan packs transcripts into
  stacks so a backlog runs as many fresh contexts, not one ballooning
  one.
- **Subscription-safe** -> the executor is always the Task sub-agent.

## What NOT to do

- **Never** summarize via `claude -p` -- that bills API. The executor is
  always a Task-tool sub-agent.
- **Never** let the bulky transcript or summary bundle into your own
  context -- a sub-agent reads the prompt files and writes card files; you
  see only short confirmations and the `ingest-staged` JSON summary.
- **Never** ingest a card by hand mid-fan-out, and never call
  `ingest-summary` per uuid in this flow -- `ingest-staged` owns the
  merge so it stays a single, race-free process.
- **Never** leave a claim dangling. Every `ran=true` path ends in exactly
  one of `sweep-complete` (done / empty) or `sweep-release` (cap hit).
- **Never** hand-edit the sweep-state JSON or any store file -- drive
  state only through the CLIs above.

## If you get confused

`docs/tiger-memory-sweep-protocol.md` in the tigerharness package is the
contract. Re-read it. This skill is the driver; the protocol doc is the
source of truth.

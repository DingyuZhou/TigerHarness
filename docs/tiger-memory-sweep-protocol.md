# tiger-memory team sweep — in-session sub-agent protocol (B1/B3)

> **Status:** wired & shipped. The full Python + CLI stack this protocol
> drives is shipped and tested (see `history/tiger-memory-rework.md`, B1/B3
> sections), and the bridge wiring + convenience CLIs that activate it are
> live (see "Wiring + status"). The only outstanding item is an
> end-to-end live sweep run in a real persona session for final
> acceptance.

This is the vendor-neutral contract an **interactive persona session**
executes to keep the whole team's tiger-memory fresh on the
**subscription rail** — extraction runs in an isolated **sub-agent**
(Task tool), never a programmatic `claude -p`, so it bills to the
subscription regardless of which conversation triggered it (B8). It is
the memory analogue of the journal's `OPERATING.md`: non-AI bookkeeping in
Python (the `tiger_memory.sweep` module + the `tiger-memory` CLI), AI in
the session's own sub-agents.

The memory model is the **three bounded stores** (`skills` /
`must_remember` / `topics`) — see [`DESIGN-memory.md`](DESIGN-memory.md)
and [ADR 0007](adr/0007-topic-store-revamp.md). A sweep turns each stale
persona's finished transcripts into entries in those stores; post-ingest
**staged compaction** (`compact-plan` → card sub-agents → `compact-apply`)
compacts any surface that has gone over its must-compact bound.

## When it runs

At **persona-session bootstrap** — any Shohoku persona conversation start
(drive-journal is one caller; the slack-bridge persona session is
another). Any human contact with any teammate becomes the heartbeat that
keeps the *whole roster* fresh (B3). It is a cheap no-op inside the
staleness floor, so triggering on every session start is safe.

## The procedure

All gating is `tigerharness.tiger_memory.sweep` (non-AI). The per-persona
extraction work is `tiger-memory plan` (stages prompts + packs **stacks**),
the extraction sub-agents (which only write bundle **cards**), and
`tiger-memory ingest-staged` (the single-process, race-free **glue** that
merges every `.extract.md` card into the three stores). `ingest-extraction`
remains for the one-bundle-over-stdin path (a single uuid). Post-ingest
compaction is the same staged shape: `tiger-memory compact-plan` (non-AI
staging), one card sub-agent per target, `tiger-memory compact-apply`
(non-AI, deterministically convergent).

1. **Claim the team sweep.** Compute `team_memories_dir =
   cfg.store.root.parent` for any persona on the team (= `<team>/memories/`).
   Call `sweep.maybe_sweep_roster(team_memories_dir, now=<utcnow>,
   token=<this-session-id>, max_personas=<per-wake cap>)`.
   - `ran=False` (`not_due` / `busy`) → **stop**: another session swept
     recently, or owns the sweep right now. No work.
   - `ran=True` → you hold the claim; `decision.plan.targets` is the list
     of `PersonaTarget(name, config_path)` to process this wake.

2. **Per target persona: stage → extract in stacks → glue.** For each
   target:
   a. `tiger-memory --config <target.config_path> plan [--max-sessions N]`
      → stages one prompt per flagged transcript under
      `<store>/.sweep-staging/<uuid>.prompt.md` and prints a manifest with
      two keys: `items` (per-uuid metadata: `conversation_uuid`,
      `prompt_path`, …) and **`stacks`** — a list of uuid-lists, **one
      stack per extraction sub-agent**. The packer (`lifecycle._pack_stacks`)
      groups transcripts in plan order until a stack would exceed
      `sweep_stack_content_chars` of summed (clipped) content or reach
      `sweep_stack_max_items`; a transcript heavier than the char budget
      becomes its own solo stack. This bounds how much any single sub-agent
      ingests, so a backlog fans out across many fresh contexts instead of
      one agent looping over — and re-reading — every transcript (the
      worse-than-linear cost the stacking fixes).
   b. For each **stack**, spawn **one constrained sub-agent** (Task tool)
      — the B7 trust boundary. Stacks are independent, so run the
      sub-agents in parallel (a sane concurrency cap). Each sub-agent:
      - **Reads**: each `<uuid>.prompt.md` in its stack (the prompt embeds
        the prefiltered transcript — staged in full up to the
        `max_staged_content_chars` ceiling, clipped only beyond it — so the
        bulky content never enters *your* context) **and** the persona's
        three stores (`skills.md` / `must_remember.md` / `topics.md`). The
        prompt also embeds the persona's **existing-topic routing list**
        (slug + summary, freshest first), so the sub-agent can route new
        knowledge into existing topics, **and** the persona's **current
        must-remember items** (id + kind + memo), so the sub-agent can
        TOUCH the ones the session relied on.
      - **Writes**: a bundle **card**
        `<store>/.sweep-staging/<uuid>.extract.md` per uuid — and nothing
        else. It does **not** ingest and runs no `tiger-memory` command.
      - **Instruction**: for each uuid in the stack, act **as that persona,
        in character**; read the prompt; emit ONLY the
        `@@SKILLS@@/@@MUST_REMEMBER@@/@@TOPICS@@` bundle per the prompt's
        output contract; **self-validate** (all three markers present, each
        on its own line, in that order; each section is well-formed blocks
        or the literal `NONE`); write it to the card; then **return only a
        short confirmation** (e.g. "carded N uuids"). The bulky bundle must
        never be returned to you (B8 fresh-window) — it lives only in the
        card. A `NONE`/`NONE`/`NONE` card is a valid, expected outcome.
      - **`@@MUST_REMEMBER@@` freshness touches**: besides new
        `KIND:`/`MEMO:` blocks, the section may include `TOUCH: <id>`
        blocks (zero or more, mixed freely) — one per existing item from
        the embedded list that this session *relied on* (followed it, was
        constrained by it, the subject came up again). The sub-agent
        should touch those items rather than re-emit them as new memos:
        ingest refreshes a touched item's freshness (`last_used` +
        `repeat_count`), and an item nobody touches for
        `must_remember.forget_days` becomes forget-eligible at
        compaction. Unknown ids are ignored at ingest.
      - **The `@@TOPICS@@` grammar**: one block per topic touched —
        `TOPIC:` (an existing slug from the embedded routing list, or
        exactly `NEW`), `NAME:` (required for `NEW`), `SUMMARY:` (required
        for `NEW`; for an existing topic only when the old summary no
        longer fits), `DETAIL:` (the new durable facts — always required).
        Route to an existing topic whenever one fits; a malformed block is
        dropped at ingest, never the whole bundle.
   c. **Glue** once all of this persona's stacks have carded: `tiger-memory
      --config <target.config_path> ingest-staged`. It reads every
      `<uuid>.extract.md` card and merges each bundle's blocks into the
      persona's three bounded stores in a **single process**, so the
      per-persona store writes are **serialized by construction** — no
      agent-side coordination, no lost-update race. It prints
      `{"ingested": N, "malformed": [...], "skipped_no_card": M}` and exits
      `0` (clean), `1` (≥1 malformed card — re-extract just those uuids, or
      leave them to re-stage next wake), or `2` (no manifest — a planning
      bug, surface it). A no-card item simply was not extracted this wake;
      the **store**, not the card, is the durable ledger (a re-`plan` wipes
      the staging dir), so it re-stages next wake.
   **Oversized transcripts — the map→reduce path (ADR 0006 Part 1).** Most
   items are `kind="single"` (one `<uuid>.prompt.md`, the flow above). An item
   whose prefiltered content exceeds `max_staged_content_chars` is staged as
   `kind="map_reduce"` instead — it carries `chunk_prompts` + `digest_paths`
   (and **no** `prompt_path`), so the oversized middle is never silently
   clipped. For each such uuid, between 2b and 2c:
   - **(map)** the extraction sub-agent reads each `<uuid>.chunkNN.prompt.md`
     (a `chunk_condense` prompt) and writes the matching
     `<uuid>.chunkNN.digest.md` — **plain neutral prose, NOT** the
     `@@SKILLS@@/@@MUST_REMEMBER@@/@@TOPICS@@` contract (that stays
     single-sourced in the reduce).
   - **(reduce, non-AI glue)** once a uuid's digests are all written, run
     `tiger-memory --config <target.config_path> build-reduce-prompts`. It
     concatenates the digests (bounded last-resort clip only if they *still*
     overflow), fills the single-sourced `extract_memory.md` contract over
     them, and writes `<uuid>.prompt.md` — the **same** filename + shape a
     single item has. It prints `{"built": [...], "pending": [...]}`; a
     `pending` uuid is simply not fully mapped yet (retried next pass).
   - From there the uuid is indistinguishable from a single item: a sub-agent
     turns `<uuid>.prompt.md` into the `<uuid>.extract.md` card, and 2c
     ingests it. `ingest-staged` drops the chunk prompts + digests with the
     card.

   d. **Compact (staged, ADR 0007)**: after the glue, run `tiger-memory
      --config <target.config_path> compact-plan`. It is non-AI: it first
      drops topics stale beyond `forget_days` deterministically, then
      stages one prompt per surface still over its must-compact bound
      under `<store>/.compact-staging/` and prints the manifest. If the
      manifest's `targets` list is **empty**, skip straight to 2e —
      nothing needs compacting (the common case). Otherwise:
      - Spawn **one Task sub-agent per staged prompt**. Each sub-agent
        reads its `<key>.prompt.md` (the prompt embeds the surface's
        current content and a strict marker contract —
        `@@MUST_REMEMBER@@`, `@@SKILLS@@`, `@@TOPIC_ROSTER@@`, or
        `@@TOPIC_DETAIL@@`), writes the compacted replacement to
        `<key>.card.md` next to it, and returns only a short
        confirmation — the card content never enters your context.
      - Run `tiger-memory --config <target.config_path> compact-apply`.
        Non-AI: it validates each card, applies it atomically, and
        guarantees convergence deterministically (an oversized card is
        hard-trimmed by keep-rank/freshness; protected content —
        operator-explicit directives, fresh topics — is never
        force-dropped and lands in `still_over` instead). Exit `1` means
        ≥1 malformed card (kept in place; re-card just those or leave
        them for the next sweep); exit `2` means no manifest (a
        sequencing bug — surface it).
   e. **Finalize**: `tiger-memory --config <target.config_path> rebuild` —
      `ingest-staged` already wrote the entries, so `rebuild` does not
      re-extract; it runs the fresh-start finalize tail (drops the retired
      legacy surface on first run, runs the `check --fix` format gate,
      then regenerates the briefing — must_remember + skill index + topic
      index + detail files + unprocessed notice). Then
      `sweep.record_persona_done(team_memories_dir, target.name)`.

3. **Close the run.**
   - All due personas processed → `sweep.mark_sweep_complete(
     team_memories_dir, now=<utcnow>)` (advances the watermark, clears the
     claim + progress). **This includes the empty case:** if
     `decision.plan.targets` is empty and `plan.remaining == 0` (idle team,
     or every persona already done), call `mark_sweep_complete`
     immediately — do NOT leave the claim dangling, or other sessions see
     `busy` until the lease expires.
   - Per-wake cap hit, more remain (`decision.plan.remaining > 0`) →
     `sweep.release_sweep_claim(team_memories_dir)` (clears the claim,
     **keeps** progress + the stale watermark) so the next wake resumes
     the rest.

## Guardrails (all already enforced by the module)

- **Staleness floor** (default 24h) + **team watermark** → most triggers
  are a no-op.
- **Soft-lease claim** → one session sweeps per window; a crashed owner's
  stale claim is stolen after the lease.
- **Per-wake cap** → a big backlog spreads across several wakes.
- **Per-persona resumable progress** → an interrupted sweep resumes where
  it stopped. The **store** (not the staged cards) is the ledger: an item
  extracted-but-not-yet-glued has no stored entries, so a re-`plan`
  re-stages it next wake.
- **Serialization is structural** → the extraction sub-agents only write
  cards; `ingest-staged` performs the single-process merge, so per-persona
  ingest ordering is never hand-coordinated.
- **Stacks bound sub-agent context** → `plan` packs transcripts into
  stacks (`sweep_stack_content_chars` / `sweep_stack_max_items`) so a
  backlog runs as many fresh contexts, not one ballooning one.
- **Subscription-safe** → the executor is always the sub-agent; the
  trigger context's billing is irrelevant.

## Worklog-only personas (journal memory)

Most journal-driven work now lands in **per-persona worklog records**
(see [`tiger-memory.md`](tiger-memory.md), "Per-persona journal memory",
and [`per-persona-journal-memory.md`](per-persona-journal-memory.md)). A
specialist like Rukawa may do all its work inside drives and have **no**
direct Slack threads of its own — only worklog entries. That persona is
still swept correctly, by design:

- The roster walk (`enumerate_persona_configs` → `plan_team_sweep`) is
  **not** activity-gated. It selects **every** roster persona that has a
  `tiger-memory.config.yaml` store (capped per wake), regardless of
  whether that persona has new activity. "No persona left behind" (B3).
- The "is there new work?" decision happens **later**, inside
  `tiger-memory plan`: its source adapters discover new records and the
  idle/clean decision skips still-active sessions. A persona with nothing
  new yields an empty manifest — a cheap no-op.
- So the only requirements for a worklog-only persona to be remembered
  are (a) it has a store + config on the roster, and (b) its config
  lists a `journal_worklog` source pointing at the team journal. With
  both, its drive worklog surfaces at `plan` time like any other source.

No special "count worklog files as activity" logic is needed at the
gating layer — the roster-wide enumeration already covers it.

## Wiring + status

The Python + CLI stack above is **complete and 100%-tested**, and the
bridge wiring + convenience CLIs that activate it have **shipped**:

1. **Trigger — done (config flag).** `TeamBridgeContext.
   tiger_memory_trigger` (`bridge.py:113`) selects the mechanism. Default
   `"rebuild"` keeps the legacy `tiger-memory rebuild --background`
   (`claude -p`, API-billed) so existing deploys are unchanged; setting it
   to `"off"` suppresses the daemon trigger (`bridge.py:475`) so the
   in-session sweep protocol — driven by the persona's `sweep-memory`
   skill over the gating CLIs below — owns the rebuild on the
   subscription rail. The two coexist behind the flag exactly as planned.
2. **Convenience CLIs — done (shipped).** `tiger-memory sweep-plan`
   (wraps `maybe_sweep_roster` for the team's memories root —
   `cfg.store.root.parent` — and prints the decision + targets),
   `sweep-done`, `sweep-complete`, and `sweep-release` are in
   `tiger_memory.cli`, so the interactive session drives gating without
   inline Python.
3. **Live verification — outstanding.** Run an end-to-end sweep in a real
   persona session — confirm the sub-agent (a) bills to the subscription,
   (b) writes the store directly, (c) returns only a short confirmation,
   and (d) its own transcript is excluded next sweep (B7). This is the
   acceptance for "subscription-safe rebuild is LIVE". (The related
   per-persona journal-memory transport — the bridge stamping
   `TIGERHARNESS_SLACK_THREAD_TS` into each turn's subprocess env so the
   driver's fat transcript is suppressed — was deployed and live-verified
   on 2026-06-08; see `per-persona-journal-memory.md`.)

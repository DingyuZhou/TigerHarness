# tiger-memory team sweep — in-session sub-agent protocol (B1/B3)

> **Status:** wired, shipped & live-verified. The full Python + CLI stack
> this protocol drives is shipped and tested (see
> `history/tiger-memory-rework.md`, B1/B3 sections), the bridge wiring +
> convenience CLIs that activate it are live (see "Wiring + status"), and
> the protocol was verified live on Shohoku on 2026-07-23 (topic-store
> migration + first staged compaction) and again via an end-to-end
> sandbox walkthrough on 2026-07-25.

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

At every sweep trigger: the **first Slack message of a new thread** (the
bridge's first-turn injection), **persona-session bootstrap**, a drive's
**idle-maintenance tail**, the **autodrive idle path**, or an explicit
"sweep memory" ask. Any human contact with any teammate becomes the
heartbeat that keeps the *whole roster* fresh (B3). Gating is the
**split gate**: the calling session's own persona sweeps whenever it has
un-swept source content that staging would extract — a completed
(idle-past-the-quiet-window) transcript with content past the ingest
cursor, or a still-active one whose post-cursor slice already exceeds
the active-slice threshold (ADR 0006 Part 2) — with no staleness floor;
its live session never counts itself. Every other persona keeps the
team floor + watermark — so most triggers are still a cheap no-op, and
firing on every session start is safe.

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
   token=<this-session-id>, max_personas=<per-wake cap>,
   own_persona=<calling persona or None>, own_pending=<bool>)` — the CLI
   form is `tiger-memory sweep-plan --own-persona <name>
   --exclude-session <uuid>`, which computes `own_pending` via
   `lifecycle.has_pending_source` before claiming. The pending test is
   kept in exact **lockstep with staging**: a record is pending iff it
   is idle per `rebuild.idle_threshold_hours` with content past the
   ingest cursor, OR still active but with a post-cursor slice over
   `budgets.active_slice_threshold_chars` (the same
   `_compute_incremental_slice` gate staging applies, ADR 0006
   Part 2) — so a claimed own-only run always stages at least one
   slice. The live session is excluded either way.
   - `ran=False` (`not_due` / `busy`) → **stop**: nothing pending and the
     team is inside the floor, or another session owns the sweep right
     now. No work.
   - `ran=True` → you hold the claim; `decision.plan.targets` is the list
     of `PersonaTarget(name, config_path)` to process this wake, and
     `decision.scope` records the claim's scope: `"team"` (floor due —
     own persona, if pending, is first and floor-exempt; the cap applies
     to the others) or `"own-only"` (targets is exactly the own persona;
     `sweep-complete` will NOT advance the team watermark for this run).

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
      - **Reads**: ONLY its staged prompt files — each `<uuid>.prompt.md`
        in its stack, and nothing else. The prompt already embeds
        everything the sub-agent needs: the prefiltered transcript —
        staged in full up to the `max_staged_content_chars` ceiling;
        beyond the ceiling the item becomes `kind="map_reduce"` (lossless
        chunking, see below), never clipped — plus the persona's
        **existing-topic routing list** (slug + summary, freshest first),
        so the sub-agent can route new knowledge into existing topics,
        and the persona's **current must-remember items** (id + kind +
        memo), so the sub-agent can TOUCH the ones the session relied on.
        The sub-agent does not read the persona's store files.
      - **Writes**: a bundle **card**
        `<store>/.sweep-staging/<uuid>.extract.md` per uuid — and nothing
        else. It does **not** ingest and runs no `tiger-memory` command.
      - **Instruction**: for each uuid in the stack, act **as that persona,
        in character**; read the prompt; emit ONLY the
        `@@SKILLS@@/@@MUST_REMEMBER@@/@@TOPICS@@/@@TEAM_EVENTS@@` bundle
        (contract v3, ADR 0008) per the prompt's
        output contract; **self-validate** (all four markers present, each
        on its own line, in that order, and **each marker exactly once** —
        a bundle with a duplicated standalone marker is MALFORMED at
        ingest, the contract-echo protection, so never echo the prompt's
        contract sample into the card; each section is well-formed blocks
        or the literal `NONE`); write it to the card; then **return only a
        short confirmation** (e.g. "carded N uuids"). The bulky bundle must
        never be returned to you (B8 fresh-window) — it lives only in the
        card. A `NONE`/`NONE`/`NONE`/`NONE` card is a valid, expected
        outcome.
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
      - **The `@@TEAM_EVENTS@@` grammar (ADR 0008)**: 0–3 `EVENT:` lines —
        what the persona actually DID this session (verb-first, past
        tense, ≤ ~15 words, no persona name; ingest prefixes the name and
        dates the line by the session's end day in the team-wide event
        log at `<team>/memories/team/events.md`). Real work only (shipped
        / reviewed / QA'd / decided / migrated), or `NONE`. Lines need no
        blank line between them (parsed line-wise).
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
      the staging dir), so it re-stages next wake. A `locked` uuid hit a
      stuck store lock — also re-stages. **Anomaly:** after a completed
      fan-out, `ingested: 0` with `skipped_no_card > 0` means
      misnamed/missing cards, not a quiet team (the CLI warns on
      stderr) — check the staging dir before moving on.
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

   **Interactive-trigger ordering — freshness first, compaction
   deferred.** When the sweep runs inline in a live persona session (the
   Slack-bootstrap or session-bootstrap trigger, where a real request is
   waiting), the freshness-critical half of the pipeline is short
   (stage → extract → glue → rebuild: a typical own-persona delta is one
   stack), while staged compaction is routinely the long pole (several
   card sub-agents). Run them in that order per target: 2a→2c, then 2e's
   `rebuild` + `record_persona_done` immediately — the briefing is fresh
   *before* the session turns to its real request, so the persona never
   answers from a stale briefing about work it just finished. Then run
   2d's `compact-plan`; if it stages targets, dispatch the card
   sub-agents **in the background** and proceed with the real request.
   When the cards land: `compact-apply`, a second `rebuild` (pure
   Python — regenerates the briefing over the compacted stores), then
   close the run (step 3). The claim is held for the whole tail **on
   purpose**: closing before `compact-apply` would let the next
   trigger's ingest race the pending cards. The lease survives the
   deferral (`record_persona_done` renews it), and a driver that dies
   mid-tail degrades gracefully — the stale claim is stolen after the
   lease, and any surface still over its bound simply re-stages at the
   next sweep's `compact-plan` (the store, as always, is the ledger).

3. **Close the run.**
   - All due personas processed (`remaining == 0`) → **first fold the
     team event log** (ADR 0008, team-level, once per completed sweep,
     while still holding the claim): run `tiger-memory --config <driver
     config> team-events-compact-plan`. `targets: []` (the common case)
     → move on. Otherwise spawn one Task sub-agent per staged prompt
     (read `prompt_path`, write the folded bullets to exactly
     `card_path` per the prompt's strict `@@TEAM_EVENTS@@` contract,
     return a one-line confirmation), then `tiger-memory --config
     <driver config> team-events-compact-apply` (exit `1` = a malformed
     card — leave it, the fold re-stages next sweep; `2` = no manifest,
     a sequencing bug). A capped wake skips this step entirely — the
     wake that finishes the roster does the fold.
   - Then `sweep.mark_sweep_complete(
     team_memories_dir, now=<utcnow>)` (advances the watermark, clears the
     claim + progress). **This includes the empty case:** if
     `decision.plan.targets` is empty and `plan.remaining == 0` (idle team,
     or every persona already done), still run the team-events fold above,
     then call `mark_sweep_complete`
     immediately — do NOT leave the claim dangling, or other sessions see
     `busy` until the lease expires.
   - Per-wake cap hit, more remain (`decision.plan.remaining > 0`) →
     `sweep.release_sweep_claim(team_memories_dir, token=<yours>)` (clears
     the claim,
     **keeps** progress + the stale watermark) so the next wake resumes
     the rest. The roster walk selects personas **least-recently-swept
     first** (the durable `done_at` map, stamped by
     `record_persona_done`), so capped wakes rotate through the whole
     roster — the old fixed-order walk re-swept the same head and starved
     the tail whenever runs kept resetting. A session with time to spare
     may immediately re-claim its own token and continue draining.

   Both closers accept the claim token and are REFUSED on a mismatch (a
   stale driver whose lease was stolen must not clobber the live owner's
   run — `mark_sweep_complete(..., token=)` / `release_sweep_claim(...,
   token=)` return ``False``; the CLI exits 3). `record_persona_done`
   RENEWS the claim lease, so a healthy long multi-persona run is never
   stolen mid-flight; only a genuinely silent driver loses the claim.

## Guardrails (all already enforced by the module)

- **Split gate**: the team **staleness floor** (default 24h) + **team
  watermark** make most triggers a no-op; the own-persona bypass fires
  only on real pending work (live session excluded, pending test in
  exact lockstep with staging — idle past the cursor, or active over
  the slice threshold — so a claimed own-only run always stages at
  least one slice), and an own-only run completes without advancing
  the team watermark (claim-scope marker; a scope-less claim from
  pre-change code completes as `"team"`).
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
- **Context-safe** → the executor is always the sub-agent; bulky
  transcripts and bundles never enter the trigger context.

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
   tiger_memory_trigger` (`bridge.py:115`) selects the mechanism. Default
   `"rebuild"` fires `_trigger_tiger_memory_rebuild` (`bridge.py:587`,
   dispatched at `bridge.py:565`): a detached, plain `tiger-memory
   rebuild` — since the topic-store revamp (ADR 0007) that verb is pure
   Python (format gate + briefing regenerate), **no model call, no
   flags**, so the trigger is cheap and billing-neutral. Setting the flag
   to `"off"` suppresses the daemon trigger entirely, so the in-session
   sweep protocol — driven by the persona's `sweep-memory` skill over the
   gating CLIs below — owns the whole refresh on the subscription rail.
   Either way, extraction never runs from the daemon.
2. **Convenience CLIs — done (shipped).** `tiger-memory sweep-plan`
   (wraps `maybe_sweep_roster` for the team's memories root —
   `cfg.store.root.parent` — and prints the decision + targets),
   `sweep-done`, `sweep-complete`, and `sweep-release` are in
   `tiger_memory.cli`, so the interactive session drives gating without
   inline Python.
3. **Live verification — done.** Verified live on Shohoku on 2026-07-23
   (the topic-store migration + the roster's first staged compaction ran
   through this protocol in a real persona session) and re-verified via
   an end-to-end sandbox walkthrough on 2026-07-25. (The related
   per-persona journal-memory transport — the bridge stamping
   `TIGERHARNESS_SLACK_THREAD_TS` into each turn's subprocess env so the
   driver's fat transcript is suppressed — was deployed and live-verified
   on 2026-06-08; see `per-persona-journal-memory.md`.)

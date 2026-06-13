# tiger-memory team sweep — in-session sub-agent protocol (B1/B3)

> **Status:** wired & shipped. The full Python + CLI stack this protocol
> drives is shipped and tested (see `history/tiger-memory-rework.md`, B1/B3
> sections), and the bridge wiring + convenience CLIs that activate it are
> live (see "Wiring + status"). The only outstanding item is an
> end-to-end live sweep run in a real persona session for final
> acceptance.

This is the vendor-neutral contract an **interactive persona session**
executes to keep the whole team's tiger-memory fresh on the
**subscription rail** — summarization runs in an isolated **sub-agent**
(Task tool), never a programmatic `claude -p`, so it bills to the
subscription regardless of which conversation triggered it (B8). It is
the memory analogue of the journal's `OPERATING.md`: non-AI bookkeeping in
Python (the `tiger_memory.sweep` module + the `tiger-memory` CLI), AI in
the session's own sub-agents.

## When it runs

At **persona-session bootstrap** — any Shohoku persona conversation start
(drive-journal is one caller; the slack-bridge persona session is
another). Any human contact with any teammate becomes the heartbeat that
keeps the *whole roster* fresh (B3). It is a cheap no-op inside the
staleness floor, so triggering on every session start is safe.

## The procedure

All gating is `tigerharness.tiger_memory.sweep` (non-AI). The per-persona
summarize work is the two CLIs `tiger-memory plan` + `ingest-summary`.

1. **Claim the team sweep.** Compute `team_memories_dir =
   cfg.store.root.parent` for any persona on the team (= `<team>/memories/`).
   Call `sweep.maybe_sweep_roster(team_memories_dir, now=<utcnow>,
   token=<this-session-id>, max_personas=<per-wake cap>)`.
   - `ran=False` (`not_due` / `busy`) → **stop**: another session swept
     recently, or owns the sweep right now. No work.
   - `ran=True` → you hold the claim; `decision.plan.targets` is the list
     of `PersonaTarget(name, config_path)` to process this wake.

2. **Per target persona, summarize via a sub-agent.** For each target:
   a. `tiger-memory --config <target.config_path> plan
      [--max-sessions N]` → stages one prompt per flagged transcript under
      `<store>/.sweep-staging/<uuid>.prompt.md` and prints the manifest
      `{"items":[{conversation_uuid, prompt_path, ...}]}`.
   b. For each manifest item, spawn **one constrained sub-agent** (Task
      tool) — the B7 trust boundary:
      - **Read**: the item's `prompt_path` (it already embeds the
        prefiltered transcript — staged in full up to the
        `max_staged_content_chars` ceiling, clipped only beyond it — so
        the bulky content never enters *your* context) **and** the
        persona's store.
      - **Write**: that persona's store path **only**. **No shell, no
        network.**
      - **Instruction**: read the prompt file; emit ONLY the
        `@@SHORT@@/@@DETAILED@@/@@MUST_MEMORIZE@@` bundle per the prompt's
        output contract; **self-validate** (all three markers present, in
        order; short + detailed non-empty); then write it back by piping
        the bundle to `tiger-memory --config <target.config_path>
        ingest-summary --uuid <conversation_uuid>` and **return only a
        short confirmation** (e.g. "ingested <uuid>"). The bulky bundle
        must never be returned to you (B8 fresh-window).
      - `ingest-summary` exit codes: `0` ok; `1` malformed bundle
        (re-ask the sub-agent once, then skip); `2` operator error
        (unknown uuid / no manifest — a planning bug, surface it).
      - **Serialize ingest per persona.** `ingest-summary` does a
        read-modify-write merge into the persona's single
        `must_memorize.md`, so for ONE persona the sub-agents' bundles
        must be ingested **one at a time** (the sub-agents may *run* in
        parallel, but pipe their bundles to `ingest-summary`
        sequentially), or two concurrent merges will lose updates.
        Different personas are independent (separate stores) and may
        ingest in parallel. (A future hardening could defer the merge to
        the finalize step via per-uuid pending files.)
   c. After the persona's items are all ingested, `tiger-memory --config
      <target.config_path> rebuild` is **not** needed for summaries (the
      sub-agents wrote them); run the **finalize** tail — rollups, decay,
      briefing — by calling `lifecycle.finalize_rebuild` (or, until a
      `tiger-memory finalize` CLI exists, a plain `rebuild` with the
      summaries already present is a clean no-op on the summarize step and
      does the rollups/briefing). Then `sweep.record_persona_done(
      team_memories_dir, target.name)`.

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
  it stopped.
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
  rebuild state dedupes already-summarized ones. A persona with nothing
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

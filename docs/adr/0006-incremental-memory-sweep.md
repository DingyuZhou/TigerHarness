# ADR 0006: incremental memory sweep — restore lossless reduce + per-session high-water mark

Status: **accepted (decision), pending implementation.** Date: 2026-06-28.
Decided with the Operator in a Slack thread. Addresses a silent regression
introduced by the b1 memory rewrite (commit `f68655b`); see Context.

## Context

The bounded-store revamp ([DESIGN-memory.md](../DESIGN-memory.md), shipped
2026-06-17, commit `f68655b`) turns a finished session into memory by staging
the transcript for a subscription sub-agent: `lifecycle.plan_extraction`
pre-filters the transcript, clips it to `max_staged_content_chars` (300K) via
`_clip`, and writes one prompt; the sub-agent reduces and emits the 3-store
bundle.

Three problems:

1. **Lossy clip → silent memory loss.** `_clip`
   (`lifecycle.py:720`) keeps the head and tail and elides the *middle*. A
   single transcript over ~300K chars of pre-filtered prose loses its middle
   before the sub-agent ever sees it.

2. **The lossless reducer was retired by accident.** PR #42 (commits
   `8621e1b`, `f61763a`) had replaced lossy clipping with chunk-and-reduce
   (`_fit_content`: split on line boundaries → condense each chunk → recurse,
   depth-capped, lossless) on the in-process summarizer path. The b1 rewrite
   (`f68655b`) dropped that machinery along with the retired rollup/archive
   surface — unintentionally, per Operator. Today there is no `chunk`/`reduce`
   left in `lifecycle.py`; the only live path is the lossy clip. (Note: even
   PR #42 left the *sub-agent* staging path as clip-with-instruction — genuine
   chunk-and-reduce ran only in-process. With the in-process path now gone, we
   need genuine chunk-and-reduce **on the sub-agent path**.)

3. **Whole-session re-summarization is wasteful and slow to fire.** There is
   no per-session cursor: every sweep re-summarizes the entire transcript from
   the start, and the idle gate (`idle_threshold_hours`, default 1h) means a
   session touched daily is never summarized while active — it can balloon
   past 300K and then lose its middle on the eventual single pass. This is the
   exact scenario the Operator raised (a session held open and used for many
   days).

## Decision

Two complementary changes, implemented in this order.

### Part 1 — Restore the lossless reduce (re-fitted to the sub-agent model)

The old `_fit_content` ran on the in-process (API) summarizer; that path is
gone, and subscription billing requires the reduce to run in the sub-agent.
So restore the *idea* on the staging path:

- When a transcript exceeds the staging ceiling, `plan_extraction` splits it
  on event/line boundaries (lossless — never mid-line) into N chunk-prompts
  under `.sweep-staging/<uuid>.chunkNN.prompt.md`, instead of lossy-clipping.
- The sub-agent **condenses each chunk** to a neutral digest
  (`<uuid>.chunkNN.digest.md`) — a map step driven by a restored
  `chunk_condense` prompt (recoverable from PR #42).
- A **reduce step** runs the normal extraction prompt over the concatenated
  digests to emit the final 3-store card (`<uuid>.extract.md`). The extraction
  contract (`@@SKILLS@@` / `@@MUST_REMEMBER@@` / `@@TOPICS@@` since
  ADR 0007) stays
  single-sourced — only the reduce emits it; the map digests are plain prose.
- Keep a hard depth cap + a bounded `_clip` as the last-resort termination
  guard (a pathological input can't loop forever), exactly as PR #42 did.

This plugs the leak directly and is a contained change.

### Part 2 — Per-session high-water mark (incremental sweep)

Persist a per-conversation cursor so each sweep processes only the new slice:

- **Cursor.** Per persona, a map keyed by `conversation_uuid` → the
  last-processed position. New slice = events after the cursor. (Q1.)
- **State location.** A dedicated `.sweep-cursors.json` in the persona store
  (sibling to `.state.json`), keyed by `conversation_uuid`. (Q2.)
- **Long-range context = the existing memory store.** The sub-agent already
  reads the persona's store; that distilled memory *is* the "story so far," so
  distant context is carried for free — no need to re-read old messages.
- **Short-range context = a small raw overlap window.** Include the last few
  events *before* the cursor (recommend 3–5 completed turns) as **read-only**
  context in the slice prompt; extract memory only from events *after* the
  cursor. Dedup + compaction fold any incidental overlap.
- **Advance the cursor** only after the slice's card is successfully ingested.

We **do not** add a periodic full re-sweep backstop (explicitly dropped per
Operator). The store-as-backdrop + overlap window + the Part 1 reducer
together cover both the context case and the size case.

### Interaction with the idle gate (the motivating scenario)

The Operator's case — one session held open and used daily — stays "active"
and is never summarized under today's idle gate (`_decide`,
`lifecycle.py:440`). To realize the high-water mark's value, allow
**incremental** extraction of a still-active session once its *unprocessed*
slice crosses a size threshold, cutting only at a completed user/assistant
turn boundary (never mid-turn). The final idle pass then mops up the tail.
(Q3.)

## Sequencing

Part 1 first — it plugs the silent leak with the least risk and the fewest
files touched. Part 2 second — it is the larger change, and once the cursor
keeps slices small, Part 1's reducer becomes a rarely-exercised safety net
rather than the main line of defense.

## Open questions (decide before/while building)

- **Q1 — cursor representation.** Last-processed event timestamp (ISO) vs.
  processed-event count vs. both. Recommend **both** — timestamp primary,
  count as a secondary guard — so the cursor stays stable even if the
  event-filter logic changes the index.
- **Q2 — state file.** New `.sweep-cursors.json` vs. a sub-key in
  `.state.json`. Recommend the **dedicated file** to keep `.state.json`'s
  schema stable and avoid write contention.
- **Q3 — active-session trigger.** The unprocessed-slice size that arms
  incremental extraction of a still-active session, and the safe cut boundary.
  Recommend a char threshold well under the 300K ceiling (e.g. ~100K) and
  cutting at the last completed turn.
- **Q4 — chunk/slice composition.** When an incremental *slice* is itself
  oversized, Part 1's chunk-and-reduce runs *within* that slice. Confirm the
  two compose (they should: a slice is just a smaller transcript).

## Consequences (what the implementation builds)

1. `plan_extraction`: split-on-boundary chunking + multi-prompt staging for
   oversized inputs; restore the `chunk_condense` prompt + the map/reduce
   sub-agent steps; keep the depth-cap + bounded `_clip` guard.
2. New per-persona cursor store (`.sweep-cursors.json`) + read/write helpers.
3. `_discover` / `_decide` / `plan_extraction`: compute the post-cursor slice
   + the read-only overlap window; advance the cursor after successful
   ingest; add the active-session incremental trigger (Q3).
4. Docs + skill: update the `sweep-memory` bundled skill,
   [tiger-memory-sweep-protocol.md](../tiger-memory-sweep-protocol.md), and
   [tiger-memory.md](../tiger-memory.md) for the map/reduce stage files and
   the cursor lifecycle.
5. Config knobs: chunk content budget, overlap-window size, active-slice
   threshold — each with a default. Whole-package 100% branch coverage held.

## Alternatives considered

- **Lossy clip status quo.** Rejected — silent middle loss is unacceptable
  (the whole reason for this ADR).
- **Raise the 300K ceiling only.** Doesn't fix it — sub-agent context is
  finite too; it just moves the cliff.
- **High-water mark without the reducer.** Rejected — a single huge increment
  (or the first-ever pass on an already-long session) still needs a lossless
  reduce.
- **Periodic full re-sweep backstop.** Considered and **dropped per Operator**
  — redundant given store-backdrop + overlap + reducer, and it re-introduces
  the cost the high-water mark removes.

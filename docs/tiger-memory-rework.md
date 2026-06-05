# tiger-memory rework — Phase-0 design

> **Status:** Draft / spec phase. No code yet. This is the decision
> scaffold we iterate on before implementation.
> **Date:** 2026-06-05.
> **Decision-makers:** Operator + Anzai.
> **Thread:** Slack DM 2026-06-05 (Operator ↔ Anzai).
>
> This document captures intent, not shipped behavior. Specific
> numbers cited from the current implementation (call counts, config
> defaults) are marked *(verify in P0)* where they came from a code
> survey rather than a line I quote directly — confirm them against
> `src/tigerharness/tiger_memory/` before building on them.

## Why this exists

Two goals, raised together because on a subscription they are the
same goal:

1. **Make tiger-memory work under the subscription-only model** — the
   file-based, human-driven execution backend
   ([`subscription-backend.md`](subscription-backend.md)) where AI
   work runs inside a human's interactive Claude Code session, not via
   programmatically-spawned `claude -p` children.
2. **Make tiger-memory leaner on tokens and context** — both the cost
   of *writing* memory (rebuild) and the cost of *reading* it (the
   briefing the agent loads each session).

These interlock. Under a subscription there are no per-token dollars —
there is a **usage quota** (the rolling 5-hour / weekly caps). Every
token saved writing memory and every token saved reading it both come
out of that one quota window. Leanness *is* subscription-fitness.

## The billing fact this whole design turns on

From [`subscription-backend.md`](subscription-backend.md) (Push vs
pull table) and
[`skills/drive-journal/SKILL.md`](../skills/drive-journal/SKILL.md):

- A **programmatically-spawned `claude -p` child** bills as **API
  tokens**. The task-runner, the workflow-runner, **and
  tiger-memory's `anthropic` summarizer** all do this today
  (`tiger_memory/summarizers/anthropic.py:97` → `claude_p` backend →
  `claude -p` subprocess). So today's rebuild is **API-billed**, not
  subscription.
- The **subscription** covers a **human's interactive session** doing
  the work *itself*. The drive-journal compile sub-protocol is the
  template: the session adopts a persona by *prompt-prepending* and
  produces the output in its own turns — never by spawning `claude
  -p`. As the skill puts it: *"The compile is in-session; the API
  budget is zero."* (`drive-journal/SKILL.md:115-118`).

**Consequence:** to make memory subscription-safe we cannot merely
re-auth the spawned summarizer. The summarization must move *into* the
interactive session — the agent summarizes transcripts as itself, in
its own turns, the same way compile works.

## Design principle: vendor-neutral at every AI touch-point

The memory system must not hard-bind to one AI vendor (Claude /
Anthropic). Treat the vendor as pluggable wherever AI — or
vendor-shaped state — appears:

- **Summarizer backend** — already pluggable via the `Summarizer`
  base + `register_summarizer` registry
  ([`tiger-memory.md`](tiger-memory.md), "Adding a new summarizer
  vendor"). Keep that contract; the rework must not reach around it.
- **In-session summarization** (B1) — express it as a *vendor-neutral
  protocol* the interactive driver executes, the way the journal's
  `OPERATING.md` is vendor-neutral. "Adopt the summarizer role and
  emit the markdown" is a generic instruction; any interactive agent
  that can read and write files can run it. No Claude-Code-specific
  mechanics in the contract.
- **Transcript sources** — reading a vendor's transcript is inherently
  vendor-specific; keep it behind the existing `sources/` adapter
  interface (`claude_code` is one adapter; others plug in).
- **Billing model** — state the *principle* generically: "AI work
  rides the human's interactive session; autonomous/programmatic
  spawns are the metered path." That maps onto any vendor's
  subscription-vs-API split, not only Anthropic's.

**Litmus test:** swapping the AI vendor should touch *adapters and
config* — never the lifecycle, store, briefing, roster, or identity
logic.

## The core reframe: bookkeeping vs summarization

A `tiger-memory rebuild` today does two very different kinds of work:

| Work | Nature | Cost | Where it should live |
|---|---|---|---|
| Source discovery, state tracking, rollup scheduling, decay scoring, briefing assembly | Plain Python, **no AI** | Free | Stays as-is — a non-AI sweep, exactly like `journal sweep` |
| Transcript → short / detailed / must-memorize summaries | **AI** | Quota | Moves **in-session** (agent summarizes itself) |

The drive-journal sweep is already *"plain non-AI Python bookkeeping
executed inside the driver's invocation — no separate process, cron
job, or systemd unit"* (`subscription-backend.md:140-143`). Memory's
bookkeeping half fits that mold unchanged. Only the summarization half
needs to move onto the in-session rail.

**One caveat on that "free" bookkeeping:** `must_memorize` **decay
must be wall-clock-anchored** — scored from each memo's own timestamp
against *now*, never per-rebuild-invocation. With bursty, irregular
sweeps (a persona rebuilt after a three-week gap; roster sweeps every
24h), per-invocation decay would distort priorities badly. *(P0:
verify decay is time-anchored in `must_memorize.py`.)*

---

## Goal A — leanness (Levers 1 & 2)

### Lever 1 — build-side cuts (make *writing* memory cheaper)

1. **Collapse the per-session calls.** Today the lifecycle issues
   ~3 summarize calls per new session — short summary, detailed
   archive, must-memorize extraction — *all reading the same
   transcript* *(verify exact count in `lifecycle.py` during P0)*.
   Collapse them into **one structured pass** that emits all three
   sections, or at minimum share a cached transcript prefix across
   them. Target: ~3× fewer calls per session.
2. **Pre-filter transcript noise** before summarizing — drop tool-call
   spam, large file dumps, redundant scaffolding — so each pass ingests
   far fewer than the current `max_prompt_content_chars` ceiling
   (120k chars ≈ 30k tokens). Smaller input = less quota, regardless
   of how the call is billed.
3. **Tier the model.** Cheap model for shorts; reserve the expensive
   model for rollups where compression quality matters most. (Once
   summarization is in-session, "model tier" becomes "how much of the
   interactive session's context we spend" — same lever, different
   currency.)
4. **Hard cost/scope cap per rebuild.** The code already wants this —
   see the TODO at `summarizers/anthropic.py:32-35` ("cost cap + scope
   cutoff + output validation"). A rebuild must never be able to run
   away; it processes at most *N* sessions per invocation and stops.

**Note on sequencing.** Once summarization moves in-session (B1), the
"3× same transcript" waste vanishes *for free* — the driver loads the
transcript into its context once and emits all three artifacts from
that single read. So on the in-session path Lever 1 reduces to the
parts that survive the move: **transcript pre-filter**, the
**cost/scope cap**, and **context-budget tiering**. The "collapse
calls / prefix-cache" sub-lever only matters for the legacy API-spawn
path we keep as an opt-in (see Non-goals).

### Lever 2 — read-side context diet (make *reading* memory cheaper)

The agent loads the walking-window briefing at the start of every
session; with `/compact` injected between iterations, that read
recurs. Levers:

1. **Shrink the always-on core.** Keep `must_memorize.md` (load-
   bearing) + a compact `MANIFEST` index always-resident; lazy-load
   deeper summaries via drill/search only when a turn actually needs
   them.
2. **Tighten budgets / walking window** where measurement shows slack
   (current defaults: `short_summary_words: 400`,
   `must_memorize_rows: 60`; walking window 2 / 7 / 28 / 90 working
   days for shorts / dailies / weeklies / monthlies — see
   [`tiger-memory.md`](tiger-memory.md)).

**Trade-off, stated honestly:** a lean core + on-demand drill trades
*resident context* for *extra retrieval calls*. Each drill is itself a
turn. We balance, not eliminate — and we measure before/after so we
know we actually won.

---

## Goal B — subscription-safe multi-persona rebuild (Lever 3)

This is the hard part, and the part with the multi-persona trap.

### B1 — Summarization moves in-session

Per the billing fact above: a memory-rebuild **skill** (human-
triggered, like drive-journal) runs the non-AI bookkeeping in Python,
then has the **interactive session summarize the flagged transcripts
itself** — adopt the summarizer role by prompt-prepending, emit the
markdown, write it to the store. No `claude -p` spawn. API budget zero.

Two properties matter here:

- **Vendor-neutral.** "Adopt the summarizer role, emit the markdown"
  is a protocol *any* interactive agent can run — not a Claude-specific
  mechanism (see the vendor-decoupling principle above).
- **It subsumes Lever 1's biggest waste.** In-session, the transcript
  is loaded into the driver's context *once* and all three artifacts
  (short / detailed / must-memorize) come out of that single read —
  the 3×-resend simply disappears. B1 and Lever 1 reinforce each other
  rather than compete.
- **Self-validating.** Because one emission now produces all three
  artifacts, the protocol must validate its own output — all sections
  present, within budget — *before* writing to the store. A malformed
  emission must fail loudly, not corrupt the store (the "output
  validation" half of the `anthropic.py:32-35` TODO).

Open: whether this is its own skill or folded into the drive-journal
wake-up sweep (see Open Questions).

### B2 — The multi-persona completeness problem

A team now has many personas, each with its own memory store. The
trap: if rebuild is tied to *whoever is currently talking*, an **idle
persona never rebuilds**. Example: you talk to Ayako three weeks ago,
then only to Anzai since — Ayako's three-week-old conversation never
gets summarized into Ayako's memory.

Two worries to separate:

- **Is the old conversation *lost*?** No — *provided rebuild always
  scans the full backlog since the last rebuild*, not just the latest
  session. The engine tracks what it has processed (`state.py`) and
  the source globs *all* of a persona's transcripts. The three-week-old
  session sits unprocessed until the next rebuild, then gets picked up.
  Completeness is not the risk; **timing** is.
- **Does the rebuild ever *get triggered*?** This is the real risk.

**The fix is a reframe:** the rebuild engine is *persona-agnostic* —
the only thing that makes it "Ayako's" is the config (which store,
which filter). So the trigger keys on the **team roster, not the
active speaker.**

### B3 — Roster sweep, piggybacked on the human session

A **team memory sweep** enumerates the team roster and rebuilds, in
turn, each persona **that has a memory store configured** — skipping
personas with no memory config. Two things must be pinned in P0: the
**canonical roster source** (`configs/personas.yaml` is the candidate
— *verify it is authoritative and not one of several competing roster
notions*) and the **persona → tiger-memory-config resolution** (which
store and which filter each persona maps to). With those, "no persona
left behind" is guaranteed *by construction* — it does not matter who
has been talking.

It piggybacks on a human-initiated interactive session, gated by:

- **Configurable trigger set.** Which session starts fire the sweep is
  a config knob. `drive_journal` (the interactive driver) always
  qualifies; `slack_bridge_dm` is **opt-in, default off**. Rationale:
  a slack-bridge DM spawns `claude -p` programmatically, which may be
  API-metered in a given deployment (see the billing fact). Default-off
  keeps the sweep strictly subscription-safe out of the box; an
  operator whose bridge is subscription-authed — or who accepts the
  API cost — flips it on.
- **Atomic sweep claim, not just a watermark.** Two interactive
  sessions can clear the staleness floor in the same instant. The sweep
  must *claim* the run atomically (a team-scoped lease, reusing the
  drive-journal heartbeat-as-soft-lease pattern) so only one session
  sweeps; the other sees a fresh claim and skips. A per-store lock
  alone is not enough — it serializes writes but still lets both
  sessions do the redundant work.
- a **team `last_sweep_at` watermark** + a staleness floor (e.g. once
  per 24h), stored in team-scoped state *(P0: pin the exact
  location)*, so the sweep does not re-run every invocation;
- a **per-wake work cap** so a large backlog spreads across several
  wakes instead of one giant turn;
- **path-contained writes** — the sweep writes only within each
  persona's own store dir;
- **resumable, not all-or-nothing** — a human can end the session (or
  `/compact` can fire) mid-sweep. Commit each persona's rebuild
  atomically and track **per-persona** progress, so an interrupted
  sweep resumes where it stopped rather than redoing or skipping work;
  the team watermark advances only for personas actually completed.

The waking persona refreshes **its own** store promptly (fresh for
*this* conversation); the roster catch-up for idle personas runs
opportunistically when the floor has elapsed.

**The invariant that falls out:** *any* human contact with *any*
teammate becomes the heartbeat that keeps the *whole* roster fresh. As
long as someone talks to someone occasionally, nobody goes stale. And
if the whole team goes silent for a month, no sweep runs — which is
**correct**: there is no new memory to build and no one there to need
it. The next wake catches everyone up.

### B4 — Team-qualified persona identity

Today's attribution tag in `threads.json` is a bare slug
(`persona: "ayako"`). Two teams that each have a "Michael" collide the
moment those records ever sit together. Make the identity
**self-describing**:

```json
"persona": { "team": "shohoku", "name": "ayako" }   // or flattened "shohoku/ayako"
```

The tiger-memory source filter matches on the **(team, name) pair**,
not the name alone. Single source of truth for `team` = the team's
`configs/personas.yaml` roster.

**Backward compat (two existing shapes to absorb).** Today's entries
are `{thread_ts: {session_id, persona}}` where `persona` is a bare
name; a pre-routing legacy entry is a bare `"session_id"` string with
no persona at all (see [`tiger-memory.md`](tiger-memory.md),
"Per-persona filtering"). The reader upgrades a bare `persona` **name**
to `{team: <from config>, name}`, and continues to treat a
no-persona / pre-routing entry as *unattributed* (excluded under strict
mode, included with `include_unattributed`). Existing `threads.json`
files keep working unchanged.

This is mostly defense-in-depth — store dirs and configs are already
team-scoped (they live in the team folder), and transcripts are keyed
by cwd-slug — but it makes records self-describing for debugging and
future-proofs any cross-team aggregation. Cheap insurance.

### B5 — The sharp edge: don't drop a persona tag before it's summarized

B2's completeness guarantee depends on Ayako's old sessions *still
being attributed to Ayako* when the sweep runs. That attribution lives
in `threads.json`. **If anything ever prunes old entries before they
are summarized, those sessions silently become "unattributed" →
excluded under strict mode → and *that* is how you would actually lose
the three-week-old chat.**

So the real safeguard is not the trigger — it is a **"summarized"
watermark**: never prune (or never strict-exclude) a persona
attribution until its session has been folded into memory. *(P0 task:
verify whether anything prunes `threads.json` today.)*

### B6 — Residual risk: catch-up burst after long silence

A wake that follows a long quiet stretch eats a big backlog in one
interactive session — quota spent on memory instead of the human's
actual task. Mitigations, layered:

- Lever 1 cuts make each session ~3× cheaper to summarize;
- the **per-wake cap** (B3) spreads a large backlog over several
  wakes;
- the **cost/scope cap** (Lever 1.4) is the hard backstop.

### B7 — In-session summarization is a new trust boundary

Moving summarization into the live driver session removes an isolation
property the spawned summarizer had — its own subprocess, its own cwd
(see the feedback-loop note at `summarizers/anthropic.py:23-36`). Two
hazards to design against:

- **Transcripts are untrusted input.** A past conversation may contain
  text that reads as an instruction. Pulled into the driver's live
  context, that is a prompt-injection surface. The summarization
  sub-turn must treat transcript content as *data to be summarized* —
  quoted, fenced, clearly delimited — never as instructions to the
  driver.
- **Don't let upkeep turns become a new source.** The spawned
  summarizer deliberately routed *its own* transcripts to a throwaway
  cwd-slug so the next rebuild would not re-ingest them
  (`anthropic.py:23-36`). In-session, the driver's own memory-upkeep
  turns live in the driver's transcript — which the next sweep reads.
  We must mark or exclude upkeep turns from source ingestion, or the
  self-referential feedback loop the old code warned about comes back.

### B8 — In-session summarization trades API dollars for context pressure

This is the sharpest tension in the design — and it is *between the two
goals themselves*. The spawned summarizer got a **fresh context window
per call**: it read one transcript and exited. In-session, the driver
summarizes *on top of* everything it is already holding (its briefing,
its current journal task). So moving summarization in-session is
**subscription-friendly but context-hostile** — it can crowd the
driver's window or degrade its real work, especially on large
transcripts (the 120k-char ceiling exists for a reason).

That makes two earlier levers **load-bearing, not optional**:

- **Transcript pre-filter (Lever 1.2)** becomes a *feasibility
  requirement* for B1, not a nice-to-have — it caps what enters the
  live context.
- **Per-wake cap (B3)** bounds how many transcripts hit the context per
  session.

If pre-filtered transcripts still overflow, the fallback is to
summarize in **bounded chunks** (or to spend one isolated sub-context
per transcript where the vendor supports it) — accepting more turns to
protect the driver's window. This is the live form of open question #4;
resolving the context budget is a **P2 design gate**, not an
afterthought.

---

## Open questions (need Operator input)

1. **Slack-bridge billing — RESOLVED into config (see B3).** The
   trigger set is a config knob; `slack_bridge_dm` defaults **off**, so
   the sweep is subscription-safe out of the box. The underlying
   billing fact (is *your* bridge subscription- or API-authed?) still
   informs whether you flip it on — but the design no longer needs the
   answer to proceed. Remaining input wanted: your default preference.
2. **Skill placement.** Memory sweep as its **own** human-triggered
   skill, or **folded into** the drive-journal wake-up sweep so users
   get it for free without remembering a separate command?
3. **Staleness floor & per-wake cap values.** Starting points to tune
   (e.g. 24h floor, cap of N sessions/wake).
4. **In-session summarization context budget.** Summarizing in-session
   loads transcripts into the live session's context — how much of a
   driver session are we willing to spend on memory upkeep? **See B8 —
   this is now a P2 design gate, not just an open question.**

## Phasing

- **P0 (this doc).** Lock the design. Measure the current footprint as
  a baseline (calls per rebuild, tokens per call, briefing size).
  Verify the *(verify in P0)* facts. Confirm `threads.json` pruning
  behavior (B5).
- **P1 — Lever 1 (build-side cuts).** Transcript pre-filter +
  cost/scope cap + context-budget tiering — the parts that survive the
  move to in-session. (Collapse/prefix-cache applies only to the legacy
  API-spawn path; it is subsumed by P2's in-session read-once.) Lowest
  risk, no change to what the agent reads.
- **P2 — Lever 3 (subscription-safe multi-persona).** In-session
  summarization skill (B1) + roster sweep (B3) + team-qualified IDs
  (B4) + the summarized-watermark safeguard (B5).
- **P3 — Lever 2 (read-side diet).** Lean core briefing + on-demand
  drill, measured before/after.

## Non-goals

- Removing the API-backed rebuild path. Like the API runners, it stays
  available as an opt-in for autonomous/parallel use; the two coexist.
- Changing the memory *hierarchy* (journal / archive / briefing) or
  the rollup cadence. This rework is about *how* and *when* rebuild
  runs and *how much* it costs — not the store's shape.
- Cross-persona memory sharing. Each persona still reads only its own
  store.

## Revision log

- **2026-06-05 — round 1 critique (Anzai).** Caught and folded in:
  (1) in-session summarization *subsumes* Lever 1's 3×-resend waste —
  connected the levers and re-scoped P1; (2) the roster sweep needs an
  *atomic claim*, not just a per-store lock, or two sessions both sweep
  (B3); (3) in-session summarization is a new **trust boundary** —
  transcript-as-untrusted-input and the upkeep-turn feedback loop (B7);
  (4) promoted **vendor decoupling** to an explicit design principle
  with a litmus test. Resolved open-question #1 into a configurable,
  default-off slack-bridge trigger.
- **2026-06-05 — round 2 critique (Anzai).** Caught and folded in:
  (1) the central tension — in-session summarization is
  **subscription-friendly but context-hostile**; makes the pre-filter
  and per-wake cap load-bearing, with chunking as fallback (B8);
  (2) `must_memorize` decay must be **wall-clock-anchored**, or bursty
  sweeps distort priorities (core-reframe caveat); (3) the sweep must
  be **resumable** — atomic per-persona commit, per-persona progress
  (B3); (4) the in-session emission must **self-validate** before
  writing to the store (B1); (5) made the **roster → config
  resolution** explicit and verify-gated (B3); (6) corrected the
  `threads.json` backward-compat to absorb both existing entry shapes
  (B4). Operator confirmed `slack_bridge_dm` opt-in, default off.

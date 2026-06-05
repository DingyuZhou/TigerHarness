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

A **team memory sweep** loops over every persona in the roster
(`configs/personas.yaml`) and rebuilds each store in turn. This is
where "no persona left behind" is guaranteed *by construction* — it
does not matter who has been talking.

It piggybacks on a human-initiated interactive session (the
drive-journal wake-up, or any interactive driver start), gated by:

- a **team `last_sweep_at` watermark** + a staleness floor (e.g. once
  per 24h) so it does not re-run every invocation;
- a **store lock** so concurrent sessions do not collide;
- a **per-wake work cap** so a large backlog spreads across several
  wakes instead of one giant turn.

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

**Backward compat:** a bare string / `name`-only entry is read as
`{team: <from config>, name: <persona>}`, so existing `threads.json`
files keep working.

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

---

## Open questions (need Operator input)

1. **Slack-bridge billing.** The drive-journal interactive session is
   unambiguously subscription. Is a **slack-bridge DM session**
   (which spawns `claude -p` programmatically) subscription-billed or
   API-billed in your deployment? This decides whether DM wake-ups are
   a valid subscription trigger for the sweep, or only the
   drive-journal interactive session is. **I will not bake an
   assumption here.**
2. **Skill placement.** Memory sweep as its **own** human-triggered
   skill, or **folded into** the drive-journal wake-up sweep so users
   get it for free without remembering a separate command?
3. **Staleness floor & per-wake cap values.** Starting points to tune
   (e.g. 24h floor, cap of N sessions/wake).
4. **In-session summarization context budget.** Summarizing in-session
   loads transcripts into the live session's context — how much of a
   driver session are we willing to spend on memory upkeep?

## Phasing

- **P0 (this doc).** Lock the design. Measure the current footprint as
  a baseline (calls per rebuild, tokens per call, briefing size).
  Verify the *(verify in P0)* facts. Confirm `threads.json` pruning
  behavior (B5).
- **P1 — Lever 1 (build-side cuts).** Collapse per-session calls +
  prefix cache + transcript pre-filter + cost/scope cap. Biggest quota
  win, lowest risk, no change to what the agent reads. Done first so
  later phases inherit a cheaper rebuild.
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

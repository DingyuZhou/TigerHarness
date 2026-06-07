# tiger-memory rework — Phase-0 design

> **Status:** Draft / spec phase. No code yet. This is the decision
> scaffold we iterate on before implementation.
> **Date:** 2026-06-05 (rounds 1-2) · 2026-06-06 (round 3).
> **Decision-makers:** Operator + Anzai.
> **Thread:** Slack DM 2026-06-05 → 2026-06-06 (Operator ↔ Anzai).
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
- **Isolation strategy** — *how* the summarization contract gets a
  bounded context to run in (so it does not crowd the driver) is a
  swappable per-vendor adapter, kept separate from the contract itself.
  `subagent` — a vendor sub-agent with its own context window — is the
  strategy we build (see B8); `fresh_session` / `inline_mapreduce` /
  `api_spawn` are other shapes a different vendor could register. The
  *contract* — produce {short, detailed, must-memorize}, validate,
  write to the store, return a short confirmation — does not change when
  the strategy does.
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
24h), per-invocation decay would distort priorities badly.
*(P0-verified — already satisfied: decay is wall-clock-anchored AND
idempotent across bursty rebuilds, `must_memorize.py:148-160`. Nothing
to build; the invariant just has to be preserved. See P0 findings.)*

---

## Goal A — leanness (Levers 1 & 2)

### Lever 1 — build-side cuts (make *writing* memory cheaper)

1. **Collapse the per-session calls.** Today the lifecycle issues
   ~3 summarize calls per new session — short summary, detailed
   archive, must-memorize extraction — *all reading the same
   transcript* *(P0-verified: exactly 3 per new session, 2 per addendum,
   and uncached — `lifecycle.py:383,387`; see P0 findings)*.
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
then summarizes the flagged transcripts on the **subscription rail
instead of the API rail**. The concrete mechanism is the `subagent`
isolation strategy (B8): the interactive session **delegates each
flagged transcript to an isolated summarizer sub-agent**, which adopts
the summarizer role, emits the markdown, **writes it straight to the
store**, and returns only a short confirmation. No programmatic
`claude -p` spawn — a sub-agent of the interactive session rides the
subscription. API budget zero.

Three properties matter here:

- **Vendor-neutral.** "Adopt the summarizer role, emit the markdown,
  write to the store" is a protocol *any* interactive agent with a
  sub-agent (or equivalent isolated context) can run — not a
  Claude-specific mechanism (see the vendor-decoupling principle and
  the contract-vs-strategy seam in B8).
- **It subsumes Lever 1's biggest waste.** Each transcript is loaded
  into a single bounded context *once* — the summarizer sub-agent's —
  and all three artifacts (short / detailed / must-memorize) come out of
  that one read; the 3×-resend simply disappears. B1 and Lever 1
  reinforce each other rather than compete.
- **Self-validating, in two layers.** The sub-agent validates its own
  emission — all sections present, within budget — *before* writing, and
  fails loudly rather than corrupting the store. After it returns, the
  parent's non-AI bookkeeping does a cheap **structural** re-check
  (files present, frontmatter parses, within budget) — defense in depth
  at zero AI-context cost. On failure the transcript stays flagged and
  the summarized-watermark (B5) does **not** advance, so the next sweep
  retries it. Together these are the "output validation" half of the
  `anthropic.py:32-35` TODO.

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

> **Trigger RESOLVED (2026-06-07):** the sweep fires at **persona-session
> bootstrap, shared by all personas** (any persona conversation start),
> NOT a config-gated trigger-set defaulting off. The "Configurable trigger
> set / `slack_bridge_dm` opt-in default off" paragraph below is
> superseded — broadening the trigger is safe because the executor is
> always the Task-tool sub-agent (B8). See "B3 — implementation design".

A **team memory sweep** enumerates the team roster and rebuilds, in
turn, each persona **that has a memory store configured** — skipping
personas with no memory config. Two things were pinned in P0, both now
resolved: the **canonical roster source** — *(P0-verified:
`configs/personas.yaml` is authoritative, no competing notion;
`workflow_runner/compile/pipeline.py:218`, `slack_bridge/multi.py:178`)*
— and the **persona → tiger-memory-config resolution** — *(P0-verified:
convention-based — persona `X` maps to
`<team>/memories/X/tiger-memory.config.yaml`, `slack_bridge/multi.py`;
the config's `agent.name` must equal the roster name. No new registry
needed.)* With those, "no persona left behind" is guaranteed *by
construction* — it does not matter who has been talking.

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
attribution until its session has been folded into memory. *(P0-verified:
nothing prunes `threads.json` today — `ThreadStore.set()` is
append/update-only, no `del`/`pop`/TTL, and the migrator copies every
key, `persistence.py:131-148`. So B5's loss scenario cannot occur on
current code; this safeguard is **preventive** — an invariant to guard
should pruning ever be added. One nuance: `set()` can overwrite an
existing thread's persona (re-attribution) but cannot drop it.)*

### B6 — Residual risk: catch-up burst after long silence

A wake that follows a long quiet stretch eats a big backlog in one
interactive session — quota spent on memory instead of the human's
actual task. Mitigations, layered:

- Lever 1 cuts make each session ~3× cheaper to summarize;
- the **per-wake cap** (B3) spreads a large backlog over several
  wakes;
- the **cost/scope cap** (Lever 1.4) is the hard backstop.

### B7 — In-session summarization is a new trust boundary

The spawned summarizer had hard isolation — its own subprocess, its own
cwd (see the feedback-loop note at `summarizers/anthropic.py:23-36`).
The `subagent` strategy (B8) restores most of it: a separate context
window plus tool access we can scope down. But the summarizer still
reads untrusted transcript text and still runs inside the team's trust
domain, so three hazards remain to design against:

- **Transcripts are untrusted input.** A past conversation may contain
  text that reads as an instruction. Pulled into the summarizer's
  context, that is a prompt-injection surface. The summarizer must treat
  transcript content as *data to be summarized* — quoted, fenced,
  clearly delimited — never as instructions.
- **Constrain the summarizer sub-agent.** Give it only what the
  contract needs — **Read** (the transcript source + the persona's
  store), **Write** (scoped to that store path), and **nothing else**:
  no shell, no network, no writes outside the store — plus
  summarizer-only instructions. A narrow tool surface both blunts the
  injection risk above and keeps a malformed run from touching anything
  beyond its target.
- **Don't let upkeep turns become a new source.** The spawned
  summarizer routed *its own* transcripts to a throwaway cwd-slug so the
  next rebuild would not re-ingest them (`anthropic.py:23-36`). The
  sub-agent strategy needs the analogous guarantee. *(P0 finding:* the
  adapter — `sources/claude_transcript.py`, kind `claude_code` — globs
  **every** `*.jsonl` in the project dir, filtered only by mtime and the
  persona filter, `:89-128`. A summarizer sub-agent's transcript is
  *unattributed*, so the **strict multi-persona default**
  (`persona` set, `include_unattributed=false`) already **excludes** it
  — the target deployment is safe. But **single-tenant** (`persona=None`)
  or `include_unattributed=true` would **re-ingest** it. The adapter
  inspects no `isSidechain`/`parentUuid` marker. **P2 fix:** add an
  explicit sidechain/sub-agent skip as defense-in-depth, independent of
  the persona filter. **Still needs a runtime check:** whether a
  sub-agent writes a *separate* glob-able `.jsonl` at all, or nests its
  turns as `isSidechain` entries inside the parent session file.*)*

### B8 — Context pressure: resolved by the `subagent` isolation strategy

This *was* the sharpest tension in the design — and it sat **between the
two goals themselves**. The spawned summarizer got a **fresh context
window per call**: it read one transcript and exited. A naive in-session
summarization — the driver doing it in *its own* turns — would summarize
*on top of* everything the driver is already holding (its briefing, its
current journal task), crowding the window and degrading the human's
real work, especially on large transcripts (the 120k-char ceiling exists
for a reason). Subscription-friendly but context-hostile.

**Resolution — delegate to an isolated sub-agent.** A vendor sub-agent
(Claude Code's Task tool) runs in its **own context window**, can
**write files to the store directly**, and returns **only a short
confirmation** to the parent. That recovers the fresh-window property
the old `claude -p` spawn had *and* keeps the work on the subscription
rail — so we no longer trade context for billing. The tension dissolves.

**Operator-verified by probe (2026-06-06).** A Claude Code sub-agent was
given a read + a file-write and asked to return a one-line confirmation.
It (a) ran on the **subscription, not the API**, (b) **wrote the file to
disk** itself, and (c) returned **only** the short confirmation — the
bulky output never transited the parent's context. Direct store-write +
short-return is confirmed to work. *(This is a Claude Code property,
verified in this deployment; other vendors verify their own — which is
exactly why isolation is a* strategy *adapter, not a baked assumption.)*

**Contract vs. strategy (the vendor-decoupling seam).** Keep two things
separate:

- The **summarization contract** is vendor-neutral and shared: given a
  flagged transcript + the persona's store config, produce
  {short, detailed, must-memorize}, self-validate, write to the store,
  return a short confirmation (or a structured failure).
- The **isolation strategy** is a swappable per-vendor adapter:
  `subagent` is the one we build. `fresh_session`, `inline_mapreduce`,
  and the legacy `api_spawn` are other shapes a different vendor could
  register without touching the contract. **Scope: `subagent` only for
  now** — we are not building fallbacks for vendors that lack sub-agents
  yet.

**What this does to the earlier levers.** Because the sub-agent's window
is *separate from the driver's*, the transcript pre-filter (Lever 1.2)
and the per-wake cap (B3) are **no longer load-bearing for protecting
the driver's window** — they now bound the *sub-agent's* context and the
*total quota per wake*, which is ordinary hygiene rather than a
feasibility gate. The budget question (open-Q4) reframes from "how much
of the driver do we spend?" to "how many sub-agent spawns per wake?" —
answered by the per-wake cap.

**Oversized-transcript fallback.** If a single transcript overflows even
the sub-agent's own window, the fallback stays isolated from the driver:
**map-reduce inside the strategy** — chunk-summarize the transcript (one
bounded pass per chunk), then reduce the chunk summaries into the three
artifacts — all within sub-agent context, never the driver's. The
must-memorize extraction runs at reduce time over the chunk summaries;
the quality trade-off of chunking is accepted only on the overflow path.

### B1/B8 — implementation design (2026-06-07, Anzai)

Grounded in the shipped P1 + P2-adapter code. **The seam: split
`lifecycle.rebuild()` at the AI boundary into three stages**, mirroring
drive-journal's "non-AI sweep + AI in-session". Today `rebuild()` does
discovery → `_decide` → summarize (spawns `claude -p`) → rollups →
briefing in one CLI process; the subscription model needs the *summarize*
middle to run inside an interactive session.

1. **plan (non-AI, Python)** — `plan_rebuild(cfg, store) -> WorkManifest`:
   `_build_adapters` + `discover` + `_decide`, apply the per-wake session
   count (reuse `_cap_reason`), and for each `SUMMARIZE_NEW`/`RE_SUMMARIZE`
   record build the **collapsed prompt** (reuse `combined_summary.md` +
   `_fill_prompt`) over the **pre-filtered** content (P1.1). Emit a JSON
   manifest: `[{conversation_uuid, action, prompt, short_path,
   archive_path, expected_budget}]`. No AI, no spend.

2. **execute (in-session — the `subagent` strategy)** — the interactive
   driver walks the manifest and spawns ONE constrained sub-agent (Task
   tool) per item: tools limited to Read(transcript source + *this*
   persona's store) / Write(store path only) / no shell, no network (the
   B7 trust boundary). The sub-agent emits the
   `@@SHORT@@/@@DETAILED@@/@@MUST_MEMORIZE@@` bundle, **self-validates**
   via `parse_collapsed` (all sections present, within budget), writes
   short + archive + merges must-memorize **straight to the store**, and
   returns only a short confirmation — the bulky output never re-enters
   the driver's context (B8 fresh-window). API budget zero.

3. **finalize (non-AI, Python)** — `finalize_rebuild(cfg, store, manifest)`:
   the parent's **structural re-check** (B1 two-layer validation) over
   each item — short + archive exist, frontmatter parses, body within
   budget; on failure leave the transcript flagged and do NOT advance the
   summarized-watermark (B5) so the next sweep retries. Then the existing
   non-AI tail unchanged: `_cascade_all_rollups`, `_refresh_longer_memory`,
   `_apply_decay`, `_write_state` (+ metrics), `rebuild_briefing`.
   - **Landed (2026-06-07):** the non-AI tail is now factored into
     `lifecycle._finalize_rebuild(...)`, shared verbatim by `bootstrap` /
     `rebuild` / `resummarize` (behavior-preserving DRY refactor). The
     in-session path will call the same helper after its sub-agents write
     the per-session artifacts. The structural re-check + manifest
     plumbing are deferred until the stage-2 executor exists (building
     them now would be unused/speculative).

**P1.3 is reused, not wasted.** The stage-2 *contract* IS P1.3's
`combined_summary.md` + `parse_collapsed` — the in-session read-once is
exactly the collapsed single pass. Likewise P1.1 prefilter shrinks the
sub-agent's read, P1.2 cap becomes the per-wake cap (B3/B6), B7 keeps the
sub-agent's own transcript out of the next sweep, and B4 routes each
persona to the right store in a roster sweep. Every P1/P2-adapter piece
feeds B1.

**Contract vs. strategy seam.** `subagent` (Task tool) is the strategy we
build; `api_spawn` (today's `claude -p`) stays registered as the fallback.
A `summarizer.strategy` config selects it; `subagent` is a no-op from the
bare CLI (it needs an interactive host) and raises a clear "run via the
memory-sweep skill" error if invoked headless.

**RESOLVED — stage-2 host / skill placement (2026-06-07, Operator).** The
executor is a vendor-neutral `OPERATING.md`-style contract; its trigger
surface is **persona-session bootstrap, shared by all personas** — NOT a
bespoke skill and NOT drive-journal-only. Any persona conversation start
calls the same gated hook; drive-journal and `slack_bridge` are two
callers. The executor is always the Task-tool sub-agent, so the trigger
context's billing is irrelevant (subscription-safe by construction). See
the "B3 — implementation design" subsection below and the resolved
open-Q2.

Stages 1 + 3 (plan, finalize) are **decision-independent** and can be
built first. **Build order once unblocked:** (i) `plan_rebuild` +
`WorkManifest` + tests; (ii) `finalize_rebuild` + structural validator +
tests; (iii) the `subagent` strategy + the in-session skill per the
placement decision; (iv) **B3** roster sweep (atomic team claim, staleness
floor, per-wake cap, per-persona resumable) wrapping the
plan→execute→finalize loop over the `configs/personas.yaml` roster.

### B3 — implementation design (2026-06-07, Anzai)

**The shared persona-session bootstrap hook.** One function — call it
`tiger_memory.sweep.maybe_sweep_roster(team_root, *, now, trigger)` — is
THE hook. Every persona-session-bootstrap caller invokes it; today's
known callers:
- **`slack_bridge`** — already fires `_trigger_tiger_memory_rebuild` on
  each new thread (`bridge.py:469`). Generalize it: instead of
  `tiger-memory rebuild --background` for the *active* persona via
  `claude -p`, call the shared hook (roster-wide, sub-agent executor).
- **`drive-journal`** — its wake sweep calls the same hook opportunistically.
- (Future callers — any other interactive persona entrypoint — plug in
  the same one-liner.)

**Gating (non-AI bookkeeping, exactly the journal-sweep mold).** The hook
returns a decision without doing any AI work itself:
1. **Team `last_sweep_at` watermark + staleness floor.** Team-scoped state
   file at **`<team_root>/.tiger-memory/sweep-state.json`** (team_root =
   the persona config's `…/memories/` parent — i.e. `memories/<persona>/
   tiger-memory.config.yaml` → `team_root = parent.parent.parent`). If
   `now - last_sweep_at < floor` (default **24h**), return "not due" — no
   sweep, cheap no-op on every trigger.
2. **Atomic claim (team-scoped lease).** Reuse the drive-journal
   heartbeat-as-soft-lease pattern: write a claim token + timestamp into
   the sweep-state file with a compare-and-set re-read. If another session
   holds a fresh claim, return "busy" and skip — only one session sweeps
   per floor window. (A per-store lock alone is insufficient — it
   serializes writes but still lets two sessions do redundant work.)
3. **Per-wake cap.** Cap personas-per-wake (default **N** = small) so a
   large backlog spreads across several wakes; reuse the P1.2
   `_cap_reason` per-session cap *inside* each persona's rebuild.
4. **Roster walk.** Enumerate `configs/personas.yaml` (the authoritative
   roster, P0-verified), resolve each persona's store via the
   `memories/<persona>/tiger-memory.config.yaml` convention, skip personas
   with no memory config, and run the **plan → execute (sub-agent) →
   finalize** loop per persona. Commit each persona atomically and advance
   a **per-persona** progress marker so an interrupted sweep resumes where
   it stopped; advance the team watermark only when all due personas
   completed (or the per-wake cap is hit).

**Executor billing (the load-bearing invariant).** Stage-2 summarization
is always the Task-tool sub-agent (B1/B8), so broadening the trigger to
any persona session does NOT reintroduce API billing — the trigger
context's billing is irrelevant.

**Build slices (s11–s14):** (a) `sweep.py` gating — watermark read/write +
staleness floor + atomic claim, pure-Python + tests; (b) the roster walk +
per-persona resumable progress; (c) the `subagent` strategy + in-session
executor contract (reusing `combined_summary.md` + `parse_collapsed` +
`finalize_rebuild`); (d) wire the two callers (`slack_bridge` generalized,
drive-journal) to the shared hook. Each slice: test-first, 100% cov,
`anzai:` commit, Slack.

---

## Open questions (need Operator input)

1. **Slack-bridge billing — RESOLVED into config (see B3).** The
   trigger set is a config knob; `slack_bridge_dm` defaults **off**, so
   the sweep is subscription-safe out of the box. The underlying
   billing fact (is *your* bridge subscription- or API-authed?) still
   informs whether you flip it on — but the design no longer needs the
   answer to proceed. Remaining input wanted: your default preference.
2. **Skill placement — RESOLVED (2026-06-07, Operator).** Neither framing
   was right; the answer is broader. The in-session rebuild is triggered
   by **ANY persona conversation**, not only the journal driver ("not all
   work is done in the journal driver"). The trigger hook lives at
   **persona-session bootstrap** — the point where any Shohoku persona
   conversation begins — and is **shared by all personas**; drive-journal
   is just *one* caller. This is the B3 invariant ("any human contact with
   any teammate keeps the whole roster fresh") realized as a shared hook,
   not a bespoke skill. Subscription-safety is preserved because the
   executor is **always** the Task-tool sub-agent (B8) regardless of which
   conversation triggered it — the trigger context's billing is
   irrelevant. (The existing `slack_bridge._trigger_tiger_memory_rebuild`,
   which today fires `tiger-memory rebuild --background` for the *active*
   persona via `claude -p`, becomes one caller of the shared hook,
   generalized to a gated roster sweep with the sub-agent executor.)
3. **Staleness floor & per-wake cap values.** Starting points to tune
   (e.g. 24h floor, cap of N sessions/wake).
4. **In-session summarization context budget — RESOLVED (see B8).** The
   `subagent` isolation strategy runs summarization in a *separate*
   context window, so memory upkeep no longer spends the driver's
   context at all. The budget question reduces to "how many sub-agent
   spawns per wake," which the per-wake cap (B3) already governs.
   Operator-verified by probe (2026-06-06): a Claude Code sub-agent runs
   on the subscription, writes to the store directly, and returns only a
   short confirmation.

## P0 verification findings (2026-06-06)

Code survey of `src/tigerharness/tiger_memory/` and `slack_bridge/` on
`main`. Each `(verify in P0)` fact, resolved with file:line:

| Claim | Verdict | Evidence |
|---|---|---|
| ~3 summarize calls per new session | **Confirmed** — 3 per new (short + detailed + must-memorize), 2 per addendum | `lifecycle.py:383,387,429,444,522` |
| Transcript re-sent per call; no prefix cache | **Confirmed (worse than implied)** — each call re-clips and re-sends `rec.content`; backend is one-shot, no `cache_control` | `lifecycle.py:427,442,519`; `anthropic.py:6-7,78-105` |
| Defaults 400 / 60 / 120k chars; window 2/7/28/90; decay 7/14/28; owner_explicit locked | **All confirmed in code**, not just docs | `config.py:65,71,77,104-107,82-85` |
| must_memorize decay is wall-clock-anchored | **Confirmed — and already idempotent across bursty rebuilds** | `must_memorize.py:148-160` |
| Nothing prunes `threads.json` | **Confirmed** — append/update-only; no `del`/`pop`/TTL; migrator copies every key | `persistence.py:131-148`; `migrate.py:96-107` |
| `configs/personas.yaml` is the authoritative roster | **Confirmed — no competing source** | `workflow_runner/compile/pipeline.py:218`; `slack_bridge/multi.py:178` |
| Sub-agent transcript could be re-ingested | **Open risk, gated by the strict filter** — see note | `sources/claude_transcript.py:89-128` |

What this moved in the design:

- **The 3× waste is real and uncached** — confirms the B1 premise. No
  prefix-cache exists to lean on today, so the in-session *read-once*
  (B1) is the actual win, not call-collapsing on the API path.
- **Rollups are cheap** — 1 summarize call per *dirty* daily/weekly/
  monthly period (`lifecycle.py:565,617,668`), reading prior summaries,
  not transcripts. The per-session 3× dominates cost.
- **Decay needs no new code** — the wall-clock anchor advances by exactly
  `points*rate` days (`must_memorize.py:159-160`); a three-week gap
  decays identically whether swept once or daily. Preserve, don't build.
- **B5 is preventive, not corrective** — nothing deletes attributions
  today, so the loss scenario cannot occur on current code. Re-attribution
  (overwriting a thread's persona) is possible; deletion is not.
- **Roster → config is convention** — enumerate `personas.yaml`, resolve
  each store at `memories/<persona>/tiger-memory.config.yaml`. The B3
  sweep needs no new registry.
- **B7 is the one live risk** — the strict multi-persona default already
  excludes a summarizer sub-agent's unattributed transcript, but
  single-tenant / permissive mode would re-ingest it, and the adapter
  reads no sidechain marker. P2 adds an explicit sidechain skip; a
  runtime check (does a sub-agent even emit a separate `.jsonl`?) stays
  open.

Read-side baseline (`teams/Shohoku/memories/`): briefings run ~575–1,830
words (~1.5k–4.6k tokens) today; for Anzai, `must_memorize.md` is 820 of
1,366 briefing words (~60%) — the pinned core dominates, exactly Lever
2's target. Per-call transcript input is capped at `max_prompt_content_chars` =
120k chars (~30k tokens); a new session's three calls send 120k + 120k +
60k chars (the extractor clips to half, `lifecycle.py:519`) ≈ 300k chars
/ ~75k tokens of transcript input worst-case — before prompt scaffolding
and output, and re-sent uncached. Tokens-per-rebuild on real transcripts
needs an instrumented run — deferred to the P1/P2 measurement harness.

## Phasing

- **P0 (this doc) — DONE (2026-06-06).** Design locked; every
  `(verify in P0)` fact resolved against source (see **P0 verification
  findings** above). Remaining runtime items, carried into P2: a
  tokens-per-rebuild measurement on real transcripts, and the
  sub-agent-transcript runtime check (B7).
- **P1 — Lever 1 (build-side cuts).** Transcript pre-filter +
  cost/scope cap + context-budget tiering — the parts that survive the
  move to in-session. (Collapse/prefix-cache applies only to the legacy
  API-spawn path; it is subsumed by P2's in-session read-once.) Lowest
  risk, no change to what the agent reads.
  - **Status (2026-06-07): pre-filter + thin metrics hook SHIPPED.**
    `tiger_memory/prefilter.py` (`filter_transcript`) elides
    `[tool_result]` payloads (→ `[tool_result elided: N chars]`) and
    strips `<system-reminder>` blocks from the *rendered* transcript,
    keeping all prose + `[tool_use: …]` intents. It runs **once per
    record** in `lifecycle._process_decisions` (via
    `dataclasses.replace` on the `SourceRecord`), *above* the summarizer
    interface, so every short/detailed/addendum/extractor call reuses the
    de-noised content and the win survives the P2 in-session move. Config
    knobs: `prefilter.{enabled,drop_tool_results,drop_system_reminders}`
    (all default-on; conservative). The thin metrics hook
    (`tiger_memory/metrics.py` `RebuildMetrics`) accumulates
    `sessions_processed`, `summarize_calls` (3 new / 2 addendum — the
    P1.3-collapse baseline), and `content_chars_raw` vs.
    `content_chars_filtered`, stamped into `state.json["metrics"]` so the
    reduction is provable across rebuilds.
  - **Status (2026-06-07): cost/scope cap (Lever 1.4) SHIPPED.** The
    automatic `rebuild` processes at most `cap.max_sessions_per_rebuild`
    (default 10) sessions, or stops once cumulative spend
    (`summarizer.cost_so_far`) reaches `cap.max_usd_per_rebuild` (default
    20.0) — whichever trips first (`lifecycle._cap_reason`, checked
    before each session). The remainder is **deferred with no extra
    state**: a skipped session writes no archive, so the next rebuild's
    `_decide` re-emits it as `SUMMARIZE_NEW` — resumability by
    construction (the design's "resumable via state.py" needs no state at
    all). `metrics.stopped_reason` (`"session_cap"`/`"usd_cap"`/`None`)
    surfaces a cap hit. **Scope:** caps apply to `rebuild` only;
    `bootstrap` (`--limit`) and `resummarize` (`--since`) are user-scoped
    and exempt. This answers the `anthropic.py:32-35` TODO's "cost cap +
    scope cutoff" half.
  - **Status (2026-06-07): 3→1 call-collapse SHIPPED (default OFF).** A
    `combined_summary.md` prompt emits {short, detailed, must-memorize}
    behind a strict `@@SHORT@@`/`@@DETAILED@@`/`@@MUST_MEMORIZE@@`
    delimiter contract; `collapse.py` `parse_collapsed` validates it
    (all markers present, in order, short+detailed non-empty) and
    `lifecycle._write_session_collapsed` issues ONE call, writes the
    artifacts, and parses must-memorize from the same output —
    `metrics.summarize_calls` drops 3→1. On any `CollapseParseError` it
    **falls back to the legacy 3-call path** (no store corruption; the
    spent attempt + 3 fallback calls are counted honestly as 4). Gated
    by `collapse.enabled` **default false**: the design downgrades
    call-collapse to the legacy API-spawn path (P2's in-session
    read-once subsumes it for free), so it ships opt-in with the safe
    3-call path remaining the default and the fallback. **P1 floor
    COMPLETE:** pre-filter + cost/scope cap + metrics + call-collapse all
    landed, tested, 100% coverage, doc synced. Model-tiering (Lever 1.3)
    stays optional/API-specific and is deferred to P2's context-budget
    framing.
- **P2 — Lever 3 (subscription-safe multi-persona).** In-session
  summarization skill via the `subagent` isolation strategy (B1, B8) +
  roster sweep (B3) + team-qualified IDs (B4) + the summarized-watermark
  safeguard (B5).
  - **Status (2026-06-07): adapter hardening landed (B7 + B4 reader + B5
    noted).** **B7** — `sources/claude_transcript.py:_record_for` now
    drops every `isSidechain == true` row up front, so sub-agent turns
    are never ingested (nested rows fall out of a parent session; an
    all-sidechain file collapses to empty → skipped). *Runtime check
    resolved:* `isSidechain` is a real per-row field across the corpus,
    but **0 rows are `true`** in the current 774-file project dir — so
    today this is pure defense-in-depth, independent of the persona
    filter, exactly as designed. **B4** — `_normalize_owner` upgrades a
    threads.json `persona` to `(team|None, name)`, accepting a bare name,
    a `team/name` string, or a `{team, name}` dict; `_allowed` enforces
    the team only when BOTH the adapter (`team=` source field) and the
    record carry one, else name-only — fully backward compatible.
    *Reader-side only*: nothing writes the `{team,name}` shape yet (the
    bridge still writes bare names), so this is forward-compat insurance
    as the design intended. **B5** — preventive only; P0 confirmed
    nothing prunes `threads.json` (`ThreadStore.set()` is append/update),
    so there is **no code to add today** — the invariant holds by
    construction and only needs a guard if pruning is ever introduced.
    **Next:** the big architectural piece — **B1/B8** in-session
    summarization via a Claude Code sub-agent — plus **B3** roster sweep.
- **P3 — Lever 2 (read-side diet).** Lean core briefing + on-demand
  drill, measured before/after.
  - **Status (2026-06-07): measurement hook landed.** `briefing.py` now
    sizes the assembled briefing — `_briefing_stats` totals chars/words
    over the memory-bearing files (`must_memorize.md`, `longer_memory.md`,
    and the recent/daily/weekly/monthly layers), written as a
    `.briefing_metrics.json` sidecar (atomic with the briefing swap, like
    `.fingerprint`) plus a "Briefing size: N words / M chars" line in
    `MANIFEST.md`. Mirrors the P1.2 write-side metrics so the read-side
    diet is provable before/after; the per-section breakdown pinpoints the
    weight (the design's baseline: `must_memorize.md` ≈ 60% of Anzai's
    briefing). **Sequencing note:** P3 was started ahead of P2's
    completion because P2's remaining work (B1 stage-2 executor + B3) is
    blocked on the Operator's skill-placement decision; read-side (Lever 2,
    `briefing.py`) and write-side (Lever 3, `lifecycle.py`) are independent
    levers, so there's no coupling.
  - **Status (2026-06-07): lean-core cut landed.** New config
    `briefing.resident_layers` (subset of recent/daily/weekly/monthly,
    **default all four**) selects which walking-window layers are copied
    into the always-resident briefing. Non-resident layers are NOT
    deleted — they stay in `journal/` and are listed in a MANIFEST
    "Drill on demand" section (reachable via `tiger-memory drill` /
    search, which the README already documents). `must_memorize.md` +
    `longer_memory.md` are always resident (load-bearing). This is the
    design's "lean core + lazy-load deeper via drill", config-driven and
    **recall-safe by default** (default = no change). Operators set e.g.
    `resident_layers: [recent, daily]` per persona to realize the diet as
    weekly/monthly rollups accumulate; the `.briefing_metrics.json`
    sidecar measures the resident size so the drop is provable. *(Measured
    on the real Anzai store: 2559 resident words today — recent 38% /
    must_memorize 32% / daily 30% / weekly+monthly empty — so the win is
    forward-looking as deeper rollups grow.)*

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
- **2026-06-06 — round 3 (Anzai + Operator).** Resolved B8's central
  tension. The `subagent` isolation strategy runs summarization in an
  isolated context window that bills to the **subscription** (not the
  API), writes to the store directly, and returns only a short
  confirmation — recovering the spawned summarizer's fresh window
  *without* its API cost. **Operator-verified by probe.** Folded in:
  (1) the **contract-vs-strategy seam** — a vendor-neutral summarization
  contract plus `subagent` as one swappable isolation strategy (the only
  one we build for now) — promoted to a vendor-decoupling lever;
  (2) B1 reworked to *delegate* to the sub-agent, with two-layer
  validation (sub-agent self-validates, parent structurally re-checks)
  and watermark-on-failure retry; (3) B7 gains a **constrain-the-
  sub-agent** hazard (Read transcript+store / Write store-only / no
  shell or network) and reframes the feedback-loop worry as a sub-agent
  verify item; (4) open-Q4 resolved — upkeep no longer spends the
  driver's context; (5) pre-filter + per-wake cap **downgraded** from
  feasibility gate to hygiene; (6) map-reduce kept as the oversized-
  transcript fallback.
- **2026-06-06 — P0 verification pass (Anzai).** Surveyed
  `tiger_memory/` + `slack_bridge/` and resolved every `(verify in P0)`
  fact against source (new **P0 verification findings** section; inline
  markers updated). Headlines: the 3×-per-session call count is real and
  **uncached** (`anthropic.py` is one-shot, no `cache_control`) — so B1's
  read-once is the genuine win; **decay is already wall-clock-anchored
  and idempotent** across bursty rebuilds, nothing to build; **nothing
  prunes `threads.json`**, so B5 is preventive not corrective;
  **`configs/personas.yaml` is the sole authoritative roster** and
  persona→store is a `memories/<persona>/tiger-memory.config.yaml`
  convention; all config defaults confirmed in code. One live risk
  remains — **B7 sub-agent re-ingestion**: the strict multi-persona
  filter excludes it, but single-tenant/permissive mode would re-ingest,
  and the adapter reads no sidechain marker (P2 fix + a runtime check
  still open). Done on a `main`-based branch in an isolated worktree.
- **2026-06-07 — P1.1 implementation (Anzai).** First build session of
  the rework: shipped the transcript **pre-filter** (`prefilter.py`,
  pure `filter_transcript`) and a **thin metrics hook** (`metrics.py`,
  `RebuildMetrics` → `state.json["metrics"]`), wired once-per-record into
  `lifecycle._process_decisions` ahead of `_clip`, with
  `prefilter.{enabled,drop_tool_results,drop_system_reminders}` config
  knobs (default-on). Landed the metrics scaffold first so the
  pre-filter's char reduction is measurable; both kept above the
  summarizer interface so they survive the P2 in-session move. 100%
  coverage held; full suite green. See the P1 status note in **Phasing**.
  Next: the Lever 1.4 cost/scope cap on top of the metrics hook.
- **2026-06-07 — P1.2 implementation (Anzai).** Shipped the **cost/scope
  cap** (`lifecycle._cap_reason` + `CapConfig`): the automatic `rebuild`
  stops after N sessions (default 10) or a USD ceiling (default 20.0),
  whichever trips first, deferring the remainder to the next rebuild with
  **zero extra state** (skipped → no archive → re-discovered). Cap hit is
  surfaced via `metrics.stopped_reason`. `bootstrap`/`resummarize` stay
  uncapped (user-scoped). Closes the "cost cap + scope cutoff" half of the
  `anthropic.py:32-35` TODO. 100% coverage held; full suite green (2872).
- **2026-06-07 — P1.3 implementation + P1 floor complete (Anzai).**
  Shipped the 3→1 **call-collapse** (`collapse.py` `parse_collapsed` +
  `combined_summary.md` + `lifecycle._write_session_collapsed`), behind
  `collapse.enabled` **default OFF** with automatic fallback to the
  legacy 3-call path on any parse drift. With this, the **P1 floor is
  complete** (pre-filter + cost/scope cap + metrics + call-collapse, all
  tested at 100% coverage, doc synced). The collapse only benefits the
  legacy API-spawn path — P2's in-session read-once subsumes it — so it's
  opt-in. Full suite green (2885). Next: P2 (Lever 3) — in-session
  summarization via the `subagent` strategy (B1/B8), roster sweep (B3),
  team-qualified IDs (B4), summarized-watermark (B5).
- **2026-06-07 — P2 adapter hardening (Anzai).** First P2 session: the
  contained, low-risk items. **B7** — explicit `isSidechain` skip in
  `claude_transcript.py:_record_for` (defense-in-depth vs sub-agent
  transcript re-ingestion; runtime check found the field present but 0
  `true` rows in the corpus). **B4** — `_normalize_owner` + team-aware
  `_allowed` make persona attribution `(team, name)`-qualified, backward
  compatible with bare-name entries and team-less adapters; reader-side
  only (no writer yet). **B5** — confirmed no-op today (nothing prunes
  `threads.json`); documented as a guard-if-pruning-added invariant.
  Full suite green (2902); 100% coverage. Next: B1/B8 (in-session
  summarization via sub-agent — needs an Operator call on skill
  placement) + B3 (roster sweep).
- **2026-06-07 — B1/B8 implementation design (Anzai).** Design-first
  session (no code; decision-agnostic, advances P2 in-order). Added the
  **B1/B8 — implementation design** subsection above: split `rebuild()`
  at the AI boundary into non-AI **plan** → in-session **execute**
  (`subagent` strategy) → non-AI **finalize**, mirroring drive-journal.
  Key realization — the stage-2 contract IS P1.3's `combined_summary.md`
  + `parse_collapsed`, so the collapse work is reused on the subscription
  rail (not legacy-only); P1.1/P1.2/B7/B4 all feed B1 too. Planner +
  finalizer are decision-independent and slated first; only the stage-2
  host depends on the open skill-placement decision (recommend folding
  into drive-journal). No suite/coverage change (doc only).
- **2026-06-07 — finalize-stage refactor (Anzai).** First *code* step of
  the B1 split, decision-independent: extracted the non-AI tail (rollups,
  longer-memory fold, decay, state, briefing) — duplicated verbatim
  across `bootstrap`/`rebuild`/`resummarize` — into a single
  `lifecycle._finalize_rebuild(...)`. Behavior-preserving (bootstrap/
  resummarize keep `duration_sec=None`; order unchanged); the in-session
  path will reuse this exact tail. `plan_rebuild` + the structural
  validator deferred to stage 2 (avoid speculative unused code). Full
  suite green (2902); 100% coverage held (existing tests cover the
  helper; lifecycle.py −8 statements).
- **2026-06-07 — P3 read-side measurement hook (Anzai).** With P2's
  remaining work (stage-2 executor + B3) blocked on the Operator's
  skill-placement decision, pivoted to the decision-independent read-side
  lever. Added `briefing._briefing_stats` + a `.briefing_metrics.json`
  sidecar + a MANIFEST size line, sizing the assembled briefing (total +
  per-section). Measurement-first, mirroring P1.2, so the upcoming
  lean-core cut is provable and recall-safe. Independent of P2 (different
  file/lever). Full suite green (2904); 100% coverage held.
- **2026-06-07 — P3 lean-core cut (Anzai).** Landed `briefing.resident_layers`
  (default all four → backward compatible): non-resident walking-window
  layers are left in `journal/` and listed as MANIFEST "Drill on demand"
  rather than copied into the resident briefing — the design's "lean core
  + lazy drill", recall-safe by default. `must_memorize`/`longer_memory`
  always resident. Data-grounded via the new sidecar (real Anzai briefing
  = 2559 resident words; win scales as weekly/monthly accrue). Full suite
  green (2910); 100% coverage held.
- **2026-06-07 — open-Q2 resolved + B3 design (Anzai).** Design session
  (s10): folded the Operator's skill-placement answer into the doc —
  open-Q2 RESOLVED (trigger = persona-session bootstrap, shared by all
  personas; executor always the sub-agent → subscription-safe), the
  B1/B8 "stage-2 host" item RESOLVED, the original B3 trigger-set
  paragraph marked superseded, and a concrete **"B3 — implementation
  design"** subsection added: the shared `maybe_sweep_roster` hook,
  team `last_sweep_at` watermark + 24h staleness floor + atomic team
  claim + per-wake cap, roster walk over `configs/personas.yaml` with
  per-persona resumable progress, and the existing
  `slack_bridge._trigger_tiger_memory_rebuild` (active-persona /
  `claude -p`) named as one caller to generalize. Build slices for
  s11–s14 enumerated. Doc-only (no code; team-root path layout flagged to
  pin during the s11 build, like `_load_defaults`); suite unchanged.

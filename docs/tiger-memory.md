# tiger-memory

## At a glance
- **What:** per-persona persistent memory built on **three bounded,
  self-pruning stores** — `skills`, `must_remember`, `emotional` — with a
  session-start briefing, in-persona extraction, and a meditation
  compaction engine (forgetting is first-class).
- **When you need it:** configuring or inspecting a persona's memory. For the
  team-wide refresh protocol see
  [tiger-memory-sweep-protocol.md](tiger-memory-sweep-protocol.md); for the
  rationale see [DESIGN-memory.md](DESIGN-memory.md).
- **Must-not-miss:** stores are **bounded** and **forgetting is
  irreversible** (no safety net). Never hand-edit a store file; let the
  sweep + meditation prune it. Length is measured in **characters**, never
  tokens.

Persistent memory management for Claude Code agents: three bounded stores,
a Python-rebuilt session-start briefing, and a meditation engine that
merges, relevance-downgrades, compacts, and forgets so memory stays focused
on what helps the team now.

## What it does

Turns an agent's *finished* conversation sessions into entries in three
bounded per-persona stores, in-character, then assembles a session-start
briefing the agent reads at the start of each session. Because every store
is bounded, crossing a store's `overflow_limit` triggers **meditation**,
which compacts and forgets to bring the store back under `max` — so memory
self-prunes instead of growing forever.

## Architecture

```
Sources (Claude transcripts, Slack threads, journal worklogs, docs)
    |
    v
Extraction (lifecycle.py): a finished session -> a strict
  @@SKILLS@@ / @@MUST_REMEMBER@@ / @@EMOTIONAL@@ bundle, in-persona
    |
    v
Ingest (bounded_store.py): bundle blocks -> the three stores
    |              \
    |               v
    |          Meditation (meditation.py), per store, ONLY when over
    |          overflow_limit: merge -> relevance-downgrade -> compact ->
    |          guarded-forget, back under max
    v
Stores (journal/skills.md + must_remember.md + emotional.md)
    |
    v
Briefing (briefing.py): skill INDEX + full must_remember + emotional
  view + unprocessed-session notice
    |
    v
Agent reads briefing/ at session start
```

## The three bounded stores

Each store has a scalar, an ordering rule, and a **two-number bound**
(`max` + `overflow_limit`). The two numbers give **hysteresis**: a store may
drift up to `overflow_limit`, and only *then* does meditation fire and
compact it back under `max` — preventing meditate-every-session thrash.

| Store | Holds | Bound | Scalar / ordering |
|---|---|---|---|
| `skills` | learned, reusable lessons (name + trigger + procedure) | count (`max_count`) | `importance = log1p(usage_count)`; no time-decay; recency tie-break |
| `must_remember` | external directives (`owner_explicit` / `preference` / `decision` / `incident`) | characters (`max_length`) | `importance`; `owner_explicit` protected until relevance-downgrade |
| `emotional` | the persona's signed reactions | characters (`max_length`) | signed `weight` in `[-10, +10]`, decays toward 0; ranked by `|weight|` |

On-disk, each store is one markdown file under the persona store's
`journal/` dir (`skills.md`, `must_remember.md`, `emotional.md`); each entry
is a YAML-frontmatter block + body. Don't hand-edit these — meditation owns
pruning, and forgetting has no safety net.

## Key modules

| Module | Purpose |
|---|---|
| `cli.py` | CLI. Writers: `init`, `rebuild`, `pin`. Reader: `state`. In-session executor (subscription rail): `plan` (stage extraction prompts + pack stacks), `ingest-extraction` (one bundle via stdin), `ingest-staged` (single-process glue of all `.extract.md` cards). Team-sweep gating: `sweep-plan`, `sweep-done`, `sweep-complete`, `sweep-release` |
| `config.py` | YAML config loading + validation (the `memory:` block) |
| `entries.py` | the three entry schemas + validation + frontmatter bridge |
| `bounded_store.py` | crash-safe store I/O, length/count, overflow detection, per-store lock, the guarded `forget` |
| `emotional.py` | signed-weight clamp + decay + emotional keep-rank |
| `skills.py` | usage-based skill importance + skills keep-rank |
| `meditation.py` | the compaction engine (merge → relevance-downgrade → compact → guarded-forget) |
| `lifecycle.py` | extraction, in-session staging, fresh-start `rebuild`, `pin`, charter mission sourcing |
| `sweep.py` | team-sweep gating + post-ingest `meditate_all_stores` |
| `briefing.py` | assemble the session-start briefing from the three stores |
| `sources/` | source adapters (claude_code, slack_thread, docs, journal_worklog) |
| `summarizers/` | LLM summarization backends (anthropic, mock) — the only model call |
| `state.py` | per-store JSON state snapshot (count / chars / max / over_overflow) |

## Configuration

A YAML config file (pointed to by `$TIGER_MEMORY_CONFIG` or `--config`).
The annotated reference copy is
[`examples/tiger-memory.config.yaml`](../examples/tiger-memory.config.yaml).

```yaml
agent:
  name: MyAgent
  role: A helpful assistant
  pronouns: they/them

store:
  root: ./memory/   # auto-appends the agent slug; the 3 stores live under journal/

sources:
  - kind: claude_code
    project_path: ~/.claude/projects/-my-project/
    # Optional, for multi-persona Slack-bridge setups:
    # persona: ayako                    # only ingest sessions owned by this persona
    # include_unattributed: false        # opt-in to also include local claude-p sessions

summarizer:
  # Pluggable — "anthropic" is the default. The ONLY model call: the
  # extraction judgement + meditation's similarity/staleness/compaction
  # judgements. See "Adding a new summarizer vendor" below.
  backend: anthropic
  model: claude-opus-4-7
  prompts: default/v1

# The three bounded stores (every key optional — these are the defaults).
memory:
  length_unit: characters        # CONFIRMED: characters, never tokens
  skills:
    max_count: 40
    overflow_limit: 50
  must_remember:
    max_length: 8000             # chars
    overflow_limit: 10000
  emotional_log:
    max_length: 12000            # chars
    overflow_limit: 15000
    weight_cap: 10               # hard cap: |weight| <= 10
    decay:
      magnitude_per_day: 0.1

rebuild:
  trigger: lazy
  idle_threshold_hours: 1

briefing:
  emotional_top: 20              # cap on emotional entries shown (top-by-|weight|); 0 = all
```

Validation is fail-fast at load: `length_unit` must be `characters` (token
units rejected); every store bound must satisfy `0 < max < overflow_limit`;
`weight_cap > 0`; `magnitude_per_day >= 0`.

## Usage

```bash
# Initialize the memory store + validate config
tiger-memory --config my-config.yaml init

# Fresh-start rebuild: drop the retired surface (first run), regenerate the
# session-start briefing (skill index + must_remember + emotional + notice)
tiger-memory --config my-config.yaml rebuild

# Pin a must_remember entry directly.
# NOTE: --kind defaults to owner_explicit (importance 5.0) — the most
# forget-protected kind. A bare `tiger-memory pin "..."` therefore writes an
# owner_explicit directive; pass `--kind preference` for an ordinary note.
tiger-memory --config my-config.yaml pin "Operator prefers tabular diffs" --kind preference
tiger-memory --config my-config.yaml pin "Never force-push main" --kind owner_explicit

# JSON snapshot of the three stores (count / chars / max / over_overflow)
tiger-memory --config my-config.yaml state
```

Extraction itself is driven by the team sweep, not by hand — see the
[sweep protocol](tiger-memory-sweep-protocol.md). The in-session executor
verbs (`plan` / `ingest-extraction` / `ingest-staged`) are driven by the
`sweep-memory` skill, not run manually.

## CLI verbs (the live set)

| Verb | What it does |
|---|---|
| `init` | create the empty store + validate the config |
| `rebuild` | fresh-start: drop the retired legacy surface (first run), regenerate the briefing |
| `pin <memo> --kind <k>` | write one `must_remember` entry. `--kind` **defaults to `owner_explicit`** (importance 5.0, the most forget-protected kind), so a bare `pin` is a protected directive — pass `--kind preference` for an ordinary note |
| `state` | JSON snapshot of the three stores |
| `plan [--max-sessions N]` | stage one extraction prompt per idle, unprocessed transcript + a manifest (items + stacks) |
| `ingest-extraction --uuid <u>` | write back ONE sub-agent's extraction bundle (stdin) for a planned uuid |
| `ingest-staged` | glue every staged `<uuid>.extract.md` card in ONE process (race-free) |
| `sweep-plan` / `sweep-done` / `sweep-complete` / `sweep-release` | team-sweep gating (non-AI) |

The retired verbs `bootstrap`, `search`, `drill`, `tree`, `raw`,
`resummarize`, and `ingest-summary` no longer exist — they belonged to the
chronological-rollup / summary-RAG surface that was removed.

## The extraction contract

Extraction turns one finished session into a strict bundle with three
whole-line section markers, in order (see
`summarizers/prompts/default/v1/extract_memory.md`):

```
@@SKILLS@@
<skill blocks, or NONE>
@@MUST_REMEMBER@@
<must-remember blocks, or NONE>
@@EMOTIONAL@@
<emotional blocks, or NONE>
```

A skill block is `NAME:` / `TRIGGER:` / `PROCEDURE:`; a must-remember block
is `KIND:` / `MEMO:`; an emotional block is `WEIGHT:` / `REACTION:` /
`TEXT:`. A malformed *bundle* (missing/out-of-order markers) is rejected
before any write; an individual malformed *block* is skipped. `NONE` for a
store is a valid, expected outcome — most sessions add little.

## Meditation (the compaction engine)

When a store crosses its `overflow_limit`, the sweep runs `meditate` over
it (per store, under a per-store lock), strictly in order:

1. **merge** near-duplicates — merging raises the survivor's scalar
   (importance / emotional magnitude), clamped;
2. *(must_remember only)* **relevance-check** each `owner_explicit`
   directive against the live charter Mission and **downgrade** stale ones to
   `decision` — this runs *before* any forget;
3. **compact** verbose survivors (the summarizer rewrites a body shorter);
4. **forget** the lowest-keep-ranked entries until under `max`, via the
   guarded `forget`.

Forgetting is irreversible and has no safety net, so the engine logs every
mutation at INFO, defaults the LLM judgement to the **safe** answer on a
garbled verdict, and **never** drops a still-relevant `owner_explicit`
directive (the forget-guard; if it cannot get under `max` without one, it
leaves the store intact and warns). The full design and the ratified
forget-guard semantics are in [DESIGN-memory.md](DESIGN-memory.md) §5.

The broad acceptance/verification suite for the revamp invariants (forget
order with nothing safe to drop, decay boundaries, relevance-downgrade
ordering, concurrent meditation, character-length edges, idempotency, and
malformed-input handling) is
`tests/tiger_memory/test_memory_revamp_qa_defense.py`.

## Session start (the briefing)

`tiger-memory rebuild` assembles `briefing/` from the three stores:

- the **full must_remember** store (highest-importance first);
- an **emotional view** — top entries by `|weight|` (capped by
  `briefing.emotional_top`);
- the **skill index** — name + trigger + one line per skill; only the index
  loads, the persona reads the full skill on demand;
- the **unprocessed-session notice** (`UNPROCESSED.md`): memory is built
  only after a session goes idle, so a still-active session may not be
  reflected yet. **Rule:** if the Operator references something you don't
  recognise, check this memory first, then check for unprocessed/active
  sessions, before claiming ignorance.

## Migrating a roster from the old memory model

A persona config written for the **old** chronological-rollup model still
loads (unknown keys are ignored), but it carries retired keys and lacks the
`memory:` block, so it falls back to defaults. To migrate a persona's
`memories/<persona>/tiger-memory.config.yaml` (or the team-level
`configs/tiger-memory.defaults.yaml`) to the new model:

**1. Remove the retired keys** (they no longer have any effect):

- `budgets.must_memorize_rows` (and any rollup word budgets such as
  `short_summary_words` / `detailed_summary_words` / `*_rollup_*` /
  `longer_memory_words`)
- `budgets.repeat_detection_similarity`
- the whole `decay:` block (the per-kind `days_per_point` table)
- the whole `embedder:` block (RAG is retired)
- `rebuild.resummarize_window_days`
- `briefing.walking` (and `briefing.resident_layers`)

`budgets.max_prompt_content_chars` / `max_staged_content_chars` /
`sweep_stack_content_chars` / `sweep_stack_max_items` are **kept** (still
live for extraction + sweep stacking).

**2. Add the `memory:` block** (optional — omit it to take the defaults
in §7 of the design). To tune, add the keys you want:

```yaml
memory:
  skills:        { max_count: 40, overflow_limit: 50 }
  must_remember: { max_length: 8000, overflow_limit: 10000 }
  emotional_log:
    max_length: 12000
    overflow_limit: 15000
    weight_cap: 10
    decay: { magnitude_per_day: 0.1 }
```

Optionally also add `memory_extract:` (per-section word budgets) and
`briefing.emotional_top` (see the example config).

**3. Fresh-start the store.** Migration is a **fresh start** (no converter,
design §10.6): run `tiger-memory --config <persona-config> rebuild` once.
The first rebuild drops the legacy on-disk surface (old rollup summaries,
`must_memorize.md`, `longer_memory.md`, the `archive/` dir) and regenerates
the briefing; the three new stores then (re)build incrementally as the team
sweep extracts new sessions.

> **Timing.** On a team that tracks live persona configs in git, do the
> roster-YAML migration **with the branch merge**, not before — editing a
> live config to the new schema while `main` still runs the old code would
> desync the config from the deployed code. The new code ignores the
> retired keys, and the old code ignores the `memory:` block, so the safe
> order is: merge the code, then migrate the configs, then `rebuild`.

`tigerharness init` already scaffolds new personas with the new model (no
retired keys), so this migration only applies to configs created before the
revamp.

## Per-persona filtering (multi-bridge integration)

When the slack-bridge runs in **multi-persona mode** (one Slack app routes
DMs to N team members), each persona has its own memory store and only wants
to extract **its own** conversations — Ayako's memory shouldn't include
Sakuragi's threads.

Tell the source adapter which persona owns the conversation by adding a
`persona:` field to the `claude_code` source:

```yaml
sources:
  - kind: claude_code
    project_path: ~/.claude/projects/-home-tigerleap-projects-teams-shohoku/
    persona: ayako               # only ingest sessions owned by Ayako
    # include_unattributed: false  # default: exclude local claude-p sessions
  - kind: slack_thread
    threads_json: ~/.local/state/slack-bridge/shohoku/threads.json
```

The bridge writes `{thread_ts: {session_id, persona}}` entries to
`threads.json` as users DM the bot. The adapter reads that mapping and only
emits records where the stored persona matches.

**Three sessions, three outcomes:**

| Session origin | `threads.json` entry | Strict mode (default) | `include_unattributed: true` |
|---|---|---|---|
| Slack DM addressed to Ayako | `{session_id, persona: "ayako"}` | ✅ in Ayako's memory | ✅ in Ayako's memory |
| Slack DM addressed to Sakuragi | `{session_id, persona: "sakuragi"}` | ❌ excluded | ❌ excluded |
| Local `claude -p` (not via bridge) | not in threads.json | ❌ excluded | ✅ in Ayako's memory |
| Pre-routing Slack thread (PR1-era) | `"session_id"` bare string | ❌ excluded | ✅ in Ayako's memory |

**Backward compat:** if `persona:` is omitted, the adapter emits every
session (legacy single-tenant behavior, unchanged). The threads.json reader
also accepts the pre-routing schema (bare `"session_id"` strings) so older
state files still work.

**`persona=None` semantics.** Both "not in `threads.json` at all" (local
`claude -p` session) and "present but with `persona=None`" (pre-routing
entry) are treated as *unattributed*. Under strict mode (default) they're
excluded from every persona's memory; `include_unattributed: true` brings
them in. There's no way to disambiguate "local work" from "stale pre-routing
thread" via config — migrate old `threads.json` entries by hand, or wait for
them to age out of the discovery window.

## Per-persona journal memory (journal_worklog source)

When the team runs work through the subscription backend's `drive-journal`
driver, all of it happens inside **one** Claude session (the driver's). The
driver adopts each persona in-session, so the raw transcript would collapse
every persona's work into the driver's store. The `journal_worklog` source
fixes that by ingesting the journal's **per-turn worklog records** instead —
each one stamped (by harness code) with the persona that actually did the
work. See [`per-persona-journal-memory.md`](per-persona-journal-memory.md)
and [`subscription-backend.md`](subscription-backend.md) for the write side.

Add the source to each journal-working persona's config:

```yaml
sources:
  - kind: journal_worklog
    journal_root: <team-root>/journal/   # e.g. teams/Shohoku/journal/
    persona: Rukawa             # only ingest worklog entries stamped Rukawa
    # team: Shohoku             # optional; defaults to the journal root's parent dir name
  - kind: claude_code
    project_path: ~/.claude/projects/-home-tigerleap-projects-teams-shohoku/
    persona: Rukawa
```

The adapter discovers `*/worklog/*.md` under `journal_root` (both `active/`
and `done/`), reads the frontmatter, keeps only entries whose `persona`
matches, and groups them **per `(task, persona)`** — "Rukawa's memory of
task X" — with a stable `uuid5("journal:<team>/<task>/<persona>")` so the
extraction grows in place as the task does. Individual turn files remain the
drill-down detail.

**Double-count suppression.** A drive session's own (fat) transcript would
otherwise be folded whole into the driver's store, double-counting work the
worklog already captured. At `journal claim`, the drive's Slack `thread_ts`
is recorded to `journal/.drive-sessions.json`; the `claude_code`
(`ClaudeTranscriptAdapter`) source reads that registry — wired in
automatically when a `journal_worklog` source is present in the same config
— and **skips** any session whose `thread_ts` is a registered drive. The
registry reader is tolerant: a missing/corrupt registry suppresses
**nothing** (the safe direction).

**Roster prerequisite.** Every persona that does journal work needs its own
tiger-memory config + store listing this source; otherwise the team-sweep
has nothing to extract for it. The team-sweep enumerates **all** roster
personas that have a store (it does not activity-gate per persona), so a
specialist with *only* worklog activity and no Slack threads is still swept —
its new worklog entries surface at `tiger-memory plan` time through this
source. See [`tiger-memory-sweep-protocol.md`](tiger-memory-sweep-protocol.md).

## The auto_memory source (legacy)

One more source kind the config validator accepts is `auto_memory`. It
concatenates the `*.md` files under a `path:` directory into a single
synthetic record (Claude Code's auto-memory dir), extracted like any other
source. It is retained for backward compatibility; new setups should prefer
the explicit sources above.

```yaml
sources:
  - kind: auto_memory
    path: ~/.claude/projects/<slug>/memory/
```

## Adding a new summarizer vendor

Tiger-memory's summarizer is vendor-agnostic by design — it is the single
model touch point (the extraction judgement + meditation's
similarity/staleness/compaction judgements). The `anthropic` backend is
pre-registered; plug in any other vendor in three steps:

**1. Implement `Summarizer`** (see [`summarizers/base.py`](../src/tigerharness/tiger_memory/summarizers/base.py)):

```python
from tigerharness.tiger_memory.summarizers import Summarizer, SummarizerError

class OpenAISummarizer(Summarizer):
    name = "openai"
    version = "v1"

    def __init__(self, model: str, api_key: str):
        super().__init__()
        self.model = model
        self.api_key = api_key

    def summarize(self, *, prompt: str, max_words: int) -> str:
        # Call your vendor's API; return the markdown body.
        # Update self.cost_so_far from the API response if available.
        ...
```

**2. Register a factory:**

```python
from tigerharness.tiger_memory.summarizers import register_summarizer
from tigerharness.tiger_memory.config import SummarizerConfig

def _build_openai(cfg: SummarizerConfig) -> Summarizer:
    import os
    return OpenAISummarizer(model=cfg.model, api_key=os.environ["OPENAI_API_KEY"])

register_summarizer("openai", _build_openai)
```

Call `register_summarizer()` at import time — typically from your project's
top-level `__init__.py` or a startup hook. The registration must run before
`tiger-memory rebuild`/`ingest-staged` is invoked.

**3. Use it in any persona's config:**

```yaml
summarizer:
  backend: openai
  model: gpt-4.1
  prompts: default/v1
```

If the backend name isn't registered, you get a `SummarizerError` listing
every registered backend. If your factory returns something that isn't a
`Summarizer` subclass, the registry catches that at lookup time and raises
`SummarizerError` with the actual type name.

**Future: entry-point-based registration.** Python's
`[project.entry-points."tigerharness.summarizers"]` mechanism would let
plugins register without anyone explicitly importing them — on the roadmap
if demand picks up. The in-process call is sufficient for single-org use.

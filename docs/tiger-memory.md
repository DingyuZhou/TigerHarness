# tiger-memory

## At a glance
- **What:** per-persona persistent memory — archive / journal / briefing
  stores with lazy rebuild, decay, pinning, and drill-down; substring search
  by default, semantic via local fastembed or OpenAI embeddings per extras.
- **When you need it:** configuring or querying a persona's memory. For the
  team-wide refresh protocol see
  [tiger-memory-sweep-protocol.md](tiger-memory-sweep-protocol.md).
- **Must-not-miss:** semantic search needs the `[memory-rag]` / `[memory-rag-openai]`
  extra; without it, search is substring-only.

Persistent memory management for Claude Code agents: archive, journal,
briefing with lazy rebuild, kind-decay must-memorize, and drill-down.

## What it does

Automatically extracts, summarizes, and organizes an agent's
conversation history into a hierarchical memory store that the agent
reads at the start of each session. Older memories are compressed
through daily/weekly/monthly rollups. Critical facts are pinned in a
`must_memorize.md` table with decay-based scoring.

## Architecture

```
Sources (Claude transcripts, Slack threads, docs, journal worklogs)
    |
    v
Lifecycle (extract new sessions, summarize, rollup)
    |
    v
Store (journal/ + archive/ + briefing/)
    |-- journal/: short summaries (one per session)
    |-- archive/: detailed summaries
    |-- briefing/: the walking-window view the agent reads
    v
Agent reads briefing/ at session start
```

## Key modules

| Module | Purpose |
|---|---|
| `cli.py` | CLI. Writers: init, bootstrap, rebuild, pin, resummarize. Readers: drill, tree, raw, search, state. Stage-2 executor (subscription rail): plan (stages prompts + packs stacks), ingest-summary (one bundle via stdin), ingest-staged (single-process glue of all `.summary.md` cards). Team sweep gating: sweep-plan, sweep-done, sweep-complete, sweep-release |
| `config.py` | YAML config loading + validation |
| `lifecycle.py` | The core engine: extract, summarize, rollup, rebuild |
| `briefing.py` | Assemble the briefing/ directory from the store |
| `store.py` | Filesystem store operations (read/write/lock) |
| `must_memorize.py` | Pinned facts table with kind-based decay |
| `drill.py` | Drill-down navigation + search (grep/rag/hybrid) |
| `rag.py` | SQLite-vec backed RAG for semantic search |
| `embedders.py` | Embedder backends (fastembed local, OpenAI) |
| `sources/` | Source adapters (claude_code, slack_thread, docs, journal_worklog) |
| `summarizers/` | LLM summarization backends (anthropic, mock) |
| `frontmatter.py` | YAML frontmatter parsing for memory files |
| `state.py` | Rebuild state tracking (what's been processed) |

## Configuration

A YAML config file (pointed to by `$TIGER_MEMORY_CONFIG` or `--config`):

```yaml
agent:
  name: MyAgent
  role: A helpful assistant
  pronouns: they/them

store:
  root: ./memory/   # auto-appends agent slug

sources:
  - kind: claude_code
    project_path: ~/.claude/projects/-my-project/
    # Optional, for multi-persona Slack-bridge setups:
    # persona: ayako                    # only ingest sessions owned by this persona
    # include_unattributed: false        # opt-in to also include local claude-p sessions
  - kind: docs
    glob: docs/*.md

summarizer:
  # Pluggable -- "anthropic" is the default (via agent-sdk's claude_p).
  # See "Adding a new summarizer vendor" below for swapping in OpenAI etc.
  backend: anthropic
  model: claude-opus-4-7
  prompts: default/v1

budgets:
  short_summary_words: 400
  must_memorize_rows: 60

decay:
  preference: { days_per_point: 7 }
  decision: { days_per_point: 14 }
  incident: { days_per_point: 28 }
  # owner_explicit memories are always locked (never decay) -- not configurable.

rebuild:
  trigger: lazy
  idle_threshold_hours: 1
  resummarize_window_days: 7

briefing:
  walking:
    full_shorts_working_days: 2
    dailies_working_days: 7
    weeklies_working_days: 28
    monthlies_working_days: 90
```

## Usage

```bash
# Initialize the memory store
tiger-memory --config my-config.yaml init

# One-shot backfill of the full history (typical first run after init)
tiger-memory --config my-config.yaml bootstrap

# Rebuild briefing (incremental)
tiger-memory --config my-config.yaml rebuild

# Background rebuild (fire-and-forget)
tiger-memory --config my-config.yaml rebuild --background

# Search across all memory
tiger-memory --config my-config.yaml search "topic" --mode hybrid

# Drill down into a specific summary
tiger-memory --config my-config.yaml drill memory/sai/journal/20260515-daily-abc.md

# Pin a fact
tiger-memory --config my-config.yaml pin "Important decision" --kind decision
```

## Memory hierarchy

The store has exactly **three** directories: `journal/`, `archive/`, and
`briefing/`. Dailies, weeklies, and monthlies are **not** separate
directories -- they live inside `journal/`, distinguished only by
filename pattern:

```
journal/   shorts + rollups + must_memorize + longer_memory:
             YYYYMMDD-HHmmss-<UUID>.md   short summaries
             YYYYMMDD-daily-<UUID>.md    daily rollups
             YYYYMMDD-week-<UUID>.md     weekly rollups (Monday's date)
             YYYYMM-month-<UUID>.md      monthly rollups
             must_memorize.md, longer_memory.md
archive/   YYYYMMDD-HHmmss-<UUID>.md     detailed summaries (raw-transcript linked)
briefing/  the assembled view the agent reads at session start
```

## Search modes

| Mode | Engine | Best for |
|---|---|---|
| `auto` | hybrid if RAG available, else grep | Default |
| `hybrid` | Grep + RAG fused via RRF | Concept-level lookups |
| `rag` | Pure embedding similarity | Semantic matching |
| `grep` | ripgrep over journal+archive | Exact strings, regexes |

## RAG backends

- **fastembed** (default): BAAI/bge-small-en-v1.5 (384-dim), local ONNX, no API key
- **OpenAI**: text-embedding-3-small (1536-dim), requires `OPENAI_API_KEY`

Install with `pip install tigerharness[memory-rag]` or `[memory-rag-openai]`.
The index built from these is portable and shareable — see
[Portable, shared RAG index](#portable-shared-rag-index) below.

## Portable, shared RAG index

The vector index lives at `<store>/journal/.embeddings.db`. It is
**portable and meant to be shared**: archive paths are stored *relative*
to the store root, so a committed `.embeddings.db` keeps working after a
fresh clone on another machine with **no rebuild** — collaborators get
working RAG immediately. The index is deliberately **tracked in git** (not
gitignored); only the transient `memories/*/.sweep-staging/` stays ignored.

Zero-rebuild reuse requires the **same embedder** on both ends. The index
is keyed to one embedder (its name + vector dim).

**Which embedder will my machine use?** `pick_embedder` picks OpenAI iff
`OPENAI_API_KEY` is set and `openai` is installed, otherwise the default
fastembed (`BAAI/bge-small-en-v1.5`). So a committed index built with the
default reuses immediately on any default setup; a machine configured for
OpenAI (different dim) takes one self-healing rebuild. Either way it
"just works" — the only difference is whether the first search reuses or
rebuilds.

Two cases trigger a one-time automatic rebuild instead of reuse, each
logged at WARNING so the cost is visible:

- **Different embedder dim** (e.g. opening a fastembed-built index with
  OpenAI): the fixed-width vector table is dropped and rebuilt at the new
  dim and re-embedded — safely, never a dimension-mismatch crash.
- **A legacy absolute-path index** (built before this became portable): it
  is detected and rebuilt once so its paths become relative/portable.

**Rebuild.** The index (re)builds **incrementally on the next
`tiger-memory search --mode rag`/`hybrid`** — there is no separate
RAG-rebuild command. (`tiger-memory rebuild` is the session-start
rollup/briefing rebuild; it does *not* touch the embeddings index.) To
force a full rebuild — e.g. after a merge conflict on the binary file —
delete `<store>/journal/.embeddings.db` and run a search. A full rebuild of
a few hundred archive entries is ~30s with the model cached.

**Verifying.** A rebuild prints the WARNING `rag index rebuilt ...` on
stderr (default-visible) — so its **absence** on the first search after a
clone confirms the committed index was reused as-is. Confirm the index is
actually shared with `git ls-files | grep embeddings.db` (it only helps
collaborators once the Operator has committed it).

**Limitation — not mergeable.** The binary sqlite index is not a
text-mergeable artifact: two machines that both re-embed and commit will
conflict at the file level. Sharing is "rebuild on conflict" (re-run a
search to regenerate), not a three-way merge.

## Per-persona filtering (multi-bridge integration)

When the slack-bridge runs in **multi-persona mode** (one Slack app
routes DMs to N team members), each persona has its own memory store
and only wants to summarize **its own** conversations -- Ayako's memory
shouldn't include Sakuragi's threads.

Tell the source adapter which persona owns the conversation by adding
a `persona:` field to the `claude_code` source:

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
`threads.json` as users DM the bot. The adapter reads that mapping and
only emits records where the stored persona matches.

**Three sessions, three outcomes:**

| Session origin | `threads.json` entry | Strict mode (default) | `include_unattributed: true` |
|---|---|---|---|
| Slack DM addressed to Ayako | `{session_id, persona: "ayako"}` | ✅ in Ayako's memory | ✅ in Ayako's memory |
| Slack DM addressed to Sakuragi | `{session_id, persona: "sakuragi"}` | ❌ excluded | ❌ excluded |
| Local `claude -p` (not via bridge) | not in threads.json | ❌ excluded | ✅ in Ayako's memory |
| Pre-routing Slack thread (PR1-era) | `"session_id"` bare string | ❌ excluded | ✅ in Ayako's memory |

**Backward compat:** if `persona:` is omitted, the adapter emits every
session (legacy single-tenant behavior, unchanged). The threads.json
reader also accepts the pre-routing schema (bare `"session_id"`
strings) so older state files still work.

**`persona=None` semantics.** Both "not in `threads.json` at all"
(local `claude -p` session, never went through the bridge) and
"present but with `persona=None`" (pre-routing entry, from before the
multi-persona bridge existed) are treated as *unattributed*. Under
strict mode (default) they're excluded from every persona's memory.
Flipping `include_unattributed: true` brings them in. There's no way
to disambiguate "local work" from "stale pre-routing thread" via
config — if you need that distinction, manually edit `threads.json`
to migrate old entries to the new schema, or wait for them to age out
of your memory window.

## Per-persona journal memory (journal_worklog source)

When the team runs work through the subscription backend's
`drive-journal` driver, all of it happens inside **one** Claude session
(the driver's). The driver adopts each persona in-session, so the raw
transcript would collapse every persona's work into the driver's store.
The `journal_worklog` source fixes that by ingesting the journal's
**per-turn worklog records** instead — each one stamped (by harness
code) with the persona that actually did the work. See
[`per-persona-journal-memory.md`](per-persona-journal-memory.md) and
[`subscription-backend.md`](subscription-backend.md) ("Per-persona
memory") for the write side.

Add the source to each journal-working persona's config:

```yaml
sources:
  - kind: journal_worklog
    journal_root: <team-root>/journal/   # e.g. teams/Shohoku/journal/
    persona: Rukawa             # only ingest worklog entries stamped Rukawa
    # team: Shohoku             # optional; defaults to the journal root's
    #                           # parent dir name (set it for non-standard layouts)
  - kind: claude_code
    project_path: ~/.claude/projects/-home-tigerleap-projects-teams-shohoku/
    persona: Rukawa
```

The adapter discovers `*/worklog/*.md` under `journal_root` (both
`active/` and `done/`), reads the frontmatter, keeps only entries whose
`persona` matches, and groups them **per `(task, persona)`** — "Rukawa's
memory of task X" — with a stable `uuid5("journal:<team>/<task>/<persona>")`
so the summary grows in place as the task does (the existing
addendum/re-summarize path handles growth). Individual turn files remain
the drill-down detail.

**Double-count suppression.** A drive session's own (fat) transcript
would otherwise be folded whole into the driver's store, double-counting
work the worklog already captured. At `journal claim`, the drive's Slack
`thread_ts` is recorded to `journal/.drive-sessions.json`; the
`claude_code` (`ClaudeTranscriptAdapter`) source reads that registry —
wired in automatically when a `journal_worklog` source is present in the
same config — and **skips** any session whose `thread_ts` is a
registered drive. The registry reader is tolerant: a missing or corrupt
registry suppresses **nothing** (the safe direction — worst case is a
double-counted driver transcript, never lost or mis-attributed persona
memory).

**Roster prerequisite.** Every persona that does journal work needs its
own tiger-memory config + store listing this source; otherwise the
team-sweep has nothing to summarize for it. The team-sweep itself
enumerates **all** roster personas that have a store (it does not
activity-gate per persona), so a specialist with *only* worklog activity
and no Slack threads is still swept — its new worklog entries surface at
`tiger-memory plan` time through this source. See
[`tiger-memory-sweep-protocol.md`](tiger-memory-sweep-protocol.md).

## Multi-operator stores (shared summaries, local briefings)

Since T12b a memory store supports MANY writers sharing one set of
summaries (the open-sourced-team scenario: summaries are
git-committed; each machine rebuilds its own briefing).

- **Operator identity**: an 8-hex slug generated once per store and
  persisted in the machine-local `journal/.state.json` — never
  derived from mutable facts. Two laptops = two operators, honestly.
- **Attribution**: every written summary carries an `operator`
  frontmatter key, and the deterministic rollup uuids are seeded
  `kind:period:operator` — re-rolls overwrite only your own file, and
  two operators' same-period files can never collide (git pushes on
  memory files are conflict-free by construction).
- **Fold locality** (the invariant): a cascade folds ONLY the local
  operator's files (plus segment-less *legacy* files iff this store
  predates the upgrade — detected by a pre-existing state file, never
  by mere file presence, which a fresh clone would false-positive
  on). Foreign files are read-only inputs to the briefing, never fold
  targets.
- **Read order**: N same-period files read deterministically — the
  legacy file first, then by filename — so last-mention-wins behaves
  identically on every machine. The briefing's daily layer copies ALL
  of a date's rollups.
- **Migration**: nothing changes for single-operator stores; a
  pre-upgrade store keeps re-rolling its legacy filenames
  (`adopt_legacy`), so old and new naming never duplicate a period.
- **Share boundary**: track `journal/*.md` + `archive/`; keep
  `briefing/`, `cache/`, and `journal/.state.json` machine-local
  (the state file holds the operator id). Public team templates
  should make sharing OPT-IN (privacy: shared summaries are public
  the moment the repo is).

## The auto_memory source (legacy)

One more source kind the config validator accepts is `auto_memory`. It
concatenates the `*.md` files under a `path:` directory into a single
synthetic record (Claude Code's auto-memory dir), summarized like any
other source. It has no adapter under `sources/` — it's assembled in the
lifecycle bootstrap — and is retained for backward compatibility; new
setups should prefer the explicit sources above.

```yaml
sources:
  - kind: auto_memory
    path: ~/.claude/projects/<slug>/memory/
```

## Adding a new summarizer vendor

Tiger-memory's summarizer is vendor-agnostic by design. The
`anthropic` backend is pre-registered; plug in any other vendor in
three steps:

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
        # Call your vendor's API; return markdown body
        # Update self.cost_so_far from the API response if available
        ...
```

**2. Register a factory:**

```python
from tigerharness.tiger_memory.summarizers import register_summarizer
from tigerharness.tiger_memory.config import SummarizerConfig

def _build_openai(cfg: SummarizerConfig) -> Summarizer:
    import os
    return OpenAISummarizer(
        model=cfg.model,
        api_key=os.environ["OPENAI_API_KEY"],
    )

register_summarizer("openai", _build_openai)
```

Call `register_summarizer()` at import time -- typically from your
project's top-level `__init__.py` or a startup hook. The registration
must run before `tiger-memory rebuild` is invoked.

**3. Use it in any persona's config:**

```yaml
summarizer:
  backend: openai
  model: gpt-4.1
  prompts: default/v1
```

Tiger-memory looks up `openai` in the registry and calls your factory.
If the backend name isn't registered, you get a `SummarizerError` with
a list of every registered backend so it's clear what to install or
register.

If your factory returns something that isn't a `Summarizer` subclass
(e.g. a typo in a refactor), the registry catches that at lookup time
and raises `SummarizerError` with the actual type name — much friendlier
than the confusing `AttributeError` you'd otherwise get later when
something calls `.summarize()` on the wrong object.

**Future: entry-point-based registration.** Today downstream packages
call `register_summarizer()` at import time, which means SOMEONE has
to import the package before tiger-memory rebuilds. Python's
`[project.entry-points."tigerharness.summarizers"]` mechanism would
let plugins register without anyone explicitly importing them — useful
if you publish a `tigerharness-openai-summarizer` package on PyPI and
want it to "just work" after `pip install`. The in-process call is
sufficient for single-org use; entry points are on the roadmap if
demand picks up.

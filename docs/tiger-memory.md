# tiger-memory

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
Sources (Claude transcripts, Slack threads, docs)
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
| `cli.py` | CLI: init, rebuild, search, drill, tree, raw, pin |
| `config.py` | YAML config loading + validation |
| `lifecycle.py` | The core engine: extract, summarize, rollup, rebuild |
| `briefing.py` | Assemble the briefing/ directory from the store |
| `store.py` | Filesystem store operations (read/write/lock) |
| `must_memorize.py` | Pinned facts table with kind-based decay |
| `drill.py` | Drill-down navigation + search (grep/rag/hybrid) |
| `rag.py` | SQLite-vec backed RAG for semantic search |
| `embedders.py` | Embedder backends (fastembed local, OpenAI) |
| `sources/` | Source adapters (claude_code, slack_thread, docs) |
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
  owner_explicit: locked

rebuild:
  trigger: lazy
  idle_threshold_hours: 2
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

```
monthly/   YYYYMM-month-<UUID>.md
weekly/    YYYYMMDD-week-<UUID>.md
daily/     YYYYMMDD-daily-<UUID>.md
journal/   YYYYMMDD-HHmmss-<UUID>.md  (short summaries)
archive/   YYYYMMDD-HHmmss-<UUID>.md  (detailed summaries)
briefing/  The assembled view the agent reads
```

## Search modes

| Mode | Engine | Best for |
|---|---|---|
| `auto` | hybrid if RAG available, else grep | Default |
| `hybrid` | Grep + RAG fused via RRF | Concept-level lookups |
| `rag` | Pure embedding similarity | Semantic matching |
| `grep` | ripgrep over journal+archive | Exact strings, regexes |

## RAG backends

- **fastembed** (default): BAAI/bge-small-en-v1.5, local ONNX, no API key
- **OpenAI**: text-embedding-3-small, requires `OPENAI_API_KEY`

Install with `pip install tigerharness[memory-rag]` or `[memory-rag-openai]`.

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

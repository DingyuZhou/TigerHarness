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
  - kind: docs
    glob: docs/*.md

summarizer:
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

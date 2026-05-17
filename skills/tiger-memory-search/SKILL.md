---
name: tiger-memory-search
description: When you need to find any conversation that touched a specific topic -- across all of tiger-memory, not just the briefing window. Defaults to hybrid (semantic + lexical, fused via RRF); fall back to grep when you need exact-string matching.
---

# tiger-memory-search

The briefing only carries a recent walking window. For anything beyond
that, search the full journal/ + archive/ store.

## CLI

```bash
tiger-memory search "<topic>" [--mode auto|hybrid|rag|grep]
```

| Mode | What it does | When to use |
|---|---|---|
| `auto` (default) | hybrid if RAG available, else grep | Default choice |
| `hybrid` | Grep + RAG fused via RRF | Best for concept-level lookups |
| `rag` | Pure semantic search | Concept matching without exact words |
| `grep` | Plain ripgrep, recency-ranked | Exact strings, function names |

## Workflow

1. Run the search.
2. Use `tiger-memory drill <path>` on the most relevant result.
3. Follow the drill-down chain to the depth you need.

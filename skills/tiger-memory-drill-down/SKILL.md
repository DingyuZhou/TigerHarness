---
name: tiger-memory-drill-down
description: When the briefing's short summaries aren't detailed enough, walk from a short summary down to its detailed archive -- or from a daily/weekly/monthly rollup down to its constituent conversations.
---

# tiger-memory-drill-down

The tiger-memory store has a strictly filename-driven hierarchy:

```
monthly  YYYYMM-month-<UUID>.md
   |
weekly   YYYYMMDD-week-<UUID>.md     (Monday's date)
   |
daily    YYYYMMDD-daily-<UUID>.md
   |
short    YYYYMMDD-HHmmss-<UUID>.md
   |
archive  YYYYMMDD-HHmmss-<UUID>.md   (same filename in archive/)
   |
raw      JSONL transcript or Slack thread
```

## CLI

```bash
python -m tigerharness.tiger_memory drill <path>
python -m tigerharness.tiger_memory tree <path>
python -m tigerharness.tiger_memory raw <archive_path>
```

Or if installed as a script:

```bash
tiger-memory drill <path>
tiger-memory tree <path>
tiger-memory raw <archive_path>
```

## Workflow

1. Start at the briefing layer covering the right time period.
2. `drill` to see immediate children with one-line previews.
3. Pick the most relevant child and `drill` again.
4. At a short summary, check it carefully. If not enough, open the archive.

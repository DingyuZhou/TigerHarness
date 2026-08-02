# Compaction — the team event log ({kind} {period})

You are compacting the team-wide event log (who did what, by date). The
dated sections below have aged out of the raw daily window and must be
folded into ONE concise section for the whole {kind} **{period}**.

Current content: {current_chars} characters. Target: **at most
{max_chars} characters** total.

## Sections to fold

{content}

## What to do

- Keep attribution: every line stays a `- <Name> <did thing>.` bullet.
- Merge repeats and near-repeats per person (`(x3)` count suffixes and
  re-occurring work collapse into one line — "shipped 4 sweep fixes").
- Keep what a teammate would need to know happened: ships, decisions,
  reviews, migrations, incidents. Drop minutiae and process noise.
- Keep chronological reading order (earliest work first is fine); do
  NOT keep the per-day headings — the fold is one flat bullet list.

## Output contract — STRICT

Emit exactly this marker on its own line, then ONLY the folded bullet
lines (every non-blank line must start with `- `). There is NO `NONE`
option — a fold always carries content:

```
@@TEAM_EVENTS@@
- <Name> <did thing>.
- <Name> <did thing>.
```

No other preamble or commentary.

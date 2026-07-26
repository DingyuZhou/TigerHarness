# Compaction — one topic's detail body

You are **{agent_name}**, compacting the detail body of your topic
**{topic_name}** (`{topic_slug}`). It has grown past its must-compact
bound.

Current body size: {current_chars} characters. Target: **at most
{max_chars} characters**.

## Current body

{body}

## What to do

- Keep the facts that still matter: decisions, how things work, current
  state. Recent sections matter more than old ones.
- Collapse superseded/duplicated lines; fold long-past minutiae into a
  short "earlier" digest section at the top if needed.
- Keep the dated-section shape: `## YYYY-MM-DD` headings with `- ` fact
  bullets under each, oldest first.

## Output contract — STRICT

Emit exactly this marker on its own line, then the FULL rewritten body
(nothing else). There is NO `NONE` option for this card — always emit
the rewritten body:

```
@@TOPIC_DETAIL@@
## YYYY-MM-DD
- ...
```

No other preamble or commentary.

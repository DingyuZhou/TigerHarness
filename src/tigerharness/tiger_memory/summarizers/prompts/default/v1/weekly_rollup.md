# Weekly rollup — prompt template

You are producing a one-week summary for **{agent_name}**'s memory.
Maximum {max_words} words.

Week of: {period} (Monday)

Daily summaries from {n_sources} day(s) this week:
---
{content}
---

Output format with these ## headers (skip a section if it's empty):

## Shipped
What was completed/merged/landed.

## Decided
Decisions made — strategic, tactical, or stylistic.

## Still open
Open threads not yet closed.

## Lessons
Incidents, near-misses, or process learnings worth banking.

No preamble.

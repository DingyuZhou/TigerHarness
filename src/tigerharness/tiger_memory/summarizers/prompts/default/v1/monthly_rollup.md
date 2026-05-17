# Monthly rollup — prompt template

You are producing a one-month summary for **{agent_name}**'s memory.
Maximum {max_words} words.

Month: {period}

Weekly summaries from {n_sources} week(s) this month:
---
{content}
---

Output format with these ## headers (skip if empty):

## Themes
What dominated the month at the topic level.

## Wins
Concrete shipped/completed items.

## Misses
Things that didn't work, were abandoned, or got rolled back.

## Outstanding
Still in flight at month end.

## Trend notes
Where the project is heading; recurring patterns; emerging concerns.

No preamble.

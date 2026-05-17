# Longer-memory refresh — prompt template

You are folding a month's worth of context into the *longer-memory*
file for **{agent_name}**. Maximum {max_words} words for the result.

Previous longer-memory (covers through {previous_covers_until}):
---
{previous_longer_memory}
---

New monthly to fold in ({new_month}):
---
{new_monthly}
---

Produce an updated longer-memory that:
- Preserves load-bearing decisions and incidents from prior periods.
- Integrates the new month's themes/wins/misses/outstanding.
- Stays within the word cap by compressing older material when needed.
- Maintains rough chronological flow (oldest → newest) so a reader
  understands when things happened.

Output structure: free-form prose with optional `## Year YYYY` headers
or `## YYYY-MM` headers if it helps continuity. No preamble.

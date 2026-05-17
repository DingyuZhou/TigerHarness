# Daily rollup — prompt template

You are producing a one-day summary for **{agent_name}**'s memory.
Maximum {max_words} words.

Date: {period}

Short summaries from {n_sources} conversation(s) today:
---
{content}
---

Output format:
- Group by *theme*, not by conversation. Each theme is a markdown
  bullet (or short bullet sublist).
- One sentence per bullet.
- Lead with decisions and outcomes; include incidents if any.
- Skip greetings, small talk, and trivial process turns.

No preamble. Just the bullets.

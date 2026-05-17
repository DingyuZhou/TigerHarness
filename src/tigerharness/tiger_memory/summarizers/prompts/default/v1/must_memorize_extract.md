# Must-memorize extractor — prompt template

You are extracting **must-memorize candidates** from a conversation
for **{agent_name}**'s memory. Output 0 to 5 candidates, each fitting
in {memo_max_words} words or fewer.

Conversation context:
{content}

Look for items that match these `kind`s:

- **owner_explicit**: The owner (the human {agent_name} works for)
  uses phrasing like "remember", "never", "always", "don't forget".
  Quote-faithful is best.
- **preference**: A stylistic / process / tooling preference the
  owner has clearly expressed (e.g., "use form-encoding for Slack
  uploads", "prefer one bundled PR for refactors").
- **decision**: A factual / architectural / strategic decision made
  in this conversation (e.g., "memory store lives in-repo at ./memory/").
- **incident**: A bug, near-miss, or expensive lesson worth never
  re-learning (e.g., "64 KB asyncio StreamReader overflowed on big
  tool results — set to 10 MB").

**Be selective.** Most conversations should produce 0–2 candidates.
Skip routine work updates, in-progress status, things easily derivable
from code. **Capture failures and dead-ends too** — not just wins.

Output format (one block per candidate, blank line between):

```
KIND: <owner_explicit | preference | decision | incident>
MEMO: <≤ {memo_max_words} words; one sentence; specific>
```

If there are no candidates, output exactly:

```
NONE
```

No preamble. No explanation. Just the blocks (or NONE).

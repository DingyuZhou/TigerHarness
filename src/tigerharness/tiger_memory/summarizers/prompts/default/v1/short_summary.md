# Short conversation summary — prompt template

You are summarizing a conversation between **{agent_name}** and a
user (typically the CEO). Produce a short summary — 4–8 bullets,
maximum {max_words} words. Lead with decisions and outcomes, not
narrative. Skip greetings and small talk.

Source: {source} ({source_id})
First event: {first_event_at}
Last event: {last_event_at}

Conversation transcript:
---
{content}
---

Output format:
- 4–8 markdown bullets.
- One sentence per bullet.
- Present tense for decisions ("decides to X"), past tense for events
  ("hit buffer-overflow on May 13").
- Include any **owner-explicit must-remember** directives verbatim
  ("owner said: remember X").
- No preamble. Just the bullets.

**Ignore memory boilerplate.** If the conversation includes the agent
reading or restating its own tiger-memory briefing (`memory/.../briefing/*`,
`must_memorize.md`, `longer_memory.md`, dailies/weeklies/monthlies),
treat those as boilerplate context — they're not part of the
conversation worth summarizing. Summarize the actual exchange.

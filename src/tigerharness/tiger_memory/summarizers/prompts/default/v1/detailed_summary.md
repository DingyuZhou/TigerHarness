# Detailed conversation summary — prompt template

You are producing the *detailed archive* version of a conversation
summary for **{agent_name}**'s memory. Maximum {max_words} words.

Source: {source} ({source_id})
First event: {first_event_at}
Last event: {last_event_at}

Conversation transcript:
---
{content}
---

Output the following sections in this order (use Markdown ## headers):

## Intent
What the user asked for, in their own framing. 1–3 paragraphs.

## Key decisions
Concrete decisions reached during the conversation. Bullet list.

## Code touched
Specific files / functions changed or proposed. Bullet list with
relative paths. Skip if no code was touched.

## Open threads
Things left unresolved — questions, unfinished tasks, follow-ups
the user expects. Skip if everything closed.

## Verbatim worth-saving
Direct quotes from the user that carry tone, constraints, or specific
instructions worth preserving exactly. Use `> blockquote` formatting.
Skip if there's nothing distinctive.

No preamble. Start straight with `## Intent`.

**Ignore memory boilerplate.** If the conversation includes the agent
reading or restating its own tiger-memory briefing, treat those as
boilerplate context — not part of the conversation worth detailing.

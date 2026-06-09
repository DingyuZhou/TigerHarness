# Combined session summary — prompt template (collapsed pass)

You are summarizing a conversation between **{agent_name}** and a user
(typically the Operator), producing ALL THREE memory artifacts in a
single response: a short summary, a detailed archive, and must-memorize
candidates. Read the transcript ONCE and emit every section.

Source: {source} ({source_id})
First event: {first_event_at}
Last event: {last_event_at}

Conversation transcript:
---
{content}
---

**Large transcripts:** this transcript may be long and span more than one
read window. Read it in FULL before summarizing — page through to the end
rather than summarizing only the opening. Never silently skip the middle:
if it is too large to hold at once, condense it in sections and combine
those into one faithful summary. No important decision, file, number, or
owner-explicit directive should be lost to length.

**Ignore memory boilerplate.** If the conversation includes the agent
reading or restating its own tiger-memory briefing
(`memory/.../briefing/*`, `must_memorize.md`, `longer_memory.md`,
dailies/weeklies/monthlies), treat those as boilerplate context — not
part of the conversation worth summarizing. Summarize the actual exchange.

## Output contract — STRICT

Emit exactly the three markers below, each on its own line, in this
order, with the section content underneath. Do not add any other text,
preamble, or trailing commentary.

```
@@SHORT@@
<short summary>
@@DETAILED@@
<detailed archive>
@@MUST_MEMORIZE@@
<must-memorize candidates, or NONE>
```

### @@SHORT@@ section — at most {short_max_words} words
- 4–8 markdown bullets, one sentence each. Lead with decisions and
  outcomes, not narrative. Skip greetings and small talk.
- Present tense for decisions ("decides to X"), past tense for events
  ("hit buffer-overflow on May 13").
- Include any **owner-explicit must-remember** directives verbatim.

### @@DETAILED@@ section — at most {detailed_max_words} words
Use Markdown `##` headers in this order (skip a section if empty):

## Intent
What the user asked for, in their own framing. 1–3 paragraphs.

## Key decisions
Concrete decisions reached. Bullet list.

## Code touched
Specific files / functions changed or proposed, with relative paths.

## Open threads
Unresolved questions, unfinished tasks, follow-ups the user expects.

## Verbatim worth-saving
Direct user quotes carrying tone, constraints, or specific instructions,
in `> blockquote` form.

### @@MUST_MEMORIZE@@ section — 0 to 5 candidates, each ≤ {memo_max_words} words
Look for `owner_explicit` ("remember"/"never"/"always"/"don't forget",
quote-faithful), `preference` (stylistic/process/tooling), `decision`
(factual/architectural/strategic), or `incident` (a bug / expensive
lesson). Be selective — most conversations yield 0–2. Capture
failures and dead-ends too. One block per candidate, blank line between:

```
KIND: <owner_explicit | preference | decision | incident>
MEMO: <≤ {memo_max_words} words; one sentence; specific>
```

If there are no candidates, write exactly `NONE` under the
`@@MUST_MEMORIZE@@` marker.

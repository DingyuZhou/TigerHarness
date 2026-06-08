# Transcript chunk condensation — prompt template

You are condensing ONE part of a long conversation between **{agent_name}**
and a user (typically the Operator). This is part {chunk_index} of
{chunk_total}. The full transcript was too large for a single pass, so it
was split into parts; each part is condensed here, and the condensed parts
are then summarized together. Your job is THIS part only.

Produce a faithful, information-dense digest of this part. **Preserve**
concrete decisions, file paths, function names, numbers, error messages,
owner-explicit directives (quote them), and any open threads or follow-ups.
**Drop** greetings, small talk, and repeated boilerplate. Do not add a
preamble, do not editorialize, do not invent anything not in the text.

This is an intermediate artifact that will be combined with the other parts
and summarized again downstream — so prioritize completeness of facts over
polished prose.

Conversation part {chunk_index}/{chunk_total}:
---
{content}
---

Write at most {max_words} words. Markdown bullets preferred. No preamble,
no trailing commentary — emit only the digest.

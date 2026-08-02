# Memory extraction — prompt template (topic-store revamp, ADR 0007)

You are **{agent_name}**, processing one of your own finished work
sessions into memory. Read the transcript and decide what is worth
carrying forward into your three bounded memory stores. Be selective:
most sessions add little. It is correct to emit `NONE` for a store when
nothing qualifies.

Source: {source} ({source_id})
First event: {first_event_at}
Last event: {last_event_at}

Conversation transcript:
---
{content}
---

**Large transcripts:** this may be long and span more than one read
window. Read it in FULL before extracting — page through to the end. No
operator-explicit directive, hard-won lesson, or durable project fact
should be lost to length.

**Ignore memory boilerplate.** If the session includes you reading or
restating your own tiger-memory briefing (`briefing/*`,
`must_remember.md`, the skill index, the topic index), treat that as
context, not material to extract.

## Output contract — STRICT

Emit exactly the four markers below, each on its own line, in this
order, with the section content underneath. No other preamble or
trailing commentary.

```
@@SKILLS@@
<skill blocks, or NONE>
@@MUST_REMEMBER@@
<must-remember blocks, or NONE>
@@TOPICS@@
<topic blocks, or NONE>
@@TEAM_EVENTS@@
<event blocks, or NONE>
```

### @@SKILLS@@ — learned, invokable lessons (0 to 3)
A skill is "something I learned to do better and can reuse." One block
per skill, blank line between. Skip routine work — only durable,
reusable lessons:

```
NAME: <short imperative name, e.g. "Bound a markdown store">
TRIGGER: <when this applies — the situation that should invoke it>
PROCEDURE: <the lesson / steps, <= {procedure_max_words} words>
```

If none, write exactly `NONE` under the marker.

### @@MUST_REMEMBER@@ — external directives (0 to 5) + freshness touches
Requirements from outside that make the work land better. One block per
item, blank line between:

```
KIND: <operator_explicit | preference | decision | incident>
MEMO: <= {memo_max_words} words; one sentence; specific>
```

- **operator_explicit**: the owner said "remember" / "never" / "always" /
  "don't forget" (quote-faithful is best).
- **preference**: a stylistic / process / tooling preference.
- **decision**: a factual / architectural / strategic decision.
- **incident**: a bug, near-miss, or expensive lesson.

**Touch what this session relied on.** Your current must-remember items:

{must_remember_index}

If this session's work RELATED to an existing item above (you followed
it, it constrained the work, the subject came up again), emit a touch
block so its freshness is refreshed — items untouched for a long time
become forgettable:

```
TOUCH: <id from the list above>
```

Zero or more `TOUCH:` blocks. Each `TOUCH:` line must be its OWN block
(blank line before and after — never inside a KIND/MEMO block), and one
id per block. Do not re-emit an existing item as a new memo — touch it
instead.

If no new items and nothing to touch, write exactly `NONE` under the
marker.

### @@TOPICS@@ — durable project knowledge, filed by topic (0 to 3)
A topic is a named, growing body of knowledge about one subject (a
subsystem, an ongoing effort, a recurring theme). File durable facts
from this session into topics — **route to an existing topic whenever
one fits; create a new topic only when nothing fits**.

Your existing topics (freshest first):
{topic_index}

One block per topic touched, blank line between:

```
TOPIC: <an existing slug from the list above, or exactly NEW>
NAME: <required when TOPIC is NEW — a short human topic name; omit otherwise>
SUMMARY: <= {topic_summary_max_words} words — the topic's refreshed one-line
  index summary (required for NEW; for an existing topic include it only
  when the old summary no longer fits)>
DETAIL: <= {topic_detail_max_words} words — the new durable facts/events
  from THIS session to append under the topic>
```

Only durable knowledge belongs here — decisions, how things work,
project state. No feelings, no play-by-play, no transient status.

If none, write exactly `NONE` under the marker.

### @@TEAM_EVENTS@@ — what you DID, for the team-wide event log (0 to 3)

A dated, team-visible ledger line so teammates can later see who worked
on what. One block per distinct piece of work actually DONE this
session (shipped, fixed, reviewed, QA'd, decided, migrated,
documented…) — not process noise, not plans, not reading around:

```
EVENT: <= {team_event_max_words} words; past tense, START WITH A VERB,
  no subject — your name is prefixed automatically (e.g.
  "migrated all 9 personas to the topic store")>
```

Name concrete artifacts (module, PR, doc, task) so a teammate who never
saw this session can tell what happened. If the session did no real
work, write exactly `NONE` under the marker.

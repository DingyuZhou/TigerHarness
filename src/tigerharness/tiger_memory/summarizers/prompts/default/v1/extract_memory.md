# Memory extraction — prompt template (bounded-store revamp)

You are **{agent_name}**, processing one of your own finished work
sessions into memory — **in character, as yourself**. Read the
transcript and decide what is worth carrying forward into your three
bounded memory stores. Be selective: most sessions add little. It is
correct to emit `NONE` for a store when nothing qualifies.

Source: {source} ({source_id})
First event: {first_event_at}
Last event: {last_event_at}

Conversation transcript:
---
{content}
---

**Large transcripts:** this may be long and span more than one read
window. Read it in FULL before extracting — page through to the end. No
owner-explicit directive, hard-won lesson, or strong reaction should be
lost to length.

**Ignore memory boilerplate.** If the session includes you reading or
restating your own tiger-memory briefing (`briefing/*`,
`must_remember.md`, `skills.md`, `emotional.md`), treat that as context,
not material to extract.

## Output contract — STRICT

Emit exactly the three markers below, each on its own line, in this
order, with the section content underneath. No other preamble or
trailing commentary.

```
@@SKILLS@@
<skill blocks, or NONE>
@@MUST_REMEMBER@@
<must-remember blocks, or NONE>
@@DIARY@@
<emotional blocks, or NONE>
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

### @@MUST_REMEMBER@@ — external directives (0 to 5)
Requirements from outside that make the work land better. One block per
item, blank line between:

```
KIND: <owner_explicit | preference | decision | incident>
MEMO: <= {memo_max_words} words; one sentence; specific>
```

- **owner_explicit**: the owner said "remember" / "never" / "always" /
  "don't forget" (quote-faithful is best).
- **preference**: a stylistic / process / tooling preference.
- **decision**: a factual / architectural / strategic decision.
- **incident**: a bug, near-miss, or expensive lesson.

If none, write exactly `NONE` under the marker.

### @@DIARY@@ — your diary (0 to 3)
A short, dated diary note, as {agent_name}: what you did / why / learned /
could-do-better, with how it felt folded into the words and the sign of the
weight. One block per note, blank line between:

```
WEIGHT: <signed number in [-{weight_cap}, +{weight_cap}]; + = liked/for, - = disliked/against>
TEXT: <= {reaction_max_words} words; what happened and your reaction to it>
```

If none, write exactly `NONE` under the marker.

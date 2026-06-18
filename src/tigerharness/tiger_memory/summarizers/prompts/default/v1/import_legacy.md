# Legacy memory re-author — prompt template (one-off import)

You are **{agent_name}**, reading one of **your own** aged-out memory
rollups and re-authoring it into your three new bounded memory stores —
**in character, as yourself**. This is the one-off migration from the
old chronological rollup memory to the new shapes: read this rollup and
decide what durable skill, directive, or reaction is worth carrying
forward. Be selective — a rollup is a summary, so most of it is already
context; only the load-bearing lessons survive. It is correct to emit
`NONE` for a store when nothing qualifies.

This is a **{rollup_kind}** rollup covering **{period}**.

Rollup body:
---
{content}
---

**Re-authoring, not transcribing.** You are not copying the rollup —
you are distilling it into reusable memory. A rollup line that merely
narrates what happened is context; a line that taught you *how to do
something better*, recorded an *external directive*, or *moved you* is
memory. Collapse near-duplicates; keep the sharpest phrasing.

## Output contract — STRICT

Emit exactly the three markers below, each on its own line, in this
order, with the section content underneath. No other preamble or
trailing commentary.

```
@@SKILLS@@
<skill blocks, or NONE>
@@MUST_REMEMBER@@
<must-remember blocks, or NONE>
@@EMOTIONAL@@
<emotional blocks, or NONE>
```

### @@SKILLS@@ — learned, invokable lessons (0 to 3)
A skill is "something I learned to do better and can reuse." One block
per skill, blank line between. Skip routine narration — only durable,
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

### @@EMOTIONAL@@ — your reactions (0 to 3)
How this period felt to you, as {agent_name}. One block per reaction,
blank line between:

```
WEIGHT: <signed number in [-{weight_cap}, +{weight_cap}]; + = liked/for, - = disliked/against>
REACTION: <a few words naming the feeling>
TEXT: <= {reaction_max_words} words; what happened and your reaction to it>
```

If none, write exactly `NONE` under the marker.

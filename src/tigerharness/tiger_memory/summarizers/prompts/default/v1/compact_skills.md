# Compaction — skill roster

You are **{agent_name}**, compacting your own skill roster. Its index (the
name + trigger listing loaded every session) has grown past its
must-compact bound.

Current index size: {current_chars} characters. Target: **an index of at
most {max_chars} characters** — roughly, keep/merge down to the skills
that still earn a line.

## Your skills (importance-ranked, most important first)

{entries}

## What to do

- **Merge** near-duplicate skills into one (one NAME/TRIGGER/PROCEDURE
  covering both; procedures may combine).
- **Drop** skills that stopped earning their line: lowest importance,
  longest unused, superseded by a better skill.
- **Tighten** names and triggers — the index renders one line per skill
  from NAME + TRIGGER, so shorter is smaller.
- Keep every skill's PROCEDURE complete enough to act on (<=
  {procedure_max_words} words each).

## Output contract — STRICT

Emit exactly this marker on its own line, then the FULL replacement
roster (every skill you keep, merged ones included):

```
@@SKILLS@@
NAME: <short imperative name>
TRIGGER: <when this applies>
PROCEDURE: <the lesson / steps>

NAME: ...
TRIGGER: ...
PROCEDURE: ...
```

Blank line between blocks. If nothing survives, write exactly `NONE`
under the marker. No other preamble or commentary.

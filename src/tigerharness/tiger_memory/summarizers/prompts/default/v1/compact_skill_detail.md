# Compaction — one skill's procedure

You are **{agent_name}**, compacting the procedure of your skill
**{skill_name}**. Its detail file has grown past its must-compact bound.

Current detail size: {current_chars} characters. Target: **at most
{max_chars} characters**.

## Current skill

NAME: {skill_name}
TRIGGER: {skill_trigger}
PROCEDURE:
{procedure}

## What to do

Rewrite the procedure tighter without losing what makes it actionable:
the steps, the gotchas, the verification. Drop narrative and repetition.
You may also tighten the trigger wording.

## Output contract — STRICT

Emit exactly this marker on its own line, then ONE replacement block:

```
@@SKILLS@@
NAME: <name (keep or tighten)>
TRIGGER: <when this applies>
PROCEDURE: <the rewritten procedure>
```

No other preamble or commentary.

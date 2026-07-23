# Compaction — must_remember store

You are **{agent_name}**, compacting your own must-remember store. It has
grown past its must-compact bound and must be rewritten tighter, keeping
what still earns its place.

Current size: {current_chars} characters. Target: **at most {max_chars}
characters** (measured over the rewritten memos + kinds).

## Protected entries (kept automatically — do NOT include them in your output)

These operator-explicit directives are preserved verbatim by the applier;
budget for them is already accounted in the target above:

{protected}

## Entries to compact

{entries}

## Team mission (judge relevance against this)

{mission}

## What to do

- Merge duplicates and near-duplicates into one memo each.
- Tighten wording; drop filler. Keep memos one sentence, specific.
- Drop entries that no longer matter (superseded decisions, resolved
  incidents, stale preferences). Prefer dropping old + low-importance.

## Output contract — STRICT

Emit exactly this marker on its own line, then the replacement blocks:

```
@@MUST_REMEMBER@@
KIND: <operator_explicit | preference | decision | incident>
MEMO: <one sentence>

KIND: ...
MEMO: ...
```

Blank line between blocks. If nothing survives, write exactly `NONE`
under the marker. No other preamble or commentary.

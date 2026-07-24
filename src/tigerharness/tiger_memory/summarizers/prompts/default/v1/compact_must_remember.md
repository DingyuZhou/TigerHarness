# Compaction — must_remember store

You are **{agent_name}**, compacting your own must-remember store. It has
grown past its must-compact bound and must be rewritten tighter, keeping
what still earns its place.

Current size: {current_chars} characters. Target: **at most {max_chars}
characters** (measured over the rewritten memos + kinds).

## Protected entries (kept automatically — do NOT rewrite them)

These operator-explicit directives are preserved verbatim by the applier;
budget for them is already accounted in the target above:

{protected}

**Relevance check.** For each protected entry above, judge it against the
team mission below. If one is clearly stale — superseded, resolved, or no
longer relevant to the live mission — mark it with a `STALE:` block (its
`ID:` value). It is then DOWNGRADED to a normal `decision` (it rejoins the
ordinary pool and may later be compacted or forgotten) — it is never
silently deleted. When in doubt, keep it protected: do not mark it.

## Entries to compact

{entries}

## Team mission (judge relevance against this)

{mission}

## What to do

- Merge duplicates and near-duplicates into one memo each.
- Tighten wording; drop filler. Keep memos one sentence, specific.
- Drop entries that no longer matter (superseded decisions, resolved
  incidents, stale preferences). Prefer dropping old + low-importance.
- Entries annotated `[forget-eligible]` have gone untouched for over
  {forget_days} days — no sweep found them related to any session. Drop
  them (or, for protected ones, mark them STALE) unless one is still
  clearly valuable despite its age.

## Output contract — STRICT

Emit exactly this marker on its own line, then the replacement blocks:

```
@@MUST_REMEMBER@@
STALE: <id of a protected entry that failed the relevance check>

KIND: <preference | decision | incident>
MEMO: <one sentence>

KIND: ...
MEMO: ...
```

Blank line between blocks; `STALE:` blocks (zero or more) may be mixed
with `KIND:`/`MEMO:` blocks. Never emit `KIND: operator_explicit` — a
compaction cannot mint operator directives. If nothing survives and
nothing is stale, write exactly `NONE` under the marker. No other
preamble or commentary.

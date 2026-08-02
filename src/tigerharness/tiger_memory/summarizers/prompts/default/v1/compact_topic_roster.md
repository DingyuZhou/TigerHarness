# Compaction — topic roster

You are **{agent_name}**, compacting your own topic index. It has grown
past its must-compact bound: too many topics and/or summaries too long.

Current index size: {current_chars} characters. Target: **at most
{max_chars} characters**.

## Your topics (freshest first)

{roster}

Topics marked `[fresh]` were touched within the last {fresh_days} days —
they are protected: you may rewrite their SUMMARY, but not FORGET or
MERGE them away.

## What to do (in preference order)

1. **FORGET** topics that have gone stale — not refreshed for a long
   time and no longer likely to matter.
2. **MERGE** topics that are really about the same subject (their detail
   bodies are concatenated automatically; give the merged topic one
   summary).
3. **SUMMARY** — rewrite verbose summaries shorter ({summary_max_words}
   words or fewer), as a table-of-contents line for the topic's WHOLE
   body — never the latest session's outcome, verdict, or status.

## Output contract — STRICT

Emit exactly this marker on its own line, then one directive block per
action, blank line between:

```
@@TOPIC_ROSTER@@
ACTION: forget
TOPIC: <slug>

ACTION: merge
INTO: <surviving slug>
FROM: <slug> <slug> ...
SUMMARY: <one line for the merged topic>

ACTION: summary
TOPIC: <slug>
SUMMARY: <the shorter summary>
```

If no action is warranted, write exactly `NONE` under the marker. No
other preamble or commentary.

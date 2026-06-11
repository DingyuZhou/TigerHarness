# ADR 0004: bridge-layer idle compaction is feasible

Status: accepted (feasibility verdict). Date: 2026-06-11.
CLI version the verdict is bound to: **Claude Code 2.1.140** — a
future CLI could change headless slash-command handling; re-run the
experiment below before trusting this ADR across major versions.

## Context

Auto-compaction during interactive drives exists (50% threshold,
loss-safe; see the team knowledge note "drive-compaction"). The gap:
*proactive idle-time* compaction — "queue empty, no task running,
context above a threshold → compact now". An interactive session
cannot trigger compaction on itself; the open question was whether
the bridge/drive_sessions controller — which owns the turn boundary
of `claude -p --resume` sessions — can issue one.

## Experiment (reproducible; run verbatim)

    $ claude -p "Remember the codeword tangerine. Reply with exactly: stored" \
        --output-format json
    -> session_id 02bb9028-2dff-466e-a54e-b0a398aad24e, result "stored"

    $ claude -p "/compact" --resume 02bb9028-... --output-format json
    -> {"type": "result", "subtype": "success", "result": "",
        "usage": {"input_tokens": 0, ..., "output_tokens": 0}, ...}

The empty-success + zero-usage observation is necessary but NOT
sufficient (a swallowed command would look identical). The verdict
rests on the session transcript
(`~/.claude/projects/<dir>/<sid>.jsonl`), which after the second
command contains:

    - system/compact_boundary       with compactMetadata
    - user (isCompactSummary: true) "This session is being continued
      from a previous conversation that ran out of con..."
    - user <command-name>/compact</command-name>
    - user <local-command-stdout>Compacted (ctrl+o to see full
      summary)</local-command-stdout>

That is the CLI executing a real compaction on a resumed headless
session: boundary event written, summary injected, command stdout
recorded.

## Verdict

**FEASIBLE.** The controller layer can compact a resumed session
between turns by sending `/compact` as a `-p` prompt on `--resume`.

## Token accounting (the trigger signal)

Each assistant entry in the session JSONL (and each turn's result
JSON) carries `usage`. Worked example from this spike's seed turn:

    input_tokens=2, cache_creation_input_tokens=6752,
    cache_read_input_tokens=16756, output_tokens=287

Approximate context load = input_tokens +
cache_creation_input_tokens + cache_read_input_tokens
= 2 + 6,752 + 16,756 ≈ 23.5k tokens — ~12% of a 200k window. The
controller reads the LAST assistant entry's usage after each turn;
crossing the configured fraction (the Operator asked for 30%) arms
the idle-compact check.

## Consequences (what the implementation task builds)

In the bridge/drive_sessions layer (it owns turn boundaries; nothing
in-session changes):

1. After each completed turn, read the session's last usage; compute
   the context fraction.
2. When fraction > threshold AND the journal queue is empty AND no
   task is running (sweep classification), send one `/compact` turn
   to the session and record it.
3. Loss-safety: compaction summarizes — the journal's durable-record
   discipline (progress.md + next_action checkpoints) is what makes
   this safe BETWEEN tasks; never compact mid-task, and do not
   over-trigger (one compact per idle period).
4. Config: threshold fraction + on/off, team-level.

Follow-up implementation task: scaffolded from this ADR (see the
journal record for its id).

## Alternatives considered

- Lowering CLAUDE_AUTOCOMPACT_PCT_OVERRIDE: changes MID-task
  behavior and trades cache hits for headroom — rejected as a
  substitute (different problem).
- Documentation-only (status quo): remains the fallback if the CLI's
  slash handling changes.

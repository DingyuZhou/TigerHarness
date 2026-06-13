# TigerHarness docs — index

Start here. This routes you to the one doc that answers your question, then
each doc opens with an "At a glance" so you get the answer cheaply and only
read `## Details` when you need to.

TigerHarness (Python 3.11+, MIT; version per the PyPI badge in the
[README](../README.md)) is a Claude Code agent harness: teams of named AI
personas run against real codebases with iterative execution, Slack
integration, and persistent per-persona memory. Zero hard dependencies —
every integration is an optional extra; the default execution backend is a
plain `claude -p` subprocess.

## Find your answer in one hop

| I want to… | Read |
|---|---|
| Install / scaffold a team, see the layout, choose extras | [README](../README.md) |
| Contribute / commit conventions / the review standard | [CONTRIBUTING](../CONTRIBUTING.md), [code-review-standard.md](code-review-standard.md) |
| Understand the journal (subscription backend) end to end | [journal.md](journal.md) |
| Run a multi-persona **workflow** (compile + graph walk) | [journal-workflow-mode.md](journal-workflow-mode.md) |
| Know how a crashed/idle task resumes | [journal-instant-resume.md](journal-instant-resume.md) |
| Schedule from Slack **cheaply**, or understand rails/billing + `status.json` | [subscription-backend.md](subscription-backend.md) |
| Understand per-persona memory from journal work (the worklog rail) | [per-persona-journal-memory.md](per-persona-journal-memory.md) |
| Set up / operate the Slack bridge (1..N lanes) | [slack-bridge.md](slack-bridge.md) |
| Use tiger-memory (stores, CLI, search) | [tiger-memory.md](tiger-memory.md) |
| Run the team-wide memory sweep | [tiger-memory-sweep-protocol.md](tiger-memory-sweep-protocol.md) |
| Use the backend-agnostic agent SDK | [agent_sdk.md](agent_sdk.md) |
| Read past design decisions | [adr/](adr/) (0001 workflow-runner, 0002 phase 2, 0003 remove legacy runners, 0004 bridge idle compaction, 0005 pydantic-ai) |

## Must-not-miss rules (one hop, never bury these)

- **Slack rail rule** — a Slack-triggered session may SCHEDULE journal tasks
  but must NEVER drive them (driving is the subscription rail). See
  [subscription-backend.md](subscription-backend.md) and
  [slack-bridge.md](slack-bridge.md#journal-tasks-over-slack-cost-discipline).
- **Cross-root dismiss safety** — `dismiss` tears down only the operated
  root's bridge, scoped by content (the 2026-06-12 incident class). See
  [slack-bridge.md](slack-bridge.md#the-bridge-one-process-1n-lanes).
- **Per-persona memory rail** — journal work is attributed via persona-stamped
  worklog notes, not the raw transcript. See
  [per-persona-journal-memory.md](per-persona-journal-memory.md).

## At a glance (the rest of the system)

- **One execution rail.** `journal` is the execution path: a file-based
  subscription backend that routes agent work through the interactive Claude
  Code app, billing a monthly subscription instead of token-metered API. The
  legacy API-billed runners were removed ([adr/0003](adr/0003-remove-legacy-runners.md)).
- **Journal.** Scaffolds single-persona tasks (`kind=task`) and multi-persona
  workflows (`kind=workflow`) from team playbooks; **19 CLI verbs** cover the
  lifecycle (incl. the `defer`/`materialize` deferred-inbox pair and
  team-pinned, provenance-stamped scheduling). Workflows compile in-session
  via a drafter/two-critic loop over mechanical Tier-1 validators, then walk
  step by step through gates that enforce order, require a per-step work note,
  and stamp each note with its persona. A heartbeat lease classifies tasks
  idle/busy/crashed; a fresh session resumes a crashed walk at the same step.
- **Slack bridge.** One Socket-Mode bridge serves 1..N teams (lanes), forwards
  DMs/@mentions to personas, posts replies in-thread, persists
  thread→session; a `notify` CLI sends proactive text/file messages.
- **Tiger-memory.** Per-persona archive/journal/briefing stores with lazy
  rebuild, pinning, decay, drill-down; substring search by default, semantic
  via local fastembed or OpenAI embeddings per extras; a team-wide sweep
  protocol keeps a roster fresh on the subscription rail under a lease,
  watermark, and per-wake cap.
- **Team tooling.** `tigerharness init` scaffolds a team and installs five
  bundled Claude Code skills (drive-journal, journal-new, slack-notify,
  workflow-append-steps, tigerharness-basics), hash-aware so hand-edited
  skills are never overwritten; `dismiss` tears down. `agent_sdk` is a typed,
  backend-agnostic API over the `claude -p` and Claude Agent SDK runtimes.
- **Logs.** Every CLI reads `TIGERHARNESS_LOG_LEVEL` (default WARNING) via one
  helper; one named logger per module; `tests/test_logging_audit.py` enforces
  coverage.

## Current reference vs history

The docs above (plus README/CONTRIBUTING) are the **current reference**.
Past design narratives and decisions live in [adr/](adr/) and the design
record below; don't treat a design narrative as current behavior —
`src/tigerharness/` is the ground truth.

**Design record / history (not current reference):**
[tiger-memory-rework.md](tiger-memory-rework.md) — the tiger-memory redesign
narrative (a follow-up task will relocate it under a `history/` area).

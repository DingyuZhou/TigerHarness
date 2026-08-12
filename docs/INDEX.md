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
| Park a task on an Operator question instead of blocking the drive | [journal-operator-questions.md](journal-operator-questions.md) |
| Schedule from Slack **cheaply**, or understand rails/billing + `status.json` | [subscription-backend.md](subscription-backend.md) |
| Drive the journal queue automatically (the sanctioned exception; auto-start + auto-stop) | [autodrive.md](autodrive.md) |
| Get Slack heartbeats + threaded drive summaries from the autodrive daemon (or mute them) | [autodrive-notifications.md](autodrive-notifications.md) |
| Understand per-persona memory from journal work (the worklog rail) | [per-persona-journal-memory.md](per-persona-journal-memory.md) |
| Set up / operate the Slack bridge (1..N lanes) | [slack-bridge.md](slack-bridge.md) |
| Use tiger-memory (the three bounded stores, CLI, config) | [tiger-memory.md](tiger-memory.md) |
| Understand the memory design (stores + staged compaction, the rationale) | [DESIGN-memory.md](DESIGN-memory.md) |
| Run the team-wide memory sweep | [tiger-memory-sweep-protocol.md](tiger-memory-sweep-protocol.md) |
| Use the backend-agnostic agent SDK | [agent_sdk.md](agent_sdk.md) |
| Make the queue self-driving (scheduling starts the daemon, draining stops it) | [adr/0010](adr/0010-self-driving-journal.md), [autodrive.md](autodrive.md) |
| Read past design decisions | [adr/](adr/) (0001 workflow-runner, 0002 phase 2, 0003 remove legacy runners, 0004 bridge idle compaction, 0005 pydantic-ai, 0006 incremental memory sweep, 0007 topic-store revamp, 0008 team event log, 0009 remove single-tenant bridge, 0010 self-driving journal) |

## Must-not-miss rules (one hop, never bury these)

- **Slack rail rule** — a Slack-triggered session may SCHEDULE journal tasks
  but must NEVER drive them (driving is the subscription rail). Unchanged by
  [adr/0010](adr/0010-self-driving-journal.md): a `defer` may now *wake* the
  autodrive daemon, but the Slack session is still not the driver. See
  [subscription-backend.md](subscription-backend.md) and
  [slack-bridge.md](slack-bridge.md#journal-tasks-over-slack-scheduling-discipline).
- **Auto-start is safe only while `claude -p` bills the subscription.** If
  that changes, set `TIGERHARNESS_AUTODRIVE_AUTOSTART=0` — no code change.
  See [autodrive.md](autodrive.md).
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
  workflows (`kind=workflow`) from team playbooks; **20 CLI verbs** cover the
  lifecycle (incl. the `defer`/`materialize` deferred-inbox pair and
  team-pinned, provenance-stamped scheduling). Workflows compile in-session
  via a drafter/two-critic loop over mechanical Tier-1 validators, then walk
  step by step through gates that enforce order, require a per-step work note,
  and stamp each note with its persona. A heartbeat lease classifies tasks
  idle/busy/crashed; a fresh session resumes a crashed walk at the same step.
- **Slack bridge.** One Socket-Mode bridge serves 1..N teams (lanes), forwards
  DMs/@mentions to personas, posts replies in-thread, persists
  thread→session; a `notify` CLI sends proactive text/file messages.
- **Tiger-memory.** Per-persona memory as **three bounded, self-pruning
  stores** (skills / must_remember / topics): in-persona extraction turns
  finished sessions into store entries, staged compaction merges / tightens /
  forgets when a surface overflows, and a Python-rebuilt, index-only briefing
  (must_remember + skill index + topic index) is read at session start. A
  team-wide sweep protocol keeps a roster fresh on the subscription rail
  under a lease, watermark, and per-wake cap; a lazy team event log records
  who-did-what ([adr/0008](adr/0008-team-event-log.md)). Design:
  [DESIGN-memory.md](DESIGN-memory.md),
  [adr/0007](adr/0007-topic-store-revamp.md).
- **Team tooling.** `tigerharness init` scaffolds a team and installs six
  bundled Claude Code skills (drive-journal, journal-new, journal-autodrive,
  slack-notify, workflow-append-steps, tigerharness-basics), hash-aware so
  hand-edited skills are never overwritten; `dismiss` tears down. `agent_sdk`
  is a typed, backend-agnostic API over the `claude -p` and Claude Agent SDK
  runtimes. `autodrive` periodically drives the journal queue via that SDK
  (the Operator-authorized exception to the human-only drive rule —
  [autodrive.md](autodrive.md)). Opt in with
  `TIGERHARNESS_AUTODRIVE_AUTOSTART` and it becomes self-driving: scheduling
  work starts it, a drained queue stops it, and an idle tick costs a file
  walk instead of a model session ([adr/0010](adr/0010-self-driving-journal.md)).
- **Logs.** Every CLI reads `TIGERHARNESS_LOG_LEVEL` (default WARNING) via one
  helper; one named logger per module; `tests/test_logging_audit.py` enforces
  coverage.

## Current reference vs history

The docs above (plus README/CONTRIBUTING) are the **current reference**.
Past design narratives and decisions live in [adr/](adr/) and the design
record below; don't treat a design narrative as current behavior —
`src/tigerharness/` is the ground truth.

The superseded design narratives that used to live under `docs/history/`
(the tiger-memory rework and the drive-journal redesign notes) were
deleted on 2026-08-11; consult git history if you need them. The durable
decisions they carried live on in [adr/](adr/) and the design docs above.

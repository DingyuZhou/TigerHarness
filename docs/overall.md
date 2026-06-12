# TigerHarness at a glance

TigerHarness (v0.2.1, Python 3.11+, MIT) is a Claude Code agent
harness: it runs teams of named AI personas against real codebases,
with iterative execution, Slack integration, and persistent
per-persona memory. It has zero hard dependencies — every
integration is an optional extra — and its default execution
backend is a plain `claude -p` subprocess.

**One execution rail.** The `journal` sub-command is the execution
path: a file-based subscription backend that routes agent work
through the interactive Claude Code app, so it bills to a monthly
subscription instead of token-metered API. (The legacy api-billed
runners were removed — ADR 0003.)

**Journal (subscription backend).** Scaffolds single-persona tasks
(`kind=task`) and multi-persona workflows (`kind=workflow`) from
team playbooks; 19 CLI verbs cover the lifecycle (incl. the
deferred-inbox pair `defer`/`materialize` and team-pinned,
provenance-stamped scheduling). Workflows are
compiled in-session by a drafter/two-critic loop over mechanical
Tier-1 validators, then walked step by step through gates that
enforce step order, require a work note per step, and stamp each
note with its persona for memory attribution. Crash handling is
built in: a heartbeat lease classifies tasks idle/busy/crashed, and
a fresh session resumes a crashed walk at the same step.


**Slack bridge.** A Socket Mode bridge forwards DMs to personas and
posts replies in-thread, with multi-team/persona routing and a
persistent thread-to-session map; a `notify` CLI sends proactive
text or file messages.

**Where the logs are.** Every CLI reads `TIGERHARNESS_LOG_LEVEL`
(default WARNING; INFO/DEBUG opt-in) via one helper; logs go to
stderr, the bridge daemon to its own handlers. One named logger
per module; `tests/test_logging_audit.py` enforces coverage and
`tests/test_logging_families.py` pins the load-bearing lines (gate
refusals, sweep classifications, retries, spawn exits, redaction).

**Tiger-memory.** Per-persona persistent memory: archive, journal,
and briefing stores with lazy rebuild, pinning, decay, and
drill-down. Search is substring by default, semantic via local
fastembed or OpenAI embeddings per extras. A team-wide sweep
protocol (`sweep-plan/done/complete/release`, with `plan` and
`ingest-summary` staging) keeps a whole roster's memory fresh on
the subscription rail under a lease, watermark, and per-wake cap.

**Team tooling.** `tigerharness init` scaffolds a team (personas,
config, .env) and installs four bundled Claude Code skills
(drive-journal, journal-new, slack-notify, workflow-append-steps),
hash-aware so hand-edited skills are never
overwritten; `dismiss` tears down. An `agent_sdk` provides a typed,
backend-agnostic API over the `claude -p` and Claude Agent SDK
runtimes, with a shared retry/error model.

**Entry points and extras.** Install: `uv add 'tigerharness[all]'`
(or pip/pipx equivalent). Console scripts: `tigerharness`
(init, dismiss, tiger-memory, slack-bridge, journal) and
`tiger-memory`. Extras: `[anthropic]`, `[slack]`,
`[memory]`, `[memory-rag]`, `[memory-rag-openai]`, `[all]`.
Thirteen further docs files plus ADRs cover each module in depth.

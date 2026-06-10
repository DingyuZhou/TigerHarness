# TigerHarness at a glance

TigerHarness (v0.2.1, Python 3.11+, MIT) is a Claude Code agent
harness: it runs teams of named AI personas against real codebases,
with iterative execution, Slack integration, and persistent
per-persona memory. It has zero hard dependencies — every
integration is an optional extra — and its default execution
backend is a plain `claude -p` subprocess.

**Two execution rails.** The `journal` sub-command is the default
rail: a file-based subscription backend that routes agent work
through the interactive Claude Code app, so it bills to a monthly
subscription instead of token-metered API. The api-billed rail
(`task_runner`, `workflow_runner`) remains for api-budget
workloads.

**Journal (subscription backend).** Scaffolds single-persona tasks
(`kind=task`) and multi-persona workflows (`kind=workflow`) from
team playbooks; 16 CLI verbs cover the lifecycle. Workflows are
compiled in-session by a drafter/two-critic loop over mechanical
Tier-1 validators, then walked step by step through gates that
enforce step order, require a work note per step, and stamp each
note with its persona for memory attribution. Crash handling is
built in: a heartbeat lease classifies tasks idle/busy/crashed, and
a fresh session resumes a crashed walk at the same step.

**Task runner (api rail).** Fire-and-forget iterative execution:
`assign/list/cancel/amend/show/logs/continue` over a per-job
working folder, resuming the same session across iterations, with
a stuck watchdog. The standalone `workflow` script
(`start/show/list/tail/cancel`) drives api-billed multi-persona
orchestration with the same compile pipeline the journal uses.

**Slack bridge.** A Socket Mode bridge forwards DMs to personas and
posts replies in-thread, with multi-team/persona routing and a
persistent thread-to-session map; a `notify` CLI sends proactive
text or file messages.

**Tiger-memory.** Per-persona persistent memory: archive, journal,
and briefing stores with lazy rebuild, pinning, decay, and
drill-down. Search is substring by default, semantic via local
fastembed or OpenAI embeddings per extras. A team-wide sweep
protocol (`sweep-plan/done/complete/release`, with `plan` and
`ingest-summary` staging) keeps a whole roster's memory fresh on
the subscription rail under a lease, watermark, and per-wake cap.

**Team tooling.** `tigerharness init` scaffolds a team (personas,
config, .env) and installs five bundled Claude Code skills
(drive-journal, journal-new, assign-task, slack-notify,
workflow-append-steps), hash-aware so hand-edited skills are never
overwritten; `dismiss` tears down. An `agent_sdk` provides a typed,
backend-agnostic API over the `claude -p` and Claude Agent SDK
runtimes, with a shared retry/error model.

**Entry points and extras.** Install: `uv add 'tigerharness[all]'`
(or pip/pipx equivalent). Console scripts: `tigerharness`
(init, dismiss, task-runner, tiger-memory, slack-bridge, journal),
`tiger-memory`, `workflow`. Extras: `[anthropic]`, `[slack]`,
`[memory]`, `[memory-rag]`, `[memory-rag-openai]`, `[all]`.
Thirteen further docs files plus ADRs cover each module in depth.

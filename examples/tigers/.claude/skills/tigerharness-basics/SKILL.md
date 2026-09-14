---
name: tigerharness-basics
description: The basics of operating a tigerharness team -- what each `tigerharness` CLI sub-command does (init, dismiss, journal, tiger-memory, slack-bridge, autodrive), the team file structure init scaffolds (what is source-of-truth vs generated), how to recruit a new persona onto an existing team, and how a workflow task is created from a playbook. Use when someone asks "how do I add a persona / team member", "what does tigerharness init do", "where do playbooks live", "what is this folder for", "how do I update the bundled skills", or any other how-does-my-own-team-work question.
---

# tigerharness-basics

How your team works: the `tigerharness` CLI and the files around you.
This skill ships with tigerharness and is installed by `tigerharness
init`; it describes the **fresh-team baseline**. Your team's own
charter (`charter/README.md`) and knowledge base (`knowledge/`) may
add local conventions on top — when they conflict, the team's own
rules win.

## The CLI at a glance

Six sub-commands (run `tigerharness --help`):

- `tigerharness init` — scaffold a team and/or persona; also installs
  and refreshes these bundled skills.
- `tigerharness dismiss` — interactively tear down a team or a single
  persona. Destructive; has `--dry-run`.
- `tigerharness journal` (alias: `j`) — the file-based subscription
  backend: schedule and inspect tasks. Driving them is **skill-only**
  (the `drive-journal` skill), never CLI-driven.
- `tigerharness autodrive` (alias: `ad`) — the background process that
  drives the journal on a fixed interval via the agent SDK (the
  Operator-authorized exception to the human-only drive rule); managed
  through the `journal-autodrive` skill.
- `tigerharness tiger-memory` (alias: `tm`) — persistent per-persona
  memory (three bounded stores): rebuild, pin, inspect.
- `tigerharness slack-bridge` (alias: `sb`) — send Slack messages from
  an agent session; `gen-service` renders the systemd unit for the
  bridge.

## `tigerharness init`

Scaffolds a team folder and a persona inside it. Interactive by
default; every prompt has a flag so it can run unattended:

    tigerharness init --team <Team> --persona <Name> --dir <teams-root>
    tigerharness init -y          # accept defaults: team "tigers", persona "assistant"

Flags: `--persona`, `--team`, `--team-dir` (custom location),
`--no-memory` (skip the per-persona tiger-memory config), `--no-slack`
(skip the Slack `.env` template), `--multi-team` / `--no-multi-team`
(multi-team Slack mode on/off without prompting), `--vendor` /
`--model` (a NEW team's default model vendor — `claude` runs
`claude -p`, `chatgpt` runs `codex exec` — and optional default model id,
written to `configs/personas.yaml`; `--yes` takes `claude`), `--goal`
(the new team's one-sentence mission), `--traits` (the persona's
initial traits), `--yes`/`-y`, `--dir` (where teams live; default
current directory), and `--refresh` (below).

Running `init` against an **existing** team is how you **recruit** —
see the walkthrough below. Existing files are never clobbered:
scaffolding is idempotent and only fills in what's missing.

`--refresh` doesn't create a persona at all; it brings an existing
team's **shipped files** current, and is the one command to run after
upgrading tigerharness:

    tigerharness init --refresh --team-dir <path-to-team>

- **Skills** (`.claude/skills/`) — installs bundled skills the team is
  missing, refreshes any skill whose on-disk content still matches a
  previously shipped version, and leaves hand-edited skills untouched
  (delete one to re-adopt the shipped version). Also recreates the
  `.agents/skills` symlink (how Codex finds the same files) if missing.
- **`.gitignore`** — appends any line from the shipped template the
  team doesn't have yet, with the comment that explains it.
  **Append-only**: nothing is removed, reordered, or rewritten, so your
  own rules survive. A shipped rule you deleted on purpose does come
  back (they are all "never commit this" safety rules) — follow it with
  a `!` negation if you truly want it gone.

Idempotent: run it as often as you like. `--refresh-skills` is the old
name, still accepted, and now does both.

## `tigerharness dismiss`

Interactive teardown of a team or one persona — it walks you through a
picker, shows the removal plan, and asks before deleting. Flags:
`--dir` (teams root) and `--dry-run` (print the plan, delete nothing).
Removing a persona also cleans its roster row, memory folder, and
prompt; removing the last team of a multi-team setup stops the
slack-bridge systemd unit. Always try `--dry-run` first.

## `tigerharness journal` (alias: `j`)

The file-based **subscription backend**: tasks live as folders under
`journal/active/` at your team root, driven by interactive
(subscription-billed) sessions instead of an API-billed programmatic
driver.
Run journal commands **from the team root** — the journal root
resolves to `<team>/journal/` when the cwd is a team folder (an
explicit `TIGERHARNESS_JOURNAL_DIR` env var overrides; with neither,
state falls back to `~/.local/state/tigerharness-journal`).

Scheduling and inspection (safe to run by hand):

    tigerharness journal new --kind task --title "..." --prd brief.md --persona <Name>
    tigerharness journal new --kind workflow --title "..." --playbook <name> --brief-file brief.md --team <Team>
    tigerharness journal list
    tigerharness journal status <task-id>
    tigerharness journal validate-personas <Team>
    tigerharness journal defer --title "..." --team <Team> --kind task --persona <Name> --payload-file brief.md

`defer` is the cheap Slack-side scheduling verb: it parks the ask in the
`deferred/` inbox and a drive's `materialize` turns it into a real task
(`--kind workflow --playbook <name>` for a workflow).

Other sub-commands exist (`sweep`, `claim`, `release`, `step-done`,
`materialize`, `answer`, the `compile-*` family, `validate-graph`,
`land-compile`, `append-steps`, `abort`; `schedule` still works but is
DEPRECATED since ADR 0010 — autodrive replaced recurring definitions) —
those belong to the **driver**: the `drive-journal` skill runs them as
part of its protocol, and `journal/OPERATING.md` (written on first use)
is the full contract. Don't hand-run gate commands outside a drive;
scaffold with the `journal-new` skill, drive with `drive-journal`.

**Drive lanes (ADR 0012).** Every task, workflow step and inbox entry
is owned by a persona, and that persona's vendor + model (from
`configs/personas.yaml`) is the *lane* the work runs on. A drive
started with `--driver <persona>` sees `journal sweep --driver`'s lane
view (`[mine]` vs. not yours), `journal claim --driver` refuses
other-lane work with **exit 3** (`--any-lane` overrides, for a hand
drive when no drive on that vendor exists), and `step-done --driver`
prints a `handoff:` cue when the next step belongs to another lane.
A single-vendor team never sees a refusal or a handoff cue (every
item reads `[mine]`). Whether a Slack-triggered
session may drive at all is the team knob
`TIGERHARNESS_JOURNAL_SLACK_DRIVES=1` in `configs/.env` (ADR 0013).

## `tigerharness tiger-memory` (alias: `tm`)

Per-persona persistent memory: **three bounded stores** (`skills` /
`must_remember` / `topics`) that self-prune via staged compaction
(ADR 0007). Only the small indexes load at session start; per-skill and
per-topic detail files load on demand. Each persona's store and config
live under `memories/<Name>/`; pass the config explicitly or via env:

    tigerharness tm --config memories/<Name>/tiger-memory.config.yaml rebuild
    tigerharness tm --config ... pin "Operator prefers tabular diffs" --kind preference
    tigerharness tm --config ... state

Common verbs: `init` (create empty store + validate config), `rebuild`
(format gate + regenerate the session-start briefing — the
session-start hook), `pin` (write a `must_remember` entry; `--kind
operator_explicit|preference|decision|incident`), `state` (JSON snapshot
of the three stores), `migrate-to-topics` (one-off: retire a pre-ADR-0007
diary/fuzzy surface). The in-session sub-agent executor (`plan` stages
extraction prompts, `ingest-extraction` writes back one bundle over
stdin, `ingest-staged` glues every staged `.extract.md` card in one
process, `compact-plan` / `compact-apply` stage + apply bound
compactions, `team-events-compact-plan` / `team-events-compact-apply`
fold the team-wide event log's aged-out periods — ADR 0008) and the
`sweep-*` family back the in-session and
team-sweep protocols — driven by the `sweep-memory` skill, like the
journal gates. Each persona's config carries two transcript sources,
`claude_code` and `codex`, so a persona on either vendor gets its
sessions ingested; a lane-restricted team sweep uses `sweep-plan
--lane-of <persona>` (defaults to `--own-persona`) and `--own-only`
never widens to a team run (ADR 0012). Deep dive: `docs/tiger-memory.md` and the canonical
design `docs/DESIGN-memory.md` in the tigerharness repo.

## `tigerharness slack-bridge` (alias: `sb`)

Agent-to-human Slack messaging (the `slack-notify` skill wraps this):

    tigerharness sb text "Build green, branch ready for review."
    tigerharness sb file --file report.pdf --comment "Q2 numbers"

Both take `--thread <thread_ts>` to reply inside a thread. Token/config
come from the team's `configs/.env` and `configs/slack-bridge.yaml`.
`tigerharness sb gen-service` emits the systemd user unit that runs
the multi-team bridge (flags: `--teams-root`, `--bridges-config`,
`--env-file`, `--venv-python`); it prints a **per-root** unit name on
stderr (`slack-bridge-<root>-<hash>.service`, e.g.
`slack-bridge-teams-4a8c8b.service`) — redirect its output to
`~/.config/systemd/user/<that-printed-name>`. Deep dive:
`docs/slack-bridge.md`.

## The team file structure

What `tigerharness init` scaffolds, and who owns each piece.
**Source-of-truth** files are yours to edit; **generated** files are
maintained by the tooling.

- `.gitignore` — seeded so secrets (`configs/.env`) and local state
  never land in git. Extend it as your team needs; `--refresh` tops it
  up append-only when tigerharness ships a new rule.
- `AGENTS.md` — the always-loaded session bootstrap (vendor-neutral;
  source of truth). `CLAUDE.md` just imports it for Claude Code.
- `configs/personas.yaml` — THE team roster + `default_persona` + the
  team's default model vendor (`default_vendor: claude | chatgpt` and
  `default_model`; a persona row may override with its own `vendor:` /
  `model:`). Source of truth; `init` appends a row per recruit and asks
  the vendor question once, when the team is created.
- `configs/repos.yaml` — path indirection: where the team root and the
  project repo live. Auto-detected when possible; otherwise created
  with a commented `# project:` placeholder and a stderr hint — fill
  it in by hand.
- `configs/tiger-memory.defaults.yaml` — team-wide memory defaults.
- `configs/.env` — Slack tokens **and the team's knobs** (gitignored):
  `TIGERHARNESS_JOURNAL_SLACK_DRIVES=1` lets Slack sessions drive the
  journal (ADR 0013), `TIGERHARNESS_AUTODRIVE_*` configures the
  self-driving daemon (ADR 0010), `SLACK_NOTIFY_CHANNEL` is the ops-log
  channel.
- `personas/<Name>/prompt.md` — each persona's operating prompt.
  Scaffolded as a template; filling it in is the recruit's first task.
- `charter/README.md`, `knowledge/README.md` — the team's operating
  manual and curated reference base. Seeded with TODOs; yours.
- `skills/README.md` — the team's OWN skills folder (yours), distinct
  from `.claude/skills/` (the bundled ones, generated/refreshable).
- `.claude/settings.json` — generated; wires
  `TIGERHARNESS_PERSONAS_CONFIG` for every session.
- `.claude/skills/<name>/SKILL.md` — the bundled skills
  (`drive-journal`, `journal-autodrive`, `journal-new`, `slack-notify`,
  `workflow-append-steps`, `tigerharness-basics`, `sweep-memory`).
  Generated; refreshed by `--refresh`; hand-edits preserved.
- `.agents/skills` — a symlink to `.claude/skills`, so Codex (which
  discovers skills under `.agents/skills/`) reads the very same files.
  One copy of every skill; generated, recreated by `--refresh` if
  missing.
- `memories/<Name>/` — per-persona tiger-memory config + store.
- `journal/` — NOT scaffolded by init: created on first journal use at
  the team root (then holds `OPERATING.md`, `active/`, `done/`).
- `workflow/` — also NOT scaffolded, but its location is fixed, not a
  style choice: `journal new --kind workflow --playbook <name>`
  resolves the bare name to `<team-root>/workflow/<name>.md` (and
  rejects path-like values). Create the folder when you write your
  first playbook.

## Recruiting a new persona

1. From the teams root, run
   `tigerharness init --team <Team> --persona <NewName> --dir .`
   (the existing-team picker also gets you there interactively).
   This creates `personas/<NewName>/prompt.md` (a template),
   `memories/<NewName>/tiger-memory.config.yaml`, and appends the
   roster row to `configs/personas.yaml`.
2. Write the persona's `prompt.md` — identity, role, boundaries.
   The template marks what to fill in.
3. Edit the new roster row: description, aliases, and (if this team
   uses Slack) make sure the persona is reachable by name. If the
   recruit should run on a different vendor than the team default,
   uncomment its `vendor:` (`claude` / `chatgpt`) and, optionally,
   `model:` -- the Slack bridge honours it after a restart; autodrive
   fires a drive on the recruit's lane whenever it owns actionable work
   (and a memory-sweep session when it has un-swept sessions), not only
   when it is the `--driver`. Without a `model:` the recruit inherits
   `default_model` only while on the team's `default_vendor`.
4. Verify what the recruit produced: `personas/<NewName>/prompt.md`
   exists and is filled in, and `configs/personas.yaml` has the new
   row with the right `prompt_file`. (`journal validate-personas` is
   NOT this check — it is the workflow-compile preflight; see the
   next walkthrough.)

## Creating a workflow task

0. Precondition — the compile roles must exist. Compiling a workflow
   uses three personas (drafter + two critics); the role -> persona
   mapping comes from `configs/workflow.yaml` and **defaults to
   Anzai/Akagi/Ayako**. A team without personas of those names must
   either recruit them or write `configs/workflow.yaml` mapping the
   roles onto its own roster (keys: `compile_personas.drafter` /
   `.akagi` / `.ayako`). Preflight check:
   `tigerharness journal validate-personas <Team>` — exit 0 prints
   the resolved mapping; exit 1 lists which prompts are missing.
   Skip this and `journal new --kind workflow` refuses with the same
   missing-personas error.
1. Write (or pick) a playbook — the markdown file describing the
   phases and seats your team runs. It must live at
   `<team-root>/workflow/<name>.md`: the scaffolder resolves the bare
   playbook name against that folder and rejects path-like values.
2. Write the task brief as a markdown file.
3. Scaffold with the `journal-new` skill, or directly:
   `tigerharness journal new --kind workflow --title "..."
   --playbook <name> --brief-file <brief.md> --team <Team>`.
4. A `kind=task` (single persona, no playbook) takes `--prd <file>`
   and `--persona <Name>` instead.
5. The scaffolder is LLM-free and cheap. Execution happens later: an
   interactive session invokes the `drive-journal` skill, which
   compiles the playbook into a step graph and walks it persona by
   persona. You never drive tasks via the CLI.

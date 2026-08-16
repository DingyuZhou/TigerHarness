# new-team setup

## At a glance
- **What:** the day-one runbook — ordered steps that take a brand-new team
  from nothing to fully operational, each with a verification that **fails
  loudly** when the step did not take.
- **Who reads it:** an **AI persona working alongside the Operator**. The
  persona runs the commands and reads the output; the Operator supplies the
  things only a human can (Slack tokens, channel ids, the team's goal). Each
  step says which of the two is on the hook.
- **Why it exists:** a team can come out *half-configured and silent*. Two
  real symptoms from the Inkstone setup: the team never knew the journal
  **scaffold** and **autodrive** features existed, so work never entered the
  queue and nothing self-drove; and the **ops-log channel** was never
  configured, so the 5-minute turn-progress heartbeats never appeared. Both
  fail without an error message.
- **Must-not-miss:** `tigerharness init` is a **scaffolder, not an
  installer**. It writes files and stops. Everything that makes a team
  *live* — Slack tokens, the bridge index, the ops-log channel, autodrive,
  the `workflow/` playbook directory — is a separate deliberate step below.
  A team that only ran `init` is a directory, not a team.
- **The 10 stages:** prerequisites → `init` → `configs/` → prompts + charter
  → Slack app + lane → ops-log channel → autodrive → journal scaffold + the
  drive rail → tiger-memory → the final "is it alive?" checklist.

> **How to read a verification.** Every step ends with a command and the
> output that proves it worked. Where a feature's failure mode is *silence*,
> the check prints something that **differs** between the configured and the
> unconfigured team — a check that cannot fail is decoration, and that is
> exactly how the two Inkstone symptoms survived setup.

## Details

### Stage 0 — Prerequisites and install

Python 3.11+ and [`uv`](https://docs.astral.sh/uv/). TigerHarness has zero
hard dependencies; every integration is an optional extra, so **install the
extras for the features you intend to turn on** — Slack in particular.

The Operator decides where the team lives. Two roots matter and they are
**not** the same directory:

- the **project checkout** (the TigerHarness source, if you are running from
  a clone), and
- the **teams root** — the parent directory your team folders sit in.

Install, then confirm the CLI resolves:

```bash
uv run tigerharness --help
```

Expected: the sub-command list — `init`, `dismiss`, `tiger-memory (tm)`,
`slack-bridge (sb)`, `journal (j)`, `autodrive (ad)`. If `tigerharness` is
not found, nothing below will work; fix this first.

**Optional but recommended:** set `TIGERHARNESS_LOG_LEVEL` (read in
`_logging.py`, valid values `CRITICAL` / `ERROR` / `WARNING` / `INFO` /
`DEBUG`) to `INFO` for the whole setup session. Several verifications in this
runbook read a log line, and the two most important ones — the ops-log
readiness lines in Stage 5 — are logged at INFO. At the default level you
will not see them and cannot tell a configured team from a broken one.

### Stage 1 — `tigerharness init`

`init` creates a persona inside a team folder, creating the team scaffold
first if it does not exist.

**Operator supplies:** the team name, the first persona's name, the team's
one-sentence goal.

```bash
cd <teams-root>
uv run tigerharness init --team <Team> --persona <Persona> \
    --goal "<one sentence — why this team exists>"
```

#### Where it creates things — read this before you run it

`init`'s search root is `--dir`, which **defaults to the current
directory**. `discover_teams` returns `[search_root]` when the search root is
*itself* a team (it treats any directory containing `configs/personas.yaml`
as a team), otherwise it scans the immediate children.

The consequence, and it is the trap that bites on your *second* team: a bare
`init` run **from inside an existing team folder** extends that team rather
than creating a new one. Run `init` from the teams root (the parent), or pass
`--dir` / `--team-dir` explicitly.

#### The flags that matter on day one

| Flag | Effect |
|---|---|
| `--team` | team name; skips the picker |
| `--persona` | persona name; skips the prompt |
| `--team-dir` | explicit team directory (default `<search-root>/<team>`) |
| `--dir` | where to search for existing teams (default `.`) |
| `--goal` | seeds the charter's Mission section with the Operator's words |
| `--traits` | recorded **verbatim** in `prompt.md` for the persona to expand later |
| `--multi-team` | create the top-level `slack-bridge.yaml` index — **say yes** (Stage 4) |
| `--no-multi-team` | skip the index; the bridge then needs one created by hand |
| `--no-slack` | skip the Slack `.env` template |
| `--no-memory` | skip the per-persona tiger-memory config |
| `--yes` / `-y` | accept defaults non-interactively |
| `--refresh` | **not** a persona step — see Stage 9 |

`--traits` is recorded verbatim on purpose: the scaffolder makes **no model
calls**. Turning traits into a full persona prompt happens later, in the
persona's own first session.

#### What it creates

Team-level, via `create_team`: `.gitignore`, `configs/personas.yaml`,
`configs/tiger-memory.defaults.yaml`, `configs/.env` (unless `--no-slack`),
`configs/repos.yaml`, `skills/README.md`, `charter/README.md`,
`knowledge/README.md`, `AGENTS.md`, `CLAUDE.md`, and `.claude/` — which holds
`settings.json` (carrying `TIGERHARNESS_PERSONAS_CONFIG` pointed at
`configs/personas.yaml`) plus the bundled skills installed by
`install_bundled_skills`.

Per-persona, via `add_persona`: `personas/<Persona>/prompt.md`, an appended
entry in `configs/personas.yaml`, and
`memories/<Persona>/tiger-memory.config.yaml` (unless `--no-memory`).

`AGENTS.md` is the vendor-neutral session bootstrap; `CLAUDE.md` imports it
so Claude Code loads the same content automatically.

#### What it deliberately does NOT create

This list is the difference between a scaffolded directory and a live team.

- **`workflow/`** — the playbook directory. `journal new --kind workflow`
  resolves a playbook as `<team-root>/workflow/<name>.md` and **exits with an
  error if the file is not there**. No playbook directory means no workflow
  tasks. See Stage 7.
- **`configs/workflow.yaml`** — optional; only needed to override the
  `compile_personas` role→persona map. Without it the defaults in
  `COMPILE_PERSONAS` apply: `Anzai` (drafter), `Akagi`, `Ayako`. Those three
  personas must **exist on disk** before a workflow task can compile.
- **Slack tokens.** The `.env` is a *template*. Real `SLACK_APP_TOKEN` /
  `SLACK_BOT_TOKEN` values are Stage 4.
- **The ops-log channel.** Never set by `init`. Stage 5 — this is Inkstone
  symptom #2.
- **Autodrive settings.** Never set by `init`; autostart is off unless you
  opt in. Stage 6 — this is Inkstone symptom #1.
- **An existing `.claude/settings.json`.** Only a *missing* one is created;
  an existing file is never rewritten.

`init` is idempotent and never clobbers: `_write_if_missing` skips any file
that already exists, and `add_persona` raises rather than overwrite a
persona whose `prompt.md` is already there.

**Verify:**

```bash
cd <teams-root>/<Team>
ls configs/ && cat configs/personas.yaml
```

Expected: `personas.yaml`, `repos.yaml`, `tiger-memory.defaults.yaml`, `.env`;
and your persona listed in `personas.yaml`. If `personas.yaml` is missing, the
directory is not a team and every later `journal` command will resolve
somewhere else.

### Stage 2 — `configs/`

Four files, three of which `init` wrote for you.

- **`configs/personas.yaml`** — the roster, plus the `default_persona` field
  the session bootstrap reads. Each entry may carry an `aliases:` list; the
  journal resolves persona names through that alias map, so an alias is the
  supported way to let the Operator address a persona by nickname.
- **`configs/repos.yaml`** — the path-indirection map, written by
  `_scaffold_repos_yaml`. Prose and config reference paths relative to the
  team root; this file is the single place a machine-specific layout is
  recorded, which is what makes a team folder portable between machines.
  `_detect_project_dir` auto-captures a nearby TigerHarness checkout: it walks
  up from the team dir at most 3 levels and, at each level, scans the
  *immediate child directories* for a `pyproject.toml` whose `[project] name`
  is `tigerharness`, first hit in sorted order winning. When nothing matches it
  writes a **placeholder**, never a silent guess — so check this file and fix
  the placeholder if the auto-capture missed.
- **`configs/tiger-memory.defaults.yaml`** — team-wide memory defaults.
  Stage 8.
- **`configs/workflow.yaml`** — optional, not scaffolded. Only to override
  `compile_personas`.

**Verify** the project path actually resolved rather than landing as a
placeholder:

```bash
cat configs/repos.yaml
```

Expected: a real absolute path to the project the team works on. A
placeholder here means every persona instruction that says "the project" will
point nowhere.

### Stage 3 — Persona prompts and the charter

`init` writes a **template** `prompt.md` per persona and a `charter/README.md`
seeded with TODO markers (the `--goal` text lands in the charter's Mission
section). Both are meant to be filled in — by the persona itself, in its
first interactive session, on the subscription rail.

**Operator supplies:** what the team is for; what each persona is good at;
the boundaries (which directories a persona may write to).

This is the one stage with no mechanical verification — a template prompt and
a finished prompt are both valid markdown. The check is human: open
`personas/<Persona>/prompt.md` and confirm it describes a specific role with
explicit read/write boundaries, not the scaffold's placeholder text. A team
whose prompts are still templates will *run*, and will behave generically.

### Stage 4 — Slack app and bridge lane registration

**Operator supplies:** a Slack app with Socket Mode enabled, its two tokens,
and the Slack user id(s) allowed to talk to the team.

There is **one** bridge process and it serves 1..N teams as *lanes*. The
former single-tenant mode (tokens read straight from the process env) was
**removed**; startup now fails fast with a migration pointer rather than
silently running the wrong deployment shape. So the index below is required,
not optional.

#### Two files

1. **The top-level index** — `slack-bridge.yaml` at the teams root, holding a
   `lanes:` list. `init --multi-team` creates it and appends your team;
   `_append_lane_to_slack_bridge_index` keeps that idempotent.
2. **The per-lane fragment** — `<team>/configs/slack-bridge.yaml`. Keys read
   by `multi.load_multi`:

| Key | Required | Meaning |
|---|---|---|
| `default_persona` | yes (legacy alias `persona`) | who answers an unaddressed DM |
| `state_dir` | yes | resolves to the lane's `threads.json` |
| `env` | no (default `configs/.env`) | the lane's env file |
| `agent_cwd` | no (default `.`) | where the agent session starts |
| `allowed_user_ids` | no | falls back to `SLACK_ALLOWED_USER_IDS` in the env file |
| `tiger_memory_trigger` | no (default `rebuild`) | valid values in `VALID_TIGER_MEMORY_TRIGGERS`: `rebuild`, `off` |
| `idle_compact` | no | opt in to bridge idle compaction |

#### The lane env file

`SLACK_APP_TOKEN` (must start with `xapp-`) and `SLACK_BOT_TOKEN` (must start
with `xoxb-`) go in the lane's `.env`. `multi.load_multi` validates both
prefixes and refuses to start otherwise. Two lanes may **not** share an
`SLACK_APP_TOKEN` — each lane needs a distinct Slack app.

Allowlist: `allowed_user_ids` in the fragment, else `SLACK_ALLOWED_USER_IDS`
in the env file (comma/whitespace separated). `notify` also accepts the legacy
spelling `ALLOWED_SLACK_USER_IDS`, but `SLACK_ALLOWED_USER_IDS` is canonical —
use it.

> **Trap — lane `env_vars` vs the process environment.** `_load_env_file`
> parses the lane's env file **without** writing into `os.environ`. That
> isolation is deliberate (it is what lets lanes differ), and it is why a
> variable exported in your shell can appear to work while the bridge, which
> reads the lane, never sees it. **Set lane configuration in the lane's env
> file, not in your shell.** A feature configured in the wrong place ships
> *inert* — no error, no effect.

#### Point the bridge at the index and run it

`TIGERHARNESS_BRIDGES_CONFIG` must point at the `slack-bridge.yaml` index;
`__main__.py` raises `SystemExit` when it is unset.

```bash
TIGERHARNESS_BRIDGES_CONFIG=<teams-root>/slack-bridge.yaml \
  uv run python -m tigerharness.slack_bridge
```

**Verify:** each lane logs its startup tagged `lane=<name>`. Then DM the bot
from an allowlisted account and confirm a reply. A DM that is silently ignored
almost always means the sending user is not in the allowlist.

For a long-running deployment, render a systemd user unit rather than
hand-writing one:

```bash
uv run tigerharness slack-bridge gen-service
```

Regenerate through this command after any lane change — a hand-edited unit
drifts from the index.

### Stage 5 — The ops-log channel and turn-progress heartbeats

**This is Inkstone symptom #2, and it is the single most silent step in the
runbook.** Configured-and-quiet and broken-and-quiet look identical from
Slack. Do not skip the verification.

**Operator supplies:** a Slack channel id for the ops log, and the bot must be
invited to that channel.

During a long Slack turn the bridge posts a progress heartbeat to an ops-log
channel — a parent message, then updates every `DEFAULT_INTERVAL_S` seconds,
which is **300.0** (five minutes). It posts to a **channel, never a DM**:
`_notifier_for_token` builds the notifier with an empty `target_user_id`, and
`TurnProgress._post` refuses to post when the channel is falsy, so the DM
target is unreachable by construction rather than by convention.

#### Two accepted names

`resolve_progress_channel` scans `CHANNEL_ENV_VARS` in order and takes the
first **non-empty** value:

1. `TIGERHARNESS_BRIDGE_PROGRESS_CHANNEL`
2. `SLACK_NOTIFY_CHANNEL`

Both are accepted, so a team can already be configured under the second name.
Empty does **not** count as set — that is deliberate: an or-chain would let
`TIGERHARNESS_BRIDGE_PROGRESS_CHANNEL=""` win and silently disable the
feature, a state the source itself calls *indistinguishable from "not
configured."* Set it in the **lane's env file** (Stage 4's trap).

There is deliberately **no DM fallback**. An unset channel does not reroute
anywhere; it simply turns the feature off.

**Verify — the check that can actually fail.** `build_turn_progress` logs one
of two *different* INFO lines on the first dispatch after a restart. Restart
the bridge, send one message, and read the log:

- **Configured:**
  `progress: turn heartbeats ARMED for <lane> -> channel <id> (first pulse after 300s, then every 300s)`
- **Not configured:**
  `progress: slack creds present but no ops-log channel (set TIGERHARNESS_BRIDGE_PROGRESS_CHANNEL); turn progress heartbeats are off`

Grep the bridge log for `heartbeats ARMED`. Its presence is proof; its absence
with the second line present is a precise diagnosis. This exists exactly so
you do not have to wait five minutes to learn the feature is off —
`_announce_ready` fires **once per lane** per process, guarded by `_ANNOUNCED`
so it does not log on every turn.

> Requires `TIGERHARNESS_LOG_LEVEL=INFO` (Stage 0). At a coarser level both
> lines are invisible and you are back to guessing.

### Stage 6 — autodrive

**This is Inkstone symptom #1: a team that does not know this exists never
self-drives.** Autodrive is a detached daemon that periodically fires "drive
the journal."

It is **off unless you opt in**, and it is the deliberate, Operator-authorized
exception to the journal's human-triggered rule — safe only while `claude -p`
bills the subscription rather than API tokens.

Settings resolve **flag > process env > the team's `configs/.env` > built-in
default**. The env-var name constants live in `autodrive/settings.py`; the
interval default and floor live in `autodrive/runner.py`:

| Constant | Env var | Default |
|---|---|---|
| `AUTOSTART_ENV` | `TIGERHARNESS_AUTODRIVE_AUTOSTART` | unset = **off** |
| `INTERVAL_ENV` | `TIGERHARNESS_AUTODRIVE_INTERVAL` | `DEFAULT_INTERVAL_SECONDS` = 600.0 |
| `MAX_BUDGET_ENV` | `TIGERHARNESS_AUTODRIVE_MAX_BUDGET` | unset = uncapped |
| `DRIVER_ENV` | `TIGERHARNESS_AUTODRIVE_DRIVER` | the team's `default_persona` |
| `NOTIFY_ENV` | `TIGERHARNESS_AUTODRIVE_NOTIFY` | `DEFAULT_NOTIFY` = `slack` |
| `NOTIFY_CHANNEL_ENV` | `TIGERHARNESS_AUTODRIVE_NOTIFY_CHANNEL` | falls back to `SLACK_NOTIFY_CHANNEL_ENV`, then Operator DM |

`MIN_INTERVAL_SECONDS` is 60.0 — a floor that catches an interval typo.
`Settings.notify_channel` accepts `DM_SENTINEL` (`"dm"`, case-insensitive) at
any layer to force the Operator DM.

Set `TIGERHARNESS_AUTODRIVE_AUTOSTART` in the team's `configs/.env` to make
the queue self-driving: `ensure_running` is called after `journal new`,
`defer`, and `materialize` write to the queue. It is idempotent, never fatal,
and opt-in — scheduling work starts the daemon, draining the queue stops it,
and steady state is *no process running*.

**Consider setting `--max-budget`.** Uncapped is a legitimate choice, but it
should be a decision, not an accident.

Manual control:

```bash
uv run tigerharness autodrive start --interval 600 --driver <Persona>
uv run tigerharness autodrive status
uv run tigerharness autodrive stop
```

The **stop brake** is cooperative-then-forceful: `stop` sets `stop_requested`
in the state file and signals the daemon's process group with `SIGTERM`.
State lives in `.autodrive.json` under the journal root (alongside
`.autodrive.lock` and `.autodrive.log`), carrying `pid`, `in_flight`,
`fire_count`, `tick_count`, `stop_requested`, `last_stop_reason`,
`last_cost_usd`, and `last_error`.

`TURN_SCOPED_ENV_VARS` — `TIGERHARNESS_SLACK_THREAD_TS` and
`TIGERHARNESS_SLACK_CHANNEL` — are scrubbed at the daemon spawn boundary, so a
drive never inherits a stale Slack thread marker. You do not set these; the
bridge sets them per turn.

**Verify:**

```bash
uv run tigerharness autodrive status
```

Expected: a state report naming the interval and driver. This is
state-revealing by design — a team that never enabled autodrive gets a
*different string*, not an absence, so "we thought it was on" cannot survive
this check. Confirm the reported driver is a real persona in
`configs/personas.yaml`.

### Stage 7 — Journal scaffold and the drive rail

**This is the other half of Inkstone symptom #1: work never entered the
queue because nobody knew how to put it there.**

The journal is the file-based execution backend. Its root resolves via
`default_journal_root`, in order: `TIGERHARNESS_JOURNAL_DIR`; else
`<cwd>/journal/` when the cwd is a team root; else `$XDG_STATE_HOME/
tigerharness-journal`; else `~/.local/state/tigerharness-journal`.

**Run journal commands from the team root.** From anywhere else the root
resolves to the XDG fallback and the work lands in a journal nobody reads.
`resolve_team_journal_root` raises `JournalRootRefusal` rather than silently
falling back when a team context is supplied — a loud failure you should
prefer, so pass `--team` / `--team-dir`, or set `TIGERHARNESS_JOURNAL_DIR`.
`TIGERHARNESS_TEAMS_DIR` overrides the teams directory in `resolve_team_root`.

#### Getting work into the queue

```bash
# single-persona task from a PRD
uv run tigerharness journal new --kind task --persona <Persona> --prd <brief.md>

# multi-persona workflow from a playbook
uv run tigerharness journal new --kind workflow --playbook <name> \
    --task-brief "<the ask>"
```

Other `new` flags: `--brief-file`, `--captain`, `--team`, `--title`, `--slug`,
`--max-sessions`, `--early-exit`, `--autonomy` (`ask` | `judgement`).

`--kind workflow` reads `<team-root>/workflow/<playbook>.md` and **exits with
an error if that file does not exist** — `init` does not create `workflow/`,
so create it and add at least one playbook before promising the team workflow
mode. Playbook names are validated against a conservative pattern, so a path
like `../../etc` is rejected at CLI time.

Before any disk write, `new_workflow_task` calls `validate_personas` on the
union of the compile trio and the persona names referenced in the playbook,
raising `MissingPersonaError` when any is missing. Nothing is scaffolded on
failure. Pre-flight it:

```bash
uv run tigerharness journal validate-personas <Team>
```

Note this is a **`journal` sub-verb**, not a top-level command.

`journal defer` is the cheap Slack-side half: it parks the Operator's
conversation **verbatim** in `deferred/` with no playbook read, no compile and
no model call. A later drive turns it into a real task with `journal
materialize <deferred-id>`.

#### The drive rail — who may drive

A **Slack-triggered session may schedule journal work but must never drive
it**; driving belongs to the subscription rail. `journal claim` enforces this
mechanically: it refuses when `TIGERHARNESS_SLACK_THREAD_TS` is set in the
environment unless `--allow-api-drive` is passed. Autodrive is the sanctioned
exception (Stage 6).

Driving verbs: `sweep` (archive done, classify in-progress as idle / busy /
crashed), `claim` (`--driver`, `--allow-api-drive`), `release` (`--state`,
`--output`, `--next-action`, `--question`), `step-done` (`--task`, `--step`,
`--verdict`, `--output`) for workflow graph walks, and `answer` to reopen a
task parked on an Operator question. `--driver` is what attributes work to a
persona's memory store, so a drive that omits it produces no per-persona
record.

The sweep's heartbeat staleness threshold is
`TIGERHARNESS_JOURNAL_STUCK_TIMEOUT`, defaulting to
`DEFAULT_STUCK_TIMEOUT_SEC` = 1800 seconds (30 minutes).

**Verify** — scaffold a real first task, then confirm the queue sees it:

```bash
uv run tigerharness journal new --kind task --persona <Persona> --prd <brief.md>
uv run tigerharness journal sweep
```

Expected: the sweep reports **1 pending** and lists your task as actionable. A
sweep that reports `0 pending` after a successful `new` means the two commands
resolved *different journal roots* — re-read the resolution order above. This
is the check that would have caught "work never entered the queue."

### Stage 8 — tiger-memory bootstrap and the sweep

Per-persona memory is what lets a persona carry knowledge across sessions.

`init` already did most of this: `add_persona` writes
`memories/<Persona>/tiger-memory.config.yaml`, and `_auto_init_tiger_memory`
then runs `tiger-memory init` as a subprocess to create the store and the
initial briefing. That subprocess is **non-fatal on failure** — so a memory
store can be quietly missing on a team that otherwise looks fine. Verify
rather than assume.

Every `tiger-memory` command needs a config: pass `--config <path>` or set
`TIGER_MEMORY_CONFIG`. Note the prefix is `TIGER_MEMORY_`, **not**
`TIGERHARNESS_` — a `TIGERHARNESS_*` grep will never surface it. Unset and
unpassed raises `ConfigError`.

The team-wide sweep is gated so that firing it often is cheap. `sweep-plan`
claims the sweep under a soft lease; the gate is a staleness floor
(`sweep.floor_hours`, default `DEFAULT_STALENESS_FLOOR_HOURS` = 24.0), a
per-wake persona cap (`sweep.max_personas`, default `DEFAULT_MAX_PERSONAS` =
3) and a lease (`sweep.lease_seconds`, default `DEFAULT_LEASE_SECONDS` =
1800.0). Each claim ends in `sweep-complete` (advances the team watermark) or
`sweep-release` (drops the claim without advancing it). State lives in
`SWEEP_STATE_FILENAME` = `.tiger-memory-sweep.json`.

`rebuild` regenerates a persona's session-start briefing — `README.md`,
`MANIFEST.md`, `UNPROCESSED.md`, `must_remember.md`, `skill_index.md`,
`topic_index.md`, plus `skills/` and `topics/` detail files. It is **pure
local computation with no model or API call**, so it is always safe to run
standalone to refresh a briefing that has drifted behind the store.

**Verify:**

```bash
uv run tigerharness tiger-memory --config memories/<Persona>/tiger-memory.config.yaml check
uv run tigerharness tiger-memory --config memories/<Persona>/tiger-memory.config.yaml doctor
```

Expected: `check` exits 0 (all three stores parse). `doctor` prints a
team-wide health table and **exits 1 if anything is flagged** — so use its
exit status, not just its output. `check --fix` repairs mechanical drift and
quarantines anything it cannot repair to `<store>.rejected.md`.

Then confirm the briefing a persona actually reads at session start exists:

```bash
ls memories/<Persona>/briefing/
```

Expected: `README.md`, `must_remember.md`, `skill_index.md`, `topic_index.md`,
`MANIFEST.md`, `UNPROCESSED.md`, and the `skills/` + `topics/` directories. If
this directory is missing, `_auto_init_tiger_memory` failed silently — run
`tiger-memory init` by hand.

### Stage 9 — Traps that fail quietly

Five ways a team ships inert. The first two are the Inkstone failures; the
rest are the same class.

1. **The ops-log channel is unset.** No error, no heartbeats. Two accepted
   names (`TIGERHARNESS_BRIDGE_PROGRESS_CHANNEL`, `SLACK_NOTIFY_CHANNEL`), and
   empty does not count as set. → Stage 5's `heartbeats ARMED` log line.
2. **Autodrive was never enabled.** The queue fills and nothing drives it.
   Autostart is off by default. → Stage 6's `autodrive status`.
3. **Lane `env_vars` vs the process environment.** `_load_env_file` does not
   touch `os.environ`. A shell export is invisible to the bridge; set lane
   config in the lane's env file. → Stage 4.
4. **`SSL_CERT_FILE` and `TIGERHARNESS_SLACK_ENV` are needed together for
   `notify`.** `_ssl_context` reads `SSL_CERT_FILE`;
   `_load_slack_bridge_dotenv` reads `TIGERHARNESS_SLACK_ENV` to find the
   `.env` when it is not in the default location. Set one without the other
   and `notify` degrades with a **WARNING** — visible in logs, invisible to a
   caller who only checks the exit status. → verify by sending yourself one
   real message: `uv run tigerharness slack-bridge text "setup check"`.
5. **Scaffold-time-only writes never refresh an existing team.** `init` writes
   with `_write_if_missing`, so a team scaffolded months ago does not gain
   newly-shipped files. `init --refresh` is the escape hatch: it installs
   missing bundled skills, refreshes any skill still byte-identical to a
   previously-shipped version, **leaves hand-edited skills untouched**, and
   appends missing lines to `.gitignore` (append-only — nothing is removed or
   rewritten). It does **not** touch `personas.yaml`, `.env`, prompts, the
   charter, or an existing `.claude/settings.json`. `--refresh-skills` is a
   retained alias that now syncs `.gitignore` too. Idempotent:

   ```bash
   uv run tigerharness init --refresh --team <Team>
   ```

   Expected when current: `Nothing to do -- bundled skills and .gitignore are
   already up to date`.

### Stage 10 — Is the team actually alive?

Run all seven from the team root. Every one of them can fail.

| # | Check | Command | Alive looks like |
|---|---|---|---|
| 1 | Team resolves | `cat configs/personas.yaml` | your personas listed, `default_persona` set |
| 2 | Project path real | `cat configs/repos.yaml` | an absolute path, not a placeholder |
| 3 | Bridge replies | DM the bot from an allowlisted account | a reply in thread |
| 4 | **Ops-log armed** | grep the bridge log for `heartbeats ARMED` | the ARMED line naming your channel |
| 5 | **Autodrive known** | `tigerharness autodrive status` | a state report with interval + a real driver |
| 6 | **Queue reachable** | `tigerharness journal sweep` | counts matching what you scaffolded |
| 7 | Memory healthy | `tiger-memory ... doctor` | exit status 0 |

Checks 4, 5 and 6 are the three that were silently false on Inkstone. If you
verify nothing else, verify those.

### Appendix — environment variable inventory

Audited from source in both directions. A forward grep for
`TIGERHARNESS_[A-Z0-9_]*` yields 23 tokens, but one of them —
`TIGERHARNESS_IDLE_COMPACT_` — is **not a variable**: it is a string literal
split across two source lines inside a log message in `idle_compact.py`, and
it is the only token of the 23 with zero exact-quoted occurrences. The real
count is **22**. A forward grep alone also **under-reports**, because
setup-critical variables do not carry the prefix at all.

There are four read mechanisms, and a table built from only the first
silently misses the rest: a direct `os.environ.get("NAME")`; a **lane's env
dict** (`e.get("NAME")` in `idle_compact.py`, `env.get(...)` in `multi.py`);
module-level constants (`INTERVAL_ENV = "TIGERHARNESS_..."` in
`autodrive/settings.py`, resolved against `self.env`, which defaults to
`os.environ`); and tuples of candidates (`CHANNEL_ENV_VARS` in `progress.py`).

**Day-one verdicts.** *Required* = set it or the feature is off or broken.
*Optional* = a working default exists. *Set for you* = written by the system;
do not set it by hand.

| Variable | Read in | Day-one |
|---|---|---|
| `SLACK_APP_TOKEN` | `multi.py` | **required** for the bridge (`xapp-`) |
| `SLACK_BOT_TOKEN` | `multi.py`, `notify.py` | **required** for any Slack |
| `TIGERHARNESS_BRIDGES_CONFIG` | `slack_bridge/__main__.py` | **required** to run the bridge |
| `SLACK_ALLOWED_USER_IDS` | `multi.py`, `notify.py` | **required** unless `allowed_user_ids` is in the fragment |
| `TIGERHARNESS_BRIDGE_PROGRESS_CHANNEL` | `progress.py` | **required for heartbeats** (Stage 5) |
| `SLACK_NOTIFY_CHANNEL` | `progress.py`, `autodrive/settings.py` | second accepted name for the ops-log channel |
| `SSL_CERT_FILE` | `notify.py` | **required for `notify`** on hosts needing an explicit CA |
| `TIGERHARNESS_SLACK_ENV` | `notify.py` | **required** when `.env` is not in the default location |
| `TIGERHARNESS_AUTODRIVE_AUTOSTART` | `autodrive/settings.py` | **required to self-drive**; off when unset |
| `TIGERHARNESS_AUTODRIVE_INTERVAL` | `autodrive/settings.py` | optional; default 600.0s, floor 60.0s |
| `TIGERHARNESS_AUTODRIVE_MAX_BUDGET` | `autodrive/settings.py` | optional; uncapped when unset |
| `TIGERHARNESS_AUTODRIVE_DRIVER` | `autodrive/settings.py` | optional; defaults to `default_persona` |
| `TIGERHARNESS_AUTODRIVE_NOTIFY` | `autodrive/settings.py` | optional; default `slack` |
| `TIGERHARNESS_AUTODRIVE_NOTIFY_CHANNEL` | `autodrive/settings.py` | optional; → `SLACK_NOTIFY_CHANNEL` → DM |
| `TIGERHARNESS_JOURNAL_DIR` | `journal/paths.py` | optional; pins the journal root |
| `TIGERHARNESS_TEAMS_DIR` | `journal/scaffold.py` | optional; overrides teams dir in `resolve_team_root` |
| `TIGERHARNESS_JOURNAL_STUCK_TIMEOUT` | `journal/sweep.py` | optional; default 1800s |
| `TIGER_MEMORY_CONFIG` | `tiger_memory/config.py` | optional; else pass `--config` |
| `TIGERHARNESS_LOG_LEVEL` | `_logging.py` | optional; **set `INFO` during setup** |
| `TIGERHARNESS_IDLE_COMPACT` | `idle_compact.py` | optional; opt-in, read from the **lane** |
| `TIGERHARNESS_IDLE_COMPACT_JOURNAL` | `idle_compact.py` | required *if* idle-compact is on |
| `TIGERHARNESS_IDLE_COMPACT_THRESHOLD` | `idle_compact.py` | optional; default 0.30 |
| `TIGERHARNESS_IDLE_COMPACT_WINDOW` | `idle_compact.py` | optional; default 200000 |
| `TIGERHARNESS_ATTACHMENT_DIR` | `downloader.py` | optional; overrides attachment dir |
| `TIGERHARNESS_SLACK_STATE_DIR` | `persistence.py` | optional; overrides `threads.json` location |
| `TIGERHARNESS_SLACK_CHANNEL` | `bridge.py`, `autodrive/cli.py` | **set for you** (per turn; scrubbed at daemon spawn) |
| `TIGERHARNESS_SLACK_THREAD_TS` | `bridge.py`, `autodrive/cli.py` | **set for you** (per turn; gates the drive rail) |
| `TIGERHARNESS_PERSONAS_CONFIG` | written by `init.py` | **set for you** in `.claude/settings.json` |
| `SLACK_CEO_USER_ID` | `notify.py` | optional; DM target override |
| `ALLOWED_SLACK_USER_IDS` | `notify.py` | legacy alias; prefer `SLACK_ALLOWED_USER_IDS` |
| `TIGER_MEMORY_CLI` | `multi.py` | optional; lane's memory CLI override |
| `XDG_STATE_HOME` | `journal/paths.py`, `persistence.py` | environment-supplied; read, never set by you |

## See also

- [README](../README.md) — install and the team layout.
- [slack-bridge.md](slack-bridge.md) — lanes, ops-log, idle compaction.
- [autodrive.md](autodrive.md) + [autodrive-notifications.md](autodrive-notifications.md).
- [journal.md](journal.md) and [journal-workflow-mode.md](journal-workflow-mode.md).
- [subscription-backend.md](subscription-backend.md) — rails and billing.
- [tiger-memory.md](tiger-memory.md) + [tiger-memory-sweep-protocol.md](tiger-memory-sweep-protocol.md).

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

**You also need at least one vendor CLI on `PATH`**, because that is what
actually runs your personas. Since 0.6.0 each persona runs on a *vendor*
(ADR 0011): `claude` needs the `claude` CLI, `chatgpt` needs `codex`
(`vendors.py:93-96`). `init` warns when the vendor you pick has no CLI
installed (`init.py:2022-2029`) — it does not fail, so the team scaffolds fine
and then cannot drive anything. Install the CLI for whichever vendor you name
in Stage 1, and both if the team will be mixed.

The extras are declared under `[project.optional-dependencies]` in
`pyproject.toml`:

| Extra | Turns on |
|---|---|
| `slack` | the bridge, `notify`, ops-log heartbeats (Stages 4–5) |
| `memory` | **pyyaml — effectively mandatory, see below** |
| `anthropic` | the `anthropic_sdk` agent backend only |
| `all` | all of the above |

**`memory` is not a Stage 8 extra.** Its only content is pyyaml
(`pyproject.toml:32-34`), and pyyaml gates far more than tiger-memory's config:

- per-persona vendor/model resolution — without it `read_personas_yaml` logs a
  warning and returns `None`, so **every persona silently resolves to the
  built-in vendor `claude`** (`vendors.py:200-208`);
- the multi-lane bridge loader, which does not degrade: `_load_yaml` raises
  `SystemExit` telling you to install it (`slack_bridge/multi.py:100-104`). That
  blocks **Stage 4**, not Stage 8;
- `notify`'s config read (`slack_bridge/notify.py:119`), the journal's roster
  and alias reads (`journal/scaffold.py:438` and its siblings), and workflow
  compilation (`journal/compile_cli.py:784`).

Treat it as required.

**`anthropic` is narrower than it looks.** Four backends are registered
(`agent_sdk/factory.py:98-101`): `claude_p`, `codex_exec`, `anthropic_sdk`, and
`openai_sdk` (a stub). Only `anthropic_sdk` needs this extra. **`codex_exec` —
the whole `chatgpt` half of per-persona vendors — is stdlib-only and needs no
extra at all**, just the `codex` CLI on `PATH`; `pyproject.toml:35-38` says so
in its own comment. And nothing on the shipped rails uses `anthropic_sdk`: the
bridge (`slack_bridge/bridge.py:1361`), idle compaction (`idle_compact.py:496`)
and even the summarizer *named* "anthropic"
(`tiger_memory/summarizers/anthropic.py:79`) all call `get_backend("claude_p")`.
Install it only if you are writing code against the SDK backend yourself.

A team that will use Slack **and** memory wants `[all]`, or at minimum
`[slack,memory]`. Install bare and Stages 4, 5 and 8 fail later, at import
time, instead of here.

The Operator decides where the team lives. Two roots matter and they are
**not** the same directory:

- the **project checkout** (the TigerHarness source, if you are running from
  a clone), and
- the **teams root** — the parent directory your team folders sit in.

Install into the teams root. The README's
[Installation](../README.md#installation) section carries all three
strategies (scoped to one folder, global CLI, or from a clone); the scoped
form, which is the one that matches this runbook's layout, is:

```bash
mkdir -p <teams-root> && cd <teams-root>
uv init --bare
uv add 'tigerharness[all]'
```

Then confirm the CLI resolves:

```bash
uv run tigerharness --help
```

Expected: the sub-command list — `init`, `dismiss`, `tiger-memory (tm)`,
`slack-bridge (sb)`, `journal (j)`, `autodrive (ad)`. If `tigerharness` is
not found, nothing below will work; fix this first.

That list is the whole of `_usage()` (`cli.py:62-73`), and it is complete for
what it claims — but the dispatcher behind it is a hand-rolled string match
(`cli.py:26-59`), not argparse, so it accepts a few spellings `--help` never
mentions: underscore aliases `tiger_memory` and `slack_bridge` (`cli.py:32`,
`:41`), and a sub-dispatch hidden under `slack-bridge` that routes
`gen-service` (Stage 5) and `compact-idle` to different modules before falling
through to the notify CLI (`cli.py:45-50`). Anything else exits **2** with
`unknown command:` and the usage block.

**Optional:** set `TIGERHARNESS_LOG_LEVEL` (read in `_logging.py`, valid
values `CRITICAL` / `ERROR` / `WARNING` / `INFO` / `DEBUG`) to `INFO` for the
setup session. It governs the CLIs that route through `configure_cli_logging`.

**It does not affect the bridge**, and you do not need it for Stage 5.
`slack_bridge/__main__.py:83` calls
`logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)` — a
hardcoded INFO with `force=True`, so the daemon ignores the variable entirely
and `_logging.py` says as much: the bridge "keeps its own richer handler setup
in `slack_bridge/__main__.py` (journald-aware)". Stage 5's ops-log readiness
lines are therefore **always visible** in the bridge log, whatever this
variable is set to.

### Stage 1 — `tigerharness init`

`init` creates a persona inside a team folder, creating the team scaffold
first if it does not exist.

**Operator supplies:** the team name, the first persona's name, the team's
default model vendor, optionally a default model id, and the team's
one-sentence goal.

**On a NEW team, interactively, two questions come before the goal** — added in
0.6.0 with per-persona vendors (ADR 0011), and they are not in this runbook's
older transcripts:

1. `Which model vendor should this team use by default?` — a menu whose
   options carry literal backticks, ``claude (runs `claude`)`` /
   ``chatgpt (runs `codex`)``, default the first (`init.py:1979-1986`).
2. ``Default <vendor> model id (Enter for the `<cli>` CLI's own default)`` —
   free text, Enter accepts the vendor CLI's default (`init.py:1989-1996`).

Both are skipped by `--vendor` / `--model`, and both are skipped entirely on an
**existing** team (they are gated on `is_new_team`), where `init` instead prints
a stderr note telling you to edit `configs/personas.yaml`
(`init.py:1971-1978`).

```bash
cd <teams-root>
uv run tigerharness init --team <Team> --persona <Persona> \
    --goal "<one sentence — why this team exists>"
```

#### Where it creates things — read this before you run it

`init`'s search root is `--dir`, which **defaults to the current directory**.
What happens next depends entirely on which flags you passed, and the trap is
that **the form this runbook prescribes above is the dangerous one.**

`discover_teams` is consulted in exactly one case: when `--team` *and*
`--team-dir` are both absent (`init.py:1869-1870`). Then you get the team
picker, and because it treats any directory containing
`configs/personas.yaml` as a team, a bare interactive `init` run **from inside
an existing team folder lists that team first, at `default_idx=0`**, and
choosing it sets `final_team_dir = existing[idx]` (`init.py:1880-1883`) — it
**cleanly extends** the team you are standing in. That path is safe.

The nesting comes from `--team`. With `--team <Team>` and no `--team-dir`,
`discover_teams` is never called at all; the code takes
`final_team_dir = (root / team).resolve()` (`init.py:1903`). Run that from
inside `<Team>/` and `root` is `<Team>/`, so you get **`<Team>/<Team>/`** — a
nested team with its own `configs/personas.yaml` listing only the new persona,
while the outer team's roster stays exactly as it was.

That shape is the reason to care. The wreckage looks plausible: a directory
tree that reads like a team, a persona that resolves nowhere the Operator is
looking, and an outer roster that is *silently missing someone*. Nothing
errors. If you have already done it, the tell is a team directory whose name
appears twice in the path.

**So: run the `--team` form from the teams root (the parent), or pass
`--team-dir` explicitly.** `TIGERHARNESS_TEAMS_DIR` does not protect you here
— it is read by the journal, not by `init`.

#### The flags that matter on day one

| Flag | Effect |
|---|---|
| `--team` | team name; skips the picker |
| `--persona` | persona name; skips the prompt |
| `--team-dir` | explicit team directory (default `<search-root>/<team>`) |
| `--dir` | where to search for existing teams (default `.`) |
| `--goal` | seeds the charter's Mission section with the Operator's words |
| `--vendor` | a **new** team's `default_vendor`: `claude` or `chatgpt`; skips the vendor prompt, and `--yes` takes `claude` (`init.py:2246-2252`) |
| `--model` | a **new** team's `default_model` for that vendor; blank means the vendor CLI's own default (`init.py:2253-2258`) |
| `--refresh-skills` | alias for `--refresh`, kept for older scripts — note it now syncs `.gitignore` too (`init.py:2271-2276`) |
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

**Plus `.agents/skills`, which is easy to miss and load-bearing.**
`_scaffold_claude_dir` also creates the symlink
`<team>/.agents/skills -> ../.claude/skills` (`init.py:1216`, helper
`ensure_agents_skills_link` `init.py:1231-1264`). That symlink is **Codex's
only door to the team's skills** — Claude Code reads `.claude/skills`
directly, Codex reads `.agents/skills`. It can fail quietly: if something
non-symlink already sits at that path, or the symlink cannot be created, the
helper logs a warning and returns `None` (`init.py:1247-1253`), and a Codex
persona then simply has no skills. Stage 9's `--refresh` recreates it.

**And in multi-team mode, two more files.** When the top-level
`slack-bridge.yaml` index exists, `_maybe_register_slack_bridge_lane`
(`init.py:1717-1748`) writes the per-team fragment
`configs/slack-bridge.yaml` and appends the team to that index — both
idempotent, and both skipped entirely under `--no-multi-team`.

**Know these by name.** The bundled skills are the team's capability surface,
and a team that does not know a skill exists never uses it — that is Inkstone
symptom #1 in one sentence. `_CURRENT_SKILL_HASHES` is the manifest; it ships
**seven**:

| Skill | What it is for |
|---|---|
| `drive-journal` | the driver — claim a task, work it, cascade the queue (Stage 7) |
| `journal-new` | schedule a task or workflow into the queue (Stage 7) |
| `journal-autodrive` | start / stop / inspect the autodrive daemon (Stage 6) |
| `sweep-memory` | run the team-wide memory sweep (Stage 8) |
| `slack-notify` | send a proactive Slack message or file (Stage 4) |
| `workflow-append-steps` | extend a compiled workflow's step graph |
| `tigerharness-basics` | orientation for a persona new to the harness |

Read them in `.claude/skills/<name>/SKILL.md` after `init`. Stage 9's
`--refresh` keeps them current without clobbering hand edits.

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

Expected: `personas.yaml`, `repos.yaml`, `tiger-memory.defaults.yaml`, `.env`
— **and `slack-bridge.yaml`**, because this runbook told you to say yes to
`--multi-team` and that writes the per-team lane fragment as well. Your
persona should be listed in `personas.yaml`, and on a new team so should
`default_vendor` / `default_model` (Stage 2). If `personas.yaml` is missing,
the directory is not a team and every later `journal` command will resolve
somewhere else. If `slack-bridge.yaml` is missing but you asked for
`--multi-team`, the top-level index did not exist when `init` ran — Stage 4
explains how to recover.

### Stage 2 — `configs/`

**Up to six files, up to five of which `init` wrote for you.** The count
depends on your flags: `configs/.env` is written unless `--no-slack`
(`init.py:1443-1447`), and `configs/slack-bridge.yaml` is written on the
multi-team path (Stage 1). Only `configs/workflow.yaml` is never scaffolded.

- **`configs/personas.yaml`** — the roster, plus the `default_persona` field
  the session bootstrap reads. Each entry may carry an `aliases:` list; the
  journal resolves persona names through that alias map, so an alias is the
  supported way to let the Operator address a persona by nickname.

  **It also carries the team's vendor policy, and this is the file where
  ADR 0011 lives.** A new team's header gets a `default_vendor` /
  `default_model` block (`_PERSONAS_YAML_VENDOR_BLOCK`, `init.py:519-532`), and
  every persona entry gets two commented hints,
  `# vendor: default   # claude | chatgpt | default` and `# model: default`
  (`_PERSONA_ENTRY`, `init.py:551-560`). Uncomment them to move one persona off
  the team default. The rules, read in `vendors.py`:

  - `default_vendor` is `claude` unless you said otherwise
    (`DEFAULT_VENDOR`, `vendors.py:85`), and a vendor maps to a backend:
    `{"claude": "claude_p", "chatgpt": "codex_exec"}` (`vendors.py:64-67`).
  - `""`, `default`, `inherit` and `team` all mean "not set, inherit"
    (`_INHERIT`, `vendors.py:90`).
  - **A persona that switches vendor and names no model does NOT inherit
    `default_model`** (`vendors.py:316-319`) — a model id belongs to one
    vendor, so it would be meaningless on the other. It falls back to that
    vendor CLI's own default.
  - The parse path is `read_personas_yaml` (`vendors.py:183-219`) ->
    `team_default_policy` (`:246-276`) -> `resolve_model_policy` (`:279-325`).

  **Scaffolded for a NEW team only** (`init.py:2013-2021`). On an existing team
  `init` prints a stderr note instead (`init.py:1971-1978`) — so a team created
  before 0.6.0 has no vendor block and you add it by hand.
- **`configs/repos.yaml`** — the path-indirection map, written by
  `_scaffold_repos_yaml`. Prose and config reference paths relative to the
  team root; this file is the single place a machine-specific layout is
  recorded, which is what makes a team folder portable between machines.
  `_detect_project_dir` auto-captures a nearby TigerHarness checkout: it walks
  up from the team dir at most 3 levels and, at each level, scans the
  *immediate child directories* of the **parent**, skipping the directory it
  came from (`init.py:1097`, `:1107`), for a `pyproject.toml` matching
  `re.search(r'^name\s*=\s*"tigerharness"', text, MULTILINE|IGNORECASE)`
  (`init.py:1117-1120`) — first hit in sorted order winning. Two consequences
  worth knowing: the test is **case-insensitive and not scoped to the
  `[project]` table**, so a `name = "TigerHarness"` anywhere in the file
  matches; and because the current directory is excluded, **a checkout sitting
  inside the team folder is never found**. When nothing matches it writes a
  **placeholder**, never a silent guess, *and* prints a stderr hint
  (`init.py:1148-1152`) — a louder signal at run time than the grep-for-`#`
  check below. A captured path is written
  **relative to the team root**, not absolute; that is what makes the team
  folder portable between machines, so a relative value here is correct and
  is not the failure mode.
- **`configs/tiger-memory.defaults.yaml`** — team-wide memory defaults.
  Stage 8.
- **`configs/.env`** — tokens *and* **the team-settings file**. This is not
  just a secrets holder: it is the second layer every `Settings` lookup reads
  after the process environment, so team-wide knobs live here — every
  `TIGERHARNESS_AUTODRIVE_*` value (Stage 6) and
  `TIGERHARNESS_JOURNAL_SLACK_DRIVES`, the ADR 0013 knob that decides whether
  Slack-spawned sessions may drive the journal (Stage 7). Written unless
  `--no-slack`.
- **`configs/slack-bridge.yaml`** — the per-team bridge lane fragment, written
  on the multi-team path. Stage 4.
- **`configs/workflow.yaml`** — optional, not scaffolded. Only to override
  `compile_personas`.

**Verify** the project path actually resolved rather than landing as a
placeholder:

```bash
cat configs/repos.yaml
```

Expected: an **uncommented `project:` key** naming the repo this team works
on, e.g. `project: ../../tigerharness`. The single thing that distinguishes
pass from fail is the leading `#`, because the placeholder is a *commented-out*
line:

```
# project: ../tigerharness  # <- set me: path to the repo this team works on
```

Absolute versus relative is not the signal — relative is the norm. A
still-commented `project:` means every persona instruction that says "the
project" points nowhere.

The file `init` writes is not just that one line: it carries a four-line
comment header and a `team_root: .` key as well (`init.py:1153-1161`), so a
`cat` that shows only `project:` means someone edited it down.

### Stage 3 — Persona prompts and the charter

`init` writes a **template** `prompt.md` per persona and a `charter/README.md`
seeded with TODO markers (the `--goal` text lands in the charter's Mission
section). Both are meant to be filled in — by the persona itself, in its
first interactive session, on the subscription rail.

**Operator supplies:** what the team is for; what each persona is good at;
the boundaries (which directories a persona may write to).

**Prompt *content* has no mechanical check, and cannot have one.** The
scaffolded `_PERSONA_TEMPLATE` is a complete, working prompt — it carries no
`TODO`, no placeholder token, nothing a grep can flag. That is deliberate (a
fresh persona is usable immediately), but it means a template prompt and a
finished prompt are indistinguishable to a machine. The check here is human:
open `personas/<Persona>/prompt.md` and confirm it describes a *specific* role
with explicit read/write boundaries. A team whose prompts are still templates
will *run*, and will behave generically. Nothing will ever tell you.

Two things in this stage **are** checkable, and both are worth running:

**1. The charter still has its TODOs.** `init` seeds `charter/README.md` with
four `TODO` markers; `--goal` replaces exactly one of them (the Mission
blockquote), leaving three:

```bash
grep -c TODO charter/README.md    # 4 = charter untouched; 3 = --goal only; 0 = filled in
```

Any non-zero count means the team's single entry-point doc is still partly
scaffold. This matters more than it looks: `AGENTS.md`/`CLAUDE.md` point every
persona at the charter first, so an unfilled `TODO` is read by every session.

**2. The compile trio may not exist yet — and this bites at first workflow.**
Workflow compilation needs three personas (roles `drafter`/`akagi`/`ayako`).
`init` does **not** write `configs/workflow.yaml`, so `resolve_compile_personas`
falls back to its **built-in defaults** — Anzai / Akagi / Ayako — names your new
team almost certainly does not have. They are defaults, not a hard-coded
requirement: `configs/workflow.yaml` remaps the three roles onto your own
personas, aliases included (Stage 7):

```bash
tigerharness journal validate-personas .
```

On a team scaffolded as `--persona Scout`, that exits **1**:

```
missing prompt.md for: ['Akagi', 'Anzai', 'Ayako'] (under /path/to/Team)
  akagi: Akagi <- MISSING
  ayako: Ayako <- MISSING
  drafter: Anzai <- MISSING
```

It is a **file-existence check only** (`is_file()` and non-zero size), scoped
to the compile trio — it says nothing about prompt quality and does not look at
your other personas. But exit 0 here is the difference between a workflow task
that compiles and one that crashes mid-compile after scaffolding. Fix it either
way: create those three personas, or map the roles onto personas you do have in
`configs/workflow.yaml`:

```yaml
compile_personas:
  drafter: Scout
  akagi:   Scout
  ayako:   Scout
```

Skip this only if the team will never run `kind=workflow` tasks.

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
| `allowed_user_ids` | **required by one of two** — see below | else `SLACK_ALLOWED_USER_IDS` in the env file |
| `tiger_memory_trigger` | no (default `rebuild`) | valid values in `VALID_TIGER_MEMORY_TRIGGERS`: `rebuild`, `off` — mind the YAML 1.1 trap below |
| `idle_compact` | no (**default off when the key is absent**) | bridge idle compaction — `init` scaffolds it `true`; set `false` to opt this lane *out* |

**`allowed_user_ids` is not optional; it is required by one of two routes.**
`None` or `[]` falls back to `SLACK_ALLOWED_USER_IDS` in the lane's env file,
and if *that* is empty too `load_multi` **raises** (`multi.py:374-383`). Note
`init` scaffolds the fragment with `allowed_user_ids: []` and a TODO
(`init.py:934`), which the loader treats exactly as absent — so a freshly
scaffolded lane will not start until you fill one of the two. Whatever you
supply is then validated by `_validate_allowed_user_ids` (`multi.py:142-156`):
a **non-empty list** of non-empty strings, each starting with `U` or `W`.

**`idle_compact` defaults off when the key is absent** — `spec.get(
"idle_compact", False)` (`multi.py:237`) — and `_coerce_flag`
(`multi.py:196-206`) reads **anything it does not recognise as `False`** rather
than erroring, so a typo'd value leaves the lane silently opted out. Only
`true` / `"true"` / `"yes"` / `"on"` / `"1"` turn it on.

**`tiger_memory_trigger` has a YAML 1.1 trap.** A bare `off` parses to the bool
`False`, and the loader deliberately recovers that back to the string `"off"`
because `off` is a real mode; but a truthy bool (`on`, `yes`, `true`) maps to
`"on"`, which is **not** a valid mode, and raises
(`slack_bridge/config.py:39-47`). Quote it if you want to be sure: `"off"`.

#### The lane env file

`SLACK_APP_TOKEN` (must start with `xapp-`) and `SLACK_BOT_TOKEN` (must start
with `xoxb-`) go in the lane's `.env`. `multi.load_multi` validates both
prefixes and refuses to start otherwise. `_check_lane_uniqueness`
(`multi.py:432-448`) then rejects **two** kinds of collision, not one: two
lanes sharing an `SLACK_APP_TOKEN` (Slack rejects two Socket Mode connections
to one app), and two lanes sharing a `state_path` (they would corrupt each
other's `threads.json`). So each lane needs a distinct Slack app **and** a
distinct `state_dir`.

Allowlist: `allowed_user_ids` in the fragment, else `SLACK_ALLOWED_USER_IDS`
in the env file (comma/whitespace separated). `notify` also accepts the legacy
spelling `ALLOWED_SLACK_USER_IDS`, but `SLACK_ALLOWED_USER_IDS` is canonical —
use it.

**Two more variables belong in that file**, and neither is a token:

- `TIGER_MEMORY_CLI` — read straight off the lane's env into the lane config
  (`multi.py:422`).
- `TIGERHARNESS_JOURNAL_SLACK_DRIVES` — ADR 0013's team knob (Stage 7).
  The **bridge** never reads it; the **journal** does, via `Settings.flag`,
  which looks at the process environment and then the team's `configs/.env`.
  Since the lane env file defaults to exactly that path, this is where it
  goes.

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

**Verify:** each lane logs its startup tagged `lane=<name>`. Three lines per
lane are worth reading by name, because together they prove the lane loaded the
config you think it did:

- `lane %r tokens loaded (bot=%s app=%s)` (`multi.py:365-368`) — redacted, so
  its presence is the signal, not its contents.
- `lane %r persona %r runs on %s (%s)` (`multi.py:406-409`) — **one line per
  persona naming the vendor and model it resolved to, and where that came
  from.** This is the ADR 0011 surface a day-one reader needs: if a persona you
  expected on `chatgpt` prints `claude`, its `vendor:` never took effect (or
  pyyaml is missing — Stage 0).
- `lane=%s registered -- cwd=%s personas=%s allowed_users=%s`
  (`slack_bridge/__main__.py:272-277`) — the resolved roster and allowlist.

Then DM the bot from an allowlisted account and confirm a reply. A DM that is
silently ignored almost always means the sending user is not in the
allowlist.

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

#### Where it is read — the lane, not your shell

This is the detail that decides whether the feature works, so follow the
route rather than the name that sounds right.

`multi._progress_channel` reads `PROGRESS_CHANNEL_KEYS` **out of the lane's
own parsed env dict** and hands the result to `TeamBridgeContext`'s
`progress_channel` field, which is passed as `build_turn_progress`'s
`channel=` argument. That argument wins: `build_turn_progress` takes it when
non-empty and only falls back to scanning `os.environ` via
`resolve_progress_channel` when it is `None`.

**That fallback is a single-tenant leftover, and on a multi-lane bridge it
finds nothing.** The single-tenant *entrypoint* was removed (ADR 0009), so on
every deployment you set up from this runbook the lane field is what carries the
channel — `build_turn_progress`'s own docstring is the source of the phrase:
"a reporter that resolved its own config would find nothing on a multi-lane
deployment" (`progress.py:485-487`), repeated in the inline comment at
`:498-501` — *the lane field is the fix and not the fallback*. So a value
exported in your shell reaches nothing, exactly as Stage 4's trap says.

Two precisions, because the shorter version of this claim is wrong in both
directions:

- **It is not "never".** `build_bridge` is still kept as the documented way to
  embed a one-persona bridge (`bridge.py:1349-1362`), and there
  `progress_channel` has no lane to come from, so the fallback does fire. If
  you are embedding rather than running the multi-lane daemon, the process
  environment is your route.
- **The fallback is not a plain `os.environ` scan.** `resolve_progress_channel`
  first calls `_load_slack_bridge_dotenv` (`progress.py:411`), which **writes
  into `os.environ`** from the first existing file among
  `$TIGERHARNESS_SLACK_ENV`, `./.env`, `./configs/.env`, and the package
  parent's `.env` (`notify.py:37-66`), skipping keys already set. So "the
  process environment" here can include a file you did not know was being
  read — which is a second way a channel can appear to work in one directory
  and not another.

#### Two accepted names

`PROGRESS_CHANNEL_KEYS` and `CHANNEL_ENV_VARS` are the same tuple — the lane
reader imports it from `progress` so the two paths can never drift. Either
name works, in this order, first **non-empty** value winning:

1. `TIGERHARNESS_BRIDGE_PROGRESS_CHANNEL`
2. `SLACK_NOTIFY_CHANNEL`

Both are accepted, so a team can already be configured under the second name.
Empty does **not** count as set — that is deliberate: an or-chain would let
`TIGERHARNESS_BRIDGE_PROGRESS_CHANNEL=""` win and silently disable the feature,
a state `resolve_progress_channel`'s docstring calls *indistinguishable from
"not configured"* (`progress.py:405-410`). The same rule applies one level up:
`build_turn_progress` computes `resolved = (channel or "").strip() or None`
(`progress.py:502`), so a lane whose `progress_channel` is the **empty string**
takes the fallback exactly as a `None` would. Set it in the **lane's env file**
(Stage 4's trap).

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
with the second line present is a precise diagnosis. This exists exactly so you
do not have to wait five minutes to learn the feature is off.

**Only the ARMED line is once-per-lane.** `_announce_ready` is guarded by
`_ANNOUNCED` keyed on `(lane, channel)`, so the ARMED line appears once per lane
per process: on a multi-lane bridge look for *your* lane's line, and a lane you
did not configure staying silent is expected, not a fault. The
**not-configured** line has no such guard — `build_turn_progress` logs it on
every call (`progress.py:505-510`), i.e. **once per Slack message**, so a
misconfigured lane repeats it for as long as you keep talking to it. Useful, and
worth expecting before you conclude something is looping.

One cosmetic note so you can match the line you actually see: when the lane name
is `None` (the embedded single-persona case), the ARMED line renders
`ARMED for this bridge` rather than a lane name (`progress.py:445`).

**Where the log is.** Two deployments, two places:

| How you run the bridge | Read the log with |
|---|---|
| systemd (`tigerharness slack-bridge gen-service`) | `journalctl --user -u <your-unit> -f` |
| foreground (`python -m tigerharness.slack_bridge`) | stdout of that terminal |

The generated unit sets `StandardOutput=journal` and `StandardError=journal`,
so under systemd there is **no log file to `tail`** — the usual reason this
verification stalls.

`<your-unit>` is **not** a fixed name. `derive_unit_name` builds it per teams
root as `slack-bridge-<basename>-<hash6>.service`, where the basename is first
**sanitized** — `re.sub(r"[^A-Za-z0-9_.-]+", "-", resolved.name)`, falling back
to `root` if nothing survives (`gen_service.py:56`) — and the hash is the first
6 hex of a SHA-256 over the *full resolved path*, so two roots sharing a
basename (`~/a/teams`, `~/b/teams`) never collide on one unit.

Because the digest covers the **expanded** path, a worked example is only ever
true for one `$HOME`: a root at `/home/tigerleap/projects/tiger-teams` yields
`slack-bridge-tiger-teams-4a9e4a.service`, and the same relative layout under a
different home yields a different digest. Don't guess it; get it one of two
ways:

```bash
systemctl --user list-units 'slack-bridge-*'   # what is actually running
```

or re-run `gen-service` — it prints **three** lines on **stderr** (stdout is
the unit file itself, so `gen-service > unit` stays clean):

```
# Save as: ~/.config/systemd/user/<name>
# Then:    systemctl --user daemon-reload
#          systemctl --user enable --now <name>
```

(`gen_service.py:196-200`.) The derivation is deterministic, so re-running never
renames anything.

**`gen-service` is Linux-only.** On any other platform it emits **no unit at
all**: it prints a warning naming your `sys.platform` and the command to run
yourself (`<venv-python> -m tigerharness.slack_bridge` with
`TIGERHARNESS_BRIDGES_CONFIG` set) and **exits 1**
(`gen_service.py:161-170`). So on macOS this row of the table does not apply and
you supply your own supervisor.

> **You do not need to set a log level for this.** Both lines are always
> visible: the bridge daemon hardcodes `basicConfig(level=logging.INFO, ...,
> force=True)` (`slack_bridge/__main__.py:83`) and so ignores
> `TIGERHARNESS_LOG_LEVEL` entirely — under systemd as much as in your shell.
> The lane-vs-shell distinction is real for the *channel* (below); it never
> applied to the log level.

### Stage 6 — autodrive

**This is Inkstone symptom #1: a team that does not know this exists never
self-drives.** Autodrive is a detached daemon that periodically fires "drive
the journal."

It is **off unless you opt in**, and it is the deliberate, Operator-authorized
exception to the journal's human-triggered rule.

**The old justification for that exception — "safe only while `claude -p` bills
the subscription" — no longer describes what the daemon does.** Since 0.6.0 a
drive runs on the *driver persona's* vendor, and on a multi-lane team the daemon
fires one drive per lane, each with its own backend and model
(`autodrive/runner.py:1311-1319`, built by `probe_lanes`
`runner.py:616-695`, dispatched through `get_backend(cfg.backend)`
`runner.py:722-736`). So the cost shape is whatever each lane's vendor
charges, and ADR 0013's header retires the cost premise outright. The reason to
opt in deliberately is unchanged — an unattended process spends *something* on
your behalf — but do not reason about it as a `claude -p`-only rail.

Settings resolve **flag > process env > the team's `configs/.env` > built-in
default** — **for the six knobs in the table below, and not for the two after
it.** The env-var name constants live in `autodrive/settings.py`; the interval
default and floor live in `autodrive/runner.py`:

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

**Two more knobs exist and they have NO env var** — do not look for one:

| Flag | Default layer |
|---|---|
| `--backend` | accepts a backend name (`claude_p`, `codex_exec`) **or** a vendor name (`claude`, `chatgpt`) (`autodrive/cli.py:829-838`) |
| `--model` | (`autodrive/cli.py:839-846`) |

Their default does not come from the environment at all; it comes from
`configs/personas.yaml` via `resolve_drive_backend`
(`autodrive/cli.py:187-224`): **the flag > the driver persona's `vendor:` /
`model:` (else the team's `default_vendor` / `default_model`) > `claude_p`
unpinned.** One subtlety worth knowing before you pass `--backend`: a model id
belongs to one vendor, so a persona's `model:` is applied only when the drive
actually runs on that persona's backend — an explicit `--backend` that differs
gets **no** model unless `--model` says which. A malformed `vendor:` in
`personas.yaml` raises rather than quietly starting the daemon on the other
vendor's bill.

There is also a **derived** setting, `lanes`, which you never set directly:
`start` computes `lanes = not (args.backend or args.model or args.prompt)`
(`autodrive/cli.py:438`, field at `runner.py:164`). Pinning any of those three
pins every drive to one shape, so lane fan-out stands down.

Set `TIGERHARNESS_AUTODRIVE_AUTOSTART` in the team's `configs/.env` to make the
queue self-driving: `ensure_running` is called after **four** verbs write to the
queue — `journal new`, `defer`, `materialize`, **and `answer`**
(`journal/cli.py:1110` is the `answer` site; `autodrive/cli.py:547` names all
four). `answer` matters more than it looks: it is the verb that returns a parked
`needs_input` task to `active/`, so without it a task the Operator just
unblocked would sit there until something else rang the bell. It is idempotent,
never fatal, and opt-in — scheduling work starts the daemon, draining the queue
stops it, and steady state is *no process running*.

**Consider setting `--max-budget`.** Uncapped is a legitimate choice, but it
should be a decision, not an accident.

#### One daemon, one drive per lane (ADR 0012)

"A detached daemon that periodically fires 'drive the journal'" is now the
**single-lane special case**. On a team whose personas do not all share one
vendor+model, the daemon is a **fan-out coordinator**: each tick it probes which
*lanes* have work and fires one drive per lane
(`autodrive/runner.py:1296-1330`). What that buys you, and what it constrains:

- **At most one drive per lane in flight** (`runner.py:1303-1304`) — a slow lane
  never gets two drives stacked on it, and a busy lane does not block the
  others.
- **An early wake is floored** at `MIN_INTERVAL_SECONDS`
  (`runner.py:1305-1311`): a lane that fired seconds ago holds until the floor,
  and logs that it is holding.
- **When every lane with work already has a drive out**, the tick does not fire
  and does not go silent either: it emits a skip pulse,
  `SKIP_LANES_BUSY` = `"lanes busy - every lane with work already has a drive
  out"` (`runner.py:701`, `:1322-1327`). Seeing that repeatedly is healthy
  saturation, not a stall.
- **The idle path fires a per-lane memory sweep** instead
  (`probe_sweep_lanes`, `runner.py:540-613`).

The practical consequence for setup: `--driver` no longer determines which
vendor the daemon runs on by itself — the lane probe does, per lane. Pin
`--backend` / `--model` / `--prompt` only when you genuinely want one shape for
every drive, because that is exactly what turns fan-out off.

Manual control:

```bash
uv run tigerharness autodrive start --interval 600 --driver <Persona>
uv run tigerharness autodrive status
uv run tigerharness autodrive stop
```

The **stop brake** is cooperative-then-forceful: `stop` sets `stop_requested`
in the state file and signals the daemon's process group with `SIGTERM`. Note
that `stop` then calls `clear_state` (`autodrive/cli.py:707`), so **after a
clean stop `status` reports "no state file"** — the same string a team that
never enabled autodrive gets. A *stale* state file means something killed the
daemon without `stop` running.

State lives in `.autodrive.json` (alongside `.autodrive.lock` and
`.autodrive.log`) and carries **23 keys**: the twelve-key config projection
`config_to_dict` writes — `interval_seconds`, `driver`, `backend`, `model`,
`max_budget_usd`, `permission_mode`, `prompt`, `cwd`, `notify`,
`notify_channel`, `journal_root`, `lanes` (`runner.py:229-243`) — plus eleven
runtime counters: `pid`, `started_at`, `fire_count`, `last_fire_at`,
`in_flight`, `tick_count`, `last_tick_at`, `stop_requested`,
`last_stop_reason`, `last_cost_usd`, `last_error` (`autodrive/cli.py:484-497`).
`backend`, `model` and `lanes` are the ones worth reading after a vendor change.

**Which journal root?** Not `--journal-dir`. The state file anchors to the
**team-canonical** journal — when you run the command from a team root, that
team's `<team>/journal` regardless of any override (`_state_root`,
`autodrive/cli.py:240-247`), so a second `start` anywhere in the team finds the
live pid and is refused. If you pass a `--journal-dir` that disagrees, `status`
prints a three-line `read:` anchor naming the file it actually read
(`autodrive/cli.py:600-603`) rather than answering about a journal you did not
ask about.

`TURN_SCOPED_ENV_VARS` — `TIGERHARNESS_SLACK_THREAD_TS` and
`TIGERHARNESS_SLACK_CHANNEL` — are scrubbed at the daemon spawn boundary, so a
drive never inherits a stale Slack thread marker. You do not set these; the
bridge sets them per turn.

**Verify:**

```bash
uv run tigerharness autodrive status
```

This is state-revealing by design — a team that never enabled autodrive gets
a *different string*, not an absence, so "we thought it was on" cannot
survive the check. Both branches, so you can tell them apart:

**Never enabled** — one line, and that is the whole output:

```
autodrive: stopped (no state file)
```

**Enabled and alive** — a multi-line state report headed `autodrive: running`.
The labels are character-exact and so is the order (`autodrive/cli.py:648-677`):

```
autodrive: running
  pid:          <pid>
  journal:      <team-root>/journal
  interval:     600s
  backend:      claude_p
  model:        <model or (backend default)>
  driver:       <Persona>
  lanes:        on (one drive per vendor/model lane with work)
  max_budget:   <usd or None>
  notify:       slack -> <channel or operator DM>
  started_at:   <iso8601>
  fire_count:   <n> (drives launched)
  last_fire_at: <iso8601 or (none yet)>
  in_flight:    <n> (running now)
  done_count:   <n> (drives completed)
  last_done_at: <iso8601 or (none yet)>
```

`backend:`, `model:` and `lanes:` are the vendor rows — the fastest way to see
what a drive will actually run on. Two rows are **conditional** and appear only
when the field is set: `last_stop:` and `last_error:`.

**Enabled but dead** — the same report headed `autodrive: stopped (stale state
file)`, preceded by a two-line `note:` saying the counters are frozen at the
daemon's last write. Do not read this as the first case: a stale state file
means the daemon *was* configured and is not running now (SIGKILL, OOM,
reboot), and `in_flight: 1 (last recorded, daemon not running)` is how it tells
you it died mid-drive.

**"Never enabled = exactly one line" holds in the common case only.** Two
things can add lines to *any* branch, including that one: the three-line
`read:` anchor when `--journal-dir` disagrees with the team-canonical root, and
`notify_failures:` / `notify_last_error:` when the notify sidecar has recorded
failures (`autodrive/cli.py:625`, `:630-631`, `:676-677`). Both are silent when
there is nothing to report, so seeing extra lines is information, not noise.

Confirm the reported `driver:` is a real persona in `configs/personas.yaml`
— `(none)` prints there when no driver resolved, which is a daemon that will
fire and attribute its work nowhere.

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
mode.

Playbook names are validated against `^[A-Za-z0-9][A-Za-z0-9._-]*$`
(`_PLAYBOOK_NAME_RE`, `scaffold.py:174`), so a path like `../../etc` is
rejected. Note **where**: the check is inside `new_workflow_task`
(`scaffold.py:823-827`), *after* `_cmd_new_workflow` has already built the path
and stat'd it (`journal/cli.py:329-337`) — so a name with a separator fails the
file-not-found check first. Either way it is exit 2 with nothing scaffolded, but
the error you see may not be the one you expect.

Before any disk write, `new_workflow_task` calls `validate_personas` on the
union of the compile-role personas and the persona names referenced in the
playbook, raising `MissingPersonaError` when any is missing. Nothing is
scaffolded on failure.

**The compile trio is no longer hard-coded.** `_required_workflow_personas`
(`scaffold.py:714-726`) takes the **team-configured** compile-role personas plus
any playbook reference that matches your roster — including aliases, resolved
before the intersection, so a playbook addressing a persona by nickname is still
recognised. Names the playbook mentions that are *not* in the roster are treated
as English prose, not as persona typos. `configs/workflow.yaml` is the override
(`compile_cli.py:1367-1372`); Anzai / Akagi / Ayako are only the **defaults**.
So the fix for a fresh team is a three-line `workflow.yaml` naming your own
personas — not renaming people to match ours. Pre-flight it:

```bash
uv run tigerharness journal validate-personas <Team>
```

Note this is a **`journal` sub-verb**, not a top-level command.

`journal defer` is the cheap Slack-side half: it parks the Operator's
conversation **verbatim** in `deferred/` with no playbook read, no compile and
no model call. A later drive turns it into a real task with `journal
materialize <deferred-id>`.

#### The drive rail — who may drive, and it is now a team setting

**A Slack-triggered session may always SCHEDULE journal work. Whether it may
DRIVE is your team's decision** (ADR 0013) — the older flat rule "Slack
schedules, never drives" is now just the **default**, not the law.

The knob is `TIGERHARNESS_JOURNAL_SLACK_DRIVES` (`journal/cli.py:85-99`), read
through the same dependency-free reader autodrive uses: the process environment
first, then the team's `configs/.env` (Stage 2). Set it to `1` there and
bridge-spawned sessions drive exactly like an interactive one.

`journal claim` is where this is enforced, and it is worth knowing the exact
shape because three of the four cases look alike from outside
(`journal/cli.py:1125-1157`):

| Situation | Result |
|---|---|
| `TIGERHARNESS_SLACK_THREAD_TS` absent | no gate at all — an interactive session never carries the marker |
| marker set **and** `--allow-api-drive` passed | allowed, and **the knob is never read** (short-circuit, `journal/cli.py:1135-1136`) |
| marker set, no flag, knob truthy | allowed, with a `log.info` naming the knob |
| marker set, no flag, knob falsey | **exit 1**, refusal printed, **before any mutation** |

Two boundaries that are easy to get wrong: the gate lives **only on `claim`** —
not on `release`, `step-done`, `sweep` or `answer` — and it runs before any
status read or write, so a refused claim provably changes nothing. Autodrive
remains the separate sanctioned exception (Stage 6).

Driving verbs: `sweep` (archive done, classify in-progress as idle / busy /
crashed), `claim`, `release`, `step-done` for workflow graph walks, and `answer`
to reopen a task parked on an Operator question. Their full flag sets are
larger than the two or three each that used to be listed here:

| Verb | Flags |
|---|---|
| `claim` | `--format`, `--stuck-timeout`, `--driver`, `--drive-thread`, `--any-lane`, `--allow-api-drive` (`journal/cli.py:2290-2340`) |
| `release` | `--session-ref`, `--driver`, `--state`, `--output`, `--next-action`, `--question` (`:2370-2388`) |
| `step-done` | `--task`, `--step`, `--verdict`, `--output`, `--driver`, `--any-lane` |

#### `--driver` is the drive's LANE IDENTITY, not just a memory label

`--driver` does attribute work to a persona's memory store — but since ADR 0012
it also decides **which work this drive is allowed to touch**. Omit it and you
switch the lane gate off entirely: the source says so in as many words, *"No
`--driver` = no lane identity = no gate"* (`journal/cli.py:1207`;
`lanes.py:169`).

What `--driver` turns on:

- **`sweep --driver <P>` grows a lane view.** Each actionable item and inbox
  entry is marked `  [mine]` or
  `  [lane <k> -- not yours; owner <o or 'team default'>]` (`_lane_suffix`,
  `journal/cli.py:853-858`), under a header naming your driver and lane, and
  followed by a **lane verdict** (`:767-769`). The verdict is one of three
  (`_lane_verdict`, `:829-850`): `mine` — pick one; `other-lanes` — nothing is
  yours, **end the drive without the idle-maintenance tail**, because the other
  lanes' own drives will take the rest; `idle` — nothing actionable anywhere.
  `--format json` adds `driver`, `driver_lane`, `lanes`, `actionable_mine`,
  `actionable_other_lane`, `deferred_lanes`, `deferred_mine` and
  `lane_verdict` (`:750-762`).
- **`claim --driver` refuses another lane's work with exit 3**
  (`journal/cli.py:1203-1221`, helper `_refuse_if_other_lane` `:861-881`),
  read-only, nothing written. A **malformed `vendor:`** in `personas.yaml` is a
  different failure: **exit 2** (`:872-873`), so a typo there never quietly
  starts work on the other vendor.
- **`step-done --driver` applies the same gate, also exit 3**
  (`journal/cli.py:1658-1662`) — a step note must never be recorded as work
  done on the wrong vendor. And when the **next** step belongs to another lane
  it prints a `handoff:` line (`:1717-1733`, printed `:1761-1767`, JSON key
  `handoff` `:1744`) telling you to release the task so a drive on that lane
  picks it up. It is advisory and fail-soft — the walk has already advanced.
- **`--any-lane` is the deliberate override, and it exists on exactly those two
  verbs** — `claim` (`:2322`) and `step-done` (`:2459`) — and nowhere else.

Ownership is resolved in `journal/lanes.py`: `work_owner` (`:132-152`) for a
task, `step_owner` (`:127-129`) for a compiled step, `deferred_owner`
(`:176-185`) for an inbox entry, `lane_of` (`:93-98`) for a persona's lane, and
`lane_check` (`:155-173`) for the comparison. A single-vendor team has one lane
and never notices any of this.

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
1800.0). State lives in `SWEEP_STATE_FILENAME` = `.tiger-memory-sweep.json`.

**`sweep-plan` has three outcomes, not two**: `claimed`, `not_due` (inside the
floor with no own-persona sources — the cheap common case), or `busy` (someone
else holds the lease) — `sweep.py:171-183`. Only the first hands you a claim.

Each claim then ends in `sweep-complete` or `sweep-release`, and
**`sweep-complete` advances the team watermark only for a `scope: "team"`
claim.** An `own-only` run deliberately leaves it untouched
(`sweep.py:269-276`, `:317-323`), because bumping it would silently postpone the
team's floor-due sweep by a whole window every time one persona's sources fired.
`sweep-complete` is also **refused** — returning `False`, changing nothing — on
a claim-token mismatch, or while roster personas are still pending, unless
`force` is passed (`sweep.py:288-289` for the token check, `:294-315` for the
pending-persona refusal). So "I ran sweep-complete" is not the
same as "the watermark moved"; a team whose sweeps never seem to go quiet is
worth checking here first.

`rebuild` regenerates a persona's session-start briefing — `README.md`,
`MANIFEST.md`, `UNPROCESSED.md`, `must_remember.md`, `skill_index.md`,
`topic_index.md`, plus `skills/` and `topics/` detail files. It is **pure local
computation with no model or API call**, so it is always safe to run standalone
to refresh a briefing that has drifted behind the store.

It is not, however, *purely* a regeneration, and two side effects are worth
knowing before you reach for it:

- It writes a hidden `.fingerprint` over the store files
  (`FINGERPRINT_NAME`, `briefing.py:48`, written at `:115-117`). That is how a
  later rebuild can skip when nothing changed. Being hidden, it does not
  disturb the `ls` check below.
- It runs `check_all(cfg, store, fix=True)` first
  (`lifecycle.py:1342-1343`, inside `rebuild` at `:1312-1353`) and logs a
  warning naming any store it repaired. So a `rebuild` can quietly fix
  mechanical drift — good, but it means a store that "was fine after rebuild"
  may have been repaired rather than found clean. Read the log.

**Verify:**

```bash
uv run tigerharness tiger-memory --config memories/<Persona>/tiger-memory.config.yaml check
uv run tigerharness tiger-memory --config memories/<Persona>/tiger-memory.config.yaml doctor
```

**`check` is the pass/fail gate: expect exit 0**, meaning all three stores
parse. `check --fix` repairs mechanical drift and quarantines anything it
cannot repair to `<store>.rejected.md`.

**`doctor` is a report, not a gate — read its `FLAGS:` section, do not gate
on its exit status.** `doctor_report` collects every anomaly into one `flags`
list and `_cmd_doctor` exits 1 whenever that list is non-empty, so **a
correctly built team exits 1 on day one and keeps exiting 1 forever.** Two
flags are expected rather than wrong:

- `<Persona>: never swept (no done_at recorded)` — unavoidable on a
  brand-new team, which by definition has never swept. It clears after the
  first sweep.
- `topic slug collision: <slug> across <Persona>, <Persona>` — two personas
  independently owning a topic of the same name. Normal and desirable on any
  team that has worked together; the reference team carries 22 of these while
  perfectly healthy.

These are the flags that mean something is actually wrong:

- `<Persona>: briefing missing (run rebuild)` — fix with `rebuild`.
- `<Persona>: rejected file(s): ...` — content `check --fix` could not
  repair; open the `.rejected.md` file.
- `<Persona>: <store> over_overflow (<n> chars, max <m>)` — a store past its
  bound that compaction has not yet reclaimed.
- `<Persona>: config error (...)` — that persona's config does not load.
- `<Persona>: last compact-apply left still_over: <stores>`
  (`inspect_tools.py:322-326`) — the previous compaction ran and did **not**
  get the named store back under its bound. Distinct from `over_overflow`: that
  one says a store is too big, this one says the thing meant to fix it already
  tried and failed.

One format correction, since the collision flag is the one you will see most:
the slug field is **every distinct spelling joined by `/`**, not a single slug.
Entries are grouped by `_norm_slug` (`inspect_tools.py:283`, grouped at
`:372-374`, rendered `:383-386`), so a real line can read
`topic slug collision: drive-lanes/Drive-Lanes across Akagi, Anzai` — which is
two personas *and* two spellings, and still expected.

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

Run all eight from the team root. Every one of them can fail.

| # | Check | Command | Alive looks like |
|---|---|---|---|
| 1 | Team resolves | `cat configs/personas.yaml` | your personas listed, `default_persona` set |
| 2 | Project path real | `cat configs/repos.yaml` | an **uncommented** `project:` key (relative is fine) |
| 3 | Bridge replies | DM the bot from an allowlisted account | a reply in thread |
| 4 | **Ops-log armed** | grep the bridge log for `heartbeats ARMED` | the ARMED line naming your channel |
| 5 | **Autodrive known** | `tigerharness autodrive status` | a state report with interval + a real driver — **not** `autodrive: stopped (no state file)` |
| 6 | **Queue reachable** | `tigerharness journal sweep` | counts matching what you scaffolded |
| 7 | Memory healthy | `tiger-memory ... check` | exit status 0 (**not** `doctor` — see Stage 8) |
| 8 | **Compile trio present** | `tigerharness journal validate-personas .` | exit 0 + `ok: ... has all of [...]` (see Stage 3) |

Checks 4, 5 and 6 are the three that were silently false on Inkstone. If you
verify nothing else, verify those. Check 8 fails on **every** freshly scaffolded
team that has not been told which personas compile workflows — it is the one
below that costs you a crashed task rather than a missing feature.

> **Check 6 is the one that is not read-only.** `journal sweep` archives every
> `done` task and can materialize deferred inbox entries into the queue —
> `journal --help` describes it as *"archive done tasks, classify in_progress
> as idle/busy/crashed, summarise. Side-effecting."* That is fine on day one,
> when the queue is yours and empty. Once the team is working, re-run this row
> as `tigerharness journal list`, which reads the same trays and changes
> nothing.

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
| `TIGERHARNESS_LOG_LEVEL` | `_logging.py` | optional; CLIs only — the bridge daemon ignores it |
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

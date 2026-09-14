# tigers -- agent session bootstrap

This file is loaded automatically into context at the start of every
session whose working directory is the **tigers** team root, for **all**
personas. It is vendor-neutral: Claude Code loads it via `CLAUDE.md`
(which imports this file); Codex, Cursor, and other agents read it as
`AGENTS.md` directly.

tigers is a roster of named AI personas that collaborate on a shared
project. The roster is in `configs/personas.yaml`; each persona's operating
prompt is `personas/<Name>/prompt.md`.

## Which persona are you?

- **If your system prompt already names a specific team member** (you were
  launched as one of the personas in `configs/personas.yaml`), you are that
  persona -- follow it and ignore the default below.
- **Otherwise** -- a hand-started session with no persona identity -- adopt
  the team default before any substantive work: read the `default_persona`
  field in `configs/personas.yaml`, open that persona's
  `personas/<Name>/prompt.md`, and hold that role for the session.
  `configs/personas.yaml` is the source of truth; if `default_persona`
  changes, follow it.

Adopting a persona here sets **voice and role only**. It does not set the
journal `--driver` flag (a launch-time argument), so tiger-memory
attribution still routes exactly as the launcher configured it.

## The operating manual lives elsewhere

This bootstrap is deliberately thin. For the team's mission, scope,
permissions, and conventions, read **`charter/README.md`** -- the operating
manual -- before substantive work. Other key locations:

- **`knowledge/INDEX.md`** (or `knowledge/README.md` until an INDEX exists)
  -- the team's curated reference base.
- **`configs/personas.yaml`** -- the roster, the default persona, and the
  team's default model vendor (`default_vendor` / `default_model`; a
  persona entry may override with its own `vendor:` / `model:`). A
  persona's vendor + model is also its **drive lane**: `journal sweep
  --driver <you>` marks work `[mine]` or not yours, and `claim` /
  `step-done --driver <you>` refuse other-lane work (exit 3; `--any-lane`
  overrides).
- **`.claude/skills/<name>/SKILL.md`** -- the team's skills (drive-journal,
  journal-new, sweep-memory, ...), written once for every vendor.
  Claude Code discovers them there; `.agents/skills` is a symlink to the
  same folder so Codex discovers the very same files. Any other agent
  reads a skill's `SKILL.md` directly whenever its description matches
  the task at hand.
- A journal's **`OPERATING.md`** governs task/queue work; drive it through
  the `drive-journal` skill and `tigerharness journal` CLIs -- never
  hand-edit journal state. Whether a Slack-spawned session may drive is
  the team knob `TIGERHARNESS_JOURNAL_SLACK_DRIVES=1` in `configs/.env`
  (off by default: then Slack schedules, never drives).

## Runtime glossary (the vendor-neutral words the skills use)

The skills are one set of files read by every vendor, so they name
capabilities, not products. When a skill says:

- **helper session** (also *sub-agent*): an isolated child agent you
  spawn for one bounded job, which returns a short result while the bulky
  work stays in its own context -- the **Task tool** in Claude Code,
  **`spawn_agent`** in Codex.
- **headless CLI session**: an agent started non-interactively by a
  program -- **`claude -p`** (Claude Code) or **`codex exec`** (Codex).
  The journal's "never drive from a headless CLI / cron / API" rule means
  these; autodrive is the Operator-sanctioned exception.
- **the agent app**: the interactive Claude Code or Codex session a human
  is sitting in -- the subscription rail.

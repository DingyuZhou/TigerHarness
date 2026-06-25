---
name: journal-autodrive
description: Start, check, or stop a background process that drives the journal on a fixed interval via the agent SDK (vendor-agnostic; default backend `claude -p`). Use when the user asks to "drive the journal every N minutes", "autodrive the journal", "keep working the queue automatically", "start/stop the auto-driver", or "is the autodrive running?". Wraps `tigerharness autodrive`. This is the Operator-authorized exception to the journal's human-only drive rule -- read the safety note before starting one.
---

# journal-autodrive

A small daemon that runs **"drive the journal"** on a timer. It spawns an
agentic backend (default `claude -p`) per tick with a self-contained,
Operator-authorized drive prompt, waits the interval, and repeats --
**drive, then sleep, no overlap**. It is **not** built on Claude Code's
`/loop`; it is a plain detached OS process you can `stop` any time.

## Read this before you start one (safety)

The journal is a **human-triggered** subscription backend -- "no
programmatic driver by design". This is the deliberate, **Operator-
authorized** exception, and it is **only safe while `claude -p` bills the
Claude subscription** rather than API tokens. If that billing flips, an
unattended autodrive spends real dollars on **every tick**. Guardrails:

- **`--max-budget <usd>`** caps each drive's reported cost. Strongly
  advised; it is your protection for the day billing changes.
- **`tigerharness autodrive stop`** is the off-switch. Use it.
- The interval has a **60s floor**; a single drive already takes minutes.
- Only **one** autodrive runs per journal (a second `start` is refused).

If the user asks for this and you are unsure billing is still on the
subscription, **say so** before starting it.

## Commands

Run from the **team root** (so the team's own journal is the target).

Start (the common request -- "drive every 10 minutes"):

    tigerharness autodrive start --interval 600 --driver <persona> \
        --max-budget 5

- `--interval` seconds between drives (default 600 = 10 min; floor 60).
- `--driver` persona the work is attributed to (defaults to the team's
  `default_persona`). Pass it so worklogs land in the right memory store.
- `--max-budget` per-drive USD cap (advised; see safety note).
- `--backend` agent_sdk backend (default `claude_p`). **Vendor-agnostic
  caveat:** only an *agentic CLI* backend (like `claude_p`) can actually
  invoke skills/tools and drive; a raw chat-completion backend cannot.
- `--model`, `--permission-mode`, `--prompt` override the model, the
  unattended permission mode (default `bypassPermissions`), and the
  built-in drive instruction.

Check:

    tigerharness autodrive status

Stop:

    tigerharness autodrive stop

## How to read the request

- "drive the journal every 10 minutes" -> `start --interval 600`.
- "every 5 minutes" -> `start --interval 300`. (Below 60s is refused.)
- "stop the autodrive" / "stop driving automatically" -> `stop`.
- "is it still running?" / "autodrive status" -> `status`.
- A bare "keep the queue moving automatically" -> `start` at the default
  10-minute interval; mention the `stop` command and the budget cap.

## What it does each tick

Spawns the backend in the team root with a prompt that **explicitly lifts**
the "never drive from claude -p / cron / API" boundary for this sanctioned
process, then drives via the **drive-journal** skill (sweep -> claim one
actionable task with `--driver <persona> --allow-api-drive` -> work it ->
cascade). A drive that hits its budget cap or context ceiling just leaves
the task `in_progress`/idle; the next tick resumes it -- truncation is
safe because the journal's session model is resumable. A tick that errors
is logged to `<journal>/.autodrive.log` and the loop continues.

State lives in `<journal>/.autodrive.json` (pid, interval, tick count,
last tick time, last stop reason / error); `status` reads it, `stop`
clears it.

## If you get confused

It is just a wrapper around `tigerharness autodrive {start,status,stop}`.
When in doubt, run `status` to see whether a daemon is live, and prefer
`stop` over leaving an unattended driver running once the user's immediate
need is met.

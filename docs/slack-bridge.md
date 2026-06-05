# slack-bridge

Slack Socket Mode bridge that connects allowlisted users to a Claude
Code agent via DM or @mention.

## What it does

1. Listens for Slack DMs and @mentions via Socket Mode (no public URL needed).
2. Downloads any file attachments and stages them on disk.
3. Dispatches the message to a `claude -p` backend session.
4. Posts the agent's reply back into the same Slack thread.
5. Persists thread-to-session mappings so conversations survive restarts.

## Architecture

```
Slack Socket Mode
    |
    v
AsyncApp (slack-bolt)
    |-- handle_message (DMs)
    |-- handle_mention (@bot in channels)
    v
SlackBridge
    |-- ThreadStore (persist thread->session)
    |-- FileDownloader (stage attachments)
    |-- agent-sdk backend (claude_p)
    v
Reply posted to thread
```

## Key modules

| Module | Purpose |
|---|---|
| `bridge.py` | SlackBridge class: routing, dispatch, thread management |
| `config.py` | BridgeConfig from env vars + .env |
| `persistence.py` | ThreadStore: atomic JSON file for thread->session map |
| `downloader.py` | SlackFileDownloader + prompt augmentation |
| `notify.py` | Outbound: SlackNotifier (text DM + file upload) + CLI |
| `multi.py` | Multi-team bridge loader (per-team lanes from a bridges config) |
| `router.py` | One-shot LLM persona routing (sticky per thread) |
| `migrate.py` | threads.json migration tool |
| `gen_service.py` | systemd unit generator (Linux) |
| `__main__.py` | Daemon entry point with graceful shutdown |

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `SLACK_APP_TOKEN` | (required) | Socket Mode app token (xapp-...) |
| `SLACK_BOT_TOKEN` | (required) | Bot OAuth token (xoxb-...) |
| `ALLOWED_SLACK_USER_IDS` | (required) | Comma-separated user IDs |
| `TIGERHARNESS_AGENT_CWD` | `.` | Working directory for the agent |
| `TIGERHARNESS_AGENT_PROMPT` | (none) | Path to system prompt .md file |
| `TIGERHARNESS_SLACK_ENV` | (none) | Explicit .env file path |
| `TIGERHARNESS_SLACK_STATE_DIR` | XDG state | Where threads.json lives |
| `TIGERHARNESS_ATTACHMENT_DIR` | `/tmp/slack-attachments` | File staging dir |
| `TIGER_MEMORY_CONFIG` | (none) | Auto-trigger memory rebuild on new threads |
| `TIGER_MEMORY_CLI` | (none) | Path to tiger-memory binary |
| `TIGERHARNESS_BRIDGES_CONFIG` | (none) | Path to a top-level `slack-bridge.yaml` index. When set, the bridge runs in **multi-team mode** ([details below](#multi-team-mode)). Single-team config vars above are ignored. |

## Running

```bash
# As a daemon
python -m tigerharness.slack_bridge

# As a systemd user unit (recommended)
# See the .service file template
```

## Notify CLI

For agents to send proactive messages:

```bash
# Text DM
python -m tigerharness.slack_bridge.notify text "Hello" --thread 1234.5678

# File upload
python -m tigerharness.slack_bridge.notify file --file /tmp/chart.png \
    --comment "Results" --thread 1234.5678
```

## Graceful shutdown

On SIGTERM, the bridge:
1. Stops accepting new dispatches.
2. Waits up to 90s for in-flight dispatches to complete.
3. Closes the Socket Mode handler.
4. Exits.

This ensures replies from in-progress turns get posted before restart.

## Thread tracking

The `[bridge-context]` block appended to each message tells the agent
which thread it's in:

```
[bridge-context]
slack_thread_ts: 1778702517.507109
slack_channel: D012ABCDEF
```

Agents use this to route follow-up DMs via `--thread`.

## Multi-team mode

A single bridge process can serve N teams concurrently — one Slack app
per team (own bot identity, own tokens, own persona, own
`threads.json`), all multiplexed through one event loop. This is the
"one bridge process, many bots" deployment shape.

### When to use it

- You have 2–10 teams and don't want N systemd units.
- Each team needs its own bot identity in Slack (different name + avatar).
- You're already using `tigerharness init` to scaffold teams.

If you only have one team, single-tenant is simpler — multi-team adds
a small config layer for no benefit at one team.

### Opt in

Multi-team mode activates when `TIGERHARNESS_BRIDGES_CONFIG` points at
a top-level **index file**. Until that env var is set, the bridge runs
in single-tenant mode exactly as before.

To opt in once:

```bash
# In your teams directory (e.g. ~/projects/teams/)
touch slack-bridge.yaml
```

From then on, every `tigerharness init` auto-appends the new team's
lane to this index and writes a per-team fragment under
`<team>/configs/slack-bridge.yaml`.

### Config layout

```
teams/
├── slack-bridge.yaml             ← top-level INDEX (lane names only)
├── shohoku/
│   ├── configs/
│   │   ├── .env                  ← tokens for Shohoku's Slack app
│   │   ├── personas.yaml         ← task-runner registry
│   │   └── slack-bridge.yaml     ← per-team FRAGMENT (this file)
│   ├── personas/ayako/prompt.md
│   └── memories/ayako/tiger-memory.config.yaml
└── tigers/
    └── ...
```

Index (`teams/slack-bridge.yaml`):

```yaml
lanes:
  - shohoku
  - tigers
```

Per-team fragment (`teams/shohoku/configs/slack-bridge.yaml`):

```yaml
default_persona: ayako          # required; legacy `persona:` accepted as alias
allowed_user_ids:
  - U0123ABC                    # required: at least one Slack user ID
state_dir: ~/.local/state/slack-bridge/shohoku   # required, must be unique across lanes

# Optional overrides (defaults shown):
# env: configs/.env
# agent_cwd: .
```

The routable persona roster is **auto-discovered** from the team's
`configs/personas.yaml`. Adding a persona via `tigerharness init`
automatically makes them reachable in the team's Slack bridge — no
second edit needed.

The loader (`tigerharness.slack_bridge.multi.load_multi`) enforces:

- Required fields present, `default_persona` exists in the team's roster, `allowed_user_ids` non-empty + each starts with `U`/`W`.
- Token prefixes (`xapp-` / `xoxb-`).
- Every persona in the roster has a `personas/<name>/prompt.md` file.
- No two lanes share a `state_dir` (would corrupt each other's `threads.json`).
- No two lanes share a `SLACK_APP_TOKEN` (Slack rejects the duplicate Socket Mode connection).
- No duplicate lane names in the index.

Validation runs at startup; the bridge refuses to launch on any failure.

### Multi-persona routing within a team

A team's Slack app routes inbound DMs to one of the team's personas
based on **who the first message addresses**. The choice is **sticky
per thread** — every subsequent message in that thread goes to the
same persona.

Example:

| First DM in a new thread | Routed to |
|---|---|
| `Hi Ayako, can you help me?` | Ayako |
| `Sakuragi — practice plan for tomorrow?` | Sakuragi |
| `What's the meeting agenda?` (no name) | `default_persona` |

#### How routing works

1. New thread arrives → bridge does a **one-shot LLM call** to the
   same backend the personas use (no separate vendor dependency).
   Prompt: "Given this roster and this message, which team member is
   addressed? Return one name or `default`."
2. If the response matches a roster name → that persona is bound to
   the thread. The mapping is persisted to `threads.json`.
3. If the response is `default`, unparseable, or off-roster → falls
   back to `default_persona`. The bridge stays up; **misroutes are
   handled conversationally** by the persona (see preamble below).
4. Network failure during routing → also falls back to `default_persona`.

#### Reply prefix

In multi-persona teams, every reply is prefixed with `[<persona>]:`
so the user can see who answered without scrolling up:

```
[Ayako]: Hi! Yes, I can help you set up the playbook for tomorrow's match.
```

Single-persona teams (legacy single-tenant bridge, or a team with only
one entry in `personas.yaml`) skip the prefix — output stays identical
to before.

#### Misroute recovery: the team-awareness preamble

Every persona's prompt has a **routing-awareness preamble** appended
at startup that teaches them about teammates and how to handle
misroutes politely:

> *You are Ayako, a member of team shohoku.
> Other team members reachable via separate Slack threads: Sakuragi, Mitsui.*
>
> *If a user's message in this thread is clearly addressed to a different
> team member (e.g. "Hi Sakuragi" when you are Ayako), politely identify
> yourself, suggest the user start a new DM thread to reach the intended
> team member, and optionally help with anything within your own scope.
> Don't attempt to act as another team member.*
>
> *You don't need to prefix your replies with your own name — the Slack
> bridge automatically labels every reply with `[Ayako]:` so the user
> knows who answered.*

So if a user types "Hi Sakuragi" but the router somehow picked Ayako
(or detection failed and fell back to default), Ayako responds with
something like "I'm Ayako — for Sakuragi, please start a new thread."

Single-persona teams skip the preamble (there's nobody to redirect to).

#### Routing is best-effort

The router is an LLM call, not a hard classifier. Edge cases:

- **Chatty model output** ("I think this is for Ayako" instead of just `Ayako`) is treated as off-roster and falls back to `default_persona`.
- **Ambiguous addressing** ("ask Sakuragi about that, Ayako") picks one — typically whichever name the model considers primary. The persona handles the rest via the preamble.
- **No name at all** ("What's tomorrow's plan?") -> `default_persona`.

The team-awareness preamble is the safety net: even when routing picks
"wrong," the persona will politely redirect rather than impersonate.

#### Bridge voice vs persona voice

The `[<persona>]:` prefix appears **only on the persona's own replies**.
Bridge-generated messages (backend errors, empty agent output,
attachment-download warnings) are posted unprefixed, so users can tell
"the bridge says X" from "Ayako says X." Examples:

```
[Ayako]: Here's the playbook for tomorrow's match.   # ← persona voice
:warning: backend error: `RequestTimeout(...)`        # ← bridge voice, unprefixed
_(empty reply)_                                       # ← bridge voice, unprefixed
```

#### Restart required for new personas

Adding a new persona via `tigerharness init --persona <name> --team <existing>`
updates the team's `personas.yaml` but the running multi-bridge has
already cached the roster. **Restart the bridge** (`systemctl --user
restart slack-bridge-multi`) for the new persona to become routable.

#### Cost tracking

The bridge accumulates both router LLM cost and agent LLM cost as
each thread is dispatched. On graceful shutdown (`SIGTERM` /
`request_shutdown`) the total is logged:

```
shutdown requested -- draining 0 in-flight dispatch(es), total LLM spend $0.4231
```

Useful for spot-checking how much a long-running multi-persona
deployment is burning. The counter is per-process — it resets on
every restart.

### Migrating an old threads.json

The bridge's threads.json schema changed in PR4 to include each
thread's persona. Pre-PR4 entries (bare `"session_id"` strings) have
no attribution, so per-persona memory filtering ([tiger-memory](tiger-memory.md))
excludes them under strict mode.

If you have an existing single-tenant bridge that you're migrating to
multi-persona, run the migration tool once to attribute all old
entries to a specific persona:

```bash
# See what would change without writing:
python -m tigerharness.slack_bridge.migrate \
    --state-dir ~/.local/state/slack-bridge/shohoku/ \
    --to ayako --dry-run

# Then run it for real:
python -m tigerharness.slack_bridge.migrate \
    --state-dir ~/.local/state/slack-bridge/shohoku/ \
    --to ayako
```

After this, all pre-routing entries get a `persona: ayako` field;
post-routing entries (already in the dict shape) are left alone. The
tool is idempotent — safe to re-run.

> **Important: stop the bridge before migrating.** Both the bridge
> and the migration tool write `threads.json` atomically, but they
> don't coordinate with each other. If the bridge writes a new entry
> between the migration tool's read and write, that entry gets
> clobbered by the migration's older snapshot — you'd lose the
> attribution for whatever conversation just happened. To migrate
> safely:
>
> ```bash
> systemctl --user stop slack-bridge-multi
> python -m tigerharness.slack_bridge.migrate --state-dir <path> --to <persona>
> systemctl --user start slack-bridge-multi
> ```

This mirrors the deferred hot-reload decision: lane add/remove also
requires a restart.

### Running the multi-team bridge

```bash
export TIGERHARNESS_BRIDGES_CONFIG=~/projects/teams/slack-bridge.yaml
python -m tigerharness.slack_bridge
```

Logs gain a `lane=<name>` field so you can grep per team:

```
2026-05-17 10:00:00 lane=shohoku tigerharness.slack_bridge INFO starting bridge with 2 lane(s)
2026-05-17 10:00:01 lane=tigers  tigerharness.slack_bridge INFO ...
```

### Adding a Slack app per team

Each lane needs its own Slack app:

1. https://api.slack.com/apps → **Create New App** → **From manifest**.
2. Configure the app manifest: enable Socket Mode, subscribe to `message.im` + `app_mention`, scopes `chat:write`, `files:read`, `im:history`, `im:write`, `app_mentions:read`.
3. Install to your workspace → grab `xapp-` (App-Level Token) + `xoxb-` (Bot User OAuth Token).
4. Drop them into `teams/<team>/configs/.env`.
5. Fill in the `allowed_user_ids` placeholder in `teams/<team>/configs/slack-bridge.yaml`.

### Lifecycle

On SIGTERM (e.g. `systemctl restart`), the multi-bridge:

1. Sends `request_shutdown()` to every lane's bridge.
2. `asyncio.gather`s each bridge's `wait_for_drain(timeout=90s)` — drains run **concurrently**, so the worst-case total wait is 90s, not 90s × N.
3. Closes every `AsyncSocketModeHandler`.
4. Exits.

If a lane's drain times out, the others still get their full window and the process logs a per-lane warning before continuing.

### Systemd unit

The fastest path: have tigerharness generate a unit file customized
for your machine — absolute paths baked in, no `%h` specifiers, no
manual editing.

```bash
cd ~/projects/teams
uv run tigerharness slack-bridge gen-service \
    > ~/.config/systemd/user/slack-bridge-multi.service

systemctl --user daemon-reload
systemctl --user enable --now slack-bridge-multi.service
```

`gen-service` is Linux-only. On other platforms it prints a friendly
message and returns 1 (you'd write your own launchd plist / Docker
recipe / whatever fits your stack).

The reference [`examples/slack-bridge-multi.service`](../examples/slack-bridge-multi.service) (with `%h` specifiers) is also available if you'd rather copy + adapt by hand. Key invariants either way:

- `EnvironmentFile=` points at a small `.env` containing only `TIGERHARNESS_BRIDGES_CONFIG=...` (per-lane tokens live in each team's `.env`, referenced by the YAML).
- `TimeoutStopSec=120` still applies; the 90 s drain budget is shared across lanes (concurrent), not per-lane.

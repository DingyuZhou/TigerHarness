# slack-bridge

## At a glance
- **What:** one Socket-Mode bridge process serving 1..N teams (lanes) — DMs
  and @mentions in, persona replies in-thread, with a `notify` CLI for
  proactive messages.
- **When you need it:** standing up / operating the bridge, multi-lane config,
  or migrating off the removed single-tenant mode (ADR 0009).
- **Must-not-miss:** `dismiss` is scoped to the operated root by content (the
  2026-06-12 cross-root incident class) — see "The bridge: one process, 1..N
  lanes" below.

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
| `config.py` | BridgeConfig dataclass + shared config primitives |
| `persistence.py` | ThreadStore: atomic JSON file for thread->session map |
| `downloader.py` | SlackFileDownloader + prompt augmentation |
| `history.py` | Thread-history fetch + transcript for untracked-thread joins |
| `notify.py` | Outbound: SlackNotifier (text DM + file upload) + CLI |
| `multi.py` | Multi-team bridge loader (per-team lanes from a bridges config) |
| `router.py` | One-shot LLM persona routing (sticky per thread) |
| `migrate.py` | threads.json migration tool |
| `gen_service.py` | systemd unit generator (Linux) |
| `__main__.py` | Daemon entry point with graceful shutdown |

## Configuration

> **One bridge, 1..N lanes.** The bridge always runs from a
> `TIGERHARNESS_BRIDGES_CONFIG` index (see [below](#the-bridge-one-process-1n-lanes));
> a single team is just a one-lane index. The former single-tenant
> env-var fallback was **removed on 2026-08-11 (ADR 0009)** — startup
> now fails fast with a migration pointer when the index var is unset;
> see [migrating off single-tenant](#migrating-off-single-tenant).

Process-wide env vars:

| Env var | Default | Purpose |
|---|---|---|
| `TIGERHARNESS_BRIDGES_CONFIG` | (required) | Path to the top-level `slack-bridge.yaml` index — one team or many ([details below](#the-bridge-one-process-1n-lanes)) |
| `TIGERHARNESS_SLACK_STATE_DIR` | XDG state | Default `threads.json` home for directly-embedded bridges (lane fragments set `state_dir` explicitly) |
| `TIGERHARNESS_ATTACHMENT_DIR` | `/tmp/slack-attachments` | File staging dir |
| `TIGERHARNESS_SLACK_ENV` | (none) | Explicit .env path for the `notify` CLI |

Per-lane `.env` file keys (the fragment's `env:` path, default
`<team>/configs/.env`):

| Key | Default | Purpose |
|---|---|---|
| `SLACK_APP_TOKEN` | (required) | Socket Mode app token (xapp-...) |
| `SLACK_BOT_TOKEN` | (required) | Bot OAuth token (xoxb-...) |
| `SLACK_ALLOWED_USER_IDS` | (see note) | Comma-separated user IDs — used when the lane fragment omits `allowed_user_ids` |
| `TIGER_MEMORY_CLI` | (none) | Path to tiger-memory binary for this lane |

> **Allowlist env spelling.** The canonical name is `SLACK_ALLOWED_USER_IDS`:
>
> - **Multi-lane bridge** (`TIGERHARNESS_BRIDGES_CONFIG`): when a lane
>   fragment omits `allowed_user_ids`, the lane env file must supply the
>   canonical `SLACK_ALLOWED_USER_IDS`.
> - **`notify` CLI** (`python -m tigerharness.slack_bridge.notify`): reads
>   canonical `SLACK_ALLOWED_USER_IDS` first, then falls back to the legacy
>   `ALLOWED_SLACK_USER_IDS` (the migration aid for older `.env` files —
>   the removed single-tenant loader was the only legacy-only reader).

## Running

```bash
# Point at your lanes index (one team = a one-lane index), then:
export TIGERHARNESS_BRIDGES_CONFIG=~/projects/teams/slack-bridge.yaml
python -m tigerharness.slack_bridge

# As a systemd user unit (recommended) — `gen-service` bakes the index
# path in for you; see the gen-service section below.
```

Running `python -m tigerharness.slack_bridge` with **no**
`TIGERHARNESS_BRIDGES_CONFIG` exits at startup with a migration
pointer — the single-tenant fallback was removed on 2026-08-11
(ADR 0009). See
[migrating off single-tenant](#migrating-off-single-tenant).

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

### Joining untracked threads (replies to notification DMs)

`notify` and the slack-notify skill post DMs via raw `chat.postMessage`
— deliberately without touching the bridge's `threads.json` (that store
stays single-writer per process family). So the first reply a user
sends inside such a thread carries a `thread_ts` the bridge has never
seen.

The bridge treats this as a **join**: before opening the new session it
fetches the thread's messages via `conversations.replies` (bot token;
needs the conversation type's `*:history` scope — `im:history` for DMs,
which receiving DM events already requires) and injects a bounded
transcript into the session's first prompt, so the persona can see the
notification the user is replying to. The transcript also feeds the
persona router, so a bare "Yes, go ahead!" under a
"[Anzai]: Task complete …" notification routes to Anzai rather than the
default persona. The same mechanism heals other blind spots — a lost
`threads.json`, threads that predate the bridge.

Bounds and behavior (`history.py`):

- The thread root is always kept; at most 30 messages / ~8 KB total,
  ~2 KB per message. Over budget, the **oldest non-root** messages are
  dropped first and a gap marker records how many.
- Fail-soft: on any fetch failure (API error, missing scope) the
  dispatch proceeds with a one-line note telling the persona the
  earlier context is unavailable.
- Tracked threads never fetch — their resumed session already carries
  the context. Each adopted thread fetches once; after the first turn
  it is tracked like any other.

## Journal tasks over Slack (scheduling discipline)

A bridge-spawned turn is a chat turn — the wrong place for heavy
journal work. Journal work follows two rules here (rails and billing:
[`subscription-backend.md`](subscription-backend.md)):

**1. Scheduling is allowed — and must stay lean.** When asked to
schedule a journal task from Slack, the persona does the minimum:

1. Collect the brief — the Operator's message verbatim plus only what
   the `journal-new` skill itself requires (title, kind, playbook,
   persona).
2. Run exactly ONE scaffold command (`tigerharness journal new ...`).
   The scaffolder is deliberately LLM-free pure Python — scheduling is
   cheap and must stay that way.
3. Reply with the task id (and task_dir) — one short message.
4. Stop.

No repo exploration, no design work. (Memory sweeps are separately
governed: the bridge's first-turn injection hands the persona session
the `sweep-memory` skill's Slack-bootstrap flow — notify-first, split
gate — so a new thread's first message may legitimately trigger a
sweep.) All real journal work happens later, on the subscription rail,
via `drive-journal` in an interactive session.

**2. Driving is forbidden (hard rule).** A Slack-triggered session
must never drive the journal — no `drive-journal`, no claim, no
graph-walk, no compile turns. `journal claim` enforces this
mechanically: the bridge exports `TIGERHARNESS_SLACK_THREAD_TS` into
every turn it spawns, and claim refuses when that marker is present
(exit 1, nothing mutated). An Operator who deliberately wants a
bridge-side drive can pass `--allow-api-drive` to override (see
[`subscription-backend.md`](subscription-backend.md)).

## The bridge: one process, 1..N lanes

There is **one** Slack bridge. One process serves 1..N teams (lanes)
concurrently — each lane is one Slack app (own bot identity, own tokens,
own persona, own `threads.json`), all multiplexed through one event loop.
**A single team is just a one-lane index** — there is no separate
"single-team bridge" to stand up.

### When to use it

- Always — it is the one supported deployment shape, for one team or many.
- One team → a one-lane index. Several teams (2–10) → more lanes, still one
  process and one systemd unit.
- Each team gets its own bot identity in Slack (different name + avatar).
- Pairs with `tigerharness init`, which auto-registers each new team's lane.

> **Single-tenant mode was removed on 2026-08-11 (ADR 0009).** Running
> the bridge with no `TIGERHARNESS_BRIDGES_CONFIG` now exits at startup
> with a migration pointer — the announced removal of the deprecated
> single-team env-var deployment. Use a one-lane index (see
> [migrating off single-tenant](#migrating-off-single-tenant)).

### Set up the index

Point `TIGERHARNESS_BRIDGES_CONFIG` at a top-level **index file**. (With it
unset, the bridge fails fast at startup.)

To create it once:

```bash
# In your teams directory (e.g. ~/projects/teams/)
touch slack-bridge.yaml
```

From then on, every `tigerharness init` auto-appends the new team's
lane to this index and writes a per-team fragment under
`<team>/configs/slack-bridge.yaml`.

### Migrating off single-tenant

If you ran the single-tenant bridge (no `TIGERHARNESS_BRIDGES_CONFIG`,
tokens in a plain `.env` — removed 2026-08-11, ADR 0009), move to a
one-lane index — it reproduces your setup and is the supported path:

1. Make your single team a lane. Ensure the team dir has
   `configs/.env` (the same `SLACK_APP_TOKEN` / `SLACK_BOT_TOKEN` you used)
   and `configs/personas.yaml`; add a per-team fragment
   `configs/slack-bridge.yaml` with `default_persona`, `allowed_user_ids`,
   and `state_dir`. (`tigerharness init` writes these for you.)
2. Create the top-level index listing that one lane:

   ```bash
   # in your teams dir
   printf 'lanes:\n  - <your-team>\n' > slack-bridge.yaml
   ```

3. Point the bridge at it and regenerate the unit:

   ```bash
   export TIGERHARNESS_BRIDGES_CONFIG=$PWD/slack-bridge.yaml
   tigerharness slack-bridge gen-service --teams-root "$PWD"   # emits the unit
   ```

A one-lane index behaves exactly like the old single-team bridge — same
module, same one bot — it just drops the removed env-var entrypoint.
Also switch the allowlist to the canonical `SLACK_ALLOWED_USER_IDS`
spelling while you're in the `.env` (the legacy `ALLOWED_SLACK_USER_IDS`
survives only as a `notify` CLI fallback). If a stale checkout still
tries the old entrypoint, startup names this section — nothing runs
half-migrated.

### Config layout

```
teams/
├── slack-bridge.yaml             ← top-level INDEX (lane names only)
├── shohoku/
│   ├── configs/
│   │   ├── .env                  ← tokens for Shohoku's Slack app
│   │   ├── personas.yaml         ← team personas registry
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
  - U0123ABC                    # at least one Slack user ID; may be omitted (or [])
                                # when the lane env file carries
                                # SLACK_ALLOWED_USER_IDS=U0123ABC,U0456DEF instead
                                # (keeps workspace ids out of a public team repo)
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

- Required fields present, `default_persona` exists in the team's roster, `allowed_user_ids` non-empty + each starts with `U`/`W` (the list may come from the fragment or, when the fragment omits it, from `SLACK_ALLOWED_USER_IDS` in the lane env file — same validation either way).
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

Single-persona teams (only one entry in `personas.yaml`) skip the
prefix — output stays identical to before.

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
restart <your-bridge-unit>`, e.g. `slack-bridge-teams-4a8c8b`) for the
new persona to become routable.

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

If you are coming from the removed single-tenant bridge (or any
pre-PR4 deployment) and migrating to multi-persona, run the migration
tool once to attribute all old entries to a specific persona:

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
> systemctl --user stop <your-bridge-unit>    # e.g. slack-bridge-teams-4a8c8b
> python -m tigerharness.slack_bridge.migrate --state-dir <path> --to <persona>
> systemctl --user start <your-bridge-unit>
> ```

This mirrors the deferred hot-reload decision: lane add/remove also
requires a restart.

### Running the bridge

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
# gen-service prints the exact `Save as:` path on stderr; use that name
# (it already ends in .service), e.g. slack-bridge-teams-4a8c8b.service:
uv run tigerharness slack-bridge gen-service \
    > ~/.config/systemd/user/slack-bridge-teams-4a8c8b.service

systemctl --user daemon-reload
systemctl --user enable --now slack-bridge-teams-4a8c8b.service
```

`gen-service` prints the **per-root unit name** on stderr (e.g.
`slack-bridge-teams-4a8c8b.service`) along with the exact save
+ enable commands — each teams root gets its own unit name (derived
from the root path) so two roots never share one bridge instance.
The name is `slack-bridge-<root-basename>-<hash6>.service`: the
basename says which root at a glance, and the 6-hex digest of the
full resolved root path keeps two roots that share a basename from
colliding.
This is what lets `tigerharness dismiss` tear down a team's bridge
without touching another root's: dismiss discovers the owning unit by
**content** (which root each `slack-bridge-*.service` unit's
`EnvironmentFile` / `TIGERHARNESS_BRIDGES_CONFIG` resolves into), so
the filename is a convenience. The scan glob is deliberately broad, so
units named under the current `slack-bridge-<root>-<hash>` scheme, the
older `slack-bridge-multi-<root>-<hash>` scheme, and the legacy global
`slack-bridge-multi.service` are all found and torn down by content —
no live unit has to be renamed for dismiss to keep working. (The
trailing `-` in the glob means the legacy single-tenant
`slack-bridge.service` is deliberately *not* matched.) A unit whose
config resolves outside the operated root is refused by name, never
stopped or deleted. (Before this, a single global unit name meant
dismissing the last team of one root could stop and delete another
root's bridge — the 2026-06-12 incident.)

`gen-service` is Linux-only. On other platforms it prints a friendly
message and returns 1 (you'd write your own launchd plist / Docker
recipe / whatever fits your stack).

The reference [`examples/slack-bridge-multi.service`](../examples/slack-bridge-multi.service) (with `%h` specifiers) is also available if you'd rather copy + adapt by hand. Key invariants either way:

- `EnvironmentFile=` points at a small `.env` containing only `TIGERHARNESS_BRIDGES_CONFIG=...` (per-lane tokens live in each team's `.env`, referenced by the YAML).
- `TimeoutStopSec=120` still applies; the 90 s drain budget is shared across lanes (concurrent), not per-lane.

## Idle compaction (ADR 0004)

Between tasks, when the journal is idle and the last turn's usage
shows the session's context above a threshold, the bridge sends one
`/compact` turn to the resumed session (the mechanism proved in
`docs/adr/0004-bridge-idle-compaction.md`). Hard rules: never
mid-task (the idle check is the guard), at most one compact per idle
period (a per-thread latch cleared by the next real turn), and
fail-soft everywhere — a failed compact logs and skips, never breaks
a turn.

### Multi-team: per-lane fragment flag (the normal path)

A single bridge process serves N lanes, but a process-wide env var
can name only one journal. So in multi-team mode each lane configures
idle compaction in its own `slack-bridge.yaml` fragment:

```yaml
idle_compact: true   # on by default for teams scaffolded by `tigerharness init`
```

The journal root is **auto-resolved to `<team>/journal`** — there is
no path to hand-write. A team whose journal has no `active/` directory
disables the feature fail-soft (it never aborts the lane), and a
typo'd flag value reads as off rather than crashing the load. Omit the
key to opt a lane out; threshold and window keep their defaults
(`0.30` / `200000`) — tuning them is a future fragment field.

New teams ship with `idle_compact: true` (Operator default,
2026-06-27). To enable an existing lane, add the line to its fragment
and restart the bridge.

### The external pass: `tigerharness slack-bridge compact-idle`

The bridge's own hook fires only at a lane's **turn boundary** — a lane
left heavy while the journal was busy stays heavy until its next Slack
turn. The `compact-idle` subcommand closes that gap from the *driver*
side: an idle drive (an autodrive tick, or a manual `drive-journal`
whose sweep found the queue drained) runs one external pass. Same
config (`idle_compact: true` in the team fragment), same journal-idle
guard, same one-`/compact`-turn mechanism. The pass resolves the
lane's `agent_cwd` itself and pins the resumed `claude` subprocess to
it (`--resume` only finds a session from the project directory it was
opened under), so the command works from any invoking directory —
`--team-dir` names the team.

To make this possible the bridge stamps turn metadata into each
`threads.json` record at its turn boundary: `team`, `last_usage`,
`last_turn_at`, and an `in_flight` marker held for the duration of a
turn. The pass only considers records stamped with the invoking team's
name, skips `in_flight` and recently-active lanes
(`--min-quiet-seconds`, default 120), requires the stamped usage to
cross the threshold, checks the journal is idle, and clears
`last_usage` after compacting (the one-per-idle-period latch — only a
real future turn can re-arm the lane). Records written by an older
bridge lack the stamps and are simply skipped until their next turn.
Every gate fails soft; the command prints a JSON report and exits 0.

### Env fallback (no per-lane config)

When a lane supplies no `idle_compact` config, the bridge falls back
to this env surface (this section is the single home for these
names):

- `TIGERHARNESS_IDLE_COMPACT` — `1`/`true` to enable. **Default off.**
- `TIGERHARNESS_IDLE_COMPACT_JOURNAL` — the journal root (REQUIRED;
  an absent or invalid root disables the feature rather than
  guessing — a guessed root could compact during real work).
- `TIGERHARNESS_IDLE_COMPACT_THRESHOLD` — context fraction, default
  `0.30`.
- `TIGERHARNESS_IDLE_COMPACT_WINDOW` — window tokens, default
  `200000`.

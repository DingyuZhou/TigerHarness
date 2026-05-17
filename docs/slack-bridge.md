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

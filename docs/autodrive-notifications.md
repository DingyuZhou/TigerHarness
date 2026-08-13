# autodrive notifications

## At a glance

- **What:** the [`tigerharness autodrive`](autodrive.md) daemon posts a
  **heartbeat to a Slack channel on every cycle** — a fire heartbeat when it
  launches a drive, a skip pulse when the queue probe declines to — then
  **threads a real drive status + summary** under each fire heartbeat once
  that drive finishes. The steady rhythm of heartbeats is the health signal;
  the threaded reply is the substance.
- **Why:** autodrive is an *unattended, detached* daemon. Notifications are the
  Operator's only live window into it. A heartbeat every cycle means **its
  absence tells you the daemon died** — no separate watchdog needed. That is
  why a cycle which spends nothing still pulses: silence must mean exactly one
  thing.
- **Must-not-miss:** notifications are **model-free** (a plain Slack HTTP POST,
  never a spawned agent), they **never break a drive** (every failure is
  swallowed + logged), and they are **mutable** (`--notify none`). When muted,
  `tigerharness autodrive status` is the always-available pull-based health
  check.

## The model: heartbeat on fire, summary on done

autodrive's loop already splits each drive into two events (see
[`runner.run_loop`](../src/tigerharness/autodrive/runner.py)): a **fire**
(launch) and a **completion** (the drive returns or raises). The notification
model maps one-to-one onto that split:

1. **On fire — post a heartbeat** (the parent message) to the configured
   channel:

   ```
   autodrive heartbeat - fire #42 launched 2026-06-26T14:00:00Z (in-flight 1)
   ```

   The post returns Slack's message `ts`, which the loop holds on the in-flight
   drive as its **thread handle**.

2. **On completion — reply in that fire's thread** with the real outcome:

   ```
   done: stop_reason=end_turn  cost=$0.12
   Completed task b5-doc-revise; released as done. Queue now idle.
   ```

   or, when the drive raised:

   ```
   FAILED: RuntimeError: claude: not authenticated
   ```

The summary line is the drive's own closing message
(`RunResult.final_output`), truncated for Slack. So you get a beat per fire
(liveness) and, attached to each beat, exactly what that drive did.

Because fires **overlap** (the loop never waits for a drive), several heartbeat
threads can be open at once; each in-flight drive carries its own thread handle,
so every completion lands under the right heartbeat.

3. **On a cycle that fires nothing — post a skip pulse.** The ADR 0010 queue
   probe lets a cycle decline to spend a drive (queue busy, a drive still
   settling, or a rescue held back). Such a cycle still pulses, with the same
   prefix so the channel reads as one rhythm:

   ```
   autodrive heartbeat - no fire at 2026-08-12T23:10:00Z - queue busy - a
   live session owns the in-flight task (in-flight 1, 1 drive(s) so far)
   ```

   A skip pulse is a parent message with no thread: there is no drive, so
   there is no completion to thread under it.

   The three reasons, and what each tells you:

   | Reason | Means |
   | --- | --- |
   | `queue busy - a live session owns the in-flight task` | Someone is working; nothing for the daemon to add. |
   | `queue idle - waiting on an in-flight drive` | The daemon's own drive is still settling. |
   | `rescue held - a drive is already out` | The queue shows a crashed task, but the daemon has a drive in flight and will not pile a second session onto it. See [autodrive.md](autodrive.md#the-daemon-never-rescues-on-top-of-its-own-drive). |

   A `rescue held` pulse repeating for many cycles is worth a look: it means
   an in-flight drive is not landing. That is a *reported* stall, which is
   the point — the alternative it replaced was an unreported stampede.

   **Why this exists.** The probe originally made the no-work cycle both free
   *and* silent, so a long busy stretch looked identical to a dead daemon --
   the exact failure the heartbeat is meant to rule out. The rhythm must
   track the daemon's *health*, not its *spending*. Restoring it costs
   nothing: a pulse is a plain HTTP POST, never a drive.

### Why a beat *per fire*, not a digest

A fixed-cadence beat is a dead-man's switch: you learn the daemon's health from
the *rhythm*, not from any single message. If beats stop arriving, the daemon is
dead or wedged — something it cannot self-report any other way. The threaded
reply keeps the channel readable: the top level is a clean pulse, and the detail
is one click down.

## Configuration

Two new `autodrive start` flags, persisted into the state file so the detached
`_loop` reads them at every tick:

| Flag | Default | Purpose |
|---|---|---|
| `--notify {slack,none}` | `slack` | Notification backend. `none` **mutes** all daemon-level notifications (the loop runs unchanged; only the posting is suppressed). |
| `--notify-channel <id>` | `SLACK_NOTIFY_CHANNEL` if set, else operator DM | Slack channel id (e.g. `C0ABC123`) for daemon-level events. Resolution: flag > `TIGERHARNESS_AUTODRIVE_NOTIFY_CHANNEL` > `SLACK_NOTIFY_CHANNEL` > DM. Inheriting the team-wide key means a team names its ops channel once; pass the literal `dm` at any layer to decline it and get the DM. |

Credentials come from the same place
[`slack-notify`](../src/tigerharness/_bundled_skills/slack-notify/SKILL.md)
reads them
(`SLACK_BOT_TOKEN` + the team's `configs/.env` / `slack-bridge.yaml`). If the
backend is `slack` but creds can't be loaded, autodrive degrades to a
**no-op notifier** (logged once) — a missing token never wedges the daemon.

### Muting and the pull-based fallback

An Operator who finds the channel too chatty can `--notify none`. That silences
the *push*; it does **not** blind them. `tigerharness autodrive status` reads
the state file and reports the full health picture — running / stale-pid, last
fire, in-flight count, drives completed, last stop reason / error, and the
notify config in force. So the mute only moves health from push to pull.

## One autodrive per team

The single-instance guard is **team-scoped**. The autodrive state file (which
*is* the lock) is anchored to the team's canonical journal (`<team>/journal`)
when a command runs from a team root, regardless of any `--journal-dir`
override. A second `start` anywhere in the same team resolves to the same state
file, sees the live pid, and is refused. (For a personal, non-team journal the
state stays under the resolved journal, as before.) The *driven* journal can
still be redirected with `--journal-dir` — only the lock location is
team-canonical.

## Implementation seams

- **`slack_bridge/notify.py`** gains `SlackNotifier.post_text(...) -> str | None`
  (returns the posted message `ts`, the thread handle; `dm_text` keeps its
  `bool` contract) and a `--channel` flag on the `text` CLI subcommand.
- **`autodrive/notifier.py`** (new) defines a tiny vendor-neutral seam:
  `Notifier` with `heartbeat(text) -> str | None` and
  `update(thread, text) -> None`; a `NullNotifier` (mute / no creds) and a
  Slack-backed implementation; and `build_notifier(notify, channel)`. Every
  method swallows its own errors — a notifier must never propagate into the
  loop.
- **`autodrive/runner.py`** — `AutodriveConfig` gains `notify` +
  `notify_channel`; `run_loop` takes an injected `notifier` (default
  `NullNotifier`), posts the heartbeat on each fire, holds the thread handle on
  the in-flight drive, and threads the completion/error update. Notifier calls
  run via `asyncio.to_thread` so a slow Slack POST never blocks the event loop;
  pending updates are drained on stop.
- **`autodrive/cli.py`** — `start` parses + persists the flags and uses the
  team-canonical state anchor; `_loop` builds the notifier from config and
  passes it to `run_loop`; `status` prints the notify config.

## Non-goals (for now)

- **Budget alerting.** Cost is *shown* in each completion summary, but there is
  no budget-threshold escalation. (`--max-budget` still caps each drive at the
  backend.) Deferred deliberately.
- **Per-task DMs from the daemon.** "Task blocked, needs a human" already flows
  from the spawned drive itself via the `drive-journal` + `slack-notify`
  contract. The daemon owns only the cross-drive heartbeat/summary layer.
- **External liveness watchdog.** The heartbeat rhythm is the liveness signal;
  a true "the daemon died" alert from outside the process is out of scope.

## Related

- [autodrive.md](autodrive.md) — the daemon these notifications describe.
- [slack-bridge.md](slack-bridge.md#turn-progress-heartbeats-ops-log)
  — the *other* Slack heartbeat. These pulse per autodrive **cycle**;
  those pulse inside a single long **bridge turn**. Both can target
  the same channel, so check which one you expected before
  concluding a quiet ops-log is broken.
- [slack-notify](../src/tigerharness/_bundled_skills/slack-notify/SKILL.md) —
  the outbound Slack path the notifier reuses.

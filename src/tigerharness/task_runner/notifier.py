"""Slack DM notifier for task-runner job lifecycle events.

When a job finishes (done / cancelled / error) the runner sends a
short DM to the user with status + cost + a result preview.

This is intentionally minimal:

- stdlib `urllib.request` (no aiohttp dependency).
- Best-effort: a notification failure must NEVER fail the job. Errors
  are logged and swallowed.
- Creds discovery: env first, then a manual parse of the slack-bridge
  `.env` if present. No `python-dotenv` dependency.
- Skips silently with a single log line if `SLACK_BOT_TOKEN` or a
  user id can't be resolved.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

from .registry import JobMeta, JobStore


log = logging.getLogger("tigerharness.task_runner.notifier")


_API_BASE = "https://slack.com/api"


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------

def _find_slack_env_file() -> Path | None:
    """Find the slack-bridge .env file.

    Checks in order:
    1. TIGERHARNESS_SLACK_ENV env var (explicit override)
    2. Colocated .env in the slack-bridge service dir (if configured)
    3. cwd/configs/.env (team-folder layout from ``tigerharness init``)
    4. None

    Step 3 mirrors the discovery in ``slack_bridge/notify.py`` so that
    detached task-runner children (whose cwd is the team root) find
    the team's Slack tokens automatically.
    """
    explicit = os.environ.get("TIGERHARNESS_SLACK_ENV", "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        if p.exists():
            return p
    # Try sibling slack_bridge package's .env
    bridge_dir = os.environ.get("TIGERHARNESS_SLACK_BRIDGE_DIR", "").strip()
    if bridge_dir:
        p = Path(bridge_dir).expanduser() / ".env"
        if p.exists():
            return p
    # Team-folder layout: cwd/configs/.env (task-runner child processes
    # run with cwd = persona.cwd, which is the team root for team-based
    # layouts scaffolded by `tigerharness init`).
    team_env = Path.cwd() / "configs" / ".env"
    if team_env.exists():
        return team_env
    return None


def _load_bridge_dotenv_into_env() -> None:
    """Parse slack-bridge .env and copy SLACK_* keys into os.environ
    if not already set."""
    env_path = _find_slack_env_file()
    if env_path is None:
        return
    try:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError as exc:
        log.warning("notifier: could not read .env: %r", exc)


def _first_allowed_user_from_yaml(path: Path) -> str | None:
    """Best-effort read of ``allowed_user_ids[0]`` from a slack-bridge
    fragment YAML file.  Returns ``None`` on any failure so the caller
    falls back to env-var resolution.

    Mirrors ``slack_bridge.notify._first_allowed_user_from_yaml`` so
    the task-runner notifier and the bridge notify CLI agree on who to
    DM when the env vars aren't set.
    """
    if not path.exists():
        return None
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, Exception):  # noqa: BLE001 — best-effort
        return None
    if not isinstance(data, dict):
        return None
    ids = data.get("allowed_user_ids")
    if not isinstance(ids, list):
        return None
    for entry in ids:
        if isinstance(entry, str) and entry.strip():
            return entry.strip()
    return None


def _resolve_creds() -> tuple[str, str] | None:
    """Returns (bot_token, target_user_id) or None if either is missing.

    Resolution order for the target user:
    1. ``SLACK_CEO_USER_ID`` env var (explicit override)
    2. ``cwd/configs/slack-bridge.yaml`` → ``allowed_user_ids[0]``
    3. ``ALLOWED_SLACK_USER_IDS`` env var (comma-separated, first entry)
    """
    _load_bridge_dotenv_into_env()
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not token:
        return None
    ceo = os.environ.get("SLACK_CEO_USER_ID", "").strip()
    if not ceo:
        ceo = _first_allowed_user_from_yaml(
            Path.cwd() / "configs" / "slack-bridge.yaml"
        ) or ""
    if not ceo:
        allow = os.environ.get("ALLOWED_SLACK_USER_IDS", "")
        for entry in allow.split(","):
            entry = entry.strip()
            if entry:
                ceo = entry
                break
    if not ceo:
        return None
    return token, ceo


# ---------------------------------------------------------------------------
# Low-level HTTP
# ---------------------------------------------------------------------------

def _post_json(endpoint: str, token: str, payload: dict) -> dict | None:
    """One-shot POST to slack.com/api/<endpoint>. Best-effort: logs and
    returns None on failure, never raises.

    Returns the full Slack response dict on success (caller can extract
    ``ts``, ``channel``, etc.).
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{_API_BASE}/{endpoint}",
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError) as exc:
        log.warning("notifier: POST %s transport error: %r", endpoint, exc)
        return None
    if not body.get("ok"):
        log.warning(
            "notifier: POST %s returned ok=false error=%s",
            endpoint, body.get("error"),
        )
        return None
    return body


# ---------------------------------------------------------------------------
# Dual-post helper
# ---------------------------------------------------------------------------

def _post_notification(
    token: str,
    target_user: str,
    text: str,
    *,
    thread_ts: str = "",
) -> dict | None:
    """Post a notification, handling the channel/thread mismatch.

    When ``SLACK_NOTIFY_CHANNEL`` is set AND ``thread_ts`` is set, the
    notification goes to **two** destinations:

    1. ``SLACK_NOTIFY_CHANNEL`` — top-level message (no ``thread_ts``,
       because that ts belongs to a different channel).
    2. ``target_user`` — in-thread reply (with ``thread_ts``), so the
       user sees the update inline in their DM conversation.

    When only one is set, posts once to the appropriate destination.
    Returns the first successful Slack response, or None if all fail.
    """
    notify_channel = os.environ.get("SLACK_NOTIFY_CHANNEL", "").strip()
    result: dict | None = None

    if notify_channel:
        # Ops-log: always top-level (never pass thread_ts from a DM).
        channel_payload: dict[str, str] = {
            "channel": notify_channel, "text": text,
        }
        result = _post_json("chat.postMessage", token, channel_payload)

    if thread_ts:
        # DM thread: reply in the user's conversation thread.
        thread_payload: dict[str, str] = {
            "channel": target_user,
            "text": text,
            "thread_ts": thread_ts,
        }
        thread_result = _post_json("chat.postMessage", token, thread_payload)
        if result is None:
            result = thread_result
    elif not notify_channel:
        # No channel, no thread — plain DM to the user.
        dm_payload: dict[str, str] = {"channel": target_user, "text": text}
        result = _post_json("chat.postMessage", token, dm_payload)

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def notify_job_end(meta: JobMeta, store: JobStore) -> bool:
    """DM the user with the final status of a job.

    Called from `runner.run_job` in the `finally` block, regardless of
    outcome. Returns True on success, False if anything failed or creds
    were missing. **Never raises.**
    """
    try:
        if not getattr(meta, "notify", True):
            log.info("notifier: notify=False; skipping DM for job=%s", meta.job_id)
            return False

        creds = _resolve_creds()
        if creds is None:
            log.info("notifier: slack creds not available; skipping notify for job=%s", meta.job_id)
            return False
        token, target_user = creds

        text = _render(meta, store)
        thread_ts = getattr(meta, "slack_thread_ts", "")
        return _post_notification(
            token, target_user, text, thread_ts=thread_ts,
        ) is not None
    except Exception as exc:  # last-ditch safety net
        log.exception("notifier: unexpected failure for job=%s: %r", meta.job_id, exc)
        return False


def notify_stuck_escalation(
    meta: JobMeta,
    *,
    iter_num: int,
    detail: str = "",
) -> bool:
    """DM the user when the stuck-watchdog escalates an iteration.

    Best-effort: returns False and logs on any failure. **Never raises.**
    Posts to ops-log (if configured) AND the DM thread (if set).
    """
    try:
        if not getattr(meta, "notify", True):
            log.info("notifier: notify=False; skipping stuck DM for job=%s", meta.job_id)
            return False
        creds = _resolve_creds()
        if creds is None:
            log.info("notifier: slack creds not available; skipping stuck DM for job=%s", meta.job_id)
            return False
        token, ceo = creds

        headline = f"iter {iter_num} exceeded stuck timeout"
        lines = [f":rotating_light: task-runner job `{meta.job_id}` {headline}"]
        if meta.name:
            lines.append(f"name: _{meta.name}_")
        if detail:
            lines.append(detail)

        thread_ts = getattr(meta, "slack_thread_ts", "")
        return _post_notification(
            token, ceo, "\n".join(lines), thread_ts=thread_ts,
        ) is not None
    except Exception as exc:
        log.exception("notify_stuck_escalation: unexpected failure for job=%s: %r", meta.job_id, exc)
        return False


def notify_job_start(meta: JobMeta) -> str:
    """Post a 'job started' notification and return the thread anchor ``ts``.

    Posts to ops-log (if configured) AND the DM thread (if ``--thread``
    was passed). The returned ``ts`` is the DM thread anchor for
    subsequent notifications.

    Returns ``""`` if no DM could be sent. **Never raises.**
    """
    try:
        if not getattr(meta, "notify", True):
            log.info("notifier: notify=False; skipping start DM for job=%s", meta.job_id)
            return ""

        creds = _resolve_creds()
        if creds is None:
            log.info("notifier: slack creds not available; skipping start DM for job=%s", meta.job_id)
            return ""

        token, target_user = creds

        cap_label = str(meta.max_iters) if meta.max_iters > 0 else "forever"
        name_part = f"  |  _{meta.name}_" if meta.name else ""
        text = (
            f":arrow_forward: task-runner job `{meta.job_id}` started\n"
            f"persona: `{meta.persona}`  |  iters: {cap_label}{name_part}"
        )

        existing_ts = getattr(meta, "slack_thread_ts", "")
        result = _post_notification(
            token, target_user, text, thread_ts=existing_ts,
        )
        if result is None:
            return ""

        if existing_ts:
            return existing_ts
        return result.get("ts", "")
    except Exception as exc:
        log.exception("notify_job_start raised; ignored: %r", exc)
        return ""


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_STATUS_ICON = {
    "done":      ":white_check_mark:",
    "cancelled": ":octagonal_sign:",
    "error":     ":warning:",
}


def _render(meta: JobMeta, store: JobStore) -> str:
    """Build the Slack message body. Skim-friendly for phone."""
    icon = _STATUS_ICON.get(meta.status, ":information_source:")
    iter_label = (
        f"{meta.current_iter}/{meta.max_iters}"
        if meta.max_iters > 0
        else f"{meta.current_iter}/forever"
    )
    cost = f"${meta.total_cost_usd:.4f}" if meta.total_cost_usd else "$0"

    lines = [
        f"{icon} task-runner job `{meta.job_id}` *{meta.status}*",
        f"persona: `{meta.persona}`  |  iter: {iter_label}  |  cost: {cost}",
    ]
    if meta.name:
        lines.append(f"name: _{meta.name}_")
    if meta.status == "error" and meta.error:
        err = meta.error.splitlines()[0][:200]
        lines.append(f"error: `{err}`")

    # Preview of latest result (truncate aggressively for mobile).
    result_path = store.result_path(meta.job_id)
    if result_path.exists():
        try:
            preview = result_path.read_text().strip()
        except OSError:
            preview = ""
        if preview:
            preview = preview[:400]
            lines.append("latest reply:")
            lines.append(f"```\n{preview}\n```")

    lines.append(
        f"_inspect:_ `python -m tigerharness.task_runner show {meta.job_id}`"
    )
    return "\n".join(lines)

"""Outbound Slack helpers: DMs + file uploads.

Two surfaces:

1. **Python API** -- `SlackNotifier.dm_text()` and `dm_file()`.
2. **CLI** -- `python -m tigerharness.slack_bridge.notify <subcommand>`.

Both use the same auth: ``SLACK_BOT_TOKEN`` env var + a target user id.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


log = logging.getLogger("tigerharness.slack_bridge.notify")


_API_BASE = "https://slack.com/api"


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------

def _load_slack_bridge_dotenv() -> None:
    """If the slack-bridge .env exists and the relevant vars aren't
    already in os.environ, parse it manually."""
    candidates: list[Path] = []
    env_override = os.environ.get("TIGERHARNESS_SLACK_ENV", "").strip()
    if env_override:
        candidates.append(Path(env_override).expanduser())
    candidates.append(Path.cwd() / ".env")
    # `tigerharness init` puts the team's .env at <team>/configs/.env.
    # When an agent is invoked from the team root (the default for
    # detached personas), this candidate lets `notify` find the
    # right team's bot tokens without an explicit TIGERHARNESS_SLACK_ENV.
    candidates.append(Path.cwd() / "configs" / ".env")
    # Also check our own package's parent dir
    pkg_env = Path(__file__).resolve().parents[1] / ".env"
    candidates.append(pkg_env)

    for env_path in candidates:
        if not env_path.exists():
            continue
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
        return


def _resolve_target_user_id() -> str | None:
    """Resolution order: explicit env override -> multi-team yaml ->
    legacy env var -> None.

    The yaml step covers the multi-team team-folder layout: when an
    agent runs from a team root, ``configs/slack-bridge.yaml`` carries
    the authoritative ``allowed_user_ids`` -- the bridge's allowlist
    and notify's "who do I DM?" choice should agree.
    """
    override = os.environ.get("SLACK_CEO_USER_ID", "").strip()
    if override:
        return override
    from_yaml = _first_allowed_user_from_yaml(Path.cwd() / "configs" / "slack-bridge.yaml")
    if from_yaml:
        return from_yaml
    allow = os.environ.get("ALLOWED_SLACK_USER_IDS", "")
    for entry in allow.split(","):
        entry = entry.strip()
        if entry:
            return entry
    return None


# Module-level sentinel: log the pyyaml-missing diagnostic at most once
# per process. Repeated logging would spam long-running detached
# jobs that call notify many times.
_PYYAML_MISSING_LOGGED = False


def _first_allowed_user_from_yaml(path: Path) -> str | None:
    """Best-effort read of ``allowed_user_ids[0]`` from a slack-bridge
    fragment. Returns ``None`` on any failure (missing file, parse
    error, no pyyaml installed, empty list) so the caller falls back
    to the env-var path.

    Diagnostic note: when pyyaml is missing, the yaml-driven path is
    silently unavailable. A user with only ``[slack]`` installed (no
    ``[memory]``) would wonder why their fragment's ``allowed_user_ids``
    isn't being read. Logging at DEBUG (once per process, via the
    ``_PYYAML_MISSING_LOGGED`` sentinel) gives them a trail to follow.
    """
    if not path.exists():
        return None
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        global _PYYAML_MISSING_LOGGED
        if not _PYYAML_MISSING_LOGGED:
            log.debug(
                "pyyaml not installed; skipping yaml-based allowlist lookup at %s. "
                "Install with `pip install 'tigerharness[memory]'` (or just pyyaml) "
                "to use the per-team slack-bridge.yaml as a single source of truth.",
                path,
            )
            _PYYAML_MISSING_LOGGED = True
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
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


@dataclass(frozen=True)
class _Creds:
    bot_token: str
    target_user_id: str


def _load_creds() -> _Creds | None:
    """Returns None if either piece is missing."""
    _load_slack_bridge_dotenv()
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    target = _resolve_target_user_id()
    if not token:
        log.warning("notify: SLACK_BOT_TOKEN not set; skipping")
        return None
    if not target:
        log.warning(
            "notify: no target user id (set SLACK_CEO_USER_ID or "
            "ALLOWED_SLACK_USER_IDS); skipping"
        )
        return None
    return _Creds(bot_token=token, target_user_id=target)


# ---------------------------------------------------------------------------
# Low-level HTTP
# ---------------------------------------------------------------------------

def _slack_post_json(endpoint: str, token: str, payload: dict) -> dict[str, Any]:
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
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        log.warning("slack POST %s failed: %r", endpoint, exc)
        return {"ok": False, "error": f"transport: {exc}"}


def _slack_post_form(endpoint: str, token: str, payload: dict) -> dict[str, Any]:
    import urllib.parse
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{_API_BASE}/{endpoint}",
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        log.warning("slack POST %s failed: %r", endpoint, exc)
        return {"ok": False, "error": f"transport: {exc}"}


def _resolve_dm_channel(token: str, user_id: str) -> str | None:
    """Open (or fetch) the DM channel id for a given user id."""
    result = _slack_post_form(
        "conversations.open", token, {"users": user_id, "return_im": "true"}
    )
    if not result.get("ok"):
        log.warning(
            "conversations.open failed for user_id=%s: %s",
            user_id, result.get("error"),
        )
        return None
    channel = result.get("channel") or {}
    return channel.get("id")


def _put_bytes(url: str, data: bytes) -> bool:
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return 200 <= resp.status < 300
    except urllib.error.URLError as exc:
        log.warning("slack upload-URL POST failed: %r", exc)
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class SlackNotifier:
    """Stateful wrapper holding creds + posting helpers."""

    def __init__(self, creds: _Creds) -> None:
        self._creds = creds

    @classmethod
    def try_load(cls) -> "SlackNotifier | None":
        """Build from env + .env. Returns None if creds incomplete."""
        creds = _load_creds()
        if creds is None:
            return None
        return cls(creds)

    # ---- text DM ----

    def dm_text(
        self,
        text: str,
        *,
        channel: str | None = None,
        thread_ts: str | None = None,
    ) -> bool:
        """Post a text message. Default channel = target user's DM."""
        target = channel or self._creds.target_user_id
        payload: dict[str, Any] = {"channel": target, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        result = _slack_post_json(
            "chat.postMessage", self._creds.bot_token, payload
        )
        if not result.get("ok"):
            log.warning(
                "notify.dm_text failed: error=%s target=%s",
                result.get("error"), target,
            )
            return False
        return True

    # ---- file upload ----

    def dm_file(
        self,
        path: str | Path,
        *,
        caption: str = "",
        channel: str | None = None,
        thread_ts: str | None = None,
    ) -> bool:
        """Upload ``path`` to the target channel via the 3-step flow."""
        path = Path(path)
        if not path.exists() or not path.is_file():
            log.warning("notify.dm_file: missing or not a file: %s", path)
            return False
        data = path.read_bytes()
        size = len(data)
        if size == 0:
            log.warning("notify.dm_file: empty file: %s", path)
            return False

        if channel:
            target_channel = channel
        else:
            target_channel = _resolve_dm_channel(
                self._creds.bot_token, self._creds.target_user_id
            )
            if target_channel is None:
                log.warning(
                    "notify.dm_file: couldn't open DM channel for user %s",
                    self._creds.target_user_id,
                )
                return False

        step1 = _slack_post_form(
            "files.getUploadURLExternal",
            self._creds.bot_token,
            {"filename": path.name, "length": size},
        )
        if not step1.get("ok"):
            log.warning(
                "notify.dm_file step1 (getUploadURLExternal) failed: %s",
                step1.get("error"),
            )
            return False

        upload_url = step1.get("upload_url")
        file_id = step1.get("file_id")
        if not upload_url or not file_id:
            log.warning(
                "notify.dm_file step1 succeeded but missing upload_url/file_id"
            )
            return False

        if not _put_bytes(upload_url, data):
            log.warning("notify.dm_file step2 (raw upload) failed")
            return False

        complete_payload: dict[str, Any] = {
            "files": json.dumps([{"id": file_id, "title": path.name}]),
            "channel_id": target_channel,
        }
        if caption:
            complete_payload["initial_comment"] = caption
        if thread_ts:
            complete_payload["thread_ts"] = thread_ts
        step3 = _slack_post_form(
            "files.completeUploadExternal",
            self._creds.bot_token,
            complete_payload,
        )
        if not step3.get("ok"):
            log.warning(
                "notify.dm_file step3 (completeUploadExternal) failed: %s",
                step3.get("error"),
            )
            return False
        return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cmd_text(args: argparse.Namespace) -> int:
    n = SlackNotifier.try_load()
    if n is None:
        print("error: slack creds not configured", file=sys.stderr)
        return 2
    ok = n.dm_text(args.text, thread_ts=args.thread or None)
    return 0 if ok else 1


def _cmd_file(args: argparse.Namespace) -> int:
    n = SlackNotifier.try_load()
    if n is None:
        print("error: slack creds not configured", file=sys.stderr)
        return 2
    ok = n.dm_file(args.file, caption=args.comment or "", thread_ts=args.thread or None)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(
        prog="tigerharness.slack_bridge.notify",
        description="DM a user or upload a file to a Slack thread.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("text", help="Send a text DM.")
    t.add_argument("text")
    t.add_argument("--thread", default="",
                   help="Reply in this thread_ts.")
    t.set_defaults(func=_cmd_text)

    f = sub.add_parser("file", help="Upload a file.")
    f.add_argument("--file", required=True, help="Path to the file to upload.")
    f.add_argument("--comment", default="", help="Caption.")
    f.add_argument("--thread", default="",
                   help="Share into this thread_ts.")
    f.set_defaults(func=_cmd_file)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

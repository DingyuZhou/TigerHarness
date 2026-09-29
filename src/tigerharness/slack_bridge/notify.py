"""Outbound Slack helpers: DMs + file uploads.

Two surfaces:

1. **Python API** -- `SlackNotifier.dm_text()` and `dm_file()`.
2. **CLI** -- `python -m tigerharness.slack_bridge.notify <subcommand>`.

Both use the same auth: ``SLACK_BOT_TOKEN`` env var + a target user id.

Config resolution is **read-only with respect to the process
environment**: :func:`_read_slack_bridge_dotenv` parses the team ``.env``
into a dict, :func:`_resolve_env` states the ``os.environ``-beats-file
precedence once, and the dict is threaded explicitly to every consumer
(notably :func:`_ssl_context`, so ``SSL_CERT_FILE`` still reaches rung 1).
Nothing here writes to ``os.environ`` -- a Slack post must not export an
operator's unrelated secrets to the rest of the process and to every
child it spawns.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


log = logging.getLogger("tigerharness.slack_bridge.notify")


_API_BASE = "https://slack.com/api"


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------

def _read_slack_bridge_dotenv() -> dict[str, str]:
    """Parse the first slack-bridge ``.env`` that exists into a dict,
    **without touching** ``os.environ``. Returns ``{}`` when no candidate
    file exists.

    Candidates, first existing one winning outright:
    ``$TIGERHARNESS_SLACK_ENV`` -> ``<cwd>/.env`` ->
    ``<cwd>/configs/.env`` -> ``<package parent>/.env``.

    This used to write every parsed key into ``os.environ`` and leave it
    there for the life of the process -- so one Slack post exported a
    team's whole ``.env``, secrets for unrelated services included, to
    everything downstream (including every child process spawned with
    ``{**os.environ}``). Callers now receive the dict and resolve through
    :func:`_resolve_env`, which states the precedence in one place. This
    mirrors ``multi.py:_load_env_file``, the same shape one layer over.

    The hand-rolled parser stays rather than moving to ``dotenv_values``:
    ``python-dotenv`` is only in the ``slack`` / ``all`` extras, while this
    module is reached from ``journal/cli.py`` and ``autodrive/notifier.py``
    on a bare install with no extras at all. ``multi.py`` can afford to
    ``SystemExit`` on a missing dotenv because it *is* the bridge daemon;
    a journal release notice cannot.
    """
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
        # Declared inside the loop, and returned from inside it, so "the
        # first existing file wins outright" is the shape of the code
        # rather than a comment on it: later candidates are never merged in.
        parsed: dict[str, str] = {}
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                parsed[key] = value
        return parsed
    return {}


def _resolve_env(name: str, env_file: Mapping[str, str] | None = None) -> str:
    """One name, resolved ``os.environ`` first and the parsed ``.env``
    second. The single statement of precedence for every name in this
    module.

    "First" means **present**, not non-empty, which is a faithful port of
    the old loader's ``if key not in os.environ`` guard: a variable
    exported as the empty string shadowed the file then and still does,
    so ``SLACK_BOT_TOKEN=`` in the environment keeps meaning "no token"
    rather than silently falling through to the file.
    """
    if name in os.environ:
        return os.environ[name]
    return (env_file or {}).get(name, "")


def _resolve_target_user_id(env_file: Mapping[str, str] | None = None) -> str | None:
    """Resolution order: explicit env override -> multi-team yaml ->
    allowlist env vars -> None.

    The yaml step covers the multi-team team-folder layout: when an
    agent runs from a team root, ``configs/slack-bridge.yaml`` carries
    the authoritative ``allowed_user_ids`` -- the bridge's allowlist
    and notify's "who do I DM?" choice should agree.

    The env step checks ``SLACK_ALLOWED_USER_IDS`` first (the canonical
    name -- what ``tigerharness init`` writes to ``configs/.env`` and
    the multi-root bridge loader reads) and falls back to the legacy
    notify-only spelling ``ALLOWED_SLACK_USER_IDS``. Both accept
    comma/whitespace-separated ids, matching the bridge loader's
    format; the first usable id wins.

    ``env_file`` is the parsed team ``.env`` from
    :func:`_read_slack_bridge_dotenv`; every "env" lookup here goes
    through :func:`_resolve_env`, so the file is consulted only after
    the process environment. Omitting it reads the process environment
    alone.
    """
    override = _resolve_env("SLACK_CEO_USER_ID", env_file).strip()
    if override:
        return override
    from_yaml = _first_allowed_user_from_yaml(Path.cwd() / "configs" / "slack-bridge.yaml")
    if from_yaml:
        return from_yaml
    for env_name in ("SLACK_ALLOWED_USER_IDS", "ALLOWED_SLACK_USER_IDS"):
        for entry in re.split(r"[,\s]+", _resolve_env(env_name, env_file)):
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
    #: The parsed team ``.env`` these creds came from, carried so the
    #: transports can resolve ``SSL_CERT_FILE`` from the same file the
    #: token came from. It replaces the old ``os.environ`` injection as
    #: the route from :func:`_load_creds` to :func:`_ssl_context`.
    #:
    #: ``repr=False`` is not cosmetic: this dict is a whole ``.env``, so
    #: an ordinary dataclass repr would print bot tokens (and whatever
    #: else the operator keeps in that file) into any traceback or log
    #: line that happens to render a ``_Creds``. ``compare=False`` keeps
    #: the generated ``__eq__``/``__hash__`` over the two scalar fields,
    #: which an unhashable dict field would otherwise break.
    env_file: Mapping[str, str] = field(
        default_factory=dict, repr=False, compare=False
    )


def _load_creds() -> _Creds | None:
    """Returns None if either piece is missing."""
    env_file = _read_slack_bridge_dotenv()
    token = _resolve_env("SLACK_BOT_TOKEN", env_file).strip()
    target = _resolve_target_user_id(env_file)
    if not token:
        log.warning("notify: SLACK_BOT_TOKEN not set; skipping")
        return None
    if not target:
        log.warning(
            "notify: no target user id (set SLACK_CEO_USER_ID or "
            "SLACK_ALLOWED_USER_IDS); skipping"
        )
        return None
    return _Creds(
        bot_token=token, target_user_id=target, env_file=env_file
    )


# ---------------------------------------------------------------------------
# TLS trust store
# ---------------------------------------------------------------------------

def _ssl_context(env_file: Mapping[str, str] | None = None) -> ssl.SSLContext:
    """Build the TLS context for an outbound post, most specific rung first:
    ``SSL_CERT_FILE`` -> ``certifi.where()`` -> the interpreter default.

    Built **per call, never at import and never cached**: the team ``.env``
    is parsed inside :func:`SlackNotifier.try_load`, not at import, so a
    context built at import time would read ``SSL_CERT_FILE`` before the
    ``.env`` was available and silently skip the operator's explicit choice.

    ``env_file`` is how that ``.env`` value reaches rung 1 now that the
    loader no longer writes into ``os.environ``. Every transport passes
    ``self._creds.env_file``, so the trust store and the bot token always
    come from the same file. Called with no dict -- which is what the CLI
    surfaces and the unit tests do -- it reads the process environment
    alone, exactly as before.

    A configured rung that does not work falls through to the next one
    rather than raising -- one typo in an ``.env`` must not take down every
    notification on the subsystem whose failure cannot notify anyone. That
    is only safe because the fall-through is logged at WARNING and because
    reaching the last rung is itself logged; a degrade here is attributed,
    not silent. ``certifi`` merely being absent logs at DEBUG, since it is
    not an actionable misconfiguration and rung 1 may still be serving the
    host correctly.
    """
    candidates: list[tuple[str, str]] = []
    cert_file = _resolve_env("SSL_CERT_FILE", env_file).strip()
    if cert_file:
        candidates.append(("SSL_CERT_FILE", cert_file))
    try:
        import certifi
    except ImportError:
        log.debug("notify: certifi not installed; skipping that trust-store rung")
    else:
        candidates.append(("certifi", certifi.where()))

    for rung, path in candidates:
        # ssl.SSLError and FileNotFoundError are both OSError subclasses, so
        # one handler covers the missing path, the unreadable file and the
        # file that is not a CA bundle.
        try:
            return ssl.create_default_context(cafile=path)
        except OSError as exc:
            log.warning(
                "notify: TLS trust store %s=%s is unusable (%r); "
                "falling through to the next rung", rung, path, exc,
            )
    log.warning(
        "notify: falling back to the interpreter's default TLS trust store; "
        "set SSL_CERT_FILE or install certifi if verification fails"
    )
    return ssl.create_default_context()


def _record_transport_result(
    ok: bool, exc: BaseException | None = None, *, site: str = ""
) -> None:
    """Observe the outcome of one HTTP attempt.

    ``ok`` means **the transport completed** -- not that the post
    succeeded. A Slack ``{"ok": false}`` body and an HTTP 500 both count as
    ``ok=True`` here, because the failure this instruments is a TLS
    handshake dying, which is what ``URLError`` marks. Never raises into
    the notify path.
    """
    from .notify_health import record_transport

    if ok:
        record_transport(True)
        return
    log.warning("notify: slack transport failed (%s): %r", site, exc)
    record_transport(False, error=f"{site}: {exc!r}")


# ---------------------------------------------------------------------------
# Low-level HTTP
# ---------------------------------------------------------------------------

def _slack_post_json(
    endpoint: str,
    token: str,
    payload: dict,
    *,
    env_file: Mapping[str, str] | None = None,
) -> dict[str, Any]:
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
    ctx = _ssl_context(env_file)
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            raw = resp.read()
    except urllib.error.URLError as exc:
        _record_transport_result(False, exc, site=f"POST {endpoint}")
        return {"ok": False, "error": f"transport: {exc}"}
    else:
        _record_transport_result(True)
    return json.loads(raw.decode("utf-8"))


def _slack_post_form(
    endpoint: str,
    token: str,
    payload: dict,
    *,
    env_file: Mapping[str, str] | None = None,
) -> dict[str, Any]:
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
    ctx = _ssl_context(env_file)
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            raw = resp.read()
    except urllib.error.URLError as exc:
        _record_transport_result(False, exc, site=f"POST {endpoint}")
        return {"ok": False, "error": f"transport: {exc}"}
    else:
        _record_transport_result(True)
    return json.loads(raw.decode("utf-8"))


def _resolve_dm_channel(
    token: str, user_id: str, *, env_file: Mapping[str, str] | None = None
) -> str | None:
    """Open (or fetch) the DM channel id for a given user id."""
    result = _slack_post_form(
        "conversations.open",
        token,
        {"users": user_id, "return_im": "true"},
        env_file=env_file,
    )
    if not result.get("ok"):
        log.warning(
            "conversations.open failed for user_id=%s: %s",
            user_id, result.get("error"),
        )
        return None
    channel = result.get("channel") or {}
    return channel.get("id")


def _put_bytes(
    url: str, data: bytes, *, env_file: Mapping[str, str] | None = None
) -> bool:
    req = urllib.request.Request(url, data=data, method="POST")
    ctx = _ssl_context(env_file)
    try:
        with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
            status = resp.status
    except urllib.error.URLError as exc:
        _record_transport_result(False, exc, site="upload-URL POST")
        return False
    else:
        _record_transport_result(True)
    return 200 <= status < 300


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class SlackNotifier:
    """Stateful wrapper holding creds + posting helpers."""

    def __init__(self, creds: _Creds) -> None:
        self._creds = creds

    @classmethod
    def try_load(cls) -> "SlackNotifier | None":
        """Build from env + ``.env``. Returns None if creds incomplete.

        The parsed ``.env`` is carried on the returned notifier's
        ``_Creds`` rather than exported into ``os.environ``, so the
        process this runs in is unchanged by having sent a message.
        """
        creds = _load_creds()
        if creds is None:
            return None
        return cls(creds)

    # ---- text DM ----

    def _post_text(
        self,
        text: str,
        *,
        channel: str | None = None,
        thread_ts: str | None = None,
    ) -> dict[str, Any]:
        """Low-level ``chat.postMessage``. Returns the raw Slack response so
        callers can read either ``ok`` (``dm_text``) or the message ``ts``
        (``post_text``, used for threading replies)."""
        target = channel or self._creds.target_user_id
        payload: dict[str, Any] = {"channel": target, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        result = _slack_post_json(
            "chat.postMessage",
            self._creds.bot_token,
            payload,
            env_file=self._creds.env_file,
        )
        if not result.get("ok"):
            log.warning(
                "notify post failed: error=%s target=%s",
                result.get("error"), target,
            )
        return result

    def dm_text(
        self,
        text: str,
        *,
        channel: str | None = None,
        thread_ts: str | None = None,
    ) -> bool:
        """Post a text message. Default channel = target user's DM."""
        result = self._post_text(text, channel=channel, thread_ts=thread_ts)
        return bool(result.get("ok"))

    def post_text(
        self,
        text: str,
        *,
        channel: str | None = None,
        thread_ts: str | None = None,
    ) -> str | None:
        """Post a message and return its Slack ``ts`` (the thread handle), or
        ``None`` on failure. Use the returned ``ts`` as ``thread_ts`` on a
        later call to reply in the same thread."""
        result = self._post_text(text, channel=channel, thread_ts=thread_ts)
        ts = result.get("ts")
        return ts if isinstance(ts, str) else None

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
                self._creds.bot_token,
                self._creds.target_user_id,
                env_file=self._creds.env_file,
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
            env_file=self._creds.env_file,
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

        if not _put_bytes(upload_url, data, env_file=self._creds.env_file):
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
            env_file=self._creds.env_file,
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

def resolve_task_origin(task_id: str) -> tuple[str, str]:
    """Read ``(thread_ts, channel)`` from a journal task's
    ``deferred_origin.json`` sidecar (archived into the task dir by
    ``journal materialize``). Looks in ``active/`` then ``done/`` then
    ``needs_input/`` under the resolved journal root.

    Fallback contract (the wrong-thread fix's safety net): a missing or
    corrupt sidecar, or one without a ``thread_ts``, logs a WARNING and
    returns ``("", "")`` so the caller keeps the current default
    behavior (top-level operator DM) instead of failing the notify.
    ``channel`` is empty for pre-channel-field sidecars.
    """
    from tigerharness.journal.paths import default_journal_root

    root = default_journal_root()
    for tray in ("active", "done", "needs_input"):
        sidecar = root / tray / task_id / "deferred_origin.json"
        if not sidecar.is_file():
            continue
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning(
                "notify --task %s: %s unreadable (%s); falling back to "
                "the default DM", task_id, sidecar, exc,
            )
            return "", ""
        if not isinstance(data, dict):
            data = {}
        thread_ts = str(data.get("thread_ts") or "").strip()
        channel = str(data.get("channel") or "").strip()
        if not thread_ts:
            log.warning(
                "notify --task %s: %s has no thread_ts; falling back to "
                "the default DM", task_id, sidecar,
            )
            return "", ""
        return thread_ts, channel
    log.warning(
        "notify --task %s: no deferred_origin.json under %s "
        "(active/done/needs_input); falling back to the default DM",
        task_id, root,
    )
    return "", ""


def _routing_from_args(args: argparse.Namespace) -> tuple[str, str]:
    """Resolve ``(channel, thread_ts)`` for a CLI send. Explicit
    ``--thread`` / ``--channel`` always win; otherwise ``--task``
    threads to the task's recorded origin (thread_ts + channel when the
    sidecar has one)."""
    channel = getattr(args, "channel", "") or ""
    thread = getattr(args, "thread", "") or ""
    if not thread and getattr(args, "task", ""):
        origin_thread, origin_channel = resolve_task_origin(args.task)
        thread = origin_thread
        if not channel:
            channel = origin_channel
    return channel, thread


def _cmd_text(args: argparse.Namespace) -> int:
    n = SlackNotifier.try_load()
    if n is None:
        print("error: slack creds not configured", file=sys.stderr)
        return 2
    channel, thread = _routing_from_args(args)
    ok = n.dm_text(
        args.text, channel=channel or None, thread_ts=thread or None
    )
    return 0 if ok else 1


def _cmd_file(args: argparse.Namespace) -> int:
    n = SlackNotifier.try_load()
    if n is None:
        print("error: slack creds not configured", file=sys.stderr)
        return 2
    channel, thread = _routing_from_args(args)
    ok = n.dm_file(
        args.file, caption=args.comment or "",
        channel=channel or None, thread_ts=thread or None,
    )
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    from tigerharness._logging import configure_cli_logging
    configure_cli_logging(default="INFO")
    p = argparse.ArgumentParser(
        prog="tigerharness.slack_bridge.notify",
        description="DM a user or upload a file to a Slack thread.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    task_help = (
        "Journal task id: thread the message into the task's recorded "
        "origin thread (deferred_origin.json). Explicit --thread/"
        "--channel override it; a task without a recorded origin falls "
        "back to the default DM with a warning."
    )

    t = sub.add_parser("text", help="Send a text DM.")
    t.add_argument("text")
    t.add_argument("--channel", default="",
                   help="Post to this channel id instead of the operator DM.")
    t.add_argument("--thread", default="",
                   help="Reply in this thread_ts.")
    t.add_argument("--task", default="", help=task_help)
    t.set_defaults(func=_cmd_text)

    f = sub.add_parser("file", help="Upload a file.")
    f.add_argument("--file", required=True, help="Path to the file to upload.")
    f.add_argument("--comment", default="", help="Caption.")
    f.add_argument("--channel", default="",
                   help="Upload to this channel id instead of the operator DM.")
    f.add_argument("--thread", default="",
                   help="Share into this thread_ts.")
    f.add_argument("--task", default="", help=task_help)
    f.set_defaults(func=_cmd_file)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

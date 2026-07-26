"""Environment loading for the Slack bridge.

Centralising this here keeps `bridge.py` testable -- the entry point in
`__main__.py` is the only place that hits the filesystem.

All paths are resolved from environment variables -- no hardcoded
workspace paths.
"""

from __future__ import annotations

import logging

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger("tigerharness.slack_bridge.config")


# tiger-memory rebuild trigger modes for a new thread:
#   "rebuild" -- fire a plain `tiger-memory rebuild` (format gate + briefing)
#                (a detached `claude -p`, API-billed). The default, so
#                existing deployments are unchanged.
#   "off"     -- daemon fires nothing; the in-session sweep protocol
#                (docs/tiger-memory-sweep-protocol.md), run by the
#                persona's own interactive session, owns the rebuild.
VALID_TIGER_MEMORY_TRIGGERS = ("rebuild", "off")


def normalize_tiger_memory_trigger(
    value: str | bool | None, *, where: str = "TIGER_MEMORY_TRIGGER"
) -> str:
    """Validate a tiger-memory trigger mode. Empty/None -> ``"rebuild"``
    (legacy default). Raises ``ValueError`` on an unknown value so a typo
    is caught at config load, not silently ignored.

    YAML 1.1 parses a bare ``off`` (the natural way to write the mode in
    a fragment) as the boolean ``False`` -- and ``on``/``yes``/``true``
    as ``True``. ``"off"`` is a valid mode, so recover it from a ``False``
    bool rather than silently falling back to ``"rebuild"``; a ``True``
    bool maps to no valid mode and is reported as an error.
    """
    raw = value
    if isinstance(value, bool):
        value = "off" if value is False else "on"
    v = (value or "rebuild").strip().lower()
    if v not in VALID_TIGER_MEMORY_TRIGGERS:
        raise ValueError(
            f"{where}: unknown tiger_memory_trigger {raw!r}; allowed: "
            f"{', '.join(VALID_TIGER_MEMORY_TRIGGERS)}."
        )
    return v


@dataclass(frozen=True)
class BridgeConfig:
    slack_app_token: str
    slack_bot_token: str
    allowed_user_ids: frozenset[str]
    agent_cwd: str
    # Optional: path to the agent's system prompt file.
    agent_prompt_path: str = ""
    # Optional: path to tiger-memory.config.yaml. When set, the bridge
    # fires a plain `tiger-memory rebuild` on each new thread.
    tiger_memory_config_path: str = ""
    # Optional: path to tiger-memory CLI binary.
    tiger_memory_cli: str = ""
    # How a new thread triggers the memory rebuild (see above).
    tiger_memory_trigger: str = "rebuild"


def load() -> BridgeConfig:
    """Read .env (if present) and the process environment. Fail fast on
    missing required keys.
    """
    # Load .env from the current directory or explicit path.
    env_path = os.environ.get("TIGERHARNESS_SLACK_ENV", "").strip()
    if env_path:
        load_dotenv(Path(env_path).expanduser())
    else:
        # Try .env in CWD
        load_dotenv()

    required = {
        "SLACK_APP_TOKEN": os.environ.get("SLACK_APP_TOKEN", "").strip(),
        "SLACK_BOT_TOKEN": os.environ.get("SLACK_BOT_TOKEN", "").strip(),
        "ALLOWED_SLACK_USER_IDS": os.environ.get("ALLOWED_SLACK_USER_IDS", "").strip(),
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise SystemExit(
            "slack-bridge: missing required env vars: "
            + ", ".join(missing)
            + ".\nSet them in your environment or .env file."
        )

    user_ids = frozenset(
        s for s in (x.strip() for x in required["ALLOWED_SLACK_USER_IDS"].split(",")) if s
    )
    if not user_ids:
        raise SystemExit("slack-bridge: ALLOWED_SLACK_USER_IDS is empty after parsing")

    # Catch the easy-to-make swap between app-level and bot tokens early.
    wrong_prefix = []
    if not required["SLACK_APP_TOKEN"].startswith("xapp-"):
        wrong_prefix.append("SLACK_APP_TOKEN should start with 'xapp-'")
    if not required["SLACK_BOT_TOKEN"].startswith("xoxb-"):
        wrong_prefix.append("SLACK_BOT_TOKEN should start with 'xoxb-'")
    bad_ids = [u for u in user_ids if u[:1] not in {"U", "W"}]
    if bad_ids:
        wrong_prefix.append(
            f"ALLOWED_SLACK_USER_IDS contains entries that don't start with U or W: {sorted(bad_ids)}"
        )
    if wrong_prefix:
        raise SystemExit("slack-bridge: " + "; ".join(wrong_prefix))

    try:
        trigger = normalize_tiger_memory_trigger(
            os.environ.get("TIGER_MEMORY_TRIGGER", "")
        )
    except ValueError as exc:
        raise SystemExit(f"slack-bridge: {exc}")

    def _redact(tok: str) -> str:
        return f"{tok[:5]}...{tok[-4:]}" if len(tok) > 12 else "<short>"
    log.info("bridge config loaded (bot=%s app=%s)",
             _redact(required["SLACK_BOT_TOKEN"]),
             _redact(required["SLACK_APP_TOKEN"]))
    return BridgeConfig(
        slack_app_token=required["SLACK_APP_TOKEN"],
        slack_bot_token=required["SLACK_BOT_TOKEN"],
        allowed_user_ids=user_ids,
        agent_cwd=os.environ.get("TIGERHARNESS_AGENT_CWD", "."),
        agent_prompt_path=os.environ.get("TIGERHARNESS_AGENT_PROMPT", "").strip(),
        tiger_memory_config_path=os.environ.get(
            "TIGER_MEMORY_CONFIG", ""
        ).strip(),
        tiger_memory_cli=os.environ.get(
            "TIGER_MEMORY_CLI", ""
        ).strip(),
        tiger_memory_trigger=trigger,
    )

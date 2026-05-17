"""Environment loading for the Slack bridge.

Centralising this here keeps `bridge.py` testable -- the entry point in
`__main__.py` is the only place that hits the filesystem.

All paths are resolved from environment variables -- no hardcoded
workspace paths.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class BridgeConfig:
    slack_app_token: str
    slack_bot_token: str
    allowed_user_ids: frozenset[str]
    agent_cwd: str
    # Optional: path to the agent's system prompt file.
    agent_prompt_path: str = ""
    # Optional: path to tiger-memory.config.yaml. When set, the bridge
    # fires `tiger-memory rebuild --background` on each new thread.
    tiger_memory_config_path: str = ""
    # Optional: path to tiger-memory CLI binary.
    tiger_memory_cli: str = ""


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
    )

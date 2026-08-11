"""Shared config primitives for the Slack bridge.

``BridgeConfig`` is the frozen per-bridge settings bundle consumed by
``bridge.build_bridge`` (the single-persona factory); the multi-lane
loader in ``multi.py`` reads each lane's env *file* and builds
``TeamBridgeContext`` objects instead. The env-reading single-tenant
``load()`` that used to live here was removed with the single-tenant
entrypoint on 2026-08-11 (ADR 0009).
"""

from __future__ import annotations

from dataclasses import dataclass


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


def redact_token(tok: str) -> str:
    """Prefix/suffix-only rendering of a Slack token for log lines --
    never log a full secret (log family V)."""
    return f"{tok[:5]}...{tok[-4:]}" if len(tok) > 12 else "<short>"


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

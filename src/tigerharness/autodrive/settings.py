"""Autodrive configuration read from the team's ``configs/.env`` (ADR 0010).

Precedence for every knob is **flag > process env > team ``.env`` > built-in
default**. The team file is the Operator-owned surface: a team decides its own
cadence and budget without anyone editing code or a systemd unit.

The reader is deliberately dependency-free. ``python-dotenv`` lives behind the
``[slack]`` extra, and autodrive is core -- a core module must not acquire an
optional dependency. The accepted shape is the same ``KEY=value`` subset the
bridge's ``.env`` files already use (``#`` comments, blank lines, optional
surrounding quotes, optional ``export`` prefix).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Mapping

log = logging.getLogger(__name__)

#: Enable the auto-start hook: scheduling work starts the team's daemon.
#: Unset/false by default -- the harness ships to deployments whose billing
#: situation we do not know, so a team opts in explicitly (see ADR 0010).
AUTOSTART_ENV = "TIGERHARNESS_AUTODRIVE_AUTOSTART"
INTERVAL_ENV = "TIGERHARNESS_AUTODRIVE_INTERVAL"
MAX_BUDGET_ENV = "TIGERHARNESS_AUTODRIVE_MAX_BUDGET"
DRIVER_ENV = "TIGERHARNESS_AUTODRIVE_DRIVER"
NOTIFY_ENV = "TIGERHARNESS_AUTODRIVE_NOTIFY"
#: Kept at its historical name -- it predates ADR 0010 and is already
#: documented; renaming it would break live configs for no gain.
NOTIFY_CHANNEL_ENV = "TIGERHARNESS_AUTODRIVE_NOTIFY_CHANNEL"

#: Relative to the team root. Matches what ``tigerharness init`` scaffolds
#: and what the slack-bridge lane index defaults to.
TEAM_ENV_REL = ("configs", ".env")

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSEY = frozenset({"0", "false", "no", "off", ""})


def truthy(value: str | None) -> bool:
    """Interpret an env-style flag. Unset, empty, and the usual negatives are
    False; anything else recognisably positive is True. An unrecognised value
    is False *and* logs -- silently treating a typo as "on" is how an opt-in
    guard stops being opt-in."""
    if value is None:
        return False
    norm = value.strip().lower()
    if norm in _TRUTHY:
        return True
    if norm in _FALSEY:
        return False
    log.warning(
        "unrecognised boolean %r; treating as false (use 1/0)", value
    )
    return False


def read_env_file(path: Path) -> dict[str, str]:
    """Parse ``KEY=value`` lines from *path*. Missing or unreadable file →
    ``{}``: team config is an optimisation, never a hard requirement, so a
    permissions problem must not break scheduling."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        out[key] = value.strip().strip('"').strip("'")
    return out


def team_env_path(team_root: Path | None) -> Path | None:
    """Where a team's ``.env`` lives, or ``None`` for a non-team journal."""
    if team_root is None:
        return None
    return team_root.joinpath(*TEAM_ENV_REL)


class Settings:
    """Resolved autodrive settings for one team.

    Holds the two lookup layers (process env, then the team ``.env``) so a
    caller reads each knob once, in one precedence order, instead of every
    call site re-deriving it.
    """

    def __init__(
        self,
        *,
        team_root: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.env: Mapping[str, str] = os.environ if env is None else env
        path = team_env_path(team_root)
        self.file: dict[str, str] = read_env_file(path) if path else {}
        self.path = path

    def get(self, key: str) -> str | None:
        """Process env wins over the team file (same precedence the bridge's
        ``.env`` loader uses), so an operator can override a team default for
        one invocation without editing the file. Empty string reads as unset
        so a blanked-out key falls through instead of forcing ``""``."""
        for source in (self.env, self.file):
            value = source.get(key)
            if value is not None and value.strip():
                return value.strip()
        return None

    def flag(self, key: str) -> bool:
        return truthy(self.get(key))

    def number(self, key: str) -> float | None:
        """Numeric knob, or ``None`` when unset. A malformed value logs and
        reads as unset -- a typo'd budget must not become a crash inside the
        `journal new` that triggered auto-start."""
        raw = self.get(key)
        if raw is None:
            return None
        try:
            return float(raw)
        except ValueError:
            log.warning("ignoring non-numeric %s=%r", key, raw)
            return None

    @property
    def autostart(self) -> bool:
        return self.flag(AUTOSTART_ENV)

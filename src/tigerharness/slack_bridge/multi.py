"""Multi-team config schema + loader for the Slack bridge.

A *single bridge process* serves N teams concurrently. Each team is a
"lane" with its own Slack app (own tokens, own bot identity), its own
persona prompt, its own ``threads.json`` state file, and its own
``allowed_user_ids`` list.

The orchestrator that actually runs N ``AsyncSocketModeHandler``s in
one event loop lives in PR3. This module is *just* the config layer:
parse YAML, resolve paths, validate, return a typed object.

Config layout
-------------

Top-level **index** (e.g. ``~/projects/teams/slack-bridge.yaml``)::

    lanes:
      - shohoku
      - tigers

Per-team **fragment** (e.g. ``shohoku/configs/slack-bridge.yaml``)::

    persona: ayako
    allowed_user_ids:
      - U0123ABC
    state_dir: ~/.local/state/slack-bridge/shohoku
    # Optional overrides; defaults shown:
    # env: configs/.env
    # agent_cwd: .
    # agent_prompt: personas/<persona>/prompt.md
    # tiger_memory_config: memories/<persona>/tiger-memory.config.yaml

Tokens live in each team's ``.env`` (gitignored), referenced by the
fragment via ``env:``. Loader reads each .env via ``dotenv_values()``
*without* touching the process ``os.environ`` -- N lanes' tokens
coexist in memory without overwriting each other.

Backward compatibility
----------------------

The single-tenant entrypoint (``python -m tigerharness.slack_bridge``)
ignores this module entirely. Only the multi-orchestrator imports it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .config import BridgeConfig


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LaneConfig:
    """One lane = one team. Bundles the per-lane ``BridgeConfig`` plus
    the explicit ``state_path`` the orchestrator passes to
    ``build_bridge(cfg, state_path=...)``.
    """
    name: str
    bridge_cfg: BridgeConfig
    state_path: Path


@dataclass(frozen=True)
class MultiBridgeConfig:
    """The full multi-lane configuration for one bridge process."""
    lanes: tuple[LaneConfig, ...]


# ---------------------------------------------------------------------------
# YAML / dotenv helpers (lazy imports so the [slack] extra stays small)
# ---------------------------------------------------------------------------

def _load_yaml(path: Path) -> dict:
    """Parse a YAML file into a dict. Lazy-imports pyyaml with a clear
    error message so users who haven't installed it know exactly what
    extra to pull in."""
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as e:
        raise SystemExit(
            "slack-bridge multi-lane mode requires pyyaml. Install with:\n"
            "    pip install 'tigerharness[memory]'    # any extra with pyyaml\n"
            "    pip install pyyaml                    # or just pyyaml directly"
        ) from e
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"could not read {path}: {e}") from e
    data = yaml.safe_load(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level must be a mapping, got {type(data).__name__}")
    return data


def _load_env_file(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict WITHOUT polluting os.environ.
    Using ``dotenv_values()`` (from python-dotenv, already a slack extra
    dep) so we don't roll our own parser."""
    try:
        from dotenv import dotenv_values
    except ImportError as e:  # pragma: no cover - covered by [slack] extra
        raise SystemExit(
            "slack-bridge requires python-dotenv. Install with:\n"
            "    pip install 'tigerharness[slack]'"
        ) from e
    if not path.exists():
        raise ValueError(f"env file not found: {path}")
    raw = dotenv_values(str(path))
    # dotenv_values returns dict[str, str | None]; coerce None -> "".
    return {k: (v or "") for k, v in raw.items()}


# ---------------------------------------------------------------------------
# Validation + lane assembly
# ---------------------------------------------------------------------------

def _require(d: dict, key: str, where: str) -> object:
    if key not in d:
        raise ValueError(f"{where}: missing required field '{key}'")
    return d[key]


def _validate_allowed_user_ids(raw: object, where: str) -> frozenset[str]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{where}: 'allowed_user_ids' must be a non-empty list")
    ids: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{where}: 'allowed_user_ids' entries must be non-empty strings")
        ids.append(item.strip())
    bad = [u for u in ids if u[:1] not in {"U", "W"}]
    if bad:
        raise ValueError(
            f"{where}: allowed_user_ids must start with 'U' or 'W'; "
            f"offending entries: {sorted(bad)}"
        )
    return frozenset(ids)


def _validate_tokens(env: dict[str, str], where: str) -> tuple[str, str]:
    """Mirror the validation in ``config.load()`` but operate on a passed-in
    dict instead of ``os.environ``. Kept structurally identical so the two
    code paths can't drift."""
    app = (env.get("SLACK_APP_TOKEN") or "").strip()
    bot = (env.get("SLACK_BOT_TOKEN") or "").strip()
    missing = [k for k, v in (("SLACK_APP_TOKEN", app), ("SLACK_BOT_TOKEN", bot)) if not v]
    if missing:
        raise ValueError(
            f"{where}: missing required env vars: {', '.join(missing)}"
        )
    issues: list[str] = []
    if not app.startswith("xapp-"):
        issues.append("SLACK_APP_TOKEN should start with 'xapp-'")
    if not bot.startswith("xoxb-"):
        issues.append("SLACK_BOT_TOKEN should start with 'xoxb-'")
    if issues:
        raise ValueError(f"{where}: " + "; ".join(issues))
    return app, bot


def _resolve_team_dir(index_dir: Path, lane_name: str) -> Path:
    team_dir = (index_dir / lane_name).resolve()
    if not team_dir.is_dir():
        raise ValueError(
            f"lane '{lane_name}': team directory not found at {team_dir}"
        )
    return team_dir


def _resolve(maybe_rel: str | os.PathLike[str], base: Path) -> Path:
    """Resolve a path that may be relative to *base* or absolute.
    ``~`` expansion is honored."""
    p = Path(maybe_rel).expanduser()
    return p if p.is_absolute() else (base / p)


def _build_lane(index_dir: Path, lane_name: str) -> LaneConfig:
    team_dir = _resolve_team_dir(index_dir, lane_name)
    fragment_path = team_dir / "configs" / "slack-bridge.yaml"
    if not fragment_path.exists():
        raise ValueError(
            f"lane '{lane_name}': fragment not found at {fragment_path}. "
            f"Run `tigerharness init --team {lane_name}` to generate it."
        )
    where = f"lane '{lane_name}' ({fragment_path})"
    spec = _load_yaml(fragment_path)

    persona = str(_require(spec, "persona", where)).strip()
    if not persona:
        raise ValueError(f"{where}: 'persona' cannot be empty")

    allowed_user_ids = _validate_allowed_user_ids(
        _require(spec, "allowed_user_ids", where), where
    )

    state_dir_raw = str(_require(spec, "state_dir", where)).strip()
    if not state_dir_raw:
        raise ValueError(f"{where}: 'state_dir' cannot be empty")
    state_path = (_resolve(state_dir_raw, team_dir) / "threads.json").resolve()

    # Optional overrides; defaults follow the team-folder convention.
    env_rel = str(spec.get("env") or "configs/.env")
    agent_cwd = str(spec.get("agent_cwd") or ".")
    agent_prompt_rel = str(
        spec.get("agent_prompt") or f"personas/{persona}/prompt.md"
    )
    tiger_memory_cfg_rel = str(
        spec.get("tiger_memory_config")
        or f"memories/{persona}/tiger-memory.config.yaml"
    )

    env_path = _resolve(env_rel, team_dir)
    env_vars = _load_env_file(env_path)
    app_token, bot_token = _validate_tokens(env_vars, where)

    bridge_cfg = BridgeConfig(
        slack_app_token=app_token,
        slack_bot_token=bot_token,
        allowed_user_ids=allowed_user_ids,
        agent_cwd=str(_resolve(agent_cwd, team_dir)),
        agent_prompt_path=str(_resolve(agent_prompt_rel, team_dir)),
        tiger_memory_config_path=str(_resolve(tiger_memory_cfg_rel, team_dir)),
        tiger_memory_cli=env_vars.get("TIGER_MEMORY_CLI", ""),
    )
    return LaneConfig(name=lane_name, bridge_cfg=bridge_cfg, state_path=state_path)


def _check_lane_uniqueness(lanes: tuple[LaneConfig, ...]) -> None:
    """Two lanes sharing a state_path corrupt each other's threads.json.
    Two lanes sharing a Slack app token would try to open two Socket Mode
    connections to the same app, which Slack rejects. Both must fail
    loudly at load time."""
    seen_state: dict[Path, str] = {}
    seen_app_token: dict[str, str] = {}
    for lane in lanes:
        if lane.state_path in seen_state:
            raise ValueError(
                f"lane '{lane.name}' and lane '{seen_state[lane.state_path]}' "
                f"share state_path {lane.state_path}; pick distinct state_dirs"
            )
        seen_state[lane.state_path] = lane.name
        tok = lane.bridge_cfg.slack_app_token
        if tok in seen_app_token:
            raise ValueError(
                f"lane '{lane.name}' and lane '{seen_app_token[tok]}' "
                f"share SLACK_APP_TOKEN; each lane must use a distinct Slack app"
            )
        seen_app_token[tok] = lane.name


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def load_multi(index_path: Path) -> MultiBridgeConfig:
    """Parse the top-level index + each per-team fragment, returning a
    fully-resolved ``MultiBridgeConfig``.

    Raises ``ValueError`` on any validation failure -- the orchestrator
    treats this as a startup-only error and refuses to launch.
    """
    index_path = index_path.expanduser().resolve()
    if not index_path.exists():
        raise ValueError(f"slack-bridge index not found: {index_path}")
    index = _load_yaml(index_path)
    raw_lanes = index.get("lanes")
    if not isinstance(raw_lanes, list) or not raw_lanes:
        raise ValueError(
            f"{index_path}: 'lanes' must be a non-empty list of team names"
        )
    names: list[str] = []
    for item in raw_lanes:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                f"{index_path}: each 'lanes' entry must be a non-empty string"
            )
        names.append(item.strip())
    # Catch duplicate lane names in the index early -- otherwise the
    # downstream uniqueness checks would also fire, but with a less
    # obvious message.
    dupes = [n for n in set(names) if names.count(n) > 1]
    if dupes:
        raise ValueError(
            f"{index_path}: duplicate lane names: {sorted(dupes)}"
        )

    index_dir = index_path.parent
    lanes = tuple(_build_lane(index_dir, name) for name in names)
    _check_lane_uniqueness(lanes)
    return MultiBridgeConfig(lanes=lanes)

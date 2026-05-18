"""Tests for the multi-lane slack-bridge config loader."""
from __future__ import annotations

from pathlib import Path

import pytest

from tigerharness.slack_bridge.multi import (
    LaneConfig,
    MultiBridgeConfig,
    _check_lane_uniqueness,
    _load_yaml,
    _resolve,
    load_multi,
)


# ---------------------------------------------------------------------------
# Layout builders
# ---------------------------------------------------------------------------

def _write_env(team_dir: Path, *, app="xapp-shohoku-1", bot="xoxb-shohoku-1") -> None:
    """Write a valid .env into <team>/configs/.env."""
    configs = team_dir / "configs"
    configs.mkdir(parents=True, exist_ok=True)
    (configs / ".env").write_text(
        f"SLACK_APP_TOKEN={app}\nSLACK_BOT_TOKEN={bot}\n"
    )


def _write_fragment(team_dir: Path, body: str) -> None:
    configs = team_dir / "configs"
    configs.mkdir(parents=True, exist_ok=True)
    (configs / "slack-bridge.yaml").write_text(body)


def _write_personas_layout(team_dir: Path, persona: str) -> None:
    (team_dir / "personas" / persona).mkdir(parents=True, exist_ok=True)
    (team_dir / "personas" / persona / "prompt.md").write_text(
        f"You are {persona}."
    )
    (team_dir / "memories" / persona).mkdir(parents=True, exist_ok=True)
    (team_dir / "memories" / persona / "tiger-memory.config.yaml").write_text(
        "agent: {name: test}\n"
    )


def _make_valid_team(
    root: Path, name: str, persona: str = "ayako",
    state_subdir: str | None = None,
    app: str | None = None, bot: str | None = None,
) -> Path:
    """Lay down a complete, valid team directory under *root*."""
    team_dir = root / name
    team_dir.mkdir(parents=True, exist_ok=True)
    _write_env(
        team_dir,
        app=app or f"xapp-{name}-1",
        bot=bot or f"xoxb-{name}-1",
    )
    _write_personas_layout(team_dir, persona)
    state = state_subdir or f"state/{name}"
    _write_fragment(team_dir, f"""\
persona: {persona}
allowed_user_ids:
  - U0CEO
state_dir: {root / state}
""")
    return team_dir


def _write_index(root: Path, lanes: list[str]) -> Path:
    body = "lanes:\n" + "".join(f"  - {l}\n" for l in lanes)
    p = root / "slack-bridge.yaml"
    p.write_text(body)
    return p


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestLoadMultiHappyPath:
    def test_single_lane(self, tmp_path: Path):
        _make_valid_team(tmp_path, "shohoku")
        idx = _write_index(tmp_path, ["shohoku"])
        cfg = load_multi(idx)
        assert isinstance(cfg, MultiBridgeConfig)
        assert len(cfg.lanes) == 1
        lane = cfg.lanes[0]
        assert lane.name == "shohoku"
        assert lane.bridge_cfg.slack_app_token == "xapp-shohoku-1"
        assert lane.bridge_cfg.slack_bot_token == "xoxb-shohoku-1"
        assert lane.bridge_cfg.allowed_user_ids == frozenset({"U0CEO"})
        # Default paths derived from team-folder convention.
        assert lane.bridge_cfg.agent_cwd == str(tmp_path / "shohoku")
        assert lane.bridge_cfg.agent_prompt_path == str(
            tmp_path / "shohoku" / "personas" / "ayako" / "prompt.md"
        )
        assert lane.bridge_cfg.tiger_memory_config_path == str(
            tmp_path / "shohoku" / "memories" / "ayako"
            / "tiger-memory.config.yaml"
        )
        assert lane.state_path == (tmp_path / "state" / "shohoku" / "threads.json").resolve()

    def test_two_lanes_distinct_tokens_and_state(self, tmp_path: Path):
        _make_valid_team(tmp_path, "shohoku", app="xapp-1", bot="xoxb-1")
        _make_valid_team(tmp_path, "tigers", persona="chief",
                         app="xapp-2", bot="xoxb-2")
        idx = _write_index(tmp_path, ["shohoku", "tigers"])
        cfg = load_multi(idx)
        assert [l.name for l in cfg.lanes] == ["shohoku", "tigers"]
        # Distinct app_tokens carried through correctly.
        assert {l.bridge_cfg.slack_app_token for l in cfg.lanes} == {"xapp-1", "xapp-2"}

    def test_optional_overrides_applied(self, tmp_path: Path):
        """env / agent_cwd / agent_prompt / tiger_memory_config can all
        be overridden in the fragment."""
        team_dir = tmp_path / "shohoku"
        team_dir.mkdir()
        # Non-default .env location
        custom_env = team_dir / "secrets.env"
        custom_env.write_text(
            "SLACK_APP_TOKEN=xapp-x\nSLACK_BOT_TOKEN=xoxb-x\n"
        )
        # Non-default prompt + memory paths
        (team_dir / "custom_prompt.md").write_text("custom persona")
        (team_dir / "custom_mem.yaml").write_text("agent: {name: x}\n")
        _write_fragment(team_dir, f"""\
persona: ayako
allowed_user_ids:
  - U0CEO
state_dir: {tmp_path}/state/sh
env: secrets.env
agent_cwd: .
agent_prompt: custom_prompt.md
tiger_memory_config: custom_mem.yaml
""")
        idx = _write_index(tmp_path, ["shohoku"])
        cfg = load_multi(idx)
        lane = cfg.lanes[0]
        assert lane.bridge_cfg.agent_prompt_path == str(team_dir / "custom_prompt.md")
        assert lane.bridge_cfg.tiger_memory_config_path == str(team_dir / "custom_mem.yaml")

    def test_tiger_memory_cli_propagated_from_env(self, tmp_path: Path):
        """If the lane's .env sets TIGER_MEMORY_CLI, the lane's BridgeConfig
        carries it through (so per-lane rebuild triggers use the right
        binary)."""
        team_dir = tmp_path / "shohoku"
        team_dir.mkdir()
        configs = team_dir / "configs"
        configs.mkdir()
        (configs / ".env").write_text(
            "SLACK_APP_TOKEN=xapp-1\nSLACK_BOT_TOKEN=xoxb-1\n"
            "TIGER_MEMORY_CLI=/opt/tm/bin/tiger-memory\n"
        )
        _write_personas_layout(team_dir, "ayako")
        _write_fragment(team_dir, f"""\
persona: ayako
allowed_user_ids: [U0CEO]
state_dir: {tmp_path}/state/sh
""")
        idx = _write_index(tmp_path, ["shohoku"])
        cfg = load_multi(idx)
        assert cfg.lanes[0].bridge_cfg.tiger_memory_cli == "/opt/tm/bin/tiger-memory"


# ---------------------------------------------------------------------------
# Index validation
# ---------------------------------------------------------------------------

class TestLoadMultiIndexValidation:
    def test_missing_index_file(self, tmp_path: Path):
        with pytest.raises(ValueError, match="index not found"):
            load_multi(tmp_path / "slack-bridge.yaml")

    def test_index_top_level_not_mapping(self, tmp_path: Path):
        idx = tmp_path / "slack-bridge.yaml"
        idx.write_text("- not\n- a\n- mapping\n")
        with pytest.raises(ValueError, match="top-level must be a mapping"):
            load_multi(idx)

    def test_index_empty_file(self, tmp_path: Path):
        idx = tmp_path / "slack-bridge.yaml"
        idx.write_text("")
        # _load_yaml returns {} for empty -> lanes missing.
        with pytest.raises(ValueError, match="'lanes' must be a non-empty list"):
            load_multi(idx)

    def test_index_lanes_is_not_a_list(self, tmp_path: Path):
        idx = tmp_path / "slack-bridge.yaml"
        idx.write_text("lanes: shohoku\n")
        with pytest.raises(ValueError, match="'lanes' must be a non-empty list"):
            load_multi(idx)

    def test_index_lanes_is_empty_list(self, tmp_path: Path):
        idx = tmp_path / "slack-bridge.yaml"
        idx.write_text("lanes: []\n")
        with pytest.raises(ValueError, match="'lanes' must be a non-empty list"):
            load_multi(idx)

    def test_index_lane_entry_not_string(self, tmp_path: Path):
        idx = tmp_path / "slack-bridge.yaml"
        idx.write_text("lanes:\n  - 42\n")
        with pytest.raises(ValueError, match="each 'lanes' entry must be a non-empty string"):
            load_multi(idx)

    def test_index_lane_entry_empty_string(self, tmp_path: Path):
        idx = tmp_path / "slack-bridge.yaml"
        idx.write_text("lanes:\n  - ''\n")
        with pytest.raises(ValueError, match="each 'lanes' entry must be a non-empty string"):
            load_multi(idx)

    def test_index_duplicate_lane_names(self, tmp_path: Path):
        _make_valid_team(tmp_path, "shohoku")
        idx = _write_index(tmp_path, ["shohoku", "shohoku"])
        with pytest.raises(ValueError, match="duplicate lane names"):
            load_multi(idx)


# ---------------------------------------------------------------------------
# Fragment validation
# ---------------------------------------------------------------------------

class TestLoadMultiFragmentValidation:
    def test_team_dir_missing(self, tmp_path: Path):
        # Index references a team that doesn't exist on disk.
        idx = _write_index(tmp_path, ["ghosts"])
        with pytest.raises(ValueError, match="team directory not found"):
            load_multi(idx)

    def test_fragment_missing(self, tmp_path: Path):
        # Team dir exists but the fragment file is missing.
        (tmp_path / "shohoku").mkdir()
        idx = _write_index(tmp_path, ["shohoku"])
        with pytest.raises(ValueError, match="fragment not found"):
            load_multi(idx)

    def test_fragment_missing_persona(self, tmp_path: Path):
        team_dir = _make_valid_team(tmp_path, "shohoku")
        _write_fragment(team_dir, "allowed_user_ids: [U0CEO]\nstate_dir: /tmp/s\n")
        idx = _write_index(tmp_path, ["shohoku"])
        with pytest.raises(ValueError, match="missing required field 'persona'"):
            load_multi(idx)

    def test_fragment_empty_persona(self, tmp_path: Path):
        team_dir = _make_valid_team(tmp_path, "shohoku")
        _write_fragment(team_dir, "persona: ''\nallowed_user_ids: [U0CEO]\nstate_dir: /tmp/s\n")
        idx = _write_index(tmp_path, ["shohoku"])
        with pytest.raises(ValueError, match="'persona' cannot be empty"):
            load_multi(idx)

    def test_fragment_missing_state_dir(self, tmp_path: Path):
        team_dir = _make_valid_team(tmp_path, "shohoku")
        _write_fragment(team_dir, "persona: ayako\nallowed_user_ids: [U0CEO]\n")
        idx = _write_index(tmp_path, ["shohoku"])
        with pytest.raises(ValueError, match="missing required field 'state_dir'"):
            load_multi(idx)

    def test_fragment_empty_state_dir(self, tmp_path: Path):
        team_dir = _make_valid_team(tmp_path, "shohoku")
        _write_fragment(team_dir, "persona: ayako\nallowed_user_ids: [U0CEO]\nstate_dir: ''\n")
        idx = _write_index(tmp_path, ["shohoku"])
        with pytest.raises(ValueError, match="'state_dir' cannot be empty"):
            load_multi(idx)


# ---------------------------------------------------------------------------
# allowed_user_ids validation
# ---------------------------------------------------------------------------

class TestLoadMultiAllowedUserIds:
    def _setup(self, tmp_path: Path, body: str) -> Path:
        team_dir = _make_valid_team(tmp_path, "shohoku")
        _write_fragment(team_dir, f"persona: ayako\nstate_dir: /tmp/s\n{body}")
        return _write_index(tmp_path, ["shohoku"])

    def test_missing(self, tmp_path: Path):
        team_dir = _make_valid_team(tmp_path, "shohoku")
        _write_fragment(team_dir, "persona: ayako\nstate_dir: /tmp/s\n")
        idx = _write_index(tmp_path, ["shohoku"])
        with pytest.raises(ValueError, match="missing required field 'allowed_user_ids'"):
            load_multi(idx)

    def test_not_a_list(self, tmp_path: Path):
        idx = self._setup(tmp_path, "allowed_user_ids: U0CEO\n")
        with pytest.raises(ValueError, match="must be a non-empty list"):
            load_multi(idx)

    def test_empty_list(self, tmp_path: Path):
        idx = self._setup(tmp_path, "allowed_user_ids: []\n")
        with pytest.raises(ValueError, match="must be a non-empty list"):
            load_multi(idx)

    def test_entry_not_string(self, tmp_path: Path):
        idx = self._setup(tmp_path, "allowed_user_ids: [42]\n")
        with pytest.raises(ValueError, match="entries must be non-empty strings"):
            load_multi(idx)

    def test_entry_empty_string(self, tmp_path: Path):
        idx = self._setup(tmp_path, "allowed_user_ids: ['']\n")
        with pytest.raises(ValueError, match="entries must be non-empty strings"):
            load_multi(idx)

    def test_bad_prefix(self, tmp_path: Path):
        idx = self._setup(tmp_path, "allowed_user_ids: [foo, U0CEO]\n")
        with pytest.raises(ValueError, match="must start with 'U' or 'W'"):
            load_multi(idx)


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------

class TestLoadMultiTokens:
    def test_missing_env_file(self, tmp_path: Path):
        team_dir = tmp_path / "shohoku"
        team_dir.mkdir()
        # Don't write the .env -- but DO write the fragment and personas.
        _write_personas_layout(team_dir, "ayako")
        _write_fragment(team_dir, f"""\
persona: ayako
allowed_user_ids: [U0CEO]
state_dir: {tmp_path}/state/s
""")
        idx = _write_index(tmp_path, ["shohoku"])
        with pytest.raises(ValueError, match="env file not found"):
            load_multi(idx)

    def test_missing_tokens(self, tmp_path: Path):
        team_dir = tmp_path / "shohoku"
        team_dir.mkdir()
        (team_dir / "configs").mkdir()
        (team_dir / "configs" / ".env").write_text("# no tokens here\n")
        _write_personas_layout(team_dir, "ayako")
        _write_fragment(team_dir, f"""\
persona: ayako
allowed_user_ids: [U0CEO]
state_dir: {tmp_path}/state/s
""")
        idx = _write_index(tmp_path, ["shohoku"])
        with pytest.raises(ValueError, match="missing required env vars"):
            load_multi(idx)

    def test_wrong_app_token_prefix(self, tmp_path: Path):
        team_dir = _make_valid_team(tmp_path, "shohoku", app="not-xapp", bot="xoxb-x")
        idx = _write_index(tmp_path, ["shohoku"])
        with pytest.raises(ValueError, match="SLACK_APP_TOKEN should start with 'xapp-'"):
            load_multi(idx)

    def test_wrong_bot_token_prefix(self, tmp_path: Path):
        team_dir = _make_valid_team(tmp_path, "shohoku", app="xapp-x", bot="not-xoxb")
        idx = _write_index(tmp_path, ["shohoku"])
        with pytest.raises(ValueError, match="SLACK_BOT_TOKEN should start with 'xoxb-'"):
            load_multi(idx)


# ---------------------------------------------------------------------------
# Uniqueness checks across lanes
# ---------------------------------------------------------------------------

class TestLoadMultiUniqueness:
    def test_shared_state_path_rejected(self, tmp_path: Path):
        # Force both lanes onto the same state_dir.
        _make_valid_team(tmp_path, "shohoku", state_subdir="state/shared",
                         app="xapp-1", bot="xoxb-1")
        _make_valid_team(tmp_path, "tigers", persona="chief",
                         state_subdir="state/shared",
                         app="xapp-2", bot="xoxb-2")
        idx = _write_index(tmp_path, ["shohoku", "tigers"])
        with pytest.raises(ValueError, match="share state_path"):
            load_multi(idx)

    def test_shared_app_token_rejected(self, tmp_path: Path):
        # Same SLACK_APP_TOKEN across two teams.
        _make_valid_team(tmp_path, "shohoku", app="xapp-clash", bot="xoxb-1")
        _make_valid_team(tmp_path, "tigers", persona="chief",
                         app="xapp-clash", bot="xoxb-2")
        idx = _write_index(tmp_path, ["shohoku", "tigers"])
        with pytest.raises(ValueError, match="share SLACK_APP_TOKEN"):
            load_multi(idx)


# ---------------------------------------------------------------------------
# Unit-level: helpers exercised in isolation
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_resolve_absolute_path(self, tmp_path: Path):
        result = _resolve("/etc/passwd", tmp_path)
        assert result == Path("/etc/passwd")

    def test_resolve_tilde_expansion(self, tmp_path: Path):
        result = _resolve("~/some/place", tmp_path)
        assert str(result).startswith(str(Path.home()))

    def test_load_yaml_oserror_wrapped(self, tmp_path: Path):
        # A directory passed where a file is expected -> read_text raises.
        with pytest.raises(ValueError, match="could not read"):
            _load_yaml(tmp_path)

    def test_load_yaml_returns_empty_dict_on_empty_file(self, tmp_path: Path):
        p = tmp_path / "empty.yaml"
        p.write_text("")
        assert _load_yaml(p) == {}

    def test_load_yaml_pyyaml_missing_message(self, monkeypatch, tmp_path: Path):
        """If pyyaml isn't installed, the error must tell the user exactly
        what to install. We simulate the missing-import by blocking
        `yaml` in sys.modules so the lazy `import yaml` raises."""
        import sys
        monkeypatch.setitem(sys.modules, "yaml", None)  # forces ImportError
        p = tmp_path / "x.yaml"
        p.write_text("a: 1\n")
        with pytest.raises(SystemExit, match="requires pyyaml"):
            _load_yaml(p)

    def test_check_lane_uniqueness_accepts_distinct(self, tmp_path: Path):
        from tigerharness.slack_bridge.config import BridgeConfig
        l1 = LaneConfig(
            name="a",
            bridge_cfg=BridgeConfig(
                slack_app_token="xapp-1", slack_bot_token="xoxb-1",
                allowed_user_ids=frozenset({"U0"}), agent_cwd="/a",
            ),
            state_path=tmp_path / "a.json",
        )
        l2 = LaneConfig(
            name="b",
            bridge_cfg=BridgeConfig(
                slack_app_token="xapp-2", slack_bot_token="xoxb-2",
                allowed_user_ids=frozenset({"U0"}), agent_cwd="/b",
            ),
            state_path=tmp_path / "b.json",
        )
        _check_lane_uniqueness((l1, l2))  # no raise

"""Tests for the multi-lane slack-bridge config loader."""
from __future__ import annotations

from pathlib import Path

import pytest

from tigerharness.slack_bridge.multi import (
    LaneConfig,
    MultiBridgeConfig,
    _check_lane_uniqueness,
    _coerce_flag,
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


def _write_personas_layout(team_dir: Path, *personas: str) -> None:
    """Write `personas/<name>/prompt.md` and `memories/<name>/...` for each
    persona, plus a minimal `configs/personas.yaml` listing them all
    (the multi loader reads this to derive the routable roster)."""
    if not personas:
        raise ValueError("at least one persona required")
    for persona in personas:
        (team_dir / "personas" / persona).mkdir(parents=True, exist_ok=True)
        (team_dir / "personas" / persona / "prompt.md").write_text(
            f"You are {persona}."
        )
        (team_dir / "memories" / persona).mkdir(parents=True, exist_ok=True)
        (team_dir / "memories" / persona / "tiger-memory.config.yaml").write_text(
            "agent: {name: test}\n"
        )
    # personas.yaml: the routable roster the multi loader reads.
    configs = team_dir / "configs"
    configs.mkdir(parents=True, exist_ok=True)
    body = "personas:\n" + "".join(f"  - name: {p}\n" for p in personas)
    (configs / "personas.yaml").write_text(body)


def _make_valid_team(
    root: Path, name: str, persona: str = "ayako",
    state_subdir: str | None = None,
    app: str | None = None, bot: str | None = None,
    extra_personas: tuple[str, ...] = (),
) -> Path:
    """Lay down a complete, valid team directory under *root*.

    *persona* is the default_persona for the lane and is included in
    the team's personas.yaml roster. *extra_personas* extends the
    roster for multi-persona tests.
    """
    team_dir = root / name
    team_dir.mkdir(parents=True, exist_ok=True)
    _write_env(
        team_dir,
        app=app or f"xapp-{name}-1",
        bot=bot or f"xoxb-{name}-1",
    )
    _write_personas_layout(team_dir, persona, *extra_personas)
    state = state_subdir or f"state/{name}"
    _write_fragment(team_dir, f"""\
default_persona: {persona}
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
        assert lane.team_ctx.slack_app_token == "xapp-shohoku-1"
        assert lane.team_ctx.slack_bot_token == "xoxb-shohoku-1"
        assert lane.team_ctx.allowed_user_ids == frozenset({"U0CEO"})
        # Default paths derived from team-folder convention.
        assert lane.team_ctx.agent_cwd == str(tmp_path / "shohoku")
        # The roster picked up from personas.yaml is {ayako}; default is ayako.
        assert set(lane.team_ctx.personas.keys()) == {"ayako"}
        assert lane.team_ctx.default_persona == "ayako"
        ayako_slot = lane.team_ctx.personas["ayako"]
        # Memory config exists -- _write_personas_layout creates it.
        assert ayako_slot.tiger_memory_config_path == str(
            tmp_path / "shohoku" / "memories" / "ayako"
            / "tiger-memory.config.yaml"
        )
        # Persona's prompt body should include the team-awareness preamble
        # only if there are other personas; with just one, the preamble is
        # omitted -- so the agent_config carries the bare prompt text.
        assert "You are ayako" in ayako_slot.agent_config.instructions
        assert lane.state_path == (tmp_path / "state" / "shohoku" / "threads.json").resolve()

    def test_two_lanes_distinct_tokens_and_state(self, tmp_path: Path):
        _make_valid_team(tmp_path, "shohoku", app="xapp-1", bot="xoxb-1")
        _make_valid_team(tmp_path, "tigers", persona="chief",
                         app="xapp-2", bot="xoxb-2")
        idx = _write_index(tmp_path, ["shohoku", "tigers"])
        cfg = load_multi(idx)
        assert [l.name for l in cfg.lanes] == ["shohoku", "tigers"]
        # Distinct app_tokens carried through correctly.
        assert {l.team_ctx.slack_app_token for l in cfg.lanes} == {"xapp-1", "xapp-2"}

    def test_multi_persona_roster(self, tmp_path: Path):
        """Roster auto-discovered from team's personas.yaml. Adding a
        2nd persona makes them auto-routable in the same lane."""
        _make_valid_team(
            tmp_path, "shohoku", persona="ayako",
            extra_personas=("sakuragi",),
        )
        idx = _write_index(tmp_path, ["shohoku"])
        cfg = load_multi(idx)
        lane = cfg.lanes[0]
        assert set(lane.team_ctx.personas.keys()) == {"ayako", "sakuragi"}
        assert lane.team_ctx.default_persona == "ayako"
        assert lane.team_ctx.is_multi_persona
        # The team-awareness preamble is appended only in multi-persona
        # mode so each persona knows how to handle misroutes.
        for slot in lane.team_ctx.personas.values():
            assert "Other team members reachable" in slot.agent_config.instructions

    def test_default_persona_must_be_in_roster(self, tmp_path: Path):
        """If `default_persona` names someone not in personas.yaml, fail."""
        team_dir = tmp_path / "shohoku"
        team_dir.mkdir()
        _write_env(team_dir)
        _write_personas_layout(team_dir, "ayako")
        _write_fragment(team_dir, f"""\
default_persona: ghost
allowed_user_ids: [U0CEO]
state_dir: {tmp_path}/state/sh
""")
        idx = _write_index(tmp_path, ["shohoku"])
        with pytest.raises(ValueError, match="default_persona 'ghost' is not in"):
            load_multi(idx)

    def test_legacy_persona_field_is_accepted_as_alias(self, tmp_path: Path):
        """PR2-era fragments used `persona:`; the new schema is
        `default_persona:`. Old fragments must keep working."""
        team_dir = tmp_path / "shohoku"
        team_dir.mkdir()
        _write_env(team_dir)
        _write_personas_layout(team_dir, "ayako")
        _write_fragment(team_dir, f"""\
persona: ayako
allowed_user_ids: [U0CEO]
state_dir: {tmp_path}/state/sh
""")
        idx = _write_index(tmp_path, ["shohoku"])
        cfg = load_multi(idx)
        assert cfg.lanes[0].team_ctx.default_persona == "ayako"

    def test_optional_overrides_applied(self, tmp_path: Path):
        """env / agent_cwd can be overridden in the fragment. (The
        per-persona prompt + memory paths come from the team's
        roster -- no longer overridable in the fragment.)"""
        team_dir = tmp_path / "shohoku"
        team_dir.mkdir()
        # Non-default .env location
        custom_env = team_dir / "secrets.env"
        custom_env.write_text(
            "SLACK_APP_TOKEN=xapp-x\nSLACK_BOT_TOKEN=xoxb-x\n"
        )
        _write_personas_layout(team_dir, "ayako")
        _write_fragment(team_dir, f"""\
default_persona: ayako
allowed_user_ids:
  - U0CEO
state_dir: {tmp_path}/state/sh
env: secrets.env
agent_cwd: .
""")
        idx = _write_index(tmp_path, ["shohoku"])
        cfg = load_multi(idx)
        lane = cfg.lanes[0]
        # Custom .env was read (otherwise tokens validation would fail).
        assert lane.team_ctx.slack_app_token == "xapp-x"
        # agent_cwd resolved relative to team dir (.) -> team_dir
        assert lane.team_ctx.agent_cwd == str(team_dir)

    def test_tiger_memory_cli_propagated_from_env(self, tmp_path: Path):
        """If the lane's .env sets TIGER_MEMORY_CLI, the team context
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
default_persona: ayako
allowed_user_ids: [U0CEO]
state_dir: {tmp_path}/state/sh
""")
        idx = _write_index(tmp_path, ["shohoku"])
        cfg = load_multi(idx)
        assert cfg.lanes[0].team_ctx.tiger_memory_cli == "/opt/tm/bin/tiger-memory"


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

    def test_fragment_missing_default_persona(self, tmp_path: Path):
        team_dir = _make_valid_team(tmp_path, "shohoku")
        _write_fragment(team_dir, "allowed_user_ids: [U0CEO]\nstate_dir: /tmp/s\n")
        idx = _write_index(tmp_path, ["shohoku"])
        with pytest.raises(
            ValueError,
            match="missing required field 'default_persona'",
        ):
            load_multi(idx)

    def test_fragment_empty_default_persona(self, tmp_path: Path):
        team_dir = _make_valid_team(tmp_path, "shohoku")
        _write_fragment(team_dir, "default_persona: ''\nallowed_user_ids: [U0CEO]\nstate_dir: /tmp/s\n")
        idx = _write_index(tmp_path, ["shohoku"])
        # Empty -> validation reports as missing (since the alias logic
        # treats "" as falsy).
        with pytest.raises(ValueError, match="missing required field 'default_persona'"):
            load_multi(idx)

    def test_fragment_missing_state_dir(self, tmp_path: Path):
        team_dir = _make_valid_team(tmp_path, "shohoku")
        _write_fragment(team_dir, "default_persona: ayako\nallowed_user_ids: [U0CEO]\n")
        idx = _write_index(tmp_path, ["shohoku"])
        with pytest.raises(ValueError, match="missing required field 'state_dir'"):
            load_multi(idx)

    def test_fragment_empty_state_dir(self, tmp_path: Path):
        team_dir = _make_valid_team(tmp_path, "shohoku")
        _write_fragment(team_dir, "default_persona: ayako\nallowed_user_ids: [U0CEO]\nstate_dir: ''\n")
        idx = _write_index(tmp_path, ["shohoku"])
        with pytest.raises(ValueError, match="'state_dir' cannot be empty"):
            load_multi(idx)


# ---------------------------------------------------------------------------
# tiger_memory_trigger override
# ---------------------------------------------------------------------------

class TestLoadMultiTigerMemoryTrigger:
    def _fragment(self, root: Path, state: str, trigger_line: str = "") -> str:
        return (
            "default_persona: ayako\n"
            "allowed_user_ids:\n  - U0CEO\n"
            f"state_dir: {root / state}\n"
            f"{trigger_line}"
        )

    def test_default_is_rebuild(self, tmp_path: Path):
        # _make_valid_team writes a fragment with no trigger line.
        _make_valid_team(tmp_path, "shohoku")
        idx = _write_index(tmp_path, ["shohoku"])
        cfg = load_multi(idx)
        assert cfg.lanes[0].team_ctx.tiger_memory_trigger == "rebuild"

    def test_explicit_off(self, tmp_path: Path):
        team_dir = _make_valid_team(tmp_path, "shohoku")
        _write_fragment(
            team_dir,
            self._fragment(tmp_path, "state/shohoku", "tiger_memory_trigger: off\n"),
        )
        idx = _write_index(tmp_path, ["shohoku"])
        cfg = load_multi(idx)
        assert cfg.lanes[0].team_ctx.tiger_memory_trigger == "off"

    def test_bad_value_raises_with_lane_context(self, tmp_path: Path):
        team_dir = _make_valid_team(tmp_path, "shohoku")
        _write_fragment(
            team_dir,
            self._fragment(tmp_path, "state/shohoku", "tiger_memory_trigger: bogus\n"),
        )
        idx = _write_index(tmp_path, ["shohoku"])
        with pytest.raises(ValueError, match="lane 'shohoku'.*unknown tiger_memory_trigger"):
            load_multi(idx)


# ---------------------------------------------------------------------------
# idle_compact (ADR 0004) per-lane wiring
# ---------------------------------------------------------------------------

class TestLoadMultiIdleCompact:
    """Per-lane idle compaction comes from the fragment's ``idle_compact``
    flag; the journal root auto-resolves to ``<team>/journal``."""

    def _fragment(self, root: Path, idle_line: str = "") -> str:
        return (
            "default_persona: ayako\n"
            "allowed_user_ids:\n  - U0CEO\n"
            f"state_dir: {root / 'state/shohoku'}\n"
            f"{idle_line}"
        )

    def test_absent_flag_disables(self, tmp_path: Path):
        # _make_valid_team's fragment has no idle_compact line.
        _make_valid_team(tmp_path, "shohoku")
        idx = _write_index(tmp_path, ["shohoku"])
        cfg = load_multi(idx)
        ic = cfg.lanes[0].team_ctx.idle_compact
        assert ic is not None
        assert ic.enabled is False

    def test_enabled_with_journal_arms(self, tmp_path: Path):
        team_dir = _make_valid_team(tmp_path, "shohoku")
        # The journal must exist with an active/ dir for the feature to arm.
        (team_dir / "journal" / "active").mkdir(parents=True)
        _write_fragment(team_dir, self._fragment(tmp_path, "idle_compact: true\n"))
        idx = _write_index(tmp_path, ["shohoku"])
        cfg = load_multi(idx)
        ic = cfg.lanes[0].team_ctx.idle_compact
        assert ic.enabled is True
        assert ic.journal_root == team_dir / "journal"

    def test_enabled_but_no_journal_disables_fail_soft(self, tmp_path: Path):
        # Flag on, but the team has no journal/active dir -> disabled,
        # and the whole multi-load still succeeds (never aborts a lane).
        team_dir = _make_valid_team(tmp_path, "shohoku")
        _write_fragment(team_dir, self._fragment(tmp_path, "idle_compact: true\n"))
        idx = _write_index(tmp_path, ["shohoku"])
        cfg = load_multi(idx)
        assert cfg.lanes[0].team_ctx.idle_compact.enabled is False

    def test_quoted_flag_forms_accepted(self, tmp_path: Path):
        team_dir = _make_valid_team(tmp_path, "shohoku")
        (team_dir / "journal" / "active").mkdir(parents=True)
        _write_fragment(
            team_dir, self._fragment(tmp_path, 'idle_compact: "on"\n'))
        idx = _write_index(tmp_path, ["shohoku"])
        cfg = load_multi(idx)
        assert cfg.lanes[0].team_ctx.idle_compact.enabled is True

    def test_coerce_flag_truth_table(self):
        for truthy in (True, "1", "true", "TRUE", "yes", "on", " On "):
            assert _coerce_flag(truthy) is True
        for falsy in (False, "0", "false", "off", "", "nope", None, 1, []):
            assert _coerce_flag(falsy) is False


# ---------------------------------------------------------------------------
# allowed_user_ids validation
# ---------------------------------------------------------------------------

class TestLoadMultiAllowedUserIds:
    def _setup(self, tmp_path: Path, body: str) -> Path:
        team_dir = _make_valid_team(tmp_path, "shohoku")
        _write_fragment(team_dir, f"default_persona: ayako\nstate_dir: /tmp/s\n{body}")
        return _write_index(tmp_path, ["shohoku"])

    def test_missing(self, tmp_path: Path):
        team_dir = _make_valid_team(tmp_path, "shohoku")
        _write_fragment(team_dir, "default_persona: ayako\nstate_dir: /tmp/s\n")
        idx = _write_index(tmp_path, ["shohoku"])
        # No YAML list and no SLACK_ALLOWED_USER_IDS in the env file:
        # the loader names both places it looked.
        with pytest.raises(ValueError, match="missing 'allowed_user_ids'"):
            load_multi(idx)

    def test_not_a_list(self, tmp_path: Path):
        idx = self._setup(tmp_path, "allowed_user_ids: U0CEO\n")
        with pytest.raises(ValueError, match="must be a non-empty list"):
            load_multi(idx)

    def test_empty_list(self, tmp_path: Path):
        # An empty YAML list now falls through to the env file; with no
        # SLACK_ALLOWED_USER_IDS there either, the combined error fires.
        idx = self._setup(tmp_path, "allowed_user_ids: []\n")
        with pytest.raises(ValueError, match="missing 'allowed_user_ids'"):
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
        from tigerharness.slack_bridge.bridge import (
            PersonaSlot, TeamBridgeContext,
        )
        from tigerharness.agent_sdk import AgentConfig
        def _ctx(app: str, bot: str, cwd: str) -> TeamBridgeContext:
            slot = PersonaSlot(
                name="x",
                agent_config=AgentConfig(name="x", instructions="x"),
            )
            return TeamBridgeContext(
                team_name="t",
                slack_app_token=app, slack_bot_token=bot,
                allowed_user_ids=frozenset({"U0"}), agent_cwd=cwd,
                personas={"x": slot}, default_persona="x",
            )
        l1 = LaneConfig(
            name="a",
            team_ctx=_ctx("xapp-1", "xoxb-1", "/a"),
            state_path=tmp_path / "a.json",
        )
        l2 = LaneConfig(
            name="b",
            team_ctx=_ctx("xapp-2", "xoxb-2", "/b"),
            state_path=tmp_path / "b.json",
        )
        _check_lane_uniqueness((l1, l2))  # no raise


# ---------------------------------------------------------------------------
# Allowlist env fallback (SLACK_ALLOWED_USER_IDS in the lane env file)
# ---------------------------------------------------------------------------

class TestAllowlistEnvFallback:
    def _team_without_yaml_allowlist(
        self, root: Path, env_allowlist: str | None
    ) -> Path:
        team_dir = _make_valid_team(root, "shohoku")
        _write_fragment(team_dir, f"""\
default_persona: ayako
state_dir: {root / 'state/shohoku'}
""")
        env = team_dir / "configs" / ".env"
        body = "SLACK_APP_TOKEN=xapp-1\nSLACK_BOT_TOKEN=xoxb-1\n"
        if env_allowlist is not None:
            body += f"SLACK_ALLOWED_USER_IDS={env_allowlist}\n"
        env.write_text(body)
        return team_dir

    def test_env_fallback_parses_commas_and_whitespace(self, tmp_path: Path):
        self._team_without_yaml_allowlist(tmp_path, "U0CEO, W0AAA  U0BBB")
        idx = _write_index(tmp_path, ["shohoku"])
        cfg = load_multi(idx)
        assert cfg.lanes[0].team_ctx.allowed_user_ids == frozenset(
            {"U0CEO", "W0AAA", "U0BBB"}
        )

    def test_missing_everywhere_raises(self, tmp_path: Path):
        self._team_without_yaml_allowlist(tmp_path, None)
        idx = _write_index(tmp_path, ["shohoku"])
        with pytest.raises(ValueError, match="missing 'allowed_user_ids'"):
            load_multi(idx)

    def test_empty_env_value_raises(self, tmp_path: Path):
        self._team_without_yaml_allowlist(tmp_path, "  ")
        idx = _write_index(tmp_path, ["shohoku"])
        with pytest.raises(ValueError, match="missing 'allowed_user_ids'"):
            load_multi(idx)

    def test_empty_yaml_list_falls_back_to_env(self, tmp_path: Path):
        team_dir = self._team_without_yaml_allowlist(tmp_path, "U0CEO")
        _write_fragment(team_dir, f"""\
default_persona: ayako
allowed_user_ids: []
state_dir: {tmp_path / 'state/shohoku'}
""")
        idx = _write_index(tmp_path, ["shohoku"])
        cfg = load_multi(idx)
        assert cfg.lanes[0].team_ctx.allowed_user_ids == frozenset({"U0CEO"})

    def test_yaml_wins_over_env(self, tmp_path: Path):
        team_dir = self._team_without_yaml_allowlist(tmp_path, "U0ENV")
        _write_fragment(team_dir, f"""\
default_persona: ayako
allowed_user_ids: [U0YAML]
state_dir: {tmp_path / 'state/shohoku'}
""")
        idx = _write_index(tmp_path, ["shohoku"])
        cfg = load_multi(idx)
        assert cfg.lanes[0].team_ctx.allowed_user_ids == frozenset({"U0YAML"})

    def test_env_fallback_still_validates_prefixes(self, tmp_path: Path):
        self._team_without_yaml_allowlist(tmp_path, "BADID")
        idx = _write_index(tmp_path, ["shohoku"])
        with pytest.raises(ValueError, match="must start with 'U' or 'W'"):
            load_multi(idx)

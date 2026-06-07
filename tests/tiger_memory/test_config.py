"""Tests for tiger_memory.config."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tigerharness.tiger_memory.config import (
    MIN_DAILIES_WORKING_DAYS,
    MIN_WEEKLIES_WORKING_DAYS,
    ConfigError,
    _deep_merge,
    load_config,
)


def test_loads_minimal(minimal_config_yaml: Path) -> None:
    cfg = load_config(minimal_config_yaml)
    assert cfg.agent.name == "TestTiger"
    # Per-agent subfolder auto-appended: <root>/<slug>.
    assert cfg.store.root.name == "testtiger"
    assert cfg.store.root.parent.name == "memory"
    assert cfg.store.root.is_absolute()
    # Default budgets exposed for callers
    assert cfg.budgets.max_prompt_content_chars == 120_000
    assert len(cfg.sources) == 1 and cfg.sources[0].kind == "claude_code"
    # Defaults take effect.
    assert cfg.briefing.walking.dailies_working_days == 7
    assert cfg.briefing.walking.weeklies_working_days == 28
    assert cfg.budgets.short_summary_words == 400


def test_rejects_dailies_below_min(invalid_dailies_config_yaml: Path) -> None:
    with pytest.raises(ConfigError, match="dailies_working_days"):
        load_config(invalid_dailies_config_yaml)


def test_rejects_weeklies_below_min(tmp_path: Path) -> None:
    cfg = tmp_path / "bad.yaml"
    cfg.write_text(
        f"""\
agent: {{name: t, role: t}}
store: {{root: {tmp_path}/mem}}
sources:
  - kind: claude_code
    project_path: {tmp_path}/p/
summarizer: {{backend: anthropic, model: m, prompts: default/v1}}
briefing:
  walking:
    weeklies_working_days: 14
"""
    )
    with pytest.raises(ConfigError, match="weeklies_working_days"):
        load_config(cfg)


def test_uses_env_var_when_no_path(
    minimal_config_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TIGER_MEMORY_CONFIG", str(minimal_config_yaml))
    cfg = load_config()
    assert cfg.source_path == minimal_config_yaml


def test_errors_when_no_path_and_no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TIGER_MEMORY_CONFIG", raising=False)
    with pytest.raises(ConfigError, match="TIGER_MEMORY_CONFIG"):
        load_config()


def test_errors_on_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_errors_on_bad_yaml(tmp_path: Path) -> None:
    cfg = tmp_path / "broken.yaml"
    cfg.write_text("agent: [unclosed")
    with pytest.raises(ConfigError):
        load_config(cfg)


def test_errors_on_unknown_source_kind(tmp_path: Path) -> None:
    cfg = tmp_path / "bad.yaml"
    cfg.write_text(
        f"""\
agent: {{name: t, role: t}}
store: {{root: {tmp_path}/mem}}
sources:
  - kind: gopher
summarizer: {{backend: anthropic, model: m, prompts: default/v1}}
"""
    )
    with pytest.raises(ConfigError, match="Unknown source kind"):
        load_config(cfg)


def test_min_constants_match_design_doc() -> None:
    """Sanity: the minimums in code match what the design doc states."""
    assert MIN_DAILIES_WORKING_DAYS == 7
    assert MIN_WEEKLIES_WORKING_DAYS == 28


def test_agent_slug_appended_to_store_root(tmp_path: Path) -> None:
    """Two agents sharing a repo should land in different store subdirs."""
    cfg_text = lambda name: f"""\
agent:
  name: {name}
  role: "test"
store:
  root: {tmp_path}/memory
sources:
  - kind: claude_code
    project_path: {tmp_path}/p/
summarizer:
  backend: anthropic
  model: claude-opus-4-7
  prompts: default/v1
"""
    cfg_sai = tmp_path / "sai.yaml"
    cfg_sai.write_text(cfg_text("Sai"))
    cfg_scout = tmp_path / "scout.yaml"
    cfg_scout.write_text(cfg_text("Scout Tiger"))
    s = load_config(cfg_sai)
    c = load_config(cfg_scout)
    assert s.store.root.name == "sai"
    assert c.store.root.name == "scout_tiger"
    assert s.store.root != c.store.root


def test_agent_slug_not_double_appended(tmp_path: Path) -> None:
    """If the user already wrote `store.root: ./memory/sai`, don't append."""
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(f"""\
agent: {{name: Sai, role: t}}
store: {{root: {tmp_path}/memory/sai}}
sources:
  - kind: claude_code
    project_path: {tmp_path}/p/
summarizer: {{backend: anthropic, model: m, prompts: default/v1}}
""")
    loaded = load_config(cfg)
    assert loaded.store.root.name == "sai"
    assert loaded.store.root.parent.name == "memory"
    # NOT memory/sai/sai
    assert loaded.store.root.parent.parent != loaded.store.root.parent


# ----- team-level defaults ---------------------------------------------------


class TestDeepMerge:
    def test_overlay_wins(self):
        assert _deep_merge({"a": 1}, {"a": 2}) == {"a": 2}

    def test_base_fills_gaps(self):
        assert _deep_merge({"a": 1, "b": 2}, {"a": 9}) == {"a": 9, "b": 2}

    def test_nested_merge(self):
        base = {"summarizer": {"backend": "anthropic", "model": "sonnet"}}
        overlay = {"summarizer": {"model": "opus"}}
        result = _deep_merge(base, overlay)
        assert result == {"summarizer": {"backend": "anthropic", "model": "opus"}}

    def test_list_replaced_not_merged(self):
        base = {"sources": [{"kind": "claude_code"}]}
        overlay = {"sources": [{"kind": "docs"}]}
        assert _deep_merge(base, overlay) == {"sources": [{"kind": "docs"}]}


def _write_team_layout(
    tmp_path: Path,
    *,
    defaults_content: str | None = None,
    persona_content: str,
    persona: str = "scout",
) -> Path:
    """Set up a realistic team layout and return the persona config path."""
    configs_dir = tmp_path / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    if defaults_content is not None:
        (configs_dir / "tiger-memory.defaults.yaml").write_text(defaults_content)
    mem_dir = tmp_path / "memories" / persona
    mem_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = mem_dir / "tiger-memory.config.yaml"
    cfg_path.write_text(persona_content)
    return cfg_path


class TestTeamDefaults:
    def test_auto_discovers_defaults(self, tmp_path: Path):
        """Persona inherits summarizer from team defaults."""
        cfg_path = _write_team_layout(
            tmp_path,
            defaults_content=(
                "summarizer:\n"
                "  backend: anthropic\n"
                "  model: claude-sonnet-4-6\n"
                "  prompts: default/v1\n"
            ),
            persona_content=(
                f"agent:\n  name: Scout\n  role: test\n"
                f"store:\n  root: .\n"
                f"sources:\n  - kind: claude_code\n"
                f"    project_path: {tmp_path}/p/\n"
            ),
        )
        cfg = load_config(cfg_path)
        assert cfg.summarizer.model == "claude-sonnet-4-6"
        assert cfg.summarizer.backend == "anthropic"

    def test_persona_overrides_defaults(self, tmp_path: Path):
        """Per-persona model wins over team default."""
        cfg_path = _write_team_layout(
            tmp_path,
            defaults_content=(
                "summarizer:\n"
                "  backend: anthropic\n"
                "  model: claude-sonnet-4-6\n"
                "  prompts: default/v1\n"
            ),
            persona_content=(
                f"agent:\n  name: Scout\n  role: test\n"
                f"store:\n  root: .\n"
                f"sources:\n  - kind: claude_code\n"
                f"    project_path: {tmp_path}/p/\n"
                f"summarizer:\n  model: claude-opus-4-7\n"
            ),
        )
        cfg = load_config(cfg_path)
        # model overridden, backend inherited
        assert cfg.summarizer.model == "claude-opus-4-7"
        assert cfg.summarizer.backend == "anthropic"

    def test_explicit_defaults_path(self, tmp_path: Path):
        """The `defaults:` key points to an explicit file."""
        custom_defaults = tmp_path / "custom-defaults.yaml"
        custom_defaults.write_text(
            "summarizer:\n"
            "  backend: anthropic\n"
            "  model: claude-opus-4-7\n"
            "  prompts: default/v1\n"
        )
        cfg_path = _write_team_layout(
            tmp_path,
            persona_content=(
                f"defaults: {custom_defaults}\n"
                f"agent:\n  name: Scout\n  role: test\n"
                f"store:\n  root: .\n"
                f"sources:\n  - kind: claude_code\n"
                f"    project_path: {tmp_path}/p/\n"
            ),
        )
        cfg = load_config(cfg_path)
        assert cfg.summarizer.model == "claude-opus-4-7"

    def test_explicit_defaults_relative_path(self, tmp_path: Path):
        """A relative `defaults:` path resolves against persona config dir."""
        # Put defaults next to the persona config
        cfg_path = _write_team_layout(
            tmp_path,
            persona_content=(
                f"defaults: ../../configs/tiger-memory.defaults.yaml\n"
                f"agent:\n  name: Scout\n  role: test\n"
                f"store:\n  root: .\n"
                f"sources:\n  - kind: claude_code\n"
                f"    project_path: {tmp_path}/p/\n"
            ),
            defaults_content=(
                "summarizer:\n"
                "  backend: anthropic\n"
                "  model: claude-opus-4-7\n"
                "  prompts: default/v1\n"
            ),
        )
        cfg = load_config(cfg_path)
        assert cfg.summarizer.model == "claude-opus-4-7"

    def test_explicit_defaults_bad_yaml_raises(self, tmp_path: Path):
        """Bad YAML in an explicitly referenced defaults file raises."""
        bad_defaults = tmp_path / "bad-defaults.yaml"
        bad_defaults.write_text("summarizer: [unclosed")
        cfg_path = _write_team_layout(
            tmp_path,
            persona_content=(
                f"defaults: {bad_defaults}\n"
                f"agent:\n  name: Scout\n  role: test\n"
                f"store:\n  root: .\n"
                f"sources:\n  - kind: claude_code\n"
                f"    project_path: {tmp_path}/p/\n"
                f"summarizer:\n  backend: anthropic\n  model: m\n  prompts: default/v1\n"
            ),
        )
        with pytest.raises(ConfigError, match="defaults YAML"):
            load_config(cfg_path)

    def test_explicit_defaults_missing_raises(self, tmp_path: Path):
        cfg_path = _write_team_layout(
            tmp_path,
            persona_content=(
                f"defaults: /nonexistent/defaults.yaml\n"
                f"agent:\n  name: Scout\n  role: test\n"
                f"store:\n  root: .\n"
                f"sources:\n  - kind: claude_code\n"
                f"    project_path: {tmp_path}/p/\n"
                f"summarizer:\n  backend: anthropic\n  model: m\n  prompts: default/v1\n"
            ),
        )
        with pytest.raises(ConfigError, match="defaults file not found"):
            load_config(cfg_path)

    def test_auto_discovers_from_one_level_up(self, tmp_path: Path):
        """Config at memories/tiger-memory.config.yaml (one level, not two)
        still finds configs/tiger-memory.defaults.yaml via the fallback."""
        configs_dir = tmp_path / "configs"
        configs_dir.mkdir()
        (configs_dir / "tiger-memory.defaults.yaml").write_text(
            "summarizer:\n"
            "  backend: anthropic\n"
            "  model: claude-sonnet-4-6\n"
            "  prompts: default/v1\n"
        )
        # Config directly in memories/ (not memories/<persona>/)
        mem_dir = tmp_path / "memories"
        mem_dir.mkdir()
        cfg_path = mem_dir / "tiger-memory.config.yaml"
        cfg_path.write_text(
            f"agent:\n  name: Solo\n  role: test\n"
            f"store:\n  root: .\n"
            f"sources:\n  - kind: claude_code\n"
            f"    project_path: {tmp_path}/p/\n"
        )
        cfg = load_config(cfg_path)
        assert cfg.summarizer.model == "claude-sonnet-4-6"

    def test_no_defaults_file_works(self, tmp_path: Path):
        """Without any defaults file, persona config is self-contained."""
        cfg_path = _write_team_layout(
            tmp_path,
            defaults_content=None,  # no defaults file
            persona_content=(
                f"agent:\n  name: Scout\n  role: test\n"
                f"store:\n  root: .\n"
                f"sources:\n  - kind: claude_code\n"
                f"    project_path: {tmp_path}/p/\n"
                f"summarizer:\n  backend: anthropic\n  model: m\n  prompts: default/v1\n"
            ),
        )
        cfg = load_config(cfg_path)
        assert cfg.summarizer.model == "m"

    def test_defaults_bad_yaml_raises(self, tmp_path: Path):
        cfg_path = _write_team_layout(
            tmp_path,
            defaults_content="summarizer: [unclosed",
            persona_content=(
                f"agent:\n  name: Scout\n  role: test\n"
                f"store:\n  root: .\n"
                f"sources:\n  - kind: claude_code\n"
                f"    project_path: {tmp_path}/p/\n"
                f"summarizer:\n  backend: anthropic\n  model: m\n  prompts: default/v1\n"
            ),
        )
        with pytest.raises(ConfigError, match="defaults YAML"):
            load_config(cfg_path)


def test_prefilter_defaults_on(minimal_config_yaml: Path) -> None:
    """No prefilter block -> conservative-on defaults."""
    cfg = load_config(minimal_config_yaml)
    assert cfg.prefilter.enabled is True
    assert cfg.prefilter.drop_tool_results is True
    assert cfg.prefilter.drop_system_reminders is True


def test_prefilter_explicit_overrides(tmp_path: Path) -> None:
    """An explicit prefilter block overrides each knob independently."""
    cfg_path = tmp_path / "pf.yaml"
    cfg_path.write_text(
        f"agent:\n  name: T\n  role: t\n"
        f"store:\n  root: {tmp_path}/memory\n"
        f"sources:\n  - kind: claude_code\n    project_path: {tmp_path}/p/\n"
        f"summarizer:\n  backend: anthropic\n  model: m\n  prompts: default/v1\n"
        f"prefilter:\n"
        f"  enabled: false\n"
        f"  drop_tool_results: false\n"
        f"  drop_system_reminders: true\n"
    )
    cfg = load_config(cfg_path)
    assert cfg.prefilter.enabled is False
    assert cfg.prefilter.drop_tool_results is False
    assert cfg.prefilter.drop_system_reminders is True


def test_cap_defaults(minimal_config_yaml: Path) -> None:
    """No cap block -> sane backstop defaults."""
    cfg = load_config(minimal_config_yaml)
    assert cfg.cap.max_sessions_per_rebuild == 10
    assert cfg.cap.max_usd_per_rebuild == 20.0


def test_cap_explicit_overrides(tmp_path: Path) -> None:
    cfg_path = tmp_path / "cap.yaml"
    cfg_path.write_text(
        f"agent:\n  name: T\n  role: t\n"
        f"store:\n  root: {tmp_path}/memory\n"
        f"sources:\n  - kind: claude_code\n    project_path: {tmp_path}/p/\n"
        f"summarizer:\n  backend: anthropic\n  model: m\n  prompts: default/v1\n"
        f"cap:\n"
        f"  max_sessions_per_rebuild: 3\n"
        f"  max_usd_per_rebuild: 1.5\n"
    )
    cfg = load_config(cfg_path)
    assert cfg.cap.max_sessions_per_rebuild == 3
    assert cfg.cap.max_usd_per_rebuild == 1.5

"""Tests for tiger_memory.config."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tigerharness.tiger_memory.config import (
    MIN_DAILIES_WORKING_DAYS,
    MIN_WEEKLIES_WORKING_DAYS,
    ConfigError,
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

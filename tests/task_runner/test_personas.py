"""Persona registry tests: registration, resolution, config building."""

from __future__ import annotations

from pathlib import Path

import pytest

from tigerharness.task_runner.personas import (
    Persona,
    build_persona_config,
    clear_registry,
    list_personas,
    load_prompt,
    register_persona,
    resolve,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Ensure each test starts with an empty registry."""
    clear_registry()
    yield
    clear_registry()


def test_register_and_resolve():
    register_persona(
        "helper",
        aliases=("helper", "h"),
        cwd="/tmp",
        prompt="You are helpful.",
        description="A helpful persona.",
    )
    p = resolve("helper")
    assert p.name == "helper"
    assert p.cwd == Path("/tmp")
    assert p.description == "A helpful persona."


def test_resolve_by_alias():
    register_persona("helper", aliases=("helper", "h"), prompt="test")
    assert resolve("h").name == "helper"


def test_resolve_case_insensitive():
    register_persona("Helper", aliases=("Helper",), prompt="test")
    assert resolve("helper").name == "Helper"


def test_resolve_separator_normalization():
    register_persona("my-agent", aliases=("my-agent", "my_agent"), prompt="test")
    assert resolve("my_agent").name == "my-agent"
    assert resolve("my-agent").name == "my-agent"


def test_resolve_unknown_raises():
    with pytest.raises(KeyError, match="unknown persona"):
        resolve("nonexistent")


def test_list_personas_empty():
    assert list_personas() == []


def test_list_personas_after_register():
    register_persona("a", prompt="p1")
    register_persona("b", prompt="p2")
    names = [p.name for p in list_personas()]
    assert "a" in names
    assert "b" in names


def test_build_config_with_inline_prompt():
    cfg = build_persona_config("tester", prompt="You are a tester.")
    assert cfg.name == "tester-task"
    assert cfg.instructions == "You are a tester."


def test_build_config_with_prompt_file(tmp_path: Path):
    (tmp_path / "researcher.md").write_text("Research things.")
    cfg = build_persona_config(
        "researcher",
        prompt_file="researcher",
        personas_dir=tmp_path,
    )
    assert cfg.instructions == "Research things."


def test_load_prompt_missing_dir():
    with pytest.raises(FileNotFoundError, match="No personas directory"):
        load_prompt("test", personas_dir=None)


def test_load_prompt_missing_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="Persona prompt missing"):
        load_prompt("nonexistent", personas_dir=tmp_path)


def test_load_prompt_from_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TIGERHARNESS_PERSONAS_DIR", str(tmp_path))
    (tmp_path / "bot.md").write_text("I am bot.")
    text = load_prompt("bot")
    assert text == "I am bot."


def test_build_config_disallowed_tools():
    cfg = build_persona_config(
        "locked",
        prompt="test",
        disallowed_tools=["Bash(rm:*)", "Read(*.secret)"],
    )
    assert "Bash(rm:*)" in cfg.extra["disallowed_tools"]
    assert "Read(*.secret)" in cfg.extra["disallowed_tools"]


def test_disallowed_tools_extends_sudo_default():
    """Caller's list adds to the sudo hard-floor, doesn't replace it.

    Changed in 0.1.2; in 0.1.x callers had to remember to repeat the
    sudo entries themselves or accidentally re-enable sudo.
    """
    cfg = build_persona_config(
        "scout",
        prompt="...",
        disallowed_tools=["Read(*_holdout*)"],
    )
    denied = cfg.extra["disallowed_tools"]
    assert "Bash(sudo:*)" in denied
    assert "Bash(sudo)" in denied
    assert "Read(*_holdout*)" in denied


def test_disallowed_tools_default_still_blocks_sudo():
    """No `disallowed_tools` arg -> sudo block still applies."""
    cfg = build_persona_config("plain", prompt="...")
    denied = cfg.extra["disallowed_tools"]
    assert "Bash(sudo:*)" in denied
    assert "Bash(sudo)" in denied


def test_disallowed_tools_dedupes_explicit_sudo():
    """A caller who passes the sudo entries explicitly doesn't double-list them."""
    cfg = build_persona_config(
        "explicit",
        prompt="...",
        disallowed_tools=["Bash(sudo:*)", "Bash(sudo)", "Read(*_holdout*)"],
    )
    denied = cfg.extra["disallowed_tools"]
    # Each appears exactly once, sudo entries first.
    assert denied.count("Bash(sudo:*)") == 1
    assert denied.count("Bash(sudo)") == 1
    assert denied[:2] == ["Bash(sudo:*)", "Bash(sudo)"]
    assert "Read(*_holdout*)" in denied


def test_build_config_with_extra():
    """Line 119: extra dict merged into cfg_extra."""
    cfg = build_persona_config(
        "custom",
        prompt="test",
        extra={"custom_key": "custom_value", "max_tokens": 8192},
    )
    assert cfg.extra["custom_key"] == "custom_value"
    assert cfg.extra["max_tokens"] == 8192
    assert "permission_mode" in cfg.extra  # default still present


def test_register_persona_builds_config(tmp_path: Path):
    (tmp_path / "worker.md").write_text("Work hard.")
    p = register_persona(
        "worker",
        prompt_file="worker",
        personas_dir=tmp_path,
        cwd="/tmp",
    )
    cfg = p.build_config()
    assert cfg.instructions == "Work hard."
    assert cfg.name == "worker-task"

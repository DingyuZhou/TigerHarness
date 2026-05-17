"""Tests for the TIGERHARNESS_PERSONAS_CONFIG YAML loader."""
from __future__ import annotations

import importlib
import logging
import textwrap
from pathlib import Path

import pytest

from tigerharness.task_runner import personas as personas_mod
from tigerharness.task_runner.personas import (
    clear_registry,
    list_personas,
    load_personas_config,
    resolve,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _write_config(tmp_path: Path, body: str, *, personas_dir: str = "personas") -> Path:
    """Write a personas.yaml + matching personas/*.md files for testing."""
    cfg = tmp_path / "personas.yaml"
    cfg.write_text(body)
    pdir = tmp_path / personas_dir
    pdir.mkdir(exist_ok=True)
    for name in ("alice", "bob", "carol"):
        (pdir / f"{name}.md").write_text(f"# {name}\nYou are {name}.")
    return cfg


def test_load_basic_yaml(tmp_path):
    cfg = _write_config(tmp_path, textwrap.dedent("""\
        personas_dir: ./personas
        personas:
          - name: alice
            cwd: .
            description: First persona.
          - name: bob
            aliases: [bob, robert]
            cwd: ./project
            description: Second persona.
    """))
    (tmp_path / "project").mkdir()

    loaded = load_personas_config(cfg)

    assert [p.name for p in loaded] == ["alice", "bob"]
    assert {p.name for p in list_personas()} == {"alice", "bob"}
    # aliases work
    assert resolve("robert").name == "bob"


def test_relative_paths_resolve_against_config_dir(tmp_path):
    """personas_dir, cwd, and add_dirs are all anchored to the config file's dir."""
    cfg = _write_config(tmp_path, textwrap.dedent("""\
        personas_dir: ./personas
        personas:
          - name: alice
            cwd: ./workdir
            extra:
              add_dirs: [./extra]
    """))
    (tmp_path / "workdir").mkdir()
    (tmp_path / "extra").mkdir()

    load_personas_config(cfg)
    p = resolve("alice")

    assert p.cwd == (tmp_path / "workdir").resolve()
    built = p.build_config()
    # add_dirs is a known path-list field, resolved relative to the
    # config file's dir so configs are portable.
    assert str((tmp_path / "extra").resolve()) in built.extra["add_dirs"]


def test_inline_prompt_overrides_prompt_file(tmp_path):
    cfg = _write_config(tmp_path, textwrap.dedent("""\
        personas:
          - name: alice
            cwd: .
            prompt: "You are Alice the inline one."
    """))
    load_personas_config(cfg)
    built = resolve("alice").build_config()
    assert built.instructions == "You are Alice the inline one."


def test_prompt_file_lookup_uses_top_level_personas_dir(tmp_path):
    cfg = _write_config(tmp_path, textwrap.dedent("""\
        personas_dir: ./personas
        personas:
          - name: alice
            cwd: .
    """))
    load_personas_config(cfg)
    built = resolve("alice").build_config()
    assert "You are alice." in built.instructions


def test_disallowed_tools_from_yaml_extends_sudo_default(tmp_path):
    """A YAML disallowed_tools list extends the sudo block (0.1.2+).

    The author can write just the project-specific denies; sudo is
    always added by build_persona_config.
    """
    cfg = _write_config(tmp_path, textwrap.dedent("""\
        personas_dir: ./personas
        personas:
          - name: alice
            cwd: .
            disallowed_tools:
              - "Read(*_holdout*)"
    """))
    load_personas_config(cfg)
    denied = resolve("alice").build_config().extra["disallowed_tools"]
    assert "Bash(sudo:*)" in denied         # auto-added
    assert "Bash(sudo)" in denied            # auto-added
    assert "Read(*_holdout*)" in denied      # author's entry


def test_unknown_keys_rejected(tmp_path):
    cfg = _write_config(tmp_path, textwrap.dedent("""\
        personas_dir: ./personas
        personas:
          - name: alice
            cwd: .
            cwdd: typo!
    """))
    with pytest.raises(ValueError, match="unknown keys"):
        load_personas_config(cfg)


def test_missing_name_rejected(tmp_path):
    cfg = _write_config(tmp_path, textwrap.dedent("""\
        personas_dir: ./personas
        personas:
          - cwd: .
    """))
    with pytest.raises(ValueError, match="missing required 'name'"):
        load_personas_config(cfg)


def test_personas_must_be_list(tmp_path):
    cfg = _write_config(tmp_path, textwrap.dedent("""\
        personas_dir: ./personas
        personas:
          alice: nope
    """))
    with pytest.raises(ValueError, match="'personas' must be a list"):
        load_personas_config(cfg)


def test_top_level_must_be_mapping(tmp_path):
    cfg = tmp_path / "personas.yaml"
    cfg.write_text("- just a list\n- of things\n")
    with pytest.raises(ValueError, match="top-level must be a mapping"):
        load_personas_config(cfg)


def test_missing_config_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_personas_config(tmp_path / "does-not-exist.yaml")


def test_persona_spec_must_be_mapping(tmp_path):
    cfg = _write_config(tmp_path, textwrap.dedent("""\
        personas:
          - "just a string"
    """))
    with pytest.raises(ValueError, match="must be a mapping"):
        load_personas_config(cfg)


def test_aliases_become_tuple(tmp_path):
    cfg = _write_config(tmp_path, textwrap.dedent("""\
        personas:
          - name: alice
            cwd: .
            aliases: [alice, ally, al]
            prompt: "..."
    """))
    load_personas_config(cfg)
    p = resolve("alice")
    assert p.aliases == ("alice", "ally", "al")
    assert resolve("al").name == "alice"


def test_explicit_prompt_file_different_from_name(tmp_path):
    """prompt_file lets a persona's display name differ from its prompt file."""
    cfg = _write_config(tmp_path, textwrap.dedent("""\
        personas_dir: ./personas
        personas:
          - name: helper
            cwd: .
            prompt_file: alice          # use alice.md for the prompt
    """))
    load_personas_config(cfg)
    built = resolve("helper").build_config()
    assert "You are alice." in built.instructions


def test_per_persona_personas_dir_overrides_top_level(tmp_path):
    """A persona can point at a different personas_dir than the top-level default."""
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "alice.md").write_text("# elsewhere/alice")

    cfg = _write_config(tmp_path, textwrap.dedent(f"""\
        personas_dir: ./personas         # default; would resolve to ./personas/alice.md
        personas:
          - name: alice
            cwd: .
            personas_dir: {other}        # but this overrides
    """))
    load_personas_config(cfg)
    built = resolve("alice").build_config()
    assert "elsewhere/alice" in built.instructions


def test_permission_mode_passthrough(tmp_path):
    cfg = _write_config(tmp_path, textwrap.dedent("""\
        personas:
          - name: alice
            cwd: .
            prompt: "..."
            permission_mode: plan
    """))
    load_personas_config(cfg)
    built = resolve("alice").build_config()
    assert built.extra["permission_mode"] == "plan"


def test_extra_without_add_dirs_passes_through_untouched(tmp_path):
    """Non-path-list keys in `extra` are not resolved (we'd mangle non-paths)."""
    cfg = _write_config(tmp_path, textwrap.dedent("""\
        personas:
          - name: alice
            cwd: .
            prompt: "..."
            extra:
              some_flag: true
              hint: "not a path"
    """))
    load_personas_config(cfg)
    built = resolve("alice").build_config()
    assert built.extra["some_flag"] is True
    assert built.extra["hint"] == "not a path"


def test_extra_add_dirs_must_be_list_to_be_resolved(tmp_path):
    """If add_dirs is malformed (not a list), pass through without resolution.

    Lets a hypothetical future consumer interpret non-list `add_dirs` (e.g.
    a single string) without the loader silently rewriting it.
    """
    cfg = _write_config(tmp_path, textwrap.dedent("""\
        personas:
          - name: alice
            cwd: .
            prompt: "..."
            extra:
              add_dirs: "./not-a-list"
    """))
    load_personas_config(cfg)
    built = resolve("alice").build_config()
    assert built.extra["add_dirs"] == "./not-a-list"


def test_autoload_via_env(tmp_path, monkeypatch, caplog):
    """Setting TIGERHARNESS_PERSONAS_CONFIG fires the loader at import time.

    Re-imports the personas module under a patched env var to trigger the
    module-top `_autoload_from_env()` call.
    """
    cfg = _write_config(tmp_path, textwrap.dedent("""\
        personas:
          - name: alice
            cwd: .
            prompt: "..."
    """))
    monkeypatch.setenv("TIGERHARNESS_PERSONAS_CONFIG", str(cfg))
    clear_registry()
    importlib.reload(personas_mod)
    try:
        assert "alice" in {p.name for p in personas_mod.list_personas()}
    finally:
        # Restore module state for other tests in this process.
        importlib.reload(personas_mod)


def test_autoload_swallows_errors_with_warning(tmp_path, monkeypatch, caplog):
    """A broken config must not break unrelated Python invocations."""
    bogus = tmp_path / "does-not-exist.yaml"
    monkeypatch.setenv("TIGERHARNESS_PERSONAS_CONFIG", str(bogus))
    clear_registry()
    with caplog.at_level(logging.WARNING, logger="tigerharness.task_runner.personas"):
        importlib.reload(personas_mod)
    try:
        messages = [r.message for r in caplog.records]
        assert any("failed to autoload" in m for m in messages), messages
    finally:
        importlib.reload(personas_mod)

"""Tests for the ``memory:`` config block (design §7; plan §2 dev-1)."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from tigerharness.tiger_memory.config import (
    ConfigError,
    MemoryConfig,
    load_config,
)


def _write_cfg(tmp_path: Path, memory_block: str) -> Path:
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        dedent(
            f"""\
            agent:
              name: Mitsui
              role: data-integrity
            store:
              root: {tmp_path}/memory
            sources:
              - kind: claude_code
                project_path: {tmp_path}/p/
            summarizer:
              backend: anthropic
              model: m
              prompts: default/v1
            """
        )
        + memory_block
    )
    return cfg


def test_memory_defaults_when_block_absent(minimal_config_yaml: Path) -> None:
    """No ``memory:`` block -> the documented defaults (design §7)."""
    cfg = load_config(minimal_config_yaml)
    m = cfg.memory
    assert isinstance(m, MemoryConfig)
    assert m.length_unit == "characters"
    assert m.skills.max_count == 40
    assert m.skills.overflow_limit == 50
    assert m.must_remember.max_length == 8000
    assert m.must_remember.overflow_limit == 10000
    assert m.emotional_log.max_length == 12000
    assert m.emotional_log.overflow_limit == 15000
    assert m.emotional_log.weight_cap == 10.0
    assert m.emotional_log.decay.magnitude_per_day == 0.1


def test_memory_full_override(tmp_path: Path) -> None:
    cfg = _write_cfg(
        tmp_path,
        dedent(
            """\
            memory:
              length_unit: characters
              skills:
                max_count: 10
                overflow_limit: 15
              must_remember:
                max_length: 500
                overflow_limit: 700
              emotional_log:
                max_length: 600
                overflow_limit: 900
                weight_cap: 8
                decay:
                  magnitude_per_day: 0.25
            """
        ),
    )
    m = load_config(cfg).memory
    assert m.skills.max_count == 10 and m.skills.overflow_limit == 15
    assert m.must_remember.max_length == 500
    assert m.must_remember.overflow_limit == 700
    assert m.emotional_log.weight_cap == 8.0
    assert m.emotional_log.decay.magnitude_per_day == 0.25


def test_memory_partial_override_keeps_defaults(tmp_path: Path) -> None:
    """Only some keys set -> the rest fall back to defaults."""
    cfg = _write_cfg(
        tmp_path,
        dedent(
            """\
            memory:
              skills:
                max_count: 12
            """
        ),
    )
    m = load_config(cfg).memory
    assert m.skills.max_count == 12
    # Untouched -> default.
    assert m.skills.overflow_limit == 50
    assert m.must_remember.max_length == 8000
    assert m.emotional_log.decay.magnitude_per_day == 0.1


def test_rejects_token_length_unit(tmp_path: Path) -> None:
    cfg = _write_cfg(
        tmp_path,
        "memory:\n  length_unit: tokens\n",
    )
    with pytest.raises(ConfigError, match="length_unit"):
        load_config(cfg)


def test_rejects_skills_overflow_not_above_max(tmp_path: Path) -> None:
    cfg = _write_cfg(
        tmp_path,
        dedent(
            """\
            memory:
              skills:
                max_count: 20
                overflow_limit: 20
            """
        ),
    )
    with pytest.raises(ConfigError, match="overflow_limit must be > max_count"):
        load_config(cfg)


def test_rejects_zero_max(tmp_path: Path) -> None:
    cfg = _write_cfg(
        tmp_path,
        dedent(
            """\
            memory:
              must_remember:
                max_length: 0
                overflow_limit: 10
            """
        ),
    )
    with pytest.raises(ConfigError, match="max_length must be > 0"):
        load_config(cfg)


def test_rejects_emotional_overflow_not_above_max(tmp_path: Path) -> None:
    cfg = _write_cfg(
        tmp_path,
        dedent(
            """\
            memory:
              emotional_log:
                max_length: 900
                overflow_limit: 800
            """
        ),
    )
    with pytest.raises(ConfigError, match="overflow_limit must be > max_length"):
        load_config(cfg)


def test_rejects_nonpositive_weight_cap(tmp_path: Path) -> None:
    cfg = _write_cfg(
        tmp_path,
        dedent(
            """\
            memory:
              emotional_log:
                weight_cap: 0
            """
        ),
    )
    with pytest.raises(ConfigError, match="weight_cap must be > 0"):
        load_config(cfg)


def test_rejects_negative_decay_rate(tmp_path: Path) -> None:
    cfg = _write_cfg(
        tmp_path,
        dedent(
            """\
            memory:
              emotional_log:
                decay:
                  magnitude_per_day: -0.1
            """
        ),
    )
    with pytest.raises(ConfigError, match="magnitude_per_day must be"):
        load_config(cfg)


def test_zero_decay_rate_allowed(tmp_path: Path) -> None:
    """A 0 decay rate (freeze magnitudes) is valid — only negative is rejected."""
    cfg = _write_cfg(
        tmp_path,
        dedent(
            """\
            memory:
              emotional_log:
                decay:
                  magnitude_per_day: 0
            """
        ),
    )
    m = load_config(cfg).memory
    assert m.emotional_log.decay.magnitude_per_day == 0.0

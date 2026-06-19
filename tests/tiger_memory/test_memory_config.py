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
    assert m.diary.max_length == 12000
    assert m.diary.overflow_limit == 15000
    assert m.diary.weight_cap == 10.0
    assert m.diary.decay.magnitude_per_day == 0.1


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
              diary:
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
    assert m.diary.weight_cap == 8.0
    assert m.diary.decay.magnitude_per_day == 0.25


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
    assert m.diary.decay.magnitude_per_day == 0.1


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
              diary:
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
              diary:
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
              diary:
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
              diary:
                decay:
                  magnitude_per_day: 0
            """
        ),
    )
    m = load_config(cfg).memory
    assert m.diary.decay.magnitude_per_day == 0.0


@pytest.mark.parametrize(
    "block, field",
    [
        (
            "memory:\n  skills:\n    max_count: lots\n",
            "memory.skills.max_count",
        ),
        (
            "memory:\n  must_remember:\n    max_length: big\n",
            "memory.must_remember.max_length",
        ),
        (
            "memory:\n  diary:\n    weight_cap: heavy\n",
            "memory.diary.weight_cap",
        ),
        (
            "memory:\n  diary:\n    decay:\n      magnitude_per_day: fast\n",
            "memory.diary.decay.magnitude_per_day",
        ),
    ],
)
def test_rejects_non_numeric_memory_value_with_config_error(
    tmp_path: Path, block: str, field: str
) -> None:
    """QI-4 (convergence pass #3): a non-numeric ``memory:`` value must raise the
    contracted ``ConfigError`` with the field named, not leak a raw ValueError."""
    cfg = _write_cfg(tmp_path, block)
    with pytest.raises(ConfigError, match=field.replace(".", r"\.")):
        load_config(cfg)

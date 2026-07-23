"""Tests for the ``memory:`` config block (design §7; stores per ADR 0007)."""
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
    """No ``memory:`` block -> the documented defaults (ADR 0007)."""
    cfg = load_config(minimal_config_yaml)
    m = cfg.memory
    assert isinstance(m, MemoryConfig)
    assert m.length_unit == "characters"
    # Skills are index-LENGTH bounded now (no max_count anywhere).
    assert m.skills.index_max_length == 1000
    assert m.skills.index_overflow_limit == 1500
    assert m.skills.detail_max_length == 3000
    assert m.skills.detail_overflow_limit == 4500
    assert not hasattr(m.skills, "max_count")
    # Must-remember tightened to 1000/1500 by the revamp.
    assert m.must_remember.max_length == 1000
    assert m.must_remember.overflow_limit == 1500
    # Topics: index + per-topic detail bounds + freshness windows.
    assert m.topics.index_max_length == 1000
    assert m.topics.index_overflow_limit == 1500
    assert m.topics.detail_max_length == 3000
    assert m.topics.detail_overflow_limit == 4500
    assert m.topics.fresh_days == 7
    assert m.topics.forget_days == 60
    # Diary/fuzzy stores are gone.
    assert not hasattr(m, "diary")
    assert not hasattr(m, "fuzzy")


def test_memory_full_override(tmp_path: Path) -> None:
    cfg = _write_cfg(
        tmp_path,
        dedent(
            """\
            memory:
              length_unit: characters
              skills:
                index_max_length: 800
                index_overflow_limit: 1200
                detail_max_length: 2000
                detail_overflow_limit: 2500
              must_remember:
                max_length: 500
                overflow_limit: 700
              topics:
                index_max_length: 900
                index_overflow_limit: 1300
                detail_max_length: 2200
                detail_overflow_limit: 3300
                fresh_days: 3
                forget_days: 30
            """
        ),
    )
    m = load_config(cfg).memory
    assert m.skills.index_max_length == 800
    assert m.skills.index_overflow_limit == 1200
    assert m.skills.detail_max_length == 2000
    assert m.skills.detail_overflow_limit == 2500
    assert m.must_remember.max_length == 500
    assert m.must_remember.overflow_limit == 700
    assert m.topics.index_max_length == 900
    assert m.topics.index_overflow_limit == 1300
    assert m.topics.detail_max_length == 2200
    assert m.topics.detail_overflow_limit == 3300
    assert m.topics.fresh_days == 3
    assert m.topics.forget_days == 30


def test_memory_partial_override_keeps_defaults(tmp_path: Path) -> None:
    """Only some keys set -> the rest fall back to defaults."""
    cfg = _write_cfg(
        tmp_path,
        dedent(
            """\
            memory:
              skills:
                index_max_length: 700
              topics:
                fresh_days: 2
            """
        ),
    )
    m = load_config(cfg).memory
    assert m.skills.index_max_length == 700
    # Untouched -> defaults.
    assert m.skills.index_overflow_limit == 1500
    assert m.skills.detail_max_length == 3000
    assert m.must_remember.max_length == 1000
    assert m.topics.fresh_days == 2
    assert m.topics.forget_days == 60
    assert m.topics.index_max_length == 1000


def test_removed_store_keys_are_ignored(tmp_path: Path) -> None:
    """An older config carrying diary:/fuzzy: blocks must still load —
    unknown keys inside ``memory:`` are ignored, not fatal (forward-compat
    with pre-ADR-0007 configs)."""
    cfg = _write_cfg(
        tmp_path,
        dedent(
            """\
            memory:
              diary:
                max_length: 6000
                overflow_limit: 8000
                weight_cap: 10
                evocation_enabled: true
                decay:
                  magnitude_per_day: 0.1
              fuzzy:
                max_length: 4000
                overflow_limit: 6000
              skills:
                index_max_length: 1100
                index_overflow_limit: 1600
            """
        ),
    )
    m = load_config(cfg).memory
    assert m.skills.index_max_length == 1100
    assert m.skills.index_overflow_limit == 1600
    # The stale blocks never surface on the parsed config.
    assert not hasattr(m, "diary")
    assert not hasattr(m, "fuzzy")
    # And the live stores keep their defaults.
    assert m.must_remember.max_length == 1000
    assert m.topics.forget_days == 60


def test_rejects_token_length_unit(tmp_path: Path) -> None:
    cfg = _write_cfg(tmp_path, "memory:\n  length_unit: tokens\n")
    with pytest.raises(ConfigError, match="length_unit"):
        load_config(cfg)


def test_rejects_words_length_unit(tmp_path: Path) -> None:
    """Anything but 'characters' is rejected, not just 'tokens'."""
    cfg = _write_cfg(tmp_path, "memory:\n  length_unit: words\n")
    with pytest.raises(ConfigError, match="must be 'characters'"):
        load_config(cfg)


# ----- hysteresis-band validation (both bounds of both indexed stores) -----


@pytest.mark.parametrize(
    "block, match",
    [
        # skills index band collapsed
        (
            "memory:\n  skills:\n    index_max_length: 1500\n"
            "    index_overflow_limit: 1500\n",
            r"memory\.skills\.overflow_limit must be > index_max_length",
        ),
        # skills detail band inverted
        (
            "memory:\n  skills:\n    detail_max_length: 5000\n"
            "    detail_overflow_limit: 4500\n",
            r"memory\.skills\.overflow_limit must be > detail_max_length",
        ),
        # topics index band inverted
        (
            "memory:\n  topics:\n    index_max_length: 2000\n"
            "    index_overflow_limit: 1500\n",
            r"memory\.topics\.overflow_limit must be > index_max_length",
        ),
        # topics detail band collapsed
        (
            "memory:\n  topics:\n    detail_max_length: 4500\n"
            "    detail_overflow_limit: 4500\n",
            r"memory\.topics\.overflow_limit must be > detail_max_length",
        ),
        # must_remember band inverted
        (
            "memory:\n  must_remember:\n    max_length: 900\n"
            "    overflow_limit: 800\n",
            r"memory\.must_remember\.overflow_limit must be > max_length",
        ),
    ],
)
def test_rejects_inverted_or_collapsed_hysteresis_band(
    tmp_path: Path, block: str, match: str
) -> None:
    cfg = _write_cfg(tmp_path, block)
    with pytest.raises(ConfigError, match=match):
        load_config(cfg)


@pytest.mark.parametrize(
    "block, match",
    [
        (
            "memory:\n  skills:\n    index_max_length: 0\n",
            r"memory\.skills\.index_max_length must be > 0",
        ),
        (
            "memory:\n  skills:\n    detail_max_length: -1\n",
            r"memory\.skills\.detail_max_length must be > 0",
        ),
        (
            "memory:\n  must_remember:\n    max_length: 0\n"
            "    overflow_limit: 10\n",
            r"memory\.must_remember\.max_length must be > 0",
        ),
        (
            "memory:\n  topics:\n    index_max_length: 0\n",
            r"memory\.topics\.index_max_length must be > 0",
        ),
        (
            "memory:\n  topics:\n    detail_max_length: 0\n",
            r"memory\.topics\.detail_max_length must be > 0",
        ),
    ],
)
def test_rejects_nonpositive_max(tmp_path: Path, block: str, match: str) -> None:
    cfg = _write_cfg(tmp_path, block)
    with pytest.raises(ConfigError, match=match):
        load_config(cfg)


# ----- topics freshness windows ---------------------------------------------


def test_rejects_negative_fresh_days(tmp_path: Path) -> None:
    cfg = _write_cfg(
        tmp_path,
        "memory:\n  topics:\n    fresh_days: -1\n",
    )
    with pytest.raises(ConfigError, match=r"fresh_days must be"):
        load_config(cfg)


def test_rejects_forget_days_below_fresh_days(tmp_path: Path) -> None:
    cfg = _write_cfg(
        tmp_path,
        "memory:\n  topics:\n    fresh_days: 10\n    forget_days: 9\n",
    )
    with pytest.raises(
        ConfigError, match=r"forget_days \(9\) must be .* fresh_days \(10\)"
    ):
        load_config(cfg)


def test_forget_days_equal_fresh_days_allowed(tmp_path: Path) -> None:
    """The boundary case is legal: forget_days >= fresh_days."""
    cfg = _write_cfg(
        tmp_path,
        "memory:\n  topics:\n    fresh_days: 14\n    forget_days: 14\n",
    )
    m = load_config(cfg).memory
    assert m.topics.fresh_days == 14
    assert m.topics.forget_days == 14


def test_zero_fresh_days_allowed(tmp_path: Path) -> None:
    """fresh_days: 0 (no freshness protection) is valid — only negative fails."""
    cfg = _write_cfg(
        tmp_path,
        "memory:\n  topics:\n    fresh_days: 0\n    forget_days: 1\n",
    )
    assert load_config(cfg).memory.topics.fresh_days == 0


# ----- non-numeric values must raise the contracted ConfigError (QI-4) ------


@pytest.mark.parametrize(
    "block, field",
    [
        (
            "memory:\n  skills:\n    index_max_length: lots\n",
            "memory.skills.index_max_length",
        ),
        (
            "memory:\n  skills:\n    index_overflow_limit: many\n",
            "memory.skills.index_overflow_limit",
        ),
        (
            "memory:\n  skills:\n    detail_max_length: big\n",
            "memory.skills.detail_max_length",
        ),
        (
            "memory:\n  skills:\n    detail_overflow_limit: [1, 2]\n",
            "memory.skills.detail_overflow_limit",
        ),
        (
            "memory:\n  must_remember:\n    max_length: big\n",
            "memory.must_remember.max_length",
        ),
        (
            "memory:\n  must_remember:\n    overflow_limit: huge\n",
            "memory.must_remember.overflow_limit",
        ),
        (
            "memory:\n  topics:\n    index_max_length: lots\n",
            "memory.topics.index_max_length",
        ),
        (
            "memory:\n  topics:\n    index_overflow_limit: {a: 1}\n",
            "memory.topics.index_overflow_limit",
        ),
        (
            "memory:\n  topics:\n    detail_max_length: wide\n",
            "memory.topics.detail_max_length",
        ),
        (
            "memory:\n  topics:\n    detail_overflow_limit: wider\n",
            "memory.topics.detail_overflow_limit",
        ),
        (
            "memory:\n  topics:\n    fresh_days: soon\n",
            "memory.topics.fresh_days",
        ),
        (
            "memory:\n  topics:\n    forget_days: never\n",
            "memory.topics.forget_days",
        ),
    ],
)
def test_rejects_non_numeric_memory_value_with_config_error(
    tmp_path: Path, block: str, field: str
) -> None:
    """A non-numeric ``memory:`` value must raise the contracted ``ConfigError``
    with the field named, not leak a raw ValueError/TypeError."""
    cfg = _write_cfg(tmp_path, block)
    with pytest.raises(ConfigError, match=field.replace(".", r"\.")):
        load_config(cfg)

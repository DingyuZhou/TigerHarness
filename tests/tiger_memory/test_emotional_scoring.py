"""Tests for signed-weight scoring (emotional.py, design §4.3; plan §2 dev-2).

Boundary cases hit hard: decay exactly at 0, at ±weight_cap, sign preserved
until 0 (never flips, never -0.0), clamp on every update, and the
magnitude+recency keep-rank.
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.emotional import (
    clamp_weight,
    decay_entry,
    decay_weight,
    emotional_keep_rank,
)
from tigerharness.tiger_memory.entries import EmotionalEntry

NOW = "2026-06-17T00:00:00Z"


def _cfg(tmp_path: Path, *, cap: float = 10.0, rate: float = 0.1):
    p = tmp_path / "cfg.yaml"
    p.write_text(
        dedent(
            f"""\
            agent:
              name: Rukawa
              role: r
            store:
              root: {tmp_path}/memory
            sources:
              - kind: claude_code
                project_path: {tmp_path}/p/
            summarizer:
              backend: anthropic
              model: m
              prompts: default/v1
            memory:
              emotional_log:
                max_length: 40
                overflow_limit: 60
                weight_cap: {cap}
                decay:
                  magnitude_per_day: {rate}
            """
        )
    )
    return load_config(p)


def _emo(weight: float, last_used: str = NOW) -> EmotionalEntry:
    return EmotionalEntry(
        text="did x", created_at=NOW, last_used=last_used, source="extract",
        weight=weight, reaction="ok",
    )


# ----- clamp ----------------------------------------------------------------


def test_clamp_within_cap_is_identity(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    assert clamp_weight(5.0, cfg) == 5.0
    assert clamp_weight(-5.0, cfg) == -5.0


def test_clamp_at_exactly_cap(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, cap=10.0)
    assert clamp_weight(10.0, cfg) == 10.0
    assert clamp_weight(-10.0, cfg) == -10.0


def test_clamp_above_cap(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, cap=10.0)
    assert clamp_weight(99.0, cfg) == 10.0
    assert clamp_weight(-99.0, cfg) == -10.0


def test_clamp_collapses_negative_zero(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    out = clamp_weight(-0.0, cfg)
    assert out == 0.0
    # No negative zero leaks through.
    assert str(out) == "0.0"


# ----- decay ----------------------------------------------------------------


def test_decay_shrinks_magnitude_positive(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, rate=0.1)
    # 5.0 over 10 days at 0.1/day -> 5.0 - 1.0 = 4.0
    assert decay_weight(5.0, 10, cfg) == pytest.approx(4.0)


def test_decay_shrinks_magnitude_negative_sign_preserved(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, rate=0.1)
    # -5.0 over 10 days -> -4.0 (sign preserved)
    assert decay_weight(-5.0, 10, cfg) == pytest.approx(-4.0)


def test_decay_hits_exactly_zero(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, rate=0.1)
    # 5.0 over exactly 50 days -> 0.0
    out = decay_weight(5.0, 50, cfg)
    assert out == 0.0
    assert str(out) == "0.0"  # not -0.0


def test_decay_never_overshoots_into_opposite_sign(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, rate=0.1)
    # 5.0 over 100 days would be -5.0 if unguarded; must pin at 0.
    assert decay_weight(5.0, 100, cfg) == 0.0
    assert decay_weight(-5.0, 100, cfg) == 0.0


def test_decay_no_elapsed_time_is_clamped_identity(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    assert decay_weight(3.0, 0, cfg) == 3.0
    assert decay_weight(3.0, -5, cfg) == 3.0  # negative days -> no change
    # ...and still clamps an out-of-range input even with no decay.
    assert decay_weight(99.0, 0, cfg) == 10.0


def test_decay_disabled_when_rate_zero(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, rate=0.0)
    assert decay_weight(5.0, 1000, cfg) == 5.0


def test_decay_of_zero_stays_zero(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, rate=0.1)
    out = decay_weight(0.0, 10, cfg)
    assert out == 0.0
    assert str(out) == "0.0"


def test_decay_clamps_before_decaying(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, cap=10.0, rate=0.1)
    # 99 clamps to 10, then 10 days * 0.1 = 1 -> 9.0
    assert decay_weight(99.0, 10, cfg) == pytest.approx(9.0)


# ----- decay_entry (last_used anchored) -------------------------------------


def test_decay_entry_uses_last_used(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, rate=0.1)
    e = _emo(5.0, last_used="2026-06-07T00:00:00Z")  # 10 days before NOW
    assert decay_entry(e, NOW, cfg) == pytest.approx(4.0)


# ----- keep-rank ------------------------------------------------------------


def test_keep_rank_orders_by_magnitude_then_recency(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, rate=0.0)  # no decay so weights stay put
    strong_pos = _emo(8.0)
    strong_neg = _emo(-8.0)
    weak = _emo(1.0)
    ranked = sorted(
        [strong_pos, weak, strong_neg],
        key=lambda e: emotional_keep_rank(e, NOW, cfg),
    )
    # Ascending = forget order: weakest first, strong (either sign) last.
    assert ranked[0] is weak
    assert abs(ranked[-1].weight) == 8.0


def test_keep_rank_recency_tiebreak(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, rate=0.0)
    old = _emo(5.0, last_used="2026-01-01T00:00:00Z")
    fresh = _emo(5.0, last_used=NOW)
    # Equal magnitude: the older one ranks lower (sorts first = forget first).
    ranked = sorted([fresh, old], key=lambda e: emotional_keep_rank(e, NOW, cfg))
    assert ranked[0] is old and ranked[1] is fresh

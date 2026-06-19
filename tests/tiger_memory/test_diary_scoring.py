"""Tests for signed-weight scoring (emotional.py, design §4.3; plan §2 dev-2).

Boundary cases hit hard: decay exactly at 0, at ±weight_cap, sign preserved
until 0 (never flips, never -0.0), clamp on every update, and the
magnitude+recency keep-rank.
"""
from __future__ import annotations

import math
import random
from pathlib import Path
from textwrap import dedent

import pytest

from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.diary import (
    clamp_weight,
    decay_entry,
    decay_weight,
    emotional_keep_rank,
)
from tigerharness.tiger_memory.entries import DiaryEntry, EntryError

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
              diary:
                max_length: 40
                overflow_limit: 60
                weight_cap: {cap}
                decay:
                  magnitude_per_day: {rate}
            """
        )
    )
    return load_config(p)


def _emo(weight: float, last_used: str = NOW) -> DiaryEntry:
    return DiaryEntry(
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


# ----- non-finite hardening (GAP-3 scoring, defense in depth) ---------------


def test_clamp_inf_maps_to_cap(tmp_path: Path) -> None:
    """+inf clamps to +cap and -inf to -cap (an over-cap value is exactly
    what the cap bounds) — never propagates a non-finite into the math."""
    cfg = _cfg(tmp_path, cap=10.0)
    assert clamp_weight(float("inf"), cfg) == 10.0
    assert clamp_weight(float("-inf"), cfg) == -10.0


def test_clamp_nan_is_rejected(tmp_path: Path) -> None:
    """NaN has no defensible clamp target (unordered vs the cap), so it is
    rejected rather than silently returned — a NaN poisons the keep-rank
    sort into a non-deterministic order (the GAP-3 symptom)."""
    cfg = _cfg(tmp_path)
    with pytest.raises(EntryError, match="finite"):
        clamp_weight(float("nan"), cfg)


def test_decay_nan_is_rejected(tmp_path: Path) -> None:
    """decay_weight clamps first, so it inherits the NaN rejection — a
    non-finite weight can never reach the decay math either."""
    cfg = _cfg(tmp_path, rate=0.1)
    with pytest.raises(EntryError, match="finite"):
        decay_weight(float("nan"), 5, cfg)


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


# ----- keep-rank DETERMINISM regression (GAP-3 — the real symptom) ----------


def test_keep_rank_forget_order_is_stable_across_shuffles(tmp_path: Path) -> None:
    """The GAP-3 symptom was that a NaN weight made ``keep_rank``'s ``sorted``
    non-deterministic, so the strongest feeling could be forgotten first.

    With Mitsui's schema reject + the clamp/decay hardening, a NaN can never
    enter. This regression guards that for ALL valid (finite) inputs the
    keep-rank yields a **total, deterministic** forget order: shuffling the
    same set of entries always produces the same sorted (forget) order, and
    the strongest ``|weight|`` is never ranked below a weaker one.

    Weights span the full valid range INCLUDING the boundary values the audit
    cared about: both cap edges (±10), zero, and near-boundary magnitudes.
    """
    cfg = _cfg(tmp_path, rate=0.0)  # no decay -> magnitude == |weight|
    # Distinct |weight| so the order is uniquely determined by magnitude;
    # boundary values included (±cap, 0, near-cap, weak).
    entries = [
        _emo(10.0),    # +cap
        _emo(-10.0),   # -cap (same magnitude as +cap -> recency would tie;
                       #       give them distinct last_used below)
        _emo(0.0),     # neutral (weakest)
        _emo(9.999),   # just under cap
        _emo(-2.5),
        _emo(0.5),
        _emo(7.0),
        _emo(-9.999),
    ]
    # Make every entry's recency distinct so (magnitude, recency) is a TOTAL
    # order even where magnitudes tie (±10, ±9.999) — a total key is what makes
    # the sort deterministic regardless of input permutation.
    for i, e in enumerate(entries):
        # earlier index -> older -> lower recency; strictly monotonic.
        day = f"{1 + i:02d}"
        e.last_used = f"2026-06-{day}T00:00:00Z"

    def forget_order(items):
        return [
            e.id
            for e in sorted(items, key=lambda x: emotional_keep_rank(x, NOW, cfg))
        ]

    baseline = forget_order(entries)

    rng = random.Random(20260617)
    for _ in range(200):
        shuffled = entries[:]
        rng.shuffle(shuffled)
        assert forget_order(shuffled) == baseline

    # The strongest |weight| must never rank below a weaker one: walking the
    # forget order (ascending keep-rank), decayed magnitude is non-decreasing.
    by_id = {e.id: e for e in entries}
    mags = [abs(by_id[eid].weight) for eid in baseline]
    assert mags == sorted(mags), "forget order must be ascending in magnitude"
    # The single strongest |weight| (a cap entry) is last = forgotten last.
    strongest_mag = max(abs(e.weight) for e in entries)
    assert abs(by_id[baseline[-1]].weight) == strongest_mag


def test_keep_rank_total_order_no_unhashable_or_nonfinite(tmp_path: Path) -> None:
    """Every keep-rank key is a finite, comparable ``(float, float)`` tuple —
    no NaN can sneak into the key and break the sort's total order."""
    cfg = _cfg(tmp_path, rate=0.1)
    for w in (-10.0, -0.0, 0.0, 0.5, 9.999, 10.0):
        key = emotional_keep_rank(_emo(w), NOW, cfg)
        assert isinstance(key, tuple) and len(key) == 2
        assert all(math.isfinite(part) for part in key)

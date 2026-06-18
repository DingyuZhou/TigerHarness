"""Tests for skill-importance scoring (skills.py, design §4.1, §10.3).

Importance grows with usage, NO continuous time-decay; recency feeds the
keep-rank so old/unused skills rank lower.
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.entries import SkillEntry
from tigerharness.tiger_memory.skills import (
    refresh_importance,
    skill_importance,
    skills_keep_rank,
)

NOW = "2026-06-17T00:00:00Z"
LONG_AGO = "2025-01-01T00:00:00Z"


def _cfg(tmp_path: Path):
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
            """
        )
    )
    return load_config(p)


def _skill(usage: int, last_used: str = NOW, name: str = "S") -> SkillEntry:
    return SkillEntry(
        text="body", created_at=NOW, last_used=last_used, source="extract",
        name=name, trigger="when", procedure="do", usage_count=usage,
    )


def test_importance_zero_for_unused(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    assert skill_importance(0, NOW, NOW, cfg) == 0.0


def test_importance_grows_with_usage(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    i1 = skill_importance(1, NOW, NOW, cfg)
    i5 = skill_importance(5, NOW, NOW, cfg)
    i50 = skill_importance(50, NOW, NOW, cfg)
    assert 0.0 < i1 < i5 < i50


def test_importance_has_no_time_decay(tmp_path: Path) -> None:
    """Same usage_count -> same importance regardless of how old last_used is."""
    cfg = _cfg(tmp_path)
    fresh = skill_importance(5, NOW, NOW, cfg)
    stale = skill_importance(5, LONG_AGO, NOW, cfg)
    assert fresh == stale


def test_importance_negative_usage_treated_as_zero(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    assert skill_importance(-3, NOW, NOW, cfg) == 0.0


# ----- keep-rank ------------------------------------------------------------


def test_keep_rank_more_usage_ranks_higher(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    low = _skill(1)
    high = _skill(20)
    ranked = sorted([high, low], key=lambda s: skills_keep_rank(s, NOW, cfg))
    # Ascending = forget order: least-used first.
    assert ranked[0] is low and ranked[1] is high


def test_keep_rank_recency_tiebreak_old_unused_ranks_lower(
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    old = _skill(5, last_used=LONG_AGO, name="Old")
    fresh = _skill(5, last_used=NOW, name="Fresh")
    ranked = sorted([fresh, old], key=lambda s: skills_keep_rank(s, NOW, cfg))
    # Same usage: the old/unused one ranks lower -> forgotten first.
    assert ranked[0] is old and ranked[1] is fresh


def test_refresh_importance_writes_scalar(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    s = _skill(10)
    s.importance = 0.0
    refresh_importance(s, NOW, cfg)
    assert s.importance == pytest.approx(skill_importance(10, NOW, NOW, cfg))
    assert s.importance > 0.0

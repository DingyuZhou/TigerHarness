"""Unit tests for associative reinforcement (reinforce.py) — b1-dev-1 (Mitsui).

Covers the three per-store reinforcement mutations (diary weight+recency; the
must_remember / skills count bumps) and the concise recall-reference builder —
including the +1-toward-sign rule, the 0-weight case, the weight_cap clamp (no
runaway hub), and a human-findable locating token per evoked store.
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.entries import (
    DiaryEntry,
    MustRememberEntry,
    SkillEntry,
)
from tigerharness.tiger_memory.reinforce import (
    build_recall_reference,
    reinforce_diary,
    reinforce_must_remember,
    reinforce_skill,
)

TS = "2026-06-19T00:00:00Z"
NOW = "2026-06-22T12:00:00Z"


def _cfg(tmp_path: Path, cap: float = 10.0):
    p = tmp_path / "cfg.yaml"
    p.write_text(dedent(f"""\
        agent: {{name: Anzai, role: r}}
        store: {{root: {tmp_path}/m}}
        sources: [{{kind: claude_code, project_path: {tmp_path}/p/}}]
        summarizer: {{backend: anthropic, model: m, prompts: default/v1}}
        memory:
          diary: {{max_length: 4000, overflow_limit: 6000, weight_cap: {cap}}}
    """))
    return load_config(p)


def _diary(weight: float, *, text="a diary note", last_used=TS):
    return DiaryEntry(id="d1", text=text, created_at=TS, last_used=last_used,
                      source="diary", weight=weight)


def _mr(text="a fact", repeat_count=1, kind="preference"):
    e = MustRememberEntry(id="m1", text=text, created_at=TS, last_used=TS,
                          source="s", kind=kind)
    e.repeat_count = repeat_count
    return e


def _skill(usage_count=0, name="commit via -F"):
    return SkillEntry(id="s1", text="t", created_at=TS, last_used=TS, source="s",
                      name=name, trigger="trig", procedure="proc",
                      usage_count=usage_count)


# --- reinforce_diary -------------------------------------------------------

def test_reinforce_diary_positive_bumps_and_redates(tmp_path: Path):
    cfg = _cfg(tmp_path)
    e = _diary(3.0)
    reinforce_diary(e, NOW, cfg)
    assert e.weight == 4.0          # +1 toward the existing positive sign
    assert e.last_used == NOW       # recency reset -> re-dates the bullet on save


def test_reinforce_diary_negative_bumps_more_negative(tmp_path: Path):
    cfg = _cfg(tmp_path)
    e = _diary(-3.0)
    reinforce_diary(e, NOW, cfg)
    assert e.weight == -4.0         # magnitude +1 toward the existing negative sign


def test_reinforce_diary_zero_bumps_positive(tmp_path: Path):
    cfg = _cfg(tmp_path)
    e = _diary(0.0)
    reinforce_diary(e, NOW, cfg)
    assert e.weight == 1.0          # 0-weight bullet bumps positive


def test_reinforce_diary_saturates_at_cap_no_runaway(tmp_path: Path):
    cfg = _cfg(tmp_path, cap=5.0)
    e = _diary(5.0)
    reinforce_diary(e, NOW, cfg)
    assert e.weight == 5.0          # at cap: +1 then clamped -> no overshoot
    reinforce_diary(e, NOW, cfg)
    assert e.weight == 5.0          # repeated evocation stays bounded (no hub)


# --- reinforce_must_remember ----------------------------------------------

def test_reinforce_must_remember_count_and_importance(tmp_path: Path):
    e = _mr(repeat_count=2)
    reinforce_must_remember(e)
    assert e.repeat_count == 3
    assert e.importance == 3.0       # importance derived from repeat_count


# --- reinforce_skill -------------------------------------------------------

def test_reinforce_skill_usage_and_importance_grows(tmp_path: Path):
    cfg = _cfg(tmp_path)
    e1 = _skill(usage_count=1)
    reinforce_skill(e1, cfg)
    assert e1.usage_count == 2
    assert e1.importance > 0.0
    e2 = _skill(usage_count=2)
    reinforce_skill(e2, cfg)         # -> usage 3
    assert e2.importance > e1.importance   # log1p monotone, bounded growth


# --- build_recall_reference + locating tokens ------------------------------

def test_recall_reference_empty_when_no_targets():
    assert build_recall_reference([]) == ""


def test_recall_reference_skill_token():
    ref = build_recall_reference([_skill(name="commit via -F")])
    assert "recalls:" in ref
    assert 'skill "commit via -F"' in ref


def test_recall_reference_cross_store_two_tokens():
    ref = build_recall_reference(
        [_skill(name="commit via -F"), _mr(text="use -F not -m", kind="decision")]
    )
    assert 'skill "commit via -F"' in ref
    assert "must_remember/decision" in ref
    assert ";" in ref                # two locating tokens joined


def test_recall_reference_diary_token_uses_day():
    e = _diary(2.0, text="tiger-memory 4-store model",
               last_used="2026-06-19T00:00:00Z")
    ref = build_recall_reference([e])
    assert "diary 2026-06-19" in ref


def test_recall_reference_long_text_is_snippeted():
    long = "x" * 100
    ref = build_recall_reference([_mr(text=long)])
    assert "…" in ref                # long bodies truncate to a snippet
    assert long not in ref           # never a full restatement


def test_recall_reference_short_text_not_truncated():
    ref = build_recall_reference([_mr(text="short fact")])
    assert "short fact" in ref
    assert "…" not in ref

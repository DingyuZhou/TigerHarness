"""Tests for the fuzzy store (4-store model, b1-dev-1/Miyagi) + its config.

Covers fuzzy_store load/bound/save (incl. the over-length convergence fallback
and lenient decode) and the new config (diary.fresh_days, memory.fuzzy) to 100%
branch coverage.
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from tigerharness.tiger_memory import fuzzy_store as fz
from tigerharness.tiger_memory.config import ConfigError, load_config
from tigerharness.tiger_memory.store import Store


def _cfg_store(tmp_path: Path, memory_block: str = "") -> tuple[object, Store]:
    body = dedent(f"""\
        agent: {{name: Anzai, role: r}}
        store: {{root: {tmp_path}/memory}}
        sources:
          - {{kind: claude_code, project_path: {tmp_path}/p/}}
        summarizer: {{backend: anthropic, model: m, prompts: default/v1}}
    """)
    if memory_block:
        body += memory_block
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(body)
    cfg = load_config(cfg_path)
    store = Store(cfg.store.root)
    store.init_layout()
    return cfg, store


# ----- config: fresh_days + fuzzy ------------------------------------------

def test_config_defaults(tmp_path: Path):
    cfg, _ = _cfg_store(tmp_path)
    assert cfg.memory.diary.fresh_days == 7
    assert cfg.memory.fuzzy.max_length == 4000
    assert cfg.memory.fuzzy.overflow_limit == 6000


def test_config_custom_fresh_days_and_fuzzy(tmp_path: Path):
    cfg, _ = _cfg_store(tmp_path, dedent("""\
        memory:
          diary: {fresh_days: 14}
          fuzzy: {max_length: 2000, overflow_limit: 3000}
    """))
    assert cfg.memory.diary.fresh_days == 14
    assert cfg.memory.fuzzy.max_length == 2000
    assert cfg.memory.fuzzy.overflow_limit == 3000


def test_config_negative_fresh_days_rejected(tmp_path: Path):
    with pytest.raises(ConfigError, match="fresh_days must be ≥ 0"):
        _cfg_store(tmp_path, "memory:\n  diary: {fresh_days: -1}\n")


def test_config_fuzzy_bad_bound_rejected(tmp_path: Path):
    # overflow_limit must be > max_length (hysteresis band).
    with pytest.raises(ConfigError):
        _cfg_store(tmp_path, "memory:\n  fuzzy: {max_length: 4000, overflow_limit: 4000}\n")


def test_config_fuzzy_non_int_rejected(tmp_path: Path):
    with pytest.raises(ConfigError, match="memory.fuzzy.max_length"):
        _cfg_store(tmp_path, "memory:\n  fuzzy: {max_length: lots}\n")


# ----- load_fuzzy -----------------------------------------------------------

def test_load_absent_is_empty(tmp_path: Path):
    _, store = _cfg_store(tmp_path)
    assert fz.load_fuzzy(store) == ""


def test_load_roundtrips_text(tmp_path: Path):
    cfg, store = _cfg_store(tmp_path)
    fz.save_fuzzy(cfg, store, "## Fuzzy\n- grouped gist\n")
    assert fz.load_fuzzy(store) == "## Fuzzy\n- grouped gist\n"


def test_load_lenient_on_bad_utf8(tmp_path: Path, caplog):
    _, store = _cfg_store(tmp_path)
    fz.fuzzy_path(store).write_bytes(b"good\n\xff\xfebad\n")
    out = fz.load_fuzzy(store)
    assert "�" in out  # replaced, not raised
    assert any("non-UTF8" in r.message for r in caplog.records)


# ----- bound_fuzzy ----------------------------------------------------------

def test_bound_under_limit_unchanged():
    text = "line one\nline two\n"
    assert fz.bound_fuzzy(text, 100) == (text, 0)


def test_bound_over_limit_trims_at_newline():
    text = "aaaa\nbbbb\ncccc\n"  # 15 chars
    bounded, dropped = fz.bound_fuzzy(text, 10)
    assert bounded == "aaaa\nbbbb\n" and dropped == 5
    assert len(bounded) <= 10


def test_bound_single_long_line_hard_cut():
    text = "x" * 50  # no newline
    bounded, dropped = fz.bound_fuzzy(text, 10)
    assert bounded == "x" * 10 and dropped == 40


# ----- save_fuzzy -----------------------------------------------------------

def test_save_within_bound_writes_all(tmp_path: Path):
    cfg, store = _cfg_store(tmp_path, "memory:\n  fuzzy: {max_length: 100, overflow_limit: 200}\n")
    dropped = fz.save_fuzzy(cfg, store, "short\n")
    assert dropped == 0
    assert fz.fuzzy_path(store).read_text() == "short\n"


def test_save_over_bound_trims_and_warns(tmp_path: Path, caplog):
    cfg, store = _cfg_store(tmp_path, "memory:\n  fuzzy: {max_length: 10, overflow_limit: 20}\n")
    dropped = fz.save_fuzzy(cfg, store, "aaaa\nbbbb\ncccc\n")
    assert dropped == 5
    assert len(fz.fuzzy_path(store).read_text()) <= 10
    assert any("over max_length" in r.message for r in caplog.records)

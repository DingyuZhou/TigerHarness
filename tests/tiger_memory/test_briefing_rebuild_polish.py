"""Tests for the README-in-briefing, state-preservation, and slug edge cases.

Covers the v0.4 critique fixes:
- Briefing README is emitted with agent_name substituted.
- last_bootstrap_cost_usd survives rebuild() after bootstrap().
- _slugify is case-insensitive when compared against existing dir name.
- _SafeFormat does not crash on unknown {placeholder} in template.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from textwrap import dedent

import pytest

from tigerharness.tiger_memory.briefing import _SafeFormat, _render_readme, rebuild_briefing
from tigerharness.tiger_memory.config import _slugify, load_config
from tigerharness.tiger_memory.lifecycle import _write_state, bootstrap, rebuild, Decision
from tigerharness.tiger_memory.sources.base import SourceRecord
from tigerharness.tiger_memory.store import Store
from tigerharness.tiger_memory.summarizers import MockSummarizer


def _setup(tmp_path: Path, agent_name: str = "Sai"):
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(dedent(f"""\
        agent: {{name: {agent_name}, role: T}}
        store: {{root: {tmp_path}/memory}}
        sources:
          - kind: claude_code
            project_path: {tmp_path}/proj/
        summarizer: {{backend: anthropic, model: claude-opus-4-7, prompts: default/v1}}
        rebuild:
          lock_path: {tmp_path}/lock
          idle_threshold_hours: 0
    """))
    cfg = load_config(cfg_path)
    store = Store(cfg.store.root)
    store.init_layout()
    return cfg, store


# ----- briefing README emission --------------------------------------------


def test_briefing_emits_readme_with_agent_name(tmp_path: Path) -> None:
    cfg, store = _setup(tmp_path, agent_name="Sai")
    # Need at least one short for briefing to have content.
    (store.paths.journal / "20260515-080000-aaa.md").write_text(
        "---\ntype: short_summary\n---\n- bullet\n"
    )
    rebuild_briefing(cfg, store)
    readme = store.paths.briefing / "README.md"
    assert readme.exists()
    text = readme.read_text(encoding="utf-8")
    assert "You are **Sai**" in text


def test_readme_template_safe_format_unknown_placeholder() -> None:
    """SafeFormat must NOT raise on unknown {x} in template."""
    fmt = _SafeFormat({"known": "yes"})
    assert "{unknown}".format_map(fmt) == "{unknown}"
    assert "{known}".format_map(fmt) == "yes"


# ----- state preservation across rebuild ----------------------------------


def test_bootstrap_cost_survives_rebuild(tmp_path: Path) -> None:
    cfg, store = _setup(tmp_path)
    # Simulate a bootstrap completion that wrote cost.
    _write_state(store, cfg, decisions=[], cost_usd=187.42, last_op="bootstrap")
    state1 = store.read_state()
    assert state1["last_bootstrap_cost_usd"] == 187.42

    # Now do a no-op rebuild — bootstrap cost preserved, running total accumulates.
    _write_state(store, cfg, decisions=[], cost_usd=0.0, last_op="rebuild")
    state2 = store.read_state()
    assert state2["last_bootstrap_cost_usd"] == 187.42  # NOT None
    assert state2["last_op"] == "rebuild"
    # total_cost_usd accumulates across operations.
    assert state2["total_cost_usd"] == 187.42

    # Another rebuild with non-zero cost.
    _write_state(store, cfg, decisions=[], cost_usd=2.50, last_op="rebuild")
    state3 = store.read_state()
    assert state3["total_cost_usd"] == 189.92
    assert state3["last_rebuild_cost_usd"] == 2.50


def test_compute_state_shape(tmp_path: Path) -> None:
    """`tiger-memory state` output groups costs under a `cost` block."""
    from tigerharness.tiger_memory.state import compute_state
    cfg, store = _setup(tmp_path)
    _write_state(store, cfg, decisions=[], cost_usd=10.0, last_op="bootstrap")
    out = compute_state(cfg, store)
    assert "cost" in out
    assert out["cost"]["last_bootstrap_usd"] == 10.0
    assert out["cost"]["total_usd_since_bootstrap"] == 10.0
    assert out["last_op"] == "bootstrap"


# ----- slug case-insensitivity --------------------------------------------


def test_slug_case_insensitive_no_double_append(tmp_path: Path) -> None:
    """`store.root: memory/SAI` + agent `Sai` should NOT produce memory/SAI/sai."""
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(dedent(f"""\
        agent: {{name: Sai, role: T}}
        store: {{root: {tmp_path}/memory/SAI}}
        sources:
          - kind: claude_code
            project_path: {tmp_path}/proj/
        summarizer: {{backend: anthropic, model: claude-opus-4-7, prompts: default/v1}}
    """))
    cfg = load_config(cfg_path)
    # The user-set leaf name is preserved (we don't lowercase it for them),
    # but no double-append happens.
    assert cfg.store.root.name.lower() == "sai"
    # Parent is memory/, not memory/SAI
    assert cfg.store.root.parent.name == "memory"


def test_slugify_handles_edge_cases() -> None:
    assert _slugify("Sai") == "sai"
    assert _slugify("Scout Tiger") == "scout_tiger"
    assert _slugify("  weird  *!^#  name  ") == "weird_name"
    assert _slugify("") == "agent"
    assert _slugify("123") == "123"


# ----- search auto-mode ----------------------------------------------------


def test_search_auto_mode_falls_back_to_grep_without_rag(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tigerharness.tiger_memory.drill import _rag_available, search
    cfg, store = _setup(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Force "no embedder available" — fastembed may be installed in the
    # test env, so deleting OPENAI_API_KEY alone isn't enough.
    monkeypatch.setattr("tigerharness.tiger_memory.embedders.pick_embedder", lambda _mode: None)
    assert _rag_available() is False
    (store.paths.journal / "20260515-080000-aaa.md").write_text(
        "TSMOM is a strategy.\n"
    )
    rc = search(cfg, store, topic="TSMOM", mode="auto")
    out = capsys.readouterr().out
    assert rc == 0
    assert "20260515" in out

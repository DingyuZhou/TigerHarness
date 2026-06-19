"""Tests for the state snapshot (state.py, bounded-store revamp)."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from tigerharness.tiger_memory.bounded_store import BoundedStore
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.entries import (
    STORE_DIARY,
    STORE_MUST_REMEMBER,
    STORE_SKILLS,
    DiaryEntry,
    MustRememberEntry,
    SkillEntry,
)
from tigerharness.tiger_memory.state import compute_state, iso_now
from tigerharness.tiger_memory.store import Store

NOW = "2026-06-17T00:00:00Z"


def _cfg(tmp_path: Path, extra: str = "") -> object:
    p = tmp_path / "cfg.yaml"
    p.write_text(dedent(f"""\
        agent: {{name: T, role: t}}
        store: {{root: {tmp_path}/memory}}
        sources:
          - kind: claude_code
            project_path: {tmp_path}/proj/
        summarizer: {{backend: anthropic, model: m, prompts: default/v1}}
        rebuild:
          lock_path: {tmp_path}/lock
    """) + extra)
    return load_config(p)


def test_compute_state_empty(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    payload = compute_state(cfg, store)
    assert payload["agent"] == "T"
    assert set(payload["stores"]) == {STORE_SKILLS, STORE_MUST_REMEMBER, STORE_DIARY}
    skills = payload["stores"][STORE_SKILLS]
    assert skills["count"] == 0
    assert skills["bound_unit"] == "count"
    assert payload["stores"][STORE_MUST_REMEMBER]["bound_unit"] == "characters"
    assert payload["lock"] == {"held": False, "pid": None}


def test_compute_state_with_entries(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    bstore = BoundedStore(cfg, store)
    bstore.save_atomic(STORE_SKILLS, [
        SkillEntry(text="p", created_at=NOW, last_used=NOW, source="x",
                   name="n", trigger="t", procedure="p", usage_count=1, importance=1.0),
    ])
    bstore.save_atomic(STORE_MUST_REMEMBER, [
        MustRememberEntry(text="m", created_at=NOW, last_used=NOW, source="x",
                          kind="decision", importance=1.0),
    ])
    bstore.save_atomic(STORE_DIARY, [
        DiaryEntry(text="e", created_at=NOW, last_used=NOW, source="x",
                       weight=2.0, reaction="r"),
    ])
    payload = compute_state(cfg, store)
    assert payload["stores"][STORE_SKILLS]["count"] == 1
    assert payload["stores"][STORE_MUST_REMEMBER]["count"] == 1
    assert payload["stores"][STORE_DIARY]["chars"] > 0
    assert payload["stores"][STORE_SKILLS]["over_overflow"] is False


def test_compute_state_lock_held(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    cfg.rebuild.lock_path.write_text("4242")
    payload = compute_state(cfg, store)
    assert payload["lock"] == {"held": True, "pid": 4242}


def test_compute_state_lock_unreadable_pid(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    cfg.rebuild.lock_path.write_text("not-an-int")
    payload = compute_state(cfg, store)
    assert payload["lock"] == {"held": True, "pid": None}


def test_iso_now_format() -> None:
    ts = iso_now()
    assert ts.endswith("Z") and "T" in ts

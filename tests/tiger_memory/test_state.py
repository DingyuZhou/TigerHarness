"""Tests for the state snapshot (state.py, topic-store revamp ADR 0007)."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from tigerharness.tiger_memory import indexes
from tigerharness.tiger_memory.bounded_store import BoundedStore
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.entries import (
    STORE_MUST_REMEMBER,
    STORE_SKILLS,
    STORE_TOPICS,
    MustRememberEntry,
    SkillEntry,
    TopicEntry,
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


def _skill() -> SkillEntry:
    return SkillEntry(text="p", created_at=NOW, last_used=NOW, source="x",
                      name="n", trigger="t", procedure="p",
                      usage_count=1, importance=1.0)


def _topic() -> TopicEntry:
    return TopicEntry(text="## 2026-06-17\n- a fact", created_at=NOW,
                      last_used=NOW, source="x", name="Revamp",
                      slug="revamp", summary="s", touch_count=2)


def test_compute_state_empty(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    payload = compute_state(cfg, store)
    assert payload["agent"] == "T"
    assert set(payload["stores"]) == {
        STORE_SKILLS, STORE_MUST_REMEMBER, STORE_TOPICS
    }
    for name, entry_payload in payload["stores"].items():
        assert entry_payload["count"] == 0
        assert entry_payload["bound_unit"] == "characters"
        assert entry_payload["over_overflow"] is False
    # Skills/topics measure their RENDERED index — non-zero even when empty
    # (the placeholder index is what a persona actually loads).
    skills = payload["stores"][STORE_SKILLS]
    assert skills["chars"] == len(indexes.render_skill_index([]))
    topics = payload["stores"][STORE_TOPICS]
    assert topics["chars"] == len(indexes.render_topic_index([]))
    # must_remember measures entry length: zero when empty, no detail files.
    mr = payload["stores"][STORE_MUST_REMEMBER]
    assert mr["chars"] == 0
    assert "details_over_overflow" not in mr
    assert skills["details_over_overflow"] == 0
    assert topics["details_over_overflow"] == 0
    # Default bounds (ADR 0007 Operator-set, revised 2026-07-23): max 2000.
    assert skills["max"] == 2000 and topics["max"] == 2000 and mr["max"] == 2000
    assert payload["lock"] == {"held": False, "pid": None}


def test_compute_state_with_entries(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    bstore = BoundedStore(cfg, store)
    skill, topic = _skill(), _topic()
    bstore.save_atomic(STORE_SKILLS, [skill])
    bstore.save_atomic(STORE_MUST_REMEMBER, [
        MustRememberEntry(text="m", created_at=NOW, last_used=NOW, source="x",
                          kind="decision"),
    ])
    bstore.save_atomic(STORE_TOPICS, [topic])
    payload = compute_state(cfg, store)
    assert payload["stores"][STORE_SKILLS]["count"] == 1
    assert payload["stores"][STORE_MUST_REMEMBER]["count"] == 1
    assert payload["stores"][STORE_TOPICS]["count"] == 1
    # chars is the rendered-index length for skills/topics...
    assert payload["stores"][STORE_SKILLS]["chars"] == len(
        indexes.render_skill_index([skill])
    )
    assert payload["stores"][STORE_TOPICS]["chars"] == len(
        indexes.render_topic_index([topic])
    )
    # ...and plain entry length for must_remember.
    assert payload["stores"][STORE_MUST_REMEMBER]["chars"] == 1  # "m"
    for name in (STORE_SKILLS, STORE_MUST_REMEMBER, STORE_TOPICS):
        assert payload["stores"][name]["over_overflow"] is False
    assert payload["stores"][STORE_SKILLS]["details_over_overflow"] == 0
    assert payload["stores"][STORE_TOPICS]["details_over_overflow"] == 0


def test_compute_state_over_overflow_and_detail_counts(tmp_path: Path) -> None:
    # Shrink the bounds so a single entry trips both the index-overflow flag
    # and the per-detail overflow counter for skills and topics, and the
    # length-overflow flag for must_remember.
    cfg = _cfg(tmp_path, dedent("""\
        memory:
          skills:
            index_max_length: 10
            index_overflow_limit: 20
            detail_max_length: 10
            detail_overflow_limit: 20
          topics:
            index_max_length: 10
            index_overflow_limit: 20
            detail_max_length: 10
            detail_overflow_limit: 20
          must_remember:
            max_length: 5
            overflow_limit: 10
    """))
    store = Store(cfg.store.root)
    store.init_layout()
    bstore = BoundedStore(cfg, store)
    bstore.save_atomic(STORE_SKILLS, [_skill()])
    bstore.save_atomic(STORE_TOPICS, [_topic()])
    bstore.save_atomic(STORE_MUST_REMEMBER, [
        MustRememberEntry(text="a directive well over ten chars",
                          created_at=NOW, last_used=NOW, source="x",
                          kind="decision"),
    ])
    payload = compute_state(cfg, store)
    for name in (STORE_SKILLS, STORE_TOPICS):
        assert payload["stores"][name]["over_overflow"] is True
        assert payload["stores"][name]["details_over_overflow"] == 1
        assert payload["stores"][name]["max"] == 10
    mr = payload["stores"][STORE_MUST_REMEMBER]
    assert mr["over_overflow"] is True and mr["max"] == 5
    assert mr["bound_unit"] == "characters"


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

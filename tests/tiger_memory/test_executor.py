"""Tests for the in-session extraction write-back (executor.py, ADR 0007)."""
from __future__ import annotations

import inspect
from pathlib import Path
from textwrap import dedent

import pytest

from tigerharness.tiger_memory.bounded_store import BoundedStore
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.entries import (
    STORE_MUST_REMEMBER,
    STORE_SKILLS,
    STORE_TOPICS,
)
from tigerharness.tiger_memory.executor import IngestResult, ingest_extraction
from tigerharness.tiger_memory.lifecycle import ExtractionParseError
from tigerharness.tiger_memory.store import Store

NOW = "2026-07-23T00:00:00Z"

_BUNDLE = dedent("""\
    @@SKILLS@@
    NAME: Skill One
    TRIGGER: when X
    PROCEDURE: do Y
    @@MUST_REMEMBER@@
    KIND: decision
    MEMO: store lives in-repo
    @@TOPICS@@
    TOPIC: NEW
    NAME: Store Revamp
    SUMMARY: Topic-store revamp status.
    DETAIL: shipped the three-store layout
""")

# A follow-up bundle routing a detail to the topic minted by _BUNDLE.
_FOLLOWUP = dedent("""\
    @@SKILLS@@
    NONE
    @@MUST_REMEMBER@@
    NONE
    @@TOPICS@@
    TOPIC: store-revamp
    DETAIL: compaction landed too
""")


def _cfg(tmp_path: Path) -> object:
    p = tmp_path / "cfg.yaml"
    p.write_text(dedent(f"""\
        agent: {{name: T, role: t}}
        store: {{root: {tmp_path}/memory}}
        sources:
          - kind: claude_code
            project_path: {tmp_path}/proj/
        summarizer: {{backend: anthropic, model: m, prompts: default/v1}}
    """))
    return load_config(p)


def test_ingest_extraction_writes_stores(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    result = ingest_extraction(
        store, cfg, conversation_uuid="c1", source="claude_code",
        bundle_text=_BUNDLE, now=NOW,
    )
    assert isinstance(result, IngestResult)
    assert result.conversation_uuid == "c1"
    assert result.skills_added == 1
    assert result.must_remember_added == 1
    assert result.topics_added == 1
    assert result.total_added == 3
    bstore = BoundedStore(cfg, store)
    assert len(bstore.load(STORE_SKILLS)) == 1
    assert len(bstore.load(STORE_MUST_REMEMBER)) == 1
    topics = bstore.load(STORE_TOPICS)
    assert len(topics) == 1
    t = topics[0]
    assert t.slug == "store-revamp" and t.touch_count == 1
    assert t.summary == "Topic-store revamp status."
    assert t.text == "## 2026-07-23\n- shipped the three-store layout"


def test_ingest_extraction_routes_to_existing_topic(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    ingest_extraction(store, cfg, conversation_uuid="c1", source="claude_code",
                      bundle_text=_BUNDLE, now=NOW)
    result = ingest_extraction(
        store, cfg, conversation_uuid="c2", source="claude_code",
        bundle_text=_FOLLOWUP, now="2026-07-24T00:00:00Z",
    )
    assert result.skills_added == 0
    assert result.must_remember_added == 0
    assert result.topics_added == 1
    assert result.total_added == 1
    topics = BoundedStore(cfg, store).load(STORE_TOPICS)
    assert len(topics) == 1  # routed, not duplicated
    t = topics[0]
    assert t.touch_count == 2
    assert t.last_used == "2026-07-24T00:00:00Z"
    assert "## 2026-07-24\n- compaction landed too" in t.text


def test_ingest_extraction_malformed_raises_before_write(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    with pytest.raises(ExtractionParseError):
        ingest_extraction(store, cfg, conversation_uuid="c1",
                          source="x", bundle_text="no markers here")
    # Nothing written.
    bstore = BoundedStore(cfg, store)
    assert bstore.load(STORE_SKILLS) == []
    assert bstore.load(STORE_TOPICS) == []


def test_ingest_extraction_has_no_summarizer_kwarg() -> None:
    # ADR 0007: extraction happens in the sub-agent; the write-back entry
    # point is pure parse+merge and takes no summarizer.
    params = inspect.signature(ingest_extraction).parameters
    assert "summarizer" not in params


def test_ingest_extraction_reports_touches(tmp_path):
    cfg = _cfg(tmp_path)
    from tigerharness.tiger_memory.store import Store
    store = Store(cfg.store.root)
    store.init_layout()
    from tigerharness.tiger_memory.entries import (
        STORE_MUST_REMEMBER, MustRememberEntry,
    )
    bstore = BoundedStore(cfg, store)
    old = MustRememberEntry(
        text="standing rule", created_at="2026-01-01T00:00:00Z",
        last_used="2026-01-01T00:00:00Z", source="pin", kind="preference",
        importance=1.0,
    )
    bstore.save_atomic(STORE_MUST_REMEMBER, [old])
    bundle = (
        "@@SKILLS@@\nNONE\n"
        "@@MUST_REMEMBER@@\n"
        f"TOUCH: {old.id}\n"
        "@@TOPICS@@\nNONE\n"
    )
    result = ingest_extraction(
        store, cfg, conversation_uuid="u-touch", source="test",
        bundle_text=bundle, now="2026-07-23T00:00:00Z",
    )
    assert result.touched == 1
    assert result.total_added == 0
    (loaded,) = bstore.load(STORE_MUST_REMEMBER)
    assert loaded.last_used == "2026-07-23T00:00:00Z"

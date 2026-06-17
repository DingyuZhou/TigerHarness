"""Tests for the in-session extraction write-back (executor.py)."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from tigerharness.tiger_memory.bounded_store import BoundedStore
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.entries import (
    STORE_EMOTIONAL,
    STORE_MUST_REMEMBER,
    STORE_SKILLS,
)
from tigerharness.tiger_memory.executor import IngestResult, ingest_extraction
from tigerharness.tiger_memory.lifecycle import ExtractionParseError
from tigerharness.tiger_memory.store import Store

_BUNDLE = dedent("""\
    @@SKILLS@@
    NAME: Skill One
    TRIGGER: when X
    PROCEDURE: do Y
    @@MUST_REMEMBER@@
    KIND: decision
    MEMO: store lives in-repo
    @@EMOTIONAL@@
    WEIGHT: 2
    REACTION: ok
    TEXT: shipped a thing
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
        bundle_text=_BUNDLE,
    )
    assert isinstance(result, IngestResult)
    assert result.conversation_uuid == "c1"
    assert result.skills_added == 1
    assert result.must_remember_added == 1
    assert result.emotional_added == 1
    assert result.total_added == 3
    bstore = BoundedStore(cfg, store)
    assert len(bstore.load(STORE_SKILLS)) == 1
    assert len(bstore.load(STORE_MUST_REMEMBER)) == 1
    assert len(bstore.load(STORE_EMOTIONAL)) == 1


def test_ingest_extraction_malformed_raises_before_write(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    with pytest.raises(ExtractionParseError):
        ingest_extraction(store, cfg, conversation_uuid="c1",
                          source="x", bundle_text="no markers here")
    # Nothing written.
    assert BoundedStore(cfg, store).load(STORE_SKILLS) == []

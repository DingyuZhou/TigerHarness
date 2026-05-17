"""Additional tests for must_memorize — pin, decay_all, append_dropped,
parse_extractor_output, merge_candidates, _parse_table roundtrip."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import pytest

from tigerharness.tiger_memory import frontmatter
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.must_memorize import (
    KIND_DECISION,
    KIND_INCIDENT,
    KIND_OWNER_EXPLICIT,
    KIND_PREFERENCE,
    Row,
    _find_similar,
    _parse_table,
    _render_table,
    append_dropped,
    decay_all,
    load,
    merge_candidates,
    parse_extractor_output,
    pin,
    save,
)
from tigerharness.tiger_memory.store import Store


def _setup(tmp_path: Path):
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(dedent(f"""\
        agent: {{name: T, role: T}}
        store: {{root: {tmp_path}/memory}}
        sources:
          - kind: claude_code
            project_path: {tmp_path}/proj/
        summarizer: {{backend: anthropic, model: claude-sonnet-4-6, prompts: default/v1}}
        rebuild: {{lock_path: {tmp_path}/lock}}
    """))
    cfg = load_config(cfg_path)
    store = Store(cfg.store.root)
    store.init_layout()
    return cfg, store


class TestPin:
    def test_pin_owner_explicit(self, tmp_path: Path):
        cfg, store = _setup(tmp_path)
        ret = pin(cfg, store, memo="CEO likes 47", kind="owner_explicit")
        assert ret == 0
        rows = load(store)
        assert len(rows) == 1
        assert rows[0].memo == "CEO likes 47"
        assert rows[0].locked is True

    def test_pin_preference(self, tmp_path: Path):
        cfg, store = _setup(tmp_path)
        ret = pin(cfg, store, memo="Prefers dark mode", kind="preference")
        assert ret == 0
        rows = load(store)
        assert len(rows) == 1
        assert rows[0].kind == "preference"

    def test_pin_invalid_kind(self, tmp_path: Path, capsys):
        cfg, store = _setup(tmp_path)
        ret = pin(cfg, store, memo="test", kind="bogus")
        assert ret == 2
        assert "unknown kind" in capsys.readouterr().out

    def test_pin_multiple(self, tmp_path: Path):
        cfg, store = _setup(tmp_path)
        pin(cfg, store, memo="First fact", kind="decision")
        pin(cfg, store, memo="Second fact", kind="incident")
        rows = load(store)
        assert len(rows) == 2


class TestDecayAll:
    def test_locked_rows_survive(self):
        rows = [
            Row(kind=KIND_OWNER_EXPLICIT, memo="always", score=5, locked=True,
                last_bump="2026-01-01", last_decay="2026-01-01"),
        ]
        kept = decay_all(
            rows, today="2026-12-31",
            days_per_point={KIND_PREFERENCE: 7, KIND_DECISION: 14, KIND_INCIDENT: 30},
        )
        assert len(kept) == 1
        assert kept[0].locked is True

    def test_preference_decays(self):
        rows = [
            Row(kind=KIND_PREFERENCE, memo="likes X", score=3,
                last_bump="2026-04-01", last_decay="2026-04-01"),
        ]
        kept = decay_all(
            rows, today="2026-05-15",
            days_per_point={KIND_PREFERENCE: 7, KIND_DECISION: 14, KIND_INCIDENT: 30},
        )
        # 44 days / 7 days_per_point = 6 points of decay → 3-6 = -3 → removed
        assert len(kept) == 0

    def test_row_without_anchor_gets_initialized(self):
        rows = [
            Row(kind=KIND_PREFERENCE, memo="new", score=5,
                last_bump="", last_decay=""),
        ]
        kept = decay_all(
            rows, today="2026-05-15",
            days_per_point={KIND_PREFERENCE: 7},
        )
        assert len(kept) == 1
        assert kept[0].last_decay == "2026-05-15"

    def test_future_anchor_no_decay(self):
        rows = [
            Row(kind=KIND_PREFERENCE, memo="future", score=5,
                last_bump="2026-06-01", last_decay="2026-06-01"),
        ]
        kept = decay_all(
            rows, today="2026-05-15",
            days_per_point={KIND_PREFERENCE: 7},
        )
        assert len(kept) == 1
        assert kept[0].score == 5


class TestMergeCandidates:
    def test_new_candidate_added(self):
        rows = []
        candidates = [Row(kind=KIND_PREFERENCE, memo="likes coffee")]
        kept, demoted = merge_candidates(
            rows, candidates,
            today="2026-05-15", similarity_threshold=0.8, max_rows=10,
        )
        assert len(kept) == 1
        assert kept[0].memo == "likes coffee"
        assert demoted == []

    def test_similar_candidate_bumps(self):
        rows = [Row(kind=KIND_PREFERENCE, memo="likes coffee", score=3,
                     last_bump="2026-05-01")]
        candidates = [Row(kind=KIND_PREFERENCE, memo="likes coffee a lot")]
        kept, demoted = merge_candidates(
            rows, candidates,
            today="2026-05-15", similarity_threshold=0.6, max_rows=10,
        )
        assert len(kept) == 1
        assert kept[0].score == 4  # bumped

    def test_cap_enforced(self):
        rows = [
            Row(kind=KIND_PREFERENCE, memo=f"fact {i}", score=10 - i)
            for i in range(5)
        ]
        candidates = [Row(kind=KIND_PREFERENCE, memo="new fact", score=1)]
        kept, demoted = merge_candidates(
            rows, candidates,
            today="2026-05-15", similarity_threshold=0.8, max_rows=5,
        )
        assert len(kept) == 5
        assert len(demoted) == 1

    def test_owner_explicit_promotes(self):
        rows = [Row(kind=KIND_PREFERENCE, memo="likes coffee", score=3)]
        candidates = [Row(kind=KIND_OWNER_EXPLICIT, memo="likes coffee")]
        kept, _ = merge_candidates(
            rows, candidates,
            today="2026-05-15", similarity_threshold=0.6, max_rows=10,
        )
        assert kept[0].kind == KIND_OWNER_EXPLICIT
        assert kept[0].locked is True


class TestAppendDropped:
    def test_appends_to_file(self, tmp_path: Path):
        store = Store(tmp_path / "memory")
        store.init_layout()
        demoted = [Row(kind=KIND_PREFERENCE, memo="old fact", score=0)]
        append_dropped(store, demoted)
        path = store.paths.journal / ".dropped_memorize.md"
        assert path.exists()
        text = path.read_text()
        assert "Dropped" in text
        assert "old fact" in text

    def test_empty_demoted_noop(self, tmp_path: Path):
        store = Store(tmp_path / "memory")
        store.init_layout()
        append_dropped(store, [])
        path = store.paths.journal / ".dropped_memorize.md"
        assert not path.exists()


class TestParseExtractorOutput:
    def test_none_output(self):
        assert parse_extractor_output("NONE") == []
        assert parse_extractor_output("  none  \n") == []
        assert parse_extractor_output("") == []

    def test_single_block(self):
        text = "KIND: preference\nMEMO: Likes dark mode\n"
        rows = parse_extractor_output(text)
        assert len(rows) == 1
        assert rows[0].kind == KIND_PREFERENCE
        assert rows[0].memo == "Likes dark mode"

    def test_multiple_blocks(self):
        text = dedent("""\
            KIND: preference
            MEMO: Likes dark mode

            KIND: decision
            MEMO: Chose Python over Rust
        """)
        rows = parse_extractor_output(text)
        assert len(rows) == 2

    def test_empty_memo_skipped(self):
        text = "KIND: preference\nMEMO: \n"
        rows = parse_extractor_output(text)
        assert len(rows) == 0


class TestTableRoundtrip:
    def test_render_and_parse(self):
        rows = [
            Row(kind=KIND_OWNER_EXPLICIT, memo="Important", score=0, locked=True,
                last_bump="2026-05-15", source="pin"),
            Row(kind=KIND_PREFERENCE, memo="Likes X", score=5,
                last_bump="2026-05-10", source="extract"),
        ]
        text = _render_table(rows)
        parsed = _parse_table(text)
        assert len(parsed) == 2
        assert parsed[0].kind == KIND_OWNER_EXPLICIT
        assert parsed[0].locked is True
        assert parsed[1].memo == "Likes X"

    def test_render_empty(self):
        text = _render_table([])
        assert "_(empty)_" in text

    def test_parse_ignores_header(self):
        text = dedent("""\
            | Score | Kind | Last bump | Source | Memo |
            |------:|------|-----------|--------|------|
        """)
        assert _parse_table(text) == []


class TestFindSimilar:
    def test_exact_match(self):
        rows = [Row(kind=KIND_PREFERENCE, memo="likes coffee")]
        result = _find_similar(rows, "likes coffee", threshold=0.8)
        assert result is not None

    def test_no_match(self):
        rows = [Row(kind=KIND_PREFERENCE, memo="likes coffee")]
        result = _find_similar(rows, "completely different topic", threshold=0.8)
        assert result is None

    def test_empty_rows(self):
        assert _find_similar([], "anything", threshold=0.8) is None

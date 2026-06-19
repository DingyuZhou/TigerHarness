"""B2 QA-defense (Sakuragi) — attacking the diary store's assumed-away edges.

Most assertions are hardening regression locks confirming the build is solid;
the multiline-note cases expose a real robustness gap (a one-line bullet format
must FLATTEN a note that contains newlines, or it crashes validate-on-write and
the migration on a multi-paragraph legacy body).
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from tigerharness.tiger_memory import diary_format as df
from tigerharness.tiger_memory import migrate_emotional_to_diary as mig
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.store import Store

NOW = "2026-06-17T00:00:00Z"


def _store(tmp_path: Path):
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(dedent(f"""\
        agent:
          name: Sakuragi
          role: qa
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
            max_length: 4000
            overflow_limit: 6000
            weight_cap: 10
    """))
    cfg = load_config(cfg_path)
    store = Store(cfg.store.root)
    store.init_layout()
    return cfg, store


# ----- the gap: a multiline note must flatten to one bullet line ------------

def test_multiline_note_flattens_and_round_trips():
    """A note containing newlines must serialise to a single bullet line (the
    format is one-line-per-bullet) and round-trip cleanly — not produce a
    malformed store that validate-on-write would refuse."""
    e = df.DiaryEntry(date="2026-06-17", weight=3.0, text="line one\nline two")
    s = df.serialize([e])
    assert df.validate(s) == []                 # FAILS until serialize flattens
    assert df.parse(s)[0].text == "line one line two"


def test_migration_does_not_crash_on_multiline_legacy_body(tmp_path: Path):
    """A multi-paragraph legacy body must migrate (flattened), not crash the
    whole migration with a round-trip ValueError."""
    cfg, store = _store(tmp_path)
    (store.paths.journal / "emotional.md").write_text(dedent("""\
        ---
        id: a
        store: emotional
        created_at: '2026-05-01T00:00:00Z'
        last_used: '2026-05-01T00:00:00Z'
        source: import-legacy
        weight: 6.0
        reaction: proud
        ---
        first paragraph of the reflection.

        second paragraph continues it.
        """))
    res = mig.migrate_store(cfg, store, apply=True)  # must not raise
    assert res.converted == 1 and res.no_loss
    body = (store.paths.journal / "diary.md").read_text()
    assert "first paragraph" in body and "second paragraph" in body


# ----- hardening locks (these confirm the build already holds) --------------

def test_note_that_looks_like_a_day_header_is_a_note():
    e = df.DiaryEntry(date="2026-06-17", weight=2.0, text="## 2026-01-01 not a header")
    assert df.parse(df.serialize([e]))[0].text == "## 2026-01-01 not a header"


def test_note_that_looks_like_a_bullet_round_trips():
    e = df.DiaryEntry(date="2026-06-17", weight=2.0, text="- (+5) looks like a bullet")
    assert df.parse(df.serialize([e]))[0].text == "- (+5) looks like a bullet"


def test_weight_at_exact_cap_round_trips():
    for w in (10, -10):
        e = df.DiaryEntry(date="2026-06-17", weight=w, text="cap")
        assert df.parse(df.serialize([e]), weight_cap=10)[0].weight == w


def test_check_fix_all_bad_leaves_empty_valid_store(tmp_path: Path):
    from tigerharness.tiger_memory import check as chk
    cfg, store = _store(tmp_path)
    (store.paths.journal / "diary.md").write_text("garbage one\ngarbage two\n")
    chk.check_all(cfg, store, fix=True)
    # live store is now valid (empty); both bad lines quarantined, none lost.
    assert chk.check_all(cfg, store).ok
    assert (store.paths.journal / "diary.md").read_text() == ""
    rej = (store.paths.journal / "diary.rejected.md").read_text()
    assert "garbage one" in rej and "garbage two" in rej

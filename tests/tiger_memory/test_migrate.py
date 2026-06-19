"""Tests for the emotional.md -> diary.md migration (plan §C, Mitsui b1-dev-3).

Covers the conversion map, dry-run/apply, snapshot + idempotency, no-entry-loss
accounting, and forget-down-to-bound, to 100% branch coverage.
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from tigerharness.tiger_memory import migrate_emotional_to_diary as mig
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.store import Store

NOW = "2026-06-17T00:00:00Z"


def _store(tmp_path: Path, *, max_length: int = 4000) -> tuple[object, Store]:
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(dedent(f"""\
        agent:
          name: Anzai
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
        memory:
          diary:
            max_length: {max_length}
            overflow_limit: {max_length + 2000}
            weight_cap: 10
    """))
    cfg = load_config(cfg_path)
    store = Store(cfg.store.root)
    store.init_layout()
    return cfg, store


def _legacy_block(id_, weight, reaction, body, *, date="2026-05-01T00:00:00Z") -> str:
    return dedent(f"""\
        ---
        id: {id_}
        store: emotional
        created_at: '{date}'
        last_used: '{date}'
        source: import-legacy
        weight: {weight}
        reaction: {reaction}
        ---
        {body}
        """)


def _write_emotional(store: Store, blocks: list[str]) -> None:
    sep = "\n<!-- tiger-memory-entry -->\n"
    (store.paths.journal / "emotional.md").write_text(sep.join(blocks))


# ----- dry-run / apply ------------------------------------------------------

def test_dry_run_converts_writes_nothing(tmp_path: Path):
    cfg, store = _store(tmp_path)
    _write_emotional(store, [
        _legacy_block("a", 7.0, "pride", "drove the harness to 100%"),
        _legacy_block("b", -5.0, "unsettled", "false confidence bugs me",
                      date="2026-05-02T00:00:00Z"),
    ])
    res = mig.migrate_store(cfg, store)
    assert res.source_blocks == 2 and res.converted == 2 and res.kept == 2
    assert res.forgotten == 0 and res.no_loss and not res.applied
    # nothing written: emotional.md still there, no diary.md.
    assert (store.paths.journal / "emotional.md").exists()
    assert not (store.paths.journal / "diary.md").exists()


def test_apply_writes_diary_backs_up_and_marks(tmp_path: Path):
    cfg, store = _store(tmp_path)
    _write_emotional(store, [_legacy_block("a", 7.0, "pride", "shipped it clean")])
    res = mig.migrate_store(cfg, store, apply=True)
    assert res.applied and res.no_loss
    diary = (store.paths.journal / "diary.md").read_text()
    assert "## 2026-05-01" in diary and "- (+7) shipped it clean" in diary
    assert (store.paths.journal / "emotional.md.bak").exists()
    assert not (store.paths.journal / "emotional.md").exists()
    assert (store.read_state() or {}).get(mig.STATE_KEY)["kept"] == 1


def test_apply_is_idempotent(tmp_path: Path):
    cfg, store = _store(tmp_path)
    _write_emotional(store, [_legacy_block("a", 3.0, "ok", "note")])
    mig.migrate_store(cfg, store, apply=True)
    # a second run is a no-op keyed on the marker (even if a new emotional.md appears).
    _write_emotional(store, [_legacy_block("b", 1.0, "x", "should not migrate")])
    res = mig.migrate_store(cfg, store, apply=True)
    assert res.skipped_reason == "already migrated" and not res.applied


def test_no_emotional_file_is_noop(tmp_path: Path):
    cfg, store = _store(tmp_path)
    res = mig.migrate_store(cfg, store, apply=True)
    assert res.skipped_reason == "no emotional.md" and res.source_blocks == 0


# ----- conversion map edges -------------------------------------------------

def test_empty_body_falls_back_to_reaction(tmp_path: Path):
    cfg, store = _store(tmp_path)
    _write_emotional(store, [_legacy_block("a", 4.0, "glad we shipped", "")])
    res = mig.migrate_store(cfg, store, apply=True)
    assert res.converted == 1
    assert "glad we shipped" in (store.paths.journal / "diary.md").read_text()


def test_skips_bad_weight_date_and_empty(tmp_path: Path):
    cfg, store = _store(tmp_path)
    _write_emotional(store, [
        _legacy_block("good", 2.0, "ok", "keep this one"),
        _legacy_block("badw", "not-a-number", "r", "bad weight"),
        _legacy_block("badd", 1.0, "r", "bad date", date="nope"),
        _legacy_block("empty", 1.0, "", ""),   # empty body + empty reaction
    ])
    res = mig.migrate_store(cfg, store)
    # 4 source blocks, only 1 convertible -> source != converted (logged, not silent).
    assert res.source_blocks == 4 and res.converted == 1
    assert not res.no_loss  # the accounting surfaces the dropped blocks


def test_skips_block_without_frontmatter(tmp_path: Path):
    cfg, store = _store(tmp_path)
    sep = "\n<!-- tiger-memory-entry -->\n"
    (store.paths.journal / "emotional.md").write_text(
        _legacy_block("a", 1.0, "r", "real") + sep + "junk no frontmatter\n"
    )
    res = mig.migrate_store(cfg, store)
    assert res.source_blocks == 1 and res.converted == 1


# ----- forget-down-to-bound -------------------------------------------------

def test_forget_down_keeps_strongest_under_max(tmp_path: Path):
    cfg, store = _store(tmp_path, max_length=120)
    # several notes, total well over 120 chars; strongest |weight| must survive.
    blocks = [
        _legacy_block("w1", 1.0, "r", "weak note number one here padding padding"),
        _legacy_block("w2", 2.0, "r", "weak note number two here padding padding",
                      date="2026-05-02T00:00:00Z"),
        _legacy_block("s", 9.0, "r", "strong feeling survives",
                      date="2026-05-03T00:00:00Z"),
    ]
    _write_emotional(store, blocks)
    res = mig.migrate_store(cfg, store, apply=True)
    assert res.converted == 3 and res.kept < 3 and res.forgotten > 0
    assert res.no_loss  # kept + forgotten == converted == source
    diary = (store.paths.journal / "diary.md").read_text()
    assert "strong feeling survives" in diary  # highest |weight| kept


# ----- CLI ------------------------------------------------------------------

def _cli(tmp_path: Path, *extra: str) -> int:
    from tigerharness.tiger_memory.cli import main
    return main(["--config", str(tmp_path / "cfg.yaml"),
                 "migrate-emotional-to-diary", *extra])


def test_cli_dry_run_then_apply(tmp_path: Path, capsys):
    cfg, store = _store(tmp_path)
    _write_emotional(store, [_legacy_block("a", 5.0, "ok", "did the thing")])
    assert _cli(tmp_path) == 0            # dry-run
    assert not (store.paths.journal / "diary.md").exists()
    assert _cli(tmp_path, "--apply") == 0
    assert (store.paths.journal / "diary.md").exists()


def test_cli_exit_one_on_unbalanced(tmp_path: Path):
    cfg, store = _store(tmp_path)
    _write_emotional(store, [_legacy_block("badw", "xx", "r", "bad weight")])
    assert _cli(tmp_path) == 1  # a source block neither kept nor forgotten


def test_cli_noop_skip_exit_zero(tmp_path: Path):
    cfg, store = _store(tmp_path)
    assert _cli(tmp_path, "--apply") == 0  # no emotional.md -> skip -> 0

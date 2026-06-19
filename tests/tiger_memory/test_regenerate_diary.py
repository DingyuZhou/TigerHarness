"""Tests for diary regeneration from produced text (plan §6 dev-1/Miyagi).

Covers idempotency, malformed-text refusal, the empty/zero-source guard, dry-run
accounting, apply (write+snapshot+mark), the forget-bound pass + itemized
forgotten ledger, cohort labelling, and lock refusal — to 100% branch coverage.
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from tigerharness.tiger_memory import diary_finalize as df
from tigerharness.tiger_memory import regenerate_diary as rg
from tigerharness.tiger_memory.bounded_store import BoundedStore
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.store import Store

VALID = (
    "## 2026-06-17\n"
    "- (+7) shipped the migration clean\n"
    "- (-5) false confidence on near-misses bugs me\n"
    "\n"
    "## 2026-06-18\n"
    "- (+4) reframed the diary store, simpler\n"
)


def _store(tmp_path: Path, *, max_length: int = 4000, emo: bool = True) -> tuple[object, Store]:
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
    if emo:
        (store.paths.journal / "emotional.md").write_text("legacy\n")
    return cfg, store


# ----- guards ---------------------------------------------------------------

def test_already_migrated_is_skipped(tmp_path: Path):
    cfg, store = _store(tmp_path)
    store.write_state({df.STATE_KEY: {"kept": 1}})
    res = rg.regenerate_store(cfg, store, VALID, apply=True)
    assert res.skipped_reason == "already migrated" and not res.applied
    assert not (store.paths.journal / "diary.md").exists()


def test_invalid_text_is_refused(tmp_path: Path):
    cfg, store = _store(tmp_path)
    res = rg.regenerate_store(cfg, store, "this is not a diary\n", apply=True)
    assert res.skipped_reason.startswith("invalid diary text:")
    assert not res.applied and not (store.paths.journal / "diary.md").exists()


def test_empty_source_is_refused(tmp_path: Path):
    cfg, store = _store(tmp_path)
    # blank-only text validates clean but parses to zero bullets -> the
    # non-empty floor / zero-source guard (plan §5): refuse, never write empty.
    res = rg.regenerate_store(cfg, store, "\n\n", apply=True)
    assert res.skipped_reason == "empty source: no diary bullets generated"
    assert res.bullets_generated == 0 and not res.applied
    assert not (store.paths.journal / "diary.md").exists()


# ----- dry-run / apply ------------------------------------------------------

def test_dry_run_computes_accounting_writes_nothing(tmp_path: Path):
    cfg, store = _store(tmp_path)
    res = rg.regenerate_store(cfg, store, VALID)  # apply defaults False
    assert res.bullets_generated == 3 and res.bullets_kept == 3
    assert res.bullets_forgotten == 0 and res.source_days == 2 and res.header_days == 2
    assert res.no_loss and not res.applied
    assert not (store.paths.journal / "diary.md").exists()
    assert (store.paths.journal / "emotional.md").exists()


def test_apply_writes_snapshots_marks(tmp_path: Path):
    cfg, store = _store(tmp_path)
    res = rg.regenerate_store(cfg, store, VALID, apply=True)
    assert res.applied and res.no_loss
    diary = (store.paths.journal / "diary.md").read_text()
    assert "## 2026-06-17" in diary and "- (+7) shipped the migration clean" in diary
    assert (store.paths.journal / "emotional.md.bak").exists()
    assert not (store.paths.journal / "emotional.md").exists()
    marker = (store.read_state() or {})[df.STATE_KEY]
    assert marker["regenerated"] is True and marker["kept"] == 3 and marker["cohort"] == "1"
    assert res.final_chars == len(diary)


def test_apply_without_emotional_file(tmp_path: Path):
    cfg, store = _store(tmp_path, emo=False)  # cohort with no emotional.md
    res = rg.regenerate_store(cfg, store, VALID, apply=True)
    assert res.applied
    assert not (store.paths.journal / "emotional.md.bak").exists()


# ----- forget-bound + itemized ledger --------------------------------------

def test_apply_bounds_and_itemizes_forgotten(tmp_path: Path):
    cfg, store = _store(tmp_path, max_length=70)
    res = rg.regenerate_store(cfg, store, VALID, apply=True)
    assert res.applied and res.no_loss
    assert res.bullets_forgotten > 0 and res.bullets_kept < res.bullets_generated
    # forgotten ledger is itemized (date, weight, text) — never a bare count.
    assert len(res.forgotten_items) == res.bullets_forgotten
    assert all(len(item) == 3 for item in res.forgotten_items)
    # the strongest bullet (+7) survives; a low-weight one is dropped.
    diary = (store.paths.journal / "diary.md").read_text()
    assert "shipped the migration clean" in diary
    assert len(diary) <= 70


def test_cohort_two_label_is_carried(tmp_path: Path):
    cfg, store = _store(tmp_path)
    res = rg.regenerate_store(cfg, store, VALID, cohort="2", apply=True)
    assert res.cohort == "2"
    assert (store.read_state() or {})[df.STATE_KEY]["cohort"] == "2"


# ----- lock refusal ---------------------------------------------------------

def test_apply_refuses_when_locked(tmp_path: Path):
    cfg, store = _store(tmp_path)
    with BoundedStore(cfg, store).store_lock("diary"):
        res = rg.regenerate_store(cfg, store, VALID, apply=True)
    assert res.skipped_reason == df.LOCKED_SKIP and not res.applied
    assert not (store.paths.journal / "diary.md").exists()
    assert (store.paths.journal / "emotional.md").exists()


def test_no_loss_property_direct():
    r = rg.RegenResult("X", bullets_generated=5, bullets_kept=3, bullets_forgotten=2)
    assert r.no_loss
    r2 = rg.RegenResult("Y", bullets_generated=5, bullets_kept=3, bullets_forgotten=1)
    assert not r2.no_loss

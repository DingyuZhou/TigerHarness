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


def _store(tmp_path: Path, *, max_length: int = 4000, emo: bool = True,
           fuzzy_max: int = 4000) -> tuple[object, Store]:
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
          fuzzy:
            max_length: {fuzzy_max}
            overflow_limit: {fuzzy_max + 2000}
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


# ----- fuzzy-seed-from-overflow (4-store migration, Operator option A) ------

def test_apply_seeds_fuzzy_from_overflow(tmp_path: Path):
    from tigerharness.tiger_memory import fuzzy_store
    cfg, store = _store(tmp_path, max_length=70)  # small -> overflow
    res = rg.regenerate_store(cfg, store, VALID, apply=True)
    assert res.applied and res.bullets_forgotten > 0
    fuzzy = fuzzy_store.load_fuzzy(store)
    assert "Coarsened older diary" in fuzzy
    assert res.fuzzy_seeded_chars == len(fuzzy) > 0
    # the dropped bullets are in fuzzy.md -> no hard drop.
    forgotten_texts = [t for (_, _, t) in res.forgotten_items]
    assert any(ft in fuzzy for ft in forgotten_texts)


def test_apply_no_overflow_no_fuzzy_seed(tmp_path: Path):
    from tigerharness.tiger_memory import fuzzy_store
    cfg, store = _store(tmp_path, max_length=4000)  # fits -> nothing forgotten
    res = rg.regenerate_store(cfg, store, VALID, apply=True)
    assert res.applied and res.bullets_forgotten == 0
    assert res.fuzzy_seeded_chars == 0 and fuzzy_store.load_fuzzy(store) == ""


def test_seed_fuzzy_false_skips_seed(tmp_path: Path):
    from tigerharness.tiger_memory import fuzzy_store
    cfg, store = _store(tmp_path, max_length=70)
    res = rg.regenerate_store(cfg, store, VALID, apply=True, seed_fuzzy=False)
    assert res.applied and res.bullets_forgotten > 0
    assert res.fuzzy_seeded_chars == 0 and fuzzy_store.load_fuzzy(store) == ""


def test_seed_appends_to_existing_fuzzy(tmp_path: Path):
    from tigerharness.tiger_memory import fuzzy_store
    cfg, store = _store(tmp_path, max_length=70)
    fuzzy_store.save_fuzzy(cfg, store, "## prior fuzzy\n- earlier gist\n")
    res = rg.regenerate_store(cfg, store, VALID, apply=True)
    fuzzy = fuzzy_store.load_fuzzy(store)
    assert "earlier gist" in fuzzy and "Coarsened older diary" in fuzzy


# ----- P0/I1 + I2: fuzzy seed coarsens (or surfaces the trim), newest-first ---

from tigerharness.tiger_memory.summarizers.base import Summarizer  # noqa: E402

# 3 dated bullets; a small diary bound keeps only the strongest (+9), forgetting
# the two low-weight older ones -> they seed fuzzy.
_OVERFLOW = (
    "## 2026-06-10\n- (+1) oldest alpha note\n"
    "## 2026-06-11\n- (+2) middle bravo note\n"
    "## 2026-06-12\n- (+9) newest charlie note\n"
)


class _GistSummarizer(Summarizer):
    name = "gist"
    version = "v1"

    def summarize(self, *, prompt: str, max_words: int) -> str:
        return "## Fuzzy\n- coarse gist of the older notes\n"


def test_seed_records_trim_when_no_summarizer(tmp_path: Path):
    """P0: with no summarizer + overflow over the fuzzy bound, the residual trim
    is RECORDED (surfaced), never silent."""
    from tigerharness.tiger_memory import fuzzy_store
    cfg, store = _store(tmp_path, max_length=45, fuzzy_max=120)
    res = rg.regenerate_store(cfg, store, _OVERFLOW, apply=True)
    assert res.applied and res.bullets_forgotten == 2
    assert res.fuzzy_trimmed_chars > 0            # surfaced, not silent
    assert len(fuzzy_store.load_fuzzy(store)) <= 120


def test_seed_coarsens_with_summarizer_no_trim(tmp_path: Path):
    """I1: with a summarizer, the overflow is coarsened to fit -> no trim."""
    from tigerharness.tiger_memory import fuzzy_store
    cfg, store = _store(tmp_path, max_length=45, fuzzy_max=120)
    res = rg.regenerate_store(cfg, store, _OVERFLOW, apply=True,
                              summarizer=_GistSummarizer())
    assert res.applied and res.bullets_forgotten == 2
    assert res.fuzzy_trimmed_chars == 0           # coarsened to fit, nothing dropped
    assert "coarse gist" in fuzzy_store.load_fuzzy(store)


def test_seed_newest_first_keeps_recent(tmp_path: Path):
    """I2: when a trim is unavoidable it drops the OLDEST, keeping the recent gist."""
    from tigerharness.tiger_memory import fuzzy_store
    cfg, store = _store(tmp_path, max_length=45, fuzzy_max=120)
    rg.regenerate_store(cfg, store, _OVERFLOW, apply=True)
    fuzzy = fuzzy_store.load_fuzzy(store)
    assert "bravo" in fuzzy        # the newer of the two forgotten survives
    assert "alpha" not in fuzzy    # the oldest is the one trimmed

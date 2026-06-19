"""Tests for the shared diary finalize mechanics (plan §6 dev-1/Miyagi).

Covers forget_to_max (under/over bound, strongest survives) and finalize_diary
(snapshot+write+mark; emotional.md present AND absent; lock refusal) to 100%
branch coverage.
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from tigerharness.tiger_memory import diary_finalize as df
from tigerharness.tiger_memory import diary_format
from tigerharness.tiger_memory.bounded_store import BoundedStore
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.store import Store


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


def _bullets() -> list[diary_format.DiaryEntry]:
    return [
        diary_format.DiaryEntry("2026-06-17", 1.0, "weak note one padded padded padded"),
        diary_format.DiaryEntry("2026-06-18", 2.0, "weak note two padded padded padded"),
        diary_format.DiaryEntry("2026-06-19", 9.0, "strong feeling survives"),
    ]


# ----- forget_to_max --------------------------------------------------------

def test_forget_under_bound_is_unchanged():
    bullets = _bullets()
    kept, dropped = df.forget_to_max(bullets, max_length=4000)
    assert kept == bullets and dropped == 0


def test_forget_over_bound_keeps_strongest():
    bullets = _bullets()
    kept, dropped = df.forget_to_max(bullets, max_length=60)
    assert dropped > 0 and len(kept) < len(bullets)
    # highest |weight| survives the bound.
    assert any(b.text == "strong feeling survives" for b in kept)


# ----- boundary precision (plan §7a, Rukawa b1-dev-2) -----------------------

def test_forget_exactly_at_bound_keeps_all():
    bullets = _bullets()
    exact = len(diary_format.serialize(bullets))
    kept, dropped = df.forget_to_max(bullets, max_length=exact)  # == is <= : keep
    assert dropped == 0 and kept == bullets


def test_forget_one_char_under_bound_drops_lowest():
    bullets = _bullets()
    exact = len(diary_format.serialize(bullets))
    kept, dropped = df.forget_to_max(bullets, max_length=exact - 1)
    assert dropped >= 1
    assert len(diary_format.serialize(kept)) <= exact - 1
    # the weakest (|weight|=1.0) is the first to go; the strongest (9.0) stays.
    assert any(b.weight == 9.0 for b in kept)
    assert all(b.weight != 1.0 for b in kept)


def test_forget_tiebreak_prefers_newer_date():
    # equal |weight|; only one bullet fits -> the newer date wins the tie.
    older = diary_format.DiaryEntry("2026-06-17", 5.0, "padded note alpha bravo charlie")
    newer = diary_format.DiaryEntry("2026-06-18", 5.0, "padded note alpha bravo charlie")
    one = len(diary_format.serialize([newer]))
    kept, dropped = df.forget_to_max([older, newer], max_length=one)
    assert dropped == 1 and len(kept) == 1 and kept[0].date == "2026-06-18"


# ----- finalize_diary -------------------------------------------------------

def test_finalize_with_emotional_present_snapshots_and_writes(tmp_path: Path):
    cfg, store = _store(tmp_path)
    (store.paths.journal / "emotional.md").write_text("legacy\n")
    serialized = diary_format.serialize(_bullets())
    skip = df.finalize_diary(cfg, store, serialized, state_extra={"kept": 3})
    assert skip is None
    assert (store.paths.journal / "diary.md").read_text() == serialized
    assert (store.paths.journal / "emotional.md.bak").exists()
    assert not (store.paths.journal / "emotional.md").exists()
    assert (store.read_state() or {})[df.STATE_KEY] == {"kept": 3}


def test_finalize_with_no_emotional_still_writes(tmp_path: Path):
    cfg, store = _store(tmp_path)  # no emotional.md
    serialized = diary_format.serialize(_bullets())
    skip = df.finalize_diary(cfg, store, serialized, state_extra={"kept": 3})
    assert skip is None
    assert (store.paths.journal / "diary.md").read_text() == serialized
    assert not (store.paths.journal / "emotional.md.bak").exists()


def test_finalize_refuses_when_locked(tmp_path: Path):
    cfg, store = _store(tmp_path)
    (store.paths.journal / "emotional.md").write_text("legacy\n")
    serialized = diary_format.serialize(_bullets())
    with BoundedStore(cfg, store).store_lock("diary"):  # a live session holds it
        skip = df.finalize_diary(cfg, store, serialized, state_extra={"kept": 3})
    assert skip == df.LOCKED_SKIP
    assert not (store.paths.journal / "diary.md").exists()
    assert (store.paths.journal / "emotional.md").exists()  # untouched

"""Tests for the bounded-store substrate (design §4, §5; plan §1+§2 dev-1).

Covers persistence (load/save_atomic + crash-safety), measurement
(length_chars/is_over_overflow hysteresis), the per-store lock, and the
forget-guard (the no-safety-net correctness anchor)."""
from __future__ import annotations

import os
from pathlib import Path
from textwrap import dedent

import pytest

from tigerharness.tiger_memory.bounded_store import (
    BoundedStore,
    ForgetGuardError,
    StoreLockHeld,
    _split_blocks,
)
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory import entries as E
from tigerharness.tiger_memory.entries import (
    DiaryEntry,
    EntryError,
    MustRememberEntry,
    SkillEntry,
)
from tigerharness.tiger_memory.store import Store

NOW = "2026-06-17T00:00:00Z"


@pytest.fixture
def bounded(tmp_path: Path) -> BoundedStore:
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(
        dedent(
            f"""\
            agent:
              name: Mitsui
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
              skills:
                max_count: 3
                overflow_limit: 5
              must_remember:
                max_length: 40
                overflow_limit: 60
              diary:
                max_length: 40
                overflow_limit: 60
                weight_cap: 10
            """
        )
    )
    cfg = load_config(cfg_path)
    store = Store(cfg.store.root)
    store.init_layout()
    return BoundedStore(cfg, store)


def _skill(name: str = "N", text: str = "body text") -> SkillEntry:
    return SkillEntry(
        text=text, created_at=NOW, last_used=NOW, source="extract",
        name=name, trigger=f"when {name}", procedure=f"do {name}",
    )


def _mr(kind: str = "preference", text: str = "memo") -> MustRememberEntry:
    return MustRememberEntry(
        text=text, created_at=NOW, last_used=NOW, source="pin", kind=kind,
    )


def _emo(weight: float = 1.0, text: str = "did x") -> DiaryEntry:
    return DiaryEntry(
        text=text, created_at=NOW, last_used=NOW, source="extract",
        weight=weight,
    )


# ----- load / save roundtrip ----------------------------------------------


def test_load_absent_store_is_empty(bounded: BoundedStore) -> None:
    assert bounded.load("skills") == []


def test_save_load_roundtrip_all_stores(bounded: BoundedStore) -> None:
    s = _skill("Alpha")
    bounded.save_atomic("skills", [s])
    got = bounded.load("skills")
    assert len(got) == 1 and got[0].name == "Alpha" and got[0].id == s.id

    m1, m2 = _mr("owner_explicit", "ship friday"), _mr("preference", "tabs")
    bounded.save_atomic("must_remember", [m1, m2])
    gm = bounded.load("must_remember")
    assert [e.kind for e in gm] == ["owner_explicit", "preference"]
    assert [e.id for e in gm] == [m1.id, m2.id]

    e1 = _emo(-4.0, "annoyed")
    bounded.save_atomic("diary", [e1])
    ge = bounded.load("diary")
    assert ge[0].weight == -4.0


def test_save_empty_then_load(bounded: BoundedStore) -> None:
    bounded.save_atomic("skills", [_skill()])
    bounded.save_atomic("skills", [])  # clear
    assert bounded.load("skills") == []


def test_save_validates_entries(bounded: BoundedStore) -> None:
    bad = _skill()
    bad.name = "  "  # invalid
    with pytest.raises(EntryError, match="name"):
        bounded.save_atomic("skills", [bad])
    # No partial file written.
    assert not (bounded.store.paths.journal / "skills.md").exists()


def test_save_validates_emotional_against_config_cap(
    bounded: BoundedStore,
) -> None:
    # cfg weight_cap is 10; this entry is fine at 10 but a >10 must fail.
    bounded.save_atomic("diary", [_emo(10.0)])
    with pytest.raises(EntryError, match="weight_cap"):
        bounded.save_atomic("diary", [_emo(10.5)])


def test_save_rejects_cross_store_entry(bounded: BoundedStore) -> None:
    with pytest.raises(EntryError, match="belongs to store"):
        bounded.save_atomic("skills", [_mr()])


def test_unknown_store_name_paths_raise(bounded: BoundedStore) -> None:
    with pytest.raises(EntryError, match="unknown store_name"):
        bounded.load("nope")
    with pytest.raises(EntryError, match="unknown store_name"):
        bounded.save_atomic("nope", [])


def test_load_skips_unparseable_blocks(bounded: BoundedStore) -> None:
    """A block with no frontmatter is skipped (lenient read)."""
    path = bounded.store.paths.journal / "skills.md"
    good = _skill("Good")
    bounded.save_atomic("skills", [good])
    # Append a junk block with no frontmatter.
    text = path.read_text()
    path.write_text(text + "\n<!-- tiger-memory-entry -->\njunk no frontmatter\n")
    got = bounded.load("skills")
    assert len(got) == 1 and got[0].name == "Good"


# ----- crash-safety (atomic write) ----------------------------------------


def test_atomic_write_no_tmp_leftover(bounded: BoundedStore) -> None:
    bounded.save_atomic("skills", [_skill()])
    path = bounded.store.paths.journal / "skills.md"
    assert path.exists()
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_save_overwrites_atomically(bounded: BoundedStore) -> None:
    bounded.save_atomic("skills", [_skill("First")])
    bounded.save_atomic("skills", [_skill("Second")])
    got = bounded.load("skills")
    assert len(got) == 1 and got[0].name == "Second"


# ----- measurement ---------------------------------------------------------


def test_length_chars_counts_characters(bounded: BoundedStore) -> None:
    e = _emo(1.0)  # text "did x" (5); reaction dropped, not counted
    assert bounded.length_chars([e]) == len("did x")


def test_length_chars_skill_counts_prose_fields(bounded: BoundedStore) -> None:
    s = _skill("Z", text="body")
    expected = (
        len("body") + len("Z") + len("when Z") + len("do Z")
    )
    assert bounded.length_chars([s]) == expected


def test_count(bounded: BoundedStore) -> None:
    assert bounded.count([_skill("a"), _skill("b")]) == 2


def test_is_over_overflow_skills_count_based(bounded: BoundedStore) -> None:
    # overflow_limit = 5
    skills = [_skill(f"s{i}") for i in range(4)]
    assert bounded.is_over_overflow("skills", skills) is False
    skills.append(_skill("s4"))  # now 5 == overflow_limit -> True
    assert bounded.is_over_overflow("skills", skills) is True


def test_is_over_overflow_length_based(bounded: BoundedStore) -> None:
    # must_remember overflow_limit = 60 chars.
    small = [_mr("preference", "x" * 10)]
    assert bounded.is_over_overflow("must_remember", small) is False
    big = [_mr("preference", "x" * 60)]
    assert bounded.is_over_overflow("must_remember", big) is True


def test_is_over_overflow_emotional(bounded: BoundedStore) -> None:
    # emotional overflow_limit = 60; weight is config-capped at 10.
    big = [_emo(1.0, "y" * 60)]
    assert bounded.is_over_overflow("diary", big) is True


def test_hysteresis_band_does_not_overflow(bounded: BoundedStore) -> None:
    """In the max<=n<overflow band, is_over_overflow stays False (no thrash)."""
    # must_remember: max_length=40, overflow_limit=60. 50 chars is in-band.
    mid = [_mr("preference", "x" * 50)]
    assert bounded.length_chars(mid) >= bounded.max_bound("must_remember")
    assert bounded.is_over_overflow("must_remember", mid) is False


def test_max_bound(bounded: BoundedStore) -> None:
    assert bounded.max_bound("skills") == 3
    assert bounded.max_bound("must_remember") == 40
    assert bounded.max_bound("diary") == 40


# ----- store_lock ----------------------------------------------------------


def test_store_lock_acquire_release(bounded: BoundedStore) -> None:
    lock_file = bounded.store.paths.journal / ".skills.lock"
    with bounded.store_lock("skills"):
        assert lock_file.exists()
    assert not lock_file.exists()


def test_store_lock_blocks_second_live_holder(bounded: BoundedStore) -> None:
    lock_file = bounded.store.paths.journal / ".skills.lock"
    # Simulate a live holder (our own PID).
    lock_file.write_text(f"{os.getpid()} 0")
    with pytest.raises(StoreLockHeld, match="locked by another live session"):
        with bounded.store_lock("skills"):
            pass
    # The foreign lock is untouched (we never owned it).
    assert lock_file.exists()
    lock_file.unlink()


def test_store_lock_reclaims_dead_holder(bounded: BoundedStore) -> None:
    lock_file = bounded.store.paths.journal / ".skills.lock"
    lock_file.write_text("999999 0")  # PID almost certainly dead
    with bounded.store_lock("skills"):
        assert lock_file.read_text().split()[0] == str(os.getpid())
    assert not lock_file.exists()


def test_store_lock_reclaims_garbage_holder(bounded: BoundedStore) -> None:
    lock_file = bounded.store.paths.journal / ".skills.lock"
    lock_file.write_text("not-a-pid")  # unparseable -> treated as reclaimable
    with bounded.store_lock("skills"):
        assert lock_file.exists()
    assert not lock_file.exists()


def test_store_lock_unknown_store(bounded: BoundedStore) -> None:
    with pytest.raises(EntryError, match="unknown store_name"):
        with bounded.store_lock("nope"):
            pass


def test_store_lock_reraises_non_eexist_oserror(
    bounded: BoundedStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-EEXIST OSError on lock create propagates (e.g. permission denied)."""
    import errno as _errno

    from tigerharness.tiger_memory import bounded_store as bs_mod

    def boom(*a, **k):
        raise OSError(_errno.EACCES, "denied")

    monkeypatch.setattr(bs_mod.os, "open", boom)
    with pytest.raises(OSError, match="denied"):
        with bounded.store_lock("skills"):
            pass


def test_store_lock_independent_per_store(bounded: BoundedStore) -> None:
    """Locking skills does not block must_remember (per-store granularity)."""
    with bounded.store_lock("skills"):
        with bounded.store_lock("must_remember"):
            assert (bounded.store.paths.journal / ".skills.lock").exists()
            assert (
                bounded.store.paths.journal / ".must_remember.lock"
            ).exists()


# ----- forget-guard (the no-safety-net anchor) -----------------------------


def test_forget_drops_normal_entries(bounded: BoundedStore) -> None:
    a, b = _mr("preference", "a"), _mr("decision", "b")
    survivors = bounded.forget("must_remember", [a, b], [a.id])
    assert [e.id for e in survivors] == [b.id]


def test_forget_guard_blocks_unchecked_owner_directive(
    bounded: BoundedStore,
) -> None:
    owner = _mr("owner_explicit", "ship friday")
    with pytest.raises(ForgetGuardError, match="relevance-check"):
        bounded.forget("must_remember", [owner], [owner.id])


def test_forget_allows_owner_directive_after_relevance_check(
    bounded: BoundedStore,
) -> None:
    owner = _mr("owner_explicit", "ship friday")
    survivors = bounded.forget(
        "must_remember", [owner], [owner.id],
        relevance_checked_ids=[owner.id],
    )
    assert survivors == []


def test_forget_ignores_unknown_drop_ids(bounded: BoundedStore) -> None:
    a = _mr("preference", "a")
    survivors = bounded.forget("must_remember", [a], ["does-not-exist"])
    assert [e.id for e in survivors] == [a.id]


def test_forget_guard_only_applies_to_must_remember(
    bounded: BoundedStore,
) -> None:
    """A skill with an id is freely forgettable — the guard is must_remember-only."""
    s = _skill("Sk")
    assert bounded.forget("skills", [s], [s.id]) == []


def test_forget_preserves_owner_when_other_dropped(
    bounded: BoundedStore,
) -> None:
    owner = _mr("owner_explicit", "keep me")
    pref = _mr("preference", "drop me")
    survivors = bounded.forget("must_remember", [owner, pref], [pref.id])
    assert [e.id for e in survivors] == [owner.id]


# ----- serialization internals --------------------------------------------


def test_split_blocks_empty() -> None:
    assert _split_blocks("") == []
    assert _split_blocks("   \n  ") == []

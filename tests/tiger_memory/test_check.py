"""Tests for the format-check + repair verb (check.py, plan §2 dev-3, Mitsui).

Covers validate / --fix / quarantine across all three stores + the CLI exit
codes, to 100% branch coverage. Pure-Python, no model calls.
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from tigerharness.tiger_memory import check as chk
from tigerharness.tiger_memory.bounded_store import BoundedStore
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.entries import DiaryEntry, MustRememberEntry, SkillEntry
from tigerharness.tiger_memory.store import Store

NOW = "2026-06-17T00:00:00Z"


@pytest.fixture
def bstore(tmp_path: Path) -> BoundedStore:
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(dedent(f"""\
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
          diary:
            max_length: 4000
            overflow_limit: 6000
            weight_cap: 10
    """))
    cfg = load_config(cfg_path)
    store = Store(cfg.store.root)
    store.init_layout()
    return BoundedStore(cfg, store)


def _diary_path(bstore: BoundedStore) -> Path:
    return bstore.store.paths.journal / "diary.md"


def _skill(name="S") -> SkillEntry:
    return SkillEntry(text="b", created_at=NOW, last_used=NOW, source="x",
                      name=name, trigger="t", procedure="p")


# ----- clean / absent -------------------------------------------------------

def test_check_absent_stores_ok(bstore: BoundedStore):
    report = chk.check_all(bstore.cfg, bstore.store)
    assert report.ok and all(s.valid == 0 for s in report.stores)


def test_check_clean_stores_ok(bstore: BoundedStore):
    bstore.save_atomic("skills", [_skill()])
    bstore.save_atomic("diary", [DiaryEntry(text="note", created_at=NOW,
                                            last_used=NOW, source="x", weight=3.0)])
    report = chk.check_all(bstore.cfg, bstore.store)
    assert report.ok
    diary = next(s for s in report.stores if s.store_name == "diary")
    assert diary.valid == 1 and diary.problems == []


# ----- diary: mechanical + quarantine --------------------------------------

def test_check_diary_noncanonical_is_mechanical(bstore: BoundedStore):
    # days descending = parseable but non-canonical (serialize sorts ascending).
    _diary_path(bstore).write_text("## 2026-06-18\n- (+1) b\n\n## 2026-06-17\n- (+1) a\n")
    report = chk.check_all(bstore.cfg, bstore.store)
    diary = next(s for s in report.stores if s.store_name == "diary")
    assert not diary.ok and "canonical" in diary.problems[0]
    # --fix rewrites canonically (ascending), nothing quarantined.
    chk.check_all(bstore.cfg, bstore.store, fix=True)
    assert chk.check_all(bstore.cfg, bstore.store).ok
    assert _diary_path(bstore).read_text().startswith("## 2026-06-17")


def test_check_diary_quarantines_unparseable(bstore: BoundedStore):
    _diary_path(bstore).write_text("## 2026-06-17\n- (+2) keep me\nGARBAGE LINE\n")
    report = chk.check_all(bstore.cfg, bstore.store, fix=True)
    diary = next(s for s in report.stores if s.store_name == "diary")
    assert diary.quarantined == 1 and diary.repaired
    # good bullet kept, garbage moved to the sidecar, live store now valid.
    assert "keep me" in _diary_path(bstore).read_text()
    assert "GARBAGE LINE" in (bstore.store.paths.journal / "diary.rejected.md").read_text()
    assert chk.check_all(bstore.cfg, bstore.store).ok


def test_check_quarantine_appends_to_existing_sidecar(bstore: BoundedStore):
    sidecar = bstore.store.paths.journal / "diary.rejected.md"
    sidecar.write_text("earlier reject\n")
    _diary_path(bstore).write_text("## 2026-06-17\n- (+1) ok\nGARBAGE\n")
    chk.check_all(bstore.cfg, bstore.store, fix=True)
    body = sidecar.read_text()
    assert "earlier reject" in body and "GARBAGE" in body


# ----- frontmatter stores ---------------------------------------------------

def test_check_frontmatter_invalid_entry(bstore: BoundedStore):
    # write a skills file with one good + one schema-invalid (blank name) block.
    bstore.save_atomic("skills", [_skill("Good")])
    path = bstore.store.paths.journal / "skills.md"
    bad = dedent("""\

        <!-- tiger-memory-entry -->
        ---
        id: bad1
        store: skills
        created_at: 2026-06-17T00:00:00Z
        last_used: 2026-06-17T00:00:00Z
        source: x
        name: "  "
        trigger: t
        procedure: p
        usage_count: 0
        importance: 0.0
        ---
        body
        """)
    path.write_text(path.read_text() + bad)
    report = chk.check_all(bstore.cfg, bstore.store)
    skills = next(s for s in report.stores if s.store_name == "skills")
    assert not skills.ok and skills.valid == 1
    # --fix quarantines the bad block, keeps the good one.
    chk.check_all(bstore.cfg, bstore.store, fix=True)
    assert chk.check_all(bstore.cfg, bstore.store).ok
    assert (bstore.store.paths.journal / "skills.rejected.md").exists()


def test_check_frontmatter_unparseable_block(bstore: BoundedStore):
    bstore.save_atomic("must_remember",
                       [MustRememberEntry(text="m", created_at=NOW, last_used=NOW,
                                          source="pin", kind="preference")])
    path = bstore.store.paths.journal / "must_remember.md"
    path.write_text(path.read_text() + "\n<!-- tiger-memory-entry -->\nno frontmatter here\n")
    report = chk.check_all(bstore.cfg, bstore.store, fix=True)
    mr = next(s for s in report.stores if s.store_name == "must_remember")
    assert mr.quarantined == 1 and mr.repaired


def test_check_frontmatter_noncanonical_mechanical(bstore: BoundedStore):
    bstore.save_atomic("skills", [_skill("A")])
    path = bstore.store.paths.journal / "skills.md"
    path.write_text(path.read_text() + "\n\n")  # trailing whitespace = non-canonical
    report = chk.check_all(bstore.cfg, bstore.store)
    skills = next(s for s in report.stores if s.store_name == "skills")
    assert not skills.ok and "canonical" in skills.problems[0]
    chk.check_all(bstore.cfg, bstore.store, fix=True)
    assert chk.check_all(bstore.cfg, bstore.store).ok


# ----- CLI exit codes -------------------------------------------------------

def _cli(tmp_path: Path, *extra: str) -> int:
    from tigerharness.tiger_memory.cli import main
    return main(["--config", str(tmp_path / "cfg.yaml"), "check", *extra])


def test_cli_check_clean_exit_zero(bstore: BoundedStore, tmp_path: Path, capsys):
    bstore.save_atomic("skills", [_skill()])
    assert _cli(tmp_path) == 0
    assert '"ok": true' in capsys.readouterr().out


def test_cli_check_dirty_exit_one(bstore: BoundedStore, tmp_path: Path):
    _diary_path(bstore).write_text("GARBAGE\n")
    assert _cli(tmp_path) == 1


def test_cli_check_fix_exit_zero(bstore: BoundedStore, tmp_path: Path):
    _diary_path(bstore).write_text("GARBAGE\n")
    assert _cli(tmp_path, "--fix") == 0
    # after --fix the store is clean.
    assert _cli(tmp_path) == 0


def test_check_frontmatter_blank_trailing_block_skipped(bstore: BoundedStore):
    """A trailing empty block (no frontmatter, blank) is skipped silently — it
    is not a 'problem', just non-canonical whitespace --fix tidies away."""
    bstore.save_atomic("skills", [_skill("A")])
    path = bstore.store.paths.journal / "skills.md"
    path.write_text(path.read_text() + "\n<!-- tiger-memory-entry -->\n\n")
    report = chk.check_all(bstore.cfg, bstore.store)
    skills = next(s for s in report.stores if s.store_name == "skills")
    # the blank block raised no per-block problem; only the canonical mismatch.
    assert skills.problems == ["skills not in canonical format"]
    chk.check_all(bstore.cfg, bstore.store, fix=True)
    assert chk.check_all(bstore.cfg, bstore.store).ok


# ----- fuzzy store (4-store model, b1-dev-3) --------------------------------

def test_check_fuzzy_empty_ok(bstore: "BoundedStore") -> None:
    r = chk.check_store(bstore, "fuzzy", fix=False)
    assert r.ok and r.valid == 0


def test_check_fuzzy_under_bound_ok(bstore: "BoundedStore") -> None:
    from tigerharness.tiger_memory import fuzzy_store
    fuzzy_store.save_fuzzy(bstore.cfg, bstore.store, "## Fuzzy\n- gist\n")
    r = chk.check_store(bstore, "fuzzy", fix=False)
    assert r.ok and r.valid == 1


def test_check_fuzzy_over_overflow_flagged_and_fixed(bstore: "BoundedStore") -> None:
    from tigerharness.tiger_memory import fuzzy_store
    bstore.store.paths.journal.mkdir(parents=True, exist_ok=True)
    fuzzy_store.fuzzy_path(bstore.store).write_text("x" * 6001)  # >= overflow 6000
    r = chk.check_store(bstore, "fuzzy", fix=False)
    assert not r.ok and "overflow_limit" in r.problems[0]
    r2 = chk.check_store(bstore, "fuzzy", fix=True)
    assert r2.repaired
    assert len(fuzzy_store.load_fuzzy(bstore.store)) <= bstore.memory.fuzzy.max_length


def test_check_all_includes_fuzzy(bstore: "BoundedStore") -> None:
    rep = chk.check_all(bstore.cfg, bstore.store)
    assert len(rep.stores) == 4
    assert any(s.store_name == "fuzzy" for s in rep.stores)

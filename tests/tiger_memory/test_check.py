"""Tests for the format-check + repair verb (check.py, ADR 0007).

Covers validate / --fix / quarantine across the three frontmatter stores
(skills / must_remember / topics) + the CLI exit codes. Pure-Python, no
model calls.
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from tigerharness.tiger_memory import check as chk
from tigerharness.tiger_memory.bounded_store import BoundedStore
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.entries import (
    STORE_NAMES,
    MustRememberEntry,
    SkillEntry,
    TopicEntry,
)
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
    """))
    cfg = load_config(cfg_path)
    store = Store(cfg.store.root)
    store.init_layout()
    return BoundedStore(cfg, store)


def _path(bstore: BoundedStore, store_name: str) -> Path:
    return bstore.store.paths.journal / f"{store_name}.md"


def _skill(name="S") -> SkillEntry:
    return SkillEntry(text="b", created_at=NOW, last_used=NOW, source="x",
                      name=name, trigger="t", procedure="p")


def _must(text="m") -> MustRememberEntry:
    return MustRememberEntry(text=text, created_at=NOW, last_used=NOW,
                             source="pin", kind="preference")


def _topic(name="Topic One") -> TopicEntry:
    return TopicEntry(text="## 2026-06-17\n- fact", created_at=NOW,
                      last_used=NOW, source="x", name=name,
                      summary="what this topic is")


def _by_name(report: chk.CheckReport, store_name: str) -> chk.StoreCheck:
    return next(s for s in report.stores if s.store_name == store_name)


# ----- clean / absent -------------------------------------------------------

def test_check_absent_stores_ok(bstore: BoundedStore):
    report = chk.check_all(bstore.cfg, bstore.store)
    assert report.ok and all(s.valid == 0 for s in report.stores)
    # Exactly the three frontmatter stores, in STORE_NAMES order.
    assert [s.store_name for s in report.stores] == list(STORE_NAMES)
    assert "diary" not in {s.store_name for s in report.stores}
    assert "fuzzy" not in {s.store_name for s in report.stores}


def test_check_clean_stores_ok(bstore: BoundedStore):
    bstore.save_atomic("skills", [_skill()])
    bstore.save_atomic("must_remember", [_must()])
    bstore.save_atomic("topics", [_topic()])
    report = chk.check_all(bstore.cfg, bstore.store)
    assert report.ok
    for name in STORE_NAMES:
        s = _by_name(report, name)
        assert s.valid == 1 and s.problems == [] and s.quarantined == 0


# ----- schema-invalid entries → quarantine ----------------------------------

def test_check_skills_invalid_entry(bstore: BoundedStore):
    # write a skills file with one good + one schema-invalid (blank name) block.
    bstore.save_atomic("skills", [_skill("Good")])
    path = _path(bstore, "skills")
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
    skills = _by_name(report, "skills")
    assert not skills.ok and skills.valid == 1
    assert "invalid skills entry id='bad1'" in skills.problems[0]
    # --fix quarantines the bad block, keeps the good one.
    chk.check_all(bstore.cfg, bstore.store, fix=True)
    assert chk.check_all(bstore.cfg, bstore.store).ok
    assert "Good" in path.read_text()
    assert "bad1" in (bstore.store.paths.journal / "skills.rejected.md").read_text()


def test_check_topics_invalid_entry(bstore: BoundedStore):
    # A topic block whose slug is not canonical (uppercase + space) is
    # schema-invalid: quarantined under --fix, the good topic kept.
    bstore.save_atomic("topics", [_topic("Keeper")])
    path = _path(bstore, "topics")
    bad = dedent("""\

        <!-- tiger-memory-entry -->
        ---
        id: badt
        store: topics
        created_at: 2026-06-17T00:00:00Z
        last_used: 2026-06-17T00:00:00Z
        source: x
        name: Bad Topic
        slug: "Bad Slug"
        summary: s
        touch_count: 1
        ---
        body
        """)
    path.write_text(path.read_text() + bad)
    report = chk.check_all(bstore.cfg, bstore.store)
    topics = _by_name(report, "topics")
    assert not topics.ok and topics.valid == 1
    assert "invalid topics entry id='badt'" in topics.problems[0]
    fixed = chk.check_all(bstore.cfg, bstore.store, fix=True)
    assert _by_name(fixed, "topics").quarantined == 1
    assert chk.check_all(bstore.cfg, bstore.store).ok
    assert "Keeper" in path.read_text()
    sidecar = bstore.store.paths.journal / "topics.rejected.md"
    assert "Bad Slug" in sidecar.read_text()


def test_check_frontmatter_unparseable_block(bstore: BoundedStore):
    bstore.save_atomic("must_remember", [_must()])
    path = _path(bstore, "must_remember")
    path.write_text(path.read_text() + "\n<!-- tiger-memory-entry -->\nno frontmatter here\n")
    report = chk.check_all(bstore.cfg, bstore.store)
    mr = _by_name(report, "must_remember")
    assert "unparseable block (no frontmatter)" in mr.problems
    fixed = chk.check_all(bstore.cfg, bstore.store, fix=True)
    mr = _by_name(fixed, "must_remember")
    assert mr.quarantined == 1 and mr.repaired
    assert "no frontmatter here" in (
        bstore.store.paths.journal / "must_remember.rejected.md"
    ).read_text()
    assert chk.check_all(bstore.cfg, bstore.store).ok


def test_check_quarantine_appends_to_existing_sidecar(bstore: BoundedStore):
    sidecar = bstore.store.paths.journal / "topics.rejected.md"
    sidecar.write_text("earlier reject\n")
    _path(bstore, "topics").write_text("GARBAGE LINE\n")
    chk.check_all(bstore.cfg, bstore.store, fix=True)
    body = sidecar.read_text()
    assert "earlier reject" in body and "GARBAGE LINE" in body


# ----- mechanical drift → canonical rewrite, no quarantine -------------------

def test_check_noncanonical_mechanical(bstore: BoundedStore):
    bstore.save_atomic("topics", [_topic("A")])
    path = _path(bstore, "topics")
    path.write_text(path.read_text() + "\n\n")  # trailing whitespace = non-canonical
    report = chk.check_all(bstore.cfg, bstore.store)
    topics = _by_name(report, "topics")
    assert not topics.ok and topics.problems == ["topics not in canonical format"]
    fixed = chk.check_all(bstore.cfg, bstore.store, fix=True)
    topics = _by_name(fixed, "topics")
    # mechanical only: repaired in place, nothing quarantined.
    assert topics.repaired and topics.quarantined == 0
    assert not (bstore.store.paths.journal / "topics.rejected.md").exists()
    assert chk.check_all(bstore.cfg, bstore.store).ok


def test_check_frontmatter_blank_trailing_block_skipped(bstore: BoundedStore):
    """A trailing empty block (no frontmatter, blank) is skipped silently — it
    is not a 'problem', just non-canonical whitespace --fix tidies away."""
    bstore.save_atomic("skills", [_skill("A")])
    path = _path(bstore, "skills")
    path.write_text(path.read_text() + "\n<!-- tiger-memory-entry -->\n\n")
    report = chk.check_all(bstore.cfg, bstore.store)
    skills = _by_name(report, "skills")
    # the blank block raised no per-block problem; only the canonical mismatch.
    assert skills.problems == ["skills not in canonical format"]
    chk.check_all(bstore.cfg, bstore.store, fix=True)
    assert chk.check_all(bstore.cfg, bstore.store).ok


def test_rejected_path_naming():
    assert chk._rejected_path(Path("/j/topics.md")) == Path("/j/topics.rejected.md")


# ----- CLI exit codes -------------------------------------------------------

def _cli(tmp_path: Path, *extra: str) -> int:
    from tigerharness.tiger_memory.cli import main
    return main(["--config", str(tmp_path / "cfg.yaml"), "check", *extra])


def test_cli_check_clean_exit_zero(bstore: BoundedStore, tmp_path: Path, capsys):
    bstore.save_atomic("skills", [_skill()])
    assert _cli(tmp_path) == 0
    assert '"ok": true' in capsys.readouterr().out


def test_cli_check_dirty_exit_one(bstore: BoundedStore, tmp_path: Path):
    _path(bstore, "topics").write_text("GARBAGE\n")
    assert _cli(tmp_path) == 1


def test_cli_check_fix_exit_zero(bstore: BoundedStore, tmp_path: Path):
    _path(bstore, "topics").write_text("GARBAGE\n")
    assert _cli(tmp_path, "--fix") == 0
    # after --fix the store is clean.
    assert _cli(tmp_path) == 0

"""Tests for the session-start briefing assembly (briefing.py, ADR 0007).

New layout: README / UNPROCESSED / must_remember.md / skill_index.md /
topic_index.md / MANIFEST.md / .fingerprint plus the skills/ and topics/
detail subdirs (one file per entry). Fingerprint no-op shortcut is over the
three store files; the rebuild is an atomic folder swap.
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from tigerharness.tiger_memory import briefing as bf
from tigerharness.tiger_memory import indexes
from tigerharness.tiger_memory.bounded_store import BoundedStore
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.entries import (
    STORE_MUST_REMEMBER,
    STORE_SKILLS,
    STORE_TOPICS,
    MustRememberEntry,
    SkillEntry,
    TopicEntry,
)
from tigerharness.tiger_memory.store import Store

NOW = "2026-06-17T00:00:00Z"
OLD = "2026-06-01T00:00:00Z"

ALL_FILES = (
    bf.README_NAME, bf.NOTICE_NAME, bf.MUST_REMEMBER_NAME,
    bf.SKILL_INDEX_NAME, bf.TOPIC_INDEX_NAME, bf.MANIFEST_NAME,
    bf.FINGERPRINT_NAME,
)


def _cfg(tmp_path: Path, extra: str = "") -> object:
    p = tmp_path / "cfg.yaml"
    p.write_text(dedent(f"""\
        agent:
          name: Sakuragi
          role: t
        store:
          root: {tmp_path}/memory
        sources:
          - kind: claude_code
            project_path: {tmp_path}/proj/
        summarizer:
          backend: anthropic
          model: m
          prompts: default/v1
    """) + extra)
    return load_config(p)


def _seed(cfg, store, *, skills=(), must=(), topics=()) -> None:
    bstore = BoundedStore(cfg, store)
    store.init_layout()
    if skills:
        bstore.save_atomic(STORE_SKILLS, list(skills))
    if must:
        bstore.save_atomic(STORE_MUST_REMEMBER, list(must))
    if topics:
        bstore.save_atomic(STORE_TOPICS, list(topics))


def _skill(name, imp=1.0, usage=0) -> SkillEntry:
    return SkillEntry(
        text=f"procedure for {name}\nmore detail", created_at=NOW, last_used=NOW,
        source="x", name=name, trigger=f"when {name}", procedure=f"do {name}",
        usage_count=usage, importance=imp,
    )


def _must(text, kind="preference", imp=1.0) -> MustRememberEntry:
    return MustRememberEntry(text=text, created_at=NOW, last_used=NOW,
                             source="x", kind=kind, importance=imp)


def _topic(name, *, last_used=NOW, touches=1, summary=None) -> TopicEntry:
    return TopicEntry(
        text=f"## 2026-06-17\n- detail about {name}", created_at=OLD,
        last_used=last_used, source="x", name=name,
        summary=summary or f"summary of {name}", touch_count=touches,
    )


# ----- full rebuild ---------------------------------------------------------


def test_rebuild_assembles_all_files(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    _seed(
        cfg, store,
        skills=[_skill("A", imp=2.0), _skill("B", imp=5.0)],
        must=[_must("never push", "operator_explicit", imp=5.0), _must("use uv")],
        topics=[_topic("Old Topic", last_used=OLD),
                _topic("Fresh Topic", last_used=NOW, touches=3)],
    )
    bf.rebuild_briefing(cfg, store)
    b = store.paths.briefing
    for name in ALL_FILES:
        assert (b / name).exists(), name
    assert (b / indexes.SKILLS_DETAIL_DIR).is_dir()
    assert (b / indexes.TOPICS_DETAIL_DIR).is_dir()
    # README is persona-substituted.
    assert "Sakuragi" in (b / bf.README_NAME).read_text()
    # Skill index ordered most-important first (B imp=5 before A imp=2).
    idx = (b / bf.SKILL_INDEX_NAME).read_text()
    assert idx.index("**B**") < idx.index("**A**")
    # Topic index ordered freshest first.
    tdx = (b / bf.TOPIC_INDEX_NAME).read_text()
    assert tdx.index("**Fresh Topic**") < tdx.index("**Old Topic**")
    # must_remember shows kind + text.
    mr = (b / bf.MUST_REMEMBER_NAME).read_text()
    assert "operator_explicit" in mr and "never push" in mr
    # The unprocessed notice names the persona.
    assert "Sakuragi" in (b / bf.NOTICE_NAME).read_text()


def test_rebuild_writes_detail_files(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    skill = _skill("Git Rebase Trick", imp=1.0, usage=2)
    topic = _topic("Sweep Protocol")
    _seed(cfg, store, skills=[skill], topics=[topic])
    bf.rebuild_briefing(cfg, store)
    b = store.paths.briefing
    # One skill detail file, named slug-<id>.md, containing the procedure.
    sfile = b / indexes.SKILLS_DETAIL_DIR / f"git-rebase-trick-{skill.id}.md"
    assert sfile.exists()
    assert [p.name for p in (b / indexes.SKILLS_DETAIL_DIR).iterdir()] == [sfile.name]
    assert "do Git Rebase Trick" in sfile.read_text()
    # One topic detail file, named <slug>.md, containing the dated body.
    tfile = b / indexes.TOPICS_DETAIL_DIR / "sweep-protocol.md"
    assert tfile.exists()
    assert [p.name for p in (b / indexes.TOPICS_DETAIL_DIR).iterdir()] == [tfile.name]
    body = tfile.read_text()
    assert "## 2026-06-17" in body and "detail about Sweep Protocol" in body
    # The index lines point at exactly these filenames.
    assert sfile.name in (b / bf.SKILL_INDEX_NAME).read_text()
    assert "`sweep-protocol`" in (b / bf.TOPIC_INDEX_NAME).read_text()


def test_rebuild_empty_stores(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    bf.rebuild_briefing(cfg, store)
    b = store.paths.briefing
    for name in ALL_FILES:
        assert (b / name).exists(), name
    assert "_(empty)_" in (b / bf.MUST_REMEMBER_NAME).read_text()
    assert "no skills" in (b / bf.SKILL_INDEX_NAME).read_text()
    assert "no topics" in (b / bf.TOPIC_INDEX_NAME).read_text()
    # Detail dirs exist but are empty.
    assert list((b / indexes.SKILLS_DETAIL_DIR).iterdir()) == []
    assert list((b / indexes.TOPICS_DETAIL_DIR).iterdir()) == []
    m = (b / bf.MANIFEST_NAME).read_text()
    assert "must_remember: 0 entries" in m
    assert "skills: 0 indexed" in m and "topics: 0 indexed" in m


# ----- fingerprint no-op shortcut -------------------------------------------


def test_rebuild_noop_when_unchanged(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    _seed(cfg, store, must=[_must("x")])
    bf.rebuild_briefing(cfg, store)
    fp1 = (store.paths.briefing / bf.FINGERPRINT_NAME).read_text()
    manifest1 = (store.paths.briefing / bf.MANIFEST_NAME).stat().st_mtime_ns
    bf.rebuild_briefing(cfg, store)  # no store change → no-op
    assert (store.paths.briefing / bf.FINGERPRINT_NAME).read_text() == fp1
    assert (store.paths.briefing / bf.MANIFEST_NAME).stat().st_mtime_ns == manifest1


def test_rebuild_noop_ignores_non_store_journal_files(tmp_path: Path) -> None:
    """Only skills.md / must_remember.md / topics.md drive the fingerprint."""
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    _seed(cfg, store, must=[_must("x")])
    bf.rebuild_briefing(cfg, store)
    manifest1 = (store.paths.briefing / bf.MANIFEST_NAME).stat().st_mtime_ns
    store.write_state({"unrelated": True})  # journal/.state.json changes
    (store.paths.journal / "notes.md").write_text("not a store file")
    bf.rebuild_briefing(cfg, store)
    assert (store.paths.briefing / bf.MANIFEST_NAME).stat().st_mtime_ns == manifest1


def test_rebuild_refreshes_on_change(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    _seed(cfg, store, must=[_must("first")])
    bf.rebuild_briefing(cfg, store)
    # Mutate a store file → fingerprint changes → rebuild runs.
    bstore = BoundedStore(cfg, store)
    bstore.save_atomic(STORE_MUST_REMEMBER, [_must("first"), _must("second")])
    bf.rebuild_briefing(cfg, store)
    assert "second" in (store.paths.briefing / bf.MUST_REMEMBER_NAME).read_text()


def test_rebuild_refreshes_on_topics_change(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    _seed(cfg, store, topics=[_topic("Alpha")])
    bf.rebuild_briefing(cfg, store)
    bstore = BoundedStore(cfg, store)
    bstore.save_atomic(STORE_TOPICS, [_topic("Alpha"), _topic("Beta")])
    bf.rebuild_briefing(cfg, store)
    b = store.paths.briefing
    assert "**Beta**" in (b / bf.TOPIC_INDEX_NAME).read_text()
    assert (b / indexes.TOPICS_DETAIL_DIR / "beta.md").exists()


def test_compute_fingerprint_absent_files(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    fp = bf._compute_fingerprint(store)
    assert "skills.md:0:0" in fp
    assert "must_remember.md:0:0" in fp
    assert "topics.md:0:0" in fp


def test_briefing_up_to_date_false_without_manifest(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    assert bf._briefing_up_to_date(store) is False


def test_briefing_up_to_date_false_without_fingerprint(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    _seed(cfg, store, must=[_must("x")])
    bf.rebuild_briefing(cfg, store)
    (store.paths.briefing / bf.FINGERPRINT_NAME).unlink()
    assert bf._briefing_up_to_date(store) is False


def test_briefing_up_to_date_unreadable_fingerprint(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    _seed(cfg, store, must=[_must("x")])
    bf.rebuild_briefing(cfg, store)
    # Make the fingerprint read raise OSError → treated as not-up-to-date.
    real_read = Path.read_text

    def fake_read(self, *a, **kw):
        if self.name == bf.FINGERPRINT_NAME:
            raise OSError("unreadable")
        return real_read(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", fake_read)
    assert bf._briefing_up_to_date(store) is False


# ----- atomic swap -----------------------------------------------------------


def test_rebuild_swaps_over_existing(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    store.paths.briefing.mkdir(parents=True, exist_ok=True)
    (store.paths.briefing / "stale.md").write_text("old")
    stale_detail = store.paths.briefing / indexes.TOPICS_DETAIL_DIR
    stale_detail.mkdir(parents=True, exist_ok=True)
    (stale_detail / "gone-topic.md").write_text("old detail")
    _seed(cfg, store, must=[_must("x")])
    bf.rebuild_briefing(cfg, store)
    assert not (store.paths.briefing / "stale.md").exists()  # swapped out
    assert not (stale_detail / "gone-topic.md").exists()


def test_rebuild_cleans_up_tmp_on_error(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    _seed(cfg, store, must=[_must("x")])

    def boom(*a, **kw):
        raise RuntimeError("swap failed")

    monkeypatch.setattr(store, "atomic_swap_dir", boom)
    parent = store.paths.briefing.parent
    before = set(parent.glob("briefing.tmp.*"))
    with pytest.raises(RuntimeError, match="swap failed"):
        bf.rebuild_briefing(cfg, store)
    # The staged temp dir was cleaned up on the error path.
    assert set(parent.glob("briefing.tmp.*")) == before


# ----- renderers / helpers ---------------------------------------------------


def test_render_must_remember_orders_by_importance() -> None:
    out = bf._render_must_remember(
        [_must("minor", imp=0.5), _must("major", "incident", imp=9.0)]
    )
    assert out.index("major") < out.index("minor")
    # Freshness is load-bearing (TOUCH-driven forgetting): every line shows
    # the last-touched day + repeat count next to the importance.
    assert "**[incident]**" in out
    assert f"(importance 9.0 · last {NOW[:10]} · 1×)" in out


def test_render_must_remember_empty() -> None:
    assert "_(empty)_" in bf._render_must_remember([])


def test_safe_format_missing_key() -> None:
    out = "{agent_name} {unknown}".format_map(bf._SafeFormat({"agent_name": "S"}))
    assert out == "S {unknown}"


# ----- MANIFEST --------------------------------------------------------------


def test_manifest_counts_and_read_order(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    _seed(cfg, store, must=[_must("a"), _must("b")], skills=[_skill("s")],
          topics=[_topic("T1"), _topic("T2"), _topic("T3")])
    bf.rebuild_briefing(cfg, store)
    m = (store.paths.briefing / bf.MANIFEST_NAME).read_text()
    assert "Agent: Sakuragi" in m
    assert "must_remember: 2 entries" in m
    assert "skills: 1 indexed" in m
    assert "topics: 3 indexed" in m
    # The read order lists the four initial-load files, in order.
    for name in (bf.NOTICE_NAME, bf.MUST_REMEMBER_NAME,
                 bf.SKILL_INDEX_NAME, bf.TOPIC_INDEX_NAME):
        assert f"`{name}`" in m
    assert m.index(bf.NOTICE_NAME) < m.index(bf.MUST_REMEMBER_NAME)
    assert (f"`{indexes.SKILLS_DETAIL_DIR}/`" in m
            and f"`{indexes.TOPICS_DETAIL_DIR}/`" in m)


def test_manifest_last_rebuild_from_state(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    _seed(cfg, store, must=[_must("x")])
    store.write_state({"last_rebuild_at": "2026-01-02T03:04:05Z"})
    bf.rebuild_briefing(cfg, store)
    m = (store.paths.briefing / bf.MANIFEST_NAME).read_text()
    assert "Last rebuild: 2026-01-02T03:04:05Z" in m

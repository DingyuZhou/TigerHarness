"""Tests for the session-start briefing assembly (briefing.py, revamp §6)."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from tigerharness.tiger_memory import briefing as bf
from tigerharness.tiger_memory.bounded_store import BoundedStore
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.entries import (
    STORE_DIARY,
    STORE_MUST_REMEMBER,
    STORE_SKILLS,
    DiaryEntry,
    MustRememberEntry,
    SkillEntry,
)
from tigerharness.tiger_memory.store import Store

NOW = "2026-06-17T00:00:00Z"


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


def _seed(cfg, store, *, skills=(), must=(), emo=()) -> None:
    bstore = BoundedStore(cfg, store)
    store.init_layout()
    if skills:
        bstore.save_atomic(STORE_SKILLS, list(skills))
    if must:
        bstore.save_atomic(STORE_MUST_REMEMBER, list(must))
    if emo:
        bstore.save_atomic(STORE_DIARY, list(emo))


def _skill(name, imp=1.0, usage=0) -> SkillEntry:
    return SkillEntry(
        text=f"procedure for {name}\nmore detail", created_at=NOW, last_used=NOW,
        source="x", name=name, trigger=f"when {name}", procedure=f"do {name}",
        usage_count=usage, importance=imp,
    )


def _must(text, kind="preference", imp=1.0) -> MustRememberEntry:
    return MustRememberEntry(text=text, created_at=NOW, last_used=NOW,
                             source="x", kind=kind, importance=imp)


def _emo(text, weight, reaction="felt") -> DiaryEntry:
    return DiaryEntry(text=text, created_at=NOW, last_used=NOW, source="x",
                          weight=weight, reaction=reaction)


# ----- full rebuild ---------------------------------------------------------


def test_rebuild_assembles_all_files(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    _seed(
        cfg, store,
        skills=[_skill("A", imp=2.0), _skill("B", imp=5.0)],
        must=[_must("never push", "owner_explicit", imp=5.0), _must("use uv")],
        emo=[_emo("good", 3.0), _emo("bad", -8.0)],
    )
    bf.rebuild_briefing(cfg, store)
    b = store.paths.briefing
    for name in (bf.README_NAME, bf.NOTICE_NAME, bf.MUST_REMEMBER_NAME,
                 bf.DIARY_NAME, bf.SKILL_INDEX_NAME, bf.MANIFEST_NAME,
                 bf.FINGERPRINT_NAME):
        assert (b / name).exists(), name
    # README is persona-substituted.
    assert "Sakuragi" in (b / bf.README_NAME).read_text()
    # Skill index ordered most-important first (B imp=5 before A imp=2).
    idx = (b / bf.SKILL_INDEX_NAME).read_text()
    assert idx.index("## B") < idx.index("## A")
    # Emotional ordered by |weight| (the -8 before the +3).
    emo = (b / bf.DIARY_NAME).read_text()
    assert emo.index("bad") < emo.index("good")
    # must_remember shows kind + importance.
    mr = (b / bf.MUST_REMEMBER_NAME).read_text()
    assert "owner_explicit" in mr and "never push" in mr


def test_rebuild_empty_stores(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    bf.rebuild_briefing(cfg, store)
    assert "_(empty)_" in (store.paths.briefing / bf.MUST_REMEMBER_NAME).read_text()
    assert "_(empty)_" in (store.paths.briefing / bf.DIARY_NAME).read_text()
    assert "no skills" in (store.paths.briefing / bf.SKILL_INDEX_NAME).read_text()


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


def test_rebuild_swaps_over_existing(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    store.paths.briefing.mkdir(parents=True, exist_ok=True)
    (store.paths.briefing / "stale.md").write_text("old")
    _seed(cfg, store, must=[_must("x")])
    bf.rebuild_briefing(cfg, store)
    assert not (store.paths.briefing / "stale.md").exists()  # swapped out


# ----- emotional top-N ------------------------------------------------------


def test_emotional_top_caps(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, "briefing:\n  emotional_top: 2\n")
    store = Store(cfg.store.root)
    _seed(cfg, store, emo=[_emo("a", 1.0), _emo("b", 9.0), _emo("c", -5.0)])
    bf.rebuild_briefing(cfg, store)
    out = (store.paths.briefing / bf.DIARY_NAME).read_text()
    # Only the two strongest (9.0, -5.0) shown; the 1.0 dropped.
    assert "b" in out and "c" in out
    assert out.count("- ") == 2


def test_emotional_top_zero_shows_all(tmp_path: Path) -> None:
    entries = [_emo("a", 1.0), _emo("b", 2.0)]
    out = bf._render_diary(entries, 0)
    assert out.count("- ") == 2


def test_emotional_positive_sign_marker() -> None:
    out = bf._render_diary([_emo("up", 4.0)], 0)
    assert "(+4.0)" in out


# ----- renderers / helpers --------------------------------------------------


def test_one_line_truncates() -> None:
    assert bf._one_line("x" * 200, limit=10).endswith("…")
    assert bf._one_line("\n\nfirst real line\nsecond") == "first real line"
    assert bf._one_line("   \n  ") == ""


def test_skill_index_shows_usage_and_importance() -> None:
    out = bf._render_skill_index([_skill("X", imp=3.5, usage=4)])
    assert "used 4×" in out and "importance 3.50" in out


def test_compute_fingerprint_absent_files(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    fp = bf._compute_fingerprint(store)
    assert "skills.md:0" in fp  # absent → 0


def test_briefing_up_to_date_false_without_manifest(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    assert bf._briefing_up_to_date(store) is False


def test_safe_format_missing_key() -> None:
    out = "{agent_name} {unknown}".format_map(bf._SafeFormat({"agent_name": "S"}))
    assert out == "S {unknown}"


def test_rebuild_cleans_up_tmp_on_error(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    _seed(cfg, store, must=[_must("x")])

    def boom(*a, **kw):
        raise RuntimeError("swap failed")

    monkeypatch.setattr(store, "atomic_swap_dir", boom)
    import pytest
    parent = store.paths.briefing.parent
    before = set(parent.glob("briefing.tmp.*"))
    with pytest.raises(RuntimeError, match="swap failed"):
        bf.rebuild_briefing(cfg, store)
    # The staged temp dir was cleaned up on the error path.
    assert set(parent.glob("briefing.tmp.*")) == before


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


def test_manifest_counts(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    _seed(cfg, store, must=[_must("a"), _must("b")], emo=[_emo("e", 1.0)],
          skills=[_skill("s")])
    bf.rebuild_briefing(cfg, store)
    m = (store.paths.briefing / bf.MANIFEST_NAME).read_text()
    assert "must_remember: 2 entries" in m
    assert "diary: 1 entries" in m
    assert "skills: 1 indexed" in m

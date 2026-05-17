"""Tests for the drill-down + tree + search readers."""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from textwrap import dedent
from uuid import uuid4

import pytest

from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.drill import _children_of, drill, raw, search, tree
from tigerharness.tiger_memory.store import Store


def _setup(tmp_path: Path):
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(dedent(f"""\
        agent: {{name: T, role: T}}
        store: {{root: {tmp_path}/memory}}
        sources:
          - kind: claude_code
            project_path: {tmp_path}/proj/
        summarizer: {{backend: anthropic, model: claude-opus-4-7, prompts: default/v1}}
        rebuild: {{lock_path: {tmp_path}/lock}}
    """))
    cfg = load_config(cfg_path)
    store = Store(cfg.store.root)
    store.init_layout()
    return cfg, store


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_children_short_to_archive(tmp_path: Path) -> None:
    cfg, store = _setup(tmp_path)
    uid = str(uuid4())
    short = store.paths.journal / f"20260514-082136-{uid}.md"
    archive = store.paths.archive / f"20260514-082136-{uid}.md"
    _write(short, "short")
    _write(archive, "detailed")
    children = _children_of(store, short)
    assert children == [archive]


def test_children_daily_to_shorts(tmp_path: Path) -> None:
    cfg, store = _setup(tmp_path)
    daily = store.paths.journal / "20260514-daily-abc.md"
    s1 = store.paths.journal / f"20260514-082136-{uuid4()}.md"
    s2 = store.paths.journal / f"20260514-093000-{uuid4()}.md"
    other = store.paths.journal / f"20260513-082136-{uuid4()}.md"
    for f in (daily, s1, s2, other):
        _write(f, "x")
    children = _children_of(store, daily)
    assert set(children) == {s1, s2}


def test_children_weekly_to_dailies(tmp_path: Path) -> None:
    cfg, store = _setup(tmp_path)
    weekly = store.paths.journal / "20260511-week-abc.md"
    mon = store.paths.journal / "20260511-daily-aa.md"
    tue = store.paths.journal / "20260512-daily-bb.md"
    next_mon = store.paths.journal / "20260518-daily-cc.md"  # next week
    for f in (weekly, mon, tue, next_mon):
        _write(f, "x")
    children = _children_of(store, weekly)
    assert set(children) == {mon, tue}


def test_children_monthly_to_weeklies(tmp_path: Path) -> None:
    cfg, store = _setup(tmp_path)
    monthly = store.paths.journal / "202605-month-abc.md"
    w1 = store.paths.journal / "20260504-week-aa.md"
    w2 = store.paths.journal / "20260511-week-bb.md"
    other_month = store.paths.journal / "20260601-week-cc.md"
    for f in (monthly, w1, w2, other_month):
        _write(f, "x")
    children = _children_of(store, monthly)
    assert set(children) == {w1, w2}


def test_drill_prints_body_and_children(tmp_path: Path, capsys) -> None:
    cfg, store = _setup(tmp_path)
    daily = store.paths.journal / "20260514-daily-abc.md"
    short = store.paths.journal / f"20260514-082136-{uuid4()}.md"
    daily.write_text("daily body\n")
    short.write_text("short body\n")
    rc = drill(store, daily)
    assert rc == 0
    out = capsys.readouterr().out
    assert "daily body" in out
    assert "1 child(ren)" in out


def test_tree_recursive(tmp_path: Path, capsys) -> None:
    cfg, store = _setup(tmp_path)
    monthly = store.paths.journal / "202605-month-abc.md"
    weekly = store.paths.journal / "20260511-week-x.md"
    daily = store.paths.journal / "20260512-daily-y.md"
    short = store.paths.journal / f"20260512-100000-{uuid4()}.md"
    for f, body in [(monthly, "M\n"), (weekly, "W\n"), (daily, "D\n"),
                    (short, "S\n")]:
        f.write_text(body)
    rc = tree(store, monthly)
    out = capsys.readouterr().out
    assert rc == 0
    # Monthly → weekly → daily → short — all 4 names in tree output
    assert monthly.name in out
    assert weekly.name in out
    assert daily.name in out
    assert short.name in out


def test_search_grep_python_fallback(tmp_path: Path, capsys,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, store = _setup(tmp_path)
    short = store.paths.journal / f"20260514-082136-{uuid4()}.md"
    short.write_text("This conversation discussed TSMOM strategy.\n")
    # Force ripgrep to fail so we exercise the python fallback.
    monkeypatch.setenv("PATH", "/nonexistent")
    rc = search(cfg, store, topic="TSMOM", mode="grep")
    out = capsys.readouterr().out
    assert rc == 0
    assert "20260514" in out


def test_archive_is_terminal_no_recursion(tmp_path: Path) -> None:
    """Regression: short→archive→archive infinite loop bug."""
    cfg, store = _setup(tmp_path)
    uid = str(uuid4())
    short = store.paths.journal / f"20260514-082136-{uid}.md"
    archive = store.paths.archive / f"20260514-082136-{uid}.md"
    short.write_text("s")
    archive.write_text("a")
    children = _children_of(store, archive)
    assert children == []


def test_resolve_strips_leading_memory_prefix(tmp_path: Path) -> None:
    """Regression: passing `memory/briefing/...` from outside store root."""
    cfg, store = _setup(tmp_path)
    daily = store.paths.briefing / "daily" / "20260514-daily-x.md"
    daily.parent.mkdir(parents=True, exist_ok=True)
    daily.write_text("d")
    from tigerharness.tiger_memory.drill import _resolve
    rel = Path(store.root.name) / "briefing" / "daily" / "20260514-daily-x.md"
    found = _resolve(store, rel)
    assert found == daily


def test_search_rag_errors_clearly_without_deps(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, store = _setup(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Force "no embedder available" — fastembed may be installed in the
    # test env, so deleting OPENAI_API_KEY alone isn't enough to trigger
    # the error path.
    monkeypatch.setattr("tigerharness.tiger_memory.rag.pick_embedder", lambda _mode: None)
    rc = search(cfg, store, topic="anything", mode="rag")
    out = capsys.readouterr().out
    assert rc == 2
    assert ("RAG" in out) or ("rag" in out.lower())


def test_search_hybrid_falls_back_to_grep_without_rag(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hybrid should still return grep hits even when RAG isn't available."""
    cfg, store = _setup(tmp_path)
    short = store.paths.journal / f"20260514-082136-{uuid4()}.md"
    short.write_text("notes about TSMOM strategy here\n")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    rc = search(cfg, store, topic="TSMOM", mode="hybrid")
    out = capsys.readouterr().out
    assert rc == 0
    # Should still surface the grep hit.
    assert "20260514" in out

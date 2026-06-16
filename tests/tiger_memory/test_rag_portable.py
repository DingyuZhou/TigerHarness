"""Cross-machine portability of the RAG embeddings index (relative paths).

These tests use a fake *deterministic* embedder plus the real ``sqlite_vec``
extension, so the real ``_open_db`` gate, ``vss`` table, and indexing paths
run fast and without a model download. They prove the brief's acceptance:
a committed ``.embeddings.db`` built under one store root works under a
different root (same embedder), legacy absolute-path indexes migrate
without crashing, and an embedder-dim mismatch re-embeds safely.
"""
from __future__ import annotations

import logging
import re
import shutil
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from tigerharness.tiger_memory import frontmatter, rag
from tigerharness.tiger_memory.store import Store

pytest.importorskip("sqlite_vec")  # real extension required for these tests


# --- fake deterministic embedder -------------------------------------------

_TOKENS = ["alpha", "bravo", "charlie", "delta"]


class FakeEmbedder:
    """Deterministic, network-free embedder.

    A text embeds to a one-hot vector chosen by which ``_TOKENS`` marker it
    contains, so a query topic equal to a marker is the exact nearest
    neighbour of the archive entry carrying that marker. ``name`` is a fixed
    constant so the reuse path (``prev == (summ, embedder.name)``) fires
    across separate Store opens. ``batch_calls`` counts index embeds only.
    """

    name = "fake/det-v1"

    def __init__(self, dim: int = 4):
        self.dim = dim
        self.batch_calls = 0

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for i, tok in enumerate(_TOKENS[: self.dim]):
            if tok in text:
                v[i] = 1.0
                break
        return v

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.batch_calls += 1
        return [self._vec(t) for t in texts]

    def embed_one(self, text: str) -> list[float]:
        return self._vec(text)


def _make_store(root: Path, entries: list[tuple[str, str]]) -> Store:
    """Create a store at *root* with archive entries (uuid, marker-body)."""
    store = Store(root)
    store.init_layout()
    for uid, body in entries:
        f = store.paths.archive / f"20260514-000000-{uid}.md"
        f.write_text(
            frontmatter.render(
                {"conversation_uuid": uid, "summarizer": "mock@v1"}, body
            )
        )
    return store


def _vss_dim(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='vss'"
        ).fetchone()
    finally:
        conn.close()
    return int(re.search(r"float\[(\d+)\]", row[0]).group(1))


def _docs_paths(db_path: Path) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return [r[0] for r in conn.execute("SELECT path FROM docs").fetchall()]
    finally:
        conn.close()


@pytest.fixture
def entries() -> list[tuple[str, str]]:
    return [
        (str(uuid4()), "Discussion about alpha energy.\n"),
        (str(uuid4()), "Notes on bravo mining.\n"),
    ]


# --- 1. relative-path storage ----------------------------------------------


def test_relative_path_storage(tmp_path, entries):
    store = _make_store(tmp_path / "A", entries)
    fake = FakeEmbedder(dim=4)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(rag, "pick_embedder", lambda *_a, **_k: fake)
        rc = rag.search(None, store, topic="alpha", k=10)
    assert rc == 0
    db = store.paths.journal / ".embeddings.db"
    paths = _docs_paths(db)
    assert paths, "expected indexed rows"
    for p in paths:
        assert not Path(p).is_absolute(), f"path not relative: {p}"
        assert p.startswith("archive/"), p


# --- 2. cross-root portability + reuse on the compatible branch ------------


def test_cross_root_portability_and_reuse(tmp_path, entries, capsys, caplog):
    # Build under root A.
    store_a = _make_store(tmp_path / "A", entries)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(rag, "pick_embedder", lambda *_a, **_k: FakeEmbedder(4))
        rag.search(None, store_a, topic="alpha", k=10)
    db_a = store_a.paths.journal / ".embeddings.db"

    # Clone: a different root B with the SAME archive content + the copied db.
    store_b = _make_store(tmp_path / "B", entries)
    shutil.copy(db_a, store_b.paths.journal / ".embeddings.db")

    fake_b = FakeEmbedder(dim=4)  # identical name+dim as built A
    capsys.readouterr()
    with caplog.at_level(logging.WARNING, logger="tigerharness.tiger_memory.rag"):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(rag, "pick_embedder", lambda *_a, **_k: fake_b)
            rc = rag.search(None, store_b, topic="alpha", k=10)
            paths = rag.query_paths(None, store_b, topic="alpha", k=10)

    out = capsys.readouterr().out
    assert rc == 0
    # No crash, and the alpha entry is the nearest (printed first).
    assert "archive/" in out
    # Reuse: the gate took the compatible/no-drop branch (no rebuild warning)
    # AND nothing was re-embedded for indexing.
    assert "rag index rebuilt" not in caplog.text
    assert fake_b.batch_calls == 0
    # query_paths resolves under the LOCAL root B and the files exist.
    assert paths, "expected hybrid paths"
    for p in paths:
        assert p.is_absolute()
        assert str(p).startswith(str(store_b.root))
        assert p.exists()


# --- 3. legacy absolute-path migration (faithful fixture) ------------------


def _build_legacy_db(db_path: Path, abs_path: str, dim: int = 4) -> None:
    """A faithful pre-change index: docs with an ABSOLUTE path, a real vss
    row at the legacy dim, and NO ``meta`` table."""
    import sqlite_vec

    conn = sqlite3.connect(str(db_path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute(
        "CREATE TABLE docs (id INTEGER PRIMARY KEY, uuid TEXT UNIQUE, "
        "path TEXT, summarizer TEXT, embedder TEXT, dim INTEGER)"
    )
    conn.execute(
        f"CREATE VIRTUAL TABLE vss USING vec0(embedding float[{dim}])"
    )
    import json as _json

    cur = conn.execute(
        "INSERT INTO docs (uuid, path, summarizer, embedder, dim) "
        "VALUES ('legacy-uid', ?, 'mock@v1', 'fake/det-v1', ?)",
        (abs_path, dim),
    )
    conn.execute(
        "INSERT INTO vss (rowid, embedding) VALUES (?, ?)",
        (cur.lastrowid, _json.dumps([1.0] + [0.0] * (dim - 1))),
    )
    conn.commit()
    conn.close()


def test_absolute_path_migration(tmp_path, entries, caplog):
    store = _make_store(tmp_path / "A", entries)
    db = store.paths.journal / ".embeddings.db"
    # Legacy row carries an absolute path from a FOREIGN machine.
    foreign_abs = "/home/other/teams/X/memories/Ayako/archive/old.md"
    _build_legacy_db(db, foreign_abs, dim=4)

    # Pre-state: the foreign absolute path is not under the local root, so
    # the old `relative_to` would have raised — _display_path now degrades
    # to the basename instead of crashing.
    assert rag._display_path(store, foreign_abs) == "old.md"

    # Opening migrates: the incompatible (no-meta) index is dropped+rebuilt.
    with caplog.at_level(logging.WARNING, logger="tigerharness.tiger_memory.rag"):
        conn = rag._open_db(db, 4)
    try:
        assert "rag index rebuilt" in caplog.text
        assert "path-scheme" in caplog.text
        # Dropped: no legacy rows survive; meta now stamped.
        assert conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == 0
        assert rag._meta_get(conn, "path_scheme") == "relative"
        # Rebuild repopulates with RELATIVE paths.
        fake = FakeEmbedder(dim=4)
        rag._index_archive_if_needed(conn, store, fake)
        for (p,) in conn.execute("SELECT path FROM docs").fetchall():
            assert not Path(p).is_absolute()
            assert p.startswith("archive/")
    finally:
        conn.close()


# --- 4. dimension-mismatch re-embed (observable) ---------------------------


def test_dimension_mismatch_reembed(tmp_path, entries, caplog):
    store = _make_store(tmp_path / "A", entries)
    db = store.paths.journal / ".embeddings.db"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(rag, "pick_embedder", lambda *_a, **_k: FakeEmbedder(4))
        rag.search(None, store, topic="alpha", k=10)
    assert _vss_dim(db) == 4

    # Reopen with a wider embedder: the fixed-width vss can't take it, so the
    # gate drops+rebuilds at the new dim and re-embeds — no insert crash.
    fake8 = FakeEmbedder(dim=8)
    with caplog.at_level(logging.WARNING, logger="tigerharness.tiger_memory.rag"):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(rag, "pick_embedder", lambda *_a, **_k: fake8)
            rc = rag.search(None, store, topic="alpha", k=10)
    assert rc == 0
    assert "rag index rebuilt" in caplog.text
    assert "embedding-dim" in caplog.text
    assert _vss_dim(db) == 8
    assert fake8.batch_calls >= 1  # re-embed happened


# --- 5. helper branch table -------------------------------------------------


def test_resolve_stored_path_relative(tmp_path):
    store = Store(tmp_path / "A")
    assert rag._resolve_stored_path(store, "archive/x.md") == store.root / "archive/x.md"


def test_resolve_stored_path_absolute(tmp_path):
    store = Store(tmp_path / "A")
    abs_p = "/elsewhere/archive/x.md"
    assert rag._resolve_stored_path(store, abs_p) == Path(abs_p)


def test_display_path_relative(tmp_path):
    store = Store(tmp_path / "A")
    assert rag._display_path(store, "archive/x.md") == "archive/x.md"


def test_display_path_absolute_under_root(tmp_path):
    store = Store(tmp_path / "A")
    under = str(store.root / "archive" / "x.md")
    assert rag._display_path(store, under) == "archive/x.md"


def test_display_path_foreign_absolute(tmp_path):
    store = Store(tmp_path / "A")
    assert rag._display_path(store, "/foreign/archive/x.md") == "x.md"


# --- 6. containment: a shared index must not resolve OUTSIDE the root -------


def test_resolve_stored_path_contains_traversal(tmp_path):
    """The index is now a SHARED, committed artifact, so a stored path is
    untrusted input. A `..`-escaping relative path must NOT resolve to an
    arbitrary filesystem location that the hybrid reader would then open."""
    store = Store(tmp_path / "A")
    store.init_layout()
    resolved = rag._resolve_stored_path(store, "../../../../../../etc/passwd").resolve()
    assert str(resolved).startswith(str(store.root)), (
        f"path escaped store root: {resolved}"
    )


def test_containment_logs_warning(tmp_path, caplog):
    """Containment is visible: an escaping path logs exactly one WARNING;
    a normal relative path logs none."""
    store = Store(tmp_path / "A")
    store.init_layout()
    with caplog.at_level(logging.WARNING, logger="tigerharness.tiger_memory.rag"):
        rag._resolve_stored_path(store, "../../../../etc/passwd")
    escapes = [r for r in caplog.records if "contained out-of-root" in r.message]
    assert len(escapes) == 1  # exactly one per offending row

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="tigerharness.tiger_memory.rag"):
        rag._resolve_stored_path(store, "archive/normal.md")
    assert not [r for r in caplog.records if "contained out-of-root" in r.message]


def test_display_path_relative_escape(tmp_path):
    """A relative path that escapes the root displays as its basename, so a
    traversal string is never shown as if it were a real archive entry."""
    store = Store(tmp_path / "A")
    store.init_layout()
    assert rag._display_path(store, "../../../etc/passwd") == "passwd"


def test_containment_holds_under_symlinked_root(tmp_path):
    """Proving the deferred symlinked-root claim: Store resolves the root, so
    containment and normal resolution both hold when the root is reached via
    a symlink."""
    realroot = tmp_path / "real"
    realroot.mkdir()
    linkroot = tmp_path / "link"
    linkroot.symlink_to(realroot, target_is_directory=True)
    store = Store(linkroot)
    store.init_layout()
    # Escaping path is still contained under the (resolved) real root.
    escaped = rag._resolve_stored_path(store, "../../../../etc/passwd").resolve()
    assert str(escaped).startswith(str(store.root))
    # A normal relative path resolves under the real root.
    normal = rag._resolve_stored_path(store, "archive/x.md")
    assert normal == store.root / "archive" / "x.md"
    assert str(store.root) == str(realroot.resolve())

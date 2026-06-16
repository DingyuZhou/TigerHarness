"""RAG search over `archive/` via embeddings + sqlite-vec.

Activated via ``tiger-memory search --mode rag`` or
``--mode hybrid`` (which fuses with grep via RRF).

Embedder selection is via ``tiger_memory.embedders.pick_embedder()``:
    1. OpenAI text-embedding-3-small if OPENAI_API_KEY + openai pkg.
    2. Otherwise fastembed/BAAI/bge-small-en-v1.5 (open-source, the
       default, and the embedder a shared/committed index is keyed to).
    3. Otherwise the CLI surfaces a clear install hint.

The vector store is sqlite-vec at ``<store>/journal/.embeddings.db``. The
``docs`` rows are keyed by (conversation_uuid, summarizer, embedder), so a
summarizer prompt bump or a same-dim embedder swap re-embeds only the
affected rows on next search.

**Portable, shareable index.** Archive paths are stored *relative* to
``store.root`` (e.g. ``archive/<file>.md``), so a committed
``.embeddings.db`` keeps working after a fresh clone on another machine —
no rebuild needed, **as long as the cloner uses the same embedder**. The
index is meant to be tracked in git and shared; it is intentionally NOT
gitignored.

Two cross-cutting incompatibilities trigger a one-time full rebuild
instead of a per-row re-embed, via the compatibility gate in ``_open_db``:

    * a legacy index that stored *absolute* paths (its ``path_scheme`` is
      not ``"relative"``) — rebuilt so paths become portable; and
    * an embedder whose vector ``dim`` differs from the committed index —
      the ``vss`` table is fixed-width, so it is dropped and rebuilt at the
      new dim rather than erroring on a dimension-mismatched insert.

Both rebuilds emit a ``log.warning`` so the (one-time) cost is visible. A
shared index is keyed to a single embedder; alternating embedders of
different dims against one store rebuilds on each open, by design.

The binary sqlite index is NOT mergeable: concurrent edits on different
machines conflict at the file level. Sharing is "rebuild on conflict", not
three-way merge.
"""
from __future__ import annotations

import logging

import json
from pathlib import Path
from typing import Iterable

from .config import Config
from .embedders import Embedder, chunks, pick_embedder
from .store import Store

log = logging.getLogger("tigerharness.tiger_memory.rag")

# On-disk path-storage scheme. An index stamped with any other value (or
# none — a legacy absolute-path index) is rebuilt once on open so its
# stored paths become portable. See ``_migrate_if_incompatible``.
_PATH_SCHEME = "relative"


def search(cfg: Config, store: Store, *, topic: str, k: int = 10) -> int:
    """Top-K nearest archive entries by embedding similarity."""
    try:
        import sqlite_vec  # noqa: F401
    except ImportError:
        print(
            "RAG mode needs sqlite-vec. Install with "
            "`uv sync --extra rag-local` (free) or "
            "`uv sync --extra rag-openai` (paid).",
            flush=True,
        )
        return 2

    embedder = pick_embedder("auto")
    if embedder is None:
        print(
            "No embedder available. Install with "
            "`uv sync --extra rag-local` (open-source, recommended) "
            "or `uv sync --extra rag-openai` (requires OPENAI_API_KEY).",
            flush=True,
        )
        return 2

    db_path = store.paths.journal / ".embeddings.db"
    conn = _open_db(db_path, embedder.dim)
    try:
        _index_archive_if_needed(conn, store, embedder)
        hits = _query(conn, embedder, topic, k=k)
        if not hits:
            print("no matches")
            return 0
        for uid, score, path in hits:
            print(f"{score:.3f}  {_display_path(store, path)}")
    finally:
        conn.close()
    return 0


def query_paths(cfg: Config, store: Store, *, topic: str, k: int = 30) -> list[Path]:
    """Hybrid-mode helper: return ranked archive paths or [] on failure.

    Unexpected failures are surfaced on stderr so a broken RAG path
    doesn't silently pretend grep was the whole answer.
    """
    try:
        import sqlite_vec  # noqa: F401
    except ImportError:
        return []
    embedder = pick_embedder("auto")
    if embedder is None:
        return []
    db_path = store.paths.journal / ".embeddings.db"
    # _open_db is inside the try so a corrupted db / failed extension
    # load also routes through the stderr warning instead of bubbling
    # up as a traceback.
    conn = None
    try:
        conn = _open_db(db_path, embedder.dim)
        _index_archive_if_needed(conn, store, embedder)
        hits = _query(conn, embedder, topic, k=k)
        return [_resolve_stored_path(store, path) for _, _, path in hits]
    except Exception as exc:  # noqa: BLE001
        import sys
        print(
            f"(rag query failed: {type(exc).__name__}: {exc}; "
            "hybrid falling back to grep only)",
            file=sys.stderr,
        )
        return []
    finally:
        if conn is not None:
            conn.close()


# ----- Path resolution (portability) ----------------------------------------


def _resolve_stored_path(store: Store, stored: str) -> Path:
    """Resolve a stored archive path to an absolute path on THIS machine.

    New rows store a path relative to ``store.root``; resolve it back
    against the local root so hybrid-mode file reads land on the real
    file after a cross-machine clone. A legacy absolute path is returned
    unchanged (the migration in ``_open_db`` rewrites such rows on next
    index, so this branch is the defensive belt-and-suspenders path).

    The index is a SHARED, committed artifact, so a stored relative path
    is untrusted input. A ``..``-escaping path is contained to the store
    root (falls back to the basename under ``archive/``) rather than
    resolving to an arbitrary filesystem location the hybrid reader would
    then open.
    """
    p = Path(stored)
    if p.is_absolute():
        return p
    candidate = store.root / p
    try:
        candidate.resolve().relative_to(store.root)
    except ValueError:
        return store.paths.archive / p.name
    return candidate


def _display_path(store: Store, stored: str) -> str:
    """Human-facing archive path, relative to the local store root.

    Never raises. A stored relative path is already root-relative. A
    foreign absolute path (pre-migration, from another machine) is NOT
    under the local ``store.root`` — falling back to its basename instead
    of letting ``relative_to`` raise ``ValueError`` (the original crash).
    """
    p = Path(stored)
    if not p.is_absolute():
        return stored
    try:
        return str(p.relative_to(store.root))
    except ValueError:
        return p.name


# ----- DB schema ------------------------------------------------------------


def _meta_get(conn, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (key,)
    ).fetchone()
    return row[0] if row else None


def _meta_set(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _migrate_if_incompatible(conn, dim: int) -> None:
    """Drop an index incompatible with the current scheme/dim so
    ``_open_db`` recreates it fresh (a one-time rebuild).

    Handles two incompatibilities that a per-row re-embed cannot:
    a legacy index that stored absolute paths (``path_scheme`` other than
    ``"relative"``), and an embedder whose vector ``dim`` differs from the
    committed index (the fixed-width ``vss`` table would reject the insert).
    """
    have_docs = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='docs'"
    ).fetchone()
    if have_docs is None:
        return  # fresh db — nothing to migrate
    scheme = _meta_get(conn, "path_scheme")
    stored_dim = _meta_get(conn, "embedding_dim")
    if scheme == _PATH_SCHEME and stored_dim == str(dim):
        return  # compatible — reuse the committed index as-is
    reason = "path-scheme" if scheme != _PATH_SCHEME else "embedding-dim"
    log.warning(
        "rag index rebuilt (%s changed): scheme %s->%s, dim %s->%s",
        reason, scheme, _PATH_SCHEME, stored_dim, dim,
    )
    conn.execute("DROP TABLE IF EXISTS vss")
    conn.execute("DROP TABLE IF EXISTS docs")


def _open_db(db_path: Path, dim: int):
    import sqlite3
    import sqlite_vec
    conn = sqlite3.connect(str(db_path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
    )
    _migrate_if_incompatible(conn, dim)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS docs (
            id INTEGER PRIMARY KEY,
            uuid TEXT UNIQUE,
            path TEXT,
            summarizer TEXT,
            embedder TEXT,
            dim INTEGER
        )
        """
    )
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS vss USING vec0(
            embedding float[{dim}]
        )
        """
    )
    # Stamp the scheme + dim and make the drop/create/stamp durable BEFORE
    # any embedding work, so a crash mid-rebuild leaves a safe
    # compatible-but-empty index (the next open simply re-indexes), never
    # the old stamp over dropped tables.
    _meta_set(conn, "path_scheme", _PATH_SCHEME)
    _meta_set(conn, "embedding_dim", str(dim))
    conn.commit()
    return conn


# ----- Indexing -------------------------------------------------------------


def _index_archive_if_needed(conn, store: Store, embedder: Embedder) -> None:
    """Embed any archive entry not yet indexed under the current
    (summarizer, embedder) keying."""
    from . import frontmatter

    existing = {
        row[0]: (row[1], row[2])
        for row in conn.execute(
            "SELECT uuid, summarizer, embedder FROM docs"
        ).fetchall()
    }

    pending: list[tuple[str, str, str, str]] = []  # uuid, path, summarizer, text
    for f in store.paths.archive.glob("*.md"):
        fm = frontmatter.read_frontmatter(f)
        uid = fm.get("conversation_uuid", "")
        summ = fm.get("summarizer", "")
        if not uid:
            continue
        prev = existing.get(uid)
        if prev == (summ, embedder.name):
            continue
        text = f.read_text(encoding="utf-8")
        # Store the path RELATIVE to store.root (POSIX-normalised) so a
        # committed index is portable across machines. ``f`` is always
        # under ``store.root`` (archive == root/archive), so relative_to
        # is safe here on the write side.
        rel = f.relative_to(store.root).as_posix()
        pending.append((uid, rel, summ, text))

    if not pending:
        return

    # Batch embed.
    for batch in chunks(pending, 64):
        inputs = [t for _, _, _, t in batch]
        vectors = embedder.embed_batch(inputs)
        for (uid, path, summ, _), vec in zip(batch, vectors):
            cur = conn.execute(
                "INSERT INTO docs (uuid, path, summarizer, embedder, dim) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(uuid) DO UPDATE SET "
                "  path=excluded.path, summarizer=excluded.summarizer, "
                "  embedder=excluded.embedder, dim=excluded.dim",
                (uid, path, summ, embedder.name, embedder.dim),
            )
            row_id = cur.lastrowid or conn.execute(
                "SELECT id FROM docs WHERE uuid = ?", (uid,)
            ).fetchone()[0]
            conn.execute("DELETE FROM vss WHERE rowid = ?", (row_id,))
            conn.execute(
                "INSERT INTO vss (rowid, embedding) VALUES (?, ?)",
                (row_id, json.dumps(vec)),
            )
        conn.commit()


def _query(
    conn, embedder: Embedder, topic: str, *, k: int
) -> list[tuple[str, float, str]]:
    q = embedder.embed_one(topic)
    # sqlite-vec 0.1.x requires `k = ?` as a vec0 constraint inside the
    # WHERE clause; an outer SQL `LIMIT` is applied after the JOIN and
    # doesn't satisfy vec0's KNN check.
    rows = conn.execute(
        """
        SELECT d.uuid, vss.distance, d.path
        FROM vss JOIN docs d ON d.id = vss.rowid
        WHERE vss.embedding MATCH ? AND k = ?
        ORDER BY vss.distance
        """,
        (json.dumps(q), k),
    ).fetchall()
    return [(uid, float(dist), path) for uid, dist, path in rows]

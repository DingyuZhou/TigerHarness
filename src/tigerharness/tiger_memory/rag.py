"""RAG search over `archive/` via embeddings + sqlite-vec.

Activated via ``tiger-memory search --mode rag`` or
``--mode hybrid`` (which fuses with grep via RRF).

Embedder selection is via ``tiger_memory.embedders.pick_embedder()``:
    1. OpenAI text-embedding-3-small if OPENAI_API_KEY + openai pkg.
    2. Otherwise fastembed/BAAI/bge-small-en-v1.5 (open-source).
    3. Otherwise the CLI surfaces a clear install hint.

The vector store is sqlite-vec at ``<store>/journal/.embeddings.db``,
keyed by (conversation_uuid, summarizer_version, embedder_name) so an
embedder swap or a summarizer prompt bump triggers re-embedding on
next search.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from .config import Config
from .embedders import Embedder, chunks, pick_embedder
from .store import Store


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
            print(f"{score:.3f}  {Path(path).relative_to(store.root)}")
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
        return [Path(path) for _, _, path in hits]
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


# ----- DB schema ------------------------------------------------------------


def _open_db(db_path: Path, dim: int):
    import sqlite3
    import sqlite_vec
    conn = sqlite3.connect(str(db_path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
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
        pending.append((uid, str(f), summ, text))

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

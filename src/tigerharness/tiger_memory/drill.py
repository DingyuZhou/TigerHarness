"""Reader commands: drill, tree, raw, search.

All lockless — pure reads. See design doc §9.

Drill-down chain (filename-pattern only — no frontmatter pointers):
    monthly → weekly → daily → short → archive → raw transcript
"""
from __future__ import annotations

import os
import re
import subprocess
from datetime import date, timedelta
from pathlib import Path

from . import frontmatter
from .config import Config
from .store import DAILY_RE, MONTHLY_RE, SHORT_RE, WEEKLY_RE, Store


# ----- drill ---------------------------------------------------------------


def drill(store: Store, path: Path) -> int:
    """Open file body + list immediate children."""
    p = _resolve(store, path)
    if p is None or not p.exists():
        print(f"not found: {path}", flush=True)
        return 2
    print(f"=== {p} ===")
    print(p.read_text(encoding="utf-8"))
    children = _children_of(store, p)
    if children:
        print(f"\n--- {len(children)} child(ren) ---")
        for c in children:
            print(f"  {c.relative_to(store.root)}  — {_preview(c)}")
    return 0


# ----- tree ----------------------------------------------------------------


def tree(store: Store, path: Path, depth: int | None = None) -> int:
    """Recursive hierarchy from *path*."""
    p = _resolve(store, path)
    if p is None or not p.exists():
        print(f"not found: {path}", flush=True)
        return 2
    _print_tree(store, p, prefix="", depth=depth, current=0)
    return 0


def _print_tree(
    store: Store, p: Path, *, prefix: str, depth: int | None, current: int
) -> None:
    """ASCII tree, one line per file.

    Caller prints the root node before invoking; each recursion prints
    its children. Avoids double-printing.
    """
    if current == 0:
        # Root — print bare name with no prefix.
        print(f"{p.name}  — {_preview(p)}")
    if depth is not None and current >= depth:
        return
    children = _children_of(store, p)
    for i, c in enumerate(children):
        last = i == len(children) - 1
        branch = "└── " if last else "├── "
        cont = "    " if last else "│   "
        print(f"{prefix}{branch}{c.name}  — {_preview(c)}")
        _print_tree(
            store, c, prefix=prefix + cont, depth=depth, current=current + 1
        )


# ----- raw -----------------------------------------------------------------


def raw(cfg: Config, store: Store, archive_path: Path) -> int:
    """Return raw-transcript locator(s) for an archive entry."""
    p = _resolve(store, archive_path)
    if p is None or not p.exists():
        print(f"not found: {archive_path}", flush=True)
        return 2
    fm = frontmatter.read_frontmatter(p)
    source = fm.get("source", "")
    source_id = fm.get("source_id", "")
    if source == "claude_code":
        # Find the JSONL by session_id (= conversation_uuid for claude_code).
        jsonl = _find_claude_jsonl(cfg, source_id or fm.get("conversation_uuid", ""))
        if jsonl is None:
            print(f"raw transcript not found for {source_id}")
            return 1
        print(str(jsonl))
        return 0
    if source == "slack":
        jsonl = _find_claude_jsonl(cfg, fm.get("conversation_uuid", ""))
        if jsonl is not None:
            print(str(jsonl))
        # source_id is "<thread_ts>@<channel>" when we captured the
        # channel; falls back to just thread_ts otherwise.
        thread_ts, _, channel = source_id.partition("@")
        if channel:
            # Standard Slack thread URL — workspace inferred at click time.
            ts_no_dot = thread_ts.replace(".", "")
            print(f"https://slack.com/archives/{channel}/p{ts_no_dot}")
        else:
            print(f"slack_thread_ts: {thread_ts}")
        return 0
    if source == "doc":
        print(source_id)  # relative path
        return 0
    print(f"unknown source: {source}")
    return 1


def _find_claude_jsonl(cfg: Config, session_uuid: str) -> Path | None:
    if not session_uuid:
        return None
    for s in cfg.sources:
        if s.kind == "claude_code":
            base = Path(s.fields.get("project_path", "")).expanduser()
            candidate = base / f"{session_uuid}.jsonl"
            if candidate.exists():
                return candidate
    return None


# ----- search --------------------------------------------------------------


def search(cfg: Config, store: Store, *, topic: str, mode: str = "auto") -> int:
    """Search the store.

    Modes:
        auto    — pick hybrid if rag is available + OPENAI_API_KEY set,
                  else fall back to grep. The recommended default.
        grep    — ripgrep over journal/ + archive/, ranked by recency.
        rag     — embedding-based semantic search (needs openai + sqlite-vec).
        hybrid  — grep + rag fused via Reciprocal Rank Fusion. Best when
                  both are available.
    """
    if mode == "auto":
        mode = "hybrid" if _rag_available() else "grep"

    if mode == "rag":
        try:
            from .rag import search as rag_search
        except ImportError:
            print("RAG mode requires the 'rag' extra: `uv sync --extra rag`",
                  flush=True)
            return 2
        return rag_search(cfg, store, topic=topic)

    if mode == "hybrid":
        return _hybrid_search(cfg, store, topic=topic)

    return _grep_search(store, topic=topic)


def _rag_available() -> bool:
    """True iff `--mode rag` would succeed (sqlite-vec + an embedder).

    Either fastembed (open-source, free) or OpenAI (paid + key) counts.
    Without sqlite-vec, RAG can't run at all.
    """
    try:
        import sqlite_vec  # noqa: F401
    except ImportError:
        return False
    from .embedders import pick_embedder
    return pick_embedder("auto") is not None


def _grep_hits(store: Store, topic: str, max_hits: int = 30) -> list[Path]:
    """Return path list (recency-sorted) of grep matches — no printing."""
    import subprocess
    cmd = [
        "rg", "-l", "-i", "--type-add", "md:*.md", "--type", "md",
        topic, str(store.paths.journal), str(store.paths.archive),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if result.returncode in (0, 1):
            paths = [Path(line) for line in result.stdout.splitlines() if line.strip()]
            paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return paths[:max_hits]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    # Python fallback
    pattern = re.compile(re.escape(topic), re.IGNORECASE)
    hits: list[Path] = []
    for d in (store.paths.journal, store.paths.archive):
        for f in d.glob("*.md"):
            try:
                if pattern.search(f.read_text(encoding="utf-8")):
                    hits.append(f)
            except OSError:
                continue
    hits.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return hits[:max_hits]


def _hybrid_search(cfg: Config, store: Store, *, topic: str, k: int = 30) -> int:
    """Combine grep + RAG via Reciprocal Rank Fusion (RRF).

    For each path appearing in either result set, score it as:
        score = sum( 1 / (rrf_k + rank) )  over both result lists
    where rrf_k is the standard RRF smoothing constant (60).
    Print top-k by combined score.
    """
    rrf_k = 60
    fused: dict[Path, float] = {}

    grep_paths = _grep_hits(store, topic, max_hits=k)
    for rank, p in enumerate(grep_paths):
        fused[p] = fused.get(p, 0.0) + 1.0 / (rrf_k + rank)

    try:
        from .rag import query_paths as rag_query
        rag_paths = rag_query(cfg, store, topic=topic, k=k)
    except (ImportError, RuntimeError):
        rag_paths = []
        print("(rag unavailable — falling back to grep only)")

    for rank, p in enumerate(rag_paths):
        fused[p] = fused.get(p, 0.0) + 1.0 / (rrf_k + rank)

    if not fused:
        print("no matches")
        return 0

    ranked = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)
    for p, score in ranked[:k]:
        print(f"{score:.4f}  {p.relative_to(store.root)}  — {_preview(p)}")
    return 0


def _grep_search(store: Store, *, topic: str, max_hits: int = 30) -> int:
    """Plain ripgrep across journal/ + archive/, ranked by recency."""
    cmd = [
        "rg", "-l", "-i", "--type-add", "md:*.md", "--type", "md",
        topic,
        str(store.paths.journal),
        str(store.paths.archive),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # Fallback to python-level grep.
        return _python_grep(store, topic, max_hits=max_hits)
    if result.returncode not in (0, 1):
        print(result.stderr, flush=True)
        return 1
    paths = [Path(line) for line in result.stdout.splitlines() if line.strip()]
    paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for p in paths[:max_hits]:
        print(f"{p.relative_to(store.root)}  — {_preview(p)}")
    if not paths:
        print("no matches")
    return 0


def _python_grep(store: Store, topic: str, *, max_hits: int) -> int:
    pattern = re.compile(re.escape(topic), re.IGNORECASE)
    hits: list[Path] = []
    for d in (store.paths.journal, store.paths.archive):
        for f in d.glob("*.md"):
            try:
                if pattern.search(f.read_text(encoding="utf-8")):
                    hits.append(f)
            except OSError:
                continue
    hits.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for p in hits[:max_hits]:
        print(f"{p.relative_to(store.root)}  — {_preview(p)}")
    if not hits:
        print("no matches")
    return 0


# ----- filename-pattern children ------------------------------------------


def _resolve(store: Store, path: Path) -> Path | None:
    """Resolve relative paths against store.root if needed.

    Tries a few sensible bases so the user can pass: absolute paths,
    paths relative to cwd, paths starting with ``memory/...``, or just
    a bare filename. Also strips a leading ``memory/`` if it would
    double up with store.root's basename.
    """
    p = Path(path)
    if p.is_absolute() and p.exists():
        return p

    # Strip a leading "memory/" if it matches store.root's basename.
    root_name = store.root.name
    parts = p.parts
    if parts and parts[0] == root_name:
        p_stripped = Path(*parts[1:])
    else:
        p_stripped = p

    candidates = [
        p,
        store.root / p_stripped,
        store.paths.briefing / p_stripped,
        store.paths.journal / p_stripped,
        store.paths.archive / p_stripped,
        # Final fallback: search by basename across the three subfolders.
    ]
    for c in candidates:
        if c.exists():
            return c
    # Basename search
    base = p.name
    for sub in (store.paths.briefing, store.paths.journal, store.paths.archive):
        for f in sub.rglob(base):
            return f
    return None


def _children_of(store: Store, path: Path) -> list[Path]:
    """Return the immediate children of *path* in the drill-down chain.

    Children of:
        monthly  YYYYMM-month-…  → weeklies in that month
        weekly   YYYYMMDD-week-… → dailies Mon..Sun
        daily    YYYYMMDD-daily- → shorts that day
        short    YYYYMMDD-HHmmss-<UUID> → archive/<same filename>
        archive  → []  (terminal; follow raw command for transcript)
    """
    name = path.name
    journal = store.paths.journal
    if MONTHLY_RE.match(name):
        m = MONTHLY_RE.match(name)
        ym = m.group(1)
        out: list[Path] = []
        for f in journal.glob("*-week-*.md"):
            wm = WEEKLY_RE.match(f.name)
            if not wm:
                continue
            monday = wm.group(1)
            if monday[:6] == ym:
                out.append(f)
        return sorted(out)
    if WEEKLY_RE.match(name):
        m = WEEKLY_RE.match(name)
        s = m.group(1)
        monday = date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        out = []
        for offset in range(7):
            d = monday + timedelta(days=offset)
            daily = store.daily_for_date(d.strftime("%Y%m%d"))
            if daily is not None:
                out.append(daily)
        return out
    if DAILY_RE.match(name):
        m = DAILY_RE.match(name)
        return store.shorts_for_date(m.group(1))
    if SHORT_RE.match(name):
        # If we're already looking at the archive entry, it's the terminal
        # node — further drilling goes via `raw` to the transcript.
        try:
            path.relative_to(store.paths.archive)
            return []  # already in archive/, no further children
        except ValueError:
            pass
        # In journal/ (or briefing/recent/) → child = archive counterpart.
        archive = store.paths.archive / name
        return [archive] if archive.exists() else []
    return []


def _preview(p: Path) -> str:
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return ""
    _, body = frontmatter.parse(text)
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("- "):
            return s[2:].strip()[:80]
        return s[:80]
    return ""

"""Docs source adapter — one-shot during backfill.

Each markdown file in the configured ``glob`` becomes one synthetic
conversation. Per design doc §3.4:

    conversation_uuid = uuid5(URL_NS, "doc:" + relative_path)
    source            = "doc"
    source_id         = relative path from repo root
    first_event_at    = git first-commit date of the file
    last_event_at     = git last-commit date at bootstrap time
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import NAMESPACE_URL, uuid5

from .base import SourceAdapter, SourceRecord


class DocsAdapter(SourceAdapter):
    kind = "docs"

    def __init__(self, glob_pattern: str, repo_root: Path | None = None):
        self.glob_pattern = glob_pattern
        # Anchor relative globs to the repo root if provided.
        self.repo_root = (
            Path(repo_root).expanduser().resolve() if repo_root else Path.cwd()
        )

    def discover(self) -> Iterator[SourceRecord]:
        for path in sorted(self.repo_root.glob(self.glob_pattern)):
            if not path.is_file():
                continue
            rec = self._record_for(path)
            if rec is not None:
                yield rec

    def _record_for(self, path: Path) -> SourceRecord | None:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        try:
            rel = path.relative_to(self.repo_root)
        except ValueError:
            rel = path
        rel_str = str(rel).replace("\\", "/")
        uid = str(uuid5(NAMESPACE_URL, f"doc:{rel_str}"))

        first_at, last_at = _git_commit_dates(path, self.repo_root)
        if first_at is None:
            # File not in git history yet — fall back to filesystem mtime.
            first_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            last_at = first_at

        return SourceRecord(
            conversation_uuid=uid,
            source="doc",
            source_id=rel_str,
            first_event_at=first_at,
            last_event_at=last_at,
            activity_mtime=path.stat().st_mtime,
            content=content,
            raw_path=path,
        )


def _git_commit_dates(
    path: Path, repo_root: Path
) -> tuple[datetime | None, datetime | None]:
    """Return (first_commit_date, last_commit_date) from git log.

    Returns (None, None) if the file isn't tracked or git isn't available.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "log", "--format=%cI", "--", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None, None
    if result.returncode != 0:
        return None, None
    lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    if not lines:
        return None, None
    try:
        last = datetime.fromisoformat(lines[0])
        first = datetime.fromisoformat(lines[-1])
    except ValueError:
        return None, None
    return first, last

"""Abstract base for source adapters."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class SourceRecord:
    """One discovered conversation source.

    ``conversation_uuid`` is derived per-source per design doc §3.4
    (full RFC-4122 UUID lowercase with hyphens).

    ``content`` is a human-readable transcript dump (chronological
    user/assistant messages). It's what the summarizer ingests.
    """

    conversation_uuid: str   # full UUID, RFC-4122 lowercase with hyphens
    source: str              # "claude_code" | "slack" | "doc"
    source_id: str           # JSONL UUID, Slack thread_ts, or doc relpath
    first_event_at: datetime
    last_event_at: datetime
    activity_mtime: float    # source file mtime; used for cascade decisions
    content: str             # transcript / doc body
    raw_path: Path           # for `tiger-memory raw <archive>` → where to point


class SourceAdapter(ABC):
    """Abstract interface every source adapter implements."""

    kind: str  # subclass sets this — "claude_code" | "slack_thread" | "docs"

    @abstractmethod
    def discover(self) -> Iterator[SourceRecord]:
        """Yield one SourceRecord per discovered conversation."""
        ...

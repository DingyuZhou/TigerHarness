"""Source adapters for tiger-memory.

A SourceAdapter knows how to discover conversation sources, derive
``conversation_uuid`` per-source rule (design doc §3.4), and emit a
``SourceRecord`` for the summarizer to ingest.

The concrete adapters:
    - ClaudeTranscriptAdapter (handles both ``claude_code`` and
      ``slack_thread`` kinds — same underlying JSONL transcript;
      origin determined by Slack-bridge threads.json reverse lookup).
    - DocsAdapter (one-shot for backfill).
    - JournalWorklogAdapter (per-persona journal memory: one synthetic
      conversation per ``(task, persona)`` from journal worklog files).

Synthetic source (auto-memory) is handled directly by the
bootstrap path; no adapter needed.
"""
from __future__ import annotations

from .base import SourceAdapter, SourceRecord
from .claude_transcript import ClaudeTranscriptAdapter
from .docs import DocsAdapter
from .journal_worklog import JournalWorklogAdapter

__all__ = [
    "ClaudeTranscriptAdapter",
    "DocsAdapter",
    "JournalWorklogAdapter",
    "SourceAdapter",
    "SourceRecord",
]

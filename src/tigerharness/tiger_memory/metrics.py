"""Lightweight rebuild instrumentation (P1 measurement hook).

A thin, billing-agnostic counter the lifecycle accumulates while it
processes per-session decisions, then stamps into ``state.json`` under
the ``metrics`` key. Its job is to make the P1 wins *provable*: the
transcript pre-filter (P1.1) should drop ``content_chars_raw`` ->
``content_chars_filtered`` measurably, and ``summarize_calls`` is the
per-rebuild baseline that the P1.3 call-collapse will later cut (3 calls
per new session today -> 1).

Deliberately minimal: no timing, no per-call cost (the summarizer's own
``cost_so_far`` already carries USD). Just counts, so it never grows into
a second cost model.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RebuildMetrics:
    """Per-rebuild counters, accumulated one session at a time."""

    sessions_processed: int = 0
    # Logical summarize calls issued: 3 for a new/resummarized session
    # (short + detailed + must-memorize extractor), 2 for an addendum
    # (short + extractor). The P1.3 collapse moves this number.
    summarize_calls: int = 0
    # Transcript input chars, before vs. after the pre-filter. The gap is
    # the P1.1 win.
    content_chars_raw: int = 0
    content_chars_filtered: int = 0
    # Why the rebuild stopped processing sessions early, if it did:
    # "session_cap" / "usd_cap" (P1.2). ``None`` == ran to completion.
    # Deferred sessions need no counter -- they simply stay unsummarized
    # and are re-discovered on the next rebuild.
    stopped_reason: str | None = None

    def record_session(
        self, *, chars_raw: int, chars_filtered: int, calls: int
    ) -> None:
        self.sessions_processed += 1
        self.content_chars_raw += chars_raw
        self.content_chars_filtered += chars_filtered
        self.summarize_calls += calls

    def note_capped(self, reason: str) -> None:
        """Mark that a cost/scope cap stopped the rebuild early."""
        self.stopped_reason = reason

    @property
    def chars_saved(self) -> int:
        return self.content_chars_raw - self.content_chars_filtered

    @property
    def reduction_pct(self) -> float:
        """Pre-filter reduction as a percentage (0.0 when nothing seen)."""
        if self.content_chars_raw <= 0:
            return 0.0
        return round(100.0 * self.chars_saved / self.content_chars_raw, 1)

    def as_dict(self) -> dict:
        """JSON-serialisable snapshot for ``state.json``."""
        return {
            "sessions_processed": self.sessions_processed,
            "summarize_calls": self.summarize_calls,
            "content_chars_raw": self.content_chars_raw,
            "content_chars_filtered": self.content_chars_filtered,
            "content_chars_saved": self.chars_saved,
            "prefilter_reduction_pct": self.reduction_pct,
            "stopped_reason": self.stopped_reason,
        }

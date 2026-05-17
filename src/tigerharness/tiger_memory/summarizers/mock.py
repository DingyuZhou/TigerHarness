"""Deterministic mock summarizer for tests + dry-run mode.

Does no model calls. Returns a predictable template body so tests
can assert on it without flakiness or cost.
"""
from __future__ import annotations

import hashlib

from .base import Summarizer


class MockSummarizer(Summarizer):
    name = "mock"
    version = "v1"

    def __init__(self) -> None:
        super().__init__()

    def summarize(self, *, prompt: str, max_words: int) -> str:
        # Deterministic hash so the same input → same output (test stability).
        h = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
        # Cap at max_words — produce roughly a 5-bullet body.
        bullets = [
            f"- MockSummary[{h}] bullet {i + 1}"
            for i in range(min(5, max_words // 8))
        ]
        return "\n".join(bullets) + "\n"

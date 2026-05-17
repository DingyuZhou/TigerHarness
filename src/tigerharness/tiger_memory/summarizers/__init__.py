"""Summarizer backends for tiger-memory.

A Summarizer turns a conversation transcript (or a list of summaries)
into a markdown body, capped to a word budget. The default backend
talks to Anthropic via agent-sdk's claude_p backend; tests use a
deterministic mock.
"""
from __future__ import annotations

from .base import Summarizer, SummarizerError
from .anthropic import AnthropicSummarizer
from .mock import MockSummarizer

__all__ = [
    "AnthropicSummarizer",
    "MockSummarizer",
    "Summarizer",
    "SummarizerError",
]

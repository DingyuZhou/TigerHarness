"""Abstract Summarizer interface.

The interface is intentionally narrow:
    summarize(prompt_template, context_text, max_words) → body markdown

The caller (lifecycle.py, must_memorize.py, briefing.py) loads the
prompt template, fills it with whatever it needs, and asks the
summarizer for a capped response. Summarizers are stateless.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class SummarizerError(RuntimeError):
    """Raised when a summarizer call hard-fails (after retries)."""


class Summarizer(ABC):
    name: str       # e.g. "anthropic"
    version: str    # e.g. "v1"

    def __init__(self) -> None:
        # Running tally of real spend (USD). Backends update this from
        # the API response after each successful call; callers can read
        # ``summarizer.cost_so_far`` to see real cost across many calls.
        self.cost_so_far: float = 0.0

    @property
    def tag(self) -> str:
        return f"{self.name}@{self.version}"

    @abstractmethod
    def summarize(
        self,
        *,
        prompt: str,
        max_words: int,
    ) -> str:
        """Run the summarizer with *prompt* and return body markdown."""
        ...

    def cost_estimate_usd(self, *, prompt_tokens: int, output_tokens: int) -> float:
        """Override for budgeting before a call. Default returns 0."""
        return 0.0

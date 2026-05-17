"""Anthropic summarizer — uses agent-sdk's claude_p backend.

This is the production summarizer. Tests/dry-run should use
MockSummarizer to avoid spend.

The summarizer runs each prompt as a one-shot (no session resume):
load prompt → send → read final_output. Retries are handled by
``agent_sdk.run_with_retry`` (3 attempts, exponential backoff —
same policy slack-bridge already uses).
"""
from __future__ import annotations

import asyncio
import os

from tigerharness.agent_sdk import AgentConfig, get_backend, run_with_retry

from .base import Summarizer, SummarizerError


# Public price table — refresh as Anthropic publishes new numbers.
# Used only for after-the-fact estimation; not authoritative.
_PRICES_PER_M_TOKENS = {
    "claude-opus-4-7":   {"input": 15.0, "output": 75.0},
    "claude-sonnet-4-6": {"input":  3.0, "output": 15.0},
    "claude-haiku-4-5":  {"input":  0.8, "output":  4.0},
}


class AnthropicSummarizer(Summarizer):
    name = "anthropic"
    version = "v1"

    def __init__(
        self,
        *,
        model: str,
        prompts_dir: str = "default/v1",
        max_attempts: int = 3,
        prices_per_m_tokens: dict | None = None,
    ):
        super().__init__()
        self.model = model
        self.prompts_dir = prompts_dir
        self.max_attempts = max_attempts
        self.prices = prices_per_m_tokens or _PRICES_PER_M_TOKENS
        # Lazy backend init so import doesn't require claude CLI on path.
        self._backend = None

    def _get_backend(self):
        if self._backend is None:
            self._backend = get_backend("claude_p")
        return self._backend

    def summarize(self, *, prompt: str, max_words: int) -> str:
        # Hard-truncate the prompt's request hint to *max_words*.
        full_prompt = (
            f"{prompt}\n\n"
            f"Respond with at most {max_words} words. "
            f"Return ONLY the markdown body — no preamble, no surrounding "
            f"code fences."
        )
        cfg = AgentConfig(
            name="tiger-memory-summarizer",
            instructions=(
                "You are an internal summarization function. Produce "
                "concise, factual summaries. No preamble, no commentary "
                "about the task — just the requested markdown body."
            ),
            model=self.model,
        )

        try:
            result = asyncio.run(
                run_with_retry(
                    self._get_backend(),
                    cfg,
                    full_prompt,
                    max_attempts=self.max_attempts,
                    label="tiger-memory-summarize",
                )
            )
        except Exception as exc:  # noqa: BLE001
            raise SummarizerError(f"claude_p backend failed: {exc}") from exc

        # Capture real cost from the agent-sdk Result. The slack-bridge
        # already proves this field is reliable.
        if getattr(result, "cost_usd", None) is not None:
            self.cost_so_far += float(result.cost_usd)

        body = result.final_output or ""
        # Strip code-fence wrappers if the model wraps the body.
        body = _strip_codefence(body)
        return body.strip() + "\n"

    def cost_estimate_usd(self, *, prompt_tokens: int, output_tokens: int) -> float:
        prices = self.prices.get(self.model)
        if not prices:
            return 0.0
        return (
            prompt_tokens / 1_000_000 * prices["input"]
            + output_tokens / 1_000_000 * prices["output"]
        )


def _strip_codefence(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        # Drop opening fence (with optional language tag)
        first_nl = s.find("\n")
        if first_nl > 0:
            s = s[first_nl + 1 :]
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()

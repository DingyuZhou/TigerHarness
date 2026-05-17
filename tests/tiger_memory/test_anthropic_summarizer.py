"""Tests for AnthropicSummarizer with mocked agent-sdk backend."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tigerharness.tiger_memory.summarizers.anthropic import (
    AnthropicSummarizer,
    _strip_codefence,
)
from tigerharness.tiger_memory.summarizers.base import SummarizerError


class TestStripCodefence:
    def test_no_fence(self):
        assert _strip_codefence("hello world") == "hello world"

    def test_with_fence(self):
        text = "```markdown\n# Title\nBody here.\n```"
        assert _strip_codefence(text) == "# Title\nBody here."

    def test_fence_no_language(self):
        text = "```\nplain content\n```"
        assert _strip_codefence(text) == "plain content"

    def test_fence_with_whitespace(self):
        text = "  ```python\ncode\n```  "
        assert _strip_codefence(text) == "code"

    def test_single_line_fence(self):
        text = "```"
        result = _strip_codefence(text)
        assert "```" not in result or result == ""


class TestAnthropicSummarizer:
    def test_init(self):
        s = AnthropicSummarizer(model="claude-sonnet-4-6")
        assert s.model == "claude-sonnet-4-6"
        assert s.max_attempts == 3
        assert s.cost_so_far == 0.0

    def test_get_backend_lazy(self):
        s = AnthropicSummarizer(model="claude-sonnet-4-6")
        assert s._backend is None
        with patch("tigerharness.tiger_memory.summarizers.anthropic.get_backend") as mock_gb:
            mock_gb.return_value = MagicMock()
            backend = s._get_backend()
            mock_gb.assert_called_once_with("claude_p")
            # Second call should reuse
            backend2 = s._get_backend()
            assert backend is backend2
            assert mock_gb.call_count == 1

    def test_summarize_success(self):
        s = AnthropicSummarizer(model="claude-sonnet-4-6")
        mock_backend = MagicMock()
        mock_result = MagicMock()
        mock_result.final_output = "Summary of the conversation."
        mock_result.cost_usd = 0.005
        s._backend = mock_backend

        with patch("tigerharness.tiger_memory.summarizers.anthropic.asyncio") as mock_asyncio:
            mock_asyncio.run.return_value = mock_result
            body = s.summarize(prompt="Summarize this.", max_words=100)
            assert body == "Summary of the conversation.\n"
            assert s.cost_so_far == 0.005

    def test_summarize_strips_codefence(self):
        s = AnthropicSummarizer(model="claude-sonnet-4-6")
        mock_result = MagicMock()
        mock_result.final_output = "```markdown\n# Summary\nContent.\n```"
        mock_result.cost_usd = None
        s._backend = MagicMock()

        with patch("tigerharness.tiger_memory.summarizers.anthropic.asyncio") as mock_asyncio:
            mock_asyncio.run.return_value = mock_result
            body = s.summarize(prompt="Summarize.", max_words=50)
            assert body.startswith("# Summary")
            assert "```" not in body

    def test_summarize_backend_failure(self):
        s = AnthropicSummarizer(model="claude-sonnet-4-6")
        s._backend = MagicMock()

        with patch("tigerharness.tiger_memory.summarizers.anthropic.asyncio") as mock_asyncio:
            mock_asyncio.run.side_effect = RuntimeError("connection failed")
            with pytest.raises(SummarizerError, match="claude_p backend failed"):
                s.summarize(prompt="Summarize.", max_words=50)

    def test_summarize_empty_output(self):
        s = AnthropicSummarizer(model="claude-sonnet-4-6")
        mock_result = MagicMock()
        mock_result.final_output = None
        mock_result.cost_usd = 0.0
        s._backend = MagicMock()

        with patch("tigerharness.tiger_memory.summarizers.anthropic.asyncio") as mock_asyncio:
            mock_asyncio.run.return_value = mock_result
            body = s.summarize(prompt="Summarize.", max_words=50)
            assert body == "\n"

    def test_cost_estimate(self):
        s = AnthropicSummarizer(model="claude-sonnet-4-6")
        cost = s.cost_estimate_usd(prompt_tokens=1_000_000, output_tokens=100_000)
        # sonnet: input=$3/M, output=$15/M
        assert cost == pytest.approx(3.0 + 1.5, rel=0.01)

    def test_cost_estimate_unknown_model(self):
        s = AnthropicSummarizer(model="unknown-model")
        cost = s.cost_estimate_usd(prompt_tokens=1_000_000, output_tokens=100_000)
        assert cost == 0.0

    def test_tag(self):
        s = AnthropicSummarizer(model="claude-sonnet-4-6")
        assert s.tag == "anthropic@v1"

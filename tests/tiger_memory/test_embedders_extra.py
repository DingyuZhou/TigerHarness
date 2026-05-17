"""Tests for embedders module — pick_embedder, chunks, and error paths."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tigerharness.tiger_memory.embedders import (
    Embedder,
    chunks,
    pick_embedder,
)


class TestChunks:
    def test_even_split(self):
        result = list(chunks([1, 2, 3, 4], 2))
        assert result == [[1, 2], [3, 4]]

    def test_uneven_split(self):
        result = list(chunks([1, 2, 3, 4, 5], 2))
        assert result == [[1, 2], [3, 4], [5]]

    def test_single_chunk(self):
        result = list(chunks([1, 2, 3], 10))
        assert result == [[1, 2, 3]]

    def test_empty(self):
        result = list(chunks([], 5))
        assert result == []


class TestPickEmbedder:
    def test_unknown_prefer_raises(self):
        with pytest.raises(ValueError, match="unknown embedder"):
            pick_embedder("nonexistent")

    def test_auto_no_deps_returns_none(self):
        with patch("tigerharness.tiger_memory.embedders.os.environ", {"no_key": "1"}):
            with patch("tigerharness.tiger_memory.embedders.FastEmbedEmbedder",
                       side_effect=ImportError("no fastembed")):
                result = pick_embedder("auto")
                assert result is None

    def test_auto_with_fastembed(self):
        mock_embedder = MagicMock(spec=Embedder)
        with patch("tigerharness.tiger_memory.embedders.os.environ", {}):
            with patch("tigerharness.tiger_memory.embedders.FastEmbedEmbedder",
                       return_value=mock_embedder):
                result = pick_embedder("auto")
                assert result is mock_embedder

    def test_auto_prefers_openai_when_key_set(self):
        mock_openai = MagicMock(spec=Embedder)
        with patch("tigerharness.tiger_memory.embedders.os.environ",
                   {"OPENAI_API_KEY": "sk-test"}):
            with patch("tigerharness.tiger_memory.embedders.OpenAIEmbedder",
                       return_value=mock_openai):
                result = pick_embedder("auto")
                assert result is mock_openai

    def test_auto_fallback_to_fastembed_when_openai_fails(self):
        mock_fe = MagicMock(spec=Embedder)
        with patch("tigerharness.tiger_memory.embedders.os.environ",
                   {"OPENAI_API_KEY": "sk-test"}):
            with patch("tigerharness.tiger_memory.embedders.OpenAIEmbedder",
                       side_effect=ImportError("no openai")):
                with patch("tigerharness.tiger_memory.embedders.FastEmbedEmbedder",
                           return_value=mock_fe):
                    result = pick_embedder("auto")
                    assert result is mock_fe

    def test_force_fastembed(self):
        mock_fe = MagicMock(spec=Embedder)
        with patch("tigerharness.tiger_memory.embedders.FastEmbedEmbedder",
                   return_value=mock_fe):
            result = pick_embedder("fastembed")
            assert result is mock_fe

    def test_force_openai(self):
        mock_openai = MagicMock(spec=Embedder)
        with patch("tigerharness.tiger_memory.embedders.OpenAIEmbedder",
                   return_value=mock_openai):
            result = pick_embedder("openai")
            assert result is mock_openai

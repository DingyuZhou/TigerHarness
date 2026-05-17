"""Tests for summarizer interfaces."""
from __future__ import annotations

from tigerharness.tiger_memory.summarizers import MockSummarizer


def test_mock_is_deterministic() -> None:
    s = MockSummarizer()
    out1 = s.summarize(prompt="hello world", max_words=400)
    out2 = s.summarize(prompt="hello world", max_words=400)
    assert out1 == out2


def test_mock_different_inputs_different_outputs() -> None:
    s = MockSummarizer()
    a = s.summarize(prompt="A", max_words=400)
    b = s.summarize(prompt="B", max_words=400)
    assert a != b


def test_mock_word_cap_respected_roughly() -> None:
    s = MockSummarizer()
    short = s.summarize(prompt="x", max_words=20)
    long = s.summarize(prompt="x", max_words=200)
    assert len(short.split()) <= len(long.split())


def test_summarizer_tag() -> None:
    s = MockSummarizer()
    assert s.tag == "mock@v1"

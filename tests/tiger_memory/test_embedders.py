"""Tests for embedder selection. Mocks the optional deps so tests
don't require fastembed or openai to be installed."""
from __future__ import annotations

import sys
import types

import pytest


def test_pick_embedder_returns_none_when_nothing_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No fastembed, no openai key — pick_embedder returns None."""
    # Stash any real modules and replace with raising stubs.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setitem(sys.modules, "fastembed", None)  # ImportError on access
    monkeypatch.setitem(sys.modules, "openai", None)
    from tigerharness.tiger_memory.embedders import pick_embedder
    assert pick_embedder("auto") is None


def test_pick_embedder_prefers_openai_when_key_and_deps_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If OPENAI_API_KEY is set and openai is importable, prefer it."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")

    # Stub the openai module + client so we don't actually call the API.
    fake_openai_mod = types.ModuleType("openai")

    class _FakeClient:
        def __init__(self): ...

    fake_openai_mod.OpenAI = _FakeClient
    monkeypatch.setitem(sys.modules, "openai", fake_openai_mod)

    from tigerharness.tiger_memory.embedders import OpenAIEmbedder, pick_embedder
    e = pick_embedder("auto")
    assert isinstance(e, OpenAIEmbedder)
    assert e.dim == 1536


def test_pick_embedder_falls_back_to_fastembed_when_no_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No key → try fastembed."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    # Stub fastembed with a TextEmbedding that returns fixed-dim vectors.
    fake_fe = types.ModuleType("fastembed")

    class _FakeText:
        def __init__(self, model_name): self.model_name = model_name
        def embed(self, texts):
            for _ in texts:
                yield [0.0] * 384  # bge-small-en is 384-dim

    fake_fe.TextEmbedding = _FakeText
    monkeypatch.setitem(sys.modules, "fastembed", fake_fe)

    from tigerharness.tiger_memory.embedders import FastEmbedEmbedder, pick_embedder
    e = pick_embedder("auto")
    assert isinstance(e, FastEmbedEmbedder)
    assert e.dim == 384


def test_force_fastembed_raises_when_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "fastembed", None)
    from tigerharness.tiger_memory.embedders import pick_embedder
    with pytest.raises(ImportError, match="fastembed"):
        pick_embedder("fastembed")


def test_force_openai_raises_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    fake_openai_mod = types.ModuleType("openai")

    class _FakeClient:
        def __init__(self): ...

    fake_openai_mod.OpenAI = _FakeClient
    monkeypatch.setitem(sys.modules, "openai", fake_openai_mod)
    from tigerharness.tiger_memory.embedders import pick_embedder
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        pick_embedder("openai")

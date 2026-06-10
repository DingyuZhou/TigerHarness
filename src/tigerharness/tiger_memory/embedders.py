"""Pluggable embedder backends for RAG search.

Two backends ship with tiger-memory:

- **FastEmbedEmbedder** (default, open-source) — ONNX-based, runs
  locally on CPU. No API key, no per-query cost. Model files download
  on first use (~100 MB for `BAAI/bge-small-en-v1.5`, the default).
  Install with ``uv sync --extra rag-local``.

- **OpenAIEmbedder** (optional, paid) — OpenAI's text-embedding-3-small.
  Requires ``OPENAI_API_KEY``. ~$0.02/M input tokens.
  Install with ``uv sync --extra rag-openai``.

Selection precedence in ``pick_embedder()``:
    1. If ``OPENAI_API_KEY`` is set AND openai is installed → OpenAI.
    2. Else if fastembed is installed → FastEmbed.
    3. Else → None (caller should raise / fall back).
"""
from __future__ import annotations

import logging

import os
from abc import ABC, abstractmethod
from typing import Iterable

log = logging.getLogger("tigerharness.tiger_memory.embedders")


class Embedder(ABC):
    """Abstract embedder. Returns one fixed-dim vector per input string."""

    name: str       # e.g. "fastembed/bge-small-en-v1.5"
    dim: int        # vector dimension

    @abstractmethod
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed *texts* and return one vector per input, in order."""
        ...

    def embed_one(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]


# ----- FastEmbed (open-source, default) -------------------------------------


class FastEmbedEmbedder(Embedder):
    """Local ONNX-based embedder. No network after first model download.

    Default model: ``BAAI/bge-small-en-v1.5`` (384-dim). Competitive
    with OpenAI text-embedding-3-small on MTEB retrieval benchmarks at
    roughly equivalent quality for our use case (hybrid retrieval over
    English summaries).
    """

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5"):
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise ImportError(
                "fastembed not installed. Install with "
                "`uv sync --extra rag-local`."
            ) from exc
        self.name = f"fastembed/{model_name}"
        self._model = TextEmbedding(model_name=model_name)
        # Probe dimension once by embedding a short string.
        probe = next(self._model.embed(["hello"]))
        self.dim = len(probe)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._model.embed(texts)]


# ----- OpenAI (paid, optional) ---------------------------------------------


class OpenAIEmbedder(Embedder):
    """OpenAI text-embedding-3-small (1536-dim). Requires OPENAI_API_KEY."""

    def __init__(self, model: str = "text-embedding-3-small"):
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ImportError(
                "openai not installed. Install with "
                "`uv sync --extra rag-openai`."
            ) from exc
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError(
                "OpenAIEmbedder requires OPENAI_API_KEY in env."
            )
        self.name = f"openai/{model}"
        self.model = model
        self._client = OpenAI()
        self.dim = 1536 if "small" in model else 3072

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        resp = self._client.embeddings.create(model=self.model, input=texts)
        return [list(e.embedding) for e in resp.data]


# ----- Factory --------------------------------------------------------------


def pick_embedder(prefer: str = "auto") -> Embedder | None:
    """Choose the best available embedder.

    *prefer* values:
        "auto"      — OpenAI if key+deps available, else FastEmbed,
                      else None (default).
        "fastembed" — force FastEmbed; ImportError if not installed.
        "openai"    — force OpenAI; ImportError or RuntimeError if not
                      installed / no key.
    """
    if prefer == "fastembed":
        return FastEmbedEmbedder()
    if prefer == "openai":
        return OpenAIEmbedder()

    if prefer != "auto":
        raise ValueError(f"unknown embedder preference: {prefer!r}")

    # auto: prefer OpenAI quality if key+deps present
    if os.environ.get("OPENAI_API_KEY"):
        try:
            return OpenAIEmbedder()
        except (ImportError, RuntimeError):
            pass
    try:
        return FastEmbedEmbedder()
    except ImportError:
        return None


def chunks(seq: list, n: int) -> Iterable[list]:
    """Yield successive n-sized chunks from seq."""
    for i in range(0, len(seq), n):
        yield seq[i : i + n]

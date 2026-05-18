"""Summarizer backends for tiger-memory.

A Summarizer turns a conversation transcript (or a list of summaries)
into a markdown body, capped to a word budget. The default backend
talks to Anthropic via agent-sdk's claude_p backend; tests use a
deterministic mock.

Adding a new vendor
-------------------

Tiger-memory's summarizer is vendor-agnostic by design. Plug in a new
LLM provider with three steps:

1. Implement the ``Summarizer`` ABC (see ``base.py``) -- a subclass
   that knows how to call your vendor's API given a prompt + word cap.
2. Write a factory function ``(SummarizerConfig) -> Summarizer`` that
   reads ``cfg.model`` (and any other fields your vendor needs) and
   constructs your impl.
3. Call ``register_summarizer("yourvendor", factory)`` at import time
   (anywhere the import path runs before ``lifecycle._build_summarizer``
   is called -- typically your project's top-level ``__init__.py``).

Then point ``summarizer.backend: yourvendor`` in any persona's
``tiger-memory.config.yaml``.

The Anthropic backend is pre-registered as ``"anthropic"``. The mock
summarizer is reserved for the ``--mock`` CLI flag and isn't registered
under any user-visible name.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from .base import Summarizer, SummarizerError
from .anthropic import AnthropicSummarizer
from .mock import MockSummarizer

if TYPE_CHECKING:
    from ..config import SummarizerConfig


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# name -> factory(SummarizerConfig) -> Summarizer
# Populated at module load with the Anthropic default; downstream code
# can call ``register_summarizer`` to add more.
_REGISTRY: dict[str, Callable[["SummarizerConfig"], Summarizer]] = {}


def register_summarizer(
    name: str,
    factory: Callable[["SummarizerConfig"], Summarizer],
) -> None:
    """Register a summarizer factory under *name*.

    Subsequent calls with the same name override the previous entry --
    that lets tests stub a fake backend in place of "anthropic" without
    a separate registration step.
    """
    if not name or not name.strip():
        raise ValueError("summarizer name must be non-empty")
    _REGISTRY[name.strip()] = factory


def get_summarizer(
    name: str, cfg: "SummarizerConfig",
) -> Summarizer:
    """Look up the factory for *name* and build a Summarizer from *cfg*.

    Raises ``SummarizerError`` with the list of registered backends if
    *name* isn't found -- callers see exactly which names are available.
    Also raises ``SummarizerError`` if the factory returns something
    that isn't a ``Summarizer`` (caught early -- otherwise the failure
    surfaces as a confusing ``AttributeError`` inside ``.summarize()``
    later).
    """
    factory = _REGISTRY.get(name)
    if factory is None:
        available = sorted(_REGISTRY.keys())
        raise SummarizerError(
            f"unknown summarizer backend: {name!r}. "
            f"Registered backends: {available or '(none)'}. "
            f"Register a new one with `register_summarizer()` -- see "
            f"`tigerharness.tiger_memory.summarizers` module docstring."
        )
    instance = factory(cfg)
    if not isinstance(instance, Summarizer):
        raise SummarizerError(
            f"summarizer factory for {name!r} returned "
            f"{type(instance).__name__}, not a Summarizer subclass. "
            f"Check your factory function -- it must construct a class "
            f"that inherits from `tigerharness.tiger_memory.summarizers.Summarizer`."
        )
    return instance


def registered_summarizers() -> list[str]:
    """Return the list of registered backend names (sorted)."""
    return sorted(_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Default backend: Anthropic via agent-sdk's claude_p
# ---------------------------------------------------------------------------

def _build_anthropic(cfg: "SummarizerConfig") -> Summarizer:
    return AnthropicSummarizer(
        model=cfg.model,
        prompts_dir=cfg.prompts,
        max_attempts=cfg.retry_max_attempts,
    )


register_summarizer("anthropic", _build_anthropic)


__all__ = [
    "AnthropicSummarizer",
    "MockSummarizer",
    "Summarizer",
    "SummarizerError",
    "register_summarizer",
    "get_summarizer",
    "registered_summarizers",
]

"""Tests for the summarizer registry.

The registry decouples ``_build_summarizer`` from any specific vendor.
``anthropic`` is pre-registered as the default; downstream code can
plug in new vendors with ``register_summarizer()``.
"""
from __future__ import annotations

import pytest

from tigerharness.tiger_memory.config import SummarizerConfig
from tigerharness.tiger_memory.summarizers import (
    Summarizer,
    SummarizerError,
    get_summarizer,
    register_summarizer,
    registered_summarizers,
)


class _FakeSummarizer(Summarizer):
    """Minimal Summarizer impl for registry tests."""
    name = "fake-vendor"
    version = "v0"

    def summarize(self, *, prompt: str, max_words: int) -> str:
        return f"fake-summary({prompt[:20]!r}, words<={max_words})"


def _sample_cfg(backend: str = "anthropic") -> SummarizerConfig:
    return SummarizerConfig(
        backend=backend, model="claude-sonnet-4-6",
        prompts="default/v1", retry_max_attempts=3,
    )


class TestAnthropicPreregistered:
    def test_anthropic_is_registered_by_default(self):
        """The Anthropic backend must work out of the box -- no manual
        ``register_summarizer`` call needed by users."""
        assert "anthropic" in registered_summarizers()

    def test_get_anthropic_returns_anthropic_summarizer(self):
        s = get_summarizer("anthropic", _sample_cfg())
        assert s.name == "anthropic"
        assert s.version == "v1"


class TestRegisterSummarizer:
    def test_register_then_get_returns_factory_output(self):
        def factory(cfg: SummarizerConfig) -> Summarizer:
            return _FakeSummarizer()
        register_summarizer("fake-vendor", factory)
        try:
            s = get_summarizer("fake-vendor", _sample_cfg(backend="fake-vendor"))
            assert isinstance(s, _FakeSummarizer)
            assert "fake-vendor" in registered_summarizers()
        finally:
            # Clean up so other tests don't see this fake registration.
            from tigerharness.tiger_memory.summarizers import _REGISTRY
            _REGISTRY.pop("fake-vendor", None)

    def test_register_with_whitespace_in_name_strips(self):
        def factory(cfg: SummarizerConfig) -> Summarizer:
            return _FakeSummarizer()
        register_summarizer("  spaced  ", factory)
        try:
            assert "spaced" in registered_summarizers()
            s = get_summarizer("spaced", _sample_cfg(backend="spaced"))
            assert isinstance(s, _FakeSummarizer)
        finally:
            from tigerharness.tiger_memory.summarizers import _REGISTRY
            _REGISTRY.pop("spaced", None)

    def test_re_registering_same_name_overrides(self):
        """Useful for tests that want to stub a fake in place of an
        existing vendor without juggling the registry by hand."""
        def factory_a(cfg: SummarizerConfig) -> Summarizer:
            return _FakeSummarizer()
        register_summarizer("overridable", factory_a)
        try:
            class _OtherFake(Summarizer):
                name = "other"
                version = "v0"
                def summarize(self, *, prompt: str, max_words: int) -> str:
                    return "other"
            def factory_b(cfg: SummarizerConfig) -> Summarizer:
                return _OtherFake()
            register_summarizer("overridable", factory_b)
            s = get_summarizer("overridable", _sample_cfg(backend="overridable"))
            assert s.name == "other"
        finally:
            from tigerharness.tiger_memory.summarizers import _REGISTRY
            _REGISTRY.pop("overridable", None)

    def test_empty_name_raises(self):
        with pytest.raises(ValueError, match="must be non-empty"):
            register_summarizer("", lambda c: _FakeSummarizer())

    def test_whitespace_only_name_raises(self):
        with pytest.raises(ValueError, match="must be non-empty"):
            register_summarizer("   ", lambda c: _FakeSummarizer())


class TestGetSummarizerErrors:
    def test_unknown_backend_lists_registered_options(self):
        """Error message must tell the user which backends ARE
        available, so they can pick one or know what to install."""
        with pytest.raises(SummarizerError) as excinfo:
            get_summarizer("nonexistent-vendor", _sample_cfg())
        msg = str(excinfo.value)
        assert "nonexistent-vendor" in msg
        # The list of registered backends must appear so the user can
        # see what's available.
        assert "anthropic" in msg
        assert "register_summarizer" in msg

    def test_unknown_backend_with_empty_registry_message(self, monkeypatch):
        """Edge case: registry is empty (shouldn't happen in practice
        because Anthropic is pre-registered, but we shouldn't crash)."""
        from tigerharness.tiger_memory.summarizers import _REGISTRY
        monkeypatch.setattr(
            "tigerharness.tiger_memory.summarizers._REGISTRY", {}
        )
        with pytest.raises(SummarizerError, match="\\(none\\)"):
            get_summarizer("anything", _sample_cfg())

    def test_factory_returning_wrong_type_is_caught_early(self):
        """If someone registers a factory that returns a non-Summarizer
        (typo, refactor missed a return type change, etc.), the failure
        must surface immediately at lookup time -- not as a confusing
        AttributeError deep inside a later summarize() call."""

        class _NotASummarizer:
            """No inheritance from Summarizer; no .summarize method."""
            name = "fake"
            version = "v0"

        def factory(cfg: SummarizerConfig):  # type: ignore[no-untyped-def]
            return _NotASummarizer()

        register_summarizer("bogus", factory)
        try:
            with pytest.raises(SummarizerError) as excinfo:
                get_summarizer("bogus", _sample_cfg(backend="bogus"))
            msg = str(excinfo.value)
            assert "_NotASummarizer" in msg
            assert "Summarizer subclass" in msg
        finally:
            from tigerharness.tiger_memory.summarizers import _REGISTRY
            _REGISTRY.pop("bogus", None)

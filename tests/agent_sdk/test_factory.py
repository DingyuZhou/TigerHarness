"""Tests for ``agent_sdk.factory``."""

from __future__ import annotations

from typing import Any

import pytest

from tigerharness.agent_sdk import (
    AgentBackend,
    AgentSDKError,
    BackendNotImplementedError,
    get_backend,
    list_backends,
    register_backend,
)
from tigerharness.agent_sdk.backends.claude_p import ClaudePBackend


class TestGetBackend:
    def test_default_is_claude_p(self) -> None:
        b = get_backend()
        assert isinstance(b, ClaudePBackend)

    def test_explicit_claude_p(self) -> None:
        b = get_backend("claude_p")
        assert isinstance(b, ClaudePBackend)

    def test_kwargs_forwarded(self) -> None:
        b = get_backend("claude_p", cli="/tmp/whatever")
        assert isinstance(b, ClaudePBackend)
        assert b.cli == "/tmp/whatever"

    def test_anthropic_sdk_constructs(self) -> None:
        """anthropic_sdk is no longer a stub -- it instantiates as long as
        ``claude-agent-sdk`` is installed (test env has it via the
        ``anthropic`` optional extra)."""
        backend = get_backend("anthropic_sdk")
        assert type(backend).__name__ == "AnthropicSDKBackend"

    def test_openai_sdk_stub_raises(self) -> None:
        with pytest.raises(BackendNotImplementedError):
            get_backend("openai_sdk")

    def test_unknown_name_raises(self) -> None:
        with pytest.raises(AgentSDKError) as exc_info:
            get_backend("does_not_exist")
        assert "does_not_exist" in str(exc_info.value)
        # Error mentions registered backends
        assert "claude_p" in str(exc_info.value)


class TestListBackends:
    def test_built_ins_present(self) -> None:
        names = list_backends()
        assert "claude_p" in names
        assert "anthropic_sdk" in names
        assert "openai_sdk" in names

    def test_sorted(self) -> None:
        names = list_backends()
        assert names == sorted(names)


class TestRegisterBackend:
    """Use ``isolated_registry`` so any registry mutation is rolled back even
    if the test fails mid-way."""

    def test_register_and_get(self, isolated_registry: Any) -> None:
        class FakeBackend:
            def __init__(self, **kw: Any) -> None:
                self.kw = kw

            async def run(self, *a: Any, **kw: Any) -> Any: ...
            def run_stream(self, *a: Any, **kw: Any) -> Any: ...
            async def open_session(self, **kw: Any) -> Any: ...

        register_backend("fake_test", lambda **kw: FakeBackend(**kw))
        b = get_backend("fake_test", custom="value")
        assert isinstance(b, FakeBackend)
        assert b.kw == {"custom": "value"}

    def test_overwrite_existing(self, isolated_registry: Any) -> None:
        class Replacement:
            def __init__(self, **kw: Any) -> None:
                self.replaced = True

            async def run(self, *a: Any, **kw: Any) -> Any: ...
            def run_stream(self, *a: Any, **kw: Any) -> Any: ...
            async def open_session(self, **kw: Any) -> Any: ...

        register_backend("claude_p", lambda **kw: Replacement(**kw))
        b = get_backend("claude_p")
        assert isinstance(b, Replacement)
        assert b.replaced is True

    def test_registry_is_restored_after_isolated_test(
        self, isolated_registry: Any
    ) -> None:
        # Sanity check: verify the fixture itself rolls back. This test
        # mutates inside the fixture, then a separate top-level assertion
        # below confirms the rollback at teardown.
        register_backend("ephemeral", lambda **kw: object())  # type: ignore[arg-type, return-value]
        assert "ephemeral" in list_backends()


def test_registry_clean_after_isolated_tests() -> None:
    # Runs after the TestRegisterBackend cases; verifies they didn't leak.
    assert "ephemeral" not in list_backends()
    assert "fake_test" not in list_backends()
    assert isinstance(get_backend("claude_p"), ClaudePBackend)

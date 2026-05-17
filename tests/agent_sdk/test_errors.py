"""Tests for ``agent_sdk.errors``."""

from __future__ import annotations

import pytest

from tigerharness.agent_sdk import (
    AgentSDKError,
    BackendNotImplementedError,
    CLIError,
    StreamNotConsumedError,
    ToolApprovalDenied,
)


class TestErrorHierarchy:
    def test_all_inherit_from_base(self) -> None:
        for cls in (BackendNotImplementedError, StreamNotConsumedError,
                    ToolApprovalDenied, CLIError):
            assert issubclass(cls, AgentSDKError)

    def test_backend_not_implemented_is_notimplemented(self) -> None:
        # BackendNotImplementedError is also a NotImplementedError so callers
        # can catch the broader exception type.
        assert issubclass(BackendNotImplementedError, NotImplementedError)

    def test_raise_and_catch_base(self) -> None:
        with pytest.raises(AgentSDKError):
            raise BackendNotImplementedError("nope")


class TestCLIError:
    def test_default_fields(self) -> None:
        err = CLIError("boom")
        assert str(err) == "boom"
        assert err.returncode is None
        assert err.stderr == ""

    def test_with_returncode_and_stderr(self) -> None:
        err = CLIError("crashed", returncode=137, stderr="OOM")
        assert err.returncode == 137
        assert err.stderr == "OOM"

    def test_is_agent_sdk_error(self) -> None:
        with pytest.raises(AgentSDKError):
            raise CLIError("boom")

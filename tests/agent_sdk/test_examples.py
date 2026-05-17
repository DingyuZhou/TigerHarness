"""Smoke tests for the example scripts.

We don't actually run them end-to-end (that needs a real ``claude`` CLI),
but we verify each module imports cleanly and exposes a ``main`` coroutine.
This catches the digit-prefix-import bug as a regression.
"""

from __future__ import annotations

import importlib
import inspect

import pytest

EXAMPLES = ["basic", "streaming", "multi_turn", "builtin_tools"]


@pytest.mark.parametrize("name", EXAMPLES)
def test_example_imports(name: str) -> None:
    mod = importlib.import_module(f"tigerharness.agent_sdk.examples.{name}")
    assert hasattr(mod, "main")
    assert inspect.iscoroutinefunction(mod.main)

"""Test helpers importable as a regular module.

Lives here (not in ``conftest.py``) because pytest's ``conftest.py`` is
auto-discovered as a fixture/hook namespace; importing it explicitly by
path works but is fragile and noisy. Plain helper functions belong in a
plain module.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")


def asyncio_test(fn: Callable[..., Awaitable[T]]) -> Callable[..., T]:
    """Wrap an async test function with ``asyncio.run``.

    Used in lieu of ``pytest-asyncio`` so the suite has no extra plugin
    dependency. Apply as the innermost decorator on a coroutine test.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        return asyncio.run(fn(*args, **kwargs))

    return wrapper

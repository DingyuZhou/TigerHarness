"""Coverage tests for session.close() exception in _classify_output / _classify_novelty
(runner.py lines 362-363, 427-428, 519)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from tigerharness.task_runner.runner import _classify_output, _classify_novelty


@dataclass
class FakeResult:
    final_output: str = "DONE"
    cost_usd: float = 0.001


@dataclass
class BadCloseSession:
    id: str = "sess-badclose"

    async def close(self):
        raise RuntimeError("session close failed")


class TestClassifyOutputSessionCloseException:
    """Lines 362-363: session.close() raises → swallowed."""

    @pytest.fixture
    def log_path(self, tmp_path):
        return tmp_path / "classify.log"

    @pytest.mark.asyncio
    async def test_session_close_exc_swallowed(self, log_path):
        fake_backend = AsyncMock()
        fake_backend.open_session = AsyncMock(return_value=BadCloseSession())

        with patch("tigerharness.task_runner.runner.run_with_retry",
                   return_value=FakeResult()):
            verdict = await _classify_output(
                fake_backend, "Done.", log_path,
                job_id="test", iter_num=1,
            )
        assert verdict == "DONE"


class TestClassifyNoveltySessionCloseException:
    """Lines 427-428: session.close() raises → swallowed."""

    @pytest.fixture
    def log_path(self, tmp_path):
        return tmp_path / "novelty.log"

    @pytest.mark.asyncio
    async def test_session_close_exc_swallowed(self, log_path):
        fake_backend = AsyncMock()
        fake_backend.open_session = AsyncMock(return_value=BadCloseSession())

        with patch("tigerharness.task_runner.runner.run_with_retry",
                   return_value=FakeResult(final_output="STALE")):
            verdict = await _classify_novelty(
                fake_backend, "prev", "curr", log_path,
                job_id="test", iter_num=2,
            )
        assert verdict == "STALE"

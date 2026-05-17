"""Coverage-push tests for runner.py — targeting uncovered lines."""

from __future__ import annotations

import asyncio
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tigerharness.task_runner.personas import clear_registry, register_persona
from tigerharness.task_runner.registry import JobMeta, JobStore
from tigerharness.task_runner.runner import (
    _build_continuation,
    main as runner_main,
    run_job,
)


@pytest.fixture(autouse=True)
def _clean_personas():
    clear_registry()
    register_persona("tester", prompt="You are a tester.", cwd="/tmp")
    yield
    clear_registry()


def _make_meta(store: JobStore, job_id: str = "cov1234", **over) -> JobMeta:
    base = dict(
        job_id=job_id,
        persona="tester",
        prompt_chars=10,
        max_iters=3,
        compact_every=0,
        continuation="",
        name="test-run",
        cwd="/tmp",
        started_at=time.time(),
        status="pending",
        pid=None,
        current_iter=0,
        session_id="",
        last_update=time.time(),
    )
    base.update(over)
    meta = JobMeta(**base)
    store.set(meta)
    store.prompt_path(job_id).write_text("Do the thing.")
    return meta


@dataclass
class FakeResult:
    final_output: str = "iteration done"
    cost_usd: float = 0.01


@dataclass
class FakeSession:
    id: str = "sess-cov"

    async def close(self):
        pass


class TestContinuationWithThreadTs:
    """Line 301: _build_continuation with thread_ts."""

    def test_thread_ts_appended(self):
        result = _build_continuation("custom", thread_ts="1111.2222")
        assert "1111.2222" in result


class TestSignalHandler:
    """Lines 515, 519, 522-523: notify with existing thread, SIGTERM handler,
    signal.signal ValueError."""

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TIGERHARNESS_STATE_DIR", str(tmp_path))
        return JobStore(tmp_path)

    @pytest.mark.asyncio
    async def test_existing_thread_ts_calls_notify(self, store, tmp_path):
        """When meta already has slack_thread_ts, notify_job_start still
        fires but doesn't overwrite the ts (line 515)."""
        _make_meta(store, max_iters=1, compact_every=0,
                   slack_thread_ts="existing.ts")

        fake_backend = AsyncMock()
        fake_backend.open_session = AsyncMock(return_value=FakeSession())
        mock_start = MagicMock(return_value="")

        with patch("tigerharness.task_runner.runner.get_backend", return_value=fake_backend), \
             patch("tigerharness.task_runner.runner.run_with_retry", return_value=FakeResult()), \
             patch("tigerharness.task_runner.runner.notify_job_start", mock_start), \
             patch("tigerharness.task_runner.runner.notify_job_end", return_value=True):
            await run_job("cov1234", state_dir=tmp_path)

        mock_start.assert_called_once()
        meta = store.get("cov1234")
        assert meta.slack_thread_ts == "existing.ts"

    @pytest.mark.asyncio
    async def test_signal_handler_valueerror(self, store, tmp_path):
        """signal.signal raising ValueError is swallowed (lines 522-523)."""
        _make_meta(store, max_iters=1, compact_every=0)

        fake_backend = AsyncMock()
        fake_backend.open_session = AsyncMock(return_value=FakeSession())

        with patch("tigerharness.task_runner.runner.get_backend", return_value=fake_backend), \
             patch("tigerharness.task_runner.runner.run_with_retry", return_value=FakeResult()), \
             patch("tigerharness.task_runner.runner.notify_job_start", return_value=""), \
             patch("tigerharness.task_runner.runner.notify_job_end", return_value=True), \
             patch("tigerharness.task_runner.runner.signal.signal", side_effect=ValueError("not main thread")):
            ret = await run_job("cov1234", state_dir=tmp_path)

        assert ret == 0


class TestEarlyExitBranches:
    """Lines 608, 612-613: consecutive_stale reset and CONTINUING branch."""

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TIGERHARNESS_STATE_DIR", str(tmp_path))
        return JobStore(tmp_path)

    @pytest.mark.asyncio
    async def test_continuing_resets_counters(self, store, tmp_path):
        """CONTINUING verdict resets both consecutive_stale and consecutive_error."""
        _make_meta(store, job_id="cont1", max_iters=4, compact_every=0,
                   early_exit=True)

        call_count = 0

        async def classify_output(*a, **kw):
            nonlocal call_count
            call_count += 1
            # First two: ERROR, third: CONTINUING (resets counters)
            if call_count <= 2:
                return "ERROR"
            return "CONTINUING"

        fake_backend = AsyncMock()
        fake_backend.open_session = AsyncMock(return_value=FakeSession())

        with patch("tigerharness.task_runner.runner.get_backend", return_value=fake_backend), \
             patch("tigerharness.task_runner.runner.run_with_retry", return_value=FakeResult()), \
             patch("tigerharness.task_runner.runner.notify_job_start", return_value=""), \
             patch("tigerharness.task_runner.runner.notify_job_end", return_value=True), \
             patch("tigerharness.task_runner.runner._classify_output", side_effect=classify_output):
            ret = await run_job("cont1", state_dir=tmp_path)

        meta = store.get("cont1")
        assert meta.status == "done"
        assert meta.current_iter == 4  # ran all 4, no early exit

    @pytest.mark.asyncio
    async def test_done_new_resets_stale(self, store, tmp_path):
        """DONE + NEW novelty resets consecutive_stale (line 608).

        Without NEW, early-exit would fire at 3 consecutive stales.
        With NEW every time, all 4 iters run to completion.
        """
        _make_meta(store, job_id="new1", max_iters=4, compact_every=0,
                   early_exit=True)

        async def classify_output(*a, **kw):
            return "DONE"

        async def classify_novelty(*a, **kw):
            return "NEW"  # always NEW → stale counter stays 0

        fake_backend = AsyncMock()
        fake_backend.open_session = AsyncMock(return_value=FakeSession())

        with patch("tigerharness.task_runner.runner.get_backend", return_value=fake_backend), \
             patch("tigerharness.task_runner.runner.run_with_retry", return_value=FakeResult()), \
             patch("tigerharness.task_runner.runner.notify_job_start", return_value=""), \
             patch("tigerharness.task_runner.runner.notify_job_end", return_value=True), \
             patch("tigerharness.task_runner.runner._classify_output", side_effect=classify_output), \
             patch("tigerharness.task_runner.runner._classify_novelty", side_effect=classify_novelty):
            ret = await run_job("new1", state_dir=tmp_path)

        meta = store.get("new1")
        assert meta.status == "done"
        assert meta.current_iter == 4  # ran all 4 — NEW kept resetting stale


class TestSessionCloseAndNotifyExceptions:
    """Lines 692-693 (session.close exc), 696-697 (notify_job_end exc)."""

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TIGERHARNESS_STATE_DIR", str(tmp_path))
        return JobStore(tmp_path)

    @pytest.mark.asyncio
    async def test_session_close_exception_swallowed(self, store, tmp_path):
        _make_meta(store, max_iters=1, compact_every=0)

        @dataclass
        class BadCloseSession:
            id: str = "sess-badclose"

            async def close(self):
                raise RuntimeError("close failed")

        fake_backend = AsyncMock()
        fake_backend.open_session = AsyncMock(return_value=BadCloseSession())

        with patch("tigerharness.task_runner.runner.get_backend", return_value=fake_backend), \
             patch("tigerharness.task_runner.runner.run_with_retry", return_value=FakeResult()), \
             patch("tigerharness.task_runner.runner.notify_job_start", return_value=""), \
             patch("tigerharness.task_runner.runner.notify_job_end", return_value=True):
            ret = await run_job("cov1234", state_dir=tmp_path)

        assert ret == 0

    @pytest.mark.asyncio
    async def test_notify_job_end_exception_swallowed(self, store, tmp_path):
        _make_meta(store, max_iters=1, compact_every=0)

        fake_backend = AsyncMock()
        fake_backend.open_session = AsyncMock(return_value=FakeSession())

        with patch("tigerharness.task_runner.runner.get_backend", return_value=fake_backend), \
             patch("tigerharness.task_runner.runner.run_with_retry", return_value=FakeResult()), \
             patch("tigerharness.task_runner.runner.notify_job_start", return_value=""), \
             patch("tigerharness.task_runner.runner.notify_job_end", side_effect=RuntimeError("notify boom")):
            ret = await run_job("cov1234", state_dir=tmp_path)

        assert ret == 0


class TestWriteIterHeaderOSError:
    """Lines 486-487: OSError writing iter log header is swallowed."""

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TIGERHARNESS_STATE_DIR", str(tmp_path))
        return JobStore(tmp_path)

    @pytest.mark.asyncio
    async def test_iter_header_oserror(self, store, tmp_path):
        _make_meta(store, max_iters=1, compact_every=0)

        fake_backend = AsyncMock()
        fake_backend.open_session = AsyncMock(return_value=FakeSession())

        # _write_iter_header opens a file — make it fail
        with patch("tigerharness.task_runner.runner.get_backend", return_value=fake_backend), \
             patch("tigerharness.task_runner.runner.run_with_retry", return_value=FakeResult()), \
             patch("tigerharness.task_runner.runner.notify_job_start", return_value=""), \
             patch("tigerharness.task_runner.runner.notify_job_end", return_value=True), \
             patch("tigerharness.task_runner.runner._write_iter_header", side_effect=OSError("nope")):
            ret = await run_job("cov1234", state_dir=tmp_path)

        assert ret == 0


class TestRunnerMainArgParsing:
    """Lines 721, 731: --resume-session and --start-iter flag parsing."""

    def test_resume_session_and_start_iter(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TIGERHARNESS_STATE_DIR", str(tmp_path))
        # Job doesn't exist, so run_job returns 1 — but we exercise arg parsing
        ret = runner_main(["nonexist", "--resume-session", "sess-x", "--start-iter", "5"])
        assert ret == 1

"""Runner tests: iteration loop, cancel, compact, early exit."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tigerharness.task_runner.personas import clear_registry, register_persona
from tigerharness.task_runner.registry import JobMeta, JobStore
from tigerharness.task_runner.runner import (
    COMPACT_TRIGGER,
    _append_log,
    _build_continuation,
    _build_initial_prompt,
    _get_slack_thread_notice,
    _iter_log_path,
    run_job,
)


@pytest.fixture(autouse=True)
def _clean_personas():
    clear_registry()
    register_persona("tester", prompt="You are a tester.", cwd="/tmp")
    yield
    clear_registry()


def _make_meta(store: JobStore, job_id: str = "test1234", **over) -> JobMeta:
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


class TestPromptBuilding:
    def test_initial_prompt_no_thread(self):
        result = _build_initial_prompt("Hello task")
        assert "Hello task" in result
        assert "Task execution guidelines" in result
        assert "Slack threading" not in result

    def test_initial_prompt_with_thread(self):
        result = _build_initial_prompt("Hello task", thread_ts="1234.5678")
        assert "1234.5678" in result
        assert "Slack threading" in result

    def test_continuation_default(self):
        result = _build_continuation("")
        assert "Continue the task" in result

    def test_continuation_custom(self):
        result = _build_continuation("Focus on X")
        assert "Focus on X" in result
        assert "Iteration guidelines" in result

    def test_get_slack_thread_notice_generic(self, monkeypatch):
        monkeypatch.delenv("TIGERHARNESS_SLACK_BRIDGE_DIR", raising=False)
        notice = _get_slack_thread_notice("1234.5678")
        assert "--thread 1234.5678" in notice
        assert "tigerharness.slack_bridge.notify" in notice

    def test_get_slack_thread_notice_with_bridge_dir(self, monkeypatch):
        monkeypatch.setenv("TIGERHARNESS_SLACK_BRIDGE_DIR", "/opt/bridge")
        notice = _get_slack_thread_notice("1234.5678")
        assert "cd /opt/bridge" in notice
        assert "--thread 1234.5678" in notice


class TestIterLogPath:
    def test_with_name(self):
        meta = MagicMock()
        meta.cwd = "/proj"
        meta.name = "my task"
        meta.job_id = "abcd1234"
        path = _iter_log_path(meta)
        assert path == Path("/proj/lab_notebooks/tasks/my-task--abcd1234.md")

    def test_without_name(self):
        meta = MagicMock()
        meta.cwd = "/proj"
        meta.name = ""
        meta.job_id = "abcd1234"
        path = _iter_log_path(meta)
        assert path == Path("/proj/lab_notebooks/tasks/abcd1234.md")


class TestRunJob:
    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TIGERHARNESS_STATE_DIR", str(tmp_path))
        return JobStore(tmp_path)

    @pytest.mark.asyncio
    async def test_missing_job(self, tmp_path):
        ret = await run_job("nonexist", state_dir=tmp_path)
        assert ret == 1

    @pytest.mark.asyncio
    async def test_unknown_persona(self, store, tmp_path):
        clear_registry()
        meta = JobMeta(
            job_id="bad_pers",
            persona="unknown",
            prompt_chars=10,
            max_iters=1,
            compact_every=0,
            continuation="",
            name="",
            cwd="/tmp",
            started_at=time.time(),
            status="pending",
            pid=None,
            current_iter=0,
            session_id="",
            last_update=time.time(),
        )
        store.set(meta)
        store.prompt_path("bad_pers").write_text("test")
        ret = await run_job("bad_pers", state_dir=tmp_path)
        assert ret == 2
        updated = store.get("bad_pers")
        assert updated.status == "error"

    @pytest.mark.asyncio
    async def test_missing_prompt(self, store, tmp_path):
        meta = JobMeta(
            job_id="no_prompt",
            persona="tester",
            prompt_chars=10,
            max_iters=1,
            compact_every=0,
            continuation="",
            name="",
            cwd="/tmp",
            started_at=time.time(),
            status="pending",
            pid=None,
            current_iter=0,
            session_id="",
            last_update=time.time(),
        )
        store.set(meta)
        # Don't write prompt file
        ret = await run_job("no_prompt", state_dir=tmp_path)
        assert ret == 2

    @pytest.mark.asyncio
    async def test_basic_run(self, store, tmp_path):
        _make_meta(store, max_iters=2, compact_every=0)

        @dataclass
        class FakeResult:
            final_output: str = "iteration done"
            cost_usd: float = 0.01

        @dataclass
        class FakeSession:
            id: str = "sess-123"
            async def close(self):
                pass

        fake_backend = AsyncMock()
        fake_backend.open_session = AsyncMock(return_value=FakeSession())

        with patch("tigerharness.task_runner.runner.get_backend", return_value=fake_backend), \
             patch("tigerharness.task_runner.runner.run_with_retry", return_value=FakeResult()), \
             patch("tigerharness.task_runner.runner.notify_job_start", return_value=""), \
             patch("tigerharness.task_runner.runner.notify_job_end", return_value=True):
            ret = await run_job("test1234", state_dir=tmp_path)

        assert ret == 0
        meta = store.get("test1234")
        assert meta.status == "done"
        assert meta.current_iter == 2

    @pytest.mark.asyncio
    async def test_cancel_mid_run(self, store, tmp_path):
        _make_meta(store, max_iters=10, compact_every=0)

        @dataclass
        class FakeResult:
            final_output: str = "ok"
            cost_usd: float = 0.001

        @dataclass
        class FakeSession:
            id: str = "sess-cancel"
            async def close(self):
                pass

        call_count = 0

        async def fake_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                # Simulate cancel after 2 iterations
                store.request_cancel("test1234")
            return FakeResult()

        fake_backend = AsyncMock()
        fake_backend.open_session = AsyncMock(return_value=FakeSession())

        with patch("tigerharness.task_runner.runner.get_backend", return_value=fake_backend), \
             patch("tigerharness.task_runner.runner.run_with_retry", side_effect=fake_run), \
             patch("tigerharness.task_runner.runner.notify_job_start", return_value=""), \
             patch("tigerharness.task_runner.runner.notify_job_end", return_value=True):
            ret = await run_job("test1234", state_dir=tmp_path)

        assert ret == 1  # not completed
        meta = store.get("test1234")
        assert meta.status == "cancelled"


    @pytest.mark.asyncio
    async def test_compact_fires(self, store, tmp_path):
        """compact_every=1 should fire a compact turn after each iteration."""
        _make_meta(store, max_iters=2, compact_every=1)

        @dataclass
        class FakeResult:
            final_output: str = "done"
            cost_usd: float = 0.001

        @dataclass
        class FakeSession:
            id: str = "sess-compact"
            async def close(self):
                pass

        call_kinds = []

        async def tracking_run(*args, **kwargs):
            # Track what kind of prompt is being sent
            prompt = args[2] if len(args) > 2 else kwargs.get("prompt", "")
            if prompt == "/compact":
                call_kinds.append("compact")
            else:
                call_kinds.append("turn")
            return FakeResult()

        fake_backend = AsyncMock()
        fake_backend.open_session = AsyncMock(return_value=FakeSession())

        with patch("tigerharness.task_runner.runner.get_backend", return_value=fake_backend), \
             patch("tigerharness.task_runner.runner.run_with_retry", side_effect=tracking_run), \
             patch("tigerharness.task_runner.runner.notify_job_start", return_value=""), \
             patch("tigerharness.task_runner.runner.notify_job_end", return_value=True):
            ret = await run_job("test1234", state_dir=tmp_path)

        assert ret == 0
        # iter 1 (turn) -> compact -> iter 2 (turn, last so no compact after)
        assert call_kinds == ["turn", "compact", "turn"]

    @pytest.mark.asyncio
    async def test_early_exit_stale(self, store, tmp_path):
        """Early exit triggers after 3 consecutive DONE+STALE."""
        _make_meta(store, max_iters=20, compact_every=0, early_exit=True)

        @dataclass
        class FakeResult:
            final_output: str = "Task is complete. Nothing more to do."
            cost_usd: float = 0.001

        @dataclass
        class FakeSession:
            id: str = "sess-early"
            async def close(self):
                pass

        fake_backend = AsyncMock()
        fake_backend.open_session = AsyncMock(return_value=FakeSession())

        with patch("tigerharness.task_runner.runner.get_backend", return_value=fake_backend), \
             patch("tigerharness.task_runner.runner.run_with_retry", return_value=FakeResult()), \
             patch("tigerharness.task_runner.runner.notify_job_start", return_value=""), \
             patch("tigerharness.task_runner.runner.notify_job_end", return_value=True), \
             patch("tigerharness.task_runner.runner._classify_output", return_value="DONE"), \
             patch("tigerharness.task_runner.runner._classify_novelty", return_value="STALE"):
            ret = await run_job("test1234", state_dir=tmp_path)

        assert ret == 0
        meta = store.get("test1234")
        assert meta.status == "done"
        # Should exit after 3 consecutive stale (not run all 20)
        assert meta.current_iter <= 4  # 3 stale + possibly 1 more

    @pytest.mark.asyncio
    async def test_exception_sets_error(self, store, tmp_path):
        """Backend exception should set status=error."""
        _make_meta(store, max_iters=5, compact_every=0)

        @dataclass
        class FakeSession:
            id: str = "sess-err"
            async def close(self):
                pass

        fake_backend = AsyncMock()
        fake_backend.open_session = AsyncMock(return_value=FakeSession())

        with patch("tigerharness.task_runner.runner.get_backend", return_value=fake_backend), \
             patch("tigerharness.task_runner.runner.run_with_retry", side_effect=RuntimeError("boom")), \
             patch("tigerharness.task_runner.runner.notify_job_start", return_value=""), \
             patch("tigerharness.task_runner.runner.notify_job_end", return_value=True):
            ret = await run_job("test1234", state_dir=tmp_path)

        assert ret == 1
        meta = store.get("test1234")
        assert meta.status == "error"
        assert "boom" in meta.error

    @pytest.mark.asyncio
    async def test_resume_session(self, store, tmp_path):
        """Resuming with a session_id passes resume_id to open_session."""
        _make_meta(store, max_iters=2, compact_every=0)

        @dataclass
        class FakeResult:
            final_output: str = "resumed"
            cost_usd: float = 0.001

        @dataclass
        class FakeSession:
            id: str = "sess-resumed"
            async def close(self):
                pass

        fake_backend = AsyncMock()
        fake_backend.open_session = AsyncMock(return_value=FakeSession())

        with patch("tigerharness.task_runner.runner.get_backend", return_value=fake_backend), \
             patch("tigerharness.task_runner.runner.run_with_retry", return_value=FakeResult()), \
             patch("tigerharness.task_runner.runner.notify_job_start", return_value=""), \
             patch("tigerharness.task_runner.runner.notify_job_end", return_value=True):
            ret = await run_job(
                "test1234", state_dir=tmp_path,
                resume_session_id="old-sess-id", start_iter=5,
            )

        assert ret == 0
        fake_backend.open_session.assert_awaited_once_with(resume_id="old-sess-id")


class TestRunJobEdgeCases:
    """Additional edge cases for run_job: thread anchor, early-exit ERROR."""

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TIGERHARNESS_STATE_DIR", str(tmp_path))
        return JobStore(tmp_path)

    @pytest.mark.asyncio
    async def test_thread_anchor_captured(self, store, tmp_path):
        """notify_job_start returning a ts should set slack_thread_ts."""
        _make_meta(store, max_iters=1, compact_every=0)

        @dataclass
        class FakeResult:
            final_output: str = "done"
            cost_usd: float = 0.001

        @dataclass
        class FakeSession:
            id: str = "s1"
            async def close(self):
                pass

        fake_backend = AsyncMock()
        fake_backend.open_session = AsyncMock(return_value=FakeSession())

        with patch("tigerharness.task_runner.runner.get_backend", return_value=fake_backend), \
             patch("tigerharness.task_runner.runner.run_with_retry", return_value=FakeResult()), \
             patch("tigerharness.task_runner.runner.notify_job_start", return_value="anchor.123"), \
             patch("tigerharness.task_runner.runner.notify_job_end", return_value=True):
            await run_job("test1234", state_dir=tmp_path)

        meta = store.get("test1234")
        assert meta.slack_thread_ts == "anchor.123"

    @pytest.mark.asyncio
    async def test_early_exit_error(self, store, tmp_path):
        """3 consecutive ERROR classifications trigger early exit with error status."""
        _make_meta(store, max_iters=20, compact_every=0, early_exit=True)

        @dataclass
        class FakeResult:
            final_output: str = "I'm stuck on an error."
            cost_usd: float = 0.001

        @dataclass
        class FakeSession:
            id: str = "s-err"
            async def close(self):
                pass

        fake_backend = AsyncMock()
        fake_backend.open_session = AsyncMock(return_value=FakeSession())

        with patch("tigerharness.task_runner.runner.get_backend", return_value=fake_backend), \
             patch("tigerharness.task_runner.runner.run_with_retry", return_value=FakeResult()), \
             patch("tigerharness.task_runner.runner.notify_job_start", return_value=""), \
             patch("tigerharness.task_runner.runner.notify_job_end", return_value=True), \
             patch("tigerharness.task_runner.runner._classify_output", return_value="ERROR"):
            ret = await run_job("test1234", state_dir=tmp_path)

        assert ret == 1
        meta = store.get("test1234")
        assert meta.status == "error"
        assert "consecutive iterations" in meta.error
        assert meta.current_iter == 3  # exits after exactly 3

    @pytest.mark.asyncio
    async def test_persona_build_config_fails(self, store, tmp_path):
        """FileNotFoundError during persona.build_config -> error status."""
        from tigerharness.task_runner.personas import clear_registry, register_persona
        clear_registry()
        # Register persona that will fail on build_config
        register_persona(
            "breaker",
            prompt_file="nonexistent",
            personas_dir=tmp_path,  # no file there
            cwd="/tmp",
        )
        meta = _make_meta(store, persona="breaker")
        # Override persona in meta (make_meta uses "tester")
        meta.persona = "breaker"
        store.set(meta)

        ret = await run_job("test1234", state_dir=tmp_path)
        assert ret == 2
        updated = store.get("test1234")
        assert updated.status == "error"
        assert "config build failed" in updated.error


class TestClassifyOutput:
    @pytest.fixture
    def log_path(self, tmp_path):
        return tmp_path / "classify.log"

    @pytest.mark.asyncio
    async def test_returns_done(self, log_path):
        from tigerharness.task_runner.runner import _classify_output

        @dataclass
        class FakeResult:
            final_output: str = "DONE"
            cost_usd: float = 0.001

        @dataclass
        class FakeSession:
            id: str = "cls-sess"
            async def close(self):
                pass

        fake_backend = AsyncMock()
        fake_backend.open_session = AsyncMock(return_value=FakeSession())

        with patch("tigerharness.task_runner.runner.run_with_retry", return_value=FakeResult()):
            verdict = await _classify_output(
                fake_backend, "The task is complete.", log_path,
                job_id="test", iter_num=1,
            )
        assert verdict == "DONE"

    @pytest.mark.asyncio
    async def test_returns_continuing_on_failure(self, log_path):
        from tigerharness.task_runner.runner import _classify_output

        @dataclass
        class FakeSession:
            id: str = "cls-fail"
            async def close(self):
                pass

        fake_backend = AsyncMock()
        fake_backend.open_session = AsyncMock(return_value=FakeSession())

        with patch("tigerharness.task_runner.runner.run_with_retry", side_effect=RuntimeError("oops")):
            verdict = await _classify_output(
                fake_backend, "some text", log_path,
                job_id="test", iter_num=1,
            )
        assert verdict == "CONTINUING"

    @pytest.mark.asyncio
    async def test_returns_continuing_on_gibberish(self, log_path):
        from tigerharness.task_runner.runner import _classify_output

        @dataclass
        class FakeResult:
            final_output: str = "I don't know what to say"
            cost_usd: float = 0.001

        @dataclass
        class FakeSession:
            id: str = "cls-gib"
            async def close(self):
                pass

        fake_backend = AsyncMock()
        fake_backend.open_session = AsyncMock(return_value=FakeSession())

        with patch("tigerharness.task_runner.runner.run_with_retry", return_value=FakeResult()):
            verdict = await _classify_output(
                fake_backend, "text", log_path,
                job_id="test", iter_num=1,
            )
        assert verdict == "CONTINUING"


class TestClassifyNovelty:
    @pytest.fixture
    def log_path(self, tmp_path):
        return tmp_path / "novelty.log"

    @pytest.mark.asyncio
    async def test_returns_stale(self, log_path):
        from tigerharness.task_runner.runner import _classify_novelty

        @dataclass
        class FakeResult:
            final_output: str = "STALE"
            cost_usd: float = 0.001

        @dataclass
        class FakeSession:
            id: str = "nov-sess"
            async def close(self):
                pass

        fake_backend = AsyncMock()
        fake_backend.open_session = AsyncMock(return_value=FakeSession())

        with patch("tigerharness.task_runner.runner.run_with_retry", return_value=FakeResult()):
            verdict = await _classify_novelty(
                fake_backend, "prev text", "curr text", log_path,
                job_id="test", iter_num=2,
            )
        assert verdict == "STALE"

    @pytest.mark.asyncio
    async def test_returns_new_on_failure(self, log_path):
        from tigerharness.task_runner.runner import _classify_novelty

        @dataclass
        class FakeSession:
            id: str = "nov-fail"
            async def close(self):
                pass

        fake_backend = AsyncMock()
        fake_backend.open_session = AsyncMock(return_value=FakeSession())

        with patch("tigerharness.task_runner.runner.run_with_retry", side_effect=RuntimeError("err")):
            verdict = await _classify_novelty(
                fake_backend, "a", "b", log_path,
                job_id="test", iter_num=2,
            )
        assert verdict == "NEW"

    @pytest.mark.asyncio
    async def test_returns_new_on_gibberish(self, log_path):
        from tigerharness.task_runner.runner import _classify_novelty

        @dataclass
        class FakeResult:
            final_output: str = "something weird"
            cost_usd: float = 0.001

        @dataclass
        class FakeSession:
            id: str = "nov-gib"
            async def close(self):
                pass

        fake_backend = AsyncMock()
        fake_backend.open_session = AsyncMock(return_value=FakeSession())

        with patch("tigerharness.task_runner.runner.run_with_retry", return_value=FakeResult()):
            verdict = await _classify_novelty(
                fake_backend, "a", "b", log_path,
                job_id="test", iter_num=2,
            )
        assert verdict == "NEW"


class TestIterLogHelpers:
    def test_append_iteration_oserror(self, tmp_path):
        """OSError in _append_iteration should not raise."""
        from tigerharness.task_runner.runner import _append_iteration
        # Use a path that can't be written (directory without write permission)
        bad_path = tmp_path / "readonly" / "file.md"
        # Don't create parent → OSError
        # Actually _append_iteration creates parents, so let's use /dev/null/x
        # Simplest: mock open to raise
        with patch("builtins.open", side_effect=OSError("permission denied")):
            # Should not raise
            _append_iteration(Path("/fake/path.md"), 1, "text")

    def test_append_runner_event_oserror(self):
        from tigerharness.task_runner.runner import _append_runner_event
        with patch("builtins.open", side_effect=OSError("no")):
            _append_runner_event(Path("/fake/path.md"), "event")


class TestRunnerMain:
    def test_no_args_returns_2(self):
        from tigerharness.task_runner.runner import main as runner_main
        assert runner_main([]) == 2

    def test_nonexistent_job(self, tmp_path, monkeypatch):
        from tigerharness.task_runner.runner import main as runner_main
        monkeypatch.setenv("TIGERHARNESS_STATE_DIR", str(tmp_path))
        assert runner_main(["nonexist"]) == 1


class TestAppendLog:
    def test_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "nested" / "dir" / "run.log"
        _append_log(path, {"key": "value"})
        assert path.exists()
        assert '"key": "value"' in path.read_text()

"""Coverage-push tests for cli.py — targeting lines:
70 (prompt_path read), 225 (cancel --signal with PID), 296-298 (continue persona
build failure), 302-303 (continue override), 421-422 (cmd_run_internal),
543 (__name__ == "__main__").
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tigerharness.task_runner.cli import main
from tigerharness.task_runner.personas import clear_registry, register_persona
from tigerharness.task_runner.registry import JobMeta, JobStore


@pytest.fixture(autouse=True)
def _clean_personas():
    clear_registry()
    register_persona("tester", prompt="You are a tester.", cwd="/tmp")
    yield
    clear_registry()


def _make_meta(store: JobStore, job_id: str = "cli1234", **over) -> JobMeta:
    base = dict(
        job_id=job_id,
        persona="tester",
        prompt_chars=10,
        max_iters=5,
        compact_every=0,
        continuation="",
        name="test-cli",
        cwd="/tmp",
        started_at=time.time(),
        status="done",
        pid=None,
        current_iter=3,
        session_id="sess-1",
        last_update=time.time(),
    )
    base.update(over)
    meta = JobMeta(**base)
    store.set(meta)
    store.prompt_path(job_id).write_text("Test prompt.")
    return meta


class TestSubmitFromFile:
    """Line 70: prompt from --prompt-file."""

    def test_prompt_file(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("TIGERHARNESS_STATE_DIR", str(tmp_path))
        prompt_file = tmp_path / "task.md"
        prompt_file.write_text("Do this task from file")

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        with patch("tigerharness.task_runner.cli.subprocess.Popen",
                   return_value=mock_proc):
            ret = main(["assign", "--persona", "tester",
                        "--prompt-file", str(prompt_file)])
        assert ret == 0


class TestCancelWithSignal:
    """Line 225: cancel --signal sends SIGTERM to PID."""

    def test_signal_sent(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("TIGERHARNESS_STATE_DIR", str(tmp_path))
        store = JobStore(tmp_path)
        _make_meta(store, status="running", pid=os.getpid())

        with patch("os.kill") as mock_kill:
            ret = main(["cancel", "cli1234", "--signal"])

        assert ret == 0
        mock_kill.assert_called_once_with(os.getpid(), 15)

    def test_signal_dead_pid(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("TIGERHARNESS_STATE_DIR", str(tmp_path))
        store = JobStore(tmp_path)
        _make_meta(store, status="running", pid=99999)

        with patch("os.kill", side_effect=ProcessLookupError("gone")):
            ret = main(["cancel", "cli1234", "--signal"])
        assert ret == 0


class TestContinuePersonaBuildFail:
    """Lines 296-298: continue fails when persona build_config raises."""

    def test_persona_build_fails(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("TIGERHARNESS_STATE_DIR", str(tmp_path))
        store = JobStore(tmp_path)
        _make_meta(store, status="done")

        clear_registry()
        register_persona(
            "breaker",
            prompt_file="nonexistent",
            personas_dir=tmp_path,
            cwd="/tmp",
        )
        meta = store.get("cli1234")
        meta.persona = "breaker"
        store.set(meta)

        ret = main(["continue", "cli1234", "--iters", "3"])
        assert ret == 2


class TestContinueForeverRejected:
    """Lines 302-303: continue with --iters forever → error."""

    def test_forever_iters_rejected(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("TIGERHARNESS_STATE_DIR", str(tmp_path))
        store = JobStore(tmp_path)
        _make_meta(store, status="done")

        ret = main(["continue", "cli1234", "--iters", "forever"])
        assert ret == 1


class TestCmdRunInternal:
    """Lines 421-422: cmd_run_internal invokes asyncio.run."""

    def test_run_internal(self, tmp_path: Path, monkeypatch):
        import argparse
        monkeypatch.setenv("TIGERHARNESS_STATE_DIR", str(tmp_path))
        from tigerharness.task_runner.cli import cmd_run_internal
        args = argparse.Namespace(
            job_id="nonexist",
            resume_session="",
            start_iter=0,
        )
        # Job doesn't exist → run_job returns 1
        ret = cmd_run_internal(args)
        assert ret == 1


class TestContinueOverride:
    """Lines 302-303: continue with --continuation override."""

    def test_continuation_override(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("TIGERHARNESS_STATE_DIR", str(tmp_path))
        store = JobStore(tmp_path)
        _make_meta(store, status="done")

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        with patch("tigerharness.task_runner.cli.subprocess.Popen",
                   return_value=mock_proc):
            ret = main(["continue", "cli1234", "--iters", "3",
                        "--continuation", "Focus on tests now"])
        assert ret == 0
        meta = store.get("cli1234")
        assert meta.continuation == "Focus on tests now"

"""Additional CLI tests — persona build failures, continue edge cases,
logs --follow, cancel --signal, amend unknown job."""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from tigerharness.task_runner.cli import (
    build_parser,
    cmd_amend,
    cmd_cancel,
    cmd_continue,
    main,
)
from tigerharness.task_runner.personas import clear_registry, register_persona
from tigerharness.task_runner.registry import JobMeta, JobStore


@pytest.fixture(autouse=True)
def _clean_personas():
    clear_registry()
    yield
    clear_registry()


@pytest.fixture
def store(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TIGERHARNESS_STATE_DIR", str(tmp_path))
    return JobStore(tmp_path)


def _make_meta(job_id: str, **over) -> JobMeta:
    base = dict(
        job_id=job_id,
        persona="helper",
        prompt_chars=42,
        max_iters=5,
        compact_every=5,
        continuation="",
        name="test",
        cwd="/tmp",
        started_at=time.time(),
        status="pending",
        pid=None,
        current_iter=0,
        session_id="",
        last_update=time.time(),
    )
    base.update(over)
    return JobMeta(**base)


class TestAssignPersonaBuildFailure:
    def test_assign_persona_build_config_fails(self, store, monkeypatch, capsys):
        """When persona.build_config() raises FileNotFoundError."""
        register_persona("helper", prompt="test", cwd="/tmp")
        # Patch personas.resolve to return a persona whose build_config raises
        mock_persona = MagicMock()
        mock_persona.name = "helper"
        mock_persona.cwd = Path("/tmp")
        mock_persona.build_config.side_effect = FileNotFoundError("prompt not found")
        with patch("tigerharness.task_runner.cli.personas.resolve", return_value=mock_persona):
            ret = main(["assign", "--to", "helper", "--prompt", "do work", "--iters", "3"])
        assert ret == 2
        err = capsys.readouterr().err
        assert "error" in err.lower()


class TestContinueEdgeCases:
    def test_continue_unknown_persona(self, store, monkeypatch, capsys):
        """Continue fails when persona is no longer registered."""
        store.set(_make_meta("aabb1122", status="done", current_iter=5,
                             persona="deleted_persona"))
        ret = main(["continue", "aabb1122", "--iters", "5"])
        assert ret == 2

    def test_continue_unknown_job(self, store, monkeypatch, capsys):
        ret = main(["continue", "zzzz", "--iters", "5"])
        assert ret == 1

    def test_continue_with_continuation_override(self, store, monkeypatch, capsys):
        register_persona("helper", prompt="test", cwd="/tmp")
        store.set(_make_meta("aabb1122", status="done", current_iter=5, session_id="s1"))
        with patch("tigerharness.task_runner.cli.subprocess.Popen") as mock_popen:
            mock_popen.return_value.pid = 99999
            ret = main(["continue", "aabb1122", "--iters", "3",
                         "--continuation", "Focus on X now."])
        assert ret == 0
        meta = store.get("aabb1122")
        assert meta.continuation == "Focus on X now."

    def test_continue_clears_cancel_flag(self, store, monkeypatch, capsys):
        register_persona("helper", prompt="test", cwd="/tmp")
        store.set(_make_meta("aabb1122", status="cancelled", current_iter=5, session_id="s1"))
        # Create cancel flag
        store.cancel_flag("aabb1122").parent.mkdir(parents=True, exist_ok=True)
        store.cancel_flag("aabb1122").write_text("1")
        with patch("tigerharness.task_runner.cli.subprocess.Popen") as mock_popen:
            mock_popen.return_value.pid = 99999
            ret = main(["continue", "aabb1122", "--iters", "3"])
        assert ret == 0
        assert not store.cancel_flag("aabb1122").exists()


class TestCancelSignal:
    def test_cancel_with_signal(self, store, monkeypatch, capsys):
        store.set(_make_meta("aabb1122", status="running", pid=999999999))
        args = build_parser().parse_args(["cancel", "aabb", "--signal"])
        ret = cmd_cancel(args)
        assert ret == 0
        out = capsys.readouterr().out
        assert "no longer alive" in out or "SIGTERM" in out


class TestAmendEdgeCases:
    def test_amend_unknown_job(self, store, monkeypatch, capsys):
        args = build_parser().parse_args(["amend", "zzzz", "--continuation", "x"])
        ret = cmd_amend(args)
        assert ret == 1


class TestLogsFollow:
    def test_logs_follow_interrupted(self, store, monkeypatch, capsys):
        store.set(_make_meta("aabb1122"))
        store.run_log("aabb1122").write_text('{"kind":"start"}\n')
        with patch("tigerharness.task_runner.cli.subprocess.run",
                    side_effect=KeyboardInterrupt):
            ret = main(["logs", "aabb1122", "--follow"])
        assert ret == 0


class TestRunnerMain:
    def test_runner_main_no_args(self):
        from tigerharness.task_runner.runner import main as runner_main
        ret = runner_main([])
        assert ret == 2

    def test_runner_main_with_args(self):
        """runner.main parses --resume-session and --start-iter flags."""
        from tigerharness.task_runner.runner import main as runner_main
        # Will fail because the job doesn't exist, but exercises arg parsing
        with patch("tigerharness.task_runner.runner.asyncio.run",
                    side_effect=SystemExit(1)):
            with pytest.raises(SystemExit):
                runner_main(["fakejob", "--resume-session", "sess1", "--start-iter", "3"])

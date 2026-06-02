"""CLI tests: assign, list, cancel, show, amend, continue, personas."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from tigerharness.task_runner.cli import (
    _fmt_ago,
    _parse_iters,
    build_parser,
    cmd_amend,
    cmd_cancel,
    cmd_list,
    cmd_show,
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


class TestParseIters:
    def test_integer(self):
        assert _parse_iters("5") == 5

    def test_forever_variants(self):
        assert _parse_iters("forever") == 0
        assert _parse_iters("0") == 0
        assert _parse_iters("inf") == 0
        assert _parse_iters("infinite") == 0

    def test_negative_raises(self):
        import argparse
        with pytest.raises(argparse.ArgumentTypeError):
            _parse_iters("-1")

    def test_non_numeric_raises(self):
        import argparse
        with pytest.raises(argparse.ArgumentTypeError):
            _parse_iters("abc")


class TestFmtAgo:
    def test_seconds(self):
        assert "s ago" in _fmt_ago(time.time() - 30)

    def test_minutes(self):
        assert "m ago" in _fmt_ago(time.time() - 300)

    def test_hours(self):
        assert "h ago" in _fmt_ago(time.time() - 7200)

    def test_days(self):
        assert "d ago" in _fmt_ago(time.time() - 172800)


class TestCmdList:
    def test_empty(self, store, monkeypatch, capsys):
        args = build_parser().parse_args(["list"])
        ret = cmd_list(args)
        assert ret == 0
        assert "No active jobs" in capsys.readouterr().out

    def test_with_jobs(self, store, monkeypatch, capsys):
        store.set(_make_meta("aabb1122", status="running", current_iter=3))
        args = build_parser().parse_args(["list"])
        ret = cmd_list(args)
        assert ret == 0
        out = capsys.readouterr().out
        assert "aabb1122" in out

    def test_json_format(self, store, monkeypatch, capsys):
        store.set(_make_meta("aabb1122"))
        args = build_parser().parse_args(["list", "--format", "json"])
        ret = cmd_list(args)
        assert ret == 0
        out = capsys.readouterr().out
        assert '"aabb1122"' in out

    def test_all_flag(self, store, monkeypatch, capsys):
        store.set(_make_meta("aabb1122", status="done"))
        args = build_parser().parse_args(["list", "--all"])
        ret = cmd_list(args)
        assert ret == 0
        assert "aabb1122" in capsys.readouterr().out


class TestCmdCancel:
    def test_cancel_running(self, store, monkeypatch, capsys):
        store.set(_make_meta("aabb1122", status="running"))
        args = build_parser().parse_args(["cancel", "aabb"])
        ret = cmd_cancel(args)
        assert ret == 0
        assert store.is_cancel_requested("aabb1122")

    def test_cancel_already_done(self, store, monkeypatch, capsys):
        store.set(_make_meta("aabb1122", status="done"))
        args = build_parser().parse_args(["cancel", "aabb1122"])
        ret = cmd_cancel(args)
        assert ret == 0
        assert "already done" in capsys.readouterr().out

    def test_cancel_unknown(self, store, monkeypatch, capsys):
        args = build_parser().parse_args(["cancel", "zzzz"])
        ret = cmd_cancel(args)
        assert ret == 1


class TestCmdShow:
    def test_show_existing(self, store, monkeypatch, capsys):
        store.set(_make_meta("aabb1122"))
        store.result_path("aabb1122").write_text("hello world")
        args = build_parser().parse_args(["show", "aabb1122"])
        ret = cmd_show(args)
        assert ret == 0
        out = capsys.readouterr().out
        assert "aabb1122" in out
        assert "hello world" in out

    def test_show_missing(self, store, monkeypatch, capsys):
        args = build_parser().parse_args(["show", "zzzz"])
        ret = cmd_show(args)
        assert ret == 1


class TestCmdAmend:
    def test_amend_continuation(self, store, monkeypatch, capsys):
        store.set(_make_meta("aabb1122"))
        args = build_parser().parse_args(
            ["amend", "aabb1122", "--continuation", "new prompt"]
        )
        ret = cmd_amend(args)
        assert ret == 0
        meta = store.get("aabb1122")
        assert meta.continuation == "new prompt"

    def test_amend_thread(self, store, monkeypatch, capsys):
        store.set(_make_meta("aabb1122"))
        args = build_parser().parse_args(
            ["amend", "aabb1122", "--thread", "1234.5678"]
        )
        ret = cmd_amend(args)
        assert ret == 0
        meta = store.get("aabb1122")
        assert meta.slack_thread_ts == "1234.5678"

    def test_amend_nothing(self, store, monkeypatch, capsys):
        store.set(_make_meta("aabb1122"))
        args = build_parser().parse_args(["amend", "aabb1122"])
        ret = cmd_amend(args)
        assert ret == 1


class TestCmdAssign:
    def test_assign_unknown_persona(self, store, monkeypatch, capsys):
        ret = main(["assign", "--to", "nonexist", "--prompt", "test", "--iters", "1"])
        assert ret == 2

    def test_assign_empty_prompt(self, store, monkeypatch, capsys):
        register_persona("helper", prompt="You help.", cwd="/tmp")
        ret = main(["assign", "--to", "helper", "--prompt", "   "])
        assert ret == 2

    def test_assign_prompt_file_missing(self, store, monkeypatch, capsys):
        register_persona("helper", prompt="You help.", cwd="/tmp")
        ret = main(["assign", "--to", "helper", "--prompt-file", "/nonexistent/path.md"])
        assert ret == 2

    def test_assign_success(self, store, monkeypatch, capsys):
        register_persona("helper", prompt="You help.", cwd="/tmp")
        # We need to mock subprocess.Popen since we don't want to spawn
        with patch("tigerharness.task_runner.cli.subprocess.Popen") as mock_popen:
            mock_popen.return_value.pid = 12345
            ret = main(["assign", "--to", "helper", "--prompt", "do stuff", "--iters", "3"])
        assert ret == 0
        out = capsys.readouterr().out
        assert "Assigned:" in out
        assert "helper" in out

    def test_assign_with_worktree_repo_records_path(
        self, store, monkeypatch, capsys, tmp_path,
    ):
        """``--worktree-repo`` on a valid git repo records the resolved
        absolute path in JobMeta and surfaces the planned worktree
        location in the assign output."""
        import subprocess as _sp
        register_persona("helper", prompt="You help.", cwd="/tmp")
        repo = tmp_path / "repo"
        repo.mkdir()
        _sp.run(["git", "init", "-q", str(repo)], check=True)
        with patch("tigerharness.task_runner.cli.subprocess.Popen") as mock_popen:
            mock_popen.return_value.pid = 12345
            ret = main([
                "assign", "--to", "helper", "--prompt", "do stuff",
                "--worktree-repo", str(repo),
            ])
        assert ret == 0
        out = capsys.readouterr().out
        # The output names the worktree path so the operator can tail it.
        assert "worktree:" in out
        assert ".worktrees" in out

        # And the resolved absolute path is in JobMeta.
        jobs = list(store.all().values())
        assert any(
            m.worktree_repo == str(repo.resolve()) for m in jobs
        ), f"no job has worktree_repo set; jobs={jobs}"

    def test_assign_worktree_repo_not_a_git_repo_errors(
        self, store, monkeypatch, capsys, tmp_path,
    ):
        """A path that isn't a git repo must error at CLI time -- the
        detached runner shouldn't get a chance to crash on this."""
        register_persona("helper", prompt="You help.", cwd="/tmp")
        bogus = tmp_path / "not-a-repo"
        bogus.mkdir()
        with patch("tigerharness.task_runner.cli.subprocess.Popen") as mock_popen:
            mock_popen.return_value.pid = 12345
            ret = main([
                "assign", "--to", "helper", "--prompt", "do stuff",
                "--worktree-repo", str(bogus),
            ])
        assert ret == 2
        err = capsys.readouterr().err
        assert "not a git repository" in err
        # No Popen happened.
        mock_popen.assert_not_called()


class TestCmdContinue:
    def test_continue_running_fails(self, store, monkeypatch, capsys):
        register_persona("helper", prompt="test", cwd="/tmp")
        store.set(_make_meta("aabb1122", status="running", current_iter=5))
        ret = main(["continue", "aabb1122", "--iters", "5"])
        assert ret == 1

    def test_continue_pending_fails(self, store, monkeypatch, capsys):
        register_persona("helper", prompt="test", cwd="/tmp")
        store.set(_make_meta("aabb1122", status="pending"))
        ret = main(["continue", "aabb1122", "--iters", "5"])
        assert ret == 1

    def test_continue_done_success(self, store, monkeypatch, capsys):
        register_persona("helper", prompt="test", cwd="/tmp")
        store.set(_make_meta("aabb1122", status="done", current_iter=5, session_id="s1"))
        with patch("tigerharness.task_runner.cli.subprocess.Popen") as mock_popen:
            mock_popen.return_value.pid = 99999
            ret = main(["continue", "aabb1122", "--iters", "5"])
        assert ret == 0
        out = capsys.readouterr().out
        assert "Continuing:" in out


class TestCmdLogs:
    def test_logs_no_log_yet(self, store, monkeypatch, capsys):
        store.set(_make_meta("aabb1122"))
        ret = main(["logs", "aabb1122"])
        assert ret == 0
        assert "no log yet" in capsys.readouterr().out

    def test_logs_existing(self, store, monkeypatch, capsys):
        store.set(_make_meta("aabb1122"))
        store.run_log("aabb1122").write_text('{"kind":"start"}\n')
        ret = main(["logs", "aabb1122"])
        assert ret == 0
        assert "start" in capsys.readouterr().out

    def test_logs_unknown_job(self, store, monkeypatch, capsys):
        ret = main(["logs", "zzzz"])
        assert ret == 1


class TestCmdPersonas:
    def test_personas_empty(self, monkeypatch, capsys):
        ret = main(["personas"])
        assert ret == 0

    def test_personas_with_registered(self, monkeypatch, capsys):
        register_persona("helper", prompt="test", cwd="/tmp", description="Helps")
        ret = main(["personas"])
        assert ret == 0
        assert "helper" in capsys.readouterr().out

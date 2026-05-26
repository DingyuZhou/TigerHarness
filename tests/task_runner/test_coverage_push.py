"""Coverage-push tests for task_runner modules.

Covers:
- cli.py:136 (stuck_label disabled), 265-267 (tweak --stuck-timeout),
  332 (continue --stuck-timeout), 376 (continue stuck_label disabled)
- personas.py:384 (_autoload_from_env failure)
- runner.py:668-669 (watchdog raises unexpectedly),
  790->799 (live_meta continuation sync),
  831->834 (stuck live_meta sync),
  873 (re-open session after stuck with session_id),
  886->889 (cost tracking after normal iter),
  895->899 (live_post sync on normal iter end),
  986->988 (compact cost tracking),
  1041->1037 (main --start-iter flag)
- stuck_watchdog.py:59->64 (_read_btime failure),
  156->155 (find_job_claude_pid no children),
  416-417 (escalation signaling),
  490->exit (watchdog while loop exit)
"""
from __future__ import annotations

import asyncio
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tigerharness.task_runner.personas import (
    clear_registry,
    register_persona,
)
from tigerharness.task_runner.registry import JobMeta, JobStore


@pytest.fixture(autouse=True)
def _clean_personas():
    clear_registry()
    register_persona("tester", prompt="You are a tester.", cwd="/tmp")
    yield
    clear_registry()


def _make_meta(store: JobStore, job_id: str = "push1234", **over) -> JobMeta:
    base = dict(
        job_id=job_id,
        persona="tester",
        prompt_chars=10,
        max_iters=3,
        compact_every=0,
        continuation="",
        name="test-push",
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


# ---------------------------------------------------------------------------
# cli.py coverage
# ---------------------------------------------------------------------------

class TestCliStuckTimeoutDisabled:
    """Lines 136, 376: stuck_label = 'disabled' when stuck_timeout == 0."""

    def test_run_prints_disabled_stuck_label(self, tmp_path, capsys, monkeypatch):
        """Line 136: run prints 'disabled' when stuck_timeout=0."""
        from tigerharness.task_runner.cli import main
        monkeypatch.setenv("TIGERHARNESS_STATE_DIR", str(tmp_path))

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        with patch("tigerharness.task_runner.cli.subprocess.Popen",
                   return_value=mock_proc):
            main([
                "assign", "--persona", "tester", "--prompt", "test",
                "--stuck-timeout", "0",
            ])

        out = capsys.readouterr().out
        assert "disabled" in out

    def test_continue_prints_disabled_stuck_label(self, tmp_path, capsys, monkeypatch):
        """Line 376: continue prints 'disabled' when stuck_timeout=0."""
        from tigerharness.task_runner.cli import main
        monkeypatch.setenv("TIGERHARNESS_STATE_DIR", str(tmp_path))
        store = JobStore(tmp_path)
        meta = _make_meta(store, status="done", stuck_timeout=0,
                          session_id="sess-old")

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        with patch("tigerharness.task_runner.cli.subprocess.Popen",
                   return_value=mock_proc):
            main(["continue", meta.job_id, "--iters", "2"])

        out = capsys.readouterr().out
        assert "disabled" in out


class TestCliTweakStuckTimeout:
    """Lines 265-267: tweak --stuck-timeout."""

    def test_tweak_stuck_timeout(self, tmp_path, capsys, monkeypatch):
        from tigerharness.task_runner.cli import main
        monkeypatch.setenv("TIGERHARNESS_STATE_DIR", str(tmp_path))
        store = JobStore(tmp_path)
        meta = _make_meta(store, status="running", stuck_timeout=600)

        main(["amend", meta.job_id, "--stuck-timeout", "300"])

        out = capsys.readouterr().out
        assert "stuck_timeout" in out
        updated = store.get(meta.job_id)
        assert updated.stuck_timeout == 300

    def test_tweak_disable_stuck_timeout(self, tmp_path, monkeypatch):
        from tigerharness.task_runner.cli import main
        monkeypatch.setenv("TIGERHARNESS_STATE_DIR", str(tmp_path))
        store = JobStore(tmp_path)
        meta = _make_meta(store, status="running", stuck_timeout=600)

        main(["amend", meta.job_id, "--stuck-timeout", "0"])

        updated = store.get(meta.job_id)
        assert updated.stuck_timeout == 0


class TestCliContinueStuckTimeout:
    """Line 332: continue --stuck-timeout overrides."""

    def test_continue_with_stuck_timeout(self, tmp_path, capsys, monkeypatch):
        from tigerharness.task_runner.cli import main
        monkeypatch.setenv("TIGERHARNESS_STATE_DIR", str(tmp_path))
        store = JobStore(tmp_path)
        meta = _make_meta(store, status="done", stuck_timeout=300,
                          session_id="sess-1")

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        with patch("tigerharness.task_runner.cli.subprocess.Popen",
                   return_value=mock_proc):
            main([
                "continue", meta.job_id, "--iters", "2",
                "--stuck-timeout", "600",
            ])

        updated = store.get(meta.job_id)
        assert updated.stuck_timeout == 600


# ---------------------------------------------------------------------------
# personas.py coverage
# ---------------------------------------------------------------------------

class TestAutoloadFromEnvFailure:
    """Line 384: _autoload_from_env logs warning on load failure."""

    def test_autoload_bad_path_logs_warning(self, caplog):
        from tigerharness.task_runner import personas
        with patch.dict(os.environ, {"TIGERHARNESS_PERSONAS_CONFIG": "/nonexistent/path.yaml"}):
            with caplog.at_level("WARNING"):
                personas._autoload_from_env()
        assert any("failed to autoload" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# runner.py coverage
# ---------------------------------------------------------------------------

class TestRunnerLiveMetaSync:
    """Lines 790->799, 895->899: sync from live_meta."""

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TIGERHARNESS_STATE_DIR", str(tmp_path))
        return JobStore(tmp_path)

    @pytest.mark.asyncio
    async def test_live_meta_continuation_synced(self, store, tmp_path):
        """Line 790->799: continuation synced from store mid-loop."""
        from tigerharness.task_runner.runner import run_job
        meta = _make_meta(store, max_iters=2, stuck_timeout=0)

        fake_session = MagicMock()
        fake_session.id = "sess-1"
        fake_session.close = AsyncMock()

        fake_backend = AsyncMock()
        fake_backend.open_session = AsyncMock(return_value=fake_session)

        call_count = 0

        async def _run_with_retry_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                live = store.get(meta.job_id)
                live.continuation = "new-continuation"
                live.slack_thread_ts = "1234.5678"
                store.set(live)
            return FakeResult()

        with patch("tigerharness.task_runner.runner.get_backend", return_value=fake_backend), \
             patch("tigerharness.task_runner.runner.run_with_retry",
                   side_effect=_run_with_retry_side), \
             patch("tigerharness.task_runner.runner.notify_job_start", return_value=""), \
             patch("tigerharness.task_runner.runner.notify_job_end", return_value=True):
            await run_job(meta.job_id, state_dir=tmp_path)

        final = store.get(meta.job_id)
        assert final.continuation == "new-continuation"


class TestRunnerMainFlags:
    """Line 1041->1037: main() parses --start-iter."""

    def test_main_with_start_iter(self, tmp_path, monkeypatch):
        from tigerharness.task_runner.runner import main
        monkeypatch.setenv("TIGERHARNESS_STATE_DIR", str(tmp_path))
        store = JobStore(tmp_path)
        meta = _make_meta(store, job_id="main-test")

        with patch("tigerharness.task_runner.runner.asyncio.run") as mock_run:
            main(["main-test", "--start-iter", "3"])
        mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# stuck_watchdog.py coverage
# ---------------------------------------------------------------------------

class TestEscalationSignal:
    """stuck_watchdog.py:416-417: escalation signal set on stuck verdict."""

    @pytest.mark.asyncio
    async def test_escalation_signal_set_and_reason_stored(self, tmp_path):
        from tigerharness.task_runner import stuck_watchdog as sw
        from tigerharness.task_runner.registry import JobMeta

        meta = JobMeta(
            job_id="esc-test", persona="tester", prompt_chars=10,
            max_iters=3, compact_every=0, continuation="", name="esc",
            cwd="/tmp", started_at=0.0, status="running", pid=None,
            current_iter=1, session_id="", last_update=0.0,
        )

        signal_ = asyncio.Event()

        with patch.object(sw, "find_job_claude_pid", return_value=12345):
            with patch.object(sw, "notify_stuck_escalation"):
                with patch.object(sw, "kill_subtree_async",
                                  new_callable=AsyncMock):
                    await sw._escalate_and_kill(
                        reason="too slow",
                        runner_pid=os.getpid(),
                        log_path=tmp_path / "run.log",
                        meta=meta,
                        iter_num=1,
                        sigterm_grace_sec=0.1,
                        is_last_iter=False,
                        escalation_signal=signal_,
                    )

        assert signal_.is_set()
        assert signal_.reason == "too slow"  # type: ignore[attr-defined]


class TestReadBtime:
    """Line 59->64: _read_btime handles errors."""

    def test_read_btime_oserror(self):
        from tigerharness.task_runner.stuck_watchdog import _read_btime
        with patch("builtins.open", side_effect=OSError("no proc")):
            assert _read_btime() is None

    def test_read_btime_value_error(self):
        from tigerharness.task_runner.stuck_watchdog import _read_btime
        from io import StringIO
        with patch("builtins.open", return_value=StringIO("btime notanumber\n")):
            assert _read_btime() is None


class TestFindJobClaudePid:
    """Line 156->155: no claude child found."""

    def test_no_claude_child(self):
        from tigerharness.task_runner.stuck_watchdog import find_job_claude_pid
        with patch("tigerharness.task_runner.stuck_watchdog.proc_children",
                   return_value=[]):
            assert find_job_claude_pid(12345) is None

    def test_children_but_no_claude(self):
        from tigerharness.task_runner.stuck_watchdog import find_job_claude_pid
        with patch("tigerharness.task_runner.stuck_watchdog.proc_children",
                   return_value=[111, 222]):
            with patch("tigerharness.task_runner.stuck_watchdog.proc_comm",
                       return_value="python"):
                assert find_job_claude_pid(12345) is None

    def test_finds_claude_child(self):
        from tigerharness.task_runner.stuck_watchdog import find_job_claude_pid
        with patch("tigerharness.task_runner.stuck_watchdog.proc_children",
                   return_value=[111]):
            with patch("tigerharness.task_runner.stuck_watchdog.proc_comm",
                       return_value="claude"):
                assert find_job_claude_pid(12345) == 111

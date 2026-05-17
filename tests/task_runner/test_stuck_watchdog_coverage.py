"""Coverage-bump tests for stuck_watchdog.py.

These exercise defensive error paths in the /proc helpers and a few
hard-to-reach branches in the watchdog loop that natural integration
tests don't cover.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from tigerharness.task_runner import registry, stuck_watchdog as sw


# ---------------------------------------------------------------------------
# /proc helpers: missing-pid paths return safe defaults
# ---------------------------------------------------------------------------

class TestProcHelpersOnMissingPid:
    NONEXISTENT = 99999999  # essentially guaranteed not to exist

    def test_proc_children_returns_empty(self):
        assert sw.proc_children(self.NONEXISTENT) == []

    def test_proc_start_time_returns_none(self):
        assert sw.proc_start_time(self.NONEXISTENT) is None

    def test_proc_state_returns_empty(self):
        assert sw.proc_state(self.NONEXISTENT) == ""

    def test_proc_cpu_ticks_returns_zero(self):
        assert sw.proc_cpu_ticks(self.NONEXISTENT) == 0

    def test_subtree_cpu_ticks_returns_zero(self):
        assert sw.subtree_cpu_ticks(self.NONEXISTENT) == 0

    def test_descendants_of_returns_empty(self):
        assert sw.descendants_of(self.NONEXISTENT) == []

    def test_find_job_claude_pid_returns_none(self):
        assert sw.find_job_claude_pid(self.NONEXISTENT) is None


# ---------------------------------------------------------------------------
# /proc helpers: corrupt-data parse fallbacks
# ---------------------------------------------------------------------------

class TestProcHelpersOnCorruptInput:
    def test_proc_children_swallows_valueerror(self, tmp_path, monkeypatch):
        """If /proc/<pid>/task/<pid>/children contains non-int data, return []."""
        fake_path = tmp_path / "children"
        fake_path.write_text("not-an-int garbage")

        real_open = open

        def fake_open(path, *a, **kw):
            if "children" in str(path):
                return real_open(fake_path)
            return real_open(path, *a, **kw)

        with patch("builtins.open", fake_open):
            assert sw.proc_children(os.getpid()) == []

    def test_proc_start_time_swallows_valueerror(self, monkeypatch):
        """If field 19 of /proc/<pid>/stat is non-int, return None."""
        # _read_stat_fields with a forged stat line. Field 19 (post-comm
        # index 19) must be the non-int.
        fake_fields = ["X"] * 30  # all non-int
        monkeypatch.setattr(sw, "_read_stat_fields", lambda pid: fake_fields)
        # _BTIME might be None on weird systems; force a value.
        monkeypatch.setattr(sw, "_BTIME", 1000)
        assert sw.proc_start_time(os.getpid()) is None

    def test_proc_cpu_ticks_swallows_valueerror(self, monkeypatch):
        """If utime/stime fields aren't ints, return 0."""
        fake_fields = ["X"] * 30
        monkeypatch.setattr(sw, "_read_stat_fields", lambda pid: fake_fields)
        assert sw.proc_cpu_ticks(os.getpid()) == 0

    def test_proc_start_time_returns_none_when_btime_unknown(self, monkeypatch):
        monkeypatch.setattr(sw, "_BTIME", None)
        # Don't care about the pid; we exit on the btime check.
        assert sw.proc_start_time(os.getpid()) is None

    def test_proc_start_time_returns_none_when_too_few_fields(self, monkeypatch):
        monkeypatch.setattr(sw, "_BTIME", 1000)
        monkeypatch.setattr(sw, "_read_stat_fields", lambda pid: ["only", "five", "fields"])
        assert sw.proc_start_time(os.getpid()) is None

    def test_proc_cpu_ticks_returns_zero_when_too_few_fields(self, monkeypatch):
        monkeypatch.setattr(sw, "_read_stat_fields", lambda pid: ["a", "b", "c"])
        assert sw.proc_cpu_ticks(os.getpid()) == 0

    def test_read_stat_fields_returns_none_on_oserror(self, monkeypatch):
        """The OSError branch of _read_stat_fields."""

        def bad_open(*a, **kw):
            raise OSError("forced")

        with patch("builtins.open", bad_open):
            assert sw._read_stat_fields(os.getpid()) is None

    def test_read_stat_fields_returns_none_when_no_close_paren(self, monkeypatch):
        """The `rp < 0` branch of _read_stat_fields (no ')' in stat data)."""
        real_open = open

        class _FakeFile:
            def __init__(self, data):
                self.data = data
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def read(self):
                return self.data

        def fake_open(path, *a, **kw):
            if "/stat" in str(path):
                return _FakeFile("no-parens-here")
            return real_open(path, *a, **kw)

        with patch("builtins.open", fake_open):
            assert sw._read_stat_fields(os.getpid()) is None

    def test_read_btime_returns_none_on_oserror(self, monkeypatch):
        """The OSError branch of _read_btime."""
        def bad_open(*a, **kw):
            raise OSError("forced")

        with patch("builtins.open", bad_open):
            assert sw._read_btime() is None


# ---------------------------------------------------------------------------
# Kill helpers: missing pid + descendants_of empty cases
# ---------------------------------------------------------------------------

class TestKillHelpers:
    def test_sigterm_subtree_swallows_missing_pid(self):
        """ProcessLookupError on a dead pid is swallowed."""
        # Spawn + immediately kill so the pid is dead.
        p = subprocess.Popen(
            ["true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        dead_pid = p.pid
        p.wait()  # reaped
        # Should not raise.
        targets = sw.sigterm_subtree(dead_pid)
        assert dead_pid in targets

    def test_sigkill_pids_swallows_missing_pids(self):
        p = subprocess.Popen(
            ["true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        p.wait()
        sw.sigkill_pids([p.pid])  # no exception


# ---------------------------------------------------------------------------
# Watchdog loop: rarely-hit branches
# ---------------------------------------------------------------------------

def _meta_for_test(stuck_timeout: int = 1) -> registry.JobMeta:
    return registry.JobMeta(
        job_id="cov12345",
        persona="tester",
        prompt_chars=0,
        max_iters=1,
        compact_every=0,
        continuation="",
        name="cov",
        cwd="/tmp",
        started_at=time.time(),
        status="running",
        pid=os.getpid(),
        current_iter=1,
        session_id="",
        last_update=time.time(),
        stuck_timeout=stuck_timeout,
    )


_KNOBS = dict(
    recheck_sec=0.1,
    sample_window_sec=0.05,
    sigterm_grace_sec=0.1,
)


@pytest.mark.asyncio
async def test_watchdog_handles_gather_diagnostic_exception(monkeypatch, tmp_path):
    """When gather_diagnostic raises, the watchdog logs check_error and
    keeps looping until stop_event fires."""
    monkeypatch.setattr(sw, "notify_stuck_escalation",
                        lambda *a, **kw: pytest.fail("must not escalate"))
    monkeypatch.setattr(sw, "find_job_claude_pid", lambda pid: 1234)

    call_count = {"n": 0}

    async def boom_gather(*a, **kw):
        call_count["n"] += 1
        raise RuntimeError("forced diagnostic failure")

    monkeypatch.setattr(sw, "gather_diagnostic", boom_gather)

    stop = asyncio.Event()
    asyncio.get_event_loop().call_later(0.4, stop.set)

    await sw.stuck_watchdog(
        stop_event=stop,
        log_path=tmp_path / "run.log",
        meta=_meta_for_test(),
        iter_num=1,
        stuck_timeout_sec=0.1,
        runner_pid=os.getpid(),
        **_KNOBS,
    )

    assert call_count["n"] >= 1
    log_text = (tmp_path / "run.log").read_text()
    assert "check_error" in log_text


@pytest.mark.asyncio
async def test_watchdog_treats_unknown_agent_verdict_as_working(monkeypatch, tmp_path):
    """If the agent returns something that isn't STUCK or WORKING (e.g.
    'MAYBE'), the watchdog falls back to WORKING."""
    monkeypatch.setattr(sw, "notify_stuck_escalation",
                        lambda *a, **kw: pytest.fail("must not escalate"))
    monkeypatch.setattr(sw, "heuristic_verdict",
                        lambda d: ("UNCLEAR", "no bash children"))

    async def weird_agent(diagnostic):
        return ("MAYBE", "the agent is confused")

    p = subprocess.Popen(
        ["sleep", "30"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        monkeypatch.setattr(sw, "find_job_claude_pid", lambda pid: p.pid)
        stop = asyncio.Event()
        asyncio.get_event_loop().call_later(0.4, stop.set)
        await sw.stuck_watchdog(
            stop_event=stop,
            log_path=tmp_path / "run.log",
            meta=_meta_for_test(),
            iter_num=1,
            stuck_timeout_sec=0.1,
            agent_check_fn=weird_agent,
            runner_pid=os.getpid(),
            **_KNOBS,
        )
    finally:
        p.terminate()
        p.wait()


@pytest.mark.asyncio
async def test_escalate_kill_swallows_exception(monkeypatch, tmp_path):
    """If kill_subtree_async raises (not CancelledError), the watchdog
    logs and returns without crashing."""
    monkeypatch.setattr(sw, "notify_stuck_escalation", lambda *a, **kw: True)
    monkeypatch.setattr(sw, "heuristic_verdict",
                        lambda d: ("STUCK", "forced"))

    async def boom_kill(pid, *, grace_sec=0):
        raise RuntimeError("forced kill failure")

    p = subprocess.Popen(
        ["sleep", "30"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        monkeypatch.setattr(sw, "find_job_claude_pid", lambda pid: p.pid)
        monkeypatch.setattr(sw, "kill_subtree_async", boom_kill)
        stop = asyncio.Event()
        # Should NOT raise.
        await sw.stuck_watchdog(
            stop_event=stop,
            log_path=tmp_path / "run.log",
            meta=_meta_for_test(),
            iter_num=1,
            stuck_timeout_sec=0.1,
            runner_pid=os.getpid(),
            **_KNOBS,
        )
    finally:
        p.terminate()
        p.wait()


def test_log_event_swallows_oserror(monkeypatch, tmp_path):
    """If the run.log can't be written, _log_event silently returns."""
    bad_path = tmp_path / "definitely-not-writable"
    # Make the path a file so mkdir raises (parent is a file).
    bad_path.write_text("blocker")
    # Should not raise.
    sw._log_event(bad_path / "child.log", {"event": "test"})


# ---------------------------------------------------------------------------
# heuristic_verdict: the 'aged is empty' UNCLEAR branch
# ---------------------------------------------------------------------------

def test_heuristic_unclear_when_no_bash_has_readable_start():
    """If every bash child has age_sec=None (couldn't read start time),
    return UNCLEAR -- distinct from the 'no bash children' UNCLEAR."""
    d = sw.Diagnostic(
        claude=sw.ProcSample(pid=1000, comm="claude", state="S",
                              age_sec=None, cpu_ticks=0),
        bash_children=[
            sw.ProcSample(pid=2001, comm="bash", state="S",
                          age_sec=None, cpu_ticks=0),
        ],
    )
    verdict, reason = sw.heuristic_verdict(d)
    assert verdict == "UNCLEAR"
    assert "start times" in reason


# ---------------------------------------------------------------------------
# _proc_sample: start_time=None branch
# ---------------------------------------------------------------------------

def test_proc_sample_handles_unknown_start_time(monkeypatch):
    """When proc_start_time returns None, age_sec is None on the sample."""
    monkeypatch.setattr(sw, "proc_start_time", lambda pid: None)
    monkeypatch.setattr(sw, "proc_comm", lambda pid: "fake")
    monkeypatch.setattr(sw, "proc_state", lambda pid: "S")
    monkeypatch.setattr(sw, "proc_cpu_ticks", lambda pid: 42)
    s = sw._proc_sample(7777)
    assert s.age_sec is None
    assert s.cpu_ticks == 42


# ---------------------------------------------------------------------------
# render_diagnostic: empty bash_children branch
# ---------------------------------------------------------------------------

def test_render_diagnostic_no_bash_children():
    d = sw.Diagnostic(
        claude=sw.ProcSample(pid=1000, comm="claude", state="S",
                              age_sec=120.0, cpu_ticks=42),
        bash_children=[],
    )
    text = sw.render_diagnostic(d)
    assert "claude has no bash-tool subprocesses running" in text


def test_render_diagnostic_handles_unknown_ages():
    d = sw.Diagnostic(
        claude=sw.ProcSample(pid=1000, comm="claude", state="S",
                              age_sec=None, cpu_ticks=0),
        bash_children=[
            sw.ProcSample(pid=2001, comm="bash", state="S",
                          age_sec=None, cpu_ticks=0),
        ],
    )
    text = sw.render_diagnostic(d)
    # The "unknown" / "?" placeholders should be present, not crash.
    assert "unknown" in text
    assert "?" in text


# ---------------------------------------------------------------------------
# _build_agent_stuck_check: parse paths
# ---------------------------------------------------------------------------

class TestAgentStuckCheck:
    """Direct unit tests for the agent-fallback closure built by
    ``_build_agent_stuck_check``. We patch ``run_with_retry`` so no real
    backend call is made."""

    def _stub_agent_cfg(self):
        from tigerharness.agent_sdk.types import AgentConfig
        return AgentConfig(
            name="test",
            instructions="x",
            max_turns=5,
            extra={"some_key": "preserved"},
        )

    def _fake_backend(self):
        class _B:
            async def open_session(self, *, resume_id=None):
                class _S:
                    id = ""
                    async def close(self): pass
                return _S()
        return _B()

    @pytest.mark.asyncio
    async def test_parses_stuck_verdict(self):
        from tigerharness.task_runner.runner import _build_agent_stuck_check

        class _Result:
            final_output = "STUCK: claude is wedged"
            cost_usd = 0.0

        async def fake_rwr(*a, **kw):
            return _Result()

        check = _build_agent_stuck_check(
            self._fake_backend(), self._stub_agent_cfg(), job_id="abc",
        )
        with patch("tigerharness.task_runner.runner.run_with_retry", fake_rwr):
            verdict, reason = await check("diagnostic blob")
        assert verdict == "STUCK"
        assert "wedged" in reason

    @pytest.mark.asyncio
    async def test_parses_working_verdict(self):
        from tigerharness.task_runner.runner import _build_agent_stuck_check

        class _Result:
            final_output = "WORKING: making progress"
            cost_usd = 0.0

        async def fake_rwr(*a, **kw):
            return _Result()

        check = _build_agent_stuck_check(
            self._fake_backend(), self._stub_agent_cfg(), job_id="abc",
        )
        with patch("tigerharness.task_runner.runner.run_with_retry", fake_rwr):
            verdict, reason = await check("diagnostic blob")
        assert verdict == "WORKING"
        assert "progress" in reason

    @pytest.mark.asyncio
    async def test_treats_unparseable_as_working(self):
        from tigerharness.task_runner.runner import _build_agent_stuck_check

        class _Result:
            final_output = "uhhh I dunno"
            cost_usd = 0.0

        async def fake_rwr(*a, **kw):
            return _Result()

        check = _build_agent_stuck_check(
            self._fake_backend(), self._stub_agent_cfg(), job_id="abc",
        )
        with patch("tigerharness.task_runner.runner.run_with_retry", fake_rwr):
            verdict, reason = await check("diagnostic blob")
        assert verdict == "WORKING"
        assert "unparseable" in reason

    @pytest.mark.asyncio
    async def test_treats_timeout_as_working(self):
        from tigerharness.task_runner.runner import _build_agent_stuck_check

        async def slow_rwr(*a, **kw):
            await asyncio.sleep(10)

        check = _build_agent_stuck_check(
            self._fake_backend(), self._stub_agent_cfg(), job_id="abc",
        )

        # Make AGENT_STUCK_CHECK_TIMEOUT_SEC tiny via patching.
        with patch("tigerharness.task_runner.runner.run_with_retry", slow_rwr), \
             patch("tigerharness.task_runner.runner.AGENT_STUCK_CHECK_TIMEOUT_SEC", 0.1):
            verdict, reason = await check("diagnostic blob")
        assert verdict == "WORKING"
        assert "timed out" in reason

    @pytest.mark.asyncio
    async def test_stuck_no_colon_uses_default_reason(self):
        from tigerharness.task_runner.runner import _build_agent_stuck_check

        class _Result:
            final_output = "STUCK"  # no colon-separated reason
            cost_usd = 0.0

        async def fake_rwr(*a, **kw):
            return _Result()

        check = _build_agent_stuck_check(
            self._fake_backend(), self._stub_agent_cfg(), job_id="abc",
        )
        with patch("tigerharness.task_runner.runner.run_with_retry", fake_rwr):
            verdict, reason = await check("blob")
        assert verdict == "STUCK"
        assert "agent said STUCK" in reason

    @pytest.mark.asyncio
    async def test_working_no_colon_uses_default_reason(self):
        from tigerharness.task_runner.runner import _build_agent_stuck_check

        class _Result:
            final_output = "WORKING"
            cost_usd = 0.0

        async def fake_rwr(*a, **kw):
            return _Result()

        check = _build_agent_stuck_check(
            self._fake_backend(), self._stub_agent_cfg(), job_id="abc",
        )
        with patch("tigerharness.task_runner.runner.run_with_retry", fake_rwr):
            verdict, reason = await check("blob")
        assert verdict == "WORKING"
        assert "agent said WORKING" in reason

    @pytest.mark.asyncio
    async def test_session_close_failure_is_swallowed(self):
        """If session.close() raises in the finally block, the check still
        returns its verdict."""
        from tigerharness.task_runner.runner import _build_agent_stuck_check

        class _BadSession:
            id = ""
            async def close(self):
                raise RuntimeError("close failed")

        class _B:
            async def open_session(self, *, resume_id=None):
                return _BadSession()

        class _Result:
            final_output = "STUCK: ok"
            cost_usd = 0.0

        async def fake_rwr(*a, **kw):
            return _Result()

        check = _build_agent_stuck_check(
            _B(), self._stub_agent_cfg(), job_id="abc",
        )
        with patch("tigerharness.task_runner.runner.run_with_retry", fake_rwr):
            verdict, reason = await check("blob")
        assert verdict == "STUCK"

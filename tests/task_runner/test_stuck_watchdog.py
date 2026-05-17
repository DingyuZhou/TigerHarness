"""Tests for the hybrid per-iteration stuck-watchdog.

Covers:
- /proc helpers (against real subprocesses)
- kill helpers (SIGTERM cascade, SIGKILL stragglers)
- gather_diagnostic (sampling claude's tree)
- heuristic_verdict (the deterministic decision rules)
- stuck_watchdog loop (first-check timing, recheck cadence, escalation,
  agent fallback for UNCLEAR)
- Cancellation robustness (shielded SIGKILL)
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from tigerharness.task_runner import registry, stuck_watchdog as sw


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

def _spawn_sleep(duration: float = 30.0) -> subprocess.Popen:
    """Spawn a sleep subprocess. Caller must terminate."""
    return subprocess.Popen(
        ["sleep", str(duration)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _spawn_bash_tree() -> subprocess.Popen:
    """Spawn `bash -c 'sleep 60 & wait'` — a bash with a sleep child."""
    return subprocess.Popen(
        ["bash", "-c", "sleep 60 & wait"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _spawn_busy_bash() -> subprocess.Popen:
    """Spawn `bash -c 'while true; do :; done'` — burns CPU continuously.

    Used to verify the heuristic's "subtree CPU advancing → WORKING" path.
    """
    return subprocess.Popen(
        ["bash", "-c", "while true; do :; done"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _spawn_idle_bash() -> subprocess.Popen:
    """Spawn `bash -c 'sleep 60'` — alive but uses essentially zero CPU.

    Used to verify the heuristic's "subtree CPU flat → STUCK" path.
    """
    return subprocess.Popen(
        ["bash", "-c", "sleep 60"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _alive(pid: int) -> bool:
    """Zombies count as gone. Reads ``/proc/PID/stat`` state."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    rp = data.rfind(")")
    if rp < 0:
        return False
    fields = data[rp + 2:].split()
    if not fields:
        return False
    return fields[0] not in ("Z", "X")


def _wait_gone(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# /proc helpers
# ---------------------------------------------------------------------------

def test_proc_comm_returns_command_name() -> None:
    p = _spawn_sleep(5.0)
    try:
        assert sw.proc_comm(p.pid) == "sleep"
    finally:
        p.terminate()
        p.wait()


def test_proc_comm_empty_on_missing_pid() -> None:
    assert sw.proc_comm(99999999) == ""


def test_proc_children_lists_direct_children() -> None:
    p = _spawn_bash_tree()
    try:
        time.sleep(0.2)
        kids = sw.proc_children(p.pid)
        assert len(kids) >= 1
        assert any(sw.proc_comm(c) == "sleep" for c in kids)
    finally:
        p.terminate()
        p.wait()


def test_proc_children_empty_for_leaf() -> None:
    p = _spawn_sleep(5.0)
    try:
        assert sw.proc_children(p.pid) == []
    finally:
        p.terminate()
        p.wait()


def test_descendants_of_walks_full_tree() -> None:
    p = _spawn_bash_tree()
    try:
        time.sleep(0.2)
        descs = sw.descendants_of(p.pid)
        assert len(descs) >= 1
        assert "sleep" in [sw.proc_comm(d) for d in descs]
    finally:
        p.terminate()
        p.wait()


def test_find_job_claude_pid_returns_none_if_no_claude_child() -> None:
    assert sw.find_job_claude_pid(os.getpid()) is None


def test_find_job_claude_pid_finds_real_claude_named_process() -> None:
    """Spawn a child whose kernel ``comm`` is literally ``"claude"`` (via
    ``prctl(PR_SET_NAME)``) and verify ``find_job_claude_pid`` returns
    its pid.
    """
    script = (
        "import ctypes, time; "
        "ctypes.CDLL('libc.so.6').prctl(15, b'claude', 0, 0, 0); "
        "time.sleep(30)"
    )
    p = subprocess.Popen(
        ["python3", "-c", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if sw.proc_comm(p.pid) == "claude":
                break
            time.sleep(0.05)
        assert sw.proc_comm(p.pid) == "claude"
        assert sw.find_job_claude_pid(os.getpid()) == p.pid
    finally:
        p.terminate()
        p.wait()


def test_proc_state_returns_letter() -> None:
    p = _spawn_sleep(5.0)
    try:
        # sleep is in interruptible-sleep, S.
        time.sleep(0.05)
        assert sw.proc_state(p.pid) == "S"
    finally:
        p.terminate()
        p.wait()


def test_proc_cpu_ticks_increases_for_busy_loop() -> None:
    p = _spawn_busy_bash()
    try:
        time.sleep(0.05)
        # bash is a single process; cpu_ticks measures it alone.
        t1 = sw.proc_cpu_ticks(p.pid)
        time.sleep(0.3)
        t2 = sw.proc_cpu_ticks(p.pid)
        assert t2 > t1, f"busy bash should accumulate CPU ticks (t1={t1} t2={t2})"
    finally:
        p.terminate()
        p.wait()


def test_subtree_cpu_ticks_zero_for_idle_sleeping_subtree() -> None:
    p = _spawn_idle_bash()
    try:
        time.sleep(0.05)
        t1 = sw.subtree_cpu_ticks(p.pid)
        time.sleep(0.3)
        t2 = sw.subtree_cpu_ticks(p.pid)
        # An idle sleep should accumulate ~0 ticks (allow small jitter).
        assert t2 - t1 <= 1
    finally:
        p.terminate()
        p.wait()


# ---------------------------------------------------------------------------
# Kill helpers
# ---------------------------------------------------------------------------

def test_sigterm_subtree_kills_root_and_descendants() -> None:
    p = _spawn_bash_tree()
    try:
        time.sleep(0.2)
        sleep_pid = sw.proc_children(p.pid)[0]
        targets = sw.sigterm_subtree(p.pid)
        assert p.pid in targets and sleep_pid in targets
        assert _wait_gone(p.pid, timeout=3.0)
        assert _wait_gone(sleep_pid, timeout=3.0)
    finally:
        if _alive(p.pid):
            p.kill()
        p.wait()


def test_sigkill_pids_handles_already_dead() -> None:
    p = _spawn_sleep(5.0)
    pid = p.pid
    p.terminate()
    p.wait()
    sw.sigkill_pids([pid])  # no-op, no exception


async def test_kill_subtree_async_uses_async_sleep() -> None:
    p = _spawn_bash_tree()
    try:
        time.sleep(0.2)
        start = time.time()
        await sw.kill_subtree_async(p.pid, grace_sec=0.1)
        assert time.time() - start < 1.0
        assert _wait_gone(p.pid, timeout=3.0)
    finally:
        if _alive(p.pid):
            p.kill()
        p.wait()


# ---------------------------------------------------------------------------
# gather_diagnostic
# ---------------------------------------------------------------------------

async def test_gather_diagnostic_sees_bash_children_and_cpu_delta() -> None:
    """A claude-like parent with a busy bash child should show:
    - a bash entry in `bash_children`
    - a positive subtree CPU delta
    """
    p = _spawn_busy_bash()
    try:
        time.sleep(0.1)
        # Pretend `p` is claude — gather_diagnostic looks at its children.
        # But here we want bash_children to include the busy bash itself,
        # so we need a parent whose direct child IS the bash. The Popen
        # arrangement: our pytest process IS the parent of `p`. So pass
        # our own pid.
        d = await sw.gather_diagnostic(os.getpid(), sample_window_sec=0.3)
    finally:
        p.terminate()
        p.wait()

    pids = [c.pid for c in d.bash_children]
    assert p.pid in pids
    # busy bash should produce a positive delta.
    delta = d.bash_subtree_cpu_delta.get(p.pid, 0)
    assert delta > 0


async def test_gather_diagnostic_idle_bash_shows_zero_cpu_delta() -> None:
    p = _spawn_idle_bash()
    try:
        time.sleep(0.1)
        d = await sw.gather_diagnostic(os.getpid(), sample_window_sec=0.3)
    finally:
        p.terminate()
        p.wait()

    delta = d.bash_subtree_cpu_delta.get(p.pid, 0)
    assert delta == 0


# ---------------------------------------------------------------------------
# heuristic_verdict — pure function, easy to drive with fabricated Diagnostics
# ---------------------------------------------------------------------------

def _fake_claude_sample() -> sw.ProcSample:
    return sw.ProcSample(pid=1000, comm="claude", state="S",
                          age_sec=300.0, cpu_ticks=500)


def _fake_bash(pid: int, age_sec: float) -> sw.ProcSample:
    return sw.ProcSample(pid=pid, comm="bash", state="S",
                          age_sec=age_sec, cpu_ticks=10)


def test_heuristic_unclear_when_no_bash_children() -> None:
    d = sw.Diagnostic(claude=_fake_claude_sample(), bash_children=[])
    verdict, reason = sw.heuristic_verdict(d)
    assert verdict == "UNCLEAR"
    assert "no bash" in reason


def test_heuristic_stuck_when_oldest_bash_too_old() -> None:
    d = sw.Diagnostic(
        claude=_fake_claude_sample(),
        bash_children=[_fake_bash(2001, age_sec=700.0)],  # > 600s threshold
    )
    verdict, reason = sw.heuristic_verdict(d)
    assert verdict == "STUCK"
    assert "700" in reason


def test_heuristic_working_when_oldest_bash_fresh() -> None:
    d = sw.Diagnostic(
        claude=_fake_claude_sample(),
        bash_children=[_fake_bash(2001, age_sec=30.0)],  # < 60s threshold
    )
    verdict, reason = sw.heuristic_verdict(d)
    assert verdict == "WORKING"
    assert "fresh" in reason or "30" in reason


def test_heuristic_stuck_when_middle_age_bash_has_flat_cpu() -> None:
    d = sw.Diagnostic(
        claude=_fake_claude_sample(),
        bash_children=[_fake_bash(2001, age_sec=300.0)],  # in middle band
        bash_subtree_cpu_delta={2001: 0},
    )
    verdict, reason = sw.heuristic_verdict(d)
    assert verdict == "STUCK"
    assert "flat" in reason


def test_heuristic_working_when_middle_age_bash_has_cpu_advancing() -> None:
    d = sw.Diagnostic(
        claude=_fake_claude_sample(),
        bash_children=[_fake_bash(2001, age_sec=300.0)],
        bash_subtree_cpu_delta={2001: 50},
    )
    verdict, reason = sw.heuristic_verdict(d)
    assert verdict == "WORKING"
    assert "advancing" in reason


def test_heuristic_picks_oldest_bash_when_multiple() -> None:
    d = sw.Diagnostic(
        claude=_fake_claude_sample(),
        bash_children=[
            _fake_bash(2001, age_sec=30.0),    # fresh
            _fake_bash(2002, age_sec=800.0),   # very old
        ],
    )
    verdict, _ = sw.heuristic_verdict(d)
    assert verdict == "STUCK"  # the old one wins


# ---------------------------------------------------------------------------
# render_diagnostic
# ---------------------------------------------------------------------------

def test_render_diagnostic_includes_key_fields() -> None:
    d = sw.Diagnostic(
        claude=_fake_claude_sample(),
        bash_children=[_fake_bash(2001, age_sec=300.0)],
        bash_subtree_cpu_delta={2001: 42},
        sample_window_sec=2.0,
    )
    text = sw.render_diagnostic(d)
    assert "pid=1000" in text
    assert "pid=2001" in text
    assert "300s" in text
    assert "42 ticks" in text


# ---------------------------------------------------------------------------
# Watchdog state machine — short knobs everywhere so the suite is fast
# ---------------------------------------------------------------------------

def _meta_for_test(job_id: str = "abc12345", stuck_timeout: int = 1) -> registry.JobMeta:
    return registry.JobMeta(
        job_id=job_id, persona="sai", prompt_chars=0,
        max_iters=1, compact_every=0, continuation="",
        name="test", cwd="/tmp",
        started_at=time.time(), status="running", pid=os.getpid(),
        current_iter=1, session_id="", last_update=time.time(),
        stuck_timeout=stuck_timeout,
    )


# Test-friendly knobs used throughout the watchdog tests.
_KNOBS = dict(
    recheck_sec=0.1,
    sample_window_sec=0.05,
    sigterm_grace_sec=0.1,
)


async def test_watchdog_disabled_when_timeout_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sw, "notify_stuck_escalation",
                        lambda *a, **kw: pytest.fail("must not notify"))
    stop = asyncio.Event()
    start = time.time()
    await sw.stuck_watchdog(
        stop_event=stop,
        log_path=tmp_path / "run.log",
        meta=_meta_for_test(stuck_timeout=0),
        iter_num=1,
        stuck_timeout_sec=0,
        **_KNOBS,
    )
    assert time.time() - start < 0.3


async def test_watchdog_silent_when_dispatch_completes_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sw, "notify_stuck_escalation",
                        lambda *a, **kw: pytest.fail("must not notify"))

    stop = asyncio.Event()
    asyncio.get_event_loop().call_later(0.1, stop.set)

    await sw.stuck_watchdog(
        stop_event=stop,
        log_path=tmp_path / "run.log",
        meta=_meta_for_test(),
        iter_num=1,
        stuck_timeout_sec=2,
        **_KNOBS,
    )
    if (tmp_path / "run.log").exists():
        for line in (tmp_path / "run.log").read_text().splitlines():
            entry = json.loads(line)
            assert entry.get("event") not in ("escalate", "watchdog_fired")


async def test_watchdog_returns_silently_when_no_claude_after_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """First check after timeout: no claude child → dispatch is wrapping
    up naturally → log `check_no_claude` and exit. Don't escalate."""
    monkeypatch.setattr(sw, "notify_stuck_escalation",
                        lambda *a, **kw: pytest.fail("must not notify"))
    stop = asyncio.Event()
    await sw.stuck_watchdog(
        stop_event=stop,
        log_path=tmp_path / "run.log",
        meta=_meta_for_test(),
        iter_num=4,
        stuck_timeout_sec=0.2,
        runner_pid=os.getpid(),
        **_KNOBS,
    )
    entries = [
        json.loads(line)
        for line in (tmp_path / "run.log").read_text().splitlines()
    ]
    assert any(e.get("event") == "watchdog_fired" for e in entries)
    assert any(e.get("event") == "check_no_claude" for e in entries)
    assert not any(e.get("event") == "escalate" for e in entries)


async def test_watchdog_escalates_when_heuristic_says_stuck(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Heuristic verdict STUCK → escalate (log, notify, kill subtree)."""
    notifications: list[dict] = []
    monkeypatch.setattr(sw, "notify_stuck_escalation",
                        lambda meta, *, iter_num, detail="":
                            notifications.append({"iter": iter_num, "detail": detail}) or True)
    monkeypatch.setattr(sw, "heuristic_verdict",
                        lambda d: ("STUCK", "test forced verdict"))

    fake_claude = _spawn_sleep(30.0)
    try:
        monkeypatch.setattr(sw, "find_job_claude_pid",
                            lambda pid: fake_claude.pid)

        stop = asyncio.Event()
        await sw.stuck_watchdog(
            stop_event=stop,
            log_path=tmp_path / "run.log",
            meta=_meta_for_test(),
            iter_num=3,
            stuck_timeout_sec=0.1,
            runner_pid=os.getpid(),
            **_KNOBS,
        )
        assert _wait_gone(fake_claude.pid, timeout=3.0)
        assert len(notifications) == 1 and notifications[0]["iter"] == 3
        entries = [
            json.loads(line)
            for line in (tmp_path / "run.log").read_text().splitlines()
        ]
        assert any(e.get("event") == "escalate" for e in entries)
    finally:
        if _alive(fake_claude.pid):
            fake_claude.kill()
        fake_claude.wait()


async def test_watchdog_rechecks_when_heuristic_says_working(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Heuristic WORKING → wait recheck_sec → check again. When dispatch
    completes between checks, the loop exits cleanly without escalation."""
    monkeypatch.setattr(sw, "notify_stuck_escalation",
                        lambda *a, **kw: pytest.fail("must not notify"))
    monkeypatch.setattr(sw, "heuristic_verdict",
                        lambda d: ("WORKING", "test forced verdict"))

    fake_claude = _spawn_sleep(30.0)
    try:
        monkeypatch.setattr(sw, "find_job_claude_pid",
                            lambda pid: fake_claude.pid)

        stop = asyncio.Event()
        # Fire stop after a couple of check cycles.
        asyncio.get_event_loop().call_later(0.5, stop.set)

        await sw.stuck_watchdog(
            stop_event=stop,
            log_path=tmp_path / "run.log",
            meta=_meta_for_test(),
            iter_num=2,
            stuck_timeout_sec=0.1,
            runner_pid=os.getpid(),
            **_KNOBS,
        )
        # fake_claude should NOT have been killed.
        assert _alive(fake_claude.pid)

        entries = [
            json.loads(line)
            for line in (tmp_path / "run.log").read_text().splitlines()
        ]
        # At least one heuristic check ran; no escalate.
        assert any(e.get("event") == "check_heuristic"
                   and e.get("verdict") == "WORKING" for e in entries)
        assert not any(e.get("event") == "escalate" for e in entries)
    finally:
        if _alive(fake_claude.pid):
            fake_claude.kill()
        fake_claude.wait()


async def test_watchdog_calls_agent_when_heuristic_unclear(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Heuristic UNCLEAR → consult agent → STUCK → escalate."""
    monkeypatch.setattr(sw, "notify_stuck_escalation", lambda *a, **kw: True)
    monkeypatch.setattr(sw, "heuristic_verdict",
                        lambda d: ("UNCLEAR", "no bash children"))

    agent_calls: list[str] = []

    async def fake_agent(diagnostic: str) -> tuple[str, str]:
        agent_calls.append(diagnostic)
        return ("STUCK", "agent says wedged")

    fake_claude = _spawn_sleep(30.0)
    try:
        monkeypatch.setattr(sw, "find_job_claude_pid",
                            lambda pid: fake_claude.pid)

        stop = asyncio.Event()
        await sw.stuck_watchdog(
            stop_event=stop,
            log_path=tmp_path / "run.log",
            meta=_meta_for_test(),
            iter_num=1,
            stuck_timeout_sec=0.1,
            agent_check_fn=fake_agent,
            runner_pid=os.getpid(),
            **_KNOBS,
        )
        assert len(agent_calls) == 1
        assert "pid=" in agent_calls[0]
        assert _wait_gone(fake_claude.pid, timeout=3.0)
    finally:
        if _alive(fake_claude.pid):
            fake_claude.kill()
        fake_claude.wait()


async def test_watchdog_unclear_without_agent_treats_as_working(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No agent provided + UNCLEAR → conservative default: keep waiting."""
    monkeypatch.setattr(sw, "notify_stuck_escalation",
                        lambda *a, **kw: pytest.fail("must not notify"))
    monkeypatch.setattr(sw, "heuristic_verdict",
                        lambda d: ("UNCLEAR", "no bash children"))

    fake_claude = _spawn_sleep(30.0)
    try:
        monkeypatch.setattr(sw, "find_job_claude_pid",
                            lambda pid: fake_claude.pid)

        stop = asyncio.Event()
        asyncio.get_event_loop().call_later(0.4, stop.set)

        await sw.stuck_watchdog(
            stop_event=stop,
            log_path=tmp_path / "run.log",
            meta=_meta_for_test(),
            iter_num=1,
            stuck_timeout_sec=0.1,
            # No agent_check_fn.
            runner_pid=os.getpid(),
            **_KNOBS,
        )
        assert _alive(fake_claude.pid)
    finally:
        if _alive(fake_claude.pid):
            fake_claude.kill()
        fake_claude.wait()


async def test_watchdog_agent_raising_treated_as_working(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the agent_check_fn raises, treat as WORKING and keep waiting."""
    monkeypatch.setattr(sw, "notify_stuck_escalation",
                        lambda *a, **kw: pytest.fail("must not notify"))
    monkeypatch.setattr(sw, "heuristic_verdict",
                        lambda d: ("UNCLEAR", "no bash children"))

    async def bad_agent(diagnostic: str) -> tuple[str, str]:
        raise RuntimeError("agent backend exploded")

    fake_claude = _spawn_sleep(30.0)
    try:
        monkeypatch.setattr(sw, "find_job_claude_pid",
                            lambda pid: fake_claude.pid)

        stop = asyncio.Event()
        asyncio.get_event_loop().call_later(0.4, stop.set)

        await sw.stuck_watchdog(
            stop_event=stop,
            log_path=tmp_path / "run.log",
            meta=_meta_for_test(),
            iter_num=1,
            stuck_timeout_sec=0.1,
            agent_check_fn=bad_agent,
            runner_pid=os.getpid(),
            **_KNOBS,
        )
        # Process still alive — we never escalated.
        assert _alive(fake_claude.pid)
        entries = [
            json.loads(line)
            for line in (tmp_path / "run.log").read_text().splitlines()
        ]
        assert any(e.get("event") == "check_agent_error" for e in entries)
    finally:
        if _alive(fake_claude.pid):
            fake_claude.kill()
        fake_claude.wait()


async def test_watchdog_dm_wording_reflects_is_last_iter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Slack DM detail should say 'continuing with next iteration'
    on non-last iters and 'last in run' on the final iter — so the CEO
    can tell at a glance whether the job is still running or has ended."""
    captured: list[dict] = []

    def capture_notify(meta, *, iter_num, detail=""):
        captured.append({"iter": iter_num, "detail": detail})
        return True

    monkeypatch.setattr(sw, "notify_stuck_escalation", capture_notify)
    monkeypatch.setattr(sw, "heuristic_verdict",
                        lambda d: ("STUCK", "test forced"))

    # --- not-last iter ---
    fake_claude_1 = _spawn_sleep(30.0)
    try:
        monkeypatch.setattr(sw, "find_job_claude_pid",
                            lambda pid: fake_claude_1.pid)
        stop = asyncio.Event()
        await sw.stuck_watchdog(
            stop_event=stop, log_path=tmp_path / "run.log",
            meta=_meta_for_test(), iter_num=2,
            stuck_timeout_sec=0.1, is_last_iter=False,
            runner_pid=os.getpid(),
            **_KNOBS,
        )
    finally:
        if _alive(fake_claude_1.pid):
            fake_claude_1.kill()
        fake_claude_1.wait()

    # --- last iter (separate watchdog run) ---
    fake_claude_2 = _spawn_sleep(30.0)
    try:
        monkeypatch.setattr(sw, "find_job_claude_pid",
                            lambda pid: fake_claude_2.pid)
        stop = asyncio.Event()
        await sw.stuck_watchdog(
            stop_event=stop, log_path=tmp_path / "run.log",
            meta=_meta_for_test(), iter_num=5,
            stuck_timeout_sec=0.1, is_last_iter=True,
            runner_pid=os.getpid(),
            **_KNOBS,
        )
    finally:
        if _alive(fake_claude_2.pid):
            fake_claude_2.kill()
        fake_claude_2.wait()

    assert len(captured) == 2
    non_last = captured[0]
    last = captured[1]
    assert non_last["iter"] == 2
    assert "continuing with next iteration" in non_last["detail"]
    assert "last in run" not in non_last["detail"]
    assert last["iter"] == 5
    assert "last in run" in last["detail"]
    assert "continuing with next iteration" not in last["detail"]


async def test_watchdog_no_claude_skips_signal_and_notify(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If claude has already exited by the time we go to kill it, we
    must NOT set the escalation_signal or send a Slack DM — the iter
    completed naturally, so an escalation report would be a false alarm.
    """
    monkeypatch.setattr(sw, "find_job_claude_pid", lambda pid: None)
    monkeypatch.setattr(sw, "notify_stuck_escalation",
                        lambda *a, **kw: pytest.fail("must not notify when no claude"))
    monkeypatch.setattr(sw, "heuristic_verdict",
                        lambda d: ("STUCK", "test forced"))

    stop = asyncio.Event()
    signal_ = asyncio.Event()
    # gather_diagnostic is called BEFORE find_job_claude_pid is re-checked
    # inside _escalate_and_kill. Mock it so the watchdog reaches the
    # STUCK branch.
    async def fake_gather(*a, **kw):
        return sw.Diagnostic(claude=sw.ProcSample(pid=0, comm="", state="",
                                                     age_sec=None, cpu_ticks=0))
    monkeypatch.setattr(sw, "gather_diagnostic", fake_gather)
    # Override find_job_claude_pid to return a pid initially (so the
    # diagnostic-gather phase doesn't bail out), then None during escalate.
    call_count = {"n": 0}
    def fjcp(pid):
        call_count["n"] += 1
        return os.getpid() if call_count["n"] == 1 else None
    monkeypatch.setattr(sw, "find_job_claude_pid", fjcp)

    await sw.stuck_watchdog(
        stop_event=stop, log_path=tmp_path / "run.log",
        meta=_meta_for_test(), iter_num=1,
        stuck_timeout_sec=0.1,
        escalation_signal=signal_,
        runner_pid=os.getpid(),
        **_KNOBS,
    )
    # signal must NOT have been set since the kill was skipped.
    assert not signal_.is_set()


async def test_watchdog_swallows_notify_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A raising notify_stuck_escalation must not prevent the kill path."""
    def bad_notify(*a, **kw):
        raise RuntimeError("slack is down")
    monkeypatch.setattr(sw, "notify_stuck_escalation", bad_notify)
    monkeypatch.setattr(sw, "heuristic_verdict",
                        lambda d: ("STUCK", "test forced"))

    fake_claude = _spawn_sleep(30.0)
    try:
        monkeypatch.setattr(sw, "find_job_claude_pid",
                            lambda pid: fake_claude.pid)
        stop = asyncio.Event()
        await sw.stuck_watchdog(
            stop_event=stop,
            log_path=tmp_path / "run.log",
            meta=_meta_for_test(),
            iter_num=1,
            stuck_timeout_sec=0.1,
            runner_pid=os.getpid(),
            **_KNOBS,
        )
        assert _wait_gone(fake_claude.pid, timeout=3.0)
    finally:
        if _alive(fake_claude.pid):
            fake_claude.kill()
        fake_claude.wait()


def _spawn_sigterm_ignoring() -> subprocess.Popen:
    script = (
        "import signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(30)"
    )
    return subprocess.Popen(
        ["python3", "-c", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def test_watchdog_sigkill_fires_even_when_cancelled_mid_grace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The wrapper cancels the watchdog after dispatch returns. With the
    shielded kill, SIGKILL still fires for SIGTERM-resistant subtrees."""
    monkeypatch.setattr(sw, "notify_stuck_escalation", lambda *a, **kw: True)
    monkeypatch.setattr(sw, "heuristic_verdict",
                        lambda d: ("STUCK", "test forced"))

    sigterm_resistant = _spawn_sigterm_ignoring()
    try:
        time.sleep(0.3)  # give python time to install SIG_IGN
        monkeypatch.setattr(sw, "find_job_claude_pid",
                            lambda pid: sigterm_resistant.pid)

        stop = asyncio.Event()
        watchdog_task = asyncio.create_task(sw.stuck_watchdog(
            stop_event=stop,
            log_path=tmp_path / "run.log",
            meta=_meta_for_test(),
            iter_num=1,
            stuck_timeout_sec=0.1,
            recheck_sec=0.1,
            sample_window_sec=0.05,
            sigterm_grace_sec=1.0,  # long-ish so we can cancel mid-grace
            runner_pid=os.getpid(),
        ))

        # Let the watchdog fire, heuristic STUCK, escalate, SIGTERM, enter grace.
        await asyncio.sleep(0.6)
        watchdog_task.cancel()
        try:
            await watchdog_task
        except asyncio.CancelledError:
            pass

        deadline = time.time() + 3.0
        while time.time() < deadline:
            if not _alive(sigterm_resistant.pid):
                break
            await asyncio.sleep(0.05)
        assert not _alive(sigterm_resistant.pid)
    finally:
        if _alive(sigterm_resistant.pid):
            sigterm_resistant.kill()
        sigterm_resistant.wait()

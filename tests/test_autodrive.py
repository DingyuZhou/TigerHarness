"""Tests for ``tigerharness autodrive`` (runner core + CLI).

The whole point of the module's dependency-injection seams is that the
loop, the clock, the backend, and process spawn/kill are all injectable,
so this suite never spawns a real subprocess or sends a real signal.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import pytest

from tests.conftest import RealDaemonSpawnBlocked
from tigerharness.autodrive import cli, runner
from tigerharness.autodrive.notifier import (
    NullNotifier,
    SlackChannelNotifier,
    build_notifier,
)
from tigerharness.autodrive.runner import AutodriveConfig


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _cfg(**over) -> AutodriveConfig:
    base = dict(
        interval_seconds=600.0,
        driver="Anzai",
        backend="claude_p",
        model=None,
        max_budget_usd=None,
        permission_mode="bypassPermissions",
        prompt="drive the journal",
        cwd=".",
    )
    base.update(over)
    return AutodriveConfig(**base)


class _FakeResult:
    def __init__(self, stop_reason="end_turn", cost_usd=0.0, final_output=None):
        self.stop_reason = stop_reason
        self.cost_usd = cost_usd
        self.final_output = final_output


class _RecordingNotifier:
    """Captures heartbeat/update calls so tests can assert the notification
    flow without any real Slack I/O. Returns a deterministic thread handle
    per heartbeat so completions can be matched to their fire."""

    def __init__(self):
        self.heartbeats = []
        self.updates = []
        self._n = 0

    def heartbeat(self, text):
        self._n += 1
        self.heartbeats.append(text)
        return f"ts-{self._n}"

    def update(self, thread, text):
        self.updates.append((thread, text))


# --------------------------------------------------------------------------
# default_prompt
# --------------------------------------------------------------------------

def test_default_prompt_with_driver():
    p = runner.default_prompt("Anzai")
    assert "--driver Anzai --allow-api-drive" in p
    assert "Operator-authorized" in p
    assert "drive-journal" in p
    # Vendor-neutral wording: the prompt is handed to claude -p and codex
    # exec alike, so it names capabilities, not one vendor's tools.
    assert "never drive from a headless CLI / cron / API" in p
    assert "helper sessions (sub-agents)" in p
    assert "Task-tool" not in p and "claude -p" not in p


def test_default_prompt_without_driver():
    p = runner.default_prompt(None)
    assert "--driver" not in p
    assert "--allow-api-drive" in p


# --------------------------------------------------------------------------
# config (de)serialization
# --------------------------------------------------------------------------

def test_config_dict_roundtrip():
    cfg = _cfg(model="opus", max_budget_usd=5.0)
    d = runner.config_to_dict(cfg)
    back = runner.config_from_state(d)
    assert back == cfg


def test_config_from_state_defaults():
    cfg = runner.config_from_state(
        {"interval_seconds": 120, "prompt": "go"}
    )
    assert cfg.backend == runner.DEFAULT_BACKEND
    assert cfg.permission_mode == runner.DEFAULT_PERMISSION_MODE
    assert cfg.driver is None
    assert cfg.cwd == "."


# --------------------------------------------------------------------------
# clock
# --------------------------------------------------------------------------

def test_utcnow_iso_shape():
    s = runner.utcnow_iso()
    assert s.endswith("Z")
    assert "T" in s and len(s) == 20


# --------------------------------------------------------------------------
# state file I/O
# --------------------------------------------------------------------------

def test_state_and_log_path(tmp_path):
    assert runner.state_path(tmp_path).name == ".autodrive.json"
    assert runner.log_path(tmp_path).name == ".autodrive.log"


def test_read_state_missing(tmp_path):
    assert runner.read_state(tmp_path / "nope.json") is None


def test_read_state_corrupt(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{not json", encoding="utf-8")
    assert runner.read_state(p) is None


def test_read_state_non_dict(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    assert runner.read_state(p) is None


def test_read_state_parent_not_dir(tmp_path):
    # A path whose "parent" is actually a file -> NotADirectoryError, caught.
    f = tmp_path / "afile"
    f.write_text("x", encoding="utf-8")
    assert runner.read_state(f / "child.json") is None


def test_write_then_read_state(tmp_path):
    p = tmp_path / "sub" / "s.json"
    runner.write_state(p, {"a": 1, "pid": 7})
    assert runner.read_state(p) == {"a": 1, "pid": 7}


def test_clear_state_idempotent(tmp_path):
    p = tmp_path / "s.json"
    runner.write_state(p, {"x": 1})
    runner.clear_state(p)
    assert not p.exists()
    runner.clear_state(p)  # second time: no error


def test_record_tick_updates_and_preserves(tmp_path):
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 99, "interval_seconds": 600})
    runner.record_tick(
        p, tick_count=3, at="2026-06-25T00:00:00Z",
        stop_reason="end_turn", cost_usd=0.0,
    )
    st = runner.read_state(p)
    assert st["pid"] == 99  # preserved
    assert st["tick_count"] == 3
    assert st["last_tick_at"] == "2026-06-25T00:00:00Z"
    assert st["last_stop_reason"] == "end_turn"
    assert st["last_error"] is None


def test_record_tick_missing_file_noop(tmp_path):
    p = tmp_path / "gone.json"
    runner.record_tick(p, tick_count=1, at="t")  # no file -> silent
    assert not p.exists()


def test_record_tick_in_flight_optional(tmp_path):
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 1})
    # Omitting in_flight leaves the field unwritten...
    runner.record_tick(p, tick_count=1, at="t")
    assert "in_flight" not in runner.read_state(p)
    # ...passing it refreshes the live gauge.
    runner.record_tick(p, tick_count=2, at="t2", in_flight=4)
    assert runner.read_state(p)["in_flight"] == 4


def test_record_fire_updates_and_preserves(tmp_path):
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 7, "fire_count": 0})
    runner.record_fire(
        p, fire_count=3, at="2026-06-25T00:00:00Z", in_flight=2
    )
    st = runner.read_state(p)
    assert st["pid"] == 7  # preserved
    assert st["fire_count"] == 3
    assert st["last_fire_at"] == "2026-06-25T00:00:00Z"
    assert st["in_flight"] == 2


def test_record_fire_missing_file_noop(tmp_path):
    p = tmp_path / "gone.json"
    runner.record_fire(p, fire_count=1, at="t", in_flight=1)  # silent
    assert not p.exists()


# --------------------------------------------------------------------------
# process liveness
# --------------------------------------------------------------------------

def test_pid_alive_self():
    assert runner.pid_alive(os.getpid()) is True


def test_pid_alive_dead():
    # A pid that is essentially never live.
    assert runner.pid_alive(2**31 - 1) is False


def test_is_running_no_file(tmp_path):
    running, state = runner.is_running(tmp_path / "nope.json")
    assert running is False and state is None


def test_is_running_live(tmp_path):
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 123})
    running, state = runner.is_running(p, alive=lambda pid: True)
    assert running is True and state["pid"] == 123


def test_is_running_dead(tmp_path):
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 123})
    running, _ = runner.is_running(p, alive=lambda pid: False)
    assert running is False


def test_is_running_pid_not_int(tmp_path):
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": None})
    running, _ = runner.is_running(p, alive=lambda pid: True)
    assert running is False


# --------------------------------------------------------------------------
# run_one_drive
# --------------------------------------------------------------------------

class _FakeBackend:
    def __init__(self):
        self.calls = []

    async def run(self, agent_cfg, prompt):
        self.calls.append((agent_cfg, prompt))
        return _FakeResult()


@pytest.mark.asyncio
async def test_run_one_drive_injected_backend():
    be = _FakeBackend()
    cfg = _cfg(max_budget_usd=2.5, model="opus", prompt="P")
    res = await runner.run_one_drive(cfg, backend=be)
    assert isinstance(res, _FakeResult)
    agent_cfg, prompt = be.calls[0]
    assert prompt == "P"
    assert agent_cfg.model == "opus"
    assert agent_cfg.extra["permission_mode"] == "bypassPermissions"
    assert agent_cfg.extra["max_budget_usd"] == 2.5


@pytest.mark.asyncio
async def test_run_one_drive_no_budget():
    be = _FakeBackend()
    cfg = _cfg(max_budget_usd=None)
    await runner.run_one_drive(cfg, backend=be)
    agent_cfg, _ = be.calls[0]
    assert "max_budget_usd" not in agent_cfg.extra


@pytest.mark.asyncio
async def test_run_one_drive_resolves_backend(monkeypatch):
    be = _FakeBackend()
    seen = {}

    def fake_get_backend(name):
        seen["name"] = name
        return be

    monkeypatch.setattr(
        "tigerharness.agent_sdk.get_backend", fake_get_backend
    )
    cfg = _cfg(backend="claude_p")
    await runner.run_one_drive(cfg)  # backend=None -> resolves by name
    assert seen["name"] == "claude_p"
    assert be.calls


# --------------------------------------------------------------------------
# run_loop
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_loop_max_ticks(tmp_path):
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 1})
    drives = []
    sleeps = []

    async def fake_drive(cfg, *, backend=None):
        drives.append(1)
        return _FakeResult(stop_reason="end_turn", cost_usd=0.1)

    async def fake_sleep(secs):
        sleeps.append(secs)

    n = await runner.run_loop(
        _cfg(), p, max_ticks=2, run_drive=fake_drive, sleep=fake_sleep,
        now=lambda: "T",
    )
    assert n == 2
    assert len(drives) == 2
    assert sleeps == [600.0]  # one sleep between the two drives, none after
    assert runner.read_state(p)["tick_count"] == 2


@pytest.mark.asyncio
async def test_run_loop_should_stop(tmp_path):
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 1})
    ran = {"n": 0}

    async def fake_drive(cfg, *, backend=None):
        ran["n"] += 1
        return _FakeResult()

    async def fake_sleep(secs):
        pass

    # Cooperative stop: the post-fire check sees fire_count advanced by
    # record_fire and breaks. (In the fire-and-forget model the drive runs
    # at the final drain, so a stop condition keyed on drive side-effects
    # would never trip -- it must key on loop progress instead.)
    def should_stop():
        st = runner.read_state(p)
        return bool(st and st.get("fire_count", 0) >= 1)

    n = await runner.run_loop(
        _cfg(), p, run_drive=fake_drive, sleep=fake_sleep,
        should_stop=should_stop, now=lambda: "T",
    )
    assert n == 1
    # The one fired drive is drained on exit, so it did run.
    assert ran["n"] == 1
    assert runner.read_state(p)["tick_count"] == 1


@pytest.mark.asyncio
async def test_run_loop_overlap_no_cap(tmp_path):
    """Two drives are in flight at once (no concurrency cap): the loop
    fires the second before the first finishes."""
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 1})
    gate = asyncio.Event()
    active = 0
    peak = 0

    async def slow_drive(cfg, *, backend=None):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active >= 2:
            gate.set()  # self-release once overlap is proven (no deadlock)
        await gate.wait()
        active -= 1
        return _FakeResult()

    async def fake_sleep(secs):
        await asyncio.sleep(0)  # yield so the just-fired drive can start

    n = await runner.run_loop(
        _cfg(), p, max_ticks=2, run_drive=slow_drive, sleep=fake_sleep,
        now=lambda: "T",
    )
    assert n == 2
    assert peak == 2  # both drives ran concurrently
    assert runner.read_state(p)["tick_count"] == 2


@pytest.mark.asyncio
async def test_run_loop_reaps_completed_drive_midloop(tmp_path):
    """A drive that finishes between fires is reaped mid-loop (not just at
    the final drain), so the in-flight gauge and completion count stay live."""
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 1})
    fires = []

    async def fake_drive(cfg, *, backend=None):
        fires.append(1)
        return _FakeResult(stop_reason="end_turn", cost_usd=0.2)

    async def fake_sleep(secs):
        await asyncio.sleep(0)  # let the in-flight drive complete

    n = await runner.run_loop(
        _cfg(), p, max_ticks=2, run_drive=fake_drive, sleep=fake_sleep,
        now=lambda: "T",
    )
    assert n == 2
    assert len(fires) == 2
    st = runner.read_state(p)
    assert st["tick_count"] == 2
    assert st["last_stop_reason"] == "end_turn"
    assert st["in_flight"] == 0  # all drained


@pytest.mark.asyncio
async def test_run_loop_reaps_errored_drive_midloop(tmp_path):
    """A drive that raises between fires is reaped mid-loop and recorded as
    an error, and the loop keeps firing (one bad drive is not fatal)."""
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 1})

    async def boom(cfg, *, backend=None):
        raise RuntimeError("kaboom")

    async def fake_sleep(secs):
        await asyncio.sleep(0)  # let the in-flight drive raise

    n = await runner.run_loop(
        _cfg(), p, max_ticks=2, run_drive=boom, sleep=fake_sleep,
        now=lambda: "T",
    )
    assert n == 2  # kept firing despite the first drive's error
    st = runner.read_state(p)
    assert st["tick_count"] == 2
    assert "kaboom" in st["last_error"]


@pytest.mark.asyncio
async def test_run_loop_stop_before_any_drive(tmp_path):
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 1})

    async def fake_drive(cfg, *, backend=None):  # pragma: no cover
        raise AssertionError("should not drive")

    n = await runner.run_loop(
        _cfg(), p, max_ticks=5, run_drive=fake_drive,
        should_stop=lambda: True, now=lambda: "T",
    )
    assert n == 0


@pytest.mark.asyncio
async def test_run_loop_zero_ticks(tmp_path):
    p = tmp_path / "s.json"

    async def fake_drive(cfg, *, backend=None):  # pragma: no cover
        raise AssertionError("should not drive")

    n = await runner.run_loop(_cfg(), p, max_ticks=0, run_drive=fake_drive)
    assert n == 0


@pytest.mark.asyncio
async def test_run_loop_drive_error_recorded(tmp_path):
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 1})

    async def boom(cfg, *, backend=None):
        raise RuntimeError("kaboom")

    async def fake_sleep(secs):
        pass

    n = await runner.run_loop(
        _cfg(), p, max_ticks=1, run_drive=boom, sleep=fake_sleep,
        now=lambda: "T",
    )
    assert n == 1
    st = runner.read_state(p)
    assert "kaboom" in st["last_error"]
    assert st["last_stop_reason"] is None


# --------------------------------------------------------------------------
# clamp_interval / with_prompt
# --------------------------------------------------------------------------

def test_clamp_interval_ok():
    assert runner.clamp_interval(60.0) == 60.0
    assert runner.clamp_interval(600.0) == 600.0


def test_clamp_interval_too_low():
    with pytest.raises(ValueError, match="must be >= 60"):
        runner.clamp_interval(10.0)


def test_with_prompt():
    cfg = _cfg(prompt="old")
    new = runner.with_prompt(cfg, "new")
    assert new.prompt == "new"
    assert cfg.prompt == "old"  # original untouched


# --------------------------------------------------------------------------
# CLI: parser / context helpers
# --------------------------------------------------------------------------

def _args(argv):
    return cli.build_parser().parse_args(argv)


def test_resolve_journal_root_override(tmp_path):
    args = _args(["status", "--journal-dir", str(tmp_path)])
    assert cli._resolve_journal_root(args) == tmp_path


def test_resolve_journal_root_default(monkeypatch, tmp_path):
    monkeypatch.setenv("TIGERHARNESS_JOURNAL_DIR", str(tmp_path / "jx"))
    args = _args(["status"])
    assert cli._resolve_journal_root(args) == tmp_path / "jx"


def test_resolve_journal_root_relative_is_anchored(monkeypatch, tmp_path):
    """A relative --journal-dir is anchored to the command's cwd, so the
    detached daemon (which runs in a different cwd) still finds the journal."""
    monkeypatch.chdir(tmp_path)
    args = _args(["status", "--journal-dir", "rel/journal"])
    resolved = cli._resolve_journal_root(args)
    assert resolved.is_absolute()
    assert resolved == tmp_path / "rel" / "journal"


def test_team_root_for_team(tmp_path):
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "personas.yaml").write_text("x", encoding="utf-8")
    jr = tmp_path / "journal"
    assert cli._team_root_for(jr) == tmp_path


def test_team_root_for_personal(tmp_path):
    assert cli._team_root_for(tmp_path / "journal") is None


# --------------------------------------------------------------------------
# CLI: cmd_start
# --------------------------------------------------------------------------

def _make_team(tmp_path, default_persona="Anzai"):
    (tmp_path / "configs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "configs" / "personas.yaml").write_text(
        f"default_persona: {default_persona}\npersonas:\n  - name: {default_persona}\n",
        encoding="utf-8",
    )


def test_cmd_start_resolves_the_spawn_default_at_call_time(tmp_path):
    """The omitted-``spawn`` path must reach the *module-level* function, so
    ``tests/conftest.py`` can bolt it shut for the whole suite.

    Written the obvious way -- ``spawn=spawn_loop_process`` in the
    signature -- the default binds at import and no amount of patching from
    outside can reach it. `ensure_running` omits the argument, so with the
    early binding every journal-scaffolding test that tripped the
    auto-start hook spawned a real detached daemon that outlived pytest.
    Thirty were found alive at once.

    Reaching the guard *is* the assertion: it proves the seam is live. The
    autouse fixture installed it, and it is a ``BaseException``, so
    ``pytest.raises`` must name it explicitly.
    """
    _make_team(tmp_path)
    args = _args(["start", "--journal-dir", str(tmp_path / "journal")])
    with pytest.raises(RealDaemonSpawnBlocked):
        cli.cmd_start(args, now=lambda: "T")


def test_autostart_hook_cannot_spawn_a_daemon_even_if_the_env_leaks(
    tmp_path, monkeypatch,
):
    """Belt and braces, verified: force the leak the env scrub prevents and
    confirm the second layer still holds.

    `ensure_running` swallows every ``Exception`` by design, so a guard
    derived from ``Exception`` would be caught here, logged at WARNING, and
    the daemon-spawn would look like a tidy no-op while the real damage --
    a detached process -- had already happened. ``RealDaemonSpawnBlocked``
    is a ``BaseException`` precisely so it escapes this handler.
    """
    _make_team(tmp_path)
    monkeypatch.setenv("TIGERHARNESS_AUTODRIVE_AUTOSTART", "1")
    with pytest.raises(RealDaemonSpawnBlocked):
        cli.ensure_running(tmp_path / "journal")


def test_cmd_start_happy(tmp_path, capsys):
    _make_team(tmp_path)
    jr = tmp_path / "journal"
    spawned = {}

    def fake_spawn(state_file, *, cwd, log_file, env):
        spawned["state_file"] = state_file
        spawned["cwd"] = cwd
        spawned["env"] = env
        return 424242

    args = _args(["start", "--journal-dir", str(jr), "--max-budget", "5"])
    rc = cli.cmd_start(args, spawn=fake_spawn, now=lambda: "2026-06-25T00:00:00Z")
    assert rc == 0
    st = runner.read_state(runner.state_path(jr))
    assert st["pid"] == 424242
    assert st["driver"] == "Anzai"  # resolved from default_persona
    assert st["interval_seconds"] == 600.0
    assert st["max_budget_usd"] == 5.0
    assert st["cwd"] == str(tmp_path)
    # fresh state initializes both the fire and completion gauges
    assert st["fire_count"] == 0
    assert st["last_fire_at"] is None
    assert st["in_flight"] == 0
    assert st["tick_count"] == 0
    # the spawned drive is pinned to exactly this journal
    assert spawned["env"]["TIGERHARNESS_JOURNAL_DIR"] == str(jr)
    assert spawned["cwd"] == str(tmp_path)
    out = capsys.readouterr().out
    assert "autodrive started (pid 424242)" in out
    assert "tigerharness autodrive stop" in out
    # max_budget set -> no subscription note
    assert "no --max-budget set" not in out


def test_cmd_start_no_budget_note(tmp_path, capsys):
    jr = tmp_path / "journal"
    args = _args(["start", "--journal-dir", str(jr)])
    rc = cli.cmd_start(
        args, spawn=lambda *a, **k: 5, now=lambda: "T"
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "no --max-budget set" in out


def test_cmd_start_explicit_driver_and_prompt(tmp_path):
    jr = tmp_path / "journal"
    args = _args(
        [
            "start", "--journal-dir", str(jr),
            "--driver", "Rukawa", "--prompt", "custom go",
        ]
    )
    cli.cmd_start(args, spawn=lambda *a, **k: 1, now=lambda: "T")
    st = runner.read_state(runner.state_path(jr))
    assert st["driver"] == "Rukawa"
    assert st["prompt"] == "custom go"


def test_cmd_start_personal_no_driver(tmp_path):
    jr = tmp_path / "journal"  # parent has no personas.yaml
    args = _args(["start", "--journal-dir", str(jr)])
    cli.cmd_start(args, spawn=lambda *a, **k: 1, now=lambda: "T")
    st = runner.read_state(runner.state_path(jr))
    assert st["driver"] is None


def test_cmd_start_already_running(tmp_path, capsys):
    jr = tmp_path / "journal"
    jr.mkdir(parents=True)
    runner.write_state(runner.state_path(jr), {"pid": os.getpid()})
    args = _args(["start", "--journal-dir", str(jr)])
    rc = cli.cmd_start(args, spawn=lambda *a, **k: 1, now=lambda: "T")
    assert rc == 1
    err = capsys.readouterr().err
    assert "already running" in err


def test_cmd_start_interval_too_low(tmp_path, capsys):
    jr = tmp_path / "journal"
    args = _args(["start", "--journal-dir", str(jr), "--interval", "5"])
    rc = cli.cmd_start(args, spawn=lambda *a, **k: 1, now=lambda: "T")
    assert rc == 2
    assert "must be >= 60" in capsys.readouterr().err


# --------------------------------------------------------------------------
# CLI: cmd_status
# --------------------------------------------------------------------------

def test_cmd_status_no_file(tmp_path, capsys):
    args = _args(["status", "--journal-dir", str(tmp_path / "j")])
    assert cli.cmd_status(args) == 0
    assert "stopped (no state file)" in capsys.readouterr().out


def test_cmd_status_running(tmp_path, capsys):
    jr = tmp_path / "journal"
    runner.write_state(
        runner.state_path(jr),
        {
            "pid": os.getpid(), "interval_seconds": 600, "backend": "claude_p",
            "driver": "Anzai", "max_budget_usd": 5.0,
            "started_at": "T0",
            "fire_count": 3, "last_fire_at": "T2", "in_flight": 1,
            "last_tick_at": "T1", "tick_count": 2,
            "last_stop_reason": "end_turn", "last_error": "boom",
        },
    )
    args = _args(["status", "--journal-dir", str(jr)])
    assert cli.cmd_status(args) == 0
    out = capsys.readouterr().out
    assert "running" in out
    assert "fire_count:   3 (drives launched)" in out
    assert "last_fire_at: T2" in out
    assert "in_flight:    1 (running now)" in out
    assert "done_count:   2 (drives completed)" in out
    assert "last_done_at: T1" in out
    assert "last_stop:    end_turn" in out
    assert "last_error:   boom" in out


def test_cmd_status_stale(tmp_path, capsys):
    jr = tmp_path / "journal"
    runner.write_state(
        runner.state_path(jr),
        {"pid": 2**31 - 1, "interval_seconds": 600, "tick_count": 0},
    )
    args = _args(["status", "--journal-dir", str(jr)])
    cli.cmd_status(args)
    out = capsys.readouterr().out
    assert "stale state file" in out
    assert "(none yet)" in out


# --------------------------------------------------------------------------
# CLI: cmd_status after an abnormal exit
#
# The incident: SIGKILL / OOM / reboot leaves the persisted counters at
# whatever the daemon last wrote, so `status` printed a drive "(running now)"
# under a header that already said the daemon was stopped. The fix labels the
# counter; it deliberately does NOT zero it, because `in_flight: 1` at the
# moment of death says how the daemon died. Neither existing fixture above
# constructs that state -- `test_cmd_status_running` has a live pid, and
# `test_cmd_status_stale` carries no `in_flight` key at all.
#
# Both paths assert WHOLE LINES out of `out.splitlines()`. A substring form
# proves nothing here: "running" is a substring of "not running", and
# "in_flight:    1" is a prefix of "in_flight:    10".
# --------------------------------------------------------------------------

_DEAD_PID = 2**31 - 1
_NOTE_LINE = "  note:         counters below are frozen at the daemon's"
_LIVE_LINE = "  in_flight:    1 (running now)"
_FROZEN_LINE = "  in_flight:    1 (last recorded, daemon not running)"


def _status_lines(tmp_path, capsys, state):
    runner.write_state(runner.state_path(tmp_path / "journal"), state)
    args = _args(["status", "--journal-dir", str(tmp_path / "journal")])
    assert cli.cmd_status(args) == 0
    return capsys.readouterr().out.splitlines()


def test_cmd_status_dead_pid_keeps_in_flight_and_labels_it_frozen(tmp_path, capsys):
    lines = _status_lines(
        tmp_path,
        capsys,
        {"pid": _DEAD_PID, "interval_seconds": 600, "in_flight": 1, "tick_count": 0},
    )
    assert _FROZEN_LINE in lines
    assert _LIVE_LINE not in lines
    assert _NOTE_LINE in lines
    assert "                last write; nothing is running now." in lines


def test_cmd_status_live_pid_still_calls_in_flight_running_now(tmp_path, capsys):
    lines = _status_lines(
        tmp_path,
        capsys,
        {"pid": os.getpid(), "interval_seconds": 600, "in_flight": 1, "tick_count": 0},
    )
    assert _LIVE_LINE in lines
    assert _FROZEN_LINE not in lines
    assert _NOTE_LINE not in lines


# --------------------------------------------------------------------------
# CLI: cmd_status when --journal-dir cannot move the state anchor
#
# Haruko's walk: standing in a team root, `status --journal-dir <other>`
# answered about the TEAM's daemon -- `running`, for a journal whose own state
# file said the pid was dead -- and said nothing about the substitution. Even a
# nonexistent path reported `running`. The anchor itself is correct and must
# stay (a daemon started in this team keeps its state at the team's journal
# while driving another root, so reading the flag's path would report
# `stopped` for a live daemon); what was wrong was the silence.
#
# Whole lines again, and the negative assertion is the load-bearing half: a
# test that only checks the note APPEARS would pass just as well if the note
# were printed unconditionally, which would be noise on every ordinary call.
# --------------------------------------------------------------------------

_ANCHOR_TAIL = "                (team-canonical: one autodrive per team, so"


def _team_root(tmp_path):
    (tmp_path / "team" / "configs").mkdir(parents=True)
    (tmp_path / "team" / "configs" / "personas.yaml").write_text("personas: []\n")
    return tmp_path / "team"


def test_cmd_status_from_team_root_names_the_state_file_it_actually_read(
    tmp_path, capsys, monkeypatch
):
    team = _team_root(tmp_path)
    runner.write_state(
        runner.state_path(team / "journal"),
        {
            "pid": os.getpid(), "interval_seconds": 600, "tick_count": 0,
            "journal_root": str(team / "journal"),
        },
    )
    monkeypatch.chdir(team)
    args = _args(["status", "--journal-dir", str(tmp_path / "elsewhere" / "journal")])
    assert cli.cmd_status(args) == 0
    lines = capsys.readouterr().out.splitlines()

    assert f"  read:         {runner.state_path(team / 'journal')}" in lines
    assert _ANCHOR_TAIL in lines
    # and the operator can now see which journal that daemon actually drives
    assert f"  journal:      {team / 'journal'}" in lines


def test_cmd_status_no_state_file_also_names_the_anchor(tmp_path, capsys, monkeypatch):
    team = _team_root(tmp_path)
    monkeypatch.chdir(team)
    args = _args(["status", "--journal-dir", str(tmp_path / "elsewhere" / "journal")])
    assert cli.cmd_status(args) == 0
    lines = capsys.readouterr().out.splitlines()

    assert "autodrive: stopped (no state file)" in lines
    assert f"  read:         {runner.state_path(team / 'journal')}" in lines


def test_cmd_status_stays_quiet_when_the_flag_matches_the_anchor(tmp_path, capsys):
    lines = _status_lines(
        tmp_path, capsys,
        {"pid": os.getpid(), "interval_seconds": 600, "tick_count": 0},
    )
    assert not [line for line in lines if line.startswith("  read:")]
    assert _ANCHOR_TAIL not in lines
    assert "  journal:      (unknown)" in lines


# --------------------------------------------------------------------------
# CLI: cmd_stop
# --------------------------------------------------------------------------

def test_cmd_stop_no_file(tmp_path, capsys):
    args = _args(["stop", "--journal-dir", str(tmp_path / "j")])
    assert cli.cmd_stop(args) == 0
    assert "not running (no state file)" in capsys.readouterr().out


def test_cmd_stop_running(tmp_path, capsys):
    jr = tmp_path / "journal"
    runner.write_state(runner.state_path(jr), {"pid": os.getpid()})
    killed = {}
    args = _args(["stop", "--journal-dir", str(jr)])
    rc = cli.cmd_stop(args, kill=lambda pid: killed.setdefault("pid", pid))
    assert rc == 0
    assert killed["pid"] == os.getpid()
    assert not runner.state_path(jr).exists()  # cleared
    assert "autodrive stopped" in capsys.readouterr().out


def test_cmd_stop_stale(tmp_path, capsys):
    jr = tmp_path / "journal"
    runner.write_state(runner.state_path(jr), {"pid": 2**31 - 1})
    called = {}
    args = _args(["stop", "--journal-dir", str(jr)])
    cli.cmd_stop(args, kill=lambda pid: called.setdefault("pid", pid))
    assert "pid" not in called  # no kill on a dead pid
    assert not runner.state_path(jr).exists()
    assert "cleared stale state file" in capsys.readouterr().out


# --------------------------------------------------------------------------
# CLI: cmd_loop
# --------------------------------------------------------------------------

def test_cmd_loop_configures_logging_so_the_daemon_log_is_not_empty(
    tmp_path, monkeypatch,
):
    """`start` redirects the daemon's stdout+stderr into `.autodrive.log`,
    but nothing ever configured a handler, so every record below WARNING
    went to `logging.lastResort` and the file stayed empty -- a six-fire
    rescue stampede left zero forensics as a result. The daemon body must
    turn its own INFO logging on.

    The ``delenv`` matters: ``configure_cli_logging`` lets
    ``TIGERHARNESS_LOG_LEVEL`` override the default, and the autouse
    scrub in ``conftest`` only covers the Slack family. Without this, a
    dev shell exporting ``DEBUG`` false-fails the level assertion.
    """
    monkeypatch.delenv("TIGERHARNESS_LOG_LEVEL", raising=False)

    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        # Stand in for the fresh `_loop` subprocess: no handlers yet, so
        # basicConfig actually applies rather than no-opping.
        root.handlers = []
        root.setLevel(logging.WARNING)
        args = _args(["_loop", "--state-file", str(tmp_path / "gone.json")])
        assert cli.cmd_loop(args) == 1
        assert root.handlers, "daemon left the root logger without a handler"
        assert root.level == logging.INFO
    finally:
        root.handlers, root.level = saved_handlers, saved_level


def test_cmd_loop_no_state(tmp_path, capsys):
    args = _args(["_loop", "--state-file", str(tmp_path / "gone.json")])
    assert cli.cmd_loop(args) == 1
    assert "no state file" in capsys.readouterr().err


def test_cmd_loop_runs_and_clears(tmp_path):
    sfile = tmp_path / "s.json"
    runner.write_state(
        sfile,
        {"interval_seconds": 600, "prompt": "go", "stop_requested": False},
    )
    seen = {}

    async def fake_runner(cfg, state_file, *, should_stop, notifier,
                          confirm_exit=None):
        seen["cfg"] = cfg
        seen["stop"] = should_stop()  # exercises the closure
        seen["notifier"] = notifier
        return 0

    args = _args(["_loop", "--state-file", str(sfile)])
    rc = cli.cmd_loop(args, runner=fake_runner)
    assert rc == 0
    assert seen["cfg"].prompt == "go"
    assert seen["stop"] is False
    # A built notifier is passed through (no Slack creds in tests -> Null).
    assert seen["notifier"] is not None
    assert not sfile.exists()  # cleared on clean exit


def test_cmd_loop_should_stop_when_flagged(tmp_path):
    sfile = tmp_path / "s.json"
    runner.write_state(
        sfile,
        {"interval_seconds": 600, "prompt": "go", "stop_requested": True},
    )
    captured = {}

    async def fake_runner(cfg, state_file, *, should_stop, notifier,
                          confirm_exit=None):
        captured["stop"] = should_stop()
        return 0

    args = _args(["_loop", "--state-file", str(sfile)])
    cli.cmd_loop(args, runner=fake_runner)
    assert captured["stop"] is True


# --------------------------------------------------------------------------
# CLI: main dispatch
# --------------------------------------------------------------------------

def test_main_status(tmp_path):
    rc = cli.main(["status", "--journal-dir", str(tmp_path / "j")])
    assert rc == 0


def test_main_requires_subcommand():
    with pytest.raises(SystemExit):
        cli.main([])


def test_loop_requires_state_file():
    with pytest.raises(SystemExit):
        cli.main(["_loop"])


def test_main_module_execution(monkeypatch):
    """Cover ``python -m tigerharness.autodrive`` (the daemon re-exec
    path) via runpy with a patched ``main``."""
    import runpy

    monkeypatch.setattr(cli, "main", lambda argv=None: 0)
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("tigerharness.autodrive", run_name="__main__")
    assert exc.value.code == 0


# --------------------------------------------------------------------------
# notification config (de)serialization
# --------------------------------------------------------------------------

def test_config_notify_roundtrip():
    cfg = _cfg(notify="none", notify_channel="C0OPS")
    back = runner.config_from_state(runner.config_to_dict(cfg))
    assert back.notify == "none"
    assert back.notify_channel == "C0OPS"


def test_config_from_state_notify_defaults():
    # State written before notifications existed has neither key; the config
    # must deserialize cleanly with the slack default + DM (None channel).
    cfg = runner.config_from_state({"interval_seconds": 120, "prompt": "go"})
    assert cfg.notify == runner.DEFAULT_NOTIFY
    assert cfg.notify_channel is None


# --------------------------------------------------------------------------
# notification text builders
# --------------------------------------------------------------------------

def test_heartbeat_text_shape():
    s = runner.heartbeat_text(42, "2026-06-26T14:00:00Z", 3)
    assert "fire #42" in s
    assert "2026-06-26T14:00:00Z" in s
    assert "in-flight 3" in s


def test_truncate_summary_under_limit():
    assert runner._truncate_summary("  short  ") == "short"


def test_truncate_summary_over_limit():
    out = runner._truncate_summary("x" * 700, limit=600)
    assert out.endswith(" [...]")
    assert len(out) <= 600 + len(" [...]")


def test_completion_text_with_summary_and_cost():
    out = runner.completion_text(
        7, _FakeResult(stop_reason="end_turn", cost_usd=0.12,
                       final_output="Did the thing."),
    )
    assert "fire #7 done: stop_reason=end_turn" in out
    assert "cost=$0.12" in out
    assert "Did the thing." in out


def test_completion_text_no_cost_no_summary():
    # cost None -> no cost segment; no final_output -> head only (one line).
    out = runner.completion_text(
        7, _FakeResult(stop_reason="error_max_turns", cost_usd=None,
                       final_output=None),
    )
    assert out == "fire #7 done: stop_reason=error_max_turns"


def test_error_text_shape():
    out = runner.error_text(3, RuntimeError("not authenticated"))
    assert "fire #3 FAILED: RuntimeError: not authenticated" == out


# --------------------------------------------------------------------------
# run_loop notification flow (recording notifier, no real Slack)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_loop_posts_heartbeat_and_threads_completion(tmp_path):
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 1})
    notifier = _RecordingNotifier()

    async def fake_drive(cfg, *, backend=None):
        return _FakeResult(stop_reason="end_turn", cost_usd=0.1,
                           final_output="ok")

    async def fake_sleep(secs):
        pass

    n = await runner.run_loop(
        _cfg(), p, max_ticks=2, run_drive=fake_drive, sleep=fake_sleep,
        now=lambda: "T", notifier=notifier,
    )
    assert n == 2
    # One heartbeat per fire...
    assert len(notifier.heartbeats) == 2
    assert "fire #1" in notifier.heartbeats[0]
    assert "fire #2" in notifier.heartbeats[1]
    # ...and one threaded completion per fire, under the matching heartbeat.
    assert len(notifier.updates) == 2
    by_thread = {thread: text for thread, text in notifier.updates}
    assert "fire #1 done" in by_thread["ts-1"]
    assert "fire #2 done" in by_thread["ts-2"]


@pytest.mark.asyncio
async def test_run_loop_threads_error_under_heartbeat(tmp_path):
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 1})
    notifier = _RecordingNotifier()

    async def boom(cfg, *, backend=None):
        raise RuntimeError("kaboom")

    async def fake_sleep(secs):
        pass

    await runner.run_loop(
        _cfg(), p, max_ticks=1, run_drive=boom, sleep=fake_sleep,
        now=lambda: "T", notifier=notifier,
    )
    assert len(notifier.heartbeats) == 1
    thread, text = notifier.updates[0]
    assert thread == "ts-1"
    assert "FAILED: RuntimeError: kaboom" in text


@pytest.mark.asyncio
async def test_run_loop_prunes_completed_notif_tasks(tmp_path):
    """Over many fires the loop must not accumulate one notification task per
    completed drive forever -- completed update tasks are pruned each
    iteration. We give the to_thread update tasks real time to finish so a
    later iteration's prune sees them done and drops them."""
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 1})
    notifier = _RecordingNotifier()

    async def fake_drive(cfg, *, backend=None):
        return _FakeResult(stop_reason="end_turn", cost_usd=0.0,
                           final_output="ok")

    async def fake_sleep(secs):
        await asyncio.sleep(0.02)  # let prior update tasks complete

    n = await runner.run_loop(
        _cfg(), p, max_ticks=4, run_drive=fake_drive, sleep=fake_sleep,
        now=lambda: "T", notifier=notifier,
    )
    assert n == 4
    # Every fire heartbeated and every completion threaded, despite pruning.
    assert len(notifier.heartbeats) == 4
    assert len(notifier.updates) == 4


@pytest.mark.asyncio
async def test_run_loop_default_notifier_is_null(tmp_path):
    # No notifier passed -> NullNotifier; the loop still runs and records.
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 1})

    async def fake_drive(cfg, *, backend=None):
        return _FakeResult()

    async def fake_sleep(secs):
        pass

    n = await runner.run_loop(
        _cfg(), p, max_ticks=1, run_drive=fake_drive, sleep=fake_sleep,
        now=lambda: "T",
    )
    assert n == 1
    assert runner.read_state(p)["tick_count"] == 1


# --------------------------------------------------------------------------
# notifier seam: Null / Slack / build_notifier
# --------------------------------------------------------------------------

class _FakeSlackBackend:
    def __init__(self):
        self.posts = []
        self.dms = []

    def post_text(self, text, *, channel=None):
        self.posts.append((text, channel))
        return "TS123"

    def dm_text(self, text, *, channel=None, thread_ts=None):
        self.dms.append((text, channel, thread_ts))
        return True


def test_null_notifier_is_noop():
    n = NullNotifier()
    assert n.heartbeat("x") is None
    assert n.update("ts", "y") is None


def test_slack_channel_notifier_heartbeat_and_update():
    be = _FakeSlackBackend()
    n = SlackChannelNotifier(be, "C0OPS")
    ts = n.heartbeat("beat")
    assert ts == "TS123"
    assert be.posts == [("beat", "C0OPS")]
    n.update("TS123", "done")
    assert be.dms == [("done", "C0OPS", "TS123")]


def test_build_notifier_muted_is_null():
    assert isinstance(build_notifier("none", None), NullNotifier)


def test_build_notifier_slack_no_creds_is_null(monkeypatch):
    # notify=slack but try_load yields no backend -> degrade to NullNotifier.
    monkeypatch.setattr(
        "tigerharness.slack_bridge.notify.SlackNotifier.try_load",
        classmethod(lambda cls: None),
    )
    assert isinstance(build_notifier("slack", "C0OPS"), NullNotifier)


def test_build_notifier_slack_with_creds(monkeypatch):
    be = _FakeSlackBackend()
    monkeypatch.setattr(
        "tigerharness.slack_bridge.notify.SlackNotifier.try_load",
        classmethod(lambda cls: be),
    )
    n = build_notifier("slack", "C0OPS")
    assert isinstance(n, SlackChannelNotifier)
    assert n._channel == "C0OPS"
    assert n._backend is be


# --------------------------------------------------------------------------
# CLI: team-scoped state anchor (one autodrive per team)
# --------------------------------------------------------------------------

def test_state_root_team_scoped_ignores_journal_dir(tmp_path, monkeypatch):
    _make_team(tmp_path)
    monkeypatch.chdir(tmp_path)
    # Even with a custom --journal-dir, the lock anchors to <team>/journal.
    args = _args(["status", "--journal-dir", str(tmp_path / "elsewhere")])
    assert cli._state_root(args) == tmp_path / "journal"


def test_state_root_personal_uses_resolved_journal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # not a team dir (no configs/personas.yaml)
    args = _args(["status", "--journal-dir", str(tmp_path / "j")])
    assert cli._state_root(args) == tmp_path / "j"


def test_cmd_start_lock_is_team_scoped(tmp_path, monkeypatch):
    """A custom --journal-dir redirects the *driven* journal, but the lock
    stays at the team's canonical journal so the one-per-team guard holds."""
    team = tmp_path / "Team"
    _make_team(team)
    monkeypatch.chdir(team)
    driven = tmp_path / "driven"
    captured = {}

    def fake_spawn(state_file, *, cwd, log_file, env):
        captured["env"] = env
        return 7

    args = _args(["start", "--journal-dir", str(driven)])
    rc = cli.cmd_start(args, spawn=fake_spawn, now=lambda: "T")
    assert rc == 0
    # Lock at the team's canonical journal, NOT the driven dir.
    assert runner.state_path(team / "journal").exists()
    assert not runner.state_path(driven).exists()
    # The spawned drive is still pinned to the driven journal.
    assert captured["env"]["TIGERHARNESS_JOURNAL_DIR"] == str(driven)


# --------------------------------------------------------------------------
# CLI: notify config (cmd_start / cmd_status output)
# --------------------------------------------------------------------------

def test_cmd_start_notify_defaults(tmp_path, capsys, monkeypatch):
    _make_team(tmp_path)
    jr = tmp_path / "journal"
    # Hermetic: this asserts the *default* (no flag, no env) resolves to the
    # operator DM, so clear both channel keys the host may have exported --
    # SLACK_NOTIFY_CHANNEL is set on any machine running a slack bridge.
    monkeypatch.delenv(cli.NOTIFY_CHANNEL_ENV, raising=False)
    monkeypatch.delenv(cli.SLACK_NOTIFY_CHANNEL_ENV, raising=False)
    args = _args(["start", "--journal-dir", str(jr)])
    cli.cmd_start(args, spawn=lambda *a, **k: 1, now=lambda: "T")
    st = runner.read_state(runner.state_path(jr))
    assert st["notify"] == "slack"
    assert st["notify_channel"] is None
    assert "notify: slack -> operator DM" in capsys.readouterr().out


def test_cmd_start_notify_channel_flag(tmp_path, capsys):
    _make_team(tmp_path)
    jr = tmp_path / "journal"
    args = _args(
        ["start", "--journal-dir", str(jr), "--notify-channel", "C0OPS"]
    )
    cli.cmd_start(args, spawn=lambda *a, **k: 1, now=lambda: "T")
    st = runner.read_state(runner.state_path(jr))
    assert st["notify_channel"] == "C0OPS"
    assert "notify: slack -> C0OPS" in capsys.readouterr().out


def test_cmd_start_notify_channel_from_env(tmp_path, monkeypatch):
    _make_team(tmp_path)
    jr = tmp_path / "journal"
    monkeypatch.setenv(cli.NOTIFY_CHANNEL_ENV, "C0ENV")
    args = _args(["start", "--journal-dir", str(jr)])
    cli.cmd_start(args, spawn=lambda *a, **k: 1, now=lambda: "T")
    st = runner.read_state(runner.state_path(jr))
    assert st["notify_channel"] == "C0ENV"


def test_cmd_start_notify_flag_beats_env(tmp_path, monkeypatch):
    _make_team(tmp_path)
    jr = tmp_path / "journal"
    monkeypatch.setenv(cli.NOTIFY_CHANNEL_ENV, "C0ENV")
    args = _args(
        ["start", "--journal-dir", str(jr), "--notify-channel", "C0FLAG"]
    )
    cli.cmd_start(args, spawn=lambda *a, **k: 1, now=lambda: "T")
    st = runner.read_state(runner.state_path(jr))
    assert st["notify_channel"] == "C0FLAG"


def test_cmd_start_inherits_slack_notify_channel(tmp_path, capsys, monkeypatch):
    """End-to-end at the CLI layer: a team that only ever set the well-known
    team-wide key gets its daemon events in that channel, not a DM."""
    _make_team(tmp_path)
    jr = tmp_path / "journal"
    monkeypatch.delenv(cli.NOTIFY_CHANNEL_ENV, raising=False)
    monkeypatch.setenv(cli.SLACK_NOTIFY_CHANNEL_ENV, "C0OPS")
    args = _args(["start", "--journal-dir", str(jr)])
    cli.cmd_start(args, spawn=lambda *a, **k: 1, now=lambda: "T")
    st = runner.read_state(runner.state_path(jr))
    assert st["notify_channel"] == "C0OPS"
    assert "notify: slack -> C0OPS" in capsys.readouterr().out


def test_cmd_start_dm_sentinel_declines_the_inherited_channel(
    tmp_path, capsys, monkeypatch
):
    _make_team(tmp_path)
    jr = tmp_path / "journal"
    monkeypatch.setenv(cli.NOTIFY_CHANNEL_ENV, cli.DM_SENTINEL)
    monkeypatch.setenv(cli.SLACK_NOTIFY_CHANNEL_ENV, "C0OPS")
    args = _args(["start", "--journal-dir", str(jr)])
    cli.cmd_start(args, spawn=lambda *a, **k: 1, now=lambda: "T")
    st = runner.read_state(runner.state_path(jr))
    assert st["notify_channel"] is None
    assert "notify: slack -> operator DM" in capsys.readouterr().out


def test_cmd_start_notify_none(tmp_path, capsys):
    _make_team(tmp_path)
    jr = tmp_path / "journal"
    args = _args(["start", "--journal-dir", str(jr), "--notify", "none"])
    cli.cmd_start(args, spawn=lambda *a, **k: 1, now=lambda: "T")
    st = runner.read_state(runner.state_path(jr))
    assert st["notify"] == "none"
    assert "notify: none (muted" in capsys.readouterr().out


def test_cmd_status_shows_notify(tmp_path, capsys):
    jr = tmp_path / "journal"
    runner.write_state(
        runner.state_path(jr),
        {
            "pid": os.getpid(), "interval_seconds": 600, "backend": "claude_p",
            "notify": "slack", "notify_channel": "C0OPS",
        },
    )
    args = _args(["status", "--journal-dir", str(jr)])
    cli.cmd_status(args)
    assert "notify:       slack -> C0OPS" in capsys.readouterr().out


def test_cmd_status_shows_notify_muted(tmp_path, capsys):
    jr = tmp_path / "journal"
    runner.write_state(
        runner.state_path(jr),
        {"pid": os.getpid(), "interval_seconds": 600, "notify": "none"},
    )
    args = _args(["status", "--journal-dir", str(jr)])
    cli.cmd_status(args)
    assert "notify:       none (muted)" in capsys.readouterr().out


def test_cmd_loop_builds_null_notifier_without_creds(tmp_path, monkeypatch):
    """With no Slack creds, cmd_loop builds a NullNotifier and passes it
    through -- the daemon runs notification-free rather than crashing."""
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    sfile = tmp_path / "s.json"
    runner.write_state(
        sfile,
        {"interval_seconds": 600, "prompt": "go", "notify": "slack",
         "notify_channel": "C0OPS"},
    )
    seen = {}

    async def fake_runner(cfg, state_file, *, should_stop, notifier,
                          confirm_exit=None):
        seen["notifier"] = notifier
        return 0

    args = _args(["_loop", "--state-file", str(sfile)])
    cli.cmd_loop(args, runner=fake_runner)
    assert isinstance(seen["notifier"], NullNotifier)


# --------------------------------------------------------------------------
# Vendor / model resolution for a drive (ADR 0011)
# --------------------------------------------------------------------------

def _make_vendor_team(tmp_path, body):
    (tmp_path / "configs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "configs" / "personas.yaml").write_text(body, encoding="utf-8")
    return tmp_path


_VENDOR_ROSTER = """\
default_persona: Anzai
default_vendor: claude
default_model: claude-opus-5
personas:
  - name: Anzai
  - name: Rukawa
    vendor: chatgpt
    model: gpt-6-astra
"""


def test_resolve_drive_backend_follows_the_driver_persona(tmp_path):
    team = _make_vendor_team(tmp_path, _VENDOR_ROSTER)
    assert cli.resolve_drive_backend(None, None, team, "Anzai") == (
        "claude_p", "claude-opus-5",
    )
    assert cli.resolve_drive_backend(None, None, team, "Rukawa") == (
        "codex_exec", "gpt-6-astra",
    )


def test_resolve_drive_backend_flags_win(tmp_path):
    team = _make_vendor_team(tmp_path, _VENDOR_ROSTER)
    # A vendor name is accepted as --backend; the persona's model applies
    # only when the drive lands on the persona's own backend.
    assert cli.resolve_drive_backend("chatgpt", None, team, "Rukawa") == (
        "codex_exec", "gpt-6-astra",
    )
    assert cli.resolve_drive_backend("claude_p", None, team, "Rukawa") == (
        "claude_p", None,
    )
    assert cli.resolve_drive_backend("codex_exec", "gpt-x", team, "Anzai") == (
        "codex_exec", "gpt-x",
    )
    # An unknown backend name passes through untouched (custom registration).
    assert cli.resolve_drive_backend("mine", None, team, "Anzai") == ("mine", None)


def test_resolve_drive_backend_without_a_team_is_the_builtin(tmp_path):
    assert cli.resolve_drive_backend(None, None, None, None) == ("claude_p", None)
    assert cli.resolve_drive_backend(None, "m", None, "Anzai") == ("claude_p", "m")


def test_cmd_start_refuses_a_malformed_vendor(tmp_path, capsys):
    """A typo in personas.yaml must not start a daemon on the other vendor."""
    _make_vendor_team(tmp_path, "default_persona: Anzai\ndefault_vendor: gemini\npersonas:\n  - name: Anzai\n")
    args = _args(["start", "--journal-dir", str(tmp_path / "journal")])
    assert cli.cmd_start(args, now=lambda: "T") == 2
    assert "unknown model vendor 'gemini'" in capsys.readouterr().err


def test_start_parser_defaults_leave_backend_and_model_unset():
    args = _args(["start"])
    assert args.backend is None and args.model is None


# --------------------------------------------------------------------------
# Drive lanes (ADR 0012): the lane probe, per-lane fan-out, early wake
# --------------------------------------------------------------------------

_LANE_ROSTER = """\
default_persona: Ayako
default_vendor: claude
personas:
  - name: Ayako
  - name: Akagi
  - name: Rukawa
    vendor: chatgpt
    model: gpt-6-astra
"""


def _lane_team(tmp_path, roster=_LANE_ROSTER):
    from tigerharness.journal.paths import JournalPaths
    team = tmp_path / "team"
    (team / "configs").mkdir(parents=True)
    (team / "configs" / "personas.yaml").write_text(roster)
    paths = JournalPaths(root=team / "journal")
    paths.ensure()
    return team, paths


def _lane_task(paths, task_id, persona):
    from tigerharness.journal.models import Status
    st = Status.new(id=task_id, title=f"T {task_id}", persona=persona)
    (paths.active / task_id).mkdir(parents=True, exist_ok=True)
    paths.status_json(task_id).write_text(st.to_json())


def _lane_workflow_no_captain(paths, task_id):
    from tigerharness.journal.models import Status
    st = Status.new_workflow(id=task_id, title="W", playbook_name="default")
    (paths.active / task_id).mkdir(parents=True, exist_ok=True)
    paths.status_json(task_id).write_text(st.to_json())


def test_probe_lanes_groups_work_by_lane(tmp_path):
    from tigerharness.journal.deferred import defer_entry
    team, paths = _lane_team(tmp_path)
    _lane_task(paths, "t-ayako", "Ayako")
    _lane_task(paths, "t-rukawa", "Rukawa")
    _lane_task(paths, "t-akagi", "Akagi")
    defer_entry(paths, title="d", team="T", payload_text="p", kind="task", persona="Rukawa")
    cfg = _cfg(driver="Ayako", journal_root=str(paths.root))
    lanes = runner.probe_lanes(cfg)
    assert [(lw.key, lw.backend, lw.model, lw.driver, lw.items) for lw in lanes] == [
        ("claude", "claude_p", None, "Ayako", 2),
        ("chatgpt/gpt-6-astra", "codex_exec", "gpt-6-astra", "Rukawa", 2),
    ]


def test_probe_lanes_driver_selection(tmp_path):
    team, paths = _lane_team(tmp_path)
    _lane_task(paths, "t-akagi", "Akagi")
    _lane_workflow_no_captain(paths, "wf-nobody")
    # Configured driver on another lane: the claude lane's driver is the
    # first owner on it (Akagi), never the chatgpt driver.
    lanes = runner.probe_lanes(_cfg(driver="Rukawa", journal_root=str(paths.root)))
    assert [(lw.key, lw.driver) for lw in lanes] == [("claude", "Akagi")]
    # No configured driver and only ownerless work: the team default persona
    # drives its own lane.
    for tid in ("t-akagi",):
        import shutil
        shutil.rmtree(paths.active / tid)
    lanes = runner.probe_lanes(_cfg(driver=None, journal_root=str(paths.root)))
    assert [(lw.key, lw.driver) for lw in lanes] == [("claude", "Ayako")]


def test_probe_lanes_skips_a_lane_nobody_can_drive(tmp_path, caplog):
    team, paths = _lane_team(
        tmp_path,
        "default_vendor: claude\npersonas:\n  - name: Rukawa\n    vendor: chatgpt\n",
    )
    _lane_workflow_no_captain(paths, "wf-nobody")  # owner None -> claude lane
    with caplog.at_level(logging.WARNING, logger="tigerharness.autodrive.runner"):
        lanes = runner.probe_lanes(_cfg(driver="Rukawa", journal_root=str(paths.root)))
    assert lanes == []
    assert "no persona to drive it as" in caplog.text


def test_probe_lanes_fail_soft(tmp_path, caplog):
    assert runner.probe_lanes(_cfg(journal_root=None)) == []
    # A personal journal (no team root) has no lanes.
    from tigerharness.journal.paths import JournalPaths
    personal = JournalPaths(root=tmp_path / "solo" / "journal")
    personal.ensure()
    assert runner.probe_lanes(_cfg(journal_root=str(personal.root))) == []
    # A malformed vendor: warn and fall back to the single default drive.
    team, paths = _lane_team(tmp_path, "personas:\n  - name: Ayako\n    vendor: gemini\n")
    _lane_task(paths, "t1", "Ayako")
    with caplog.at_level(logging.WARNING, logger="tigerharness.autodrive.runner"):
        assert runner.probe_lanes(_cfg(driver="Ayako", journal_root=str(paths.root))) == []
    assert "lane probe failed" in caplog.text


def _lw(key, driver, backend="claude_p", model=None):
    return runner.LaneWork(key=key, backend=backend, model=model, driver=driver, items=1)


@pytest.mark.asyncio
async def test_run_loop_fires_one_drive_per_lane(tmp_path):
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 1})
    seen = []

    async def fake_drive(cfg, *, backend=None):
        seen.append((cfg.driver, cfg.backend, cfg.model, cfg.prompt))
        return _FakeResult()

    async def fake_sleep(secs):
        await asyncio.sleep(0)

    notifier = _RecordingNotifier()
    n = await runner.run_loop(
        _cfg(driver="Ayako"), p, max_ticks=2, run_drive=fake_drive,
        sleep=fake_sleep, now=lambda: "T", notifier=notifier,
        probe=lambda cfg: runner.QUEUE_ACTIONABLE,
        lane_probe=lambda cfg: [
            _lw("claude", "Ayako"),
            _lw("chatgpt/gpt-6-astra", "Rukawa", "codex_exec", "gpt-6-astra"),
        ],
    )
    assert n == 2
    assert seen[0][:3] == ("Ayako", "claude_p", None)
    assert seen[1][:3] == ("Rukawa", "codex_exec", "gpt-6-astra")
    assert "LANE RULE" in seen[1][3] and "lane chatgpt/gpt-6-astra as Rukawa" in seen[1][3]
    assert "lane=claude as Ayako" in notifier.heartbeats[0]
    assert "lane=chatgpt/gpt-6-astra as Rukawa" in notifier.heartbeats[1]


@pytest.mark.asyncio
async def test_run_loop_keeps_one_drive_per_lane_in_flight(tmp_path):
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 1})
    gate = asyncio.Event()
    fired = []

    async def drive(cfg, *, backend=None):
        fired.append(cfg.driver)
        if cfg.driver == "Ayako":
            await gate.wait()  # lane claude stays busy
        return _FakeResult()

    async def fake_sleep(secs):
        await asyncio.sleep(0)

    calls = {"n": 0}

    def lane_probe(cfg):
        calls["n"] += 1
        if calls["n"] == 1:
            return [_lw("claude", "Ayako")]
        return [_lw("claude", "Ayako"), _lw("chatgpt", "Rukawa", "codex_exec")]

    notifier = _RecordingNotifier()

    def should_stop():
        if len(fired) >= 2:
            gate.set()
            return True
        return False

    n = await runner.run_loop(
        _cfg(driver="Ayako"), p, run_drive=drive, sleep=fake_sleep,
        now=lambda: "T", notifier=notifier, should_stop=should_stop,
        probe=lambda cfg: runner.QUEUE_ACTIONABLE, lane_probe=lane_probe,
    )
    # Cycle 1 fired claude; cycle 2 saw claude still out and fired only chatgpt.
    assert n == 2 and fired == ["Ayako", "Rukawa"]


@pytest.mark.asyncio
async def test_run_loop_pulses_when_every_lane_has_a_drive_out(tmp_path):
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 1})
    gate = asyncio.Event()

    async def slow(cfg, *, backend=None):
        await gate.wait()
        return _FakeResult()

    async def fake_sleep(secs):
        await asyncio.sleep(0)

    notifier = _RecordingNotifier()

    def should_stop():
        # Stop once the busy cycle has pulsed; release the drive so the
        # exit drain completes.
        if any(runner.SKIP_LANES_BUSY in h for h in notifier.heartbeats):
            gate.set()
            return True
        return False

    n = await runner.run_loop(
        _cfg(driver="Ayako"), p, run_drive=slow, sleep=fake_sleep,
        now=lambda: "T", notifier=notifier, should_stop=should_stop,
        probe=lambda cfg: runner.QUEUE_ACTIONABLE,
        lane_probe=lambda cfg: [_lw("claude", "Ayako")],
    )
    assert n == 1
    assert any(runner.SKIP_LANES_BUSY in h for h in notifier.heartbeats)


@pytest.mark.asyncio
async def test_run_loop_lanes_off_or_empty_fires_the_default_drive(tmp_path):
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 1})
    seen = []
    probes = {"n": 0}

    async def fake_drive(cfg, *, backend=None):
        seen.append(cfg.driver)
        return _FakeResult()

    async def fake_sleep(secs):
        pass

    def lane_probe(cfg):
        probes["n"] += 1
        return [_lw("chatgpt", "Rukawa", "codex_exec")]

    # Pinned daemon: the lane probe is never consulted, and the interval
    # sleep is the plain injected sleep (no early-wake race).
    slept = []

    async def plain_sleep(secs):
        slept.append(secs)

    n = await runner.run_loop(
        _cfg(driver="Ayako", lanes=False), p, max_ticks=2, run_drive=fake_drive,
        sleep=plain_sleep, now=lambda: "T",
        probe=lambda cfg: runner.QUEUE_ACTIONABLE, lane_probe=lane_probe,
    )
    assert n == 2 and seen == ["Ayako", "Ayako"] and probes["n"] == 0
    assert slept == [600.0]
    seen.clear()
    # Lanes on but nothing attributable: the single default drive.
    runner.write_state(p, {"pid": 1})
    n = await runner.run_loop(
        _cfg(driver="Ayako"), p, max_ticks=1, run_drive=fake_drive,
        sleep=fake_sleep, now=lambda: "T",
        probe=lambda cfg: runner.QUEUE_ACTIONABLE, lane_probe=lambda cfg: [],
    )
    assert n == 1 and seen == ["Ayako"]


@pytest.mark.asyncio
async def test_run_loop_wakes_early_when_a_drive_completes(tmp_path):
    """A clean completion interrupts the interval sleep: the next probe (idle
    here) runs at once and the maintenance drive fires, instead of waiting
    out a 600 s interval."""
    import time as _time
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 1})
    verdicts = iter([runner.QUEUE_ACTIONABLE, runner.QUEUE_IDLE, runner.QUEUE_IDLE])

    async def fake_drive(cfg, *, backend=None):
        await asyncio.sleep(0.05)
        return _FakeResult()

    t0 = _time.monotonic()
    n = await runner.run_loop(
        _cfg(driver="Ayako"), p, max_ticks=2, run_drive=fake_drive,
        sleep=asyncio.sleep, now=lambda: "T",
        probe=lambda cfg: next(verdicts),
        lane_probe=lambda cfg: [_lw("claude", "Ayako")],
    )
    assert n == 2
    assert _time.monotonic() - t0 < 5


@pytest.mark.asyncio
async def test_run_loop_early_wake_respects_the_per_lane_floor(tmp_path, caplog):
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 1})
    never = asyncio.Event()
    sleeps = {"n": 0}

    async def fake_sleep(secs):
        sleeps["n"] += 1
        if sleeps["n"] == 1:
            await never.wait()  # the first interval is cut short by the wake
        await asyncio.sleep(0)

    async def fake_drive(cfg, *, backend=None):
        return _FakeResult()

    # clock() calls: cycle 1 floor check + fire stamp (0, 0); cycle 2 --
    # woken early -- floor check (10 -> held); cycle 3 floor check + fire
    # stamp (200, 200). max_ticks=3 bounds cycles, not just fires.
    ticks = iter([0.0, 0.0, 10.0, 200.0, 200.0, 200.0, 200.0])
    notifier = _RecordingNotifier()
    with caplog.at_level(logging.INFO, logger="tigerharness.autodrive.runner"):
        n = await runner.run_loop(
            _cfg(driver="Ayako"), p, max_ticks=3, run_drive=fake_drive,
            sleep=fake_sleep, now=lambda: "T", notifier=notifier,
            probe=lambda cfg: runner.QUEUE_ACTIONABLE,
            lane_probe=lambda cfg: [_lw("claude", "Ayako")],
            clock=lambda: next(ticks),
        )
    assert n == 2
    assert "woke early" in caplog.text
    assert "fired 10s ago; holding until the floor" in caplog.text
    assert any(runner.SKIP_LANES_BUSY in h for h in notifier.heartbeats)


@pytest.mark.asyncio
async def test_run_loop_errored_drive_does_not_wake(tmp_path):
    """An exception is not a clean completion: no early wake, so the loop
    sleeps its full (fake) interval and the cadence is unchanged."""
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 1})
    slept = []

    async def boom(cfg, *, backend=None):
        raise RuntimeError("kaboom")

    async def fake_sleep(secs):
        slept.append(secs)
        await asyncio.sleep(0)

    n = await runner.run_loop(
        _cfg(driver="Ayako"), p, max_ticks=2, run_drive=boom, sleep=fake_sleep,
        now=lambda: "T", probe=lambda cfg: runner.QUEUE_ACTIONABLE,
        lane_probe=lambda cfg: [_lw("claude", "Ayako")],
    )
    assert n == 2 and slept == [600.0]


def test_heartbeat_text_names_the_lane():
    assert runner.heartbeat_text(3, "T", 1) == "autodrive heartbeat - fire #3 launched T (in-flight 1)"
    assert runner.heartbeat_text(3, "T", 1, lane="chatgpt", driver="Rukawa").endswith(
        " lane=chatgpt as Rukawa"
    )
    assert runner.heartbeat_text(3, "T", 1, lane="claude").endswith(" lane=claude as (none)")


def test_default_prompt_lane_rule():
    p = runner.default_prompt("Rukawa", lane="chatgpt/gpt-6-astra")
    assert "LANE RULE (ADR 0012): this drive runs on lane chatgpt/gpt-6-astra as Rukawa" in p
    assert "journal sweep --driver Rukawa" in p and "handoff:" in p
    assert "LANE RULE" not in runner.default_prompt("Rukawa")
    assert "LANE RULE" not in runner.default_prompt(None, lane="claude")


def test_config_lanes_round_trip():
    cfg = _cfg(lanes=False)
    assert runner.config_to_dict(cfg)["lanes"] is False
    assert runner.config_from_state(runner.config_to_dict(cfg)).lanes is False
    # An older state file (no key) reads as lanes on.
    old = runner.config_to_dict(_cfg())
    del old["lanes"]
    assert runner.config_from_state(old).lanes is True


def test_cmd_start_pins_lanes_off_with_explicit_backend(tmp_path):
    _make_team(tmp_path)
    args = _args(["start", "--journal-dir", str(tmp_path / "journal"), "--backend", "claude_p"])
    with pytest.raises(RealDaemonSpawnBlocked):
        cli.cmd_start(args, now=lambda: "T")
    state = runner.read_state(runner.state_path(tmp_path / "journal"))
    assert state["lanes"] is False
    runner.state_path(tmp_path / "journal").unlink()  # no daemon on record
    args = _args(["start", "--journal-dir", str(tmp_path / "journal")])
    with pytest.raises(RealDaemonSpawnBlocked):
        cli.cmd_start(args, now=lambda: "T")
    assert runner.read_state(runner.state_path(tmp_path / "journal"))["lanes"] is True


def test_cmd_status_prints_the_lanes_line(tmp_path, capsys, monkeypatch):
    team = _team_root(tmp_path)
    runner.write_state(
        runner.state_path(team / "journal"),
        {"pid": os.getpid(), "interval_seconds": 600, "tick_count": 0,
         "journal_root": str(team / "journal"), "lanes": False},
    )
    monkeypatch.chdir(team)
    assert cli.cmd_status(_args(["status"])) == 0
    out = capsys.readouterr().out
    assert "lanes:        off (pinned by --backend/--model/--prompt)" in out

"""Tests for ``tigerharness autodrive`` (runner core + CLI).

The whole point of the module's dependency-injection seams is that the
loop, the clock, the backend, and process spawn/kill are all injectable,
so this suite never spawns a real subprocess or sends a real signal.
"""

from __future__ import annotations

import json
import os

import pytest

from tigerharness.autodrive import cli, runner
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
    def __init__(self, stop_reason="end_turn", cost_usd=0.0):
        self.stop_reason = stop_reason
        self.cost_usd = cost_usd


# --------------------------------------------------------------------------
# default_prompt
# --------------------------------------------------------------------------

def test_default_prompt_with_driver():
    p = runner.default_prompt("Anzai")
    assert "--driver Anzai --allow-api-drive" in p
    assert "Operator-authorized" in p
    assert "drive-journal" in p


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
    state = {"n": 0}

    async def fake_drive(cfg, *, backend=None):
        state["n"] += 1
        return _FakeResult()

    async def fake_sleep(secs):
        pass

    # Stop after the first drive completes.
    n = await runner.run_loop(
        _cfg(), p, run_drive=fake_drive, sleep=fake_sleep,
        should_stop=lambda: state["n"] >= 1, now=lambda: "T",
    )
    assert n == 1


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
            "started_at": "T0", "last_tick_at": "T1", "tick_count": 2,
            "last_stop_reason": "end_turn", "last_error": "boom",
        },
    )
    args = _args(["status", "--journal-dir", str(jr)])
    assert cli.cmd_status(args) == 0
    out = capsys.readouterr().out
    assert "running" in out
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

    async def fake_runner(cfg, state_file, *, should_stop):
        seen["cfg"] = cfg
        seen["stop"] = should_stop()  # exercises the closure
        return 0

    args = _args(["_loop", "--state-file", str(sfile)])
    rc = cli.cmd_loop(args, runner=fake_runner)
    assert rc == 0
    assert seen["cfg"].prompt == "go"
    assert seen["stop"] is False
    assert not sfile.exists()  # cleared on clean exit


def test_cmd_loop_should_stop_when_flagged(tmp_path):
    sfile = tmp_path / "s.json"
    runner.write_state(
        sfile,
        {"interval_seconds": 600, "prompt": "go", "stop_requested": True},
    )
    captured = {}

    async def fake_runner(cfg, state_file, *, should_stop):
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

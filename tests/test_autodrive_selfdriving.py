"""Tests for the self-driving journal (ADR 0010).

Three surfaces, kept apart from ``test_autodrive.py`` because they are one
coherent feature rather than more of the old one:

- :mod:`tigerharness.autodrive.settings` -- team ``configs/.env`` knobs.
- ``runner.probe_queue`` + ``run_loop``'s auto-stop -- the daemon deciding
  for itself whether to spend a drive, and when to exit.
- ``cli.start_lock`` / ``cli.ensure_running`` -- the atomic one-per-team
  guard and the opt-in auto-start hook.

As in ``test_autodrive.py``, nothing here spawns a real process, sends a
real signal, or calls a model: every seam is injected.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import multiprocessing
import os
import time
from pathlib import Path

import pytest

from tigerharness.autodrive import cli, runner, settings
from tigerharness.autodrive.runner import AutodriveConfig
from tigerharness.journal.models import State, Status, _utcnow_iso
from tigerharness.journal.paths import JournalPaths


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


def _args(argv):
    return cli.build_parser().parse_args(argv)


def _make_team(root: Path, *, default_persona="Anzai", env_lines=None):
    (root / "configs").mkdir(parents=True, exist_ok=True)
    (root / "configs" / "personas.yaml").write_text(
        f"default_persona: {default_persona}\n"
        f"personas:\n  - name: {default_persona}\n",
        encoding="utf-8",
    )
    if env_lines is not None:
        (root / "configs" / ".env").write_text(
            "\n".join(env_lines) + "\n", encoding="utf-8"
        )


def _make_journal(root: Path) -> JournalPaths:
    paths = JournalPaths(root=root)
    paths.active.mkdir(parents=True, exist_ok=True)
    return paths


def _write_task(paths: JournalPaths, task_id: str, **over) -> None:
    """Write a minimal active/<id>/status.json in the requested shape."""
    d = paths.active / task_id
    d.mkdir(parents=True, exist_ok=True)
    now = _utcnow_iso()
    fields = dict(
        id=task_id,
        title="t",
        kind="task",
        persona="Anzai",
        state=State.PENDING,
        sessions=0,
        max_sessions=8,
        created_at=now,
        updated_at=now,
        session_ref=None,
    )
    fields.update(over)
    (d / "status.json").write_text(
        Status(**fields).to_json(), encoding="utf-8"
    )


@pytest.fixture(autouse=True)
def _scrub_autodrive_env(monkeypatch):
    """The knobs are read from the *process* env first, so a developer's own
    exported settings would otherwise leak into assertions."""
    for name in (
        settings.AUTOSTART_ENV,
        settings.INTERVAL_ENV,
        settings.MAX_BUDGET_ENV,
        settings.DRIVER_ENV,
        settings.NOTIFY_ENV,
        settings.NOTIFY_CHANNEL_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


# ==========================================================================
# settings: the team .env layer
# ==========================================================================

@pytest.mark.parametrize("raw", ["1", "true", "TRUE", " yes ", "on"])
def test_truthy_accepts_positives(raw):
    assert settings.truthy(raw) is True


@pytest.mark.parametrize("raw", [None, "", "0", "false", "no", "off", "  "])
def test_truthy_rejects_negatives(raw):
    assert settings.truthy(raw) is False


def test_truthy_unrecognised_is_false_and_warns(caplog):
    with caplog.at_level(logging.WARNING):
        assert settings.truthy("maybe") is False
    assert "unrecognised boolean" in caplog.text


def test_read_env_file_parses_the_usual_shapes(tmp_path):
    p = tmp_path / ".env"
    p.write_text(
        "\n".join([
            "# a comment",
            "",
            "PLAIN=value",
            '  QUOTED = "spaced"  ',
            "SINGLE='sq'",
            "export EXPORTED=e",
            "NOEQUALS",
            "=novalue",
        ]),
        encoding="utf-8",
    )
    got = settings.read_env_file(p)
    assert got == {
        "PLAIN": "value",
        "QUOTED": "spaced",
        "SINGLE": "sq",
        "EXPORTED": "e",
    }


def test_read_env_file_missing_is_empty(tmp_path):
    assert settings.read_env_file(tmp_path / "nope") == {}


def test_team_env_path_none_for_personal_journal():
    assert settings.team_env_path(None) is None


def test_settings_process_env_beats_team_file(tmp_path):
    _make_team(tmp_path, env_lines=[f"{settings.INTERVAL_ENV}=900"])
    s = settings.Settings(
        team_root=tmp_path, env={settings.INTERVAL_ENV: "120"}
    )
    assert s.number(settings.INTERVAL_ENV) == 120.0


def test_settings_falls_through_to_team_file(tmp_path):
    _make_team(tmp_path, env_lines=[f"{settings.INTERVAL_ENV}=900"])
    s = settings.Settings(team_root=tmp_path, env={})
    assert s.number(settings.INTERVAL_ENV) == 900.0
    assert s.path == tmp_path / "configs" / ".env"


def test_settings_blank_process_value_falls_through(tmp_path):
    """A key blanked out in the environment must not shadow the team file --
    otherwise `FOO= tigerharness ...` silently forces an empty string."""
    _make_team(tmp_path, env_lines=[f"{settings.DRIVER_ENV}=Rukawa"])
    s = settings.Settings(
        team_root=tmp_path, env={settings.DRIVER_ENV: "   "}
    )
    assert s.get(settings.DRIVER_ENV) == "Rukawa"


def test_settings_unset_reads_none(tmp_path):
    _make_team(tmp_path)
    s = settings.Settings(team_root=tmp_path, env={})
    assert s.get(settings.DRIVER_ENV) is None
    assert s.number(settings.MAX_BUDGET_ENV) is None
    assert s.autostart is False


def test_settings_non_numeric_reads_as_unset_and_warns(tmp_path, caplog):
    _make_team(tmp_path, env_lines=[f"{settings.MAX_BUDGET_ENV}=lots"])
    s = settings.Settings(team_root=tmp_path, env={})
    with caplog.at_level(logging.WARNING):
        assert s.number(settings.MAX_BUDGET_ENV) is None
    assert "non-numeric" in caplog.text


def test_settings_personal_journal_has_no_file(tmp_path):
    s = settings.Settings(team_root=None, env={})
    assert s.file == {}
    assert s.path is None


def test_settings_defaults_to_process_environ(monkeypatch):
    monkeypatch.setenv(settings.AUTOSTART_ENV, "1")
    assert settings.Settings().autostart is True


# ==========================================================================
# probe_queue: the free, non-AI queue check
# ==========================================================================

def test_probe_without_journal_root_is_actionable():
    """No configured root (an old state file) must behave exactly like the
    pre-ADR-0010 daemon: fire every interval."""
    assert runner.probe_queue(_cfg()) == runner.QUEUE_ACTIONABLE


def test_probe_empty_journal_is_idle(tmp_path):
    _make_journal(tmp_path)
    assert runner.probe_queue(_cfg(journal_root=str(tmp_path))) == \
        runner.QUEUE_IDLE


def test_probe_pending_task_is_actionable(tmp_path):
    paths = _make_journal(tmp_path)
    _write_task(paths, "20260812-000000-a")
    assert runner.probe_queue(_cfg(journal_root=str(tmp_path))) == \
        runner.QUEUE_ACTIONABLE


def test_probe_busy_task_is_busy(tmp_path):
    """A live session owns the task: a fire would sweep, see busy, and exit.
    The daemon should not pay for that."""
    paths = _make_journal(tmp_path)
    _write_task(
        paths, "20260812-000000-b",
        state=State.IN_PROGRESS, session_ref="tok",
    )
    assert runner.probe_queue(_cfg(journal_root=str(tmp_path))) == \
        runner.QUEUE_BUSY


def test_probe_deferred_entry_is_actionable(tmp_path):
    """The regression that matters most: a Slack `journal defer` leaves an
    inbox entry that is not a task yet. Miss it and the daemon stops with a
    full inbox."""
    paths = _make_journal(tmp_path)
    entry = paths.deferred / "20260812-000000-slack-ask"
    entry.mkdir(parents=True)
    (entry / "deferred.json").write_text("{}", encoding="utf-8")
    assert runner.probe_queue(_cfg(journal_root=str(tmp_path))) == \
        runner.QUEUE_ACTIONABLE


def test_probe_blocked_only_is_idle(tmp_path):
    """Blocked work waits on the Operator, not on a driver. Spinning the
    daemon against it would burn an interval forever for no progress."""
    paths = _make_journal(tmp_path)
    _write_task(paths, "20260812-000000-c", state=State.BLOCKED)
    assert runner.probe_queue(_cfg(journal_root=str(tmp_path))) == \
        runner.QUEUE_IDLE


def test_probe_failure_degrades_to_actionable(tmp_path, monkeypatch, caplog):
    """Fail-soft direction is deliberate: an over-fire costs one drive, a
    false idle costs every task in the queue."""
    _make_journal(tmp_path)

    def boom(*a, **k):
        raise OSError("disk gone")

    # NB: `tigerharness.journal.sweep` resolves to the function re-exported
    # by the package __init__, which shadows the submodule of the same name.
    # import_module reaches the real module object.
    sweep_mod = importlib.import_module("tigerharness.journal.sweep")
    monkeypatch.setattr(sweep_mod, "sweep", boom)
    with caplog.at_level(logging.WARNING):
        verdict = runner.probe_queue(_cfg(journal_root=str(tmp_path)))
    assert verdict == runner.QUEUE_ACTIONABLE
    assert "queue probe failed" in caplog.text


# ==========================================================================
# run_loop: auto-stop
# ==========================================================================

@pytest.mark.asyncio
async def test_loop_idle_fires_maintenance_then_stops(tmp_path):
    """The whole ADR-0010 arc in one test: idle -> one maintenance drive ->
    clean exit, with a closing message so the silence is legible."""
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 1})
    drives = []
    notifier = _RecordingNotifier()

    async def fake_drive(cfg, *, backend=None):
        drives.append(1)
        return _FakeResult()

    async def fake_sleep(secs):
        return None

    n = await runner.run_loop(
        _cfg(), p,
        run_drive=fake_drive, sleep=fake_sleep, now=lambda: "T",
        probe=lambda cfg: runner.QUEUE_IDLE,
        notifier=notifier,
        max_ticks=10,
    )
    assert n == 1                      # exactly one maintenance drive
    assert len(drives) == 1
    assert any("queue drained" in h for h in notifier.heartbeats)


@pytest.mark.asyncio
async def test_loop_busy_skips_the_fire(tmp_path):
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 1})
    drives = []

    async def fake_drive(cfg, *, backend=None):
        drives.append(1)
        return _FakeResult()

    async def fake_sleep(secs):
        return None

    n = await runner.run_loop(
        _cfg(), p,
        run_drive=fake_drive, sleep=fake_sleep, now=lambda: "T",
        probe=lambda cfg: runner.QUEUE_BUSY,
        max_ticks=3,
    )
    assert n == 0                      # never spent a drive
    assert drives == []
    # ...and the cycle bound is what stopped it, not the fire bound: with no
    # fire, `record_fire` never ran, so the key was never written at all.
    assert "fire_count" not in runner.read_state(p)


@pytest.mark.asyncio
async def test_loop_work_arriving_resets_the_maintenance_latch(tmp_path):
    """Idle, then work, then idle again must run maintenance a second time
    -- otherwise the daemon exits without sweeping the memory it just
    dirtied."""
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 1})
    verdicts = [
        runner.QUEUE_IDLE,        # -> maintenance fire #1
        runner.QUEUE_ACTIONABLE,  # -> work fire #2, latch reset
    ]

    async def fake_drive(cfg, *, backend=None):
        return _FakeResult()

    async def fake_sleep(secs):
        return None

    def probe(cfg):
        # Idle forever after the scripted prefix. Extra idle cycles are
        # expected while a drive is still settling, so the assertion is on
        # the number of *fires*, not the number of probes.
        return verdicts.pop(0) if verdicts else runner.QUEUE_IDLE

    n = await runner.run_loop(
        _cfg(), p,
        run_drive=fake_drive, sleep=fake_sleep, now=lambda: "T",
        probe=probe,
        max_ticks=10,
    )
    # maintenance #1, work #2, and -- the point of the test -- maintenance #3
    # for the memory the work drive dirtied. Without the latch reset this is 2.
    assert n == 3
    assert verdicts == []


@pytest.mark.asyncio
async def test_loop_idle_waits_for_an_in_flight_drive(tmp_path):
    """A drive still running when the queue reads idle must be allowed to
    finish before the daemon decides anything."""
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 1})
    gate = {"open": False}

    async def fake_drive(cfg, *, backend=None):
        while not gate["open"]:
            await runner.asyncio.sleep(0)
        return _FakeResult()

    async def fake_sleep(secs):
        gate["open"] = True     # let the in-flight drive finish
        await runner.asyncio.sleep(0)

    verdicts = [runner.QUEUE_ACTIONABLE] + [runner.QUEUE_IDLE] * 8
    n = await runner.run_loop(
        _cfg(), p,
        run_drive=fake_drive, sleep=fake_sleep, now=lambda: "T",
        probe=lambda cfg: verdicts.pop(0),
        max_ticks=10,
    )
    # fire 1 (work) + fire 2 (maintenance, after the first drained), then stop
    assert n == 2


@pytest.mark.asyncio
async def test_loop_failed_maintenance_drive_still_stops(tmp_path):
    """A maintenance drive that raises must not pin the daemon open forever
    -- the error is recorded and notified, and the loop still exits."""
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 1})

    async def fake_drive(cfg, *, backend=None):
        raise RuntimeError("backend down")

    async def fake_sleep(secs):
        return None

    n = await runner.run_loop(
        _cfg(), p,
        run_drive=fake_drive, sleep=fake_sleep, now=lambda: "T",
        probe=lambda cfg: runner.QUEUE_IDLE,
        max_ticks=10,
    )
    assert n == 1
    assert "RuntimeError" in runner.read_state(p)["last_error"]


@pytest.mark.asyncio
async def test_loop_stop_requested_posts_no_drained_message(tmp_path):
    """`autodrive stop` is not a drained queue; don't claim it was."""
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 1})
    notifier = _RecordingNotifier()

    async def fake_drive(cfg, *, backend=None):
        return _FakeResult()

    n = await runner.run_loop(
        _cfg(), p,
        run_drive=fake_drive, sleep=lambda s: _noop(), now=lambda: "T",
        probe=lambda cfg: runner.QUEUE_ACTIONABLE,
        should_stop=lambda: True,
        notifier=notifier,
    )
    assert n == 0
    assert not any("queue drained" in h for h in notifier.heartbeats)


async def _noop():
    return None


def test_drained_text_names_the_restart_path():
    txt = runner.drained_text(3, "T")
    assert "queue drained" in txt
    assert "3 drive(s)" in txt
    assert "Scheduling new work starts it again" in txt


def test_config_roundtrips_the_journal_root():
    cfg = _cfg(journal_root="/tmp/j")
    assert runner.config_to_dict(cfg)["journal_root"] == "/tmp/j"
    assert runner.config_from_state(
        runner.config_to_dict(cfg)
    ).journal_root == "/tmp/j"


def test_config_from_old_state_has_no_journal_root():
    """Upgrade path: a state file written before ADR 0010 must deserialize,
    and must not suddenly acquire auto-stop mid-flight."""
    old = {
        "interval_seconds": 600.0,
        "prompt": "p",
    }
    assert runner.config_from_state(old).journal_root is None


# ==========================================================================
# cli: the atomic one-per-team lock
# ==========================================================================

def test_start_lock_is_exclusive_across_processes(tmp_path):
    """The race the flock exists for. A child process holds the lock while
    the parent tries to take it; the parent must block until released.

    Uses a real second process because that is the only honest test of an
    advisory file lock -- two threads in one process would share it.
    """
    state_root = tmp_path / "journal"
    state_root.mkdir()
    ready = multiprocessing.Event()
    release = multiprocessing.Event()
    proc = multiprocessing.Process(
        target=_hold_lock, args=(str(state_root), ready, release)
    )
    proc.start()
    try:
        assert ready.wait(timeout=10)
        acquired_at = {}

        def _take():
            with cli.start_lock(state_root):
                acquired_at["t"] = time.monotonic()

        started = time.monotonic()
        release.set()
        _take()
        assert acquired_at["t"] >= started
    finally:
        release.set()
        proc.join(timeout=10)
    assert cli.lock_path(state_root).exists()


def _hold_lock(state_root, ready, release):  # pragma: no cover - child process
    with cli.start_lock(Path(state_root)):
        ready.set()
        release.wait(timeout=10)


def test_start_lock_creates_missing_root(tmp_path):
    root = tmp_path / "a" / "b" / "journal"
    with cli.start_lock(root):
        pass
    assert cli.lock_path(root).is_file()


def test_start_holds_the_lock_across_check_and_spawn(tmp_path):
    """Regression for the TOCTOU: the spawn must happen *inside* the lock,
    not after it. Asserted by observing the lock is held at spawn time."""
    _make_team(tmp_path)
    jr = tmp_path / "journal"
    held = {}

    def fake_spawn(state_file, *, cwd, log_file, env):
        # If the lock were already released, a non-blocking acquire here
        # would succeed. It must not.
        import fcntl
        with open(cli.lock_path(jr), "w") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                held["locked"] = False
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except BlockingIOError:  # pragma: no cover - same-process quirk
                held["locked"] = True
        return 7

    args = _args(["start", "--journal-dir", str(jr)])
    rc = cli.cmd_start(args, spawn=fake_spawn, now=lambda: "T")
    assert rc == 0
    assert "locked" in held  # spawn ran inside the with-block


# ==========================================================================
# cli: env-driven defaults
# ==========================================================================

def test_start_reads_interval_and_budget_from_team_env(tmp_path):
    _make_team(tmp_path, env_lines=[
        f"{settings.INTERVAL_ENV}=900",
        f"{settings.MAX_BUDGET_ENV}=3.5",
        f"{settings.DRIVER_ENV}=Rukawa",
    ])
    jr = tmp_path / "journal"
    args = _args(["start", "--journal-dir", str(jr)])
    assert cli.cmd_start(args, spawn=lambda *a, **k: 1, now=lambda: "T") == 0
    st = runner.read_state(runner.state_path(jr))
    assert st["interval_seconds"] == 900.0
    assert st["max_budget_usd"] == 3.5
    assert st["driver"] == "Rukawa"
    assert st["journal_root"] == str(jr)


def test_start_flag_beats_team_env(tmp_path):
    _make_team(tmp_path, env_lines=[f"{settings.INTERVAL_ENV}=900"])
    jr = tmp_path / "journal"
    args = _args(["start", "--journal-dir", str(jr), "--interval", "1200"])
    cli.cmd_start(args, spawn=lambda *a, **k: 1, now=lambda: "T")
    assert runner.read_state(
        runner.state_path(jr)
    )["interval_seconds"] == 1200.0


def test_start_team_env_below_the_floor_is_refused(tmp_path, capsys):
    """The 60s floor is not bypassable by editing a file instead of typing
    a flag."""
    _make_team(tmp_path, env_lines=[f"{settings.INTERVAL_ENV}=5"])
    jr = tmp_path / "journal"
    args = _args(["start", "--journal-dir", str(jr)])
    rc = cli.cmd_start(args, spawn=lambda *a, **k: 1, now=lambda: "T")
    assert rc == 2
    assert "must be >= 60" in capsys.readouterr().err


def test_start_reads_notify_from_team_env(tmp_path):
    _make_team(tmp_path, env_lines=[
        f"{settings.NOTIFY_ENV}=none",
        f"{settings.NOTIFY_CHANNEL_ENV}=C0IGNORED",
    ])
    jr = tmp_path / "journal"
    cli.cmd_start(
        _args(["start", "--journal-dir", str(jr)]),
        spawn=lambda *a, **k: 1, now=lambda: "T",
    )
    st = runner.read_state(runner.state_path(jr))
    assert st["notify"] == "none"
    assert st["notify_channel"] == "C0IGNORED"


def test_start_rejects_bad_notify_from_team_env(tmp_path, capsys):
    """argparse guards the flag; nothing guarded the file until now."""
    _make_team(tmp_path, env_lines=[f"{settings.NOTIFY_ENV}=email"])
    jr = tmp_path / "journal"
    rc = cli.cmd_start(
        _args(["start", "--journal-dir", str(jr)]),
        spawn=lambda *a, **k: 1, now=lambda: "T",
    )
    assert rc == 2
    assert "not 'slack' or 'none'" in capsys.readouterr().err


def test_start_default_interval_when_nothing_configured(tmp_path):
    _make_team(tmp_path)
    jr = tmp_path / "journal"
    cli.cmd_start(
        _args(["start", "--journal-dir", str(jr)]),
        spawn=lambda *a, **k: 1, now=lambda: "T",
    )
    assert runner.read_state(runner.state_path(jr))["interval_seconds"] == \
        runner.DEFAULT_INTERVAL_SECONDS


def test_start_quiet_prints_one_line(tmp_path, capsys):
    _make_team(tmp_path)
    jr = tmp_path / "journal"
    cli.cmd_start(
        _args(["start", "--journal-dir", str(jr)]),
        spawn=lambda *a, **k: 9, now=lambda: "T", quiet=True,
    )
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    assert "auto-started (pid 9)" in out[0]


def test_start_quiet_already_running_is_silent(tmp_path, capsys, caplog):
    jr = tmp_path / "journal"
    jr.mkdir(parents=True)
    runner.write_state(runner.state_path(jr), {"pid": os.getpid()})
    with caplog.at_level(logging.INFO):
        rc = cli.cmd_start(
            _args(["start", "--journal-dir", str(jr)]),
            spawn=lambda *a, **k: 1, now=lambda: "T", quiet=True,
        )
    assert rc == cli.RC_ALREADY_RUNNING
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    assert "already running" in caplog.text


# ==========================================================================
# cli: ensure_running (the auto-start hook)
# ==========================================================================

def test_ensure_running_is_a_noop_when_not_opted_in(tmp_path):
    _make_team(tmp_path)
    called = []
    assert cli.ensure_running(
        tmp_path / "journal",
        start=lambda *a, **k: called.append(1) or 0,
    ) is False
    assert called == []


def test_ensure_running_starts_when_team_env_opts_in(tmp_path):
    _make_team(tmp_path, env_lines=[f"{settings.AUTOSTART_ENV}=1"])
    seen = {}

    def fake_start(args, **kw):
        seen["journal_dir"] = args.journal_dir
        seen["quiet"] = kw.get("quiet")
        return 0

    assert cli.ensure_running(tmp_path / "journal", start=fake_start) is True
    assert seen["journal_dir"] == str(tmp_path / "journal")
    assert seen["quiet"] is True


def test_ensure_running_treats_already_running_as_success(tmp_path):
    _make_team(tmp_path, env_lines=[f"{settings.AUTOSTART_ENV}=1"])
    assert cli.ensure_running(
        tmp_path / "journal",
        start=lambda *a, **k: cli.RC_ALREADY_RUNNING,
    ) is True


def test_ensure_running_reports_other_failures_without_raising(
    tmp_path, caplog
):
    _make_team(tmp_path, env_lines=[f"{settings.AUTOSTART_ENV}=1"])
    with caplog.at_level(logging.WARNING):
        assert cli.ensure_running(
            tmp_path / "journal", start=lambda *a, **k: 2
        ) is False
    assert "still queued" in caplog.text


def test_ensure_running_swallows_exceptions(tmp_path, caplog):
    """The task is already on disk. Losing the daemon must not lose it."""
    _make_team(tmp_path, env_lines=[f"{settings.AUTOSTART_ENV}=1"])

    def boom(*a, **k):
        raise OSError("no fork for you")

    with caplog.at_level(logging.WARNING):
        assert cli.ensure_running(tmp_path / "journal", start=boom) is False
    assert "auto-start failed" in caplog.text


def test_ensure_running_accepts_injected_settings(tmp_path):
    s = settings.Settings(team_root=None, env={settings.AUTOSTART_ENV: "1"})
    assert cli.ensure_running(
        tmp_path / "journal", start=lambda *a, **k: 0, settings=s
    ) is True


def test_ensure_running_end_to_end_spawns_once(tmp_path):
    """Two schedulers in a row: the first starts a daemon, the second finds
    it and does not start a second one."""
    _make_team(tmp_path, env_lines=[f"{settings.AUTOSTART_ENV}=1"])
    jr = tmp_path / "journal"
    spawns = []

    def fake_spawn(state_file, *, cwd, log_file, env):
        spawns.append(1)
        return os.getpid()      # a pid that is definitely alive

    def start(args, **kw):
        return cli.cmd_start(
            args, spawn=fake_spawn, now=lambda: "T", **kw
        )

    assert cli.ensure_running(jr, start=start) is True
    assert cli.ensure_running(jr, start=start) is True
    assert len(spawns) == 1


# ==========================================================================
# journal CLI: the hook is actually wired in
# ==========================================================================

def _journal_args(**over):
    base = dict(journal_dir=None)
    base.update(over)
    return argparse.Namespace(**base)


def test_journal_autostart_helper_swallows_import_failure(
    tmp_path, monkeypatch, caplog
):
    from tigerharness.journal import cli as jcli

    def boom(root):
        raise RuntimeError("nope")

    monkeypatch.setattr(
        "tigerharness.autodrive.ensure_running", boom, raising=True
    )
    with caplog.at_level(logging.WARNING):
        jcli._autostart(JournalPaths(root=tmp_path / "journal"))
    assert "auto-start skipped" in caplog.text


def test_journal_new_calls_the_autostart_hook(tmp_path, monkeypatch, capsys):
    """`journal new` must ring the bell after the task is safely on disk."""
    from tigerharness.journal import cli as jcli

    _make_team(tmp_path)
    prd = tmp_path / "prd.md"
    prd.write_text("# do a thing\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(jcli, "_autostart", lambda paths: calls.append(paths))

    rc = jcli.main([
        "--journal-dir", str(tmp_path / "journal"), "new",
        "--kind", "task", "--prd", str(prd), "--persona", "Anzai",
    ])
    assert rc == 0
    assert len(calls) == 1


def test_journal_defer_calls_the_autostart_hook(tmp_path, monkeypatch):
    """The Slack rail: defer schedules and rings the bell, but the bridge
    session still never drives."""
    from tigerharness.journal import cli as jcli

    _make_team(tmp_path)
    _make_journal(tmp_path / "journal")
    (tmp_path / "workflow").mkdir(exist_ok=True)
    (tmp_path / "workflow" / "triage.md").write_text("x", encoding="utf-8")
    payload = tmp_path / "convo.md"
    payload.write_text("please do the thing\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(jcli, "_autostart", lambda paths: calls.append(paths))

    rc = jcli.main([
        "defer", "--team", tmp_path.name, "--team-dir", str(tmp_path),
        "--title", "t",
        "--playbook", "triage", "--payload-file", str(payload),
    ])
    assert rc == 0
    assert len(calls) == 1


def test_schedule_add_warns_that_it_is_deprecated(tmp_path, capsys):
    from tigerharness.journal import cli as jcli

    _make_team(tmp_path)
    _make_journal(tmp_path / "journal")
    prd = tmp_path / "prd.md"
    prd.write_text("# x\n", encoding="utf-8")
    rc = jcli.main([
        "--journal-dir", str(tmp_path / "journal"), "schedule", "add",
        "--title", "daily thing", "--at", "09:00",
        "--kind", "task", "--prd", str(prd), "--persona", "Anzai",
    ])
    assert rc == 0
    err = capsys.readouterr().err
    assert "DEPRECATED" in err
    assert "ADR 0010" in err

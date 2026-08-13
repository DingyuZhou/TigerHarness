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
import contextlib
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


def _make_crashed(paths: JournalPaths, task_id: str) -> None:
    """Write a task the sweep will call *crashed*.

    Two independent signals must both be stale: an ``updated_at`` older
    than the stuck timeout, *and* a task dir nobody has touched. The sweep
    checks file mtimes as a second opinion precisely so a live worker with
    a lagging heartbeat is not mistaken for a corpse -- which means a
    fixture written a millisecond ago reads as alive unless it is aged."""
    _write_task(
        paths, task_id,
        state=State.IN_PROGRESS, session_ref="tok",
        updated_at="2026-01-01T00:00:00Z",
    )
    old = time.time() - 86_400
    os.utime(paths.active / task_id / "status.json", (old, old))


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
        # Not autodrive-owned, but autodrive now inherits it -- and any host
        # running a slack bridge has it exported.
        settings.SLACK_NOTIFY_CHANNEL_ENV,
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


def test_probe_crashed_only_is_rescue(tmp_path):
    """A crashed task alone is real work -- but the loop, not the probe,
    decides whether now is the moment. Reported as its own verdict so the
    loop can tell "somebody else's crash" from "our own drive whose
    heartbeat went stale"."""
    paths = _make_journal(tmp_path)
    _make_crashed(paths, "20260812-000000-x")
    assert runner.probe_queue(_cfg(journal_root=str(tmp_path))) == \
        runner.QUEUE_RESCUE


def test_probe_stale_heartbeat_over_a_live_task_is_busy(tmp_path):
    """The two fixes composing: a workflow that advances its walk without
    stamping status.json looks crashed by heartbeat alone. Recent writes in
    the task dir out-vote the stale timestamp, so the probe says busy --
    the daemon neither fires nor even considers a rescue."""
    paths = _make_journal(tmp_path)
    _write_task(
        paths, "20260812-000000-x",
        state=State.IN_PROGRESS, session_ref="tok",
        updated_at="2026-01-01T00:00:00Z",
    )  # files written just now: stale heartbeat, live on disk
    assert runner.probe_queue(_cfg(journal_root=str(tmp_path))) == \
        runner.QUEUE_BUSY


def test_probe_crashed_outranks_pending_so_it_is_still_a_rescue(tmp_path):
    """A pending task in the queue must NOT downgrade the verdict.

    Tempting to reason "there is real new work, so fire normally" -- and
    wrong. `SweepResult.actionable()` returns idle+crashed *before*
    pending, so a drive fired for the pending task sweeps, finds the
    crashed one ranked higher, and takes that instead. The verdict has to
    describe what a fire would actually do, not what queued it."""
    paths = _make_journal(tmp_path)
    _make_crashed(paths, "20260812-000000-x")
    _write_task(paths, "20260812-000001-y")
    assert runner.probe_queue(_cfg(journal_root=str(tmp_path))) == \
        runner.QUEUE_RESCUE


def test_probe_crashed_plus_deferred_is_still_a_rescue(tmp_path):
    """Same reasoning via the other actionable source: the drive
    materializes an inbox entry into a *pending* task, which the crashed
    one then outranks."""
    paths = _make_journal(tmp_path)
    _make_crashed(paths, "20260812-000000-x")
    entry = paths.deferred / "20260812-000000-slack-ask"
    entry.mkdir(parents=True)
    (entry / "deferred.json").write_text("{}", encoding="utf-8")
    assert runner.probe_queue(_cfg(journal_root=str(tmp_path))) == \
        runner.QUEUE_RESCUE


def test_probe_idle_task_without_a_crash_is_plain_actionable(tmp_path):
    """The gate stays narrow where it can: a cleanly-detached task is
    resumable, not a rescue, so overlap is still allowed."""
    paths = _make_journal(tmp_path)
    _write_task(
        paths, "20260812-000000-z",
        state=State.IN_PROGRESS, session_ref=None,
        updated_at="2026-01-01T00:00:00Z",
    )
    assert runner.probe_queue(_cfg(journal_root=str(tmp_path))) == \
        runner.QUEUE_ACTIONABLE


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
async def test_loop_busy_stretch_never_goes_silent(tmp_path):
    """Regression: the queue probe made a busy cycle free *and* silent, so a
    long busy stretch was indistinguishable from a crashed daemon. Every
    cycle must still pulse -- and still without spending a drive."""
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 1})
    notifier = _RecordingNotifier()
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
        notifier=notifier,
        max_ticks=3,
    )
    assert n == 0 and drives == []          # still costs nothing
    assert len(notifier.heartbeats) == 3    # ...but is no longer silent
    assert all("no fire at T" in h for h in notifier.heartbeats)
    assert all(runner.SKIP_BUSY in h for h in notifier.heartbeats)
    # A skip has no drive to thread a completion under, so it must not
    # invent one -- the pulse is a parent message and nothing else.
    assert notifier.updates == []


@pytest.mark.asyncio
async def test_loop_pulses_while_waiting_on_an_in_flight_drive(tmp_path):
    """The other silent path: queue idle but a drive still settling."""
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 1})
    notifier = _RecordingNotifier()
    gate = {"open": False}

    async def fake_drive(cfg, *, backend=None):
        while not gate["open"]:
            await runner.asyncio.sleep(0)
        return _FakeResult()

    sleeps = {"n": 0}

    async def fake_sleep(secs):
        # Hold the drive in flight across one full cycle, so the next probe
        # genuinely lands on "idle, but a drive is still settling". Opening
        # the gate on the first sleep would let `_reap_done` clear it before
        # the branch is ever reached.
        sleeps["n"] += 1
        if sleeps["n"] >= 2:
            gate["open"] = True
        await runner.asyncio.sleep(0)

    verdicts = [runner.QUEUE_ACTIONABLE] + [runner.QUEUE_IDLE] * 8
    await runner.run_loop(
        _cfg(), p,
        run_drive=fake_drive, sleep=fake_sleep, now=lambda: "T",
        probe=lambda cfg: verdicts.pop(0),
        notifier=notifier,
        max_ticks=10,
    )
    assert any(runner.SKIP_WAITING in h for h in notifier.heartbeats)


@pytest.mark.asyncio
async def test_loop_rescue_with_nothing_in_flight_fires(tmp_path):
    """A crash the daemon did not cause is genuinely its to pick up."""
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 1})
    notifier = _RecordingNotifier()
    drives = []

    async def fake_drive(cfg, *, backend=None):
        drives.append(1)
        return _FakeResult()

    async def fake_sleep(secs):
        return None

    n = await runner.run_loop(
        _cfg(), p,
        run_drive=fake_drive, sleep=fake_sleep, now=lambda: "T",
        probe=lambda cfg: runner.QUEUE_RESCUE,
        notifier=notifier,
        max_ticks=1,
    )
    assert n == 1 and len(drives) == 1
    assert not any(runner.SKIP_RESCUE_HELD in h for h in notifier.heartbeats)


@pytest.mark.asyncio
async def test_loop_holds_the_rescue_while_a_drive_is_in_flight(tmp_path):
    """The stampede regression, in miniature. A workflow task whose
    heartbeat lags reads as crashed; the sweep calls that actionable; the
    daemon fires a rescue *on top of the drive already working it* -- and
    because the stale verdict never clears, again every interval, until
    six sessions on a 3.8 GiB box brought the OOM killer down on the whole
    cgroup. The loop must hold a rescue whenever it has a drive out, and
    say so rather than going quiet."""
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 1})
    notifier = _RecordingNotifier()
    drives = []
    gate = {"open": False}

    async def fake_drive(cfg, *, backend=None):
        drives.append(1)
        while not gate["open"]:
            await runner.asyncio.sleep(0)
        return _FakeResult()

    sleeps = {"n": 0}

    async def fake_sleep(secs):
        # Keep the first drive in flight for several cycles, so the rescue
        # verdict lands repeatedly while it is still working -- the shape
        # of the real incident, not a single unlucky tick.
        sleeps["n"] += 1
        if sleeps["n"] >= 4:
            gate["open"] = True
        await runner.asyncio.sleep(0)

    # One real fire to get a drive in flight, then nothing but rescue.
    verdicts = [runner.QUEUE_ACTIONABLE] + [runner.QUEUE_RESCUE] * 20
    await runner.run_loop(
        _cfg(), p,
        run_drive=fake_drive, sleep=fake_sleep, now=lambda: "T",
        probe=lambda cfg: verdicts.pop(0),
        notifier=notifier,
        max_ticks=4,
    )
    held = [h for h in notifier.heartbeats if runner.SKIP_RESCUE_HELD in h]
    assert len(held) >= 2            # held every cycle, not just once
    assert "in-flight 1" in held[0]  # and says what it is waiting on
    # The point of the whole fix: no second session piled onto the task
    # the first one still owns.
    assert len(drives) == 1


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


def test_skip_text_shares_the_heartbeat_prefix():
    """The rhythm is the health signal, so a skip pulse must read as the
    same pulse as a fire -- a differently-shaped message would break the
    channel's continuity and re-open the "is it dead?" question."""
    txt = runner.skip_text(2, "T", runner.SKIP_BUSY, 1)
    fire = runner.heartbeat_text(2, "T", 1)
    prefix = "autodrive heartbeat - "
    assert txt.startswith(prefix) and fire.startswith(prefix)
    assert "no fire at T" in txt
    assert "queue busy" in txt
    assert "in-flight 1" in txt
    assert "2 drive(s) so far" in txt


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
# cli: cgroup isolation for the spawned daemon
# ==========================================================================

def test_cgroup_scope_prefix_wraps_the_spawn_when_systemd_is_available(
    monkeypatch,
):
    """The OOM shared-fate fix: a daemon auto-started from inside the Slack
    bridge inherits the bridge's cgroup, so a memory spike in a drive takes
    the bridge down with it. A transient scope under user.slice breaks
    that. --collect so repeated starts leave no failed units behind."""
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/" + name)
    prefix = cli.cgroup_scope_prefix({"XDG_RUNTIME_DIR": "/run/user/1000"})
    assert prefix == [
        "systemd-run", "--user", "--scope", "--quiet", "--collect",
    ]


def test_cgroup_scope_prefix_empty_without_systemd_run(monkeypatch):
    """Containers, non-systemd hosts, CI. Degrading to a plain spawn is the
    safe direction: a wrong "yes" stops the daemon starting at all, a wrong
    "no" only forfeits the isolation."""
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.cgroup_scope_prefix({"XDG_RUNTIME_DIR": "/run/user/1000"}) == []


def test_cgroup_scope_prefix_empty_without_a_user_runtime_dir(monkeypatch):
    """No per-user runtime dir means no user bus to register a scope on --
    `systemd-run --user` would fail outright."""
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/" + name)
    assert cli.cgroup_scope_prefix({}) == []


def test_cgroup_scope_prefix_defaults_to_the_process_environment(
    monkeypatch,
):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert cli.cgroup_scope_prefix() == []
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    assert cli.cgroup_scope_prefix()[0] == "systemd-run"


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


# ==========================================================================
# settings: a trailing comment must not eat a numeric knob
# ==========================================================================

def test_env_value_drops_a_trailing_comment(tmp_path):
    """`MAX_BUDGET=5  # cap` has to read as 5. Before this, it parsed as the
    string "5  # cap", `number()` rejected it, and the budget guard degraded
    to *uncapped* -- the exact failure the knob exists to prevent, announced
    only in a log line nobody reads."""
    _make_team(tmp_path, env_lines=[
        f"{settings.MAX_BUDGET_ENV}=5  # per-drive cap",
    ])
    s = settings.Settings(team_root=tmp_path, env={})
    assert s.number(settings.MAX_BUDGET_ENV) == 5.0


def test_env_tab_before_a_comment_is_also_a_comment(tmp_path):
    _make_team(tmp_path, env_lines=[
        f"{settings.INTERVAL_ENV}=900\t# fifteen minutes",
    ])
    s = settings.Settings(team_root=tmp_path, env={})
    assert s.number(settings.INTERVAL_ENV) == 900.0


def test_env_hash_without_leading_space_stays_in_the_value(tmp_path):
    """Only a *whitespace-preceded* `#` starts a comment (dotenv's rule), so
    a value that legitimately contains one survives."""
    _make_team(tmp_path, env_lines=[
        f"{settings.NOTIFY_CHANNEL_ENV}=C0AB#123",
    ])
    s = settings.Settings(team_root=tmp_path, env={})
    assert s.get(settings.NOTIFY_CHANNEL_ENV) == "C0AB#123"


def test_env_quoted_value_is_taken_verbatim(tmp_path):
    _make_team(tmp_path, env_lines=[
        f"{settings.NOTIFY_CHANNEL_ENV}='C0AB #123'",
    ])
    s = settings.Settings(team_root=tmp_path, env={})
    assert s.get(settings.NOTIFY_CHANNEL_ENV) == "C0AB #123"


def test_env_quoted_value_then_a_comment_keeps_neither_quotes_nor_comment(
    tmp_path,
):
    """Quotes AND a trailing comment together -- the ordinary way an operator
    annotates a channel id. A parser that asks "does it end in a quote?"
    answers no here and hands Slack the literal `"C0AB"`, quotes included,
    which fails to post and says nothing about why."""
    _make_team(tmp_path, env_lines=[
        f'{settings.NOTIFY_CHANNEL_ENV}="C0AB"  # operator DM',
    ])
    s = settings.Settings(team_root=tmp_path, env={})
    assert s.get(settings.NOTIFY_CHANNEL_ENV) == "C0AB"


def test_env_quoted_number_then_a_comment_still_reads_as_a_number(tmp_path):
    """Same shape on the knob that matters most: a budget cap that fails to
    parse degrades to *uncapped*."""
    _make_team(tmp_path, env_lines=[
        f'{settings.MAX_BUDGET_ENV}="5"   # per-drive cap',
    ])
    s = settings.Settings(team_root=tmp_path, env={})
    assert s.number(settings.MAX_BUDGET_ENV) == 5.0


def test_env_unterminated_quote_is_returned_literally(tmp_path):
    """Malformed input: we do not guess where the value was meant to end."""
    _make_team(tmp_path, env_lines=[f'{settings.NOTIFY_CHANNEL_ENV}="C0AB'])
    s = settings.Settings(team_root=tmp_path, env={})
    assert s.get(settings.NOTIFY_CHANNEL_ENV) == '"C0AB'


def test_env_empty_value_reads_as_unset(tmp_path):
    """A commented-out knob left as a bare `KEY=` must fall through to the
    default, not force an empty string."""
    _make_team(tmp_path, env_lines=[f"{settings.MAX_BUDGET_ENV}="])
    s = settings.Settings(team_root=tmp_path, env={})
    assert s.number(settings.MAX_BUDGET_ENV) is None


# ==========================================================================
# settings: notify-channel resolution (inherits SLACK_NOTIFY_CHANNEL)
# ==========================================================================

def test_notify_channel_unset_is_the_operator_dm(tmp_path):
    _make_team(tmp_path)
    s = settings.Settings(team_root=tmp_path, env={})
    assert s.notify_channel("") is None


def test_notify_channel_reads_the_autodrive_key(tmp_path):
    _make_team(tmp_path, env_lines=[f"{settings.NOTIFY_CHANNEL_ENV}=C0OWN"])
    s = settings.Settings(team_root=tmp_path, env={})
    assert s.notify_channel("") == "C0OWN"


def test_notify_channel_falls_back_to_the_team_wide_key(tmp_path):
    """The fix: a team that declared its ops channel once, under the
    well-known name, gets daemon notifications there without also declaring
    an autodrive-only alias for the same id."""
    _make_team(
        tmp_path, env_lines=[f"{settings.SLACK_NOTIFY_CHANNEL_ENV}=C0OPS"]
    )
    s = settings.Settings(team_root=tmp_path, env={})
    assert s.notify_channel("") == "C0OPS"


def test_notify_channel_autodrive_key_beats_the_team_wide_key(tmp_path):
    """Both set: the specific key wins, so a team can send noisy per-fire
    heartbeats somewhere other than its general notification channel."""
    _make_team(tmp_path, env_lines=[
        f"{settings.NOTIFY_CHANNEL_ENV}=C0OWN",
        f"{settings.SLACK_NOTIFY_CHANNEL_ENV}=C0OPS",
    ])
    s = settings.Settings(team_root=tmp_path, env={})
    assert s.notify_channel("") == "C0OWN"


def test_notify_channel_flag_beats_every_key(tmp_path):
    _make_team(tmp_path, env_lines=[
        f"{settings.NOTIFY_CHANNEL_ENV}=C0OWN",
        f"{settings.SLACK_NOTIFY_CHANNEL_ENV}=C0OPS",
    ])
    s = settings.Settings(team_root=tmp_path, env={})
    assert s.notify_channel("C0FLAG") == "C0FLAG"


@pytest.mark.parametrize("sentinel", ["dm", "DM", " Dm "])
def test_notify_channel_flag_sentinel_forces_the_dm(tmp_path, sentinel):
    _make_team(
        tmp_path, env_lines=[f"{settings.SLACK_NOTIFY_CHANNEL_ENV}=C0OPS"]
    )
    s = settings.Settings(team_root=tmp_path, env={})
    assert s.notify_channel(sentinel) is None


def test_notify_channel_autodrive_key_sentinel_opts_out_of_inheriting(
    tmp_path,
):
    """The escape hatch that makes inheritance safe. Blanking the autodrive
    key reads as *unset* and would fall through to the team-wide one again,
    so "I want the DM" needs a value it can actually say -- otherwise a team
    whose bot was never invited to SLACK_NOTIFY_CHANNEL loses every daemon
    notification to `channel_not_found` with no way back."""
    _make_team(tmp_path, env_lines=[
        f"{settings.NOTIFY_CHANNEL_ENV}=dm",
        f"{settings.SLACK_NOTIFY_CHANNEL_ENV}=C0OPS",
    ])
    s = settings.Settings(team_root=tmp_path, env={})
    assert s.notify_channel("") is None


def test_notify_channel_team_wide_sentinel_also_means_dm(tmp_path):
    """Accepted at every layer, so the sentinel means one thing everywhere
    rather than being a special case of the autodrive key."""
    _make_team(
        tmp_path, env_lines=[f"{settings.SLACK_NOTIFY_CHANNEL_ENV}=dm"]
    )
    s = settings.Settings(team_root=tmp_path, env={})
    assert s.notify_channel("") is None


def test_notify_channel_process_env_beats_the_team_file(tmp_path):
    _make_team(
        tmp_path, env_lines=[f"{settings.SLACK_NOTIFY_CHANNEL_ENV}=C0FILE"]
    )
    s = settings.Settings(
        team_root=tmp_path,
        env={settings.SLACK_NOTIFY_CHANNEL_ENV: "C0PROC"},
    )
    assert s.notify_channel("") == "C0PROC"


def test_notify_channel_flag_defaults_to_none(tmp_path):
    """`ensure_running` passes `""`; a hand-built Namespace may pass `None`.
    Both mean "no flag", not "empty channel"."""
    _make_team(
        tmp_path, env_lines=[f"{settings.SLACK_NOTIFY_CHANNEL_ENV}=C0OPS"]
    )
    s = settings.Settings(team_root=tmp_path, env={})
    assert s.notify_channel(None) == "C0OPS"
    assert s.notify_channel() == "C0OPS"


# ==========================================================================
# cli: the daemon must not inherit the Slack turn that started it
# ==========================================================================

def test_daemon_env_drops_turn_scoped_markers_and_pins_the_journal(tmp_path):
    env = cli.daemon_env(
        {
            "PATH": "/usr/bin",
            "SLACK_BOT_TOKEN": "xoxb-keep",
            "TIGERHARNESS_SLACK_THREAD_TS": "1786545466.429719",
            "TIGERHARNESS_SLACK_CHANNEL": "D0B4L5V7RFG",
        },
        tmp_path / "journal",
    )
    assert "TIGERHARNESS_SLACK_THREAD_TS" not in env
    assert "TIGERHARNESS_SLACK_CHANNEL" not in env
    # Credentials stay: the daemon posts its own heartbeats.
    assert env["SLACK_BOT_TOKEN"] == "xoxb-keep"
    assert env["PATH"] == "/usr/bin"
    assert env["TIGERHARNESS_JOURNAL_DIR"] == str(tmp_path / "journal")


def test_daemon_env_does_not_mutate_the_caller_env(tmp_path):
    base = {"TIGERHARNESS_SLACK_THREAD_TS": "1.1"}
    cli.daemon_env(base, tmp_path / "journal")
    assert base == {"TIGERHARNESS_SLACK_THREAD_TS": "1.1"}


def test_start_scrubs_the_slack_turn_from_the_spawned_daemon(
    tmp_path, monkeypatch
):
    """Auto-start's normal trigger is `journal defer`, which runs *inside* a
    Slack turn. Unscrubbed, the daemon -- and every drive it ever spawns --
    would look like that one thread to the claim gate, to `journal defer`'s
    origin sidecar, and to drive-transcript suppression."""
    _make_team(tmp_path)
    jr = tmp_path / "journal"
    monkeypatch.setenv("TIGERHARNESS_SLACK_THREAD_TS", "1786545466.429719")
    monkeypatch.setenv("TIGERHARNESS_SLACK_CHANNEL", "D0B4L5V7RFG")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-keep")
    seen = {}

    def fake_spawn(state_file, *, cwd, log_file, env):
        seen["env"] = env
        return 1

    rc = cli.cmd_start(
        _args(["start", "--journal-dir", str(jr)]),
        spawn=fake_spawn, now=lambda: "T",
    )
    assert rc == 0
    assert "TIGERHARNESS_SLACK_THREAD_TS" not in seen["env"]
    assert "TIGERHARNESS_SLACK_CHANNEL" not in seen["env"]
    assert seen["env"]["SLACK_BOT_TOKEN"] == "xoxb-keep"
    assert seen["env"]["TIGERHARNESS_JOURNAL_DIR"] == str(jr)


# ==========================================================================
# the drained-exit veto: no lost wakeups
# ==========================================================================

@pytest.mark.asyncio
async def test_run_loop_exits_when_the_veto_agrees(tmp_path):
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 1})
    notifier = _RecordingNotifier()
    calls = []

    async def fake_drive(cfg, *, backend=None):
        return _FakeResult()

    async def fake_sleep(secs):
        return None

    def confirm():
        calls.append(1)
        return True

    n = await runner.run_loop(
        _cfg(), p,
        run_drive=fake_drive, sleep=fake_sleep, now=lambda: "T",
        probe=lambda cfg: runner.QUEUE_IDLE,
        confirm_exit=confirm, notifier=notifier, max_ticks=10,
    )
    assert n == 1                      # the one maintenance drive
    assert calls == [1]
    assert any("queue drained" in h for h in notifier.heartbeats)


@pytest.mark.asyncio
async def test_run_loop_stays_up_when_the_veto_refuses(tmp_path):
    """The lost-wakeup case. A scheduler that queued work in the gap saw our
    live pid and stood down, so exiting would strand its task: the veto keeps
    us alive and the next cycle fires on that work."""
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 1})
    asked = []

    async def fake_drive(cfg, *, backend=None):
        return _FakeResult()

    async def fake_sleep(secs):
        return None

    def confirm():
        # Refuse once (work arrived), then agree.
        asked.append(len(asked))
        return len(asked) > 1

    n = await runner.run_loop(
        _cfg(), p,
        run_drive=fake_drive, sleep=fake_sleep, now=lambda: "T",
        probe=lambda cfg: runner.QUEUE_IDLE,
        confirm_exit=confirm, max_ticks=20,
    )
    # Fire #1 was the first maintenance drive; the veto forced fire #2 rather
    # than an exit. Without the veto this is 1 and the queued task is orphaned.
    assert n == 2
    assert len(asked) == 2


@pytest.mark.asyncio
async def test_a_refused_veto_never_spins(tmp_path):
    """The veto is the one path that ``continue``s without sleeping, so a
    daemon whose exit keeps being vetoed must not busy-loop -- every cycle runs
    a full journal sweep, so a spin burns CPU *and* writes to the journal as
    fast as it can.

    It cannot, and the reason is structural rather than lucky: re-arming
    ``maintenance_done`` requires a maintenance drive to be launched and
    completed, and every launch is followed by ``sleep(interval)``. So a sleep
    is necessarily interposed between any two vetoes. This test pins that,
    because the invariant is invisible at the call site -- deleting the
    trailing sleep, or arming the latch from somewhere else, would turn it
    into a hot loop silently.
    """
    p = tmp_path / "s.json"
    runner.write_state(p, {"pid": 1})
    events: list[str] = []

    async def fake_drive(cfg, *, backend=None):
        return _FakeResult()

    async def fake_sleep(secs):
        events.append("sleep")

    def confirm():
        events.append("veto")
        return False          # never let it exit

    await runner.run_loop(
        _cfg(), p,
        run_drive=fake_drive, sleep=fake_sleep, now=lambda: "T",
        probe=lambda cfg: runner.QUEUE_IDLE,
        confirm_exit=confirm, max_ticks=6,
    )

    # The veto really did fire repeatedly (otherwise this proves nothing)...
    assert events.count("veto") >= 2, events
    # ...and no two vetoes are adjacent: something slept in between.
    vetoes = [i for i, e in enumerate(events) if e == "veto"]
    assert all(b - a > 1 for a, b in zip(vetoes, vetoes[1:])), events


def test_cmd_loop_veto_refuses_while_the_journal_still_has_work(tmp_path):
    _make_team(tmp_path)
    jr = tmp_path / "journal"
    paths = _make_journal(jr)
    _write_task(paths, "20260812-work", state=State.PENDING)
    sfile = runner.state_path(jr)
    runner.write_state(sfile, {
        "interval_seconds": 600, "prompt": "go", "journal_root": str(jr),
    })
    seen = {}

    async def fake_runner(cfg, state_file, *, should_stop, notifier,
                          confirm_exit=None):
        seen["confirmed"] = confirm_exit()
        # A refused veto must leave the daemon's state file in place -- it is
        # staying up, and a scheduler asking "is one running?" must say yes.
        seen["kept"] = state_file.is_file()
        return 0

    cli.cmd_loop(_args(["_loop", "--state-file", str(sfile)]),
                 runner=fake_runner)
    assert seen["confirmed"] is False
    assert seen["kept"] is True


def test_cmd_loop_veto_agrees_on_an_empty_journal_and_surrenders(tmp_path):
    _make_team(tmp_path)
    jr = tmp_path / "journal"
    _make_journal(jr)
    sfile = runner.state_path(jr)
    runner.write_state(sfile, {
        "interval_seconds": 600, "prompt": "go", "journal_root": str(jr),
    })
    seen = {}

    async def fake_runner(cfg, state_file, *, should_stop, notifier,
                          confirm_exit=None):
        seen["confirmed"] = confirm_exit()
        seen["gone_at_confirm"] = not state_file.exists()
        return 0

    cli.cmd_loop(_args(["_loop", "--state-file", str(sfile)]),
                 runner=fake_runner)
    assert seen["confirmed"] is True
    # The handover is the *removal*, and it happens inside the veto's lock --
    # not after the loop returns.
    assert seen["gone_at_confirm"] is True


def test_cmd_loop_does_not_delete_a_successor_daemons_state(tmp_path):
    """The hazard the veto introduces: once we surrender the state file, a
    scheduler may start a fresh daemon that writes its own. The trailing
    clear must not reach across and delete it."""
    _make_team(tmp_path)
    jr = tmp_path / "journal"
    _make_journal(jr)
    sfile = runner.state_path(jr)
    runner.write_state(sfile, {
        "interval_seconds": 600, "prompt": "go", "journal_root": str(jr),
    })

    async def fake_runner(cfg, state_file, *, should_stop, notifier,
                          confirm_exit=None):
        assert confirm_exit() is True          # we surrender the file
        runner.write_state(state_file, {       # a successor takes over
            "interval_seconds": 600, "prompt": "go", "pid": 4242,
        })
        return 0

    cli.cmd_loop(_args(["_loop", "--state-file", str(sfile)]),
                 runner=fake_runner)
    surviving = runner.read_state(sfile)
    assert surviving is not None and surviving["pid"] == 4242


def test_cmd_loop_veto_probes_inside_the_team_lock(tmp_path, monkeypatch):
    """The veto is only worth anything if it cannot interleave with a
    scheduler's `is_running` check -- i.e. if the probe and the removal both
    happen under the same lock the scheduler takes."""
    _make_team(tmp_path)
    jr = tmp_path / "journal"
    _make_journal(jr)
    sfile = runner.state_path(jr)
    runner.write_state(sfile, {
        "interval_seconds": 600, "prompt": "go", "journal_root": str(jr),
    })
    order: list[str] = []
    real_lock = cli.start_lock

    @contextlib.contextmanager
    def spy_lock(root):
        order.append("lock")
        with real_lock(root):
            yield
        order.append("unlock")

    def spy_probe(cfg):
        order.append("probe")
        return runner.QUEUE_IDLE

    monkeypatch.setattr(cli, "start_lock", spy_lock)
    monkeypatch.setattr(cli, "probe_queue", spy_probe)

    async def fake_runner(cfg, state_file, *, should_stop, notifier,
                          confirm_exit=None):
        confirm_exit()
        return 0

    cli.cmd_loop(_args(["_loop", "--state-file", str(sfile)]),
                 runner=fake_runner)
    assert order == ["lock", "probe", "unlock"]
    assert not sfile.exists()

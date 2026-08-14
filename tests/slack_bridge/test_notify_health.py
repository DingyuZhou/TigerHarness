"""Regression tests for the notify transport-health counter.

The loudness mechanism for the TLS incident: the notifier is the one
subsystem whose failure cannot announce itself, so it records consecutive
transport failures to a JSON sidecar and ``autodrive status`` prints them
back -- where the operator already looks when he asks "why have I seen no
heartbeats?".

The writer lives in ``slack_bridge`` and the reader in ``autodrive``, and
``slack_bridge`` must not import ``autodrive``; the data travels
writer -> file -> reader. The tests that matter most here are therefore
the ones that pin *both halves to the same resolved root* in a single
test -- two tests that each pass against their own idea of where the file
lives would agree with each other and still not deliver a byte.
"""
from __future__ import annotations

import json
import os
import stat

import pytest

from tigerharness.autodrive import cli, runner
from tigerharness.slack_bridge import notify_health


def _args(argv):
    return cli.build_parser().parse_args(argv)


@pytest.fixture
def team_journal(tmp_path, monkeypatch):
    """A team root whose cwd-based resolution and the writer's own
    ``default_journal_root()`` land on the same directory."""
    team = tmp_path / "team"
    (team / "configs").mkdir(parents=True)
    (team / "configs" / "personas.yaml").write_text(
        "default_persona: Anzai\n", encoding="utf-8"
    )
    journal = team / "journal"
    journal.mkdir()
    monkeypatch.chdir(team)
    monkeypatch.delenv("TIGERHARNESS_JOURNAL_DIR", raising=False)
    return journal


# ---------------------------------------------------------------------------
# Writer -> reader, end to end, at ONE resolved root
# ---------------------------------------------------------------------------

def test_writer_and_reader_meet_at_the_same_root(team_journal, capsys):
    """The load-bearing test: a real ``record_transport`` failure written
    through ``default_journal_root()`` is read back by a real
    ``autodrive status`` through ``_resolve_journal_root``. No path is
    hand-passed between the halves."""
    notify_health.record_transport(False, error="ssl: CERTIFICATE_VERIFY_FAILED")

    assert cli.cmd_status(_args(["status"])) == 0
    out = capsys.readouterr().out
    assert "notify_failures:   1" in out
    assert "notify_last_error: ssl: CERTIFICATE_VERIFY_FAILED" in out


def test_journal_dir_override_moves_the_sidecar_the_reader_reads(tmp_path, monkeypatch):
    """The reader honours ``--journal-dir`` -- and reads the *driven*
    journal, not the team-canonical lock anchor. A sidecar under the lock
    root must NOT be picked up when the override points elsewhere."""
    lock_root = tmp_path / "lock-journal"
    driven = tmp_path / "driven-journal"
    lock_root.mkdir()
    driven.mkdir()
    _write_sidecar(lock_root, count=9, error="wrong root")
    _write_sidecar(driven, count=4, error="right root")

    lines = notify_health.status_lines(
        cli._resolve_journal_root(_args(["status", "--journal-dir", str(driven)]))
    )
    assert any("notify_failures:   4" in line for line in lines)
    assert not any("wrong root" in line for line in lines)


def _write_sidecar(root, *, count: int, error: str = "", updated_at: str = "T0") -> None:
    (root / notify_health.SIDECAR_NAME).write_text(
        json.dumps(
            {
                "consecutive_failures": count,
                "last_error": error,
                "updated_at": updated_at,
            }
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# The counter itself
# ---------------------------------------------------------------------------

def test_consecutive_failures_increment(team_journal):
    for _ in range(3):
        notify_health.record_transport(False, error="boom")
    payload = json.loads(
        (team_journal / notify_health.SIDECAR_NAME).read_text(encoding="utf-8")
    )
    assert payload["consecutive_failures"] == 3
    assert payload["last_error"] == "boom"
    assert payload["updated_at"].endswith("Z")


def test_success_resets_the_counter(team_journal):
    notify_health.record_transport(False, error="boom")
    notify_health.record_transport(True)
    payload = json.loads(
        (team_journal / notify_health.SIDECAR_NAME).read_text(encoding="utf-8")
    )
    assert payload["consecutive_failures"] == 0
    assert payload["last_error"] == ""


def test_success_on_a_healthy_host_never_touches_disk(team_journal):
    """Every successful post must not rewrite a file. A notifier that
    fsyncs a sidecar on each heartbeat is a worse citizen than the bug."""
    notify_health.record_transport(True)
    assert not (team_journal / notify_health.SIDECAR_NAME).exists()
    # ...and not once the sidecar exists but already reads healthy.
    _write_sidecar(team_journal, count=0)
    before = (team_journal / notify_health.SIDECAR_NAME).read_text(encoding="utf-8")
    notify_health.record_transport(True)
    assert (team_journal / notify_health.SIDECAR_NAME).read_text(encoding="utf-8") == before


def test_sidecar_is_not_world_readable(team_journal):
    notify_health.record_transport(False, error="boom")
    mode = (team_journal / notify_health.SIDECAR_NAME).stat().st_mode
    assert not mode & (stat.S_IRWXG | stat.S_IRWXO)


def test_no_temp_files_are_left_behind(team_journal):
    for _ in range(3):
        notify_health.record_transport(False, error="boom")
    leftovers = [p.name for p in team_journal.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


# ---------------------------------------------------------------------------
# The writer never raises into the notify path
# ---------------------------------------------------------------------------

def test_missing_journal_root_is_a_silent_no_op(tmp_path, monkeypatch):
    monkeypatch.setenv("TIGERHARNESS_JOURNAL_DIR", str(tmp_path / "absent"))
    notify_health.record_transport(False, error="boom")
    assert not (tmp_path / "absent").exists()


def test_an_unwritable_root_is_logged_not_raised(team_journal, caplog, monkeypatch):
    """The whole point of the sidecar is to report a failure. If recording
    the failure could itself raise, the notifier would crash on exactly the
    host this feature exists for."""
    def _boom(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(notify_health.tempfile, "mkstemp", _boom)
    with caplog.at_level("WARNING", logger="tigerharness.slack_bridge.notify_health"):
        assert notify_health.record_transport(False, error="boom") is None
    assert "could not record transport health" in caplog.text


# ---------------------------------------------------------------------------
# The reader degrades to silence, never to noise
# ---------------------------------------------------------------------------

def test_absent_sidecar_renders_nothing(tmp_path):
    assert notify_health.status_lines(tmp_path) == []


@pytest.mark.parametrize(
    "body",
    ["", "not json at all", "[]", '{"consecutive_failures": "not-a-number"}', "{}"],
    ids=["empty", "garbage", "wrong-type", "unparseable-count", "missing-key"],
)
def test_corrupt_sidecar_renders_nothing(tmp_path, body):
    (tmp_path / notify_health.SIDECAR_NAME).write_text(body, encoding="utf-8")
    assert notify_health.status_lines(tmp_path) == []


def test_healthy_sidecar_renders_nothing(tmp_path):
    _write_sidecar(tmp_path, count=0, error="")
    assert notify_health.status_lines(tmp_path) == []


def test_failure_without_an_error_string_renders_one_line(tmp_path):
    _write_sidecar(tmp_path, count=2, error="")
    assert notify_health.status_lines(tmp_path) == [
        "  notify_failures:   2 (last T0)"
    ]


def test_missing_updated_at_renders_unknown(tmp_path):
    _write_sidecar(tmp_path, count=2, error="", updated_at="")
    assert notify_health.status_lines(tmp_path) == [
        "  notify_failures:   2 (last unknown)"
    ]


def test_corrupt_sidecar_does_not_break_status(tmp_path, capsys):
    """``status`` must keep working -- a corrupt sidecar is a bad counter,
    not a broken command."""
    journal = tmp_path / "journal"
    journal.mkdir()
    (journal / notify_health.SIDECAR_NAME).write_text("{{{", encoding="utf-8")
    assert cli.cmd_status(_args(["status", "--journal-dir", str(journal)])) == 0
    assert "stopped (no state file)" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Where it surfaces in `autodrive status`
# ---------------------------------------------------------------------------

def test_counter_prints_even_with_no_state_file(tmp_path, capsys):
    """The incident's shape exactly: the operator sees no heartbeats and
    runs ``status``. The daemon may well be stopped -- the notify counter
    is rendered before that early return on purpose, because it belongs to
    the notifier and not to daemon liveness."""
    journal = tmp_path / "journal"
    journal.mkdir()
    _write_sidecar(journal, count=37, error="ssl: CERTIFICATE_VERIFY_FAILED")
    assert cli.cmd_status(_args(["status", "--journal-dir", str(journal)])) == 0
    out = capsys.readouterr().out
    assert "stopped (no state file)" in out
    assert "notify_failures:   37 (last T0)" in out
    assert "notify_last_error: ssl: CERTIFICATE_VERIFY_FAILED" in out


def test_counter_prints_alongside_a_running_daemon(tmp_path, capsys):
    journal = tmp_path / "journal"
    runner.write_state(
        runner.state_path(journal),
        {"pid": os.getpid(), "interval_seconds": 600, "tick_count": 0},
    )
    _write_sidecar(journal, count=5, error="ssl: boom")
    assert cli.cmd_status(_args(["status", "--journal-dir", str(journal)])) == 0
    out = capsys.readouterr().out
    assert "autodrive: running" in out
    assert "notify_failures:   5" in out


def test_labels_do_not_collide_with_the_daemons_last_error(tmp_path, capsys):
    """``cmd_status`` already prints ``last_error:`` for the daemon's own
    field. The notify labels are distinct so an operator (or a grep) cannot
    read a TLS failure as a drive failure."""
    journal = tmp_path / "journal"
    runner.write_state(
        runner.state_path(journal),
        {"pid": os.getpid(), "interval_seconds": 600, "last_error": "drive blew up"},
    )
    _write_sidecar(journal, count=5, error="ssl: boom")
    cli.cmd_status(_args(["status", "--journal-dir", str(journal)]))
    out = capsys.readouterr().out
    assert "  last_error:   drive blew up" in out
    assert "  notify_last_error: ssl: boom" in out


# ---------------------------------------------------------------------------
# The seam's boundary: what counts as a transport failure
# ---------------------------------------------------------------------------

def test_an_http_error_status_is_not_a_transport_failure(team_journal):
    """``_put_bytes`` returning False on a 500 is Slack saying no, not the
    transport failing. Counting it would make the counter a general error
    tally and blunt the one signal it exists to carry."""
    from unittest.mock import MagicMock, patch

    from tigerharness.slack_bridge.notify import _put_bytes

    resp = MagicMock()
    resp.status = 500
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    with patch(
        "tigerharness.slack_bridge.notify.urllib.request.urlopen", return_value=resp
    ):
        assert _put_bytes("https://upload.url", b"data") is False
    assert not (team_journal / notify_health.SIDECAR_NAME).exists()

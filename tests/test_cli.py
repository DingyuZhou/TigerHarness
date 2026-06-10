"""Top-level CLI dispatch tests."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tigerharness.cli import main


def test_help(capsys):
    ret = main(["--help"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "task-runner" not in out
    assert "tiger-memory" in out
    assert "journal" in out


def test_unknown_command(capsys):
    ret = main(["unknown-cmd"])
    assert ret == 2


def test_empty_args(capsys):
    ret = main([])
    assert ret == 0


def test_tiger_memory_dispatch():
    # tiger-memory also requires subcommand
    with pytest.raises(SystemExit):
        main(["tiger-memory"])


def test_tiger_memory_alias():
    with pytest.raises(SystemExit):
        main(["tm"])


def test_slack_bridge_dispatch(monkeypatch):
    # sb dispatches to notify CLI which requires a subcommand
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        main(["sb"])


def test_slack_bridge_gen_service_dispatch(monkeypatch, tmp_path, capsys):
    """`tigerharness slack-bridge gen-service ...` routes to the
    gen_service subcommand (not the notify CLI)."""
    monkeypatch.chdir(tmp_path)
    from unittest.mock import patch as _patch
    with _patch(
        "tigerharness.slack_bridge.gen_service._is_linux", return_value=True,
    ):
        rc = main(["sb", "gen-service"])
    assert rc == 0
    out = capsys.readouterr().out
    # gen-service emits a systemd unit, not a notify error.
    assert "[Unit]" in out
    assert "tigerharness.slack_bridge" in out


def test_help_alias(capsys):
    ret = main(["help"])
    assert ret == 0


def test_journal_dispatch():
    """`tigerharness journal` dispatches to the journal sub-CLI which
    requires a subcommand."""
    with pytest.raises(SystemExit):
        main(["journal"])


def test_journal_alias():
    with pytest.raises(SystemExit):
        main(["j"])

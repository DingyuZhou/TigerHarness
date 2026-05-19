"""Tests for ``tigerharness slack-bridge gen-service``.

Generates a systemd user unit for the multi-team slack-bridge, with
paths baked in so the user doesn't have to edit ``%h`` specifiers.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tigerharness.slack_bridge.gen_service import (
    _is_linux,
    main,
    render_systemd_unit,
)


class TestRenderSystemdUnit:
    def test_unit_contains_all_paths(self, tmp_path: Path):
        unit = render_systemd_unit(
            teams_root=tmp_path / "teams",
            bridges_config=tmp_path / "teams" / "slack-bridge.yaml",
            env_file=tmp_path / "teams" / "multi-bridge.env",
            venv_python=tmp_path / "teams" / ".venv" / "bin" / "python",
        )
        # All four absolute paths appear.
        assert f"WorkingDirectory={tmp_path / 'teams'}" in unit
        assert "slack-bridge.yaml" in unit
        assert "multi-bridge.env" in unit
        assert ".venv/bin/python" in unit

    def test_unit_has_required_systemd_fields(self, tmp_path: Path):
        unit = render_systemd_unit(
            teams_root=tmp_path, bridges_config=tmp_path / "x.yaml",
            env_file=tmp_path / "x.env",
            venv_python=tmp_path / ".venv" / "bin" / "python",
        )
        # Section headers
        assert "[Unit]" in unit
        assert "[Service]" in unit
        assert "[Install]" in unit
        # Drain budget must exceed _DRAIN_TIMEOUT_S (90s)
        assert "TimeoutStopSec=120" in unit
        # KillMode=mixed so claude_p children finish posting
        assert "KillMode=mixed" in unit
        # Logs go to journal
        assert "StandardOutput=journal" in unit


class TestMainCLI:
    def test_defaults_compute_sensible_paths(
        self, tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch,
    ):
        """With no flags, --teams-root defaults to . and all other
        paths derive from it."""
        monkeypatch.chdir(tmp_path)
        with patch(
            "tigerharness.slack_bridge.gen_service._is_linux",
            return_value=True,
        ):
            rc = main([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "slack-bridge.yaml" in out
        assert "multi-bridge.env" in out

    def test_custom_paths_honored(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ):
        with patch(
            "tigerharness.slack_bridge.gen_service._is_linux",
            return_value=True,
        ):
            rc = main([
                "--teams-root", str(tmp_path / "myteams"),
                "--bridges-config", str(tmp_path / "custom.yaml"),
                "--env-file", str(tmp_path / "custom.env"),
                "--venv-python", str(tmp_path / "py" / "python"),
            ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "myteams" in out
        assert "custom.yaml" in out
        assert "custom.env" in out
        # Custom python path
        assert str(tmp_path / "py" / "python") in out

    def test_non_linux_returns_1_with_friendly_message(
        self, capsys: pytest.CaptureFixture,
    ):
        with patch(
            "tigerharness.slack_bridge.gen_service._is_linux",
            return_value=False,
        ):
            rc = main([])
        assert rc == 1
        err = capsys.readouterr().err
        # Friendly fallback message tells the user what to run manually.
        assert "systemd" in err.lower() or "systemd unit" in err.lower()
        assert "tigerharness.slack_bridge" in err


class TestIsLinux:
    def test_current_platform(self):
        """Smoke test only -- the helper just wraps sys.platform."""
        assert _is_linux() == sys.platform.startswith("linux")

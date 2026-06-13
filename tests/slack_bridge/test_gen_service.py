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
    derive_unit_name,
    main,
    render_systemd_unit,
)


class TestDeriveUnitName:
    def test_per_root_name_is_deterministic_and_distinct(
        self, tmp_path: Path,
    ):
        a = tmp_path / "teams"
        b = tmp_path / "my-teams"
        a.mkdir(); b.mkdir()
        name_a = derive_unit_name(a)
        assert name_a == derive_unit_name(a)  # stable across calls
        assert name_a != derive_unit_name(b)  # roots never collide
        assert name_a.startswith("slack-bridge-teams-")
        assert name_a.endswith(".service")
        # The vestigial ``multi-`` infix is gone: the name reads as
        # slack-bridge-<root>-<hash>, not slack-bridge-multi-<root>-<hash>.
        assert "slack-bridge-multi-" not in name_a

    def test_same_basename_different_roots_do_not_collide(
        self, tmp_path: Path,
    ):
        a = tmp_path / "one" / "teams"
        b = tmp_path / "two" / "teams"
        a.mkdir(parents=True); b.mkdir(parents=True)
        assert derive_unit_name(a) != derive_unit_name(b)

    def test_weird_basename_is_sanitized(self, tmp_path: Path):
        root = tmp_path / "my teams!?"
        root.mkdir()
        name = derive_unit_name(root)
        # systemd-safe: no spaces or shell-hostile chars in the name.
        assert " " not in name and "!" not in name and "?" not in name

    def test_basename_sanitising_to_empty_falls_back_to_root(
        self, tmp_path: Path,
    ):
        """A basename made entirely of disallowed chars (or non-ASCII
        that the regex strips) sanitises to '' -> the `or "root"`
        fallback keeps the unit name well-formed and still hashed."""
        special = tmp_path / "!!!"
        special.mkdir()
        name = derive_unit_name(special)
        assert name.startswith("slack-bridge-root-")
        assert name.endswith(".service")
        # The digest still disambiguates two empty-sanitising roots.
        other = tmp_path / "sub" / "???"
        other.mkdir(parents=True)
        assert derive_unit_name(special) != derive_unit_name(other)

    def test_basename_literally_named_multi_is_hashed_not_confused(
        self, tmp_path: Path,
    ):
        """A root whose basename starts with 'multi' yields a name of the
        same SHAPE as the legacy 'slack-bridge-multi-<root>-<hash>' scheme.
        That is cosmetic only: nothing parses the basename out of the unit
        name (dismiss discovers by content), and the per-path hash keeps it
        distinct. Lock the shape so the coincidence stays understood."""
        root = tmp_path / "multi-teams"
        root.mkdir()
        name = derive_unit_name(root)
        assert name.startswith("slack-bridge-multi-teams-")
        # Distinct from a DIFFERENT root that shares the basename shape.
        other = tmp_path / "sub" / "multi-teams"
        other.mkdir(parents=True)
        assert derive_unit_name(root) != derive_unit_name(other)


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

    def test_prints_per_root_install_instructions_on_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ):
        """stdout stays clean unit content; the per-root unit name and
        enable commands go to stderr so redirects don't swallow them."""
        root = tmp_path / "teams"
        root.mkdir()
        with patch(
            "tigerharness.slack_bridge.gen_service._is_linux",
            return_value=True,
        ):
            rc = main(["--teams-root", str(root)])
        assert rc == 0
        captured = capsys.readouterr()
        expected = derive_unit_name(root)
        assert expected in captured.err
        assert "enable --now" in captured.err
        # The unit name never leaks into the unit content on stdout
        # except as the journalctl hint comment.
        assert f"Save as" not in captured.out
        assert expected.removesuffix(".service") in captured.out

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

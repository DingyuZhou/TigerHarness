"""Tests for tigerharness.tiger_memory.cli module."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tigerharness.tiger_memory.cli import main


class TestCliArgParsing:
    """Test that subcommands are correctly routed."""

    def test_no_args_exits(self):
        with pytest.raises(SystemExit) as exc_info:
            main([])
        assert exc_info.value.code == 2

    def test_unknown_command_exits(self):
        with pytest.raises(SystemExit):
            main(["--config", "/dev/null", "bogus"])

    def test_init_command(self, minimal_config_yaml: Path, tmp_path: Path):
        ret = main(["--config", str(minimal_config_yaml), "init"])
        assert ret == 0
        # Store should be initialized
        store_root = tmp_path / "memory"
        assert store_root.exists()

    def test_state_command(self, minimal_config_yaml: Path, tmp_path: Path):
        # Need store to exist first
        store_root = tmp_path / "memory"
        store_root.mkdir(parents=True, exist_ok=True)
        (store_root / "archive").mkdir()
        (store_root / "journal").mkdir()
        (store_root / "briefing").mkdir()

        ret = main(["--config", str(minimal_config_yaml), "state"])
        assert ret == 0

    def test_config_error_returns_2(self, tmp_path: Path):
        bad_cfg = tmp_path / "bad.yaml"
        bad_cfg.write_text("not valid config at all")
        # Provide a subcommand so argparse doesn't exit, but config is bad
        ret = main(["--config", str(bad_cfg), "init"])
        assert ret == 2


class TestCliBootstrap:
    def test_bootstrap_dispatches(self, minimal_config_yaml: Path, tmp_path: Path):
        # Init store first
        main(["--config", str(minimal_config_yaml), "init"])

        with patch("tigerharness.tiger_memory.lifecycle.bootstrap", return_value=0) as mock_bs:
            ret = main(["--config", str(minimal_config_yaml), "bootstrap"])
            assert ret == 0
            mock_bs.assert_called_once()
            _, kwargs = mock_bs.call_args
            assert kwargs["dry_run"] is False
            assert kwargs["limit"] is None

    def test_bootstrap_dry_run(self, minimal_config_yaml: Path, tmp_path: Path):
        main(["--config", str(minimal_config_yaml), "init"])

        with patch("tigerharness.tiger_memory.lifecycle.bootstrap", return_value=0) as mock_bs:
            ret = main(["--config", str(minimal_config_yaml), "bootstrap", "--dry-run", "--limit", "5"])
            assert ret == 0
            _, kwargs = mock_bs.call_args
            assert kwargs["dry_run"] is True
            assert kwargs["limit"] == 5


class TestCliRebuild:
    def test_rebuild_dispatches(self, minimal_config_yaml: Path, tmp_path: Path):
        main(["--config", str(minimal_config_yaml), "init"])

        with patch("tigerharness.tiger_memory.lifecycle.rebuild", return_value=0) as mock_rb:
            ret = main(["--config", str(minimal_config_yaml), "rebuild"])
            assert ret == 0
            mock_rb.assert_called_once()
            _, kwargs = mock_rb.call_args
            assert kwargs["background"] is False

    def test_rebuild_background(self, minimal_config_yaml: Path, tmp_path: Path):
        main(["--config", str(minimal_config_yaml), "init"])

        with patch("tigerharness.tiger_memory.lifecycle.rebuild", return_value=0) as mock_rb:
            ret = main(["--config", str(minimal_config_yaml), "rebuild", "--background"])
            assert ret == 0
            _, kwargs = mock_rb.call_args
            assert kwargs["background"] is True


class TestCliPin:
    def test_pin_dispatches(self, minimal_config_yaml: Path, tmp_path: Path):
        main(["--config", str(minimal_config_yaml), "init"])

        with patch("tigerharness.tiger_memory.must_memorize.pin", return_value=0) as mock_pin:
            ret = main(["--config", str(minimal_config_yaml), "pin", "Remember this fact"])
            assert ret == 0
            mock_pin.assert_called_once()
            _, kwargs = mock_pin.call_args
            assert kwargs["memo"] == "Remember this fact"
            assert kwargs["kind"] == "owner_explicit"

    def test_pin_with_kind(self, minimal_config_yaml: Path, tmp_path: Path):
        main(["--config", str(minimal_config_yaml), "init"])

        with patch("tigerharness.tiger_memory.must_memorize.pin", return_value=0) as mock_pin:
            ret = main(["--config", str(minimal_config_yaml), "pin", "--kind", "decision", "We chose X"])
            assert ret == 0
            _, kwargs = mock_pin.call_args
            assert kwargs["kind"] == "decision"


class TestCliResummarize:
    def test_resummarize_dispatches(self, minimal_config_yaml: Path, tmp_path: Path):
        main(["--config", str(minimal_config_yaml), "init"])

        with patch("tigerharness.tiger_memory.lifecycle.resummarize", return_value=0) as mock_rs:
            ret = main(["--config", str(minimal_config_yaml), "resummarize", "--since", "2026-05-01"])
            assert ret == 0
            mock_rs.assert_called_once()
            _, kwargs = mock_rs.call_args
            assert kwargs["since"] == "2026-05-01"
            assert kwargs["summarizer"] is None


class TestCliDrill:
    def test_drill_dispatches(self, minimal_config_yaml: Path, tmp_path: Path):
        main(["--config", str(minimal_config_yaml), "init"])

        with patch("tigerharness.tiger_memory.drill.drill", return_value=0) as mock_dr:
            ret = main(["--config", str(minimal_config_yaml), "drill", "/some/path.md"])
            assert ret == 0
            mock_dr.assert_called_once()

    def test_tree_dispatches(self, minimal_config_yaml: Path, tmp_path: Path):
        main(["--config", str(minimal_config_yaml), "init"])

        with patch("tigerharness.tiger_memory.drill.tree", return_value=0) as mock_tr:
            ret = main(["--config", str(minimal_config_yaml), "tree", "/some/path.md", "--depth", "3"])
            assert ret == 0
            mock_tr.assert_called_once()

    def test_raw_dispatches(self, minimal_config_yaml: Path, tmp_path: Path):
        main(["--config", str(minimal_config_yaml), "init"])

        with patch("tigerharness.tiger_memory.drill.raw", return_value=0) as mock_raw:
            ret = main(["--config", str(minimal_config_yaml), "raw", "/some/archive.md"])
            assert ret == 0
            mock_raw.assert_called_once()


class TestCliSearch:
    def test_search_dispatches(self, minimal_config_yaml: Path, tmp_path: Path):
        main(["--config", str(minimal_config_yaml), "init"])

        with patch("tigerharness.tiger_memory.drill.search", return_value=0) as mock_sr:
            ret = main(["--config", str(minimal_config_yaml), "search", "bitcoin strategies"])
            assert ret == 0
            mock_sr.assert_called_once()
            _, kwargs = mock_sr.call_args
            assert kwargs["topic"] == "bitcoin strategies"
            assert kwargs["mode"] == "auto"

    def test_search_with_mode(self, minimal_config_yaml: Path, tmp_path: Path):
        main(["--config", str(minimal_config_yaml), "init"])

        with patch("tigerharness.tiger_memory.drill.search", return_value=0) as mock_sr:
            ret = main(["--config", str(minimal_config_yaml), "search", "--mode", "grep", "BTC"])
            assert ret == 0
            _, kwargs = mock_sr.call_args
            assert kwargs["mode"] == "grep"

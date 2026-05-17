"""Tests for tigerharness.init + __main__ coverage."""
from __future__ import annotations

import runpy
from pathlib import Path
from unittest.mock import patch

import pytest

from tigerharness.init import _write_if_missing, init, main


class TestWriteIfMissing:
    def test_creates_file(self, tmp_path: Path):
        f = tmp_path / "sub" / "test.txt"
        assert _write_if_missing(f, "hello") is True
        assert f.read_text() == "hello"

    def test_skips_existing(self, tmp_path: Path):
        f = tmp_path / "test.txt"
        f.write_text("original")
        assert _write_if_missing(f, "replacement") is False
        assert f.read_text() == "original"


class TestInit:
    def test_creates_gitignore_persona_and_env(self, tmp_path: Path):
        created = init(name="mybot", target_dir=tmp_path)
        assert ".gitignore" in created
        assert "personas/mybot.md" in created
        assert ".env" in created
        assert (tmp_path / ".gitignore").exists()
        assert (tmp_path / "personas" / "mybot.md").exists()
        assert (tmp_path / ".env").exists()
        # .gitignore prevents committing secrets
        assert ".env" in (tmp_path / ".gitignore").read_text()
        # Check persona content
        content = (tmp_path / "personas" / "mybot.md").read_text()
        assert "You are mybot" in content

    def test_creates_memory_config(self, tmp_path: Path):
        created = init(name="mybot", target_dir=tmp_path, include_memory=True)
        assert "tiger-memory.config.yaml" in created
        cfg = (tmp_path / "tiger-memory.config.yaml").read_text()
        assert "name: mybot" in cfg
        assert "tiger-memory-mybot.lock" in cfg

    def test_skips_existing_files(self, tmp_path: Path):
        # First run creates files (including memory config)
        init(name="mybot", target_dir=tmp_path, include_memory=True)
        # Second run skips them all
        created = init(name="mybot", target_dir=tmp_path, include_memory=True)
        assert created == []

    def test_defaults(self, tmp_path: Path):
        created = init(target_dir=tmp_path)
        assert "personas/assistant.md" in created

    def test_no_memory_by_default(self, tmp_path: Path):
        created = init(target_dir=tmp_path)
        assert "tiger-memory.config.yaml" not in created
        assert not (tmp_path / "tiger-memory.config.yaml").exists()


class TestMain:
    def test_basic(self, tmp_path: Path):
        rc = main(["--dir", str(tmp_path), "--name", "scout"])
        assert rc == 0
        assert (tmp_path / "personas" / "scout.md").exists()
        assert (tmp_path / ".env").exists()

    def test_with_memory(self, tmp_path: Path):
        rc = main(["--dir", str(tmp_path), "--name", "scout", "--memory"])
        assert rc == 0
        assert (tmp_path / "tiger-memory.config.yaml").exists()

    def test_nothing_to_do(self, tmp_path: Path):
        main(["--dir", str(tmp_path)])
        rc = main(["--dir", str(tmp_path)])
        assert rc == 0  # "Nothing to do" path

    def test_via_top_level_cli(self, tmp_path: Path):
        from tigerharness.cli import main as cli_main
        rc = cli_main(["init", "--dir", str(tmp_path), "--name", "test"])
        assert rc == 0
        assert (tmp_path / "personas" / "test.md").exists()


class TestDunderMain:
    """Cover __main__.py modules via runpy."""

    def test_task_runner_main(self):
        """Cover task_runner/__main__.py lines 3-7."""
        with patch("tigerharness.task_runner.cli.main", return_value=0):
            with pytest.raises(SystemExit) as exc_info:
                runpy.run_module(
                    "tigerharness.task_runner",
                    run_name="__main__",
                    alter_sys=True,
                )
            assert exc_info.value.code == 0

    def test_task_runner_main_error(self):
        """Cover task_runner/__main__.py with non-zero exit."""
        with patch("tigerharness.task_runner.cli.main", return_value=2):
            with pytest.raises(SystemExit) as exc_info:
                runpy.run_module(
                    "tigerharness.task_runner",
                    run_name="__main__",
                    alter_sys=True,
                )
            assert exc_info.value.code == 2

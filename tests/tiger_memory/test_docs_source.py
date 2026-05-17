"""Tests for DocsAdapter source."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tigerharness.tiger_memory.sources.docs import DocsAdapter, _git_commit_dates


class TestDocsAdapter:
    def test_discover_finds_md_files(self, tmp_path: Path):
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "design.md").write_text("# Design\nContent.")
        (tmp_path / "docs" / "readme.md").write_text("# Readme\nInfo.")
        (tmp_path / "docs" / "image.png").write_bytes(b"\x89PNG")

        adapter = DocsAdapter(glob_pattern="docs/*.md", repo_root=tmp_path)
        records = list(adapter.discover())
        assert len(records) == 2
        assert all(r.source == "doc" for r in records)
        names = {r.source_id for r in records}
        assert "docs/design.md" in names
        assert "docs/readme.md" in names

    def test_discover_skips_dirs(self, tmp_path: Path):
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "subdir").mkdir()
        adapter = DocsAdapter(glob_pattern="docs/*", repo_root=tmp_path)
        records = list(adapter.discover())
        assert len(records) == 0

    def test_discover_handles_unreadable(self, tmp_path: Path):
        (tmp_path / "docs").mkdir()
        f = tmp_path / "docs" / "binary.md"
        # Write binary content that will cause UnicodeDecodeError
        f.write_bytes(b"\xff\xfe" + b"\x00" * 100)
        adapter = DocsAdapter(glob_pattern="docs/*.md", repo_root=tmp_path)
        records = list(adapter.discover())
        # Should skip the unreadable file
        assert len(records) == 0

    def test_record_has_uuid(self, tmp_path: Path):
        (tmp_path / "test.md").write_text("content")
        adapter = DocsAdapter(glob_pattern="test.md", repo_root=tmp_path)
        records = list(adapter.discover())
        assert len(records) == 1
        assert records[0].conversation_uuid  # non-empty UUID

    def test_uuid_deterministic(self, tmp_path: Path):
        (tmp_path / "test.md").write_text("content")
        adapter = DocsAdapter(glob_pattern="test.md", repo_root=tmp_path)
        r1 = list(adapter.discover())
        r2 = list(adapter.discover())
        assert r1[0].conversation_uuid == r2[0].conversation_uuid


class TestGitCommitDates:
    def test_file_not_in_git(self, tmp_path: Path):
        f = tmp_path / "test.md"
        f.write_text("hello")
        first, last = _git_commit_dates(f, tmp_path)
        assert first is None
        assert last is None

    def test_git_not_available(self, tmp_path: Path):
        f = tmp_path / "test.md"
        f.write_text("hello")
        with patch("tigerharness.tiger_memory.sources.docs.subprocess.run",
                    side_effect=FileNotFoundError("no git")):
            first, last = _git_commit_dates(f, tmp_path)
            assert first is None
            assert last is None

    def test_git_timeout(self, tmp_path: Path):
        import subprocess as sp
        f = tmp_path / "test.md"
        f.write_text("hello")
        with patch("tigerharness.tiger_memory.sources.docs.subprocess.run",
                    side_effect=sp.TimeoutExpired("git", 10)):
            first, last = _git_commit_dates(f, tmp_path)
            assert first is None
            assert last is None

    def test_git_tracked_file(self, tmp_path: Path):
        """Use a real git repo to test the success path."""
        import subprocess as sp
        sp.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
        sp.run(["git", "-C", str(tmp_path), "config", "user.email", "test@test.com"],
               capture_output=True, check=True)
        sp.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"],
               capture_output=True, check=True)
        f = tmp_path / "test.md"
        f.write_text("hello")
        sp.run(["git", "-C", str(tmp_path), "add", "test.md"], capture_output=True, check=True)
        sp.run(["git", "-C", str(tmp_path), "commit", "-m", "initial"],
               capture_output=True, check=True)
        first, last = _git_commit_dates(f, tmp_path)
        assert first is not None
        assert last is not None
        assert first == last  # single commit

    def test_git_nonzero_return(self, tmp_path: Path):
        """git log returns non-zero for untracked file in a repo."""
        import subprocess as sp
        mock_result = MagicMock()
        mock_result.returncode = 128
        mock_result.stdout = ""
        with patch("tigerharness.tiger_memory.sources.docs.subprocess.run",
                    return_value=mock_result):
            first, last = _git_commit_dates(tmp_path / "x.md", tmp_path)
            assert first is None

    def test_git_bad_date_format(self, tmp_path: Path):
        """git log returns unparseable dates."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "not-a-date\n"
        with patch("tigerharness.tiger_memory.sources.docs.subprocess.run",
                    return_value=mock_result):
            first, last = _git_commit_dates(tmp_path / "x.md", tmp_path)
            assert first is None

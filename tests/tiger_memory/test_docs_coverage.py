"""Coverage-push tests for docs.py — targeting lines 48-49, 54->59, 91, 96."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tigerharness.tiger_memory.sources.docs import DocsAdapter, _git_commit_dates


class TestRecordForRelativeToValueError:
    """Lines 48-49: path.relative_to raises ValueError → uses full path."""

    def test_relative_to_fails(self, tmp_path: Path):
        # doc in /tmp/other, repo_root in /tmp/repo → relative_to raises
        repo = tmp_path / "repo"
        repo.mkdir()
        adapter = DocsAdapter(glob_pattern="*.md", repo_root=repo)

        doc = tmp_path / "outside.md"
        doc.write_text("content")

        rec = adapter._record_for(doc)
        assert rec is not None
        # source_id should use the full path (not relative)
        assert "outside.md" in rec.source_id


class TestRecordForNotInGit:
    """Lines 54->59: file not in git → falls back to mtime."""

    def test_not_in_git_uses_mtime(self, tmp_path: Path):
        adapter = DocsAdapter(glob_pattern="*.md", repo_root=tmp_path)
        doc = tmp_path / "untracked.md"
        doc.write_text("new doc")

        # Mock git to return empty (no commits)
        with patch("tigerharness.tiger_memory.sources.docs._git_commit_dates",
                   return_value=(None, None)):
            rec = adapter._record_for(doc)

        assert rec is not None
        assert rec.first_event_at is not None


class TestGitCommitDatesValueError:
    """Line 96: invalid ISO date from git → returns None, None."""

    def test_invalid_date(self, tmp_path: Path):
        doc = tmp_path / "test.md"
        doc.write_text("content")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "not-a-date\n"

        with patch("subprocess.run", return_value=mock_result):
            first, last = _git_commit_dates(doc, tmp_path)

        assert first is None
        assert last is None


class TestGitCommitDatesEmptyOutput:
    """Line 91: git log returns empty output."""

    def test_empty_git_output(self, tmp_path: Path):
        doc = tmp_path / "test.md"
        doc.write_text("content")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""

        with patch("subprocess.run", return_value=mock_result):
            first, last = _git_commit_dates(doc, tmp_path)

        assert first is None
        assert last is None

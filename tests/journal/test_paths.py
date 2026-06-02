"""Tests for ``tigerharness.journal.paths``: JournalPaths + resolver."""

from __future__ import annotations

from pathlib import Path

import pytest

from tigerharness.journal import paths as paths_mod
from tigerharness.journal.paths import (
    JournalPathError,
    JournalPaths,
    default_journal_root,
)


# ---------------------------------------------------------------------------
# default_journal_root resolution
# ---------------------------------------------------------------------------

class TestDefaultJournalRoot:
    def test_env_override_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TIGERHARNESS_JOURNAL_DIR", str(tmp_path))
        monkeypatch.setenv("XDG_STATE_HOME", "/tmp/should-be-ignored")
        assert default_journal_root() == tmp_path

    def test_blank_env_falls_through(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TIGERHARNESS_JOURNAL_DIR", "   ")
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        # Not a team dir (no configs/personas.yaml).
        monkeypatch.chdir(tmp_path)
        assert default_journal_root() == tmp_path / "tigerharness-journal"

    def test_team_dir_takes_precedence_over_xdg(
        self, monkeypatch, tmp_path,
    ):
        team = tmp_path / "team"
        (team / "configs").mkdir(parents=True)
        (team / "configs" / "personas.yaml").write_text("personas: []\n")
        xdg = tmp_path / "xdg"
        monkeypatch.delenv("TIGERHARNESS_JOURNAL_DIR", raising=False)
        monkeypatch.setenv("XDG_STATE_HOME", str(xdg))
        monkeypatch.chdir(team)
        assert default_journal_root() == team / "journal"

    def test_falls_back_to_xdg(self, monkeypatch, tmp_path):
        monkeypatch.delenv("TIGERHARNESS_JOURNAL_DIR", raising=False)
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        non_team = tmp_path / "not-team"
        non_team.mkdir()
        monkeypatch.chdir(non_team)
        assert default_journal_root() == tmp_path / "tigerharness-journal"

    def test_falls_back_to_home(self, monkeypatch, tmp_path):
        monkeypatch.delenv("TIGERHARNESS_JOURNAL_DIR", raising=False)
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        non_team = tmp_path / "not-team"
        non_team.mkdir()
        monkeypatch.chdir(non_team)
        assert default_journal_root() == (
            tmp_path / ".local" / "state" / "tigerharness-journal"
        )

    def test_is_team_dir_swallows_oserror(self, monkeypatch, tmp_path):
        """If ``Path.is_file`` raises on an exotic filesystem, the team
        check returns False rather than crashing the resolver."""
        def _boom(self):
            raise OSError("simulated permission denied")
        monkeypatch.setattr(Path, "is_file", _boom)
        assert paths_mod._is_team_dir(tmp_path) is False


# ---------------------------------------------------------------------------
# JournalPaths layout + per-task accessors
# ---------------------------------------------------------------------------

class TestJournalPathsLayout:
    def test_top_level(self, tmp_path):
        p = JournalPaths(root=tmp_path)
        assert p.active == tmp_path / "active"
        assert p.done == tmp_path / "done"
        assert p.operating_md == tmp_path / "OPERATING.md"

    def test_per_task_active(self, tmp_path):
        p = JournalPaths(root=tmp_path)
        tid = "20260602-foo-12345678"
        assert p.task_dir(tid) == tmp_path / "active" / tid
        assert p.status_json(tid) == tmp_path / "active" / tid / "status.json"
        assert p.task_md(tid) == tmp_path / "active" / tid / "task.md"
        assert p.progress_md(tid) == tmp_path / "active" / tid / "progress.md"
        assert p.artifacts(tid) == tmp_path / "active" / tid / "artifacts"

    def test_per_task_archived(self, tmp_path):
        p = JournalPaths(root=tmp_path)
        tid = "20260602-foo-12345678"
        assert p.task_dir(tid, archived=True) == tmp_path / "done" / tid
        assert p.status_json(tid, archived=True) == (
            tmp_path / "done" / tid / "status.json"
        )

    @pytest.mark.parametrize("bad", ["..", "foo/bar", "", ".hidden"])
    def test_per_task_rejects_unsafe_id(self, tmp_path, bad):
        p = JournalPaths(root=tmp_path)
        with pytest.raises(JournalPathError):
            p.task_dir(bad)
        with pytest.raises(JournalPathError):
            p.status_json(bad)

    def test_ensure_is_idempotent(self, tmp_path):
        p = JournalPaths(root=tmp_path / "j")
        out = p.ensure()
        assert out is p
        assert (tmp_path / "j" / "active").is_dir()
        assert (tmp_path / "j" / "done").is_dir()
        # Re-running is a no-op.
        p.ensure()


# ---------------------------------------------------------------------------
# task_exists + list_active_ids
# ---------------------------------------------------------------------------

class TestExistenceAndListing:
    def test_task_exists_false_when_missing(self, tmp_path):
        p = JournalPaths(root=tmp_path).ensure()
        assert p.task_exists("nope") is False

    def test_task_exists_true_when_status_present(self, tmp_path):
        p = JournalPaths(root=tmp_path).ensure()
        tid = "20260602-x-12345678"
        (p.active / tid).mkdir()
        (p.active / tid / "status.json").write_text("{}")
        assert p.task_exists(tid) is True

    def test_task_exists_false_on_unsafe_id(self, tmp_path):
        p = JournalPaths(root=tmp_path).ensure()
        assert p.task_exists("..") is False

    def test_list_returns_sorted_safe_ids_only(self, tmp_path):
        p = JournalPaths(root=tmp_path).ensure()
        # Three valid task dirs + one stray file + one hidden + one
        # missing-status case.
        valid = ["20260602-a-11111111", "20260602-b-22222222"]
        for tid in valid:
            (p.active / tid).mkdir()
            (p.active / tid / "status.json").write_text("{}")
        # Dir without status.json -- skipped.
        (p.active / "20260602-c-33333333").mkdir()
        # Hidden -- skipped by safety filter.
        (p.active / ".hidden").mkdir()
        # Regular file (not a dir) -- skipped.
        (p.active / "stray.txt").write_text("hi")
        assert p.list_active_ids() == sorted(valid)

    def test_list_when_active_missing(self, tmp_path):
        p = JournalPaths(root=tmp_path)
        # No active/ on disk yet.
        assert p.list_active_ids() == []


# ---------------------------------------------------------------------------
# archive
# ---------------------------------------------------------------------------

class TestArchive:
    def test_archive_happy_path(self, tmp_path):
        p = JournalPaths(root=tmp_path).ensure()
        tid = "20260602-x-11111111"
        (p.active / tid).mkdir()
        (p.active / tid / "status.json").write_text("{}")
        new_path = p.archive(tid)
        assert new_path == p.done / tid
        assert (p.done / tid / "status.json").is_file()
        assert not (p.active / tid).exists()

    def test_archive_rejects_unsafe_id(self, tmp_path):
        p = JournalPaths(root=tmp_path).ensure()
        with pytest.raises(JournalPathError):
            p.archive("..")

    def test_archive_rejects_missing_source(self, tmp_path):
        p = JournalPaths(root=tmp_path).ensure()
        with pytest.raises(JournalPathError) as exc:
            p.archive("20260602-nope-12345678")
        assert "not in active" in str(exc.value)

    def test_archive_refuses_to_overwrite_existing_done(self, tmp_path):
        p = JournalPaths(root=tmp_path).ensure()
        tid = "20260602-x-11111111"
        (p.active / tid).mkdir()
        (p.active / tid / "status.json").write_text("{}")
        (p.done / tid).mkdir(parents=True)
        with pytest.raises(JournalPathError) as exc:
            p.archive(tid)
        assert "already exists" in str(exc.value)

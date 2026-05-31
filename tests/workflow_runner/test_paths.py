"""Unit tests for ``tigerharness.workflow_runner.paths``."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from tigerharness.workflow_runner import paths as paths_mod
from tigerharness.workflow_runner.models import WorkflowModelError
from tigerharness.workflow_runner.paths import (
    TaskPaths,
    default_journal_root,
    new_task_id,
)


# --------------------------------------------------------------------------- #
# default_journal_root resolution
# --------------------------------------------------------------------------- #


def test_default_journal_root_uses_explicit_override(monkeypatch, tmp_path):
    monkeypatch.setenv("TIGERHARNESS_WORKFLOW_JOURNAL", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/should-be-ignored")
    assert default_journal_root() == tmp_path


def test_default_journal_root_falls_back_to_xdg(monkeypatch, tmp_path):
    monkeypatch.delenv("TIGERHARNESS_WORKFLOW_JOURNAL", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert default_journal_root() == tmp_path / "tigerharness-workflows"


def test_default_journal_root_falls_back_to_home(monkeypatch, tmp_path):
    monkeypatch.delenv("TIGERHARNESS_WORKFLOW_JOURNAL", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert default_journal_root() == (
        tmp_path / ".local" / "state" / "tigerharness-workflows"
    )


def test_default_journal_root_ignores_blank_override(monkeypatch, tmp_path):
    """Empty / whitespace override must NOT win over XDG."""
    monkeypatch.setenv("TIGERHARNESS_WORKFLOW_JOURNAL", "   ")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert default_journal_root() == tmp_path / "tigerharness-workflows"


def _make_team_dir(p: Path) -> Path:
    (p / "configs").mkdir(parents=True, exist_ok=True)
    (p / "configs" / "personas.yaml").write_text("personas: []\n")
    return p


def test_default_journal_root_prefers_team_folder_when_cwd_is_team(
    monkeypatch, tmp_path
):
    """When cwd looks like a team root (configs/personas.yaml present),
    default to ``<cwd>/workflow_journal`` per the original design."""
    monkeypatch.delenv("TIGERHARNESS_WORKFLOW_JOURNAL", raising=False)
    _make_team_dir(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert default_journal_root() == tmp_path / "workflow_journal"


def test_default_journal_root_explicit_override_beats_team_folder(
    monkeypatch, tmp_path
):
    """Explicit env override still wins over the team-folder default."""
    team = tmp_path / "team"
    override = tmp_path / "override"
    _make_team_dir(team)
    monkeypatch.chdir(team)
    monkeypatch.setenv("TIGERHARNESS_WORKFLOW_JOURNAL", str(override))
    assert default_journal_root() == override


def test_default_journal_root_team_folder_beats_xdg(monkeypatch, tmp_path):
    """Team-folder default beats XDG fallback."""
    team = tmp_path / "team"
    xdg = tmp_path / "xdg"
    _make_team_dir(team)
    monkeypatch.chdir(team)
    monkeypatch.delenv("TIGERHARNESS_WORKFLOW_JOURNAL", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg))
    assert default_journal_root() == team / "workflow_journal"


def test_default_journal_root_non_team_cwd_falls_back_to_xdg(
    monkeypatch, tmp_path
):
    """A directory without configs/personas.yaml is NOT a team dir."""
    monkeypatch.delenv("TIGERHARNESS_WORKFLOW_JOURNAL", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    not_team = tmp_path / "not-team"
    not_team.mkdir()
    monkeypatch.chdir(not_team)
    assert default_journal_root() == tmp_path / "tigerharness-workflows"


def test_is_team_dir_returns_false_on_oserror(monkeypatch, tmp_path):
    """OSError from ``is_file()`` (e.g. permission denied on an odd
    filesystem) is swallowed -- the dir is treated as not-a-team."""
    def _raise(self):
        raise OSError("simulated permission denied")
    monkeypatch.setattr(Path, "is_file", _raise)
    assert paths_mod._is_team_dir(tmp_path) is False


# --------------------------------------------------------------------------- #
# new_task_id
# --------------------------------------------------------------------------- #


def test_new_task_id_shape():
    tid = new_task_id(
        "Add cache eviction",
        now=dt.datetime(2026, 5, 28, 14, 0, 0, tzinfo=dt.timezone.utc),
    )
    head, slug, suffix = tid.split("-", 2)
    # The slug itself contains hyphens; rsplit gets the trailing uuid8 part.
    slug_part, _, uuid_part = suffix.rpartition("-")
    assert head == "20260528"
    assert f"{slug}-{slug_part}" == "add-cache-eviction"
    assert len(uuid_part) == 8
    assert all(c in "0123456789abcdef" for c in uuid_part)


def test_new_task_id_slug_cleanup():
    tid = new_task_id("WITH spaces & punctuation!!", now=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
    assert tid.startswith("20260101-with-spaces-punctuation-")


def test_new_task_id_empty_slug_fallback():
    tid = new_task_id("!!!", now=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
    assert tid.startswith("20260101-task-")


def test_new_task_id_uses_now_default(monkeypatch):
    sentinel = dt.datetime(2099, 12, 31, tzinfo=dt.timezone.utc)

    class _FrozenDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return sentinel

    monkeypatch.setattr(paths_mod._dt, "datetime", _FrozenDateTime)
    tid = new_task_id("anything")
    assert tid.startswith("20991231-")


def test_new_task_id_uniqueness():
    """Two consecutive mints must not collide (cryptographic 32-bit suffix
    makes collisions astronomically unlikely)."""
    when = dt.datetime(2026, 5, 28, tzinfo=dt.timezone.utc)
    a = new_task_id("same", now=when)
    b = new_task_id("same", now=when)
    assert a != b


# --------------------------------------------------------------------------- #
# TaskPaths
# --------------------------------------------------------------------------- #


def test_task_paths_layout(tmp_path):
    tp = TaskPaths(root=tmp_path, task_id="20260528-foo-abcd1234")
    expected_dir = tmp_path / "20260528-foo-abcd1234"
    assert tp.task_dir == expected_dir
    assert tp.status_json == expected_dir / "status.json"
    assert tp.orchestration_json == expected_dir / "orchestration.json"
    assert tp.sessions_json == expected_dir / "sessions.json"
    assert tp.events_jsonl == expected_dir / "events.jsonl"
    assert tp.steps_dir == expected_dir / "steps"
    assert tp.logs_dir == expected_dir / "logs"
    assert tp.lock_file == expected_dir / ".lock"
    assert tp.pid_file == expected_dir / ".pid"
    assert tp.task_brief == expected_dir / "task_brief.md"
    assert tp.playbook_snapshot == expected_dir / "playbook_snapshot.md"
    assert tp.compile_trace == expected_dir / "compile_trace.txt"
    assert tp.compile_critique == expected_dir / "compile_critique.md"


def test_task_paths_step_and_iter_helpers(tmp_path):
    tp = TaskPaths(root=tmp_path, task_id="t1")
    assert tp.step_log_dir("01-foo") == tp.logs_dir / "01-foo"
    assert tp.iter_dir("01-foo", 1) == tp.logs_dir / "01-foo" / "iter-01"
    assert tp.iter_dir("01-foo", 12) == tp.logs_dir / "01-foo" / "iter-12"
    assert tp.step_file("01-foo") == tp.steps_dir / "01-foo.md"


def test_task_paths_iter_rejects_zero(tmp_path):
    tp = TaskPaths(root=tmp_path, task_id="t1")
    with pytest.raises(ValueError):
        tp.iter_dir("01-foo", 0)
    with pytest.raises(ValueError):
        tp.iter_dir("01-foo", -3)


@pytest.mark.parametrize(
    "bad_id",
    ["..", "foo/bar", "-rf", ""],
)
def test_task_paths_step_helpers_reject_unsafe_ids(tmp_path, bad_id):
    """Pin the sanitizer integration at the path layer. Full charset
    matrix lives in ``test_ids.py``."""
    tp = TaskPaths(root=tmp_path, task_id="t1")
    with pytest.raises(WorkflowModelError):
        tp.step_log_dir(bad_id)
    with pytest.raises(WorkflowModelError):
        tp.step_file(bad_id)
    with pytest.raises(WorkflowModelError):
        tp.iter_dir(bad_id, 1)


def test_task_paths_ensure_is_idempotent(tmp_path):
    tp = TaskPaths(root=tmp_path, task_id="t1")
    assert not tp.task_dir.exists()
    out = tp.ensure()
    assert out is tp
    assert tp.task_dir.is_dir()
    assert tp.steps_dir.is_dir()
    assert tp.logs_dir.is_dir()
    # Second call is a no-op (idempotent).
    tp.ensure()


def test_task_paths_ensure_iter_dir_creates(tmp_path):
    tp = TaskPaths(root=tmp_path, task_id="t1")
    d = tp.ensure_iter_dir("step-x", 3)
    assert d.is_dir()
    assert d == tp.iter_dir("step-x", 3)


def test_task_paths_for_task_uses_default_root(monkeypatch, tmp_path):
    monkeypatch.setenv("TIGERHARNESS_WORKFLOW_JOURNAL", str(tmp_path))
    tp = TaskPaths.for_task("abc")
    assert tp.root == tmp_path
    assert tp.task_id == "abc"


def test_task_paths_for_task_explicit_root(tmp_path):
    tp = TaskPaths.for_task("abc", root=tmp_path)
    assert tp.root == tmp_path

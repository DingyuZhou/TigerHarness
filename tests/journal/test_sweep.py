"""Tests for ``tigerharness.journal.sweep``: classification + archive."""

from __future__ import annotations

import pytest

from tigerharness.journal.models import State, Status
from tigerharness.journal.paths import JournalPaths
from tigerharness.journal.sweep import (
    DEFAULT_STUCK_TIMEOUT_SEC,
    MalformedEntry,
    SweepResult,
    stuck_timeout_from_env,
    sweep,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_status(
    paths: JournalPaths,
    task_id: str,
    *,
    state: State = State.PENDING,
    updated_at: str = "2026-06-02T08:00:00Z",
) -> None:
    """Seed one status.json on disk in active/<task_id>/."""
    paths.ensure()
    (paths.active / task_id).mkdir(exist_ok=True)
    s = Status(
        id=task_id,
        title=f"Task {task_id}",
        kind="task",
        persona="P",
        state=state,
        sessions=0 if state is State.PENDING else 1,
        max_sessions=5,
        created_at="2026-06-02T08:00:00Z",
        updated_at=updated_at,
        next_action="",
        session_ref=None,
    )
    paths.status_json(task_id).write_text(s.to_json())


# ---------------------------------------------------------------------------
# stuck_timeout_from_env
# ---------------------------------------------------------------------------

class TestStuckTimeoutFromEnv:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv(
            "TIGERHARNESS_JOURNAL_STUCK_TIMEOUT", raising=False,
        )
        assert stuck_timeout_from_env() == DEFAULT_STUCK_TIMEOUT_SEC

    def test_default_when_blank(self, monkeypatch):
        monkeypatch.setenv("TIGERHARNESS_JOURNAL_STUCK_TIMEOUT", "   ")
        assert stuck_timeout_from_env() == DEFAULT_STUCK_TIMEOUT_SEC

    def test_valid_override(self, monkeypatch):
        monkeypatch.setenv("TIGERHARNESS_JOURNAL_STUCK_TIMEOUT", "300")
        assert stuck_timeout_from_env() == 300

    def test_non_int_raises(self, monkeypatch):
        monkeypatch.setenv("TIGERHARNESS_JOURNAL_STUCK_TIMEOUT", "thirty")
        with pytest.raises(ValueError):
            stuck_timeout_from_env()

    def test_zero_or_negative_rejected(self, monkeypatch):
        monkeypatch.setenv("TIGERHARNESS_JOURNAL_STUCK_TIMEOUT", "0")
        with pytest.raises(ValueError):
            stuck_timeout_from_env()


# ---------------------------------------------------------------------------
# SweepResult helpers
# ---------------------------------------------------------------------------

class TestSweepResultHelpers:
    def test_actionable_orders_pending_first_then_stale_oldest_first(self):
        oldest = Status(
            id="a", title="A", kind="task", persona="P",
            state=State.IN_PROGRESS, sessions=1, max_sessions=5,
            created_at="t", updated_at="2026-06-02T08:00:00Z",
        )
        newer = Status(
            id="b", title="B", kind="task", persona="P",
            state=State.IN_PROGRESS, sessions=1, max_sessions=5,
            created_at="t", updated_at="2026-06-02T09:00:00Z",
        )
        pending = Status(
            id="c", title="C", kind="task", persona="P",
            state=State.PENDING, sessions=0, max_sessions=5,
            created_at="t", updated_at="t",
        )
        r = SweepResult(
            pending=[pending],
            in_progress_stale=[newer, oldest],
        )
        out = r.actionable()
        assert [s.id for s in out] == ["c", "a", "b"]

    def test_has_actionable(self):
        assert SweepResult().has_actionable() is False
        pending = Status(
            id="c", title="C", kind="task", persona="P",
            state=State.PENDING, sessions=0, max_sessions=5,
            created_at="t", updated_at="t",
        )
        assert SweepResult(pending=[pending]).has_actionable() is True

    def test_to_summary_counts_and_extras(self):
        s = Status(
            id="c", title="C", kind="task", persona="P",
            state=State.PENDING, sessions=0, max_sessions=5,
            created_at="t", updated_at="t",
        )
        r = SweepResult(
            archived=["x"],
            pending=[s],
            malformed=[MalformedEntry(task_id="bad", error="parse")],
        )
        line = r.to_summary()
        assert "1 pending" in line
        assert "archived 1 done" in line
        assert "1 malformed" in line


# ---------------------------------------------------------------------------
# sweep -- the headline
# ---------------------------------------------------------------------------

class TestSweep:
    @pytest.fixture
    def paths(self, tmp_path):
        return JournalPaths(root=tmp_path).ensure()

    def test_classifies_pending_in_progress_fresh_stale_blocked(self, paths):
        _write_status(paths, "p1", state=State.PENDING)
        _write_status(
            paths, "ip-fresh",
            state=State.IN_PROGRESS,
            updated_at="2026-06-02T08:08:00Z",  # 2 min before "now" -- fresh
        )
        _write_status(
            paths, "ip-stale",
            state=State.IN_PROGRESS,
            updated_at="2026-06-01T08:00:00Z",  # 24h+ before "now" -- stale
        )
        _write_status(paths, "bl1", state=State.BLOCKED)
        result = sweep(
            paths,
            stuck_timeout_sec=300,
            now="2026-06-02T08:10:00Z",
        )
        assert [s.id for s in result.pending] == ["p1"]
        assert [s.id for s in result.in_progress_fresh] == ["ip-fresh"]
        assert [s.id for s in result.in_progress_stale] == ["ip-stale"]
        assert [s.id for s in result.blocked] == ["bl1"]
        assert result.archived == []
        assert result.malformed == []

    def test_archives_done_tasks(self, paths):
        _write_status(paths, "d1", state=State.DONE)
        _write_status(paths, "d2", state=State.DONE)
        result = sweep(paths, stuck_timeout_sec=300, now="2026-06-02T08:00:00Z")
        assert sorted(result.archived) == ["d1", "d2"]
        # Active is empty.
        assert paths.list_active_ids() == []
        # Done dirs exist.
        assert (paths.done / "d1").is_dir()
        assert (paths.done / "d2").is_dir()

    def test_malformed_status_json_surfaced(self, paths):
        # A task dir with invalid JSON in status.json.
        (paths.active / "bad").mkdir()
        (paths.active / "bad" / "status.json").write_text("{not json")
        result = sweep(paths, stuck_timeout_sec=300, now="2026-06-02T08:00:00Z")
        assert len(result.malformed) == 1
        assert result.malformed[0].task_id == "bad"

    def test_uses_env_default_when_no_arg(self, paths, monkeypatch):
        monkeypatch.setenv("TIGERHARNESS_JOURNAL_STUCK_TIMEOUT", "600")
        _write_status(
            paths, "ip-fresh",
            state=State.IN_PROGRESS,
            updated_at="2026-06-02T08:00:00Z",
        )
        # 5 min < 10 min threshold from env -> fresh.
        result = sweep(paths, now="2026-06-02T08:05:00Z")
        assert [s.id for s in result.in_progress_fresh] == ["ip-fresh"]
        # 11 min > 10 min threshold -> stale.
        result = sweep(paths, now="2026-06-02T08:11:00Z")
        assert [s.id for s in result.in_progress_stale] == ["ip-fresh"]

    def test_empty_journal_returns_empty_result(self, paths):
        result = sweep(paths, stuck_timeout_sec=300, now="2026-06-02T08:00:00Z")
        assert result.archived == []
        assert result.pending == []
        assert result.has_actionable() is False
        assert "0 pending" in result.to_summary()

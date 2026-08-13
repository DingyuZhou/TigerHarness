"""Tests for ``tigerharness.journal.sweep``: classification + archive."""

from __future__ import annotations

import os
from datetime import datetime

import pytest

from tigerharness.journal.models import State, Status
from tigerharness.journal.paths import JournalPaths
from tigerharness.journal.sweep import (
    DEFAULT_STUCK_TIMEOUT_SEC,
    MalformedEntry,
    SweepResult,
    newest_mtime_age_seconds,
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
    session_ref: str | None = None,
    mtime: str | None = None,
) -> None:
    """Seed one status.json on disk in active/<task_id>/.

    ``session_ref`` is the attach token: ``None`` (default) = detached
    (an ``in_progress`` task is then *idle*/resumable); a token = a
    session is attached (then fresh=*busy*, stale=*crashed*).

    ``mtime`` backdates the file's modification time; it defaults to
    ``updated_at`` so the fixture is internally consistent -- a task
    whose heartbeat says "24h ago" also *looks* untouched for 24h on
    disk. Pass it explicitly to build the interesting mismatch: a stale
    heartbeat over files that were written seconds ago (a live worker
    that advanced its walk without refreshing status.json)."""
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
        session_ref=session_ref,
    )
    paths.status_json(task_id).write_text(s.to_json())
    stamp = datetime.fromisoformat(
        (mtime or updated_at).replace("Z", "+00:00"),
    ).timestamp()
    os.utime(paths.status_json(task_id), (stamp, stamp))


# ---------------------------------------------------------------------------
# newest_mtime_age_seconds
# ---------------------------------------------------------------------------

class TestNewestMtimeAgeSeconds:
    def test_reports_age_of_the_newest_file_anywhere_below(self, tmp_path):
        (tmp_path / "nested").mkdir()
        old = tmp_path / "old.txt"
        new = tmp_path / "nested" / "new.txt"
        old.write_text("a")
        new.write_text("b")
        os.utime(old, (1000.0, 1000.0))
        os.utime(new, (1500.0, 1500.0))
        assert newest_mtime_age_seconds(tmp_path, now_epoch=2000.0) == 500.0

    def test_clock_skew_never_yields_a_negative_age(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("a")
        os.utime(f, (5000.0, 5000.0))
        assert newest_mtime_age_seconds(tmp_path, now_epoch=1000.0) == 0.0

    def test_no_files_means_no_liveness_signal(self, tmp_path):
        # Directories alone carry no evidence, so the caller must fall
        # back to the heartbeat rather than treating the task as alive.
        (tmp_path / "empty-subdir").mkdir()
        assert newest_mtime_age_seconds(
            tmp_path, now_epoch=2000.0,
        ) == float("inf")

    def test_missing_directory_means_no_liveness_signal(self, tmp_path):
        assert newest_mtime_age_seconds(
            tmp_path / "gone", now_epoch=2000.0,
        ) == float("inf")


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
    def test_actionable_resumes_in_progress_before_pending(self):
        # Finish-before-start: resumable in_progress (idle + crashed),
        # oldest heartbeat first, come BEFORE any pending task.
        crashed_old = Status(
            id="a", title="A", kind="task", persona="P",
            state=State.IN_PROGRESS, sessions=1, max_sessions=5,
            created_at="t", updated_at="2026-06-02T08:00:00Z",
            session_ref="tok",
        )
        idle_new = Status(
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
            in_progress_idle=[idle_new],
            in_progress_crashed=[crashed_old],
        )
        # Resumable first (oldest heartbeat first); pending held back.
        assert [s.id for s in r.actionable()] == ["a", "b"]

    def test_actionable_waits_when_only_busy_and_pending(self):
        # A live session owns the in-flight task; do NOT start pending.
        busy = Status(
            id="a", title="A", kind="task", persona="P",
            state=State.IN_PROGRESS, sessions=1, max_sessions=5,
            created_at="t", updated_at="2026-06-02T08:00:00Z",
            session_ref="tok",
        )
        pending = Status(
            id="c", title="C", kind="task", persona="P",
            state=State.PENDING, sessions=0, max_sessions=5,
            created_at="t", updated_at="t",
        )
        r = SweepResult(pending=[pending], in_progress_busy=[busy])
        assert r.actionable() == []
        assert r.has_actionable() is False

    def test_actionable_starts_pending_when_nothing_in_progress(self):
        pending = Status(
            id="c", title="C", kind="task", persona="P",
            state=State.PENDING, sessions=0, max_sessions=5,
            created_at="t", updated_at="t",
        )
        r = SweepResult(pending=[pending])
        assert [s.id for s in r.actionable()] == ["c"]
        assert r.has_actionable() is True

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

    def test_classifies_pending_idle_busy_crashed_blocked(self, paths):
        _write_status(paths, "p1", state=State.PENDING)
        # Detached in_progress = idle/resumable -- regardless of heartbeat
        # age (a stale timestamp here proves the heartbeat is ignored).
        _write_status(
            paths, "ip-idle",
            state=State.IN_PROGRESS,
            updated_at="2026-06-01T08:00:00Z",  # 24h+ old, but detached
            session_ref=None,
        )
        # Attached + fresh = busy (a live session owns it).
        _write_status(
            paths, "ip-busy",
            state=State.IN_PROGRESS,
            updated_at="2026-06-02T08:08:00Z",  # 2 min before "now"
            session_ref="tok-busy",
        )
        # Attached + stale = crashed (owner went silent).
        _write_status(
            paths, "ip-crashed",
            state=State.IN_PROGRESS,
            updated_at="2026-06-01T08:00:00Z",  # 24h+ old
            session_ref="tok-crash",
        )
        _write_status(paths, "bl1", state=State.BLOCKED)
        result = sweep(
            paths,
            stuck_timeout_sec=300,
            now="2026-06-02T08:10:00Z",
        )
        assert [s.id for s in result.pending] == ["p1"]
        assert [s.id for s in result.in_progress_idle] == ["ip-idle"]
        assert [s.id for s in result.in_progress_busy] == ["ip-busy"]
        assert [s.id for s in result.in_progress_crashed] == ["ip-crashed"]
        assert [s.id for s in result.blocked] == ["bl1"]
        assert result.archived == []
        assert result.malformed == []

    def test_stale_heartbeat_but_fresh_files_is_busy_not_crashed(
        self, paths,
    ):
        """The false-crash regression: a workflow task advances its walk
        cursor (walk.json) on every step but only refreshes status.json
        at session boundaries. Under load a *healthy* task therefore
        carries a heartbeat older than the stuck timeout. Classifying it
        crashed makes the sweep actionable, which makes autodrive fire a
        rescue drive on top of the live one -- every interval. Files on
        disk are the second opinion: recent writes mean somebody is home.
        """
        _write_status(
            paths, "ip-working",
            state=State.IN_PROGRESS,
            updated_at="2026-06-02T07:00:00Z",   # 70 min stale
            session_ref="tok-working",
            mtime="2026-06-02T08:09:30Z",        # ...but touched 30s ago
        )
        result = sweep(
            paths, stuck_timeout_sec=300, now="2026-06-02T08:10:00Z",
        )
        assert [s.id for s in result.in_progress_busy] == ["ip-working"]
        assert result.in_progress_crashed == []
        assert result.has_actionable() is False

    def test_crashed_still_reclaimed_when_the_task_dir_is_quiet(
        self, paths,
    ):
        """The other half of the contract: mtime liveness must not make a
        genuinely dead task immortal. Sweep never writes into an
        in_progress task dir, so a dead owner leaves the dir frozen and
        the task is still reclaimed."""
        _write_status(
            paths, "ip-dead",
            state=State.IN_PROGRESS,
            updated_at="2026-06-02T07:00:00Z",
            session_ref="tok-dead",
            mtime="2026-06-02T07:00:00Z",
        )
        # A second file in the dir, equally stale -- the scan takes the
        # *newest* mtime, so this must not rescue the task either.
        note = paths.active / "ip-dead" / "worklog.md"
        note.write_text("last words\n")
        stamp = datetime.fromisoformat(
            "2026-06-02T07:05:00+00:00",
        ).timestamp()
        os.utime(note, (stamp, stamp))
        result = sweep(
            paths, stuck_timeout_sec=300, now="2026-06-02T08:10:00Z",
        )
        assert [s.id for s in result.in_progress_crashed] == ["ip-dead"]

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
        # Attached task so the heartbeat threshold actually applies.
        _write_status(
            paths, "ip",
            state=State.IN_PROGRESS,
            updated_at="2026-06-02T08:00:00Z",
            session_ref="tok",
        )
        # 5 min < 10 min threshold from env -> attached + fresh = busy.
        result = sweep(paths, now="2026-06-02T08:05:00Z")
        assert [s.id for s in result.in_progress_busy] == ["ip"]
        # 11 min > 10 min threshold -> attached + stale = crashed.
        result = sweep(paths, now="2026-06-02T08:11:00Z")
        assert [s.id for s in result.in_progress_crashed] == ["ip"]

    def test_empty_journal_returns_empty_result(self, paths):
        result = sweep(paths, stuck_timeout_sec=300, now="2026-06-02T08:00:00Z")
        assert result.archived == []
        assert result.pending == []
        assert result.has_actionable() is False
        assert "0 pending" in result.to_summary()

    def test_naive_timestamp_in_one_task_does_not_abort_sweep(self, paths):
        """Regression for the critique workflow's HIGH finding: a
        hand-edited naive ``updated_at`` on one task must not abort
        classification of every other task. The bad task gets flagged
        as ``malformed``; the others classify normally."""
        # One healthy in_progress task.
        _write_status(
            paths, "ip-fresh",
            state=State.IN_PROGRESS,
            updated_at="2026-06-02T08:08:00Z",
        )
        # One healthy pending task.
        _write_status(paths, "p1", state=State.PENDING)
        # One bad task -- naive timestamp (no Z, no +00:00). It must be
        # *attached* (session_ref set) so the classifier actually reads
        # the heartbeat; a detached task is idle and never parses it.
        # Without the fix the sweep would abort here with a TypeError.
        _write_status(
            paths, "bad-naive",
            state=State.IN_PROGRESS,
            updated_at="2026-06-02T08:00:00",
            session_ref="tok-bad",
        )
        result = sweep(
            paths,
            stuck_timeout_sec=300,
            now="2026-06-02T08:10:00Z",
        )
        # The bad task is captured as malformed -- NOT silently dropped.
        assert [m.task_id for m in result.malformed] == ["bad-naive"]
        assert "heartbeat unreadable" in result.malformed[0].error
        # The healthy tasks still classify (ip-fresh is detached -> idle).
        assert [s.id for s in result.pending] == ["p1"]
        assert [s.id for s in result.in_progress_idle] == ["ip-fresh"]

    def test_legacy_in_progress_without_session_ref_key_is_idle(self, paths):
        # A pre-instant-resume status.json with the session_ref key
        # physically absent must sweep as idle (resumable), not stuck.
        import json
        (paths.active / "legacy").mkdir()
        d = Status.new(id="legacy", title="L", persona="P").to_dict()
        d.pop("session_ref")
        d["state"] = "in_progress"
        d["sessions"] = 1
        (paths.active / "legacy" / "status.json").write_text(json.dumps(d))
        result = sweep(paths, stuck_timeout_sec=300, now="2099-01-01T00:00:00Z")
        assert [x.id for x in result.in_progress_idle] == ["legacy"]
        assert result.in_progress_busy == []
        assert result.in_progress_crashed == []

    def test_same_day_pending_tasks_actionable_in_scheduled_order(
        self, paths,
    ):
        """End-to-end feature pin for the HHmmSS id prefix: same-day
        pending tasks flow through list_active_ids() -> result.pending
        -> actionable() in their scheduled (time-of-day) order. Guards
        the chain against a future re-sort of pending silently
        regressing the scheduled-sequence promise."""
        import datetime as dt

        from tigerharness.journal.ids import new_task_id

        day = dict(year=2026, month=6, day=11, tzinfo=dt.timezone.utc)
        first = new_task_id(
            "zz scheduled first", now=dt.datetime(hour=9, **day),
        )
        second = new_task_id(
            "mm scheduled second",
            now=dt.datetime(hour=9, second=1, **day),
        )
        third = new_task_id(
            "aa scheduled third",
            now=dt.datetime(hour=12, minute=30, **day),
        )
        # Seed out of order; sort must come from the id, not insertion.
        for tid in (third, first, second):
            _write_status(paths, tid, state=State.PENDING)
        result = sweep(paths, now="2026-06-11T13:00:00Z")
        assert [s.id for s in result.actionable()] == [
            first, second, third,
        ]

    def test_legacy_and_new_format_ids_coexist(self, paths):
        """Mixed-format journal: a legacy date-only id lists, sweeps,
        and orders lexicographically next to new-format ids (the
        accepted, documented interleave) with no migration."""
        import datetime as dt

        from tigerharness.journal.ids import new_task_id

        legacy = "20260611-add-legacy-task-12345678"
        new = new_task_id(
            "new format task",
            now=dt.datetime(
                2026, 6, 11, 14, 30, 52, tzinfo=dt.timezone.utc,
            ),
        )
        for tid in (legacy, new):
            _write_status(paths, tid, state=State.PENDING)
        result = sweep(paths, now="2026-06-11T15:00:00Z")
        # Both classify as pending; order is plain lexicographic:
        # digit (1 of 143052) < letter (a of add-...).
        assert [s.id for s in result.pending] == sorted([legacy, new])
        assert [s.id for s in result.pending] == [new, legacy]


# ---------------------------------------------------------------------------
# needs_input -- the parked-question tray
# ---------------------------------------------------------------------------

def _write_needs_input(
    paths: JournalPaths,
    task_id: str,
    *,
    updated_at: str = "2026-06-26T08:00:00Z",
) -> None:
    """Seed one status.json on disk in needs_input/<task_id>/ (the
    parked tray). A parked task is state=needs_input, detached."""
    paths.ensure()
    paths.needs_input_dir(task_id).mkdir(exist_ok=True)
    s = Status(
        id=task_id,
        title=f"Task {task_id}",
        kind="task",
        persona="P",
        state=State.NEEDS_INPUT,
        sessions=1,
        max_sessions=5,
        created_at="2026-06-26T08:00:00Z",
        updated_at=updated_at,
        next_action="",
        session_ref=None,
    )
    (paths.needs_input_dir(task_id) / "status.json").write_text(s.to_json())


class TestSweepNeedsInput:
    @pytest.fixture
    def paths(self, tmp_path):
        return JournalPaths(root=tmp_path).ensure()

    def test_tray_task_surfaced_but_not_actionable(self, paths):
        # A parked task in the needs_input/ tray is surfaced for
        # visibility but is NEVER actionable (the Operator reopens it).
        _write_needs_input(paths, "parked-1")
        result = sweep(paths, stuck_timeout_sec=300, now="2026-06-26T09:00:00Z")
        assert [s.id for s in result.needs_input] == ["parked-1"]
        assert result.actionable() == []
        assert result.has_actionable() is False

    def test_tray_malformed_surfaced(self, paths):
        # A malformed status.json in the tray is reported like any other
        # malformed entry -- it does not abort the sweep.
        paths.needs_input_dir("bad").mkdir()
        (paths.needs_input_dir("bad") / "status.json").write_text("{not json")
        result = sweep(paths, stuck_timeout_sec=300, now="2026-06-26T09:00:00Z")
        assert [m.task_id for m in result.malformed] == ["bad"]
        assert result.needs_input == []

    def test_active_needs_input_defensive_branch(self, paths):
        # A needs_input status found in active/ (e.g. a hand-moved dir)
        # is surfaced as needs_input rather than falling through to the
        # in_progress classifier.
        (paths.active / "stray").mkdir()
        s = Status(
            id="stray", title="Stray", kind="task", persona="P",
            state=State.NEEDS_INPUT, sessions=1, max_sessions=5,
            created_at="2026-06-26T08:00:00Z",
            updated_at="2026-06-26T08:00:00Z",
            next_action="", session_ref=None,
        )
        paths.status_json("stray").write_text(s.to_json())
        result = sweep(paths, stuck_timeout_sec=300, now="2026-06-26T09:00:00Z")
        assert [s.id for s in result.needs_input] == ["stray"]
        assert result.in_progress_idle == []
        assert result.in_progress_busy == []
        assert result.in_progress_crashed == []

    def test_to_summary_includes_needs_input_count(self, paths):
        _write_needs_input(paths, "parked-1")
        _write_needs_input(paths, "parked-2")
        result = sweep(paths, stuck_timeout_sec=300, now="2026-06-26T09:00:00Z")
        assert "2 needs-input" in result.to_summary()

    def test_tray_coexists_with_active_queue(self, paths):
        # A parked task and an actionable pending task coexist: pending
        # is actionable, the parked one is only surfaced.
        _write_status(paths, "p1", state=State.PENDING)
        _write_needs_input(paths, "parked-1")
        result = sweep(paths, stuck_timeout_sec=300, now="2026-06-26T09:00:00Z")
        assert [s.id for s in result.pending] == ["p1"]
        assert [s.id for s in result.needs_input] == ["parked-1"]
        assert [s.id for s in result.actionable()] == ["p1"]

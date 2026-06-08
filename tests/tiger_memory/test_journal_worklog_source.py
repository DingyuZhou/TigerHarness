"""Tests for ``tigerharness.tiger_memory.sources.journal_worklog``: the
per-persona journal-memory adapter (Phase 2).

Coverage intent: discover groups one ``SourceRecord`` per ``(task,
persona)`` across ``active/`` and ``done/``, filters strictly by persona,
derives the event window from frontmatter timestamps (falling back to
file mtimes), uses max worklog-file mtime as ``activity_mtime`` (the
cascade trigger), and never crashes discovery over a transient FS error.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from tigerharness.journal import worklog
from tigerharness.journal.paths import JournalPaths
from tigerharness.journal.worklog import WorklogEntry
from tigerharness.tiger_memory.sources import JournalWorklogAdapter
from tigerharness.tiger_memory.sources import journal_worklog as jw


TASK_A = "20260608-task-a-aaaa1111"
TASK_B = "20260608-task-b-bbbb2222"
TASK_C = "20260608-task-c-cccc3333"
TASK_D = "20260608-task-d-dddd4444"


@pytest.fixture()
def paths(tmp_path: Path) -> JournalPaths:
    return JournalPaths(tmp_path / "journal").ensure()


# ---------------------------------------------------------------------------
# _parse_dt
# ---------------------------------------------------------------------------

class TestParseDt:
    def test_none_and_empty(self):
        assert jw._parse_dt(None) is None
        assert jw._parse_dt("") is None

    def test_valid_z_suffix(self):
        got = jw._parse_dt("2026-06-08T12:00:00Z")
        assert got == datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)

    def test_naive_timestamp_is_assumed_utc(self):
        # A hand-edited stamp with no offset must come back tz-aware
        # (UTC), so it can be compared against our normal ``...Z`` stamps
        # without raising. See test_mixed_naive_and_aware_stamps_no_crash.
        got = jw._parse_dt("2026-06-08T12:00:00")
        assert got == datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
        assert got.tzinfo is not None

    def test_explicit_offset_is_preserved(self):
        got = jw._parse_dt("2026-06-08T12:00:00+05:00")
        assert got is not None and got.utcoffset() is not None
        assert got.utcoffset().total_seconds() == 5 * 3600

    def test_garbage_returns_none(self):
        assert jw._parse_dt("not-a-date") is None


# ---------------------------------------------------------------------------
# _format_entry
# ---------------------------------------------------------------------------

class TestFormatEntry:
    def test_full_entry_uses_ended_at_and_all_meta(self):
        e = WorklogEntry(
            task_id=TASK_A, persona="Rukawa", step="build", kind="workflow",
            role="builder", verdict="APPROVE", objective="Build X",
            started_at="2026-06-08T11:00:00Z", ended_at="2026-06-08T12:00:00Z",
            body="Did the build.",
        )
        out = jw._format_entry(e)
        assert out.splitlines()[0] == (
            "[2026-06-08T12:00:00Z] Rukawa "
            "(step=build, role=builder, verdict=APPROVE)"
        )
        assert "objective: Build X" in out
        assert out.endswith("Did the build.")

    def test_minimal_entry_uses_started_at_no_meta_no_body(self):
        e = WorklogEntry(
            task_id=TASK_A, persona="Rukawa", step="review",
            started_at="2026-06-08T13:00:00Z", body="",
        )
        out = jw._format_entry(e)
        assert out == "[2026-06-08T13:00:00Z] Rukawa (step=review)"

    def test_bare_entry_has_empty_timestamp(self):
        e = WorklogEntry(
            task_id=TASK_A, persona="Rukawa", step="notes", body="Some notes.",
        )
        out = jw._format_entry(e)
        assert out == "[] Rukawa (step=notes)\n\nSome notes."


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------

class TestDiscover:
    def _seed(self, paths: JournalPaths) -> None:
        # Task A (active): two Rukawa turns + one Mitsui turn (must be
        # filtered out for a Rukawa adapter).
        worklog.write_entry(paths, WorklogEntry(
            task_id=TASK_A, persona="Rukawa", step="build", kind="workflow",
            role="builder", verdict="APPROVE", objective="Build X",
            started_at="2026-06-08T11:00:00Z", ended_at="2026-06-08T12:00:00Z",
            body="Did the build.",
        ))
        worklog.write_entry(paths, WorklogEntry(
            task_id=TASK_A, persona="Rukawa", step="review",
            started_at="2026-06-08T13:00:00Z", body="",
        ))
        worklog.write_entry(paths, WorklogEntry(
            task_id=TASK_A, persona="Mitsui", step="audit",
            ended_at="2026-06-08T14:00:00Z", body="Audited.",
        ))
        # Task B (active): a Rukawa turn with NO frontmatter timestamps ->
        # exercises the file-mtime fallback for the event window.
        worklog.write_entry(paths, WorklogEntry(
            task_id=TASK_B, persona="Rukawa", step="notes", body="Some notes.",
        ))
        # Task C (active): Mitsui only -> no record for Rukawa.
        worklog.write_entry(paths, WorklogEntry(
            task_id=TASK_C, persona="Mitsui", step="audit", body="x",
        ))
        # Task D (done): a Rukawa turn -> archived tree is scanned too.
        worklog.write_entry(paths, WorklogEntry(
            task_id=TASK_D, persona="Rukawa", step="ship",
            ended_at="2026-06-07T09:00:00Z", body="Shipped.",
        ), archived=True)
        # Noise under active/: a loose file and an unsafe-named dir.
        (paths.active / "loose.txt").write_text("ignore me")
        (paths.active / ".hidden").mkdir()

    def test_groups_per_task_persona_across_active_and_done(
        self, paths: JournalPaths,
    ):
        self._seed(paths)
        adapter = JournalWorklogAdapter(
            journal_root=paths.root, persona="Rukawa", team="tigers",
        )
        recs = list(adapter.discover())
        # A and B from active (C is Mitsui-only -> skipped), then D from done.
        assert [r.source_id for r in recs] == [
            f"{TASK_A}/Rukawa", f"{TASK_B}/Rukawa", f"{TASK_D}/Rukawa",
        ]

        rec_a = recs[0]
        assert rec_a.source == "journal"
        assert rec_a.conversation_uuid == str(
            uuid5(NAMESPACE_URL, f"journal:tigers/{TASK_A}/Rukawa")
        )
        assert rec_a.raw_path == paths.worklog(TASK_A)
        # Event window from frontmatter stamps: min 11:00, max 13:00.
        assert rec_a.first_event_at == datetime(
            2026, 6, 8, 11, 0, tzinfo=timezone.utc
        )
        assert rec_a.last_event_at == datetime(
            2026, 6, 8, 13, 0, tzinfo=timezone.utc
        )
        # activity_mtime == newest of Rukawa's own worklog files.
        rukawa_files = [
            e.path for e in worklog.list_entries(paths, TASK_A)
            if e.persona == "Rukawa"
        ]
        assert rec_a.activity_mtime == max(
            p.stat().st_mtime for p in rukawa_files
        )
        # Content carries Rukawa's turns and excludes Mitsui's.
        assert "step=build" in rec_a.content
        assert "objective: Build X" in rec_a.content
        assert "Did the build." in rec_a.content
        assert "step=review" in rec_a.content
        assert "Mitsui" not in rec_a.content
        assert "Audited." not in rec_a.content

    def test_no_timestamps_falls_back_to_file_mtime(self, paths: JournalPaths):
        self._seed(paths)
        adapter = JournalWorklogAdapter(
            journal_root=paths.root, persona="Rukawa", team="tigers",
        )
        rec_b = next(
            r for r in adapter.discover() if r.source_id == f"{TASK_B}/Rukawa"
        )
        # Single file, no stamps -> first == last, both UTC, from the mtime.
        assert rec_b.first_event_at == rec_b.last_event_at
        assert rec_b.first_event_at.tzinfo == timezone.utc
        assert rec_b.content.startswith("[] Rukawa (step=notes)")

    def test_archived_record_points_at_done_worklog(self, paths: JournalPaths):
        self._seed(paths)
        adapter = JournalWorklogAdapter(
            journal_root=paths.root, persona="Rukawa", team="tigers",
        )
        rec_d = next(
            r for r in adapter.discover() if r.source_id == f"{TASK_D}/Rukawa"
        )
        assert rec_d.raw_path == paths.worklog(TASK_D, archived=True)
        assert rec_d.last_event_at == datetime(
            2026, 6, 7, 9, 0, tzinfo=timezone.utc
        )

    def test_missing_done_dir_is_skipped(self, tmp_path: Path):
        # Only an active entry; never ``ensure()`` -> done/ does not exist,
        # exercising the ``base_dir not is_dir -> return`` branch.
        paths = JournalPaths(tmp_path / "journal")
        worklog.write_entry(paths, WorklogEntry(
            task_id=TASK_A, persona="Rukawa", step="build", body="x",
        ))
        assert not paths.done.is_dir()
        adapter = JournalWorklogAdapter(
            journal_root=paths.root, persona="Rukawa", team="tigers",
        )
        recs = list(adapter.discover())
        assert [r.source_id for r in recs] == [f"{TASK_A}/Rukawa"]

    def test_empty_journal_root_yields_nothing(self, tmp_path: Path):
        adapter = JournalWorklogAdapter(
            journal_root=tmp_path / "journal", persona="Rukawa", team="tigers",
        )
        assert list(adapter.discover()) == []


# ---------------------------------------------------------------------------
# _record_for — defensive FS-error path
# ---------------------------------------------------------------------------

class TestRecordForStatFailure:
    def test_unstatable_entries_yield_no_record(
        self, paths: JournalPaths, monkeypatch,
    ):
        # list_entries returns a Rukawa entry whose file does not exist, so
        # every stat raises -> mtimes empty -> no record (never crash).
        ghost = WorklogEntry(
            task_id=TASK_A, persona="Rukawa", step="build",
            body="x", path=Path("/nonexistent/0001-rukawa-build.md"),
        )
        monkeypatch.setattr(jw.worklog, "list_entries", lambda *a, **k: [ghost])
        adapter = JournalWorklogAdapter(
            journal_root=paths.root, persona="Rukawa", team="tigers",
        )
        assert adapter._record_for(paths, TASK_A, archived=False) is None

    def test_unreadable_worklog_skips_task(
        self, paths: JournalPaths, monkeypatch,
    ):
        # A corrupt worklog file makes list_entries raise; discovery must
        # skip the task rather than crash the whole sweep.
        def boom(*a, **k):
            raise OSError("unreadable")

        monkeypatch.setattr(jw.worklog, "list_entries", boom)
        adapter = JournalWorklogAdapter(
            journal_root=paths.root, persona="Rukawa", team="tigers",
        )
        assert adapter._record_for(paths, TASK_A, archived=False) is None

    def test_mixed_naive_and_aware_stamps_no_crash(self, paths: JournalPaths):
        # Regression: a task whose worklog mixes an aware ``...Z`` stamp
        # with a hand-edited naive one must not raise TypeError out of
        # discover() (which would abort the persona's whole sweep). The
        # event window is computed over both, treating naive as UTC.
        worklog.write_entry(paths, WorklogEntry(
            task_id=TASK_A, persona="Rukawa", step="build",
            ended_at="2026-06-08T12:00:00Z", body="aware turn",
        ))
        worklog.write_entry(paths, WorklogEntry(
            task_id=TASK_A, persona="Rukawa", step="review",
            ended_at="2026-06-08T11:00:00", body="naive turn",
        ))
        adapter = JournalWorklogAdapter(
            journal_root=paths.root, persona="Rukawa", team="tigers",
        )
        rec = next(
            r for r in adapter.discover() if r.source_id == f"{TASK_A}/Rukawa"
        )
        assert rec.first_event_at == datetime(
            2026, 6, 8, 11, 0, tzinfo=timezone.utc
        )
        assert rec.last_event_at == datetime(
            2026, 6, 8, 12, 0, tzinfo=timezone.utc
        )

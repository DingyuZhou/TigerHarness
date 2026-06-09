"""Tests for ``tigerharness.journal.walk``: the kind=workflow graph-walk
cursor sidecar (``walk.json``).

Coverage intent: render/parse round-trip, read (missing -> None, present
-> parse), atomic write + read-back, lazy ``initial`` cursor, ``advance``
(history append + cursor move), and the sentinel constants. The cursor is
plain JSON (no yaml) so the core install works without the ``[memory]``
extra.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tigerharness.journal import walk
from tigerharness.journal.paths import JournalPaths
from tigerharness.journal.walk import WalkState, WalkStep


TASK_ID = "20260608-wf-abcd1234"


@pytest.fixture()
def paths(tmp_path: Path) -> JournalPaths:
    return JournalPaths(tmp_path / "journal").ensure()


# ---------------------------------------------------------------------------
# sentinels
# ---------------------------------------------------------------------------

class TestSentinels:
    def test_constants(self):
        assert walk.DONE == "__done__"
        assert walk.ESCALATE == "__escalate__"
        assert walk.SENTINELS == {"__done__", "__escalate__"}


# ---------------------------------------------------------------------------
# render / parse
# ---------------------------------------------------------------------------

class TestRenderParse:
    def test_roundtrip_empty_history(self):
        state = WalkState(task_id=TASK_ID, current="plan")
        got = walk.parse(walk.render(state))
        assert got.task_id == TASK_ID
        assert got.current == "plan"
        assert got.history == ()

    def test_roundtrip_with_history(self):
        state = WalkState(
            task_id=TASK_ID,
            current="build",
            history=(
                WalkStep(step="plan", verdict="APPROVE", next="build",
                         at="2026-06-08T12:00:00Z"),
            ),
            started_at="2026-06-08T11:00:00Z",
            updated_at="2026-06-08T12:00:00Z",
        )
        got = walk.parse(walk.render(state))
        assert got == state

    def test_render_is_valid_json(self):
        text = walk.render(WalkState(task_id=TASK_ID, current="plan"))
        data = json.loads(text)
        assert data["task_id"] == TASK_ID
        assert data["current"] == "plan"
        assert data["history"] == []
        assert text.endswith("\n")

    def test_parse_tolerates_missing_fields(self):
        got = walk.parse('{"current": "plan"}')
        assert got.task_id == ""
        assert got.current == "plan"
        assert got.history == ()
        assert got.started_at is None

    def test_parse_unicode(self):
        state = WalkState(task_id=TASK_ID, current="实现")
        assert walk.parse(walk.render(state)).current == "实现"


# ---------------------------------------------------------------------------
# read / write
# ---------------------------------------------------------------------------

class TestReadWrite:
    def test_read_missing_returns_none(self, paths: JournalPaths):
        assert walk.read(paths, TASK_ID) is None

    def test_write_then_read(self, paths: JournalPaths):
        state = WalkState(task_id=TASK_ID, current="plan")
        walk.write(paths, state)
        assert paths.walk_json(TASK_ID).is_file()
        got = walk.read(paths, TASK_ID)
        assert got.current == "plan"

    def test_read_corrupt_raises(self, paths: JournalPaths):
        p = paths.walk_json(TASK_ID)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json")
        with pytest.raises(json.JSONDecodeError):
            walk.read(paths, TASK_ID)

    def test_archived_path_distinct(self, paths: JournalPaths):
        state = WalkState(task_id=TASK_ID, current="plan")
        walk.write(paths, state, archived=True)
        assert walk.read(paths, TASK_ID) is None  # not under active/
        assert walk.read(paths, TASK_ID, archived=True).current == "plan"


# ---------------------------------------------------------------------------
# initial / advance
# ---------------------------------------------------------------------------

class TestInitialAdvance:
    def test_initial_positions_at_entrypoint(self):
        state = walk.initial(TASK_ID, "plan")
        assert state.task_id == TASK_ID
        assert state.current == "plan"
        assert state.history == ()
        assert state.started_at
        assert state.updated_at == state.started_at

    def test_advance_moves_cursor_and_records(self):
        state = walk.initial(TASK_ID, "plan")
        nxt = walk.advance(
            state, step="plan", verdict="APPROVE", next_step="build",
        )
        assert nxt.current == "build"
        assert len(nxt.history) == 1
        h = nxt.history[0]
        assert (h.step, h.verdict, h.next) == ("plan", "APPROVE", "build")
        assert h.at
        # original is unchanged (frozen / replace semantics)
        assert state.current == "plan"
        assert state.history == ()

    def test_advance_revise_self_loop_appends(self):
        state = walk.initial(TASK_ID, "plan")
        once = walk.advance(
            state, step="plan", verdict="REVISE", next_step="plan",
        )
        twice = walk.advance(
            once, step="plan", verdict="APPROVE", next_step="build",
        )
        assert [h.verdict for h in twice.history] == ["REVISE", "APPROVE"]
        assert twice.current == "build"

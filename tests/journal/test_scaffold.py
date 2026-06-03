"""Tests for ``tigerharness.journal.scaffold``: new_task end-to-end."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tigerharness.journal.models import JournalModelError, State, Status
from tigerharness.journal.paths import JournalPaths
from tigerharness.journal.scaffold import (
    JournalScaffoldError,
    _first_h1,
    _write_atomic,
    new_task,
)


# ---------------------------------------------------------------------------
# _first_h1
# ---------------------------------------------------------------------------

class TestFirstH1:
    def test_basic(self):
        assert _first_h1("# Hello\nbody") == "Hello"

    def test_skips_to_first_h1(self):
        text = "intro\n# First\n# Second\n"
        assert _first_h1(text) == "First"

    def test_strips_surrounding_whitespace(self):
        assert _first_h1("   #   Spaced   ") == "Spaced"

    def test_returns_empty_when_absent(self):
        assert _first_h1("just body, no heading") == ""

    def test_ignores_h2_and_deeper(self):
        assert _first_h1("## Sub\n### Sub\n") == ""


# ---------------------------------------------------------------------------
# _write_atomic
# ---------------------------------------------------------------------------

class TestWriteAtomic:
    def test_creates_file_with_content(self, tmp_path):
        target = tmp_path / "nested" / "out.txt"
        _write_atomic(target, "hello\n")
        assert target.read_text() == "hello\n"

    def test_replaces_existing_file_atomically(self, tmp_path):
        target = tmp_path / "out.txt"
        target.write_text("old")
        _write_atomic(target, "new")
        assert target.read_text() == "new"
        # No stray .tmp left behind.
        assert sorted(p.name for p in tmp_path.iterdir()) == ["out.txt"]


# ---------------------------------------------------------------------------
# new_task end-to-end
# ---------------------------------------------------------------------------

class TestNewTask:
    @pytest.fixture
    def paths(self, tmp_path):
        return JournalPaths(root=tmp_path / "journal")

    def test_happy_path_creates_all_artifacts(self, paths):
        prd = "# Cache eviction\nAdd LRU eviction to redis layer.\n"
        result = new_task(
            prd_text=prd,
            persona="Mitsui",
            paths=paths,
        )
        assert result.status.title == "Cache eviction"
        assert result.status.persona == "Mitsui"
        assert result.status.state is State.PENDING
        assert result.task_dir.is_dir()
        assert paths.task_md(result.task_id).read_text() == prd
        # status.json round-trips back to the same Status.
        status_on_disk = Status.from_json(
            paths.status_json(result.task_id).read_text(),
        )
        assert status_on_disk == result.status
        # progress.md seeded with H1 only.
        progress = paths.progress_md(result.task_id).read_text()
        assert progress.startswith(f"# Progress: {result.task_id}")
        # artifacts/ created.
        assert paths.artifacts(result.task_id).is_dir()
        # OPERATING.md installed at journal root.
        assert paths.operating_md.is_file()
        text = paths.operating_md.read_text()
        assert "decision procedure" in text.lower()

    def test_title_arg_wins_over_prd_h1(self, paths):
        result = new_task(
            prd_text="# From PRD\nbody\n",
            persona="P",
            paths=paths,
            title="Explicit",
        )
        assert result.status.title == "Explicit"

    def test_falls_back_to_h1_when_no_title_arg(self, paths):
        result = new_task(
            prd_text="# From PRD\nbody\n",
            persona="P",
            paths=paths,
        )
        assert result.status.title == "From PRD"

    def test_falls_back_to_default_title_when_no_h1(self, paths):
        result = new_task(
            prd_text="body without heading",
            persona="P",
            paths=paths,
        )
        assert result.status.title == "task"

    def test_empty_prd_rejected(self, paths):
        with pytest.raises(JournalScaffoldError):
            new_task(prd_text="   ", persona="P", paths=paths)

    def test_unsupported_kind_rejected(self, paths):
        with pytest.raises(JournalScaffoldError):
            new_task(
                prd_text="# T\nb\n",
                persona="P",
                paths=paths,
                kind="workflow",
            )

    def test_blank_persona_rejected_via_model(self, paths):
        """Even though scaffold doesn't validate persona directly, the
        Status.new layer does -- we just confirm the error surface is
        JournalScaffoldError (the model error gets re-raised)."""
        with pytest.raises(JournalScaffoldError):
            new_task(
                prd_text="# T\nbody\n",
                persona="   ",
                paths=paths,
            )

    def test_slug_overrider_lands_in_task_id(self, paths):
        result = new_task(
            prd_text="# Long Title That Would Slugify\nbody\n",
            persona="P",
            paths=paths,
            slug="short",
        )
        assert "-short-" in result.task_id

    def test_operating_md_not_overwritten_when_present(self, paths):
        paths.ensure()
        paths.operating_md.write_text("CUSTOMISED BY HUMAN")
        new_task(prd_text="# T\nbody\n", persona="P", paths=paths)
        # Untouched.
        assert paths.operating_md.read_text() == "CUSTOMISED BY HUMAN"

    def test_collision_with_active_then_done_handled(
        self, paths, monkeypatch,
    ):
        """A scaffold should retry the uuid8 if the proposed id already
        exists in active/ OR done/. We force a collision with done/."""
        # Pre-create a done/<some-id> using the same date+slug we expect
        # the next mint to produce, and a stable uuid8 we can intercept.
        # Easiest: stub secrets.token_hex to return a colliding value
        # first, then a fresh one.
        from tigerharness.journal import ids as id_mod
        calls = []

        def fake_token_hex(n: int) -> str:
            calls.append(n)
            return "deadbeef" if len(calls) == 1 else "feedface"

        monkeypatch.setattr(id_mod.secrets, "token_hex", fake_token_hex)
        # Pre-seed done/<...>-deadbeef as if it were already archived.
        import datetime as dt
        date = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
        colliding_id = f"{date}-collision-deadbeef"
        (paths.done / colliding_id).mkdir(parents=True)
        (paths.done / colliding_id / "status.json").write_text("{}")
        result = new_task(
            prd_text="# Collision\nbody\n",
            persona="P",
            paths=paths,
            slug="collision",
        )
        # The retry should have used feedface.
        assert result.task_id.endswith("-feedface")

    def test_id_collision_persistent_raises(
        self, paths, monkeypatch,
    ):
        """Both attempted ids collide -> the inner JournalIdError is
        re-raised as JournalScaffoldError."""
        from tigerharness.journal import ids as id_mod
        monkeypatch.setattr(
            id_mod.secrets, "token_hex", lambda n: "deadbeef",
        )
        # Pre-seed both possible ids in done/.
        import datetime as dt
        date = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
        (paths.done / f"{date}-collision-deadbeef").mkdir(parents=True)
        (paths.done / f"{date}-collision-deadbeef" / "status.json").write_text("{}")
        with pytest.raises(JournalScaffoldError):
            new_task(
                prd_text="# Collision\nbody\n",
                persona="P",
                paths=paths,
                slug="collision",
            )

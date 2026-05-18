"""Tests for the threads.json migration CLI.

Rewrites pre-routing ``{thread_ts: "session_id"}`` entries to the new
``{thread_ts: {session_id, persona}}`` shape so per-persona memory
filtering picks them up.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tigerharness.slack_bridge.migrate import migrate, main


def _write_threads(state_dir: Path, content: dict) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    p = state_dir / "threads.json"
    p.write_text(json.dumps(content), encoding="utf-8")
    return p


class TestMigrate:
    def test_pre_routing_entries_rewritten(self, tmp_path: Path):
        """The core case: all entries are bare strings (pre-PR4 schema).
        After migration every entry has the new dict shape with
        persona=target."""
        _write_threads(tmp_path, {
            "t1": "sess-A",
            "t2": "sess-B",
        })
        result = migrate(tmp_path, "ayako")
        assert result.rewritten == 2
        assert result.already_new == 0
        on_disk = json.loads((tmp_path / "threads.json").read_text())
        assert on_disk == {
            "t1": {"session_id": "sess-A", "persona": "ayako"},
            "t2": {"session_id": "sess-B", "persona": "ayako"},
        }

    def test_mixed_schemas_only_rewrites_pre_routing(self, tmp_path: Path):
        """A real-world file mid-migration: some old, some new. Tool
        rewrites only the old ones, leaves the new ones untouched."""
        _write_threads(tmp_path, {
            "old.ts": "sess-old",
            "new.ts": {"session_id": "sess-new", "persona": "sakuragi"},
        })
        result = migrate(tmp_path, "ayako")
        assert result.rewritten == 1
        assert result.already_new == 1
        on_disk = json.loads((tmp_path / "threads.json").read_text())
        # Old one rewritten.
        assert on_disk["old.ts"] == {"session_id": "sess-old", "persona": "ayako"}
        # New one preserved (different persona!).
        assert on_disk["new.ts"] == {"session_id": "sess-new", "persona": "sakuragi"}

    def test_idempotent_when_already_migrated(self, tmp_path: Path):
        """Re-running on a fully-migrated file rewrites nothing."""
        _write_threads(tmp_path, {
            "t1": {"session_id": "sess-A", "persona": "ayako"},
        })
        result = migrate(tmp_path, "ayako")
        assert result.rewritten == 0
        assert result.already_new == 1

    def test_dry_run_does_not_write(self, tmp_path: Path):
        """--dry-run reports the same counts but leaves the file alone."""
        original = {"t1": "sess-A"}
        p = _write_threads(tmp_path, original)
        result = migrate(tmp_path, "ayako", dry_run=True)
        assert result.rewritten == 1
        # File unchanged on disk.
        assert json.loads(p.read_text()) == original

    def test_malformed_entries_left_alone(self, tmp_path: Path):
        """Entries that aren't bare strings AND aren't valid dicts (no
        session_id) are left as-is and counted in ``invalid_skipped``."""
        _write_threads(tmp_path, {
            "good_old": "sess-A",
            "good_new": {"session_id": "sess-B", "persona": "ayako"},
            "bad_int": 42,
            "bad_missing_sid": {"persona": "ayako"},
            "bad_empty_str": "",
        })
        result = migrate(tmp_path, "sakuragi")
        assert result.rewritten == 1  # only good_old
        assert result.already_new == 1  # good_new
        assert result.invalid_skipped == 3

    def test_missing_threads_file_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="threads.json not found"):
            migrate(tmp_path, "ayako")

    def test_invalid_json_raises(self, tmp_path: Path):
        p = tmp_path / "threads.json"
        p.write_text("not valid json {")
        with pytest.raises(ValueError, match="not valid JSON"):
            migrate(tmp_path, "ayako")

    def test_top_level_not_dict_raises(self, tmp_path: Path):
        p = tmp_path / "threads.json"
        p.write_text(json.dumps(["a", "list"]))
        with pytest.raises(ValueError, match="must be a JSON object"):
            migrate(tmp_path, "ayako")

    def test_empty_persona_raises(self, tmp_path: Path):
        _write_threads(tmp_path, {"t1": "sess"})
        with pytest.raises(ValueError, match="cannot be empty"):
            migrate(tmp_path, "   ")

    def test_persona_stripped(self, tmp_path: Path):
        """Whitespace around the persona name is stripped."""
        _write_threads(tmp_path, {"t1": "sess"})
        migrate(tmp_path, "  ayako  ")
        on_disk = json.loads((tmp_path / "threads.json").read_text())
        assert on_disk["t1"]["persona"] == "ayako"


class TestMigrateMain:
    """Tests of the CLI entry point (argparse plumbing)."""

    def test_main_success_prints_counts(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ):
        _write_threads(tmp_path, {"t1": "sess", "t2": "sess2"})
        rc = main([
            "--state-dir", str(tmp_path),
            "--to", "ayako",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "rewrote 2" in out
        assert "ayako" in out

    def test_main_dry_run_uses_would_rewrite_verb(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ):
        _write_threads(tmp_path, {"t1": "sess"})
        rc = main([
            "--state-dir", str(tmp_path),
            "--to", "ayako",
            "--dry-run",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "would rewrite" in out

    def test_main_missing_file_returns_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ):
        rc = main([
            "--state-dir", str(tmp_path / "nowhere"),
            "--to", "ayako",
        ])
        assert rc == 1
        err = capsys.readouterr().err
        assert "error:" in err
        assert "threads.json not found" in err

    def test_main_reports_invalid_entries_to_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ):
        _write_threads(tmp_path, {
            "ok": "sess",
            "bad": 42,
        })
        rc = main([
            "--state-dir", str(tmp_path),
            "--to", "ayako",
        ])
        assert rc == 0
        captured = capsys.readouterr()
        assert "rewrote 1" in captured.out
        assert "warning" in captured.err
        assert "1 malformed" in captured.err

    def test_main_no_changes_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture,
    ):
        """If everything is already migrated, mention that explicitly --
        helps users notice 'oh, I already ran this'."""
        _write_threads(tmp_path, {
            "t1": {"session_id": "sess", "persona": "ayako"},
        })
        rc = main([
            "--state-dir", str(tmp_path),
            "--to", "ayako",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "no changes needed" in out

"""Journal-placement guarantees (2026-06-12 multiroot item 5):
scaffold-time provenance in status.json, the sweep's misplaced-task
flagging, and the scheduling verbs' refusal to fall back silently to
the per-user XDG journal."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tigerharness.journal.cli import main
from tigerharness.journal.models import Status
from tigerharness.journal.paths import JournalPaths
from tigerharness.journal.sweep import sweep


def _team_root(tmp_path: Path) -> Path:
    team = tmp_path / "tigers"
    (team / "configs").mkdir(parents=True)
    (team / "configs" / "personas.yaml").write_text(
        "personas:\n  - name: chief\n"
    )
    pdir = team / "personas" / "chief"
    pdir.mkdir(parents=True)
    (pdir / "prompt.md").write_text("You are chief.\n")
    return team


class TestProvenanceStamping:
    def test_new_task_records_journal_root(self, tmp_path, capsys):
        journal = tmp_path / "j"
        prd = tmp_path / "brief.md"
        prd.write_text("# Do the thing\n")
        rc = main([
            "--journal-dir", str(journal), "new",
            "--prd", str(prd), "--persona", "chief",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        task_id = out.split("Scaffolded: ")[1].splitlines()[0].strip()
        status = json.loads(
            (journal / "active" / task_id / "status.json").read_text()
        )
        assert status["journal_root"] == str(journal.resolve())

    def test_old_status_without_field_still_loads(self):
        """Pre-provenance statuses parse; the field reads as None."""
        s = Status.new(
            id="t-1", title="x", persona="p", journal_root=None,
        )
        d = s.to_dict()
        assert "journal_root" not in d  # suppressed when unset
        assert Status.from_dict(d).journal_root is None


class TestSweepFlagsMisplacement:
    def _scaffold(self, journal: Path, tmp_path, capsys) -> str:
        prd = tmp_path / "brief.md"
        prd.write_text("# Thing\n")
        rc = main([
            "--journal-dir", str(journal), "new",
            "--prd", str(prd), "--persona", "chief",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        return out.split("Scaffolded: ")[1].splitlines()[0].strip()

    def test_misplaced_task_is_flagged(self, tmp_path, capsys):
        journal = tmp_path / "j"
        task_id = self._scaffold(journal, tmp_path, capsys)
        # Simulate the incident: rewrite provenance to another root,
        # as if the task dir had been dropped into the wrong journal.
        status_path = journal / "active" / task_id / "status.json"
        data = json.loads(status_path.read_text())
        data["journal_root"] = str(tmp_path / "other-journal")
        status_path.write_text(json.dumps(data))

        result = sweep(JournalPaths(root=journal))
        assert result.misplaced == [
            (task_id, str(tmp_path / "other-journal"))
        ]
        assert result.provenance_unknown == []

    def test_matching_provenance_is_quiet(self, tmp_path, capsys):
        journal = tmp_path / "j"
        self._scaffold(journal, tmp_path, capsys)
        result = sweep(JournalPaths(root=journal))
        assert result.misplaced == []
        assert result.provenance_unknown == []

    def test_pre_field_task_reports_unknown_not_guessed(
        self, tmp_path, capsys
    ):
        journal = tmp_path / "j"
        task_id = self._scaffold(journal, tmp_path, capsys)
        status_path = journal / "active" / task_id / "status.json"
        data = json.loads(status_path.read_text())
        del data["journal_root"]
        status_path.write_text(json.dumps(data))
        result = sweep(JournalPaths(root=journal))
        assert result.misplaced == []
        assert result.provenance_unknown == [task_id]

    def test_cli_sweep_prints_misplacement(self, tmp_path, capsys):
        journal = tmp_path / "j"
        task_id = self._scaffold(journal, tmp_path, capsys)
        status_path = journal / "active" / task_id / "status.json"
        data = json.loads(status_path.read_text())
        data["journal_root"] = "/somewhere/else"
        status_path.write_text(json.dumps(data))
        rc = main(["--journal-dir", str(journal), "sweep"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "MISPLACED" in out
        assert "/somewhere/else" in out
        rc = main([
            "--journal-dir", str(journal), "sweep", "--format", "json",
        ])
        payload = json.loads(capsys.readouterr().out)
        assert payload["misplaced"] == [
            {"task_id": task_id, "recorded_root": "/somewhere/else"}
        ]


class TestSchedulingGuard:
    """`new` and `schedule add` refuse the silent XDG fallback."""

    def test_new_refuses_from_non_team_cwd(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.delenv("TIGERHARNESS_JOURNAL_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        prd = tmp_path / "brief.md"
        prd.write_text("# x\n")
        rc = main(["new", "--prd", str(prd), "--persona", "chief"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "refusing to schedule" in err
        assert str(tmp_path) in err
        assert "--journal-dir" in err

    def test_new_allowed_from_team_root_cwd(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.delenv("TIGERHARNESS_JOURNAL_DIR", raising=False)
        team = _team_root(tmp_path)
        monkeypatch.chdir(team)
        prd = tmp_path / "brief.md"
        prd.write_text("# x\n")
        rc = main(["new", "--prd", str(prd), "--persona", "chief"])
        assert rc == 0
        out = capsys.readouterr().out
        task_id = out.split("Scaffolded: ")[1].splitlines()[0].strip()
        assert (team / "journal" / "active" / task_id).is_dir()

    def test_explicit_journal_dir_overrides_guard(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.delenv("TIGERHARNESS_JOURNAL_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        prd = tmp_path / "brief.md"
        prd.write_text("# x\n")
        rc = main([
            "--journal-dir", str(tmp_path / "explicit"), "new",
            "--prd", str(prd), "--persona", "chief",
        ])
        assert rc == 0

    def test_env_var_override_passes_guard(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv(
            "TIGERHARNESS_JOURNAL_DIR", str(tmp_path / "envj")
        )
        monkeypatch.chdir(tmp_path)
        prd = tmp_path / "brief.md"
        prd.write_text("# x\n")
        rc = main(["new", "--prd", str(prd), "--persona", "chief"])
        assert rc == 0

    def test_schedule_add_refuses_from_non_team_cwd(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.delenv("TIGERHARNESS_JOURNAL_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        prd = tmp_path / "brief.md"
        prd.write_text("# x\n")
        rc = main([
            "schedule", "add", "--title", "t", "--prd", str(prd),
            "--persona", "chief", "--at", "09:00",
        ])
        assert rc == 2
        assert "refusing to schedule" in capsys.readouterr().err

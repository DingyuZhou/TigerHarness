"""Deferred-task inbox (`journal defer` / `journal materialize`): the
cheap Slack-side scheduling rail and its subscription-rail
materialization. Pins the design invariants from the 2026-06-12
multiroot plan: team pinning never silently falls back to the XDG
journal; a materialized task is indistinguishable from a `journal new`
scaffold; malformed entries fail loudly and stay in the inbox."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tigerharness.journal.cli import main
from tigerharness.journal.deferred import (
    DeferredError,
    defer_entry,
    list_deferred,
    read_entry,
)
from tigerharness.journal.paths import (
    JournalPaths,
    JournalRootRefusal,
    resolve_team_journal_root,
)
from tigerharness.journal.scaffold import COMPILE_PERSONAS


PLAYBOOK = "# Minimal flow\n\nAnzai plans, then ships.\n"


def _make_team(root: Path, name: str = "Shohoku") -> Path:
    """Team dir with roster + prompts for the compile trio and a
    default playbook, under ``<root>/<name>`` (the
    TIGERHARNESS_TEAMS_DIR layout)."""
    team = root / name
    (team / "configs").mkdir(parents=True)
    personas = list(COMPILE_PERSONAS)
    lines = ["personas:\n"]
    for p in personas:
        lines.append(f"  - name: {p}\n")
    (team / "configs" / "personas.yaml").write_text("".join(lines))
    for p in personas:
        pdir = team / "personas" / p
        pdir.mkdir(parents=True)
        (pdir / "prompt.md").write_text(f"You are {p}.\n")
    (team / "workflow").mkdir()
    (team / "workflow" / "default.md").write_text(PLAYBOOK)
    return team


@pytest.fixture()
def team_env(tmp_path, monkeypatch):
    teams_dir = tmp_path / "teams"
    team = _make_team(teams_dir)
    monkeypatch.setenv("TIGERHARNESS_TEAMS_DIR", str(teams_dir))
    monkeypatch.delenv("TIGERHARNESS_JOURNAL_DIR", raising=False)
    return team


# ---------------------------------------------------------------------------
# resolve_team_journal_root -- the pinned resolver contract
# ---------------------------------------------------------------------------

class TestResolveTeamJournalRoot:
    def test_team_name_resolves_to_team_journal(self, team_env):
        root = resolve_team_journal_root(team="Shohoku")
        assert root == team_env / "journal"

    def test_explicit_team_dir_wins(self, team_env, tmp_path):
        other = _make_team(tmp_path / "elsewhere", name="Sai")
        assert resolve_team_journal_root(
            team="Shohoku", team_dir=other
        ) == other / "journal"

    def test_team_dir_without_personas_yaml_refuses(self, tmp_path):
        bogus = tmp_path / "not-a-team"
        bogus.mkdir()
        with pytest.raises(JournalRootRefusal) as exc:
            resolve_team_journal_root(team_dir=bogus)
        assert "personas.yaml" in str(exc.value)

    def test_no_team_root_found_refuses_not_falls_back(
        self, tmp_path, monkeypatch
    ):
        """The incident class: nothing resolvable must REFUSE, never
        land in the XDG state dir."""
        monkeypatch.delenv("TIGERHARNESS_TEAMS_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        with pytest.raises(JournalRootRefusal) as exc:
            resolve_team_journal_root(team="Shohoku")
        msg = str(exc.value)
        assert "refusing" in msg
        assert str(tmp_path) in msg  # names the cwd
        assert "--team-dir" in msg   # names the fix

    def test_wrong_team_cwd_refuses(self, tmp_path, monkeypatch):
        """Scheduling team X's work from team Y's root must refuse,
        not land in Y's journal."""
        monkeypatch.delenv("TIGERHARNESS_TEAMS_DIR", raising=False)
        team_y = _make_team(tmp_path, name="Sai")
        monkeypatch.chdir(team_y)
        with pytest.raises(JournalRootRefusal) as exc:
            resolve_team_journal_root(team="Shohoku")
        assert "Sai" in str(exc.value)
        assert "Shohoku" in str(exc.value)

    def test_needs_some_team_context(self):
        with pytest.raises(ValueError):
            resolve_team_journal_root()

    def test_team_name_traversal_is_refused(
        self, tmp_path, monkeypatch
    ):
        """Defense pin (b2): a path-shaped --team ("../outside/Victim")
        must never escape the teams dir into another root -- the
        name-mismatch check refuses it before anything is written."""
        teams = tmp_path / "teams"
        teams.mkdir()
        victim = tmp_path / "outside" / "Victim"
        (victim / "configs").mkdir(parents=True)
        (victim / "configs" / "personas.yaml").write_text("personas: []\n")
        monkeypatch.setenv("TIGERHARNESS_TEAMS_DIR", str(teams))
        with pytest.raises(JournalRootRefusal):
            resolve_team_journal_root(team="../outside/Victim")
        assert not (victim / "journal").exists()


# ---------------------------------------------------------------------------
# defer: the API-rail half
# ---------------------------------------------------------------------------

class TestDefer:
    def test_defer_writes_verbatim_payload_and_sidecar(self, team_env):
        paths = JournalPaths(root=team_env / "journal")
        payload = "> Operator said things\n\nverbatim, with whitespace  \n"
        entry = defer_entry(
            paths, title="Fix the thing", team="Shohoku",
            payload_text=payload, requester="dingyu",
            thread_ts="1781214421.526019",
        )
        assert (entry.path / "payload.md").read_text() == payload
        sidecar = json.loads((entry.path / "deferred.json").read_text())
        assert sidecar["team"] == "Shohoku"
        assert sidecar["playbook"] == "default"
        assert sidecar["thread_ts"] == "1781214421.526019"
        assert sidecar["journal_root"] == str((team_env / "journal").resolve())
        assert list_deferred(paths) == [entry.id]

    @pytest.mark.parametrize("kwargs", [
        dict(title="x", team="Shohoku", payload_text="   \n"),
        dict(title="  ", team="Shohoku", payload_text="hi"),
        dict(title="x", team=" ", payload_text="hi"),
    ])
    def test_defer_rejects_empties(self, team_env, kwargs):
        paths = JournalPaths(root=team_env / "journal")
        with pytest.raises(DeferredError):
            defer_entry(paths, **kwargs)

    def test_cli_defer_pins_team_and_reads_stdin(
        self, team_env, capsys, monkeypatch
    ):
        monkeypatch.setattr(
            "sys.stdin",
            __import__("io").StringIO("the verbatim ask\n"),
        )
        rc = main(["defer", "--title", "From Slack", "--team", "Shohoku"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Deferred:" in out
        assert list_deferred(JournalPaths(root=team_env / "journal"))

    def test_cli_defer_refuses_without_team_context(
        self, tmp_path, capsys, monkeypatch
    ):
        monkeypatch.delenv("TIGERHARNESS_TEAMS_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        rc = main([
            "defer", "--title", "x", "--team", "Ghost",
            "--payload-file", "/nonexistent-not-reached",
        ])
        assert rc == 2
        err = capsys.readouterr().err
        assert "refusing" in err

    def test_cli_defer_empty_payload_is_envelope_exit_1(
        self, team_env, capsys, monkeypatch
    ):
        monkeypatch.setattr(
            "sys.stdin", __import__("io").StringIO("   "),
        )
        rc = main(["defer", "--title", "x", "--team", "Shohoku"])
        assert rc == 1
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["ok"] is False
        assert envelope["errors"]

    def test_cli_defer_payload_file_happy_and_missing(
        self, team_env, capsys, tmp_path
    ):
        payload = tmp_path / "ask.md"
        payload.write_text("verbatim from a file\n")
        rc = main([
            "defer", "--title", "From file", "--team", "Shohoku",
            "--payload-file", str(payload),
        ])
        assert rc == 0
        capsys.readouterr()
        rc = main([
            "defer", "--title", "x", "--team", "Shohoku",
            "--payload-file", str(tmp_path / "ghost.md"),
        ])
        assert rc == 2
        assert "payload file not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# materialize: the subscription-rail half
# ---------------------------------------------------------------------------

class TestMaterialize:
    def _defer(self, team: Path, payload="Do the thing.\n") -> str:
        paths = JournalPaths(root=team / "journal")
        return defer_entry(
            paths, title="Deferred ask", team="Shohoku",
            payload_text=payload,
        ).id

    def test_lifecycle_and_indistinguishability(
        self, team_env, capsys, monkeypatch
    ):
        """defer -> sweep surfaces it -> materialize -> the task's
        status.json has EXACTLY the same shape as a journal-new
        workflow scaffold, the brief is the verbatim payload, the
        inbox entry is consumed, and the audit sidecar lands in the
        task dir."""
        journal = str(team_env / "journal")
        did = self._defer(team_env)

        rc = main(["--journal-dir", journal, "sweep"])
        assert rc == 0
        assert did in capsys.readouterr().out

        rc = main(["--journal-dir", journal, "materialize", did])
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "Materialized:" in out
        task_id = out.split("Materialized: ")[1].splitlines()[0].strip()

        paths = JournalPaths(root=team_env / "journal")
        task_dir = paths.active / task_id
        materialized = json.loads(
            (task_dir / "status.json").read_text()
        )
        assert (task_dir / "task_brief.md").read_text() == "Do the thing.\n"
        assert (task_dir / "deferred_origin.json").exists()
        assert list_deferred(paths) == []

        # Indistinguishability: scaffold the same brief via `new` and
        # compare status.json key sets and the load-bearing values.
        rc = main([
            "--journal-dir", journal, "new", "--kind", "workflow",
            "--title", "Direct ask", "--playbook", "default",
            "--task-brief", "Do the thing.\n", "--team", "Shohoku",
        ])
        out2 = capsys.readouterr().out
        assert rc == 0, out2
        direct_id = out2.split("Scaffolded: ")[1].splitlines()[0].strip()
        direct = json.loads(
            (paths.active / direct_id / "status.json").read_text()
        )
        assert set(materialized) == set(direct)
        for field in ("kind", "state", "compile_pending", "compile_phase",
                      "max_sessions", "early_exit", "autonomy",
                      "playbook_name", "journal_root"):
            assert materialized[field] == direct[field], field
        # Provenance survives materialization (item 5's invariant):
        # the deferred entry's recorded root and the task's agree.
        origin = json.loads(
            (task_dir / "deferred_origin.json").read_text()
        )
        assert materialized["journal_root"] == origin["journal_root"]

    def test_malformed_sidecar_exits_1_and_entry_stays(
        self, team_env, capsys
    ):
        journal = str(team_env / "journal")
        did = self._defer(team_env)
        paths = JournalPaths(root=team_env / "journal")
        sidecar = paths.deferred / did / "deferred.json"
        sidecar.write_text("{not json")
        rc = main(["--journal-dir", journal, "materialize", did])
        assert rc == 1
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["ok"] is False
        assert list_deferred(paths) == [did]  # stays for repair

    def test_missing_required_field_exits_1(self, team_env, capsys):
        journal = str(team_env / "journal")
        did = self._defer(team_env)
        paths = JournalPaths(root=team_env / "journal")
        sidecar = paths.deferred / did / "deferred.json"
        data = json.loads(sidecar.read_text())
        data["team"] = ""
        sidecar.write_text(json.dumps(data))
        rc = main(["--journal-dir", journal, "materialize", did])
        assert rc == 1
        envelope = json.loads(capsys.readouterr().out)
        assert any("team" in e for e in envelope["errors"])

    def test_unknown_playbook_exits_1_and_entry_stays(
        self, team_env, capsys
    ):
        journal = str(team_env / "journal")
        did = self._defer(team_env)
        paths = JournalPaths(root=team_env / "journal")
        sidecar = paths.deferred / did / "deferred.json"
        data = json.loads(sidecar.read_text())
        data["playbook"] = "ghost"
        sidecar.write_text(json.dumps(data))
        rc = main(["--journal-dir", journal, "materialize", did])
        assert rc == 1
        envelope = json.loads(capsys.readouterr().out)
        assert any("ghost" in e for e in envelope["errors"])
        assert list_deferred(paths) == [did]

    def test_double_materialize_second_attempt_fails_cleanly(
        self, team_env, capsys
    ):
        """Defense pin (b2): materializing the same entry twice cannot
        double-deliver -- the entry was consumed, the second attempt
        is an envelope, no duplicate task appears."""
        journal = str(team_env / "journal")
        did = self._defer(team_env)
        rc = main(["--journal-dir", journal, "materialize", did])
        assert rc == 0
        capsys.readouterr()
        paths = JournalPaths(root=team_env / "journal")
        tasks_after_first = sorted(
            p.name for p in (paths.active).iterdir()
        )
        rc = main(["--journal-dir", journal, "materialize", did])
        assert rc == 1
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["ok"] is False
        assert sorted(
            p.name for p in (paths.active).iterdir()
        ) == tasks_after_first

    def test_unknown_id_exits_1(self, team_env, capsys):
        journal = str(team_env / "journal")
        rc = main(["--journal-dir", journal, "materialize", "nope-123"])
        assert rc == 1
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["ok"] is False

    def test_unsafe_id_exits_1(self, team_env, capsys):
        journal = str(team_env / "journal")
        rc = main(["--journal-dir", journal, "materialize", "../../etc"])
        assert rc == 1
        envelope = json.loads(capsys.readouterr().out)
        assert any("unsafe" in e for e in envelope["errors"])

    def test_missing_payload_file_is_deferred_error(self, team_env):
        paths = JournalPaths(root=team_env / "journal")
        did = self._defer(team_env)
        (paths.deferred / did / "payload.md").unlink()
        with pytest.raises(DeferredError):
            read_entry(paths, did)

    def test_emptied_payload_is_deferred_error(self, team_env):
        """defer rejects empty payloads, but a hand-emptied file must
        still fail loudly at read time."""
        paths = JournalPaths(root=team_env / "journal")
        did = self._defer(team_env)
        (paths.deferred / did / "payload.md").write_text("  \n")
        with pytest.raises(DeferredError, match="empty"):
            read_entry(paths, did)

    def test_missing_compile_persona_exits_1_and_entry_stays(
        self, team_env, capsys
    ):
        """The persona preflight runs at materialization (not at
        defer): a roster gap surfaces as the envelope, entry kept."""
        journal = str(team_env / "journal")
        did = self._defer(team_env)
        (team_env / "personas" / "Akagi" / "prompt.md").unlink()
        rc = main(["--journal-dir", journal, "materialize", did])
        assert rc == 1
        envelope = json.loads(capsys.readouterr().out)
        assert any("Akagi" in e for e in envelope["errors"])
        paths = JournalPaths(root=team_env / "journal")
        assert list_deferred(paths) == [did]

    def test_unreadable_sidecar_and_payload_are_deferred_errors(
        self, team_env, monkeypatch
    ):
        paths = JournalPaths(root=team_env / "journal")
        did = self._defer(team_env)
        real_read = Path.read_text

        def denied(self, *a, **k):
            if self.name == "deferred.json":
                raise OSError("locked")
            return real_read(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", denied)
        with pytest.raises(DeferredError, match="unreadable"):
            read_entry(paths, did)

        def denied_payload(self, *a, **k):
            if self.name == "payload.md":
                raise OSError("locked")
            return real_read(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", denied_payload)
        with pytest.raises(DeferredError, match="unreadable"):
            read_entry(paths, did)

    def test_non_object_sidecar_is_deferred_error(self, team_env):
        paths = JournalPaths(root=team_env / "journal")
        did = self._defer(team_env)
        (paths.deferred / did / "deferred.json").write_text("[1, 2]")
        with pytest.raises(DeferredError, match="JSON object"):
            read_entry(paths, did)

    def test_scaffolder_failure_keeps_entry_and_envelopes(
        self, team_env, capsys, monkeypatch
    ):
        """Any scaffolder exception maps to the envelope and the inbox
        entry survives for retry -- never half-scaffolded."""
        from tigerharness.journal import cli as cli_mod
        from tigerharness.journal.scaffold import JournalScaffoldError

        journal = str(team_env / "journal")
        did = self._defer(team_env)

        def boom(**kwargs):
            raise JournalScaffoldError("disk full")

        monkeypatch.setattr(cli_mod, "new_workflow_task", boom)
        rc = main(["--journal-dir", journal, "materialize", did])
        assert rc == 1
        envelope = json.loads(capsys.readouterr().out)
        assert "disk full" in envelope["errors"][0]
        paths = JournalPaths(root=team_env / "journal")
        assert list_deferred(paths) == [did]


class TestSweepJsonIncludesDeferred:
    def test_json_payload_lists_deferred(self, team_env, capsys):
        journal = str(team_env / "journal")
        paths = JournalPaths(root=team_env / "journal")
        entry = defer_entry(
            paths, title="x", team="Shohoku", payload_text="y",
        )
        rc = main(["--journal-dir", journal, "sweep", "--format", "json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["deferred"] == [entry.id]

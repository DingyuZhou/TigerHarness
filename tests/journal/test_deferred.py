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
        with pytest.raises(JournalRootRefusal) as exc:
            resolve_team_journal_root(team="../outside/Victim")
        assert not (victim / "journal").exists()
        # The message tells the truth (b2 finding): it's a
        # name-vs-directory mismatch, not a wrong-cwd situation.
        msg = str(exc.value)
        assert "cwd is team" not in msg
        assert "resolves to a directory named" in msg


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

    def test_defer_records_channel_in_sidecar_and_entry(self, team_env):
        paths = JournalPaths(root=team_env / "journal")
        entry = defer_entry(
            paths, title="With channel", team="Shohoku",
            payload_text="hi", thread_ts="1786460709.118669",
            channel="D0B4L5V7RFG",
        )
        sidecar = json.loads((entry.path / "deferred.json").read_text())
        assert sidecar["channel"] == "D0B4L5V7RFG"
        assert entry.channel == "D0B4L5V7RFG"
        assert read_entry(paths, entry.id).channel == "D0B4L5V7RFG"

    def test_defer_channel_defaults_empty_and_old_sidecars_read(
        self, team_env
    ):
        """Old sidecars predate the channel field; reading one must
        yield "" rather than KeyError."""
        paths = JournalPaths(root=team_env / "journal")
        entry = defer_entry(
            paths, title="No channel", team="Shohoku", payload_text="hi",
        )
        sidecar_path = entry.path / "deferred.json"
        assert json.loads(sidecar_path.read_text())["channel"] == ""
        data = json.loads(sidecar_path.read_text())
        del data["channel"]  # simulate a pre-channel sidecar
        sidecar_path.write_text(json.dumps(data))
        assert read_entry(paths, entry.id).channel == ""

    def test_cli_defer_channel_flag_and_env_fallback(
        self, team_env, capsys, monkeypatch, tmp_path
    ):
        paths = JournalPaths(root=team_env / "journal")
        payload = tmp_path / "ask.md"
        payload.write_text("verbatim\n")
        monkeypatch.setenv("TIGERHARNESS_SLACK_CHANNEL", "DENVCHAN")
        rc = main([
            "defer", "--title", "flag wins", "--team", "Shohoku",
            "--payload-file", str(payload), "--channel", "DFLAGCHAN",
        ])
        assert rc == 0
        capsys.readouterr()
        entry_id = list_deferred(paths)[-1]
        assert read_entry(paths, entry_id).channel == "DFLAGCHAN"

        rc = main([
            "defer", "--title", "env fallback", "--team", "Shohoku",
            "--payload-file", str(payload),
        ])
        assert rc == 0
        capsys.readouterr()
        by_channel = {
            read_entry(paths, did).title: read_entry(paths, did).channel
            for did in list_deferred(paths)
        }
        assert by_channel["env fallback"] == "DENVCHAN"

        monkeypatch.delenv("TIGERHARNESS_SLACK_CHANNEL", raising=False)
        rc = main([
            "defer", "--title", "no channel anywhere", "--team", "Shohoku",
            "--payload-file", str(payload),
        ])
        assert rc == 0
        capsys.readouterr()
        by_channel = {
            read_entry(paths, did).title: read_entry(paths, did).channel
            for did in list_deferred(paths)
        }
        assert by_channel["no channel anywhere"] == ""

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

    def test_pre_kind_sidecar_defaults_to_workflow_and_empty_persona(
        self, team_env
    ):
        """The single most important line in the --kind change: an
        entry written before the kind/persona keys existed must still
        read, and must read as a WORKFLOW.

        The sidecar here is authored from a literal pre-change key set
        rather than written-then-edited. A write-then-delete fixture is
        derived from the current writer, so it would keep passing if
        `defer_entry` ever stopped emitting the keys -- proving
        nothing. This one is a genuine old-format artifact.
        """
        paths = JournalPaths(root=team_env / "journal")
        entry = defer_entry(
            paths, title="Pre-kind", team="Shohoku", payload_text="hi",
        )
        (entry.path / "deferred.json").write_text(json.dumps({
            "id": entry.id,
            "title": "Pre-kind",
            "team": "Shohoku",
            "playbook": "default",
            "requester": "",
            "thread_ts": "",
            "channel": "",
            "created_at": entry.created_at,
            "journal_root": str((team_env / "journal").resolve()),
        }))
        reread = read_entry(paths, entry.id)
        assert reread.kind == "workflow"
        assert reread.persona == ""

    def test_unknown_kind_in_sidecar_is_deferred_error(self, team_env):
        """An absent kind means workflow (back-compat). A kind that is
        present but not a real lane is a malformation, not a silent
        workflow -- hand-editing `kind: quick` must not buy a 19-step
        compile for a half-hour job."""
        paths = JournalPaths(root=team_env / "journal")
        entry = defer_entry(
            paths, title="Bogus", team="Shohoku", payload_text="hi",
        )
        sidecar_path = entry.path / "deferred.json"
        data = json.loads(sidecar_path.read_text())
        data["kind"] = "quick"
        sidecar_path.write_text(json.dumps(data))
        with pytest.raises(DeferredError, match="expected 'workflow'"):
            read_entry(paths, entry.id)

    def test_cli_defer_task_kind_without_persona_exits_2(
        self, team_env, capsys, monkeypatch
    ):
        monkeypatch.setattr(
            "sys.stdin", __import__("io").StringIO("quick ask\n"),
        )
        rc = main([
            "defer", "--title", "x", "--team", "Shohoku",
            "--kind", "task",
        ])
        assert rc == 2
        assert "--persona is required" in capsys.readouterr().err
        # A rejected defer must not park a half-formed entry.
        assert list_deferred(JournalPaths(root=team_env / "journal")) == []

    def test_cli_defer_workflow_kind_with_persona_exits_2(
        self, team_env, capsys, monkeypatch
    ):
        monkeypatch.setattr(
            "sys.stdin", __import__("io").StringIO("an ask\n"),
        )
        rc = main([
            "defer", "--title", "x", "--team", "Shohoku",
            "--kind", "workflow", "--persona", "Akagi",
        ])
        assert rc == 2
        err = capsys.readouterr().err
        assert "--persona is task-only" in err
        assert "default_captain" in err
        assert list_deferred(JournalPaths(root=team_env / "journal")) == []

    def test_cli_defer_task_kind_with_playbook_exits_2(
        self, team_env, capsys, monkeypatch
    ):
        """Passing the value that IS the old default is the whole
        point: this can only be rejected if an explicit --playbook is
        distinguishable from no flag at all."""
        monkeypatch.setattr(
            "sys.stdin", __import__("io").StringIO("quick ask\n"),
        )
        rc = main([
            "defer", "--title", "x", "--team", "Shohoku",
            "--kind", "task", "--persona", "Akagi",
            "--playbook", "default",
        ])
        assert rc == 2
        assert "--playbook is workflow-only" in capsys.readouterr().err
        assert list_deferred(JournalPaths(root=team_env / "journal")) == []

    def test_defer_without_kind_flags_records_todays_sidecar(
        self, team_env
    ):
        """No new flags -> the recorded playbook is still exactly
        "default" (the None sentinel never reaches disk) and the lane
        is still workflow."""
        paths = JournalPaths(root=team_env / "journal")
        entry = defer_entry(
            paths, title="Unchanged", team="Shohoku", payload_text="hi",
        )
        sidecar = json.loads((entry.path / "deferred.json").read_text())
        assert sidecar["playbook"] == "default"
        assert sidecar["kind"] == "workflow"
        assert sidecar["persona"] == ""


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

    def test_origin_sidecar_carries_thread_and_channel(
        self, team_env, capsys
    ):
        """The wrong-thread fix rides on this: the materialized task's
        deferred_origin.json must keep the Slack origin (thread_ts +
        channel) so notify --task can route back to it."""
        journal = str(team_env / "journal")
        paths = JournalPaths(root=team_env / "journal")
        did = defer_entry(
            paths, title="Slack ask", team="Shohoku",
            payload_text="Do it.\n", thread_ts="1786460709.118669",
            channel="D0B4L5V7RFG",
        ).id
        rc = main(["--journal-dir", journal, "materialize", did])
        out = capsys.readouterr().out
        assert rc == 0, out
        task_id = out.split("Materialized: ")[1].splitlines()[0].strip()
        origin = json.loads(
            (paths.active / task_id / "deferred_origin.json").read_text()
        )
        assert origin["thread_ts"] == "1786460709.118669"
        assert origin["channel"] == "D0B4L5V7RFG"

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

    # -- the two lanes, end to end ------------------------------------

    def test_defer_then_materialize_workflow_end_to_end(
        self, team_env, capsys
    ):
        """No new flags -> the workflow lane, unchanged, still printing
        `playbook:` and NOT `persona:`."""
        journal = str(team_env / "journal")
        did = self._defer(team_env)
        rc = main(["--journal-dir", journal, "materialize", did])
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "  kind:         workflow" in out
        assert "  playbook:     default" in out
        assert "  persona:" not in out

        paths = JournalPaths(root=team_env / "journal")
        task_id = out.split("Materialized: ")[1].splitlines()[0].strip()
        status = json.loads(
            (paths.active / task_id / "status.json").read_text()
        )
        assert status["kind"] == "workflow"
        assert status["compile_pending"] is True
        assert list_deferred(paths) == []

    def test_defer_then_materialize_task_end_to_end(
        self, team_env, capsys
    ):
        """--kind task reaches the Quick lane: one persona, no compile,
        no playbook lookup, and `persona:` printed in place of
        `playbook:`."""
        journal = str(team_env / "journal")
        paths = JournalPaths(root=team_env / "journal")
        did = defer_entry(
            paths, title="Quick ask", team="Shohoku",
            payload_text="Fix the thing.\n",
            kind="task", persona="Akagi",
        ).id
        rc = main(["--journal-dir", journal, "materialize", did])
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "  kind:         task" in out
        assert "  persona:      Akagi" in out
        assert "  playbook:" not in out

        task_id = out.split("Materialized: ")[1].splitlines()[0].strip()
        task_dir = paths.active / task_id
        status = json.loads((task_dir / "status.json").read_text())
        assert status["kind"] == "task"
        assert status["persona"] == "Akagi"
        assert status["early_exit"] is True
        assert status["autonomy"] == "judgement"
        assert status["max_sessions"] == 3
        # A Quick task has no compile phase at all -- the key is absent
        # from the schema, not merely false.
        assert "compile_pending" not in status
        assert not (task_dir / "steps").exists()
        assert (task_dir / "task.md").read_text() == "Fix the thing.\n"
        # Provenance survives the Quick lane too -- `journal release`
        # reads this to thread a completion notice back under the
        # Operator's original Slack ask.
        assert (task_dir / "deferred_origin.json").exists()
        assert list_deferred(paths) == []

    def test_materialize_unknown_persona_exits_1_and_entry_stays(
        self, team_env, capsys
    ):
        """The roster check runs at materialize, not at defer: a name
        typed in Slack hours ago fails here, naming the persona and
        personas.yaml, and the entry survives for repair."""
        journal = str(team_env / "journal")
        paths = JournalPaths(root=team_env / "journal")
        did = defer_entry(
            paths, title="Quick ask", team="Shohoku",
            payload_text="Fix the thing.\n",
            kind="task", persona="Nobody",
        ).id
        rc = main(["--journal-dir", journal, "materialize", did])
        assert rc == 1
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["ok"] is False
        assert "Nobody" in envelope["errors"][0]
        assert "personas.yaml" in envelope["errors"][0]
        assert list_deferred(paths) == [did]

    def test_task_materializes_with_no_workflow_dir_at_all(
        self, team_env, capsys
    ):
        """The discriminating test for 'the playbook-missing error path
        must NOT fire': delete the team's whole workflow/ directory and
        a Quick task still materializes. With a playbook present, a
        materialize that wrongly consulted it would pass unnoticed."""
        import shutil

        journal = str(team_env / "journal")
        paths = JournalPaths(root=team_env / "journal")
        did = defer_entry(
            paths, title="Quick ask", team="Shohoku",
            payload_text="Fix the thing.\n",
            kind="task", persona="Akagi",
        ).id
        shutil.rmtree(team_env / "workflow")
        rc = main(["--journal-dir", journal, "materialize", did])
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "  kind:         task" in out
        assert list_deferred(paths) == []

    @pytest.mark.parametrize("bad", [
        "../../Other/personas/Akagi",
        "a/b",
        "a\\b",
        "",
    ])
    def test_materialize_refuses_persona_that_is_a_path(
        self, team_env, capsys, bad
    ):
        """The persona arrives over the Slack rail and becomes a path
        component. A `..` segment would otherwise reach a sibling
        team's personas/ and materialize a task across the team
        boundary."""
        journal = str(team_env / "journal")
        paths = JournalPaths(root=team_env / "journal")
        did = defer_entry(
            paths, title="Quick ask", team="Shohoku",
            payload_text="Fix the thing.\n",
            kind="task", persona=bad,
        ).id
        rc = main(["--journal-dir", journal, "materialize", did])
        assert rc == 1
        envelope = json.loads(capsys.readouterr().out)
        assert "personas.yaml" in envelope["errors"][0]
        assert list_deferred(paths) == [did]

    def test_task_scaffolder_failure_keeps_entry_and_envelopes(
        self, team_env, capsys, monkeypatch
    ):
        """The Quick lane gets the same never-half-scaffolded contract
        as the workflow lane."""
        from tigerharness.journal import cli as cli_mod
        from tigerharness.journal.scaffold import JournalScaffoldError

        journal = str(team_env / "journal")
        paths = JournalPaths(root=team_env / "journal")
        did = defer_entry(
            paths, title="Quick ask", team="Shohoku",
            payload_text="Fix the thing.\n",
            kind="task", persona="Akagi",
        ).id

        def boom(**kwargs):
            raise JournalScaffoldError("disk full")

        monkeypatch.setattr(cli_mod, "new_task", boom)
        rc = main(["--journal-dir", journal, "materialize", did])
        assert rc == 1
        envelope = json.loads(capsys.readouterr().out)
        assert "disk full" in envelope["errors"][0]
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

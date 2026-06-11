"""T8 scheduler tests: cadence math (DST-pinned), definition
round-trip, exactly-once materialization (deterministic race),
run-late-once, skip-if-in-flight, crash recovery, CLI verbs."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tigerharness.journal.cli import main
from tigerharness.journal.models import Status
from tigerharness.journal.paths import JournalPaths
from tigerharness.journal.schedule import (
    MaterializeResult,
    ScheduleDef,
    ScheduleDefError,
    def_path,
    list_def_ids,
    materialize_due,
    next_occurrence,
    save_def,
    schedule_dir,
)
from tigerharness.journal.sweep import sweep

NY = ZoneInfo("America/New_York")
UTC = dt.timezone.utc


def _utc(y, m, d, h=0, mi=0):
    return dt.datetime(y, m, d, h, mi, tzinfo=UTC)


def _iso(d_):
    return d_.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _task_def(
    def_id="morning-check", *, next_due, enabled=True, period="daily",
    at="08:00", autonomy="ask", materializing=None, last=None,
):
    return ScheduleDef(
        id=def_id,
        title="Morning check",
        period=period,
        at=at,
        next_due=next_due,
        payload={
            "kind": "task",
            "prd_text": "# Morning check\nLook at the logs.\n",
            "persona": "Ayako",
            "max_sessions": 2,
            "early_exit": True,
            "autonomy": autonomy,
        },
        enabled=enabled,
        materializing=materializing,
        last=last,
    )


@pytest.fixture
def paths(tmp_path) -> JournalPaths:
    return JournalPaths(root=tmp_path / "journal")


class TestNextOccurrence:
    def test_same_day_future_time(self):
        # 06:00 NY = 11:00 UTC (EST? March pre-DST: EST=UTC-5).
        after = dt.datetime(2026, 1, 10, 6, 0, tzinfo=NY)
        nxt = next_occurrence(
            after.astimezone(UTC), period="daily", at="08:00", tz=NY,
        )
        local = nxt.astimezone(NY)
        assert (local.hour, local.minute) == (8, 0)
        assert local.date() == dt.date(2026, 1, 10)

    def test_past_time_rolls_to_next_day(self):
        after = dt.datetime(2026, 1, 10, 9, 0, tzinfo=NY)
        nxt = next_occurrence(
            after.astimezone(UTC), period="daily", at="08:00", tz=NY,
        )
        local = nxt.astimezone(NY)
        assert local.date() == dt.date(2026, 1, 11)
        assert local.hour == 8

    def test_weekly_advances_seven_days(self):
        after = dt.datetime(2026, 1, 10, 9, 0, tzinfo=NY)
        nxt = next_occurrence(
            after.astimezone(UTC), period="weekly", at="08:00", tz=NY,
        )
        assert nxt.astimezone(NY).date() == dt.date(2026, 1, 17)

    def test_dst_spring_forward_keeps_wall_clock(self):
        # US DST starts 2026-03-08: 08:00 EST (UTC-5) the day before,
        # 08:00 EDT (UTC-4) the day after. Wall clock holds; the UTC
        # offset shifts -- exactly what "+86400s" would get wrong.
        after = dt.datetime(2026, 3, 7, 9, 0, tzinfo=NY)
        nxt = next_occurrence(
            after.astimezone(UTC), period="daily", at="08:00", tz=NY,
        )
        local = nxt.astimezone(NY)
        assert local.date() == dt.date(2026, 3, 8)
        assert (local.hour, local.minute) == (8, 0)
        assert nxt.hour == 12  # 08:00 EDT == 12:00 UTC

    def test_dst_fall_back_keeps_wall_clock(self):
        # US DST ends 2026-11-01.
        after = dt.datetime(2026, 10, 31, 9, 0, tzinfo=NY)
        nxt = next_occurrence(
            after.astimezone(UTC), period="daily", at="08:00", tz=NY,
        )
        local = nxt.astimezone(NY)
        assert local.date() == dt.date(2026, 11, 1)
        assert (local.hour, local.minute) == (8, 0)
        assert nxt.hour == 13  # 08:00 EST == 13:00 UTC

    def test_invalid_cadence_rejected(self):
        with pytest.raises(ScheduleDefError):
            next_occurrence(_utc(2026, 1, 1), period="hourly", at="08:00")
        with pytest.raises(ScheduleDefError):
            next_occurrence(_utc(2026, 1, 1), period="daily", at="8am")


class TestScheduleDefRoundTrip:
    def test_round_trip(self):
        d = _task_def(next_due=_iso(_utc(2026, 6, 12, 12)))
        again = ScheduleDef.from_json(d.to_json())
        assert again == d

    def test_optional_fields_suppressed(self):
        d = _task_def(next_due=_iso(_utc(2026, 6, 12, 12)))
        assert "materializing" not in d.to_dict()
        assert "last" not in d.to_dict()

    def test_missing_keys_rejected(self):
        with pytest.raises(ScheduleDefError, match="missing"):
            ScheduleDef.from_dict({"id": "x"})

    def test_unknown_keys_rejected(self):
        d = _task_def(next_due=_iso(_utc(2026, 6, 12, 12))).to_dict()
        d["surprise"] = 1
        with pytest.raises(ScheduleDefError, match="unknown"):
            ScheduleDef.from_dict(d)

    def test_bad_payload_rejected(self):
        d = _task_def(next_due=_iso(_utc(2026, 6, 12, 12)))
        d.payload = {"kind": "task"}  # no prd_text/persona
        with pytest.raises(ScheduleDefError, match="missing"):
            d.validate()
        d.payload = {"kind": "magic"}
        with pytest.raises(ScheduleDefError, match="kind"):
            d.validate()

    def test_bad_max_sessions_rejected_at_validate(self):
        # b2-sakuragi finding 1: fail at add time, not at 8am.
        d = _task_def(next_due=_iso(_utc(2026, 6, 12, 12)))
        d.payload["max_sessions"] = -1
        with pytest.raises(ScheduleDefError, match="max_sessions"):
            d.validate()
        d.payload["max_sessions"] = "three"
        with pytest.raises(ScheduleDefError, match="max_sessions"):
            d.validate()

    def test_cli_add_rejects_bad_max_sessions(self, tmp_path, capsys):
        journal_dir = tmp_path / "journal"
        prd = tmp_path / "b.md"
        prd.write_text("x")
        rc = main(["--journal-dir", str(journal_dir),
                   "schedule", "add", "--title", "Bad", "--at", "08:00",
                   "--kind", "task", "--prd", str(prd),
                   "--persona", "P", "--max-sessions", "-1"])
        assert rc == 1
        assert "max_sessions" in capsys.readouterr().err
        assert not (journal_dir / "schedule" / "bad.json").exists()

    def test_bad_autonomy_rejected(self):
        d = _task_def(
            next_due=_iso(_utc(2026, 6, 12, 12)), autonomy="ask",
        )
        d.payload["autonomy"] = "yolo"
        with pytest.raises(ScheduleDefError, match="autonomy"):
            d.validate()

    def test_bad_next_due_and_blank_id(self):
        d = _task_def(next_due="not-a-date")
        with pytest.raises(ScheduleDefError, match="ISO"):
            d.validate()
        d2 = _task_def(next_due=_iso(_utc(2026, 6, 12)))
        d2.id = "  "
        with pytest.raises(ScheduleDefError, match="required"):
            d2.validate()

    def test_not_json_and_not_object(self):
        with pytest.raises(ScheduleDefError, match="JSON"):
            ScheduleDef.from_json("{nope")
        with pytest.raises(ScheduleDefError, match="object"):
            ScheduleDef.from_dict([1, 2])  # type: ignore[arg-type]

    def test_bad_cadence_on_validate(self):
        d = _task_def(next_due=_iso(_utc(2026, 6, 12)), period="daily")
        d.at = "25:99"
        with pytest.raises(ScheduleDefError, match="cadence"):
            d.validate()


class TestStoreHelpers:
    def test_list_empty_without_dir(self, paths):
        assert list_def_ids(paths) == []

    def test_save_and_list(self, paths):
        d = _task_def(next_due=_iso(_utc(2026, 6, 12, 12)))
        p = save_def(paths, d)
        assert p == def_path(paths, d.id)
        assert list_def_ids(paths) == [d.id]
        assert schedule_dir(paths).is_dir()


class TestMaterializeDue:
    NOW = _iso(_utc(2026, 6, 12, 20))  # 16:00 NY (EDT)

    def _materialized_status(self, paths, task_id) -> Status:
        return Status.from_json(
            paths.status_json(task_id).read_text()
        )

    def test_not_due_and_disabled_skip(self, paths):
        save_def(paths, _task_def(
            "future", next_due=_iso(_utc(2026, 6, 13, 12))))
        save_def(paths, _task_def(
            "off", next_due=_iso(_utc(2026, 6, 1, 12)), enabled=False))
        res = materialize_due(paths, now=self.NOW, tz=NY)
        assert res.materialized == []
        assert res.skipped_in_flight == []

    def test_due_materializes_once_run_late(self, paths):
        # Due at 08:00, first sweep at 16:00 local: runs ONCE, and
        # next_due advances to TOMORROW 08:00 local (never backfilled).
        # An unrelated active task exercises the in-flight check's
        # non-matching branch on the way.
        other = paths.active / "other-task"
        other.mkdir(parents=True)
        (other / "status.json").write_text(
            Status.new(id="other-task", title="t", persona="P").to_json()
        )
        save_def(paths, _task_def(next_due=_iso(_utc(2026, 6, 12, 12))))
        res = materialize_due(paths, now=self.NOW, tz=NY)
        assert len(res.materialized) == 1
        task_id = res.materialized[0]
        st = self._materialized_status(paths, task_id)
        assert st.schedule_def == "morning-check"
        assert st.schedule_due == _iso(_utc(2026, 6, 12, 12))
        assert st.autonomy == "ask"
        assert st.early_exit is True
        d = ScheduleDef.from_json(
            def_path(paths, "morning-check").read_text())
        local = dt.datetime.fromisoformat(
            d.next_due.replace("Z", "+00:00")).astimezone(NY)
        assert local.date() == dt.date(2026, 6, 13)
        assert local.hour == 8
        assert d.materializing is None
        assert d.last["task_id"] == task_id
        # Second sweep, same now: nothing due anymore.
        res2 = materialize_due(paths, now=self.NOW, tz=NY)
        assert res2.materialized == []

    def test_skip_if_in_flight_no_advance(self, paths):
        save_def(paths, _task_def(next_due=_iso(_utc(2026, 6, 12, 12))))
        res = materialize_due(paths, now=self.NOW, tz=NY)
        task_id = res.materialized[0]
        # Force the def due again while the instance is still active.
        d = ScheduleDef.from_json(
            def_path(paths, "morning-check").read_text())
        d.next_due = _iso(_utc(2026, 6, 12, 13))
        save_def(paths, d)
        res2 = materialize_due(paths, now=self.NOW, tz=NY)
        assert res2.materialized == []
        assert res2.skipped_in_flight == ["morning-check"]
        d2 = ScheduleDef.from_json(
            def_path(paths, "morning-check").read_text())
        assert d2.next_due == _iso(_utc(2026, 6, 12, 13))  # NOT advanced
        # Instance finishes (archive it) -> next sweep fires.
        st = self._materialized_status(paths, task_id)
        from dataclasses import replace
        from tigerharness.journal.models import State
        done = replace(st, state=State.DONE)
        paths.status_json(task_id).write_text(done.to_json())
        paths.archive(task_id)
        res3 = materialize_due(paths, now=self.NOW, tz=NY)
        assert len(res3.materialized) == 1

    def test_exactly_once_under_deterministic_race(self, paths):
        """Two materializers interleaved at the CAS seam: exactly one
        scaffolds."""
        save_def(paths, _task_def(next_due=_iso(_utc(2026, 6, 12, 12))))

        first_done = False

        def racing_hook(phase: str, def_id: str) -> None:
            # At the loser's phase A (between computing the write and
            # the save), let the WINNER run to completion first.
            nonlocal first_done
            if phase == "A" and not first_done:
                first_done = True
                materialize_due(paths, now=self.NOW, tz=NY)

        res_loser = materialize_due(
            paths, now=self.NOW, tz=NY, cas_hook=racing_hook,
        )
        # The hook's inner run won; the outer one lost the CAS re-read.
        assert res_loser.materialized == []
        active = list(paths.active.glob("*/status.json"))
        assert len(active) == 1  # exactly one task, total

    def test_recovery_completes_lost_run(self, paths):
        # Crash after phase A, before B: stale intent, nothing
        # scaffolded -> recovery completes phase B (branch c, not found).
        due = _iso(_utc(2026, 6, 12, 12))
        save_def(paths, _task_def(
            next_due=_iso(_utc(2026, 6, 13, 12)),  # already advanced
            materializing={
                "token": "tok", "started_at": _iso(_utc(2026, 6, 12, 8)),
                "due": due,
            },
        ))
        res = materialize_due(paths, now=self.NOW, tz=NY)
        assert len(res.materialized) == 1
        d = ScheduleDef.from_json(
            def_path(paths, "morning-check").read_text())
        assert d.materializing is None
        assert d.last["due"] == due

    def test_recovery_fresh_intent_left_alone(self, paths):
        due = _iso(_utc(2026, 6, 12, 12))
        save_def(paths, _task_def(
            next_due=_iso(_utc(2026, 6, 13, 12)),
            materializing={
                "token": "tok", "started_at": self.NOW, "due": due,
            },
        ))
        res = materialize_due(
            paths, now=self.NOW, tz=NY, stuck_timeout_sec=1800,
        )
        assert res.materialized == []
        d = ScheduleDef.from_json(
            def_path(paths, "morning-check").read_text())
        assert d.materializing is not None  # untouched

    def test_recovery_finds_active_instance(self, paths):
        # Crash after scaffold, before close: the instance exists in
        # active/ stamped with (def, due) -> branch (a): close only.
        due = _iso(_utc(2026, 6, 12, 12))
        save_def(paths, _task_def(next_due=due))
        res = materialize_due(paths, now=self.NOW, tz=NY)
        task_id = res.materialized[0]
        # Simulate the crash: restore the intent, erase `last`.
        d = ScheduleDef.from_json(
            def_path(paths, "morning-check").read_text())
        d.materializing = {
            "token": "tok", "started_at": _iso(_utc(2026, 6, 12, 8)),
            "due": due,
        }
        d.last = None
        save_def(paths, d)
        res2 = materialize_due(paths, now=self.NOW, tz=NY)
        assert res2.materialized == []  # nothing NEW scaffolded
        d2 = ScheduleDef.from_json(
            def_path(paths, "morning-check").read_text())
        assert d2.materializing is None
        assert d2.last["task_id"] == task_id

    def test_recovery_last_due_branch(self, paths):
        # Branch (b): last.due >= intent.due -> cleared crash duplicate.
        due = _iso(_utc(2026, 6, 12, 12))
        save_def(paths, _task_def(
            next_due=_iso(_utc(2026, 6, 13, 12)),
            materializing={
                "token": "tok", "started_at": _iso(_utc(2026, 6, 12, 8)),
                "due": due,
            },
            last={"task_id": "t-old", "due": due,
                  "at": _iso(_utc(2026, 6, 12, 9))},
        ))
        res = materialize_due(paths, now=self.NOW, tz=NY)
        assert res.materialized == []
        d = ScheduleDef.from_json(
            def_path(paths, "morning-check").read_text())
        assert d.materializing is None

    def test_recovery_finds_archived_instance(self, paths):
        # Branch (c) found: the instance already finished and archived.
        due = _iso(_utc(2026, 6, 12, 12))
        save_def(paths, _task_def(next_due=due))
        res = materialize_due(paths, now=self.NOW, tz=NY)
        task_id = res.materialized[0]
        st = Status.from_json(paths.status_json(task_id).read_text())
        from dataclasses import replace
        from tigerharness.journal.models import State
        paths.status_json(task_id).write_text(
            replace(st, state=State.DONE).to_json())
        paths.archive(task_id)
        d = ScheduleDef.from_json(
            def_path(paths, "morning-check").read_text())
        d.materializing = {
            "token": "tok", "started_at": _iso(_utc(2026, 6, 12, 8)),
            "due": due,
        }
        d.last = None
        save_def(paths, d)
        res2 = materialize_due(paths, now=self.NOW, tz=NY)
        assert res2.materialized == []
        d2 = ScheduleDef.from_json(
            def_path(paths, "morning-check").read_text())
        assert d2.materializing is None
        assert d2.last["task_id"] == task_id

    def test_recovery_unreadable_intent_is_stale(self, paths):
        due = _iso(_utc(2026, 6, 12, 12))
        save_def(paths, _task_def(
            next_due=_iso(_utc(2026, 6, 13, 12)),
            materializing={"token": "tok", "started_at": "garbage",
                           "due": due},
        ))
        res = materialize_due(paths, now=self.NOW, tz=NY)
        assert len(res.materialized) == 1  # recovered as a lost run

    def test_malformed_definition_reported_not_fatal(self, paths):
        save_def(paths, _task_def(next_due=_iso(_utc(2026, 6, 12, 12))))
        bad = schedule_dir(paths) / "broken.json"
        bad.write_text("{nope")
        res = materialize_due(paths, now=self.NOW, tz=NY)
        assert len(res.materialized) == 1  # the good one still fires
        assert any("broken" in m for m in res.malformed)

    def test_scaffold_failure_reported_not_fatal(self, paths):
        # Whitespace persona passes the definition's presence check
        # (truthy) but Status.new strips-and-rejects it -- the scaffold
        # failure path survives now that add-time validation catches
        # the cruder cases like max_sessions=-1.
        d = _task_def(next_due=_iso(_utc(2026, 6, 12, 12)))
        d.payload["persona"] = " "
        save_def(paths, d)
        res = materialize_due(paths, now=self.NOW, tz=NY)
        assert res.materialized == []
        assert any("morning-check" in m for m in res.malformed)



    def test_simultaneous_write_last_writer_wins(self, paths):
        """Token re-read backs off when a foreign write lands between
        our save and our confirm (the POST_WRITE seam)."""
        save_def(paths, _task_def(next_due=_iso(_utc(2026, 6, 12, 12))))

        def overwrite_hook(phase: str, def_id: str) -> None:
            if phase == "POST_WRITE":
                d = ScheduleDef.from_json(
                    def_path(paths, def_id).read_text())
                d.materializing = dict(
                    d.materializing or {}, token="foreign")
                save_def(paths, d)

        res = materialize_due(
            paths, now=self.NOW, tz=NY, cas_hook=overwrite_hook,
        )
        assert res.materialized == []  # backed off at the confirm
        d = ScheduleDef.from_json(
            def_path(paths, "morning-check").read_text())
        assert d.materializing["token"] == "foreign"

    def test_workflow_payload_materializes(self, paths, tmp_path):
        team = tmp_path / "teams" / "Shohoku"
        (team / "configs").mkdir(parents=True)
        personas = ["Anzai", "Akagi", "Ayako"]
        (team / "configs" / "personas.yaml").write_text(
            "personas:\n" + "".join(
                f"  - name: {p}\n" for p in personas)
        )
        for p_ in personas:
            pdir = team / "personas" / p_
            pdir.mkdir(parents=True)
            (pdir / "prompt.md").write_text(f"You are {p_}.\n")
        d = ScheduleDef(
            id="weekly-diag",
            title="Weekly diagnosis",
            period="weekly",
            at="08:00",
            next_due=_iso(_utc(2026, 6, 12, 12)),
            payload={
                "kind": "workflow",
                "brief_text": "# Diag\nrun checks\n",
                "playbook": "diag",
                "playbook_text": "# Diag playbook\nAnzai plans.\n",
                "team_root": str(team),
                "captain": "Anzai",
            },
        )
        save_def(paths, d)
        res = materialize_due(paths, now=self.NOW, tz=NY)
        assert len(res.materialized) == 1
        st = Status.from_json(
            paths.status_json(res.materialized[0]).read_text())
        assert st.kind == "workflow"
        assert st.schedule_def == "weekly-diag"

    def test_scan_and_in_flight_skip_malformed_status(self, paths):
        # The bounded scans tolerate junk status.json files.
        due = _iso(_utc(2026, 6, 12, 12))
        junk = paths.active / "junk-task"
        junk.mkdir(parents=True)
        (junk / "status.json").write_text("{nope")
        save_def(paths, _task_def(next_due=due))
        res = materialize_due(paths, now=self.NOW, tz=NY)
        assert len(res.materialized) == 1

    def test_recovery_hook_fires_on_lost_run(self, paths):
        due = _iso(_utc(2026, 6, 12, 12))
        save_def(paths, _task_def(
            next_due=_iso(_utc(2026, 6, 13, 12)),
            materializing={
                "token": "tok", "started_at": _iso(_utc(2026, 6, 12, 8)),
                "due": due,
            },
        ))
        phases = []
        res = materialize_due(
            paths, now=self.NOW, tz=NY,
            cas_hook=lambda ph, did: phases.append(ph),
        )
        assert len(res.materialized) == 1
        assert "B" in phases


class TestMaterializeDueExtra:
    NOW = _iso(_utc(2026, 6, 12, 20))



    def test_phase_b_hook_fires_on_normal_path(self, paths):
        save_def(paths, _task_def(next_due=_iso(_utc(2026, 6, 12, 12))))
        phases = []
        res = materialize_due(
            paths, now=self.NOW, tz=NY,
            cas_hook=lambda ph, did: phases.append(ph),
        )
        assert len(res.materialized) == 1
        assert phases == ["A", "POST_WRITE", "B"]

    def test_recovery_scan_tolerates_junk_status(self, paths):
        # _scan_for_instance's except path: a junk status.json in
        # active/ during a stale-intent recovery scan -- plus a valid
        # but UNRELATED task, so the non-matching loop branch runs too.
        due = _iso(_utc(2026, 6, 12, 12))
        junk = paths.active / "junk-task"
        junk.mkdir(parents=True)
        (junk / "status.json").write_text("{nope")
        other = paths.active / "other-task"
        other.mkdir(parents=True)
        (other / "status.json").write_text(
            Status.new(id="other-task", title="t", persona="P").to_json()
        )
        save_def(paths, _task_def(
            next_due=_iso(_utc(2026, 6, 13, 12)),
            materializing={
                "token": "tok", "started_at": _iso(_utc(2026, 6, 12, 8)),
                "due": due,
            },
        ))
        res = materialize_due(paths, now=self.NOW, tz=NY)
        assert len(res.materialized) == 1  # junk skipped, run completed


class TestScheduleCliWorkflow:
    def test_workflow_add_round_trip(self, tmp_path, capsys, monkeypatch):
        journal_dir = tmp_path / "journal"
        team = tmp_path / "teams" / "Shohoku"
        (team / "configs").mkdir(parents=True)
        (team / "configs" / "personas.yaml").write_text(
            "personas:\n  - name: Anzai\n")
        (team / "workflow").mkdir()
        (team / "workflow" / "diag.md").write_text(
            "# Diag playbook\nAnzai plans.\n")
        brief = tmp_path / "brief.md"
        brief.write_text("# Diag\nrun checks\n")
        monkeypatch.chdir(tmp_path)  # resolve_team_root searches cwd
        rc = main([
            "--journal-dir", str(journal_dir),
            "schedule", "add", "--title", "Weekly diag",
            "--period", "weekly", "--at", "08:00",
            "--kind", "workflow", "--playbook", "diag",
            "--brief-file", str(brief), "--captain", "Anzai",
            "--team", "Shohoku",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Scheduled: weekly-diag" in out
        data = json.loads((journal_dir / "schedule" /
                           "weekly-diag.json").read_text())
        assert data["payload"]["playbook"] == "diag"
        assert data["payload"]["brief_text"].startswith("# Diag")
        assert data["payload"]["captain"] == "Anzai"

    def test_workflow_add_inline_brief(self, tmp_path, capsys, monkeypatch):
        journal_dir = tmp_path / "journal"
        team = tmp_path / "teams" / "Shohoku"
        (team / "configs").mkdir(parents=True)
        (team / "configs" / "personas.yaml").write_text("personas: []\n")
        (team / "workflow").mkdir()
        (team / "workflow" / "diag.md").write_text("# Diag\n")
        monkeypatch.chdir(tmp_path)
        rc = main([
            "--journal-dir", str(journal_dir),
            "schedule", "add", "--title", "Diag2", "--at", "07:00",
            "--kind", "workflow", "--playbook", "diag",
            "--task-brief", "# inline\n", "--team", "Shohoku",
        ])
        assert rc == 0

    def test_workflow_add_both_briefs_exit_2(self, tmp_path, capsys,
                                             monkeypatch):
        journal_dir = tmp_path / "journal"
        team = tmp_path / "teams" / "Shohoku"
        (team / "configs").mkdir(parents=True)
        (team / "configs" / "personas.yaml").write_text("personas: []\n")
        (team / "workflow").mkdir()
        (team / "workflow" / "diag.md").write_text("# Diag\n")
        brief = tmp_path / "b.md"
        brief.write_text("x")
        monkeypatch.chdir(tmp_path)
        rc = main([
            "--journal-dir", str(journal_dir),
            "schedule", "add", "--title", "X", "--at", "07:00",
            "--kind", "workflow", "--playbook", "diag",
            "--task-brief", "y", "--brief-file", str(brief),
            "--team", "Shohoku",
        ])
        assert rc == 2
        assert "mutually" in capsys.readouterr().err

    def test_workflow_add_missing_playbook_file_exit_2(
        self, tmp_path, capsys, monkeypatch,
    ):
        journal_dir = tmp_path / "journal"
        team = tmp_path / "teams" / "Shohoku"
        (team / "configs").mkdir(parents=True)
        (team / "configs" / "personas.yaml").write_text("personas: []\n")
        monkeypatch.chdir(tmp_path)
        rc = main([
            "--journal-dir", str(journal_dir),
            "schedule", "add", "--title", "X", "--at", "07:00",
            "--kind", "workflow", "--playbook", "ghost",
            "--task-brief", "y", "--team", "Shohoku",
        ])
        assert rc == 2
        assert "playbook not found" in capsys.readouterr().err

    def test_workflow_add_missing_brief_file_exit_2(
        self, tmp_path, capsys, monkeypatch,
    ):
        journal_dir = tmp_path / "journal"
        team = tmp_path / "teams" / "Shohoku"
        (team / "configs").mkdir(parents=True)
        (team / "configs" / "personas.yaml").write_text("personas: []\n")
        (team / "workflow").mkdir()
        (team / "workflow" / "diag.md").write_text("# Diag\n")
        monkeypatch.chdir(tmp_path)
        rc = main([
            "--journal-dir", str(journal_dir),
            "schedule", "add", "--title", "X", "--at", "07:00",
            "--kind", "workflow", "--playbook", "diag",
            "--brief-file", str(tmp_path / "ghost.md"),
            "--team", "Shohoku",
        ])
        assert rc == 2
        assert "brief not found" in capsys.readouterr().err


class TestSweepIntegration:
    def test_sweep_materializes_and_reports(self, paths):
        save_def(paths, _task_def(next_due=_iso(_utc(2026, 6, 12, 12))))
        result = sweep(paths, now=_iso(_utc(2026, 6, 12, 20)))
        assert len(result.schedule_materialized) == 1
        # Born task appears in THIS sweep's pending list.
        assert len(result.pending) == 1
        assert "materialized 1 scheduled" in result.to_summary()

    def test_sweep_reports_malformed_definitions(self, paths):
        sd = schedule_dir(paths)
        sd.mkdir(parents=True)
        (sd / "junk.json").write_text("{")
        result = sweep(paths, now=_iso(_utc(2026, 6, 12, 20)))
        assert result.schedule_materialized == []
        assert len(result.schedule_malformed) == 1
        assert "malformed-definitions" in result.to_summary()


class TestScheduleCli:
    def _add(self, journal_dir, prd, extra=()):
        return main([
            "--journal-dir", str(journal_dir),
            "schedule", "add", "--title", "Morning check",
            "--at", "08:00", "--kind", "task",
            "--prd", str(prd), "--persona", "Ayako",
            *extra,
        ])

    def test_add_list_rm_round_trip(self, tmp_path, capsys):
        journal_dir = tmp_path / "journal"
        prd = tmp_path / "brief.md"
        prd.write_text("# Check\nbody\n")
        assert self._add(journal_dir, prd, ("--autonomy", "judgement",
                                            "--early-exit")) == 0
        out = capsys.readouterr().out
        assert "Scheduled: morning-check" in out

        assert main(["--journal-dir", str(journal_dir),
                     "schedule", "list"]) == 0
        out = capsys.readouterr().out
        assert "morning-check" in out and "daily@08:00" in out

        assert main(["--journal-dir", str(journal_dir),
                     "schedule", "list", "--format", "json"]) == 0
        data = json.loads(capsys.readouterr().out)
        d = data["definitions"][0]
        assert d["payload"]["autonomy"] == "judgement"
        assert d["payload"]["early_exit"] is True
        assert d["payload"]["prd_text"].startswith("# Check")

        assert main(["--journal-dir", str(journal_dir),
                     "schedule", "rm", "morning-check"]) == 0
        assert main(["--journal-dir", str(journal_dir),
                     "schedule", "list"]) == 0
        assert "No schedule definitions." in capsys.readouterr().out

    def test_add_duplicate_refused(self, tmp_path, capsys):
        journal_dir = tmp_path / "journal"
        prd = tmp_path / "brief.md"
        prd.write_text("# Check\n")
        assert self._add(journal_dir, prd) == 0
        capsys.readouterr()
        assert self._add(journal_dir, prd) == 1
        assert "already exists" in capsys.readouterr().err

    def test_add_task_missing_args_exit_2(self, tmp_path, capsys):
        journal_dir = tmp_path / "journal"
        rc = main(["--journal-dir", str(journal_dir),
                   "schedule", "add", "--title", "X", "--at", "08:00",
                   "--kind", "task"])
        assert rc == 2
        assert "--prd and --persona" in capsys.readouterr().err

    def test_add_missing_prd_file_exit_2(self, tmp_path, capsys):
        journal_dir = tmp_path / "journal"
        rc = main(["--journal-dir", str(journal_dir),
                   "schedule", "add", "--title", "X", "--at", "08:00",
                   "--kind", "task", "--prd", str(tmp_path / "nope.md"),
                   "--persona", "P"])
        assert rc == 2

    def test_add_invalid_at_exit_1(self, tmp_path, capsys):
        journal_dir = tmp_path / "journal"
        prd = tmp_path / "b.md"
        prd.write_text("x")
        rc = main(["--journal-dir", str(journal_dir),
                   "schedule", "add", "--title", "X", "--at", "8am",
                   "--kind", "task", "--prd", str(prd),
                   "--persona", "P"])
        assert rc == 1
        assert "cadence" in capsys.readouterr().err

    def test_rm_missing_def_exit_1(self, tmp_path, capsys):
        journal_dir = tmp_path / "journal"
        rc = main(["--journal-dir", str(journal_dir),
                   "schedule", "rm", "ghost"])
        assert rc == 1

    def test_rm_refuses_fresh_lease(self, tmp_path, capsys):
        journal_dir = tmp_path / "journal"
        paths = JournalPaths(root=journal_dir)
        now = dt.datetime.now(UTC)
        save_def(paths, _task_def(
            next_due=_iso(now + dt.timedelta(days=1)),
            materializing={"token": "tok", "started_at": _iso(now),
                           "due": _iso(now)},
        ))
        rc = main(["--journal-dir", str(journal_dir),
                   "schedule", "rm", "morning-check"])
        assert rc == 1
        assert "mid-materialization" in capsys.readouterr().err

    def test_rm_allows_stale_lease_and_malformed(self, tmp_path, capsys):
        journal_dir = tmp_path / "journal"
        paths = JournalPaths(root=journal_dir)
        old = dt.datetime.now(UTC) - dt.timedelta(hours=2)
        save_def(paths, _task_def(
            next_due=_iso(dt.datetime.now(UTC) + dt.timedelta(days=1)),
            materializing={"token": "tok", "started_at": _iso(old),
                           "due": _iso(old)},
        ))
        assert main(["--journal-dir", str(journal_dir),
                     "schedule", "rm", "morning-check"]) == 0
        sd = schedule_dir(paths)
        (sd / "junk.json").write_text("{")
        assert main(["--journal-dir", str(journal_dir),
                     "schedule", "rm", "junk"]) == 0

    def test_list_reports_malformed(self, tmp_path, capsys):
        journal_dir = tmp_path / "journal"
        paths = JournalPaths(root=journal_dir)
        sd = schedule_dir(paths)
        sd.mkdir(parents=True)
        (sd / "junk.json").write_text("{")
        assert main(["--journal-dir", str(journal_dir),
                     "schedule", "list"]) == 0
        assert "malformed" in capsys.readouterr().out

    def test_workflow_add_requires_playbook_args(self, tmp_path, capsys):
        journal_dir = tmp_path / "journal"
        rc = main(["--journal-dir", str(journal_dir),
                   "schedule", "add", "--title", "X", "--at", "08:00",
                   "--kind", "workflow"])
        assert rc == 2
        assert "--playbook" in capsys.readouterr().err

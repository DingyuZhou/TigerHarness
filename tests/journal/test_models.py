"""Tests for ``tigerharness.journal.models``: Status + State + JSON round-trip."""

from __future__ import annotations

import json

import pytest

from tigerharness.journal.models import (
    CompilePhase,
    JournalModelError,
    State,
    Status,
)


# ---------------------------------------------------------------------------
# Status.new -- field validation
# ---------------------------------------------------------------------------

class TestStatusNew:
    def test_minimal_happy_path(self):
        s = Status.new(
            id="20260602-test-12345678",
            title="Test",
            persona="Mitsui",
            now="2026-06-02T08:00:00Z",
        )
        assert s.state is State.PENDING
        assert s.sessions == 0
        assert s.max_sessions == 5  # default
        assert s.created_at == s.updated_at == "2026-06-02T08:00:00Z"
        assert s.session_ref is None
        assert s.next_action == ""

    def test_strips_title_and_persona(self):
        s = Status.new(
            id="x",
            title="  Test  ",
            persona="  Mitsui  ",
            now="2026-06-02T08:00:00Z",
        )
        assert s.title == "Test"
        assert s.persona == "Mitsui"

    def test_new_only_builds_kind_task(self):
        """Status.new is the task-mode constructor; workflow tasks must
        be built via Status.new_workflow (their persona / compile_pending
        / compile_phase semantics differ enough that a shared
        constructor would be confusing). Phase 1.5 expanded the
        supported kinds; this asserts the per-constructor split."""
        with pytest.raises(JournalModelError) as exc:
            Status.new(
                id="x", title="t", persona="P",
                kind="workflow",
                now="2026-06-02T08:00:00Z",
            )
        assert "new_workflow" in str(exc.value)

    def test_blank_title_rejected(self):
        with pytest.raises(JournalModelError):
            Status.new(
                id="x", title="   ", persona="P",
                now="2026-06-02T08:00:00Z",
            )

    def test_blank_persona_rejected(self):
        with pytest.raises(JournalModelError):
            Status.new(
                id="x", title="t", persona="   ",
                now="2026-06-02T08:00:00Z",
            )

    def test_zero_max_sessions_rejected(self):
        with pytest.raises(JournalModelError):
            Status.new(
                id="x", title="t", persona="P", max_sessions=0,
                now="2026-06-02T08:00:00Z",
            )

    def test_now_defaults_to_utcnow(self, monkeypatch):
        from tigerharness.journal import models as m
        monkeypatch.setattr(m, "_utcnow_iso", lambda: "2099-12-31T23:59:00Z")
        s = Status.new(id="x", title="t", persona="P")
        assert s.created_at == "2099-12-31T23:59:00Z"
        assert s.updated_at == "2099-12-31T23:59:00Z"


# ---------------------------------------------------------------------------
# JSON round-trip
# ---------------------------------------------------------------------------

class TestJsonRoundTrip:
    def _make(self) -> Status:
        return Status.new(
            id="20260602-test-12345678",
            title="Test",
            persona="Mitsui",
            now="2026-06-02T08:00:00Z",
        )

    def test_to_json_then_from_json_identity(self):
        s = self._make()
        s2 = Status.from_json(s.to_json())
        assert s == s2

    def test_state_serialises_as_plain_string(self):
        s = self._make()
        decoded = json.loads(s.to_json())
        assert decoded["state"] == "pending"
        assert isinstance(decoded["state"], str)

    def test_from_json_rejects_non_json(self):
        with pytest.raises(JournalModelError):
            Status.from_json("not json")

    def test_from_json_rejects_non_object(self):
        with pytest.raises(JournalModelError) as exc:
            Status.from_json("[1, 2, 3]")
        assert "JSON object" in str(exc.value)

    def test_from_dict_rejects_missing_required(self):
        with pytest.raises(JournalModelError) as exc:
            Status.from_dict({"id": "x"})
        assert "missing" in str(exc.value)

    def test_from_dict_rejects_unknown_keys(self):
        s = self._make().to_dict()
        s["bogus"] = "foo"
        with pytest.raises(JournalModelError) as exc:
            Status.from_dict(s)
        assert "unknown" in str(exc.value)

    def test_from_dict_rejects_invalid_state(self):
        s = self._make().to_dict()
        s["state"] = "neverexisted"
        with pytest.raises(JournalModelError):
            Status.from_dict(s)

    def test_from_dict_rejects_unsupported_kind(self):
        """Phase 1.5 accepts ``task`` and ``workflow`` only -- anything
        else is rejected by ``from_dict`` so a hand-edited
        ``status.json`` cannot bypass the scope gate."""
        s = self._make().to_dict()
        s["kind"] = "lab-notebook"  # not in {task, workflow}
        with pytest.raises(JournalModelError) as exc:
            Status.from_dict(s)
        assert "lab-notebook" in str(exc.value)

    def test_from_dict_rejects_kind_task_with_compile_fields(self):
        """A task-mode status.json must not carry the workflow-only
        ``compile_pending`` / ``compile_phase`` keys."""
        s = self._make().to_dict()
        s["compile_pending"] = False
        with pytest.raises(JournalModelError) as exc:
            Status.from_dict(s)
        assert "compile_pending is rejected for kind=task" in str(exc.value)

    def test_from_dict_rejects_kind_task_with_compile_phase(self):
        s = self._make().to_dict()
        s["compile_phase"] = "pending"
        with pytest.raises(JournalModelError) as exc:
            Status.from_dict(s)
        assert "compile_phase is rejected for kind=task" in str(exc.value)

    def test_from_dict_rejects_negative_sessions(self):
        s = self._make().to_dict()
        s["sessions"] = -1
        with pytest.raises(JournalModelError) as exc:
            Status.from_dict(s)
        assert "sessions" in str(exc.value)

    def test_from_dict_rejects_zero_max_sessions(self):
        s = self._make().to_dict()
        s["max_sessions"] = 0
        with pytest.raises(JournalModelError) as exc:
            Status.from_dict(s)
        assert "max_sessions" in str(exc.value)

    def test_from_dict_rejects_non_int_sessions(self):
        s = self._make().to_dict()
        s["sessions"] = "many"
        with pytest.raises(JournalModelError):
            Status.from_dict(s)


# ---------------------------------------------------------------------------
# kind=workflow constructor + schema gates (Phase 1.5)
# ---------------------------------------------------------------------------

class TestWorkflowMode:
    """Phase 1.5: ``Status.new_workflow`` + per-kind enforcement of
    ``compile_pending`` and ``compile_phase`` in ``from_dict`` /
    ``to_dict``."""

    def test_new_workflow_happy_path(self):
        s = Status.new_workflow(
            id="w1",
            title="Test workflow",
            playbook_name="default",
            captain="Akagi",
            max_sessions=12,
            now="2026-06-03T08:00:00Z",
        )
        assert s.kind == "workflow"
        assert s.state is State.PENDING
        assert s.persona == "Akagi"
        assert s.sessions == 0
        assert s.max_sessions == 12
        assert s.compile_pending is True
        assert s.compile_phase is CompilePhase.PENDING

    def test_new_workflow_captain_none_allowed(self):
        s = Status.new_workflow(
            id="w1", title="Test", captain=None, playbook_name="default",
        )
        assert s.persona is None
        assert s.compile_pending is True
        assert s.compile_phase is CompilePhase.PENDING

    def test_new_workflow_default_max_sessions(self):
        """Default is 10 (not 5 as for tasks) so the in-session
        compile has budget."""
        s = Status.new_workflow(
            id="w1", title="Test", playbook_name="default",
        )
        assert s.max_sessions == 10

    def test_new_workflow_rejects_blank_title(self):
        with pytest.raises(JournalModelError):
            Status.new_workflow(id="w1", title="   ", playbook_name="default")

    def test_new_workflow_rejects_blank_captain(self):
        """``captain=""`` is wrong shape; should pass ``None`` to mean
        'no captain'. We reject blank strings so a typo doesn't
        silently produce a no-owner workflow."""
        with pytest.raises(JournalModelError):
            Status.new_workflow(id="w1", title="t", captain="   ", playbook_name="default")

    def test_new_workflow_rejects_zero_max_sessions(self):
        with pytest.raises(JournalModelError):
            Status.new_workflow(id="w1", title="t", max_sessions=0, playbook_name="default")

    def test_to_dict_emits_workflow_fields(self):
        s = Status.new_workflow(id="w1", title="t", captain="Akagi", playbook_name="default")
        d = s.to_dict()
        assert d["kind"] == "workflow"
        assert d["compile_pending"] is True
        assert d["compile_phase"] == "pending"
        assert d["persona"] == "Akagi"

    def test_to_dict_suppresses_workflow_fields_for_task(self):
        """Task-mode ``to_dict`` must NOT emit ``compile_pending`` /
        ``compile_phase`` keys -- so a Phase 1 task status.json round-
        trips byte-identical to its pre-Phase-1.5 shape."""
        t = Status.new(id="t1", title="t", persona="P")
        d = t.to_dict()
        assert "compile_pending" not in d
        assert "compile_phase" not in d

    def test_workflow_json_round_trip(self):
        s = Status.new_workflow(id="w1", title="t", captain="Akagi", playbook_name="default")
        s2 = Status.from_json(s.to_json())
        assert s == s2

    def test_workflow_null_captain_round_trip(self):
        s = Status.new_workflow(id="w1", title="t", captain=None, playbook_name="default")
        s2 = Status.from_json(s.to_json())
        assert s == s2
        assert s2.persona is None

    # ---- from_dict gates for workflow tasks ----

    def _workflow_dict(self) -> dict:
        s = Status.new_workflow(
            id="w1", title="t", captain="Akagi",
            playbook_name="default",
            now="2026-06-03T08:00:00Z",
        )
        return s.to_dict()

    def test_from_dict_rejects_workflow_missing_compile_pending(self):
        d = self._workflow_dict()
        del d["compile_pending"]
        with pytest.raises(JournalModelError) as exc:
            Status.from_dict(d)
        assert "compile_pending is required for kind=workflow" in str(
            exc.value
        )

    def test_from_dict_rejects_workflow_missing_compile_phase(self):
        d = self._workflow_dict()
        del d["compile_phase"]
        with pytest.raises(JournalModelError) as exc:
            Status.from_dict(d)
        assert "compile_phase is required for kind=workflow" in str(
            exc.value
        )

    def test_from_dict_rejects_invalid_compile_phase(self):
        d = self._workflow_dict()
        d["compile_phase"] = "neverexisted"
        with pytest.raises(JournalModelError) as exc:
            Status.from_dict(d)
        assert "invalid compile_phase" in str(exc.value)

    def test_from_dict_rejects_non_bool_compile_pending(self):
        d = self._workflow_dict()
        d["compile_pending"] = "yes"  # string, not bool
        with pytest.raises(JournalModelError) as exc:
            Status.from_dict(d)
        assert "compile_pending must be a bool" in str(exc.value)

    def test_from_dict_rejects_blank_captain(self):
        d = self._workflow_dict()
        d["persona"] = "   "
        with pytest.raises(JournalModelError) as exc:
            Status.from_dict(d)
        assert "non-blank captain" in str(exc.value)

    def test_from_dict_rejects_non_string_captain(self):
        d = self._workflow_dict()
        d["persona"] = 42
        with pytest.raises(JournalModelError) as exc:
            Status.from_dict(d)
        assert "must be a string or null" in str(exc.value)

    def test_from_dict_accepts_workflow_with_null_persona(self):
        d = self._workflow_dict()
        d["persona"] = None
        s = Status.from_dict(d)
        assert s.persona is None

    # ---- Phase 2: playbook_name on Status ----

    def test_new_workflow_requires_playbook_name(self):
        with pytest.raises(JournalModelError) as exc:
            Status.new_workflow(
                id="w1", title="t", captain="Akagi", playbook_name="",
            )
        assert "playbook_name" in str(exc.value)

    def test_new_workflow_rejects_whitespace_playbook_name(self):
        with pytest.raises(JournalModelError):
            Status.new_workflow(
                id="w1", title="t", captain="Akagi", playbook_name="   ",
            )

    def test_new_workflow_strips_playbook_name(self):
        s = Status.new_workflow(
            id="w1", title="t", captain="Akagi",
            playbook_name="  research-pass  ",
        )
        assert s.playbook_name == "research-pass"

    def test_to_dict_emits_playbook_name_for_workflow(self):
        s = Status.new_workflow(
            id="w1", title="t", captain="Akagi", playbook_name="ml-eval",
        )
        d = s.to_dict()
        assert d["playbook_name"] == "ml-eval"

    def test_to_dict_suppresses_playbook_name_for_task(self):
        t = Status.new(id="t1", title="t", persona="P").to_dict()
        assert "playbook_name" not in t

    def test_workflow_json_round_trip_preserves_playbook_name(self):
        s = Status.new_workflow(
            id="w1", title="t", captain="Akagi", playbook_name="ml-eval",
        )
        s2 = Status.from_json(s.to_json())
        assert s2.playbook_name == "ml-eval"
        assert s == s2

    def test_from_dict_rejects_workflow_missing_playbook_name(self):
        d = self._workflow_dict()
        del d["playbook_name"]
        with pytest.raises(JournalModelError) as exc:
            Status.from_dict(d)
        assert "playbook_name is required for kind=workflow" in str(exc.value)

    def test_from_dict_rejects_blank_playbook_name_for_workflow(self):
        d = self._workflow_dict()
        d["playbook_name"] = "   "
        with pytest.raises(JournalModelError):
            Status.from_dict(d)

    def test_from_dict_rejects_non_string_playbook_name_for_workflow(self):
        d = self._workflow_dict()
        d["playbook_name"] = 42
        with pytest.raises(JournalModelError):
            Status.from_dict(d)

    def test_from_dict_rejects_playbook_name_for_task(self):
        t = Status.new(id="t1", title="t", persona="P").to_dict()
        t["playbook_name"] = "ml-eval"
        with pytest.raises(JournalModelError) as exc:
            Status.from_dict(t)
        assert "playbook_name is rejected for kind=task" in str(exc.value)

    def test_from_dict_rejects_non_string_persona_for_task(self):
        """Type validation on persona for kind=task -- a non-string
        value (e.g. integer) is a corrupted entry, not 'use the
        default'."""
        s = Status.new(id="t1", title="t", persona="P").to_dict()
        s["persona"] = 42
        with pytest.raises(JournalModelError) as exc:
            Status.from_dict(s)
        assert "must be a string for kind=task" in str(exc.value)

    def test_from_dict_rejects_null_persona_for_task(self):
        """A null persona on a kind=task entry is rejected -- only
        kind=workflow allows a null captain."""
        s = Status.new(id="t1", title="t", persona="P").to_dict()
        s["persona"] = None
        with pytest.raises(JournalModelError) as exc:
            Status.from_dict(s)
        assert "blank / null" in str(exc.value)

    def test_compile_phase_enum_values_are_seven(self):
        """If anyone changes the CompilePhase enum, this test forces
        the sub-protocol prose to be updated alongside."""
        assert {p.value for p in CompilePhase} == {
            "pending", "drafting", "tier1_pre", "critiquing",
            "tier1_post", "complete", "failed",
        }


# ---------------------------------------------------------------------------
# heartbeat / staleness classification
# ---------------------------------------------------------------------------

class TestHeartbeat:
    def _in_progress(self, updated_at: str) -> Status:
        s = Status(
            id="x", title="t", kind="task", state=State.IN_PROGRESS,
            persona="P", sessions=1, max_sessions=5,
            created_at="2026-06-02T08:00:00Z",
            updated_at=updated_at,
        )
        return s

    def test_age_in_seconds(self):
        s = self._in_progress("2026-06-02T08:00:00Z")
        age = s.heartbeat_age_seconds(now="2026-06-02T08:10:00Z")
        assert age == pytest.approx(600.0)

    def test_age_clamped_to_zero_on_future_timestamp(self):
        """Hand-edited timestamp 'into the future' should not yield a
        negative age that would silently flip the stale/fresh check."""
        s = self._in_progress("2099-12-31T23:59:00Z")
        age = s.heartbeat_age_seconds(now="2026-06-02T08:00:00Z")
        assert age == 0.0

    def test_age_accepts_plus0000_form(self):
        """A status.json that was hand-edited with the explicit +00:00
        suffix must still parse (we always emit the Z form, but the
        reader is liberal)."""
        s = self._in_progress("2026-06-02T08:00:00+00:00")
        age = s.heartbeat_age_seconds(now="2026-06-02T08:10:00Z")
        assert age == pytest.approx(600.0)

    def test_age_malformed_raises(self):
        s = self._in_progress("not-a-timestamp")
        with pytest.raises(JournalModelError):
            s.heartbeat_age_seconds(now="2026-06-02T08:00:00Z")

    def test_naive_timestamp_raises_journal_error_not_typeerror(self):
        """Regression for the critique workflow's HIGH finding: a
        hand-edited naive timestamp (no Z, no +00:00) must surface as
        ``JournalModelError`` rather than as the silent ``TypeError``
        from aware-vs-naive subtraction. The sweep's malformed-entry
        handling depends on this."""
        s = self._in_progress("2026-06-02T08:00:00")  # naive: no tz
        with pytest.raises(JournalModelError) as exc:
            s.heartbeat_age_seconds(now="2026-06-02T08:10:00Z")
        # The error message names the offending timestamp.
        assert "2026-06-02T08:00:00" in str(exc.value)

    def test_is_stale_only_for_in_progress(self):
        s = Status(
            id="x", title="t", kind="task", state=State.PENDING,
            persona="P", sessions=0, max_sessions=5,
            created_at="2000-01-01T00:00:00Z",
            updated_at="2000-01-01T00:00:00Z",
        )
        # Pending tasks are NEVER stale regardless of heartbeat age.
        assert s.is_stale(stuck_timeout_sec=10, now="2099-01-01T00:00:00Z") is False

    def test_is_stale_true_when_old(self):
        s = self._in_progress("2026-06-02T08:00:00Z")
        assert s.is_stale(
            stuck_timeout_sec=300, now="2026-06-02T08:10:00Z",
        ) is True

    def test_is_stale_false_when_fresh(self):
        s = self._in_progress("2026-06-02T08:00:00Z")
        assert s.is_stale(
            stuck_timeout_sec=3600, now="2026-06-02T08:10:00Z",
        ) is False

    def test_is_fresh_in_progress_inverse(self):
        s = self._in_progress("2026-06-02T08:00:00Z")
        assert s.is_fresh_in_progress(
            stuck_timeout_sec=3600, now="2026-06-02T08:10:00Z",
        ) is True
        assert s.is_fresh_in_progress(
            stuck_timeout_sec=300, now="2026-06-02T08:10:00Z",
        ) is False

    def test_is_fresh_false_for_non_in_progress(self):
        s = Status(
            id="x", title="t", kind="task", state=State.DONE,
            persona="P", sessions=0, max_sessions=5,
            created_at="2026-06-02T08:00:00Z",
            updated_at="2026-06-02T08:00:00Z",
        )
        assert s.is_fresh_in_progress(
            stuck_timeout_sec=3600, now="2026-06-02T08:01:00Z",
        ) is False

    def test_heartbeat_age_default_now_uses_utcnow(self, monkeypatch):
        from tigerharness.journal import models as m
        monkeypatch.setattr(m, "_utcnow_iso", lambda: "2026-06-02T09:00:00Z")
        s = self._in_progress("2026-06-02T08:00:00Z")
        assert s.heartbeat_age_seconds() == pytest.approx(3600.0)


class TestInProgressClass:
    """in_progress_class() -- idle / busy / crashed via session_ref."""

    def _ip(self, *, session_ref, updated_at):
        return Status(
            id="x", title="t", kind="task", persona="P",
            state=State.IN_PROGRESS, sessions=1, max_sessions=5,
            created_at="2026-06-02T08:00:00Z", updated_at=updated_at,
            session_ref=session_ref,
        )

    def test_idle_when_detached_regardless_of_heartbeat(self):
        # Detached + a very old heartbeat is still idle: the heartbeat is
        # not even read when no session is attached.
        s = self._ip(session_ref=None, updated_at="2026-06-01T08:00:00Z")
        assert s.in_progress_class(
            stuck_timeout_sec=300, now="2026-06-02T08:10:00Z",
        ) == "idle"

    def test_busy_when_attached_and_fresh(self):
        s = self._ip(session_ref="tok", updated_at="2026-06-02T08:08:00Z")
        assert s.in_progress_class(
            stuck_timeout_sec=300, now="2026-06-02T08:10:00Z",
        ) == "busy"

    def test_crashed_when_attached_and_stale(self):
        s = self._ip(session_ref="tok", updated_at="2026-06-01T08:00:00Z")
        assert s.in_progress_class(
            stuck_timeout_sec=300, now="2026-06-02T08:10:00Z",
        ) == "crashed"

    def test_raises_on_non_in_progress(self):
        s = Status(
            id="x", title="t", kind="task", persona="P",
            state=State.PENDING, sessions=0, max_sessions=5,
            created_at="2026-06-02T08:00:00Z",
            updated_at="2026-06-02T08:00:00Z",
        )
        with pytest.raises(JournalModelError):
            s.in_progress_class(stuck_timeout_sec=300)

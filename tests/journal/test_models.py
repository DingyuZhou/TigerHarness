"""Tests for ``tigerharness.journal.models``: Status + State + JSON round-trip."""

from __future__ import annotations

import json

import pytest

from tigerharness.journal.models import (
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

    def test_unsupported_kind_rejected(self):
        with pytest.raises(JournalModelError) as exc:
            Status.new(
                id="x", title="t", persona="P",
                kind="workflow",
                now="2026-06-02T08:00:00Z",
            )
        assert "kind" in str(exc.value)
        assert "workflow" in str(exc.value)

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

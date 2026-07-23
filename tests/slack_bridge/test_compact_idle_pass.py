"""The external idle-compaction pass (``slack-bridge compact-idle``):
fragment gating, per-record skip reasons, the journal-idle gate, the
one-per-idle-period latch, fail-soft sends, and the CLI wrapper."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tigerharness.slack_bridge.idle_compact import (
    _parse_iso,
    compact_idle_once,
    main as compact_idle_main,
)
from tigerharness.slack_bridge.persistence import ThreadStore

NOW = datetime.now(timezone.utc).replace(microsecond=0)
OLD = (NOW - timedelta(minutes=30)).isoformat()
FRESH = (NOW - timedelta(seconds=10)).isoformat()

HOT_USAGE = {
    "input_tokens": 10,
    "cache_creation_input_tokens": 30_000,
    "cache_read_input_tokens": 50_000,
}  # ~0.40 of the 200k default window
COLD_USAGE = {"input_tokens": 10}


def _team(tmp_path: Path, name: str = "Shohoku", *, fragment: str | None = None) -> Path:
    team = tmp_path / name
    (team / "configs").mkdir(parents=True)
    (team / "journal" / "active").mkdir(parents=True)
    if fragment is None:
        fragment = "idle_compact: true\nstate_dir: state\n"
    (team / "configs" / "slack-bridge.yaml").write_text(fragment)
    return team


def _seed(team: Path, records: dict[str, dict]) -> ThreadStore:
    store = ThreadStore(team / "state" / "threads.json")
    for ts, kw in records.items():
        store.set(ts, kw.pop("session_id", f"sess-{ts}"), **kw)
    return store


def _sender(fail_for: set[str] | None = None):
    sent: list[str] = []

    async def send(session_id: str) -> None:
        if fail_for and session_id in fail_for:
            raise RuntimeError("boom")
        sent.append(session_id)

    return send, sent


class TestParseIso:
    def test_valid(self):
        assert _parse_iso(OLD) is not None

    def test_none_and_empty(self):
        assert _parse_iso(None) is None
        assert _parse_iso("") is None

    def test_garbage(self):
        assert _parse_iso("not-a-date") is None


class TestFragmentGating:
    @pytest.mark.asyncio
    async def test_missing_fragment(self, tmp_path):
        team = tmp_path / "T"
        (team / "configs").mkdir(parents=True)
        report = await compact_idle_once(team, now=NOW)
        assert report == {
            "ran": False, "team": "T", "checked": 0, "compacted": [],
            "skipped": {}, "reason": "no_fragment",
        }

    @pytest.mark.asyncio
    async def test_unparseable_fragment(self, tmp_path, caplog):
        team = _team(tmp_path, fragment="- just\n- a list\n")
        report = await compact_idle_once(team, now=NOW)
        assert report["reason"] == "bad_fragment"
        assert report["ran"] is False

    @pytest.mark.asyncio
    async def test_opted_out(self, tmp_path):
        team = _team(tmp_path, fragment="idle_compact: false\nstate_dir: state\n")
        report = await compact_idle_once(team, now=NOW)
        assert report["reason"] == "disabled"

    @pytest.mark.asyncio
    async def test_missing_journal_disables_fail_soft(self, tmp_path):
        team = _team(tmp_path)
        import shutil

        shutil.rmtree(team / "journal")
        report = await compact_idle_once(team, now=NOW)
        assert report["reason"] == "disabled"

    @pytest.mark.asyncio
    async def test_no_state_dir_falls_back_to_default_path(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv(
            "TIGERHARNESS_SLACK_STATE_DIR", str(tmp_path / "xdg")
        )
        team = _team(tmp_path, fragment="idle_compact: true\n")
        send, sent = _sender()
        report = await compact_idle_once(team, send=send, now=NOW)
        assert report["ran"] is True
        assert report["checked"] == 0 and sent == []


class TestRecordGates:
    @pytest.mark.asyncio
    async def test_skip_reasons_accumulate(self, tmp_path):
        team = _team(tmp_path)
        _seed(team, {
            "a.1": {"team": "OtherTeam", "last_usage": HOT_USAGE,
                    "last_turn_at": OLD},
            "b.1": {"team": "Shohoku", "last_usage": HOT_USAGE,
                    "last_turn_at": OLD, "in_flight": True},
            "c.1": {"team": "Shohoku", "last_usage": HOT_USAGE},
            "d.1": {"team": "Shohoku", "last_usage": HOT_USAGE,
                    "last_turn_at": FRESH},
            "e.1": {"team": "Shohoku", "last_usage": COLD_USAGE,
                    "last_turn_at": OLD},
            "f.1": {"team": "Shohoku", "last_turn_at": OLD},
        })
        send, sent = _sender()
        report = await compact_idle_once(team, send=send, now=NOW)
        assert report["ran"] is True
        assert report["compacted"] == [] and sent == []
        assert report["skipped"] == {
            "other_team": 1,
            "in_flight": 1,
            "no_turn_stamp": 1,
            "too_recent": 1,
            # cold usage AND the usage-less record both read below threshold
            "below_threshold": 2,
        }
        assert report["checked"] == 6

    @pytest.mark.asyncio
    async def test_naive_timestamp_is_treated_as_utc(self, tmp_path):
        team = _team(tmp_path)
        naive_old = (NOW - timedelta(minutes=30)).replace(tzinfo=None)
        _seed(team, {
            "a.1": {"team": "Shohoku", "last_usage": HOT_USAGE,
                    "last_turn_at": naive_old.isoformat()},
        })
        send, sent = _sender()
        report = await compact_idle_once(team, send=send, now=NOW)
        assert report["compacted"] == ["a.1"]


class TestCompaction:
    @pytest.mark.asyncio
    async def test_compacts_and_latches(self, tmp_path):
        team = _team(tmp_path)
        _seed(team, {
            "a.1": {"session_id": "sess-a", "persona": "Ayako",
                    "team": "Shohoku", "last_usage": HOT_USAGE,
                    "last_turn_at": OLD},
        })
        send, sent = _sender()
        report = await compact_idle_once(team, send=send, now=NOW)
        assert report["compacted"] == ["a.1"] and sent == ["sess-a"]
        # Latch: usage cleared on disk, so a second pass is a no-op.
        reloaded = ThreadStore(team / "state" / "threads.json")
        rec = reloaded.get_record("a.1")
        assert rec.last_usage is None and rec.persona == "Ayako"
        send2, sent2 = _sender()
        report2 = await compact_idle_once(team, send=send2, now=NOW)
        assert report2["compacted"] == [] and sent2 == []
        assert report2["skipped"] == {"below_threshold": 1}

    @pytest.mark.asyncio
    async def test_journal_busy_blocks_all_candidates(self, tmp_path):
        from tigerharness.journal.models import Status

        team = _team(tmp_path)
        tdir = team / "journal" / "active" / "20260722-x-aaaa"
        tdir.mkdir()
        (tdir / "status.json").write_text(
            Status.new(id="20260722-x-aaaa", title="t", persona="P").to_json()
        )
        _seed(team, {
            "a.1": {"team": "Shohoku", "last_usage": HOT_USAGE,
                    "last_turn_at": OLD},
        })
        send, sent = _sender()
        report = await compact_idle_once(team, send=send, now=NOW)
        assert report["reason"] == "journal_busy"
        assert report["skipped"] == {"journal_busy": 1} and sent == []

    @pytest.mark.asyncio
    async def test_send_failure_skips_that_lane_only(self, tmp_path):
        team = _team(tmp_path)
        _seed(team, {
            "a.1": {"session_id": "sess-a", "team": "Shohoku",
                    "last_usage": HOT_USAGE, "last_turn_at": OLD},
            "b.1": {"session_id": "sess-b", "team": "Shohoku",
                    "last_usage": HOT_USAGE, "last_turn_at": OLD},
        })
        send, sent = _sender(fail_for={"sess-a"})
        report = await compact_idle_once(team, send=send, now=NOW)
        assert report["compacted"] == ["b.1"] and sent == ["sess-b"]
        assert report["skipped"] == {"send_failed": 1}
        # The failed lane keeps its usage stamp (no latch), so a later
        # pass retries it.
        rec = ThreadStore(team / "state" / "threads.json").get_record("a.1")
        assert rec.last_usage == HOT_USAGE


class TestConcurrentWriterSafety:
    @pytest.mark.asyncio
    async def test_latch_write_does_not_drop_records_created_mid_pass(
        self, tmp_path,
    ):
        """A /compact turn is slow; the live bridge may create NEW thread
        records while the pass runs. The latch write must go through a
        fresh load of threads.json, not the pass's start-of-run snapshot
        -- otherwise the new record is silently dropped and that Slack
        thread loses its session."""
        team = _team(
            tmp_path,
            fragment="idle_compact: true\nstate_dir: state\nagent_cwd: .\n",
        )
        _seed(team, {
            "a.1": {"session_id": "sess-a", "team": "Shohoku",
                    "last_usage": HOT_USAGE, "last_turn_at": OLD},
        })
        state_path = team / "state" / "threads.json"

        async def send(session_id: str) -> None:
            # Simulate the bridge writing a brand-new thread record
            # while the /compact turn is in flight.
            ThreadStore(state_path).set("new.9", "sess-new", persona="Rukawa")

        report = await compact_idle_once(team, send=send, now=NOW)
        assert report["compacted"] == ["a.1"]
        reloaded = ThreadStore(state_path)
        assert reloaded.get_record("a.1").last_usage is None  # latched
        assert reloaded.get("new.9") == "sess-new"  # survived the pass


class TestDefaultSend:
    @pytest.mark.asyncio
    async def test_builds_resume_compact_turn(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        import tigerharness.agent_sdk as sdk
        from tigerharness.slack_bridge.idle_compact import _default_send

        session = MagicMock()
        session.close = AsyncMock()
        backend = MagicMock()
        backend.open_session = AsyncMock(return_value=session)
        ok = MagicMock()
        ok.stop_reason = "end_turn"
        backend.run = AsyncMock(return_value=ok)
        seen_kwargs: dict = {}

        def fake_get_backend(name, **kw):
            seen_kwargs.update(kw)
            return backend

        monkeypatch.setattr(sdk, "get_backend", fake_get_backend)

        send = _default_send(cwd="/some/team")
        await send("sess-1")
        # The backend must be pinned to the lane's agent_cwd -- --resume
        # only finds a session from the project dir it was opened under.
        assert seen_kwargs == {"cwd": "/some/team"}
        backend.open_session.assert_awaited_once_with(resume_id="sess-1")
        prompt = backend.run.await_args.args[1]
        assert prompt == "/compact"
        session.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_error_stop_reason_raises_no_false_success(self, monkeypatch):
        """claude_p reports a failed CLI (stale session id, future CLI
        drift) as a RESULT with stop_reason='error', not an exception.
        Swallowing that would clear the latch on a lane that was never
        compacted -- the sender must raise instead."""
        from unittest.mock import AsyncMock, MagicMock

        import tigerharness.agent_sdk as sdk
        from tigerharness.slack_bridge.idle_compact import _default_send

        session = MagicMock()
        session.close = AsyncMock()
        bad = MagicMock()
        bad.stop_reason = "error"
        backend = MagicMock()
        backend.open_session = AsyncMock(return_value=session)
        backend.run = AsyncMock(return_value=bad)
        monkeypatch.setattr(sdk, "get_backend", lambda name, **kw: backend)

        send = _default_send(cwd="/t")
        with pytest.raises(RuntimeError, match="stop_reason='error'"):
            await send("sess-1")
        session.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_wedged_compact_turn_times_out(self, monkeypatch):
        """A hung claude subprocess must not hang the calling drive."""
        import asyncio as aio
        from unittest.mock import AsyncMock, MagicMock

        import tigerharness.agent_sdk as sdk
        from tigerharness.slack_bridge.idle_compact import _default_send

        session = MagicMock()
        session.close = AsyncMock()

        async def hang(*a, **kw):
            await aio.sleep(3600)

        backend = MagicMock()
        backend.open_session = AsyncMock(return_value=session)
        backend.run = hang
        monkeypatch.setattr(sdk, "get_backend", lambda name, **kw: backend)

        send = _default_send(cwd="/t", timeout_seconds=0.05)
        with pytest.raises(aio.TimeoutError):
            await send("sess-1")
        session.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_session_closed_even_when_run_raises(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        import tigerharness.agent_sdk as sdk
        from tigerharness.slack_bridge.idle_compact import _default_send

        session = MagicMock()
        session.close = AsyncMock()
        backend = MagicMock()
        backend.open_session = AsyncMock(return_value=session)
        backend.run = AsyncMock(side_effect=RuntimeError("no"))
        monkeypatch.setattr(sdk, "get_backend", lambda name, **kw: backend)

        send = _default_send()
        with pytest.raises(RuntimeError):
            await send("sess-1")
        session.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pass_uses_default_send_when_none_injected(
        self, tmp_path, monkeypatch,
    ):
        """A pass with eligible lanes and no injected sender builds the
        real one -- patch the builder to prove the seam is exercised."""
        import tigerharness.slack_bridge.idle_compact as mod

        team = _team(tmp_path)
        _seed(team, {
            "a.1": {"session_id": "sess-a", "team": "Shohoku",
                    "last_usage": HOT_USAGE, "last_turn_at": OLD},
        })
        sent: list[str] = []

        async def fake(session_id: str) -> None:
            sent.append(session_id)

        monkeypatch.setattr(mod, "_default_send", lambda **kw: fake)
        report = await compact_idle_once(team, now=NOW)
        assert report["compacted"] == ["a.1"] and sent == ["sess-a"]


class TestCli:
    def test_main_prints_report_and_exits_zero(self, tmp_path, capsys):
        team = _team(tmp_path, fragment="idle_compact: false\nstate_dir: state\n")
        rc = compact_idle_main(["--team-dir", str(team)])
        assert rc == 0
        report = json.loads(capsys.readouterr().out)
        assert report["reason"] == "disabled"

    def test_top_level_cli_dispatches(self, tmp_path, capsys):
        from tigerharness.cli import main as th_main

        team = _team(tmp_path, fragment="idle_compact: false\nstate_dir: state\n")
        rc = th_main(["slack-bridge", "compact-idle", "--team-dir", str(team)])
        assert rc == 0
        assert json.loads(capsys.readouterr().out)["reason"] == "disabled"

    def test_min_quiet_seconds_flag(self, tmp_path, capsys, monkeypatch):
        import tigerharness.slack_bridge.idle_compact as mod

        team = _team(tmp_path)
        _seed(team, {
            "a.1": {"team": team.name, "last_usage": HOT_USAGE,
                    "last_turn_at": FRESH},
        })
        sent: list[str] = []

        async def fake(session_id: str) -> None:
            sent.append(session_id)

        monkeypatch.setattr(mod, "_default_send", lambda **kw: fake)
        rc = compact_idle_main(
            ["--team-dir", str(team), "--min-quiet-seconds", "0"]
        )
        assert rc == 0
        report = json.loads(capsys.readouterr().out)
        # With the quiet window disabled the fresh lane is eligible.
        assert report["compacted"] == ["a.1"] and sent == ["sess-a.1"]


class TestMidPassRaces:
    @pytest.mark.asyncio
    async def test_concurrent_pass_exits_busy(self, tmp_path):
        import fcntl

        team = _team(tmp_path)
        _seed(team, {})
        lease_path = team / "state" / "compact-idle.lock"
        lease_path.parent.mkdir(parents=True, exist_ok=True)
        holder = open(lease_path, "w")
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            report = await compact_idle_once(team, now=NOW)
        finally:
            holder.close()
        assert report["ran"] is False and report["reason"] == "busy"

    @pytest.mark.asyncio
    async def test_lane_that_went_active_mid_pass_is_skipped(self, tmp_path):
        """Candidate 2's session gets a live bridge turn while candidate
        1's slow /compact runs -- the pre-send recheck must catch it."""
        team = _team(tmp_path)
        _seed(team, {
            "a.1": {"session_id": "sess-a", "team": "Shohoku",
                    "last_usage": HOT_USAGE, "last_turn_at": OLD},
            "b.1": {"session_id": "sess-b", "team": "Shohoku",
                    "last_usage": HOT_USAGE, "last_turn_at": OLD},
        })
        state_path = team / "state" / "threads.json"
        sent: list[str] = []

        async def send(session_id: str) -> None:
            sent.append(session_id)
            if session_id == "sess-a":
                # Bridge marks candidate b in_flight during our send.
                ThreadStore(state_path).mark_in_flight("b.1", True)

        report = await compact_idle_once(team, send=send, now=NOW)
        assert sent == ["sess-a"]
        assert report["compacted"] == ["a.1"]
        assert report["skipped"] == {"went_active": 1}

    @pytest.mark.asyncio
    async def test_journal_going_busy_mid_pass_stops_sending(self, tmp_path):
        from tigerharness.journal.models import Status

        team = _team(tmp_path)
        _seed(team, {
            "a.1": {"session_id": "sess-a", "team": "Shohoku",
                    "last_usage": HOT_USAGE, "last_turn_at": OLD},
            "b.1": {"session_id": "sess-b", "team": "Shohoku",
                    "last_usage": HOT_USAGE, "last_turn_at": OLD},
        })
        sent: list[str] = []

        async def send(session_id: str) -> None:
            sent.append(session_id)
            tdir = team / "journal" / "active" / "20260722-x-bbbb"
            tdir.mkdir(parents=True, exist_ok=True)
            (tdir / "status.json").write_text(
                Status.new(id="20260722-x-bbbb", title="t", persona="P")
                .to_json()
            )

        report = await compact_idle_once(team, send=send, now=NOW)
        assert sent == ["sess-a"]
        assert report["compacted"] == ["a.1"]
        assert report["reason"] == "journal_busy"
        assert report["skipped"] == {"journal_busy": 1}

    @pytest.mark.asyncio
    async def test_latch_skipped_when_session_id_changed_mid_send(
        self, tmp_path,
    ):
        """A turn landing DURING the send restamps the record (new
        session id + fresh usage); the latch must not roll that back."""
        team = _team(tmp_path)
        _seed(team, {
            "a.1": {"session_id": "sess-a", "team": "Shohoku",
                    "last_usage": HOT_USAGE, "last_turn_at": OLD},
        })
        state_path = team / "state" / "threads.json"

        async def send(session_id: str) -> None:
            ThreadStore(state_path).set(
                "a.1", "sess-a2", persona="Ayako",
                last_usage=HOT_USAGE, last_turn_at=FRESH,
            )

        report = await compact_idle_once(team, send=send, now=NOW)
        assert report["compacted"] == ["a.1"]
        rec = ThreadStore(state_path).get_record("a.1")
        # The mid-send restamp survives untouched -- no rollback, no latch.
        assert rec.session_id == "sess-a2"
        assert rec.last_usage == HOT_USAGE

    @pytest.mark.asyncio
    async def test_latch_write_failure_is_fail_soft(self, tmp_path, monkeypatch):
        import tigerharness.slack_bridge.idle_compact as mod

        team = _team(tmp_path)
        _seed(team, {
            "a.1": {"session_id": "sess-a", "team": "Shohoku",
                    "last_usage": HOT_USAGE, "last_turn_at": OLD},
        })
        send, sent = _sender()
        real_set = mod.ThreadStore.set

        def broken_set(self, *a, **kw):
            raise OSError("disk full")

        # Break set() only after the scan (the seeding above used it).
        monkeypatch.setattr(mod.ThreadStore, "set", broken_set)
        try:
            report = await compact_idle_once(team, send=send, now=NOW)
        finally:
            monkeypatch.setattr(mod.ThreadStore, "set", real_set)
        assert sent == ["sess-a"]
        assert report["compacted"] == ["a.1"]
        assert report["skipped"] == {"latch_failed": 1}

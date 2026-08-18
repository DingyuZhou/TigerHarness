"""The delivery ledger: the thing that makes replay safe.

Its whole job is one promise -- ``mark`` returns ``True`` exactly once
per ``(channel, message_ts)``, durably, across processes. Everything
here is either that promise or the ways the file can be damaged without
taking the bridge down with it.
"""
from __future__ import annotations

import json
import logging

import pytest

from tigerharness.slack_bridge.persistence import (
    SEEN_CHANNELS_MAX,
    SEEN_RING_MAX,
    ChannelDelivery,
    SeenLedger,
    ThreadStore,
    _ts_sort_key,
)

DM = "D0B4L5V7RFG"


@pytest.fixture
def ledger(tmp_path):
    return SeenLedger(tmp_path / "threads.seen.json")


class TestTheOnePromise:
    def test_mark_wins_once(self, ledger):
        assert ledger.mark(DM, "1.1") is True
        assert ledger.mark(DM, "1.1") is False

    def test_the_claim_survives_a_restart(self, tmp_path):
        path = tmp_path / "threads.seen.json"
        assert SeenLedger(path).mark(DM, "1.1") is True
        assert SeenLedger(path).mark(DM, "1.1") is False

    def test_channels_are_independent(self, ledger):
        assert ledger.mark(DM, "1.1") is True
        assert ledger.mark("D0OTHER", "1.1") is True

    def test_an_unkeyable_message_is_refused_loudly(self, ledger, caplog):
        with caplog.at_level(logging.WARNING):
            assert ledger.mark("", "1.1") is False
            assert ledger.mark(DM, "") is False
        assert "unkeyable" in caplog.text

    def test_watermark_tracks_the_newest_not_the_latest_call(self, ledger):
        ledger.mark(DM, "100.000200")
        ledger.mark(DM, "100.000100")  # arrives late, is older
        assert ledger.watermark(DM) == "100.000200"

    def test_watermark_is_none_for_an_unknown_channel(self, ledger):
        assert ledger.watermark("D0NEVER") is None

    def test_was_seen_is_a_read_only_probe(self, ledger):
        assert ledger.was_seen(DM, "1.1") is False
        ledger.mark(DM, "1.1")
        assert ledger.was_seen(DM, "1.1") is True
        assert ledger.was_seen("D0OTHER", "1.1") is False

    def test_channel_type_is_remembered_from_the_live_event(self, ledger):
        ledger.mark(DM, "1.1", "im")
        assert ledger.channels() == [(DM, "im")]
        # A later mark without the hint must not erase it.
        ledger.mark(DM, "1.2")
        assert ledger.channels() == [(DM, "im")]

    def test_the_ring_is_bounded(self, ledger):
        for i in range(SEEN_RING_MAX + 5):
            ledger.mark(DM, f"100.{i:06d}")
        entry = ledger._read_disk()[DM]
        assert len(entry.seen) == SEEN_RING_MAX
        assert entry.watermark == f"100.{SEEN_RING_MAX + 4:06d}"


class TestBoundedGrowth:
    def test_evicting_channels_is_loud(self, caplog):
        disk = {
            f"D{i:04d}": ChannelDelivery(watermark=f"{i}.0")
            for i in range(SEEN_CHANNELS_MAX + 3)
        }
        with caplog.at_level(logging.WARNING):
            kept = SeenLedger._evict(disk)
        assert len(kept) == SEEN_CHANNELS_MAX
        assert "dropping 3 least-recent" in caplog.text
        # The three oldest went, not three arbitrary ones.
        assert "D0000" not in kept
        assert f"D{SEEN_CHANNELS_MAX + 2:04d}" in kept

    def test_under_the_limit_nothing_moves(self):
        disk = {DM: ChannelDelivery(watermark="1.0")}
        assert SeenLedger._evict(disk) is disk


class TestDamagedFilesFailOpen:
    """An unreadable ledger costs at most one duplicate. A ledger that
    refuses to load costs every message -- so it never refuses."""

    def test_unparseable_json(self, tmp_path, caplog):
        path = tmp_path / "threads.seen.json"
        path.write_text("{not json", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            assert SeenLedger(path).channels() == []
        assert "starting fresh" in caplog.text

    def test_json_that_is_not_an_object(self, tmp_path, caplog):
        path = tmp_path / "threads.seen.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            assert SeenLedger(path).channels() == []
        assert "not a JSON object" in caplog.text

    def test_junk_entries_are_skipped_not_fatal(self, tmp_path):
        path = tmp_path / "threads.seen.json"
        path.write_text(
            json.dumps(
                {
                    "D0BAD": "not-a-dict",
                    "D0PARTIAL": {"watermark": 17, "seen": "nope",
                                  "channel_type": 5},
                    DM: {"watermark": "1.1", "seen": ["1.1", 7, ""],
                         "channel_type": "im"},
                }
            ),
            encoding="utf-8",
        )
        led = SeenLedger(path)
        assert led.channels() == [(DM, "im"), ("D0PARTIAL", None)]
        assert led.watermark("D0PARTIAL") is None
        assert led.was_seen(DM, "1.1") is True

    def test_a_missing_file_is_simply_empty(self, tmp_path):
        assert SeenLedger(tmp_path / "nope.json").channels() == []

    def test_a_failed_publish_leaves_no_temp_file(self, tmp_path, monkeypatch):
        path = tmp_path / "threads.seen.json"
        import tigerharness.slack_bridge.persistence as mod

        monkeypatch.setattr(
            mod.os, "replace",
            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
        )
        with pytest.raises(OSError):
            SeenLedger(path).mark(DM, "1.1")
        assert not list(tmp_path.glob(".seen.*.tmp"))

    def test_a_publish_that_cannot_even_clean_up_still_reports(
        self, tmp_path, monkeypatch
    ):
        """The original failure is what the caller needs to see; a
        failed cleanup must not mask it."""
        import tigerharness.slack_bridge.persistence as mod

        def _boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(mod.os, "replace", _boom)
        monkeypatch.setattr(mod.os, "unlink", _boom)
        with pytest.raises(OSError, match="disk full"):
            SeenLedger(tmp_path / "threads.seen.json").mark(DM, "1.1")


class TestSortKey:
    def test_slack_timestamps_sort_numerically(self):
        assert _ts_sort_key("10.0") > _ts_sort_key("9.0")

    def test_a_non_timestamp_sorts_oldest(self):
        assert _ts_sort_key("not-a-ts") == float("-inf")
        assert _ts_sort_key(None) == float("-inf")


class TestStoreWiring:
    def test_the_ledger_sits_beside_the_thread_store(self, tmp_path):
        store = ThreadStore(tmp_path / "threads.json")
        assert store.seen_ledger()._path == tmp_path / "threads.seen.json"

    def test_channel_round_trips_through_the_thread_store(self, tmp_path):
        path = tmp_path / "threads.json"
        ThreadStore(path).set("1.1", "sess-1", channel=DM)
        assert ThreadStore(path).get_record("1.1").channel == DM

    def test_an_old_record_without_a_channel_reads_as_none(self, tmp_path):
        path = tmp_path / "threads.json"
        path.write_text(
            json.dumps({"1.1": {"session_id": "s", "channel": ""}}),
            encoding="utf-8",
        )
        assert ThreadStore(path).get_record("1.1").channel is None

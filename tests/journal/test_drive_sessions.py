"""Tests for ``tigerharness.journal.drive_sessions``: the drive-session
registry that lets tiger-memory's ``claude_transcript`` adapter skip a
journal drive's own (fat) transcript.

Coverage intent: ``register`` is an idempotent upsert that preserves the
first-sighting ``registered_at`` while refreshing ``last_seen_at`` /
``task_id`` / ``driver``; the reader (``registered_threads`` /
``_read``) is tolerant -- a missing, corrupt, or wrong-shape registry
yields the empty set ("suppress nothing", the safe direction).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tigerharness.journal import drive_sessions as ds
from tigerharness.journal.paths import JournalPaths


@pytest.fixture()
def paths(tmp_path: Path) -> JournalPaths:
    # No ensure(): register()'s atomic write creates the parent dir, so
    # the registry works on a journal root that doesn't yet exist.
    return JournalPaths(root=tmp_path / "journal")


# ---------------------------------------------------------------------------
# _read / registered_threads — tolerance
# ---------------------------------------------------------------------------

class TestReadTolerance:
    def test_missing_file_is_empty(self, paths: JournalPaths):
        assert ds._read(paths.drive_sessions_json) == {}
        assert ds.registered_threads(paths.drive_sessions_json) == set()

    def test_corrupt_json_is_empty(self, paths: JournalPaths):
        paths.root.mkdir(parents=True)
        paths.drive_sessions_json.write_text("{not json", encoding="utf-8")
        assert ds._read(paths.drive_sessions_json) == {}
        assert ds.registered_threads(paths.drive_sessions_json) == set()

    def test_non_object_top_level_is_empty(self, paths: JournalPaths):
        # A valid-JSON array is not a registry object -> ignored.
        paths.root.mkdir(parents=True)
        paths.drive_sessions_json.write_text("[1, 2, 3]", encoding="utf-8")
        assert ds._read(paths.drive_sessions_json) == {}
        assert ds.registered_threads(paths.drive_sessions_json) == set()

    def test_unreadable_dir_is_empty(self, paths: JournalPaths):
        # The registry "path" is actually a directory -> read_text raises
        # OSError -> tolerated as empty.
        paths.drive_sessions_json.mkdir(parents=True)
        assert ds.registered_threads(paths.drive_sessions_json) == set()


# ---------------------------------------------------------------------------
# register — upsert + merge semantics
# ---------------------------------------------------------------------------

class TestRegister:
    def test_new_thread_stamps_both_times_and_fields(self, paths: JournalPaths):
        ds.register(paths, "111.222", task_id="t1", driver="Anzai")
        data = json.loads(paths.drive_sessions_json.read_text())
        assert set(data) == {"111.222"}
        rec = data["111.222"]
        assert rec["task_id"] == "t1"
        assert rec["driver"] == "Anzai"
        # First sighting: both stamps set (and equal -- one clock read).
        assert rec["registered_at"]
        assert rec["last_seen_at"] == rec["registered_at"]
        assert ds.registered_threads(paths.drive_sessions_json) == {"111.222"}

    def test_driver_optional_stored_as_null(self, paths: JournalPaths):
        ds.register(paths, "111.222", task_id="t1")
        rec = json.loads(paths.drive_sessions_json.read_text())["111.222"]
        assert rec["driver"] is None

    def test_distinct_threads_accumulate(self, paths: JournalPaths):
        ds.register(paths, "a", task_id="t1", driver="Anzai")
        ds.register(paths, "b", task_id="t2", driver="Rukawa")
        assert ds.registered_threads(paths.drive_sessions_json) == {"a", "b"}

    def test_reregister_preserves_registered_at_refreshes_rest(
        self, paths: JournalPaths,
    ):
        # Seed a prior record with a known, in-the-past registered_at so we
        # can prove it survives a later claim under the same thread. Its
        # last_seen_at is recent (within the TTL) so the entry isn't pruned
        # before the merge -- this exercises the normal re-claim case (a
        # drive resumed across a few sessions, not one idle past the TTL).
        from tigerharness.journal.models import _utcnow_iso

        paths.root.mkdir(parents=True)
        paths.drive_sessions_json.write_text(json.dumps({
            "111.222": {
                "task_id": "old", "driver": "Anzai",
                "registered_at": "2020-01-01T00:00:00Z",
                "last_seen_at": _utcnow_iso(),
            }
        }), encoding="utf-8")
        ds.register(paths, "111.222", task_id="new", driver="Rukawa")
        rec = json.loads(paths.drive_sessions_json.read_text())["111.222"]
        assert rec["registered_at"] == "2020-01-01T00:00:00Z"  # preserved
        assert rec["last_seen_at"] != "2020-01-01T00:00:00Z"   # refreshed
        assert rec["task_id"] == "new"                          # most recent
        assert rec["driver"] == "Rukawa"

    def test_prior_dict_without_registered_at_resets_stamp(
        self, paths: JournalPaths,
    ):
        # A prior entry that is a dict but lacks a string registered_at
        # (hand-edited / partial) -> registered_at is (re)stamped to now.
        paths.root.mkdir(parents=True)
        paths.drive_sessions_json.write_text(
            json.dumps({"111.222": {"task_id": "x"}}), encoding="utf-8",
        )
        ds.register(paths, "111.222", task_id="y", driver="Anzai")
        rec = json.loads(paths.drive_sessions_json.read_text())["111.222"]
        assert rec["registered_at"]
        assert rec["last_seen_at"] == rec["registered_at"]

    def test_prior_not_a_dict_is_overwritten(self, paths: JournalPaths):
        # A corrupt non-dict value for the key -> overwritten cleanly.
        paths.root.mkdir(parents=True)
        paths.drive_sessions_json.write_text(
            json.dumps({"111.222": "garbage"}), encoding="utf-8",
        )
        ds.register(paths, "111.222", task_id="y", driver="Anzai")
        rec = json.loads(paths.drive_sessions_json.read_text())["111.222"]
        assert rec["task_id"] == "y"
        assert rec["registered_at"]


# ---------------------------------------------------------------------------
# cross-reader agreement: the journal-side canonical reader and the
# claude_transcript adapter's inline mirror must see the same threads, so
# the suppression hook can't silently drift from what claim writes.
# ---------------------------------------------------------------------------

class TestCrossReaderAgreement:
    def test_claude_transcript_reader_matches(self, paths: JournalPaths):
        from tigerharness.tiger_memory.sources.claude_transcript import (
            _load_drive_threads,
        )

        ds.register(paths, "a", task_id="t1", driver="Anzai")
        ds.register(paths, "b", task_id="t2")
        canonical = ds.registered_threads(paths.drive_sessions_json)
        mirror = _load_drive_threads(paths.drive_sessions_json)
        assert set(mirror) == canonical == {"a", "b"}


# ---------------------------------------------------------------------------
# TTL pruning: register() bounds registry growth by dropping entries whose
# last_seen_at is older than _REGISTRY_TTL_DAYS, fail-safe on anything it
# cannot evaluate (keep, never drop a live suppression).
# ---------------------------------------------------------------------------

class TestParseIso:
    def test_non_string_is_none(self):
        assert ds._parse_iso(None) is None
        assert ds._parse_iso(123) is None

    def test_malformed_is_none(self):
        assert ds._parse_iso("not-a-date") is None

    def test_naive_is_none(self):
        # No offset -> we don't trust it for expiry math.
        assert ds._parse_iso("2026-01-01T00:00:00") is None

    def test_aware_z_parsed(self):
        dt = ds._parse_iso("2026-01-01T00:00:00Z")
        assert dt is not None and dt.tzinfo is not None

    def test_aware_offset_parsed(self):
        dt = ds._parse_iso("2026-01-01T00:00:00+02:00")
        assert dt is not None and dt.tzinfo is not None


class TestPrune:
    NOW = "2026-06-08T12:00:00Z"

    def test_empty_stays_empty(self):
        assert ds._prune({}, self.NOW) == {}

    def test_drops_expired_keeps_fresh(self):
        data = {
            "old": {"last_seen_at": "2026-01-01T00:00:00Z"},
            "fresh": {"last_seen_at": "2026-06-07T00:00:00Z"},
        }
        assert set(ds._prune(data, self.NOW, ttl_days=30)) == {"fresh"}

    def test_unparseable_now_keeps_all(self):
        data = {"x": {"last_seen_at": "2020-01-01T00:00:00Z"}}
        assert ds._prune(data, "garbage") == data

    def test_keeps_entry_without_last_seen(self):
        data = {"x": {"task_id": "t"}}
        assert ds._prune(data, self.NOW) == data

    def test_keeps_non_dict_entry(self):
        data = {"x": "garbage"}
        assert ds._prune(data, self.NOW) == data

    def test_keeps_naive_last_seen(self):
        # Naive stamp is unparseable-as-aware -> kept (fail-safe), even
        # though the date itself is ancient.
        data = {"x": {"last_seen_at": "2020-01-01T00:00:00"}}
        assert ds._prune(data, self.NOW) == data


class TestRegisterPrunes:
    def test_register_prunes_expired_entries(self, paths: JournalPaths):
        from tigerharness.journal.models import _utcnow_iso

        paths.root.mkdir(parents=True)
        paths.drive_sessions_json.write_text(json.dumps({
            "old": {
                "task_id": "o", "driver": None,
                "registered_at": "2020-01-01T00:00:00Z",
                "last_seen_at": "2020-01-01T00:00:00Z",
            },
            "fresh": {
                "task_id": "f", "driver": None,
                "registered_at": _utcnow_iso(),
                "last_seen_at": _utcnow_iso(),
            },
        }), encoding="utf-8")
        ds.register(paths, "new", task_id="n", driver="Anzai")
        # "old" is dropped on write; "fresh" and the new claim remain.
        assert ds.registered_threads(paths.drive_sessions_json) == {
            "fresh", "new",
        }

    def test_reclaiming_expired_thread_restamps_fresh(
        self, paths: JournalPaths,
    ):
        # A thread last seen long ago is pruned, then re-added as new (its
        # registered_at is "now", not the ancient stamp).
        paths.root.mkdir(parents=True)
        paths.drive_sessions_json.write_text(json.dumps({
            "111.222": {
                "task_id": "old", "driver": "Anzai",
                "registered_at": "2019-01-01T00:00:00Z",
                "last_seen_at": "2019-01-01T00:00:00Z",
            }
        }), encoding="utf-8")
        ds.register(paths, "111.222", task_id="new", driver="Rukawa")
        rec = json.loads(paths.drive_sessions_json.read_text())["111.222"]
        assert rec["registered_at"] != "2019-01-01T00:00:00Z"  # restamped
        assert rec["task_id"] == "new"

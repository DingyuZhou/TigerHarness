"""ThreadStore persistence tests."""

from __future__ import annotations

import json
from pathlib import Path

from tigerharness.slack_bridge.persistence import ThreadStore, default_state_path


def test_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "threads.json"
    store = ThreadStore(path)
    store.set("1234.5678", "session-abc")
    assert store.get("1234.5678") == "session-abc"


def test_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "threads.json"
    store1 = ThreadStore(path)
    store1.set("ts1", "sess1")
    store2 = ThreadStore(path)
    assert store2.get("ts1") == "sess1"


def test_empty_session_id_ignored(tmp_path: Path) -> None:
    path = tmp_path / "threads.json"
    store = ThreadStore(path)
    store.set("ts1", "")
    assert store.get("ts1") is None


def test_no_write_on_same_value(tmp_path: Path) -> None:
    path = tmp_path / "threads.json"
    store = ThreadStore(path)
    store.set("ts1", "sess1")
    mtime1 = path.stat().st_mtime_ns
    store.set("ts1", "sess1")  # same value
    mtime2 = path.stat().st_mtime_ns
    assert mtime1 == mtime2


def test_corrupt_file_recovers(tmp_path: Path) -> None:
    path = tmp_path / "threads.json"
    path.write_text("not valid json {{{")
    store = ThreadStore(path)
    assert store.get("anything") is None
    # Can still write new data
    store.set("ts1", "sess1")
    assert store.get("ts1") == "sess1"


def test_non_dict_file_recovers(tmp_path: Path) -> None:
    path = tmp_path / "threads.json"
    path.write_text(json.dumps(["not", "a", "dict"]))
    store = ThreadStore(path)
    assert store.get("anything") is None


def test_default_state_path_env_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TIGERHARNESS_SLACK_STATE_DIR", str(tmp_path / "custom"))
    result = default_state_path()
    assert result == tmp_path / "custom" / "threads.json"


def test_default_state_path_xdg(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("TIGERHARNESS_SLACK_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    result = default_state_path()
    assert result == tmp_path / "slack-bridge" / "threads.json"


# ----- turn metadata (compact-idle support) -----

USAGE = {"input_tokens": 5, "cache_read_input_tokens": 100}


def test_metadata_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "threads.json"
    store = ThreadStore(path)
    store.set(
        "1.1", "sess", persona="Ayako", team="Shohoku",
        last_usage=USAGE, last_turn_at="2026-07-22T00:00:00+00:00",
        in_flight=True,
    )
    rec = ThreadStore(path).get_record("1.1")
    assert rec.team == "Shohoku"
    assert rec.last_usage == USAGE
    assert rec.last_turn_at == "2026-07-22T00:00:00+00:00"
    assert rec.in_flight is True


def test_unset_kwargs_preserve_stored_metadata(tmp_path: Path) -> None:
    """The two-argument legacy call must not wipe metadata a newer
    caller stamped."""
    path = tmp_path / "threads.json"
    store = ThreadStore(path)
    store.set(
        "1.1", "sess", persona="Ayako", team="Shohoku",
        last_usage=USAGE, last_turn_at="t", in_flight=False,
    )
    store.set("1.1", "sess", persona="Ayako")  # legacy shape
    rec = store.get_record("1.1")
    assert rec.team == "Shohoku" and rec.last_usage == USAGE
    assert rec.last_turn_at == "t"


def test_explicit_none_clears_usage(tmp_path: Path) -> None:
    path = tmp_path / "threads.json"
    store = ThreadStore(path)
    store.set("1.1", "sess", persona="A", team="T", last_usage=USAGE)
    store.set("1.1", "sess", persona="A", last_usage=None)
    rec = store.get_record("1.1")
    assert rec.last_usage is None and rec.team == "T"


def test_legacy_records_read_default_metadata(tmp_path: Path) -> None:
    path = tmp_path / "threads.json"
    path.write_text(
        '{"1.1": {"session_id": "s", "persona": "A"}, "2.2": "bare-sid"}'
    )
    store = ThreadStore(path)
    rec = store.get_record("1.1")
    assert rec.team is None and rec.last_usage is None
    assert rec.last_turn_at is None and rec.in_flight is False
    assert store.get("2.2") == "bare-sid"


def test_malformed_metadata_tolerated(tmp_path: Path) -> None:
    path = tmp_path / "threads.json"
    path.write_text(
        '{"1.1": {"session_id": "s", "team": 7, "last_usage": "x",'
        ' "last_turn_at": 3, "in_flight": "yes"}}'
    )
    rec = ThreadStore(path).get_record("1.1")
    assert rec.team is None and rec.last_usage is None
    assert rec.last_turn_at is None and rec.in_flight is True


def test_mark_in_flight(tmp_path: Path) -> None:
    path = tmp_path / "threads.json"
    store = ThreadStore(path)
    store.mark_in_flight("ghost", True)  # no record -> silent no-op
    assert store.get_record("ghost") is None
    store.set("1.1", "sess", persona="A", team="T", last_usage=USAGE)
    store.mark_in_flight("1.1", True)
    rec = ThreadStore(path).get_record("1.1")
    assert rec.in_flight is True and rec.last_usage == USAGE
    store.mark_in_flight("1.1", True)  # same value -> no write needed
    store.mark_in_flight("1.1", False)
    assert ThreadStore(path).get_record("1.1").in_flight is False


def test_records_snapshot_is_a_copy(tmp_path: Path) -> None:
    store = ThreadStore(tmp_path / "threads.json")
    store.set("1.1", "sess")
    snap = store.records()
    snap.clear()
    assert store.get("1.1") == "sess"


# ----- cross-process merge-patch writes (two writers, one file) -----


def test_two_stores_never_clobber_each_other(tmp_path: Path) -> None:
    """Bridge daemon and compact-idle CLI each hold their own ThreadStore
    over the same file. A write must publish only its own record's delta,
    merged over CURRENT disk state -- never its stale in-memory snapshot."""
    path = tmp_path / "threads.json"
    a = ThreadStore(path)
    b = ThreadStore(path)  # loaded before a writes anything
    a.set("1.1", "sess-1", persona="Ayako", team="T", last_usage=USAGE)
    b.set("2.2", "sess-2", persona="Rukawa")  # stale snapshot lacks 1.1
    disk = ThreadStore(path)
    assert disk.get("1.1") == "sess-1"  # survived b's write
    assert disk.get("2.2") == "sess-2"
    assert disk.get_record("1.1").last_usage == USAGE


def test_set_merges_against_disk_not_memory(tmp_path: Path) -> None:
    """The _UNSET-preserve base is the on-disk record at write time: a
    latch cleared by another process must not be resurrected by a stale
    in-memory value."""
    path = tmp_path / "threads.json"
    bridge_store = ThreadStore(path)
    bridge_store.set("1.1", "sess", team="T", last_usage=USAGE)
    # Another process (compact-idle) clears the latch on disk.
    ThreadStore(path).set("1.1", "sess", last_usage=None)
    # The bridge writes an unrelated field; its stale memory of USAGE
    # must not come back.
    bridge_store.set("1.1", "sess", persona="Ayako")
    assert ThreadStore(path).get_record("1.1").last_usage is None


def test_mark_in_flight_merges_against_disk(tmp_path: Path) -> None:
    path = tmp_path / "threads.json"
    a = ThreadStore(path)
    a.set("1.1", "sess", team="T")
    b = ThreadStore(path)
    a.set("1.1", "sess", last_usage=USAGE)  # disk now has usage
    b.mark_in_flight("1.1", True)  # b's snapshot predates the usage
    rec = ThreadStore(path).get_record("1.1")
    assert rec.in_flight is True and rec.last_usage == USAGE


def test_clear_in_flight_all(tmp_path: Path) -> None:
    path = tmp_path / "threads.json"
    store = ThreadStore(path)
    store.set("1.1", "s1", in_flight=True)
    store.set("2.2", "s2", in_flight=False)
    ThreadStore(path).clear_in_flight_all()
    reloaded = ThreadStore(path)
    assert reloaded.get_record("1.1").in_flight is False
    assert reloaded.get_record("2.2").in_flight is False
    # Idempotent second call (no-write path).
    reloaded.clear_in_flight_all()


def test_usage_sanitized_to_token_allowlist(tmp_path: Path) -> None:
    """Exotic usage payloads (extra keys, non-numeric, non-JSON values)
    must never reach the save path -- only the three token counts the
    threshold check reads are stored."""
    path = tmp_path / "threads.json"
    store = ThreadStore(path)
    store.set("1.1", "sess", last_usage={
        "input_tokens": 5,
        "cache_read_input_tokens": 100.0,
        "server_tool_use": {"web_search_requests": object()},  # unserializable
        "output_tokens": 9,  # not in the allowlist
        "cache_creation_input_tokens": "not-a-number",
    })
    rec = ThreadStore(path).get_record("1.1")
    assert rec.last_usage == {"input_tokens": 5, "cache_read_input_tokens": 100}
    store.set("2.2", "sess2", last_usage={"only": "junk"})
    assert ThreadStore(path).get_record("2.2").last_usage is None
    store.set("3.3", "sess3", last_usage="not-even-a-dict")
    assert ThreadStore(path).get_record("3.3").last_usage is None


def test_legacy_two_arg_set_preserves_persona(tmp_path: Path) -> None:
    """persona now follows the same preserve-unless-passed rule as the
    metadata fields (explicit None still clears)."""
    path = tmp_path / "threads.json"
    store = ThreadStore(path)
    store.set("1.1", "sess", persona="Ayako")
    store.set("1.1", "sess-new")  # legacy shape: session id only
    assert store.get_record("1.1").persona == "Ayako"
    store.set("1.1", "sess-new", persona=None)
    assert store.get_record("1.1").persona is None

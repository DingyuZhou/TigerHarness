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

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

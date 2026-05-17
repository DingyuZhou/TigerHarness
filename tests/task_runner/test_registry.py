"""Registry persistence: roundtrip, prefix resolution, atomic write,
corrupt-file recovery, cancel-flag semantics."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tigerharness.task_runner.registry import JobMeta, JobStore, new_job_id


def _make_meta(job_id: str, **over) -> JobMeta:
    base = dict(
        job_id=job_id,
        persona="test-agent",
        prompt_chars=42,
        max_iters=5,
        compact_every=5,
        continuation="continue",
        name="test-job",
        cwd="/tmp/cwd",
        started_at=time.time(),
        status="pending",
        pid=None,
        current_iter=0,
        session_id="",
        last_update=time.time(),
    )
    base.update(over)
    return JobMeta(**base)


def test_roundtrip(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    m = _make_meta(new_job_id())
    store.set(m)
    back = store.get(m.job_id)
    assert back == m


def test_multiple_jobs_persist_independently(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    a = _make_meta("aaaaaaaa", name="A")
    b = _make_meta("bbbbbbbb", name="B")
    store.set(a)
    store.set(b)
    all_ = store.all()
    assert all_["aaaaaaaa"].name == "A"
    assert all_["bbbbbbbb"].name == "B"


def test_corrupt_jobs_json_recovers(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    store.registry.write_text("not-json-at-all{")
    assert store.all() == {}
    m = _make_meta("cafe1234")
    store.set(m)
    assert store.get("cafe1234") == m


def test_unknown_fields_in_jobs_json_dropped_silently(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    bad = {"abcdef00": {"job_id": "abcdef00", "unknown_field": True}}
    store.registry.write_text(json.dumps(bad))
    assert store.all() == {}


def test_atomic_write_no_tmp_leftover(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    store.set(_make_meta("12345678"))
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".jobs.json.")]
    assert leftovers == [], f"tmp files left behind: {leftovers}"


def test_resolve_prefix_unique(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    store.set(_make_meta("ab12cd34"))
    store.set(_make_meta("11112222"))
    assert store.resolve_prefix("ab").job_id == "ab12cd34"
    assert store.resolve_prefix("ab12").job_id == "ab12cd34"
    assert store.resolve_prefix("11").job_id == "11112222"


def test_resolve_prefix_full_id(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    store.set(_make_meta("ab12cd34"))
    assert store.resolve_prefix("ab12cd34").job_id == "ab12cd34"


def test_resolve_prefix_ambiguous(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    store.set(_make_meta("aaaaaaaa"))
    store.set(_make_meta("aaaabbbb"))
    with pytest.raises(KeyError, match="ambiguous"):
        store.resolve_prefix("aaaa")


def test_resolve_prefix_missing(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    with pytest.raises(KeyError, match="no job matches"):
        store.resolve_prefix("zz")


def test_cancel_flag_roundtrip(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    jid = "deadbeef"
    store.set(_make_meta(jid))
    assert not store.is_cancel_requested(jid)
    store.request_cancel(jid)
    assert store.is_cancel_requested(jid)
    assert store.cancel_flag(jid).read_text().strip()


def test_per_job_paths_consistent(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    jid = "12abcdef"
    assert store.job_dir(jid).exists()
    assert store.run_log(jid).parent == store.job_dir(jid)
    assert store.result_path(jid).parent == store.job_dir(jid)
    assert store.prompt_path(jid).parent == store.job_dir(jid)


def test_delete(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    store.set(_make_meta("aabbccdd"))
    assert store.get("aabbccdd") is not None
    store.delete("aabbccdd")
    assert store.get("aabbccdd") is None


def test_default_state_path_uses_env(monkeypatch, tmp_path: Path) -> None:
    from tigerharness.task_runner.registry import default_state_path
    monkeypatch.setenv("TIGERHARNESS_STATE_DIR", str(tmp_path / "custom"))
    assert default_state_path() == tmp_path / "custom"


def test_default_state_path_xdg_fallback(monkeypatch, tmp_path: Path) -> None:
    from tigerharness.task_runner.registry import default_state_path
    monkeypatch.delenv("TIGERHARNESS_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert default_state_path() == tmp_path / "tigerharness-tasks"

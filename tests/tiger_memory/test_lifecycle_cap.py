"""Cost/scope cap (P1.2 / Lever 1.4) — unit + end-to-end."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from textwrap import dedent

from datetime import datetime, timezone

from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.lifecycle import (
    Decision,
    SUMMARIZE_NEW,
    _cap_reason,
    _process_decisions,
    rebuild,
)
from tigerharness.tiger_memory.sources.base import SourceRecord
from tigerharness.tiger_memory.store import Store
from tigerharness.tiger_memory.summarizers import MockSummarizer


# ---- _cap_reason: every branch -------------------------------------------


def test_cap_reason_uncapped_returns_none() -> None:
    # Both bounds None (bootstrap / resummarize) -> never trips.
    assert _cap_reason(99, 1000.0, None, None) is None


def test_cap_reason_session_cap_trips() -> None:
    assert _cap_reason(5, 0.0, 5, None) == "session_cap"
    # Under the count cap, no usd cap -> continue.
    assert _cap_reason(2, 0.0, 5, None) is None


def test_cap_reason_usd_cap_trips() -> None:
    assert _cap_reason(0, 10.0, None, 10.0) == "usd_cap"
    # Under the usd cap -> continue.
    assert _cap_reason(0, 2.0, None, 10.0) is None


def test_cap_trips_without_metrics(tmp_path: Path) -> None:
    """A cap can stop a run even when no metrics object is threaded
    through (e.g. a direct caller). Nothing is processed."""
    cfg_path = tmp_path / "m.yaml"
    cfg_path.write_text(dedent(f"""\
        agent: {{name: T, role: T}}
        store: {{root: {tmp_path}/memory}}
        sources:
          - kind: claude_code
            project_path: {tmp_path}/proj/
        summarizer: {{backend: anthropic, model: claude-sonnet-4-6, prompts: default/v1}}
        rebuild:
          lock_path: {tmp_path}/lock
    """))
    cfg = load_config(cfg_path)
    store = Store(cfg.store.root)
    store.init_layout()
    now = datetime.now(timezone.utc)
    rec = SourceRecord(
        conversation_uuid="abcdabcd-0000-0000-0000-000000000000",
        source="claude_code",
        source_id="abcdabcd-0000-0000-0000-000000000000",
        first_event_at=now,
        last_event_at=now,
        activity_mtime=time.time(),
        content="some conversation",
        raw_path=Path("/dev/null"),
    )
    # max_sessions=0 trips immediately; metrics omitted (None branch).
    cost = _process_decisions(
        [Decision(rec, SUMMARIZE_NEW)], store, cfg, MockSummarizer(),
        max_sessions=0,
    )
    assert cost == 0
    assert list(store.paths.archive.glob("*.md")) == []


# ---- end-to-end: rebuild stops at the session cap, resumes next time ------


def _make_jsonl(path: Path, uuid: str, ts: str) -> None:
    path.write_text(
        json.dumps({
            "type": "user", "timestamp": ts,
            "message": {"role": "user", "content": f"hello {uuid}"},
        }) + "\n"
        + json.dumps({
            "type": "assistant", "timestamp": ts,
            "message": {"role": "assistant", "content": "ack"},
        }) + "\n"
    )


def _backdate(path: Path, hours: float) -> None:
    new_time = time.time() - hours * 3600
    os.utime(path, (new_time, new_time))


def _config(tmp_path: Path, project: Path) -> Path:
    cfg_path = tmp_path / "cap.yaml"
    cfg_path.write_text(dedent(f"""\
        agent:
          name: TestTiger
          role: "Test consumer."
        store:
          root: {tmp_path}/memory
        sources:
          - kind: claude_code
            project_path: {project}
        summarizer:
          backend: anthropic
          model: claude-opus-4-7
          prompts: default/v1
        rebuild:
          lock_path: {tmp_path}/test.lock
          idle_threshold_hours: 0
        cap:
          max_sessions_per_rebuild: 2
    """))
    return cfg_path


def test_session_cap_defers_then_resumes(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    uids = [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
        "33333333-3333-3333-3333-333333333333",
    ]
    for u in uids:
        f = project / f"{u}.jsonl"
        _make_jsonl(f, u, "2026-05-14T08:21:36.000Z")
        _backdate(f, hours=3)  # past idle threshold

    cfg = load_config(_config(tmp_path, project))
    store = Store(cfg.store.root)

    # Rebuild #1: cap=2 -> processes 2, defers 1.
    rebuild(cfg, store, summarizer_override=MockSummarizer())
    archives_1 = list(store.paths.archive.glob("*.md"))
    assert len(archives_1) == 2
    state_1 = store.read_state()
    assert state_1["metrics"]["sessions_processed"] == 2
    assert state_1["metrics"]["stopped_reason"] == "session_cap"

    # Rebuild #2: the deferred session is re-discovered (no archive) and
    # processed; the first two are clean and skipped. Resumability with
    # no extra state.
    rebuild(cfg, store, summarizer_override=MockSummarizer())
    archives_2 = list(store.paths.archive.glob("*.md"))
    assert len(archives_2) == 3
    state_2 = store.read_state()
    assert state_2["metrics"]["sessions_processed"] == 1
    assert state_2["metrics"]["stopped_reason"] is None

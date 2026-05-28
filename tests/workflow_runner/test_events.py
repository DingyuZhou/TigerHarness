"""Unit tests for ``tigerharness.workflow_runner.events``."""

from __future__ import annotations

import json

import pytest

from tigerharness.workflow_runner.events import (
    append_event,
    read_events,
    tail_events,
)


def test_append_then_read_round_trip(tmp_path):
    path = tmp_path / "events.jsonl"
    e1 = append_event(path, "task_started", task_id="t1")
    e2 = append_event(path, "step_started", step="01", iter=1)
    events = read_events(path)
    assert len(events) == 2
    assert events[0].kind == "task_started"
    assert events[0].extra == {"task_id": "t1"}
    assert events[1].extra == {"step": "01", "iter": 1}
    # In-memory event matches the persisted one.
    assert events[0].to_dict() == e1.to_dict()
    assert events[1].to_dict() == e2.to_dict()


def test_append_creates_parent_dir(tmp_path):
    path = tmp_path / "deep" / "nest" / "events.jsonl"
    append_event(path, "task_started")
    assert path.exists()


def test_append_one_object_per_line(tmp_path):
    path = tmp_path / "events.jsonl"
    append_event(path, "a", x=1)
    append_event(path, "b", y=2)
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    for line in lines:
        json.loads(line)  # each must be valid JSON on its own


def test_append_uses_explicit_ts(tmp_path):
    path = tmp_path / "events.jsonl"
    e = append_event(path, "x", ts="2026-05-28T12:00:00Z")
    assert e.ts == "2026-05-28T12:00:00Z"


def test_append_accepts_explicit_ts_kwarg(tmp_path):
    """``append_event``'s signature makes ``kind`` and ``ts`` explicit
    parameters, so a caller cannot accidentally shadow them via
    ``**fields``. Just confirm passing ``ts`` works (the reserved-key
    check inside :class:`Event` is exercised in ``test_models.py``)."""
    path = tmp_path / "events.jsonl"
    e = append_event(path, "x", ts="2026-05-28T12:00:00Z", n=1)
    assert e.ts == "2026-05-28T12:00:00Z"
    assert e.extra == {"n": 1}


def test_read_events_missing_file_returns_empty(tmp_path):
    assert read_events(tmp_path / "nope.jsonl") == []


def test_read_events_skips_blank_and_corrupt_lines(tmp_path):
    path = tmp_path / "events.jsonl"
    append_event(path, "ok", x=1)
    # Inject a corrupt line and a blank line manually.
    with open(path, "a") as fh:
        fh.write("not-json\n\n")
        fh.write('{"ts":"2026-05-28T12:00:00Z","kind":"ok2","y":2}\n')
    events = read_events(path)
    assert [e.kind for e in events] == ["ok", "ok2"]


def test_read_events_skips_non_object_lines(tmp_path):
    path = tmp_path / "events.jsonl"
    with open(path, "w") as fh:
        fh.write("[1,2,3]\n")  # valid JSON but not an object
        fh.write('{"ts":"2026-05-28T12:00:00Z","kind":"ok"}\n')
    events = read_events(path)
    assert [e.kind for e in events] == ["ok"]


def test_read_events_skips_validation_failures(tmp_path):
    path = tmp_path / "events.jsonl"
    with open(path, "w") as fh:
        # Missing ts; valid JSON object but fails Event.from_dict.
        fh.write('{"kind": "broken"}\n')
        fh.write('{"ts": "2026-05-28T12:00:00Z", "kind": "good"}\n')
    events = read_events(path)
    assert [e.kind for e in events] == ["good"]


def test_read_events_tolerates_unterminated_last_line(tmp_path):
    path = tmp_path / "events.jsonl"
    with open(path, "w") as fh:
        fh.write('{"ts":"2026-05-28T12:00:00Z","kind":"a"}\n')
        fh.write('{"ts":"2026-05-28T12:00:00Z","kind":"b"}')  # no \n
    events = read_events(path)
    assert [e.kind for e in events] == ["a", "b"]


def test_tail_events(tmp_path):
    path = tmp_path / "events.jsonl"
    for i in range(10):
        append_event(path, f"k{i}", n=i)
    tail = tail_events(path, 3)
    assert [e.kind for e in tail] == ["k7", "k8", "k9"]


def test_tail_events_zero_and_oversize(tmp_path):
    path = tmp_path / "events.jsonl"
    append_event(path, "only")
    assert tail_events(path, 0) == []
    big = tail_events(path, 50)
    assert [e.kind for e in big] == ["only"]


def test_tail_events_missing_file(tmp_path):
    assert tail_events(tmp_path / "nope.jsonl", 5) == []


def test_tail_events_negative_raises(tmp_path):
    path = tmp_path / "events.jsonl"
    with pytest.raises(ValueError):
        tail_events(path, -1)


def test_concurrent_appends_serialise(tmp_path):
    """Even across processes, every appended record must be a complete
    line: ``O_APPEND`` + write under PIPE_BUF guarantees this."""
    import subprocess
    import sys
    import textwrap

    path = tmp_path / "events.jsonl"
    helper = textwrap.dedent(f"""
        from tigerharness.workflow_runner.events import append_event
        for i in range(50):
            append_event({str(path)!r}, "ping", n=i, worker="A")
    """)
    helper_b = textwrap.dedent(f"""
        from tigerharness.workflow_runner.events import append_event
        for i in range(50):
            append_event({str(path)!r}, "ping", n=i, worker="B")
    """)
    p1 = subprocess.Popen([sys.executable, "-c", helper])
    p2 = subprocess.Popen([sys.executable, "-c", helper_b])
    p1.wait(timeout=30)
    p2.wait(timeout=30)
    assert p1.returncode == 0
    assert p2.returncode == 0

    events = read_events(path)
    # No torn writes => every event parsed cleanly => total = 100.
    assert len(events) == 100
    # Roughly 50 from each worker.
    a = sum(1 for e in events if e.extra.get("worker") == "A")
    b = sum(1 for e in events if e.extra.get("worker") == "B")
    assert a == 50
    assert b == 50

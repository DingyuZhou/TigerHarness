"""Edge-case coverage for incidental lines whose previous coverage came from
tests retired in the bounded-store revamp (b1-dev-3).

These exercise pre-existing branches in still-live modules (claude_transcript
discovery cutoff / empty-timestamp / slack-channel / briefing-read drop, the
store lock liveness edges, the anthropic code-fence stripper, and the base
summarizer cost estimate) that lost their incidental coverage when the old
lifecycle/rag/drill tests were deleted.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from tigerharness.tiger_memory.sources.claude_transcript import (
    ClaudeTranscriptAdapter,
)


# ----- claude_transcript discovery cutoff (166-167) ------------------------


def test_discover_skips_jsonl_older_than_cutoff(tmp_path: Path) -> None:
    import os
    import time

    proj = tmp_path / "proj"
    proj.mkdir()
    old = proj / f"{uuid4()}.jsonl"
    old.write_text(json.dumps({"type": "user", "timestamp": "2020-01-01T00:00:00Z",
                               "message": {"content": "hi"}}) + "\n")
    # Set mtime to ~100 days ago (older than the 7-day cutoff).
    ancient = time.time() - 100 * 86400
    os.utime(old, (ancient, ancient))
    adapter = ClaudeTranscriptAdapter(project_path=proj, max_age_days=7,
                                      include_unattributed=True)
    assert list(adapter.discover()) == []


def test_discover_cutoff_stat_oserror_skips(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    jsonl = proj / f"{uuid4()}.jsonl"
    jsonl.write_text(json.dumps({"type": "user", "timestamp": "2026-06-01T00:00:00Z",
                                 "message": {"content": "hi"}}) + "\n")
    adapter = ClaudeTranscriptAdapter(project_path=proj, max_age_days=7,
                                      include_unattributed=True)
    real_stat = Path.stat

    def boom(self, *a, **kw):
        if self == jsonl:
            raise OSError("stat failed")
        return real_stat(self, *a, **kw)

    with patch.object(Path, "stat", boom):
        assert list(adapter.discover()) == []


# ----- claude_transcript _record_for: no timestamps (272) ------------------


def test_record_for_no_timestamps_returns_none(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    jsonl = proj / f"{uuid4()}.jsonl"
    # A user event with content but NO timestamp → ts_events empty → None.
    jsonl.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
    adapter = ClaudeTranscriptAdapter(project_path=proj)
    assert adapter._record_for(jsonl, {}) is None


# ----- claude_transcript slack-channel capture (293) + briefing-read drop ---


def test_record_for_captures_slack_channel(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    jsonl = proj / f"{uuid4()}.jsonl"
    events = [
        {"type": "user", "timestamp": "2026-06-01T10:00:00Z",
         "message": {"content": "[slack-bridge-context] channel=C123 "
                                 "thread_ts=1.2 user=U9\nreal question here"}},
        {"type": "assistant", "timestamp": "2026-06-01T10:01:00Z",
         "message": {"content": [{"type": "text", "text": "an answer"}]}},
    ]
    jsonl.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    adapter = ClaudeTranscriptAdapter(project_path=proj)
    rec = adapter._record_for(jsonl, {})
    assert rec is not None
    assert "an answer" in rec.content


def test_record_for_drops_briefing_read_tool_result(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    jsonl = proj / f"{uuid4()}.jsonl"
    events = [
        {"type": "assistant", "timestamp": "2026-06-01T10:00:00Z",
         "message": {"content": [
             {"type": "tool_use", "id": "tu-1", "name": "Read",
              "input": {"file_path": "/x/memory/sai/briefing/MANIFEST.md"}},
         ]}},
        {"type": "user", "timestamp": "2026-06-01T10:00:05Z",
         "message": {"content": [
             {"type": "tool_result", "tool_use_id": "tu-1",
              "content": "the whole briefing dump that should be dropped"},
         ]}},
        {"type": "assistant", "timestamp": "2026-06-01T10:01:00Z",
         "message": {"content": [{"type": "text", "text": "kept answer"}]}},
    ]
    jsonl.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    adapter = ClaudeTranscriptAdapter(project_path=proj)
    rec = adapter._record_for(jsonl, {})
    assert rec is not None
    assert "kept answer" in rec.content
    assert "should be dropped" not in rec.content


def test_record_for_briefing_read_without_string_id(tmp_path: Path) -> None:
    """A briefing-read tool_use whose id is missing/non-string still drops
    (the 428->430 branch: skip the id-capture, still `continue`)."""
    proj = tmp_path / "proj"
    proj.mkdir()
    jsonl = proj / f"{uuid4()}.jsonl"
    events = [
        {"type": "assistant", "timestamp": "2026-06-01T10:00:00Z",
         "message": {"content": [
             # No "id" field → isinstance(..., str) is False.
             {"type": "tool_use", "name": "Read",
              "input": {"file_path": "/x/memory/sai/briefing/MANIFEST.md"}},
             {"type": "text", "text": "answer body"},
         ]}},
    ]
    jsonl.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    adapter = ClaudeTranscriptAdapter(project_path=proj)
    rec = adapter._record_for(jsonl, {})
    assert rec is not None
    assert "answer body" in rec.content
    assert "tool_use: Read" not in rec.content  # briefing read dropped


# ----- store lock liveness edges (255-256, 310-312) ------------------------


def test_lock_garbage_pid_is_reclaimed(tmp_path: Path) -> None:
    from tigerharness.tiger_memory.store import Store

    store = Store(tmp_path / "mem")
    lock = tmp_path / "lk.lock"
    lock.write_text("not-a-pid")  # unparseable → holder_pid = -1 → reclaimed
    assert store._try_acquire_lock(lock, timeout_minutes=60) is True


def test_pid_alive_permission_error_is_alive() -> None:
    from tigerharness.tiger_memory import store as store_mod

    def boom(pid, sig):
        raise PermissionError("not allowed to signal")

    with patch.object(store_mod.os, "kill", boom):
        assert store_mod._pid_alive(4242) is True


# ----- anthropic code-fence stripper (140->142) ----------------------------


def test_strip_codefence_no_newline() -> None:
    from tigerharness.tiger_memory.summarizers.anthropic import _strip_codefence

    # A fence with no newline after it: first_nl <= 0 → the slice is skipped.
    assert _strip_codefence("```") == ""
    # An opening fence with a language tag and no newline → unchanged body.
    assert _strip_codefence("```json") == "```json"


# ----- base summarizer cost estimate default (45) --------------------------


def test_base_cost_estimate_default_zero() -> None:
    from tigerharness.tiger_memory.summarizers.mock import MockSummarizer

    assert MockSummarizer().cost_estimate_usd(prompt_tokens=10, output_tokens=5) == 0.0

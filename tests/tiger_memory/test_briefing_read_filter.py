"""Regression tests for the briefing-read filter in the transcript adapter."""
from __future__ import annotations

import json
from pathlib import Path

from tigerharness.tiger_memory.sources import ClaudeTranscriptAdapter


def _write_jsonl(path: Path, events: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def test_briefing_reads_filtered_out(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    uid = "deadbeef-1234-5678-9012-345678901234"
    _write_jsonl(project / f"{uid}.jsonl", [
        {"type": "user", "timestamp": "2026-05-15T08:00:00.000Z",
         "message": {"role": "user", "content": "Hi Sai, what's TSMOM?"}},
        {"type": "assistant", "timestamp": "2026-05-15T08:00:01.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "tu1", "name": "Read",
              "input": {"file_path": "/path/memory/briefing/MANIFEST.md"}},
         ]}},
        {"type": "user", "timestamp": "2026-05-15T08:00:02.000Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "tu1",
              "content": "MANIFEST contents here..."},
         ]}},
        {"type": "assistant", "timestamp": "2026-05-15T08:00:03.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "TSMOM is time-series momentum."},
         ]}},
    ])
    adapter = ClaudeTranscriptAdapter(project_path=project)
    [rec] = list(adapter.discover())
    assert "MANIFEST" not in rec.content
    assert "tool_use: Read" not in rec.content
    assert "TSMOM is time-series momentum" in rec.content


def test_non_briefing_reads_kept(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    uid = "deadbeef-1234-5678-9012-345678901234"
    _write_jsonl(project / f"{uid}.jsonl", [
        {"type": "assistant", "timestamp": "2026-05-15T08:00:00.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "tu1", "name": "Read",
              "input": {"file_path": "strategy-research/something.py"}},
         ]}},
        {"type": "user", "timestamp": "2026-05-15T08:00:01.000Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "tu1",
              "content": "python source ..."},
         ]}},
    ])
    adapter = ClaudeTranscriptAdapter(project_path=project)
    [rec] = list(adapter.discover())
    assert "tool_use: Read" in rec.content
    assert "tool_result" in rec.content


def test_bash_cat_of_briefing_filtered(tmp_path: Path) -> None:
    """`cat memory/.../briefing/...` via Bash should also be filtered."""
    project = tmp_path / "project"
    project.mkdir()
    uid = "deadbeef-1234-5678-9012-345678901234"
    _write_jsonl(project / f"{uid}.jsonl", [
        {"type": "user", "timestamp": "2026-05-15T08:00:00.000Z",
         "message": {"role": "user", "content": "What's TSMOM?"}},
        {"type": "assistant", "timestamp": "2026-05-15T08:00:01.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "tu1", "name": "Bash",
              "input": {"command": "cat /path/memory/sai/briefing/MANIFEST.md"}},
         ]}},
        {"type": "user", "timestamp": "2026-05-15T08:00:02.000Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "tu1",
              "content": "MANIFEST contents..."},
         ]}},
        {"type": "assistant", "timestamp": "2026-05-15T08:00:03.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "TSMOM is time-series momentum."},
         ]}},
    ])
    adapter = ClaudeTranscriptAdapter(project_path=project)
    [rec] = list(adapter.discover())
    assert "MANIFEST contents" not in rec.content
    assert "tool_use: Bash" not in rec.content
    assert "TSMOM is time-series momentum" in rec.content


def test_glob_of_briefing_filtered(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    uid = "deadbeef-1234-5678-9012-345678901234"
    _write_jsonl(project / f"{uid}.jsonl", [
        {"type": "user", "timestamp": "2026-05-15T08:00:00.000Z",
         "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "timestamp": "2026-05-15T08:00:01.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "tu1", "name": "Glob",
              "input": {"pattern": "**/*.md",
                        "path": "memory/sai/briefing/"}},
         ]}},
        {"type": "assistant", "timestamp": "2026-05-15T08:00:02.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "Done."},
         ]}},
    ])
    adapter = ClaudeTranscriptAdapter(project_path=project)
    [rec] = list(adapter.discover())
    assert "tool_use: Glob" not in rec.content
    assert "Done" in rec.content


def test_bridge_context_block_stripped(tmp_path: Path) -> None:
    """Slack-bridge appends a [bridge-context] metadata block; strip it."""
    project = tmp_path / "project"
    project.mkdir()
    uid = "deadbeef-1234-5678-9012-345678901234"
    user_text = (
        "Hi Sai, please summarize TSMOM.\n\n"
        "[bridge-context]\nslack_thread_ts: 1778736494.495229\n"
        "slack_channel: D0B305E1QMV"
    )
    _write_jsonl(project / f"{uid}.jsonl", [
        {"type": "user", "timestamp": "2026-05-15T08:00:00.000Z",
         "message": {"role": "user", "content": user_text}},
        {"type": "assistant", "timestamp": "2026-05-15T08:00:01.000Z",
         "message": {"role": "assistant", "content": "TSMOM is..."}},
    ])
    adapter = ClaudeTranscriptAdapter(project_path=project)
    [rec] = list(adapter.discover())
    assert "[bridge-context]" not in rec.content
    assert "slack_thread_ts" not in rec.content
    assert "TSMOM" in rec.content
    assert "summarize TSMOM" in rec.content  # original message preserved


def test_unrelated_bash_kept(tmp_path: Path) -> None:
    """Bash calls without memory/briefing paths must pass through."""
    project = tmp_path / "project"
    project.mkdir()
    uid = "deadbeef-1234-5678-9012-345678901234"
    _write_jsonl(project / f"{uid}.jsonl", [
        {"type": "assistant", "timestamp": "2026-05-15T08:00:00.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "tu1", "name": "Bash",
              "input": {"command": "pytest -x"}},
         ]}},
    ])
    adapter = ClaudeTranscriptAdapter(project_path=project)
    [rec] = list(adapter.discover())
    assert "tool_use: Bash" in rec.content

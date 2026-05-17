"""Tests for source adapters."""
from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import pytest

from tigerharness.tiger_memory.sources import ClaudeTranscriptAdapter, DocsAdapter


def _write_jsonl(path: Path, events: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def test_claude_transcript_discovers_jsonl(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    uid = "3d7a9a41-079b-4d6b-8de1-33ee112bb155"
    _write_jsonl(
        project / f"{uid}.jsonl",
        [
            {"type": "user", "timestamp": "2026-05-14T08:21:36.000Z",
             "message": {"role": "user", "content": "Hello Sai"}},
            {"type": "assistant", "timestamp": "2026-05-14T08:22:00.000Z",
             "message": {"role": "assistant", "content": "Hi CEO"}},
        ],
    )
    adapter = ClaudeTranscriptAdapter(project_path=project)
    records = list(adapter.discover())
    assert len(records) == 1
    r = records[0]
    assert r.conversation_uuid == uid
    assert r.source == "claude_code"
    assert r.source_id == uid
    assert r.first_event_at.year == 2026
    assert "Hello Sai" in r.content
    assert "Hi CEO" in r.content


def test_claude_transcript_classifies_slack_via_threads_json(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    uid = "3d7a9a41-079b-4d6b-8de1-33ee112bb155"
    _write_jsonl(
        project / f"{uid}.jsonl",
        [
            {"type": "user", "timestamp": "2026-05-14T08:21:36.000Z",
             "message": {"role": "user", "content": "From slack"}},
        ],
    )
    threads_json = tmp_path / "threads.json"
    threads_json.write_text(json.dumps({"1778736494.495229": uid}))
    adapter = ClaudeTranscriptAdapter(
        project_path=project, threads_json=threads_json
    )
    records = list(adapter.discover())
    assert len(records) == 1
    assert records[0].source == "slack"
    assert records[0].source_id == "1778736494.495229"


def test_claude_transcript_handles_anthropic_content_blocks(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    uid = "deadbeef-1234-5678-9012-345678901234"
    _write_jsonl(
        project / f"{uid}.jsonl",
        [
            {"type": "assistant", "timestamp": "2026-05-14T08:00:00.000Z",
             "message": {"role": "assistant", "content": [
                 {"type": "text", "text": "Sure thing."},
                 {"type": "tool_use", "name": "Read", "input": {}},
             ]}},
        ],
    )
    adapter = ClaudeTranscriptAdapter(project_path=project)
    [rec] = list(adapter.discover())
    assert "Sure thing." in rec.content
    assert "[tool_use: Read]" in rec.content


def test_claude_transcript_skips_empty_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "deadbeef-1234-5678-9012-345678901234.jsonl").write_text("")
    adapter = ClaudeTranscriptAdapter(project_path=project)
    assert list(adapter.discover()) == []


def test_claude_transcript_tolerates_bad_json_lines(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    uid = "deadbeef-1234-5678-9012-345678901234"
    f = project / f"{uid}.jsonl"
    f.write_text(
        "not-json\n"
        + json.dumps({"type": "user", "timestamp": "2026-05-14T08:00:00.000Z",
                     "message": {"role": "user", "content": "ok"}})
        + "\n"
    )
    adapter = ClaudeTranscriptAdapter(project_path=project)
    [rec] = list(adapter.discover())
    assert "ok" in rec.content


def test_docs_adapter_uses_uuid5(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    p = tmp_path / "docs" / "001_test.md"
    p.write_text("# Test doc\nbody\n")
    adapter = DocsAdapter("docs/*.md", repo_root=tmp_path)
    records = list(adapter.discover())
    assert len(records) == 1
    # uuid5 is deterministic — two runs return the same UUID.
    again = list(DocsAdapter("docs/*.md", repo_root=tmp_path).discover())
    assert records[0].conversation_uuid == again[0].conversation_uuid
    assert records[0].source == "doc"
    assert records[0].source_id == "docs/001_test.md"
    assert "Test doc" in records[0].content

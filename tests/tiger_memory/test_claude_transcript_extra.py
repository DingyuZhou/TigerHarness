"""Additional claude_transcript tests — JSONL parsing, bridge context, event iteration."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from tigerharness.tiger_memory.sources.claude_transcript import (
    ClaudeTranscriptAdapter,
    _extract_text,
    _is_briefing_read,
    _iter_events,
    _parse_ts,
    _path_is_briefing,
)


class TestIterEvents:
    def test_valid_jsonl(self, tmp_path: Path):
        f = tmp_path / "test.jsonl"
        f.write_text('{"a": 1}\n{"b": 2}\n')
        events = list(_iter_events(f))
        assert len(events) == 2

    def test_malformed_line_skipped(self, tmp_path: Path):
        f = tmp_path / "test.jsonl"
        f.write_text('{"a": 1}\nnot json\n{"b": 2}\n')
        events = list(_iter_events(f))
        assert len(events) == 2

    def test_empty_lines_skipped(self, tmp_path: Path):
        f = tmp_path / "test.jsonl"
        f.write_text('{"a": 1}\n\n\n{"b": 2}\n')
        events = list(_iter_events(f))
        assert len(events) == 2

    def test_unreadable_file(self, tmp_path: Path):
        f = tmp_path / "nonexistent.jsonl"
        events = list(_iter_events(f))
        assert events == []


class TestParseTs:
    def test_z_suffix(self):
        result = _parse_ts("2026-05-15T10:30:00Z")
        assert result.year == 2026
        assert result.month == 5

    def test_offset_suffix(self):
        result = _parse_ts("2026-05-15T10:30:00+00:00")
        assert result.year == 2026

    def test_invalid_returns_epoch(self):
        result = _parse_ts("not-a-date")
        assert result.year == 1970

    def test_none_input(self):
        result = _parse_ts(None)
        assert result.year == 1970


class TestExtractText:
    def _event(self, content):
        return {"message": {"content": content}}

    def test_text_blocks(self):
        event = self._event([{"type": "text", "text": "Hello world"}])
        result = _extract_text(event)
        assert "Hello world" in result

    def test_tool_use_blocks(self):
        event = self._event([
            {"type": "tool_use", "id": "tu1", "name": "Read",
             "input": {"file_path": "/tmp/test.py"}},
        ])
        result = _extract_text(event)
        assert "Read" in result

    def test_tool_result_blocks(self):
        event = self._event([
            {"type": "tool_result", "tool_use_id": "tu1", "content": "file contents here"},
        ])
        result = _extract_text(event)
        assert "file contents here" in result

    def test_tool_result_list_content(self):
        event = self._event([
            {"type": "tool_result", "tool_use_id": "tu1",
             "content": [{"type": "text", "text": "line1"}, {"type": "text", "text": "line2"}]},
        ])
        result = _extract_text(event)
        assert "line1" in result

    def test_skipped_tool_results(self):
        event = self._event([
            {"type": "tool_result", "tool_use_id": "tu_skip", "content": "hidden"},
        ])
        result = _extract_text(event, skipped_tool_use_ids={"tu_skip"})
        assert "hidden" not in result

    def test_string_blocks(self):
        event = self._event(["plain string"])
        result = _extract_text(event)
        assert "plain string" in result

    def test_string_content(self):
        event = self._event("simple text")
        result = _extract_text(event)
        assert "simple text" in result

    def test_empty_content(self):
        assert _extract_text({"message": {"content": []}}) == ""
        assert _extract_text({"message": {}}) == ""
        assert _extract_text({}) == ""

    def test_briefing_read_skipped(self):
        event = self._event([
            {"type": "tool_use", "id": "tu_br", "name": "Read",
             "input": {"file_path": "/memory/sai/briefing/README.md"}},
            {"type": "tool_result", "tool_use_id": "tu_br",
             "content": "briefing content"},
        ])
        skipped = set()
        result = _extract_text(event, skipped_tool_use_ids=skipped)
        assert "briefing content" not in result
        assert "tu_br" in skipped


class TestIsBriefingRead:
    def test_read_tool_briefing(self):
        block = {"name": "Read", "input": {"file_path": "/home/user/memory/sai/briefing/README.md"}}
        assert _is_briefing_read(block) is True

    def test_read_tool_not_briefing(self):
        block = {"name": "Read", "input": {"file_path": "/home/user/docs/design.md"}}
        assert _is_briefing_read(block) is False

    def test_bash_tool_briefing(self):
        block = {"name": "Bash", "input": {"command": "cat /memory/sai/briefing/README.md"}}
        assert _is_briefing_read(block) is True

    def test_grep_tool_briefing(self):
        block = {"name": "Grep", "input": {"path": "/memory/sai/briefing/", "pattern": "test"}}
        assert _is_briefing_read(block) is True

    def test_grep_tool_not_briefing(self):
        block = {"name": "Grep", "input": {"path": "/src/", "pattern": "test"}}
        assert _is_briefing_read(block) is False

    def test_unknown_tool(self):
        block = {"name": "UnknownTool", "input": {"x": "y"}}
        assert _is_briefing_read(block) is False


class TestPathIsBriefing:
    def test_briefing_path(self):
        assert _path_is_briefing("/home/user/memory/agent/briefing/README.md") is True

    def test_non_briefing_path(self):
        assert _path_is_briefing("/home/user/docs/design.md") is False

    def test_memory_without_briefing(self):
        assert _path_is_briefing("/home/user/memory/agent/journal/daily.md") is False


class TestClaudeTranscriptAdapter:
    def test_discover_empty_dir(self, tmp_path: Path):
        proj = tmp_path / "proj"
        proj.mkdir()
        adapter = ClaudeTranscriptAdapter(project_path=proj)
        records = list(adapter.discover())
        assert records == []

    def test_discover_nonexistent_dir(self, tmp_path: Path):
        adapter = ClaudeTranscriptAdapter(project_path=tmp_path / "nope")
        records = list(adapter.discover())
        assert records == []

    def test_discover_with_conversation(self, tmp_path: Path):
        proj = tmp_path / "proj"
        proj.mkdir()
        uid = str(uuid4())
        jsonl = proj / f"{uid}.jsonl"
        # Need events with timestamp + type in _CONTENT_TYPES (human/assistant)
        lines = [
            json.dumps({"type": "human", "timestamp": "2026-05-15T10:00:00Z",
                        "message": {"content": "Hello agent"}}),
            json.dumps({"type": "assistant", "timestamp": "2026-05-15T10:01:00Z",
                        "message": {"content": "Hello! How can I help?"}}),
        ]
        jsonl.write_text("\n".join(lines) + "\n")
        adapter = ClaudeTranscriptAdapter(project_path=proj)
        records = list(adapter.discover())
        assert len(records) == 1
        assert records[0].conversation_uuid == uid
        assert records[0].source == "claude_code"

    def test_discover_with_threads_json(self, tmp_path: Path):
        proj = tmp_path / "proj"
        proj.mkdir()
        uid = str(uuid4())
        jsonl = proj / f"{uid}.jsonl"
        lines = [
            json.dumps({"type": "human", "timestamp": "2026-05-15T10:00:00Z",
                        "message": {"content": "Hi from slack"}}),
            json.dumps({"type": "assistant", "timestamp": "2026-05-15T10:01:00Z",
                        "message": {"content": "Hello!"}}),
        ]
        jsonl.write_text("\n".join(lines) + "\n")
        threads = tmp_path / "threads.json"
        threads.write_text(json.dumps({"1234.5678": uid}))
        adapter = ClaudeTranscriptAdapter(project_path=proj, threads_json=threads)
        records = list(adapter.discover())
        assert len(records) == 1
        assert records[0].source == "slack"
        assert "1234.5678" in records[0].source_id

"""Coverage tests for claude_transcript.py — lines 66-67, 109, 202."""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from tigerharness.tiger_memory.sources.claude_transcript import (
    ClaudeTranscriptAdapter,
    _raw_text,
)


class TestReverseThreadMapErrors:
    """Lines 66-67: bad threads.json → empty map."""

    def test_invalid_json(self, tmp_path: Path):
        threads = tmp_path / "threads.json"
        threads.write_text("not valid json {{{")
        adapter = ClaudeTranscriptAdapter(
            project_path=tmp_path / "proj",
            threads_json=threads,
        )
        assert adapter._reverse_thread_map() == {}

    def test_oserror(self, tmp_path: Path):
        threads = tmp_path / "threads.json"
        # Don't create the file — but adapter has the path
        # Actually, the code checks exists() first. Let me force OSError
        threads.write_text("valid")
        from unittest.mock import patch

        def bad_read(self, *a, **kw):
            raise OSError("read fail")

        with patch.object(Path, "read_text", bad_read):
            adapter = ClaudeTranscriptAdapter(
                project_path=tmp_path / "proj",
                threads_json=threads,
            )
            assert adapter._reverse_thread_map() == {}


class TestRecordForEmptyContent:
    """Line 109: JSONL with events but no extractable text → None."""

    def test_empty_content_returns_none(self, tmp_path: Path):
        proj = tmp_path / "proj"
        proj.mkdir()
        uid = str(uuid4())
        jsonl = proj / f"{uid}.jsonl"
        # Events with no useful content
        jsonl.write_text(
            json.dumps({"type": "system", "timestamp": "2026-05-15T10:00:00Z"}) + "\n"
        )
        adapter = ClaudeTranscriptAdapter(project_path=proj)
        rec = adapter._record_for(jsonl, {})
        assert rec is None


class TestRawTextNonListNonString:
    """Line 202: content is neither list nor string → ""."""

    def test_integer_content(self):
        event = {"message": {"content": 42}}
        assert _raw_text(event) == ""

    def test_none_content(self):
        event = {"message": {"content": None}}
        assert _raw_text(event) == ""

    def test_no_content(self):
        event = {"message": {}}
        assert _raw_text(event) == ""

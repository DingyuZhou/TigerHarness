"""P2 adapter hardening — B7 (sidechain skip) + B4 (team-qualified id)."""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from tigerharness.tiger_memory.sources.claude_transcript import (
    ClaudeTranscriptAdapter,
    _normalize_owner,
)


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _row(role: str, text: str, ts: str, *, sidechain: bool = False) -> dict:
    r = {"type": role, "timestamp": ts, "message": {"content": text}}
    if sidechain:
        r["isSidechain"] = True
    return r


# ---- B4: _normalize_owner branch matrix -----------------------------------


@pytest.mark.parametrize("value,expected", [
    ({"team": "shohoku", "name": "ayako"}, ("shohoku", "ayako")),
    ({"name": "ayako"}, (None, "ayako")),          # dict, no team
    ({"team": "shohoku"}, None),                    # dict, missing name
    ({"name": 123}, None),                          # dict, non-str name
    ("kainan/sakuragi", ("kainan", "sakuragi")),    # flattened team/name
    ("ayako", (None, "ayako")),                     # bare name
    ("/ayako", (None, "ayako")),                    # empty team -> None
    ("ayako/", None),                               # empty name -> None
    ("", None),                                     # empty string
    (None, None),                                   # not str / not dict
])
def test_normalize_owner(value, expected) -> None:
    assert _normalize_owner(value) == expected


# ---- B7: sidechain rows are never ingested --------------------------------


class TestSidechainSkip:
    def test_nested_sidechain_rows_dropped_from_content(self, tmp_path: Path):
        proj = tmp_path / "proj"
        proj.mkdir()
        uid = str(uuid4())
        _write_rows(proj / f"{uid}.jsonl", [
            _row("user", "KEEP the human ask", "2026-05-18T10:00:00Z"),
            _row("assistant", "KEEP the reply", "2026-05-18T10:01:00Z"),
            _row("user", "SECRET subagent prompt", "2026-05-18T10:02:00Z",
                 sidechain=True),
            _row("assistant", "SECRET subagent output", "2026-05-18T10:03:00Z",
                 sidechain=True),
        ])
        adapter = ClaudeTranscriptAdapter(project_path=proj)
        records = list(adapter.discover())
        assert len(records) == 1
        content = records[0].content
        assert "KEEP the human ask" in content
        assert "KEEP the reply" in content
        assert "SECRET" not in content

    def test_all_sidechain_file_yields_no_record(self, tmp_path: Path):
        proj = tmp_path / "proj"
        proj.mkdir()
        uid = str(uuid4())
        _write_rows(proj / f"{uid}.jsonl", [
            _row("user", "subagent prompt", "2026-05-18T10:00:00Z",
                 sidechain=True),
            _row("assistant", "subagent reply", "2026-05-18T10:01:00Z",
                 sidechain=True),
        ])
        adapter = ClaudeTranscriptAdapter(project_path=proj)
        assert list(adapter.discover()) == []


# ---- B4: team-qualified matching via discover() ---------------------------


class TestTeamMatching:
    def _setup(self, tmp_path: Path, persona_value):
        proj = tmp_path / "proj"
        proj.mkdir()
        uid = str(uuid4())
        _write_rows(proj / f"{uid}.jsonl", [
            _row("user", "hi", "2026-05-18T10:00:00Z"),
            _row("assistant", "yo", "2026-05-18T10:01:00Z"),
        ])
        threads = tmp_path / "threads.json"
        threads.write_text(json.dumps({
            "t1": {"session_id": uid, "persona": persona_value},
        }))
        return proj, threads, uid

    def test_team_match_includes(self, tmp_path: Path):
        proj, threads, uid = self._setup(
            tmp_path, {"team": "shohoku", "name": "ayako"})
        adapter = ClaudeTranscriptAdapter(
            project_path=proj, threads_json=threads,
            persona="ayako", team="shohoku")
        assert {r.conversation_uuid for r in adapter.discover()} == {uid}

    def test_team_mismatch_excludes(self, tmp_path: Path):
        proj, threads, _uid = self._setup(
            tmp_path, {"team": "kainan", "name": "ayako"})
        adapter = ClaudeTranscriptAdapter(
            project_path=proj, threads_json=threads,
            persona="ayako", team="shohoku")
        assert list(adapter.discover()) == []

    def test_bare_name_record_matches_team_aware_adapter(self, tmp_path: Path):
        # Today's bare-name attribution still matches a team-aware adapter
        # (the record names no team -> name alone decides).
        proj, threads, uid = self._setup(tmp_path, "ayako")
        adapter = ClaudeTranscriptAdapter(
            project_path=proj, threads_json=threads,
            persona="ayako", team="shohoku")
        assert {r.conversation_uuid for r in adapter.discover()} == {uid}

    def test_teamless_adapter_ignores_record_team(self, tmp_path: Path):
        # Adapter with no team -> name-only match even if the record has one.
        proj, threads, uid = self._setup(
            tmp_path, {"team": "kainan", "name": "ayako"})
        adapter = ClaudeTranscriptAdapter(
            project_path=proj, threads_json=threads, persona="ayako")
        assert {r.conversation_uuid for r in adapter.discover()} == {uid}

    def test_name_mismatch_excludes(self, tmp_path: Path):
        proj, threads, _uid = self._setup(
            tmp_path, {"team": "shohoku", "name": "sakuragi"})
        adapter = ClaudeTranscriptAdapter(
            project_path=proj, threads_json=threads,
            persona="ayako", team="shohoku")
        assert list(adapter.discover()) == []

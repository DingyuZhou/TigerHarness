"""Tests for the OpenAI Codex session-rollout source adapter (ADR 0011
limit 4) and the transcript base it shares with the Claude adapter."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from tigerharness.tiger_memory.sources import CodexTranscriptAdapter
from tigerharness.tiger_memory.sources.codex_transcript import (
    _clean_message,
    _content_text,
    _id_from_name,
    _session_meta,
)

SID = "01a09bcc-c728-7053-ac24-0b339282b48c"
SID2 = "01a09bcc-c728-7053-ac24-0b339282b49d"


def _line(ts: str, type_: str, payload: dict) -> str:
    return json.dumps({"timestamp": ts, "type": type_, "payload": payload})


def _msg(ts: str, role: str, text: str, *, kind: str = "input_text") -> str:
    return _line(ts, "response_item", {
        "type": "message", "role": role, "content": [{"type": kind, "text": text}],
    })


def _rollout(
    root: Path, sid: str, cwd: str, body: list[str], *, parent: str | None = None,
    day: str = "2026/09/13", meta: bool = True,
) -> Path:
    d = root / day
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"rollout-2026-09-13T10-24-43-{sid}.jsonl"
    lines: list[str] = []
    if meta:
        payload = {"id": sid, "session_id": sid, "cwd": cwd, "timestamp": "2026-09-13T17:24:43.947Z"}
        if parent:
            payload["parent_thread_id"] = parent
        lines.append(_line("2026-09-13T17:24:44.038Z", "session_meta", payload))
    lines += body
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _conversation() -> list[str]:
    return [
        _msg("2026-09-13T17:24:45.041Z", "developer", "<skills_instructions>secret</skills_instructions>"),
        _msg("2026-09-13T17:24:45.100Z", "user",
             "<recommended_plugins>\n- Airtable\n</recommended_plugins>\n"
             "Please run the tests.\n\n[bridge-context]\nslack_thread_ts: 1.2\nslack_channel: D0ABC\n"),
        _line("2026-09-13T17:24:46.000Z", "event_msg", {"type": "user_message", "message": "dup"}),
        _line("2026-09-13T17:24:47.000Z", "response_item", {"type": "reasoning", "summary": [], "encrypted_content": "xxx"}),
        _msg("2026-09-13T17:24:48.000Z", "assistant", "Running them now.", kind="output_text"),
        _line("2026-09-13T17:24:49.000Z", "response_item", {
            "type": "custom_tool_call", "call_id": "c1", "name": "exec",
            "input": "text(await tools.exec_command({cmd:\"pytest -q\"}))",
        }),
        _line("2026-09-13T17:24:50.000Z", "response_item", {
            "type": "custom_tool_call_output", "call_id": "c1",
            "output": [{"type": "input_text", "text": "12 passed"}],
        }),
        _line("2026-09-13T17:24:51.000Z", "response_item", {
            "type": "function_call", "call_id": "f1", "name": "shell",
            "arguments": "{\"command\": [\"ls\"]}",
        }),
        _line("2026-09-13T17:24:52.000Z", "response_item", {
            "type": "function_call_output", "call_id": "f1", "output": "a.py\nb.py",
        }),
        _line("2026-09-13T17:24:53.000Z", "response_item", {
            "type": "custom_tool_call", "call_id": "b1", "name": "exec",
            "input": "cat memory/Rukawa/briefing/README.md",
        }),
        _line("2026-09-13T17:24:54.000Z", "response_item", {
            "type": "custom_tool_call_output", "call_id": "b1",
            "output": [{"type": "input_text", "text": "BRIEFING TEXT"}],
        }),
        _line("2026-09-13T17:24:55.000Z", "response_item", "not-a-dict"),
        _line("2026-09-13T17:24:56.000Z", "response_item", {"type": "message", "role": "assistant", "content": [{"type": "output_text"}]}),
        "this line is not json",
        _msg("2026-09-13T17:24:57.000Z", "assistant", "All green.", kind="output_text"),
        _line("2026-09-13T17:24:58.000Z", "event_msg", {"type": "task_complete"}),
    ]


class TestParsing:
    def test_basic_record(self, tmp_path: Path) -> None:
        team = tmp_path / "team"
        team.mkdir()
        sessions = tmp_path / "sessions"
        p = _rollout(sessions, SID, str(team), _conversation())
        recs = list(CodexTranscriptAdapter(sessions, cwd=team).discover())
        assert len(recs) == 1
        rec = recs[0]
        assert rec.conversation_uuid == SID
        assert rec.source == "codex" and rec.source_id == SID
        assert rec.raw_path == p
        assert rec.first_event_at.isoformat().startswith("2026-09-13T17:24:44")
        assert rec.last_event_at.isoformat().startswith("2026-09-13T17:24:58")
        c = rec.content
        # Developer instructions, the plugin catalogue, the bridge trailer,
        # reasoning, event_msg duplicates and the briefing read are out.
        assert "secret" not in c and "Airtable" not in c and "bridge-context" not in c
        assert "xxx" not in c and "dup" not in c and "BRIEFING TEXT" not in c
        # The conversation and the other tools are in, in order.
        assert c.index("Please run the tests.") < c.index("Running them now.")
        assert "[tool_use: exec]" in c and "[tool_result] 12 passed" in c
        assert "[tool_use: shell]" in c and "[tool_result] a.py\nb.py" in c
        assert c.rstrip().endswith("All green.")

    def test_no_content_is_skipped(self, tmp_path: Path) -> None:
        sessions = tmp_path / "s"
        _rollout(sessions, SID, "/x", [_msg("2026-09-13T17:24:45.041Z", "developer", "only me")])
        assert list(CodexTranscriptAdapter(sessions).discover()) == []
        # A file with no timestamped line at all.
        (sessions / "2026/09/13" / f"rollout-x-{SID2}.jsonl").write_text(
            json.dumps({"type": "session_meta", "payload": {"id": SID2}}) + "\n"
        )
        assert list(CodexTranscriptAdapter(sessions).discover()) == []

    def test_helpers(self, tmp_path: Path) -> None:
        assert _content_text("plain") == "plain"
        assert _content_text([{"type": "input_text", "text": "a"}, "junk", {"type": "x"}, {"text": 3}]) == "a"
        assert _content_text(None) == ""
        assert _clean_message("<environment_context>e</environment_context>\nhi\n[bridge-context]\nx") == "hi"
        assert _id_from_name(Path(f"rollout-2026-09-13T10-24-43-{SID}.jsonl")) == SID
        assert _id_from_name(Path("rollout-nothing.jsonl")) == ""
        f = tmp_path / "f.jsonl"
        f.write_text(json.dumps({"type": "session_meta", "payload": "odd"}) + "\n")
        assert _session_meta(f) == {}
        f.write_text(json.dumps({"type": "other"}) + "\n")
        assert _session_meta(f) is None


class TestDiscovery:
    def test_cwd_filter_and_helper_sessions(self, tmp_path: Path) -> None:
        team = tmp_path / "team"
        team.mkdir()
        sessions = tmp_path / "s"
        body = [_msg("2026-09-13T17:24:45Z", "user", "hi"), _msg("2026-09-13T17:24:46Z", "assistant", "yo", kind="output_text")]
        _rollout(sessions, SID, str(team), body)
        _rollout(sessions, SID2, str(tmp_path / "elsewhere"), body)
        child = "01a09bcc-c728-7053-ac24-0b339282b400"
        _rollout(sessions, child, str(team), body, parent=SID)
        nocwd = "01a09bcc-c728-7053-ac24-0b339282b401"
        (sessions / "2026/09/13" / f"rollout-x-{nocwd}.jsonl").write_text(
            _line("2026-09-13T17:24:44Z", "session_meta", {"id": nocwd}) + "\n" + "\n".join(body) + "\n"
        )
        got = {r.conversation_uuid for r in CodexTranscriptAdapter(sessions, cwd=team).discover()}
        assert got == {SID}
        # No cwd filter: every top-level session (helpers still excluded).
        got = {r.conversation_uuid for r in CodexTranscriptAdapter(sessions).discover()}
        assert got == {SID, SID2, nocwd}

    def test_missing_meta_or_id(self, tmp_path: Path) -> None:
        sessions = tmp_path / "s"
        body = [_msg("2026-09-13T17:24:45Z", "user", "hi")]
        # No session_meta line at all: not a rollout.
        _rollout(sessions, SID, "/x", body, meta=False)
        assert list(CodexTranscriptAdapter(sessions).discover()) == []
        # Meta without an id: the filename id is used...
        (sessions / "2026/09/13" / f"rollout-y-{SID2}.jsonl").write_text(
            _line("2026-09-13T17:24:44Z", "session_meta", {"cwd": "/x"}) + "\n" + body[0] + "\n"
        )
        # ... and a file whose name carries no id is skipped.
        (sessions / "2026/09/13" / "rollout-z-noid.jsonl").write_text(
            _line("2026-09-13T17:24:44Z", "session_meta", {"cwd": "/x"}) + "\n" + body[0] + "\n"
        )
        got = [r.conversation_uuid for r in CodexTranscriptAdapter(sessions).discover()]
        assert got == [SID2]

    def test_sessions_path_missing_and_age_cutoff(self, tmp_path: Path) -> None:
        assert list(CodexTranscriptAdapter(tmp_path / "nope").discover()) == []
        sessions = tmp_path / "s"
        body = [_msg("2026-09-13T17:24:45Z", "user", "hi")]
        p = _rollout(sessions, SID, "/x", body)
        old = time.time() - 30 * 86400
        os.utime(p, (old, old))
        assert list(CodexTranscriptAdapter(sessions, max_age_days=7).discover()) == []
        assert len(list(CodexTranscriptAdapter(sessions, max_age_days=None).discover())) == 1

    def test_default_sessions_path_expands_home(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        a = CodexTranscriptAdapter()
        assert a.sessions_path == tmp_path / ".codex" / "sessions"
        assert a.cwd is None


class TestAttribution:
    def _setup(self, tmp_path: Path):
        sessions = tmp_path / "s"
        body = [
            _msg("2026-09-13T17:24:45Z", "user", "hi\n\n[bridge-context]\nslack_thread_ts: 1.2\nslack_channel: D0ABC\n"),
            _msg("2026-09-13T17:24:46Z", "assistant", "yo", kind="output_text"),
        ]
        _rollout(sessions, SID, "/x", body)
        _rollout(sessions, SID2, "/x", body[1:])
        threads = tmp_path / "threads.json"
        threads.write_text(json.dumps({
            "1.2": {"session_id": SID, "persona": "Rukawa", "backend": "codex_exec"},
        }))
        return sessions, threads

    def test_slack_source_and_persona_filter(self, tmp_path: Path) -> None:
        sessions, threads = self._setup(tmp_path)
        recs = {r.conversation_uuid: r for r in CodexTranscriptAdapter(sessions, threads).discover()}
        assert recs[SID].source == "slack" and recs[SID].source_id == "1.2@D0ABC"
        assert recs[SID2].source == "codex"
        mine = [r.conversation_uuid for r in CodexTranscriptAdapter(sessions, threads, persona="Rukawa").discover()]
        assert mine == [SID]
        theirs = list(CodexTranscriptAdapter(sessions, threads, persona="Ayako").discover())
        assert theirs == []
        loose = {r.conversation_uuid for r in CodexTranscriptAdapter(
            sessions, threads, persona="Rukawa", include_unattributed=True,
        ).discover()}
        assert loose == {SID, SID2}

    def test_drive_sessions_are_suppressed(self, tmp_path: Path) -> None:
        sessions, threads = self._setup(tmp_path)
        reg = tmp_path / ".drive-sessions.json"
        reg.write_text(json.dumps({"1.2": {"task_id": "t"}}))
        got = {r.conversation_uuid for r in CodexTranscriptAdapter(
            sessions, threads, drive_sessions_json=reg,
        ).discover()}
        assert got == {SID2}


class TestWiring:
    def test_config_accepts_codex_and_names_it_in_the_error(self, tmp_path: Path) -> None:
        from tigerharness.tiger_memory.config import ConfigError, load_config
        cfg_path = tmp_path / "cfg.yaml"
        cfg_path.write_text(
            f"agent: {{name: T, role: t}}\nstore: {{root: {tmp_path}/memory}}\n"
            "sources:\n  - kind: codex\n    cwd: auto\n"
            "summarizer: {backend: anthropic, model: m, prompts: default/v1}\n"
        )
        cfg = load_config(cfg_path)
        assert cfg.sources[0].kind == "codex"
        cfg_path.write_text(
            f"agent: {{name: T, role: t}}\nstore: {{root: {tmp_path}/memory}}\n"
            "sources:\n  - kind: nope\n"
            "summarizer: {backend: anthropic, model: m, prompts: default/v1}\n"
        )
        with pytest.raises(ConfigError, match="claude_code, codex, slack_thread"):
            load_config(cfg_path)

    def test_build_adapters_codex_cwd_variants(self, tmp_path: Path, monkeypatch) -> None:
        from tigerharness.tiger_memory import lifecycle as lc
        from tigerharness.tiger_memory.config import load_config

        def _cfg(sources: str):
            cfg_dir = tmp_path / "memories" / "T"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            cfg_path = cfg_dir / "tiger-memory.config.yaml"
            cfg_path.write_text(
                f"agent: {{name: T, role: t}}\nstore: {{root: {cfg_dir}}}\n"
                f"sources:\n{sources}"
                "summarizer: {backend: anthropic, model: m, prompts: default/v1}\n"
            )
            return load_config(cfg_path)

        # auto -> the team root two levels above the config; default sessions path.
        [a] = lc._build_adapters(_cfg("  - kind: codex\n    persona: T\n    team: X\n"))
        assert type(a).__name__ == "CodexTranscriptAdapter"
        assert a.cwd == tmp_path.resolve() and a.persona == "T" and a.team == "X"
        assert a.sessions_path == Path("~/.codex/sessions").expanduser()
        # explicit cwd + sessions_path
        [a] = lc._build_adapters(_cfg(
            f"  - kind: codex\n    cwd: {tmp_path}/elsewhere\n    sessions_path: {tmp_path}/sess\n"
            "    include_unattributed: true\n"
        ))
        assert a.cwd == (tmp_path / "elsewhere").resolve()
        assert a.sessions_path == tmp_path / "sess" and a.include_unattributed is True
        # empty cwd disables the filter
        [a] = lc._build_adapters(_cfg("  - kind: codex\n    cwd: ''\n"))
        assert a.cwd is None
        # a path-less config resolves auto against the cwd
        from tigerharness.tiger_memory.lifecycle import _resolve_team_cwd
        monkeypatch.chdir(tmp_path)

        class _NoPath:
            source_path = None

        assert _resolve_team_cwd("auto", tmp_path, _NoPath()) == Path.cwd()
        assert _resolve_team_cwd(None, tmp_path, _NoPath()) is None

    def test_init_renders_the_codex_source(self, tmp_path: Path) -> None:
        from tigerharness.init import _render_memory_config
        single = _render_memory_config(persona="chief", team="tigers", project_root=tmp_path, multi_team=False)
        assert "  - kind: codex\n" in single and "    cwd: auto\n" in single
        multi = _render_memory_config(persona="chief", team="tigers", project_root=tmp_path, multi_team=True)
        codex_block = multi.split("  - kind: codex\n", 1)[1].split("  - kind: slack_thread\n", 1)[0]
        assert "    persona: chief\n" in codex_block


class TestEdges:
    def test_briefing_call_without_id_and_empty_tool_output(self, tmp_path: Path) -> None:
        sessions = tmp_path / "s"
        body = [
            _msg("2026-09-13T17:24:45Z", "user", "hi"),
            _line("2026-09-13T17:24:46Z", "response_item", {
                "type": "custom_tool_call", "name": "exec",
                "input": "cat memory/x/briefing/README.md",
            }),
            _line("2026-09-13T17:24:47Z", "response_item", {
                "type": "custom_tool_call_output", "call_id": "z", "output": [],
            }),
            _line("2026-09-13T17:24:48Z", "response_item", {
                "type": "custom_tool_call_output", "call_id": "", "output": "trailing",
            }),
        ]
        _rollout(sessions, SID, "/x", body)
        [rec] = list(CodexTranscriptAdapter(sessions).discover())
        assert "briefing" not in rec.content and "[tool_use" not in rec.content
        assert "[tool_result] trailing" in rec.content
        assert rec.content.count("[tool_result]") == 1

    def test_unreadable_file_is_logged_and_skipped(self, tmp_path: Path, caplog) -> None:
        import logging as _logging
        sessions = tmp_path / "s"
        p = _rollout(sessions, SID, "/x", [_msg("2026-09-13T17:24:45Z", "user", "hi")])
        p.chmod(0)
        try:
            with caplog.at_level(_logging.WARNING, logger="tigerharness.tiger_memory.sources._transcripts"):
                recs = list(CodexTranscriptAdapter(sessions).discover())
        finally:
            p.chmod(0o644)
        if os.geteuid() != 0:  # root can read anything
            assert recs == [] and "unreadable JSONL" in caplog.text

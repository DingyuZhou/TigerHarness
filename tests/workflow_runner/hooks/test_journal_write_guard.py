"""Tests for the journal write-guard PreToolUse hook.

Two layers:

``TestHookContract`` drives the module exactly as Claude Code does -- a
``python -m ...`` subprocess fed the tool-call JSON on stdin -- and asserts
the real exit-code + stderr contract. These are the required deny/allow
cases from the Phase 2 spec.

``TestMainBranches`` calls :func:`main` in-process (monkeypatched stdin /
env / cwd) to exercise every branch for the 100% line+branch coverage gate;
subprocess invocations are not measured by coverage.py.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tigerharness.workflow_runner.hooks import journal_write_guard as guard

MODULE = "tigerharness.workflow_runner.hooks.journal_write_guard"
TASK_ID = "20260601-foo-abcd1234"


# --------------------------------------------------------------------------- #
# Subprocess contract tests
# --------------------------------------------------------------------------- #


def _invoke(
    raw: bytes,
    *,
    journal_root: Path | None = None,
    cwd: Path | None = None,
) -> tuple[int, str]:
    """Run the hook as ``python -m <module>`` with *raw* on stdin.

    Starts from a copy of the environment with any host
    ``TIGERHARNESS_WORKFLOW_JOURNAL`` stripped so the test's intent (env
    override vs team-dir resolution) is the only signal. Returns
    ``(returncode, stderr_text)``.
    """
    env = os.environ.copy()
    env.pop("TIGERHARNESS_WORKFLOW_JOURNAL", None)
    if journal_root is not None:
        env["TIGERHARNESS_WORKFLOW_JOURNAL"] = str(journal_root)
    proc = subprocess.run(
        [sys.executable, "-m", MODULE],
        input=raw,
        capture_output=True,
        env=env,
        cwd=str(cwd) if cwd is not None else None,
    )
    return proc.returncode, proc.stderr.decode()


def _payload(tool_name: str, **tool_input: object) -> bytes:
    return json.dumps(
        {"tool_name": tool_name, "tool_input": tool_input}
    ).encode()


class TestHookContract:
    """The real subprocess contract: exit 2 + deny message, or exit 0."""

    def test_deny_status_json(self, tmp_path: Path):
        root = tmp_path / "journal"
        target = root / TASK_ID / "status.json"
        code, err = _invoke(
            _payload("Edit", file_path=str(target)), journal_root=root
        )
        assert code == 2
        assert err.strip() == guard.DENY_MESSAGE

    def test_deny_orchestration_json(self, tmp_path: Path):
        root = tmp_path / "journal"
        target = root / TASK_ID / "orchestration.json"
        code, err = _invoke(
            _payload("Edit", file_path=str(target)), journal_root=root
        )
        assert code == 2
        assert guard.DENY_MESSAGE in err

    def test_deny_sessions_json(self, tmp_path: Path):
        root = tmp_path / "journal"
        target = root / TASK_ID / "sessions.json"
        code, err = _invoke(
            _payload("Edit", file_path=str(target)), journal_root=root
        )
        assert code == 2
        assert guard.DENY_MESSAGE in err

    def test_deny_events_jsonl(self, tmp_path: Path):
        root = tmp_path / "journal"
        target = root / TASK_ID / "events.jsonl"
        code, err = _invoke(
            _payload("Edit", file_path=str(target)), journal_root=root
        )
        assert code == 2
        assert guard.DENY_MESSAGE in err

    def test_deny_steps_md(self, tmp_path: Path):
        root = tmp_path / "journal"
        target = root / TASK_ID / "steps" / "05-rukawa-impl.md"
        code, err = _invoke(
            _payload("Edit", file_path=str(target)), journal_root=root
        )
        assert code == 2
        assert guard.DENY_MESSAGE in err

    def test_deny_write_tool(self, tmp_path: Path):
        """Write, not Edit, is equally blocked -- the guard keys on the
        path, not the tool name (the matcher already gates the tool set)."""
        root = tmp_path / "journal"
        target = root / TASK_ID / "status.json"
        code, err = _invoke(
            _payload("Write", file_path=str(target)), journal_root=root
        )
        assert code == 2
        assert guard.DENY_MESSAGE in err

    def test_deny_notebookedit(self, tmp_path: Path):
        """NotebookEdit carries the path in ``notebook_path``."""
        root = tmp_path / "journal"
        target = root / TASK_ID / "orchestration.json"
        code, err = _invoke(
            _payload("NotebookEdit", notebook_path=str(target)),
            journal_root=root,
        )
        assert code == 2
        assert guard.DENY_MESSAGE in err

    def test_allow_logs_dir(self, tmp_path: Path):
        root = tmp_path / "journal"
        target = root / TASK_ID / "logs" / "01-foo" / "iter-01" / "stdout.txt"
        code, err = _invoke(
            _payload("Write", file_path=str(target)), journal_root=root
        )
        assert code == 0
        assert err == ""

    def test_allow_unrelated_path(self, tmp_path: Path):
        root = tmp_path / "journal"
        target = tmp_path / "elsewhere" / "file.txt"
        code, err = _invoke(
            _payload("Edit", file_path=str(target)), journal_root=root
        )
        assert code == 0
        assert err == ""

    def test_allow_journal_root_via_env(self, tmp_path: Path):
        """A path under the $TIGERHARNESS_WORKFLOW_JOURNAL override is
        protected even though cwd is unrelated."""
        root = tmp_path / "custom-state"
        target = root / TASK_ID / "sessions.json"
        code, err = _invoke(
            _payload("Edit", file_path=str(target)),
            journal_root=root,
            cwd=tmp_path,
        )
        assert code == 2
        assert guard.DENY_MESSAGE in err

    def test_allow_journal_root_via_team_dir(self, tmp_path: Path):
        """With no env override, a team dir (configs/personas.yaml present)
        resolves the journal root to <cwd>/workflow_journal."""
        team_dir = tmp_path / "Shohoku"
        (team_dir / "configs").mkdir(parents=True)
        (team_dir / "configs" / "personas.yaml").write_text("x: 1\n")
        target = team_dir / "workflow_journal" / TASK_ID / "status.json"
        # No journal_root -> env override stripped -> rule 2 (team dir) fires.
        code, err = _invoke(
            _payload("Edit", file_path=str(target)), cwd=team_dir
        )
        assert code == 2
        assert guard.DENY_MESSAGE in err

    def test_malformed_input_allows(self):
        """Empty stdin -> fail open (exit 0)."""
        code, err = _invoke(b"")
        assert code == 0
        assert err == ""

    def test_non_json_input_allows(self):
        code, err = _invoke(b"this is not json")
        assert code == 0
        assert err == ""


# --------------------------------------------------------------------------- #
# In-process branch coverage
# --------------------------------------------------------------------------- #


def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    *,
    journal_root: Path | None = None,
    chdir: Path | None = None,
) -> int:
    """Call :func:`main` in-process with *raw* as stdin.

    Always clears the host journal override first so each test's resolution
    path is deterministic.
    """
    monkeypatch.delenv("TIGERHARNESS_WORKFLOW_JOURNAL", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    if journal_root is not None:
        monkeypatch.setenv("TIGERHARNESS_WORKFLOW_JOURNAL", str(journal_root))
    if chdir is not None:
        monkeypatch.chdir(chdir)
    return guard.main()


def _json(tool_name: str = "Edit", **tool_input: object) -> str:
    return json.dumps({"tool_name": tool_name, "tool_input": tool_input})


class TestMainBranches:
    """Exercise every branch of main / _is_protected / _extract_target."""

    def test_protected_status_denied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ):
        root = tmp_path / "j"
        target = root / TASK_ID / "status.json"
        rc = _run_main(
            monkeypatch, _json(file_path=str(target)), journal_root=root
        )
        assert rc == 2
        assert capsys.readouterr().err == guard.DENY_MESSAGE + "\n"

    def test_protected_steps_md_denied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        root = tmp_path / "j"
        target = root / TASK_ID / "steps" / "01-anzai-plan.md"
        rc = _run_main(
            monkeypatch, _json(file_path=str(target)), journal_root=root
        )
        assert rc == 2

    def test_allow_deep_logs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        root = tmp_path / "j"
        target = root / TASK_ID / "logs" / "01" / "iter-01" / "out.txt"
        rc = _run_main(
            monkeypatch, _json(file_path=str(target)), journal_root=root
        )
        assert rc == 0

    def test_allow_unrelated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        root = tmp_path / "j"
        target = tmp_path / "outside" / "thing.txt"
        rc = _run_main(
            monkeypatch, _json(file_path=str(target)), journal_root=root
        )
        assert rc == 0

    def test_allow_task_level_unprotected_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """task_brief.md sits at the task-dir level but is not truth-surface
        -- covers the 'len==2 but not in protected set' branch."""
        root = tmp_path / "j"
        target = root / TASK_ID / "task_brief.md"
        rc = _run_main(
            monkeypatch, _json(file_path=str(target)), journal_root=root
        )
        assert rc == 0

    def test_allow_steps_non_md(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A non-.md file under steps/ is not protected -- covers the
        endswith('.md') False branch."""
        root = tmp_path / "j"
        target = root / TASK_ID / "steps" / "notes.txt"
        rc = _run_main(
            monkeypatch, _json(file_path=str(target)), journal_root=root
        )
        assert rc == 0

    def test_allow_three_level_non_steps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A 3-segment path whose middle segment isn't 'steps' -- covers
        the parts[1] == 'steps' False branch."""
        root = tmp_path / "j"
        target = root / TASK_ID / "logs" / "stdout.txt"
        rc = _run_main(
            monkeypatch, _json(file_path=str(target)), journal_root=root
        )
        assert rc == 0

    def test_malformed_empty_stdin(self, monkeypatch: pytest.MonkeyPatch):
        assert _run_main(monkeypatch, "") == 0

    def test_malformed_garbage_stdin(self, monkeypatch: pytest.MonkeyPatch):
        assert _run_main(monkeypatch, "<<not json>>") == 0

    def test_non_dict_json(self, monkeypatch: pytest.MonkeyPatch):
        """Valid JSON that isn't an object -> fail open."""
        assert _run_main(monkeypatch, "[1, 2, 3]") == 0

    def test_missing_tool_input(self, monkeypatch: pytest.MonkeyPatch):
        assert _run_main(monkeypatch, json.dumps({"tool_name": "Edit"})) == 0

    def test_non_dict_tool_input(self, monkeypatch: pytest.MonkeyPatch):
        raw = json.dumps({"tool_name": "Edit", "tool_input": "oops"})
        assert _run_main(monkeypatch, raw) == 0

    def test_no_target_in_tool_input(self, monkeypatch: pytest.MonkeyPatch):
        """tool_input present but carries no file/notebook path."""
        assert _run_main(monkeypatch, _json()) == 0

    def test_empty_file_path_falls_through(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """file_path='' is falsy -> _extract_target falls through to a
        missing notebook_path and yields None (fail open)."""
        raw = json.dumps(
            {"tool_name": "Edit", "tool_input": {"file_path": ""}}
        )
        assert _run_main(monkeypatch, raw) == 0

    def test_non_string_file_path_falls_through_to_notebook(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A non-string file_path is ignored and notebook_path is honoured
        -- covers the isinstance(value, str) False branch."""
        root = tmp_path / "j"
        target = root / TASK_ID / "events.jsonl"
        raw = json.dumps(
            {
                "tool_name": "NotebookEdit",
                "tool_input": {
                    "file_path": 12345,
                    "notebook_path": str(target),
                },
            }
        )
        assert _run_main(monkeypatch, raw, journal_root=root) == 2

    def test_team_dir_resolution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """No env override + cwd is a team dir -> journal root resolves to
        <cwd>/workflow_journal (paths.py rule 2)."""
        team_dir = tmp_path / "Shohoku"
        (team_dir / "configs").mkdir(parents=True)
        (team_dir / "configs" / "personas.yaml").write_text("x: 1\n")
        target = team_dir / "workflow_journal" / TASK_ID / "status.json"
        rc = _run_main(monkeypatch, _json(file_path=str(target)), chdir=team_dir)
        assert rc == 2

    def test_deny_message_is_verbatim(self):
        """Pin the exact deny text the spec mandates (no backticks, the
        three-line wrap)."""
        assert guard.DENY_MESSAGE == (
            "Direct Edit of workflow journal files is forbidden. Use the\n"
            "workflow-append-steps skill (or another approved workflow "
            "skill) to\n"
            "mutate task state."
        )

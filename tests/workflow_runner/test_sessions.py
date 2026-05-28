"""Unit tests for ``tigerharness.workflow_runner.sessions``.

Strategy: stand up a small Python script as a fake ``claude`` CLI and
point :class:`SessionManager` at it via ``TIGERHARNESS_CLAUDE_BIN``.
The fake's behaviour is steered through env vars so we exercise the
full matrix without monkey-patching ``subprocess``.

The fake's source lives at ``fixtures/fake_claude.py`` so it's a
normal Python module the editor / linter can see — no escaped triple
quotes.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from tigerharness.task_runner import stuck_watchdog
from tigerharness.workflow_runner import sessions as sessions_mod
from tigerharness.workflow_runner.sessions import (
    TIMEOUT_EXIT_CODE,
    InvocationResult,
    SessionManager,
    _decode_partial,
    _parse_envelope,
    _safe_float,
    _safe_str,
)


# --------------------------------------------------------------------------- #
# Popen spy factory (shared by the reap-path tests below)
# --------------------------------------------------------------------------- #


CommunicateHook = Callable[[Any, Optional[str], Optional[float], int], Any]


def _make_spy_popen(
    *,
    capture_kwargs: Optional[dict] = None,
    communicate_hook: Optional[CommunicateHook] = None,
):
    """Build a ``Popen`` subclass that captures kwargs and/or scripts
    ``communicate`` per-call.

    One ``# type: ignore`` lives here; tests stay flat. ``capture_kwargs``
    (if given) is updated with the kwargs passed to ``__init__``.
    ``communicate_hook`` receives ``(self, input, timeout, call_index)``
    where ``call_index`` starts at 1; return its value or raise. When
    ``None``, ``communicate`` passes through to the real implementation.
    """
    real_popen = sessions_mod.Popen

    class _SpyPopen(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            self._spy_communicate_calls = 0
            if capture_kwargs is not None:
                capture_kwargs.update(kwargs)
            super().__init__(*args, **kwargs)

        def communicate(self, input=None, timeout=None):  # type: ignore[override]
            self._spy_communicate_calls += 1
            if communicate_hook is None:
                return super().communicate(input=input, timeout=timeout)
            return communicate_hook(
                self, input, timeout, self._spy_communicate_calls
            )

    return _SpyPopen


# --------------------------------------------------------------------------- #
# Fake claude CLI
# --------------------------------------------------------------------------- #


_FAKE_CLAUDE_SOURCE = (
    Path(__file__).parent / "fixtures" / "fake_claude.py"
).read_text()


@pytest.fixture
def fake_claude(tmp_path, monkeypatch) -> Path:
    """Drop a fake claude binary on disk and point the manager at it.

    Returns the path to the fake so tests can inspect / tweak its
    behaviour by setting ``FAKE_*`` env vars via ``monkeypatch.setenv``.

    The shebang is injected at write time so the script runs under the
    same interpreter as the test suite — avoids ``/usr/bin/env python3``
    pointing at a wrong / missing interpreter in CI sandboxes.
    """
    script = tmp_path / "claude-fake.py"
    script.write_text(f"#!{sys.executable}\n{_FAKE_CLAUDE_SOURCE}")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("TIGERHARNESS_CLAUDE_BIN", str(script))
    # Clear all FAKE_* env vars to keep tests independent.
    for key in list(os.environ):
        if key.startswith("FAKE_"):
            monkeypatch.delenv(key, raising=False)
    return script


@pytest.fixture
def task_dir(tmp_path: Path) -> Path:
    """A clean per-task directory for sessions.json."""
    d = tmp_path / "task"
    d.mkdir()
    return d


@pytest.fixture
def personas_dir(tmp_path, monkeypatch) -> Path:
    """Stand up a personas dir with a stub 'rukawa.md' prompt file."""
    d = tmp_path / "personas"
    d.mkdir()
    (d / "rukawa.md").write_text("you are rukawa; be terse.\n")
    monkeypatch.setenv("TIGERHARNESS_PERSONAS_DIR", str(d))
    return d


# --------------------------------------------------------------------------- #
# First call (fresh session)
# --------------------------------------------------------------------------- #


def test_first_call_persists_new_sid(
    fake_claude, task_dir, personas_dir, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_SESSION_ID", "sid-from-fresh")
    monkeypatch.setenv("FAKE_RESULT_TEXT", "hello from rukawa")
    argv_dump = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_ARGV_DUMP", str(argv_dump))

    mgr = SessionManager(task_dir)
    assert mgr.get_session_id("rukawa") is None

    result = mgr.invoke("rukawa", "ping", timeout_sec=10)

    assert isinstance(result, InvocationResult)
    assert result.exit_code == 0
    assert result.error is None
    assert result.session_id == "sid-from-fresh"
    assert result.stdout == "hello from rukawa"
    assert result.cost_usd == pytest.approx(0.0123)
    assert result.raw_envelope["type"] == "result"

    # Persisted.
    assert mgr.get_session_id("rukawa") == "sid-from-fresh"
    saved = json.loads((task_dir / "sessions.json").read_text())
    assert saved == {"rukawa": "sid-from-fresh"}

    # Argv contains --append-system-prompt with the persona prompt,
    # not --resume.
    argv = json.loads(argv_dump.read_text())
    assert "-p" in argv
    assert "--output-format" in argv and "json" in argv
    assert "--append-system-prompt" in argv
    prompt_idx = argv.index("--append-system-prompt")
    assert "rukawa" in argv[prompt_idx + 1].lower()
    assert "--resume" not in argv

    # Sudo floor is always applied — sessions.py reuses
    # ``task_runner.personas._SUDO_DENY`` so a persona can't sudo via
    # the workflow-runner even on a fresh session.
    assert "--disallowedTools" in argv
    deny = argv[argv.index("--disallowedTools") + 1]
    assert "Bash(sudo:*)" in deny and "Bash(sudo)" in deny


# --------------------------------------------------------------------------- #
# Resume (existing sid)
# --------------------------------------------------------------------------- #


def test_resume_uses_stored_sid_and_persists_rotation(
    fake_claude, task_dir, personas_dir, tmp_path, monkeypatch
):
    # Pre-populate sessions.json.
    (task_dir / "sessions.json").write_text(
        json.dumps({"rukawa": "sid-pre-existing"})
    )

    monkeypatch.setenv("FAKE_SESSION_ID", "sid-rotated")
    argv_dump = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_ARGV_DUMP", str(argv_dump))

    mgr = SessionManager(task_dir)
    result = mgr.invoke("rukawa", "iter 2", timeout_sec=10)

    assert result.exit_code == 0
    assert result.session_id == "sid-rotated"

    # sessions.json updated to the rotated id.
    assert mgr.get_session_id("rukawa") == "sid-rotated"

    # Argv used --resume with the old id; never re-sent the system prompt.
    argv = json.loads(argv_dump.read_text())
    assert "--resume" in argv
    assert argv[argv.index("--resume") + 1] == "sid-pre-existing"
    assert "--append-system-prompt" not in argv
    # Sudo floor still applied on the resume path.
    assert "--disallowedTools" in argv


def test_resume_no_rotation_keeps_sid(
    fake_claude, task_dir, personas_dir, monkeypatch
):
    """When the envelope sid equals the stored sid, sessions.json is not rewritten."""
    (task_dir / "sessions.json").write_text(
        json.dumps({"rukawa": "stable-sid"})
    )
    mtime_before = (task_dir / "sessions.json").stat().st_mtime_ns

    monkeypatch.setenv("FAKE_SESSION_ID", "stable-sid")

    mgr = SessionManager(task_dir)
    result = mgr.invoke("rukawa", "iter N", timeout_sec=10)
    assert result.session_id == "stable-sid"

    mtime_after = (task_dir / "sessions.json").stat().st_mtime_ns
    assert mtime_before == mtime_after  # no rewrite


def test_two_personas_share_sessions_file(
    fake_claude, task_dir, personas_dir, tmp_path, monkeypatch
):
    """Different personas keep independent sids under the same task."""
    (personas_dir / "akagi.md").write_text("you are akagi.\n")

    monkeypatch.setenv("FAKE_SESSION_ID", "sid-rukawa")
    mgr = SessionManager(task_dir)
    mgr.invoke("rukawa", "go", timeout_sec=10)

    monkeypatch.setenv("FAKE_SESSION_ID", "sid-akagi")
    mgr.invoke("akagi", "go", timeout_sec=10)

    saved = json.loads((task_dir / "sessions.json").read_text())
    assert saved == {"rukawa": "sid-rukawa", "akagi": "sid-akagi"}


# --------------------------------------------------------------------------- #
# Timeout
# --------------------------------------------------------------------------- #


def test_timeout_sentinel_is_out_of_posix_signal_range():
    """``returncode`` for a signal-killed process is ``-SIGNUM`` where
    ``SIGNUM`` is in ``1..64``. Our timeout sentinel must sit outside
    that range so callers can tell "we killed it" from "the OS killed it".
    """
    assert TIMEOUT_EXIT_CODE < -64


def test_timeout_returns_sentinel_no_raise(
    fake_claude, task_dir, personas_dir, monkeypatch
):
    monkeypatch.setenv("FAKE_SLEEP_SEC", "5")

    mgr = SessionManager(task_dir)
    result = mgr.invoke("rukawa", "slow", timeout_sec=1)

    assert result.exit_code == TIMEOUT_EXIT_CODE
    assert result.error is not None
    assert "timeout" in result.error.lower()
    assert result.stdout == ""
    assert result.cost_usd == 0.0
    assert result.raw_envelope == {}

    # No sid was discovered, so sessions.json must not exist.
    assert not (task_dir / "sessions.json").exists()


def test_timeout_preserves_existing_sid(
    fake_claude, task_dir, personas_dir, monkeypatch
):
    (task_dir / "sessions.json").write_text(
        json.dumps({"rukawa": "previously-stored"})
    )
    monkeypatch.setenv("FAKE_SLEEP_SEC", "5")

    mgr = SessionManager(task_dir)
    result = mgr.invoke("rukawa", "slow", timeout_sec=1)

    assert result.exit_code == TIMEOUT_EXIT_CODE
    assert result.session_id == "previously-stored"


# --------------------------------------------------------------------------- #
# Non-zero exit
# --------------------------------------------------------------------------- #


def test_nonzero_exit_captures_stderr(
    fake_claude, task_dir, personas_dir, monkeypatch
):
    monkeypatch.setenv("FAKE_EXIT_CODE", "2")
    monkeypatch.setenv("FAKE_STDERR", "boom: catastrophic vibes\n")
    monkeypatch.setenv("FAKE_STDOUT_OVERRIDE", "")  # blank stdout

    mgr = SessionManager(task_dir)
    result = mgr.invoke("rukawa", "ping", timeout_sec=10)

    assert result.exit_code == 2
    assert result.error is not None
    assert "exited with code 2" in result.error
    assert "boom" in result.error


def test_nonzero_exit_without_stderr_still_reports(
    fake_claude, task_dir, personas_dir, monkeypatch
):
    monkeypatch.setenv("FAKE_EXIT_CODE", "3")
    monkeypatch.setenv("FAKE_STDOUT_OVERRIDE", "")  # blank stdout
    # No FAKE_STDERR set.

    mgr = SessionManager(task_dir)
    result = mgr.invoke("rukawa", "ping", timeout_sec=10)

    assert result.exit_code == 3
    assert result.error == "claude exited with code 3"


# --------------------------------------------------------------------------- #
# Malformed envelope
# --------------------------------------------------------------------------- #


def test_malformed_envelope_returns_error(
    fake_claude, task_dir, personas_dir, monkeypatch
):
    monkeypatch.setenv("FAKE_ENVELOPE_RAW", "this is not json {")

    mgr = SessionManager(task_dir)
    result = mgr.invoke("rukawa", "ping", timeout_sec=10)

    assert result.exit_code == 0  # process succeeded; payload didn't.
    assert result.error is not None
    assert "malformed" in result.error.lower()
    assert result.raw_envelope == {}
    assert result.stdout == ""
    assert result.cost_usd == 0.0


def test_empty_stdout_returns_error(
    fake_claude, task_dir, personas_dir, monkeypatch
):
    monkeypatch.setenv("FAKE_STDOUT_OVERRIDE", "   ")

    mgr = SessionManager(task_dir)
    result = mgr.invoke("rukawa", "ping", timeout_sec=10)

    assert result.error is not None
    assert "empty" in result.error.lower()


def test_envelope_not_a_dict_returns_error(
    fake_claude, task_dir, personas_dir, monkeypatch
):
    monkeypatch.setenv("FAKE_ENVELOPE_RAW", "[1, 2, 3]")

    mgr = SessionManager(task_dir)
    result = mgr.invoke("rukawa", "ping", timeout_sec=10)

    assert result.error is not None
    assert "object" in result.error.lower() or "dict" in result.error.lower()


# --------------------------------------------------------------------------- #
# Cost extraction
# --------------------------------------------------------------------------- #


def test_cost_extracted_from_realistic_envelope(
    fake_claude, task_dir, personas_dir, monkeypatch
):
    realistic = {
        "type": "result",
        "subtype": "success",
        "result": "ack",
        "session_id": "sid-xyz",
        "total_cost_usd": 0.42,
        "model": "claude-opus-4",
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }
    monkeypatch.setenv("FAKE_ENVELOPE_RAW", json.dumps(realistic))

    mgr = SessionManager(task_dir)
    result = mgr.invoke("rukawa", "ping", timeout_sec=10)

    assert result.exit_code == 0
    assert result.error is None
    assert result.cost_usd == pytest.approx(0.42)
    assert result.session_id == "sid-xyz"
    assert result.stdout == "ack"
    assert result.raw_envelope == realistic


def test_missing_total_cost_defaults_to_zero_and_warns(
    fake_claude, task_dir, personas_dir, monkeypatch, caplog
):
    monkeypatch.setenv("FAKE_COST_USD", "__omit__")

    mgr = SessionManager(task_dir)
    with caplog.at_level("WARNING", logger="tigerharness.workflow_runner.sessions"):
        result = mgr.invoke("rukawa", "ping", timeout_sec=10)

    assert result.exit_code == 0
    assert result.cost_usd == 0.0
    assert any("total_cost_usd" in rec.message for rec in caplog.records)


def test_unparseable_cost_field_defaults_to_zero(
    fake_claude, task_dir, personas_dir, monkeypatch
):
    envelope = {
        "type": "result",
        "result": "ok",
        "session_id": "sid",
        "total_cost_usd": "not-a-number",
    }
    monkeypatch.setenv("FAKE_ENVELOPE_RAW", json.dumps(envelope))

    mgr = SessionManager(task_dir)
    result = mgr.invoke("rukawa", "ping", timeout_sec=10)

    assert result.cost_usd == 0.0


# --------------------------------------------------------------------------- #
# Log capture
# --------------------------------------------------------------------------- #


def test_log_dir_capture_writes_four_files(
    fake_claude, task_dir, personas_dir, tmp_path, monkeypatch
):
    log_dir = tmp_path / "logs" / "step-01" / "iter-01"
    monkeypatch.setenv("FAKE_SESSION_ID", "sid-captured")
    monkeypatch.setenv("FAKE_RESULT_TEXT", "captured assistant text")
    monkeypatch.setenv("FAKE_STDERR", "warn: deprecation X\n")

    mgr = SessionManager(task_dir)
    mgr.invoke(
        "rukawa", "prompt-payload", timeout_sec=10, log_dir=log_dir
    )

    assert log_dir.is_dir()
    assert (log_dir / "prompt.txt").read_text() == "prompt-payload"
    assert (log_dir / "stdout.txt").read_text() == "captured assistant text"
    # stderr.txt carries claude-side warnings even on a clean run.
    assert (log_dir / "stderr.txt").read_text() == "warn: deprecation X\n"

    envelope = json.loads((log_dir / "envelope.json").read_text())
    assert envelope["session_id"] == "sid-captured"
    assert envelope["result"] == "captured assistant text"


def test_log_dir_capture_on_timeout(
    fake_claude, task_dir, personas_dir, tmp_path, monkeypatch
):
    log_dir = tmp_path / "logs" / "step-01" / "iter-01"
    monkeypatch.setenv("FAKE_SLEEP_SEC", "5")

    mgr = SessionManager(task_dir)
    result = mgr.invoke(
        "rukawa", "will-timeout", timeout_sec=1, log_dir=log_dir
    )

    assert result.exit_code == TIMEOUT_EXIT_CODE
    assert (log_dir / "prompt.txt").read_text() == "will-timeout"
    assert json.loads((log_dir / "envelope.json").read_text()) == {}
    # stdout.txt and stderr.txt both exist (may be empty); the shape
    # is the same as the success path so consumers stay simple.
    assert (log_dir / "stdout.txt").exists()
    assert (log_dir / "stderr.txt").exists()


def test_log_dir_capture_preserves_partial_stdout_on_timeout(
    fake_claude, task_dir, personas_dir, tmp_path, monkeypatch
):
    """When the fake emits a chunk then hangs, the partial stdout
    survives in ``stdout.txt`` so we can debug timeouts."""
    log_dir = tmp_path / "logs" / "step-01" / "iter-01"
    monkeypatch.setenv("FAKE_PARTIAL_STDOUT", "half-an-envelope-here\n")
    monkeypatch.setenv("FAKE_SLEEP_SEC", "5")

    mgr = SessionManager(task_dir)
    result = mgr.invoke(
        "rukawa", "p", timeout_sec=1, log_dir=log_dir
    )

    assert result.exit_code == TIMEOUT_EXIT_CODE
    captured = (log_dir / "stdout.txt").read_text()
    assert "half-an-envelope-here" in captured


def test_log_dir_capture_preserves_partial_stderr_on_timeout(
    fake_claude, task_dir, personas_dir, tmp_path, monkeypatch
):
    """Partial stderr survives in ``stderr.txt`` on timeout.

    Wedged tool subprocesses are most likely to spit a diagnostic to
    stderr before hanging; without this capture the executor's debug
    trail goes dark exactly when we most need it.
    """
    log_dir = tmp_path / "logs" / "step-01" / "iter-01"
    monkeypatch.setenv(
        "FAKE_PARTIAL_STDERR", "tool: connecting to mcp server...\n"
    )
    monkeypatch.setenv("FAKE_SLEEP_SEC", "5")

    mgr = SessionManager(task_dir)
    result = mgr.invoke(
        "rukawa", "p", timeout_sec=1, log_dir=log_dir
    )

    assert result.exit_code == TIMEOUT_EXIT_CODE
    captured = (log_dir / "stderr.txt").read_text()
    assert "connecting to mcp server" in captured


def test_log_dir_creates_missing_parents(
    fake_claude, task_dir, personas_dir, tmp_path
):
    nested = tmp_path / "a" / "b" / "c" / "iter-01"
    assert not nested.exists()
    mgr = SessionManager(task_dir)
    mgr.invoke("rukawa", "p", timeout_sec=10, log_dir=nested)
    assert nested.is_dir()
    assert (nested / "prompt.txt").exists()


# --------------------------------------------------------------------------- #
# Validation + edge cases
# --------------------------------------------------------------------------- #


def test_invoke_rejects_nonpositive_timeout(task_dir, personas_dir):
    mgr = SessionManager(task_dir)
    with pytest.raises(ValueError):
        mgr.invoke("rukawa", "ping", timeout_sec=0)
    with pytest.raises(ValueError):
        mgr.invoke("rukawa", "ping", timeout_sec=-3)


def test_missing_persona_prompt_propagates(
    fake_claude, task_dir, tmp_path, monkeypatch
):
    """No personas dir configured -> FileNotFoundError. Setup bug, not runtime."""
    empty = tmp_path / "empty-personas"
    empty.mkdir()
    monkeypatch.setenv("TIGERHARNESS_PERSONAS_DIR", str(empty))

    mgr = SessionManager(task_dir)
    with pytest.raises(FileNotFoundError):
        mgr.invoke("nonexistent_persona", "ping", timeout_sec=10)


def test_get_session_id_missing_file_returns_none(task_dir):
    mgr = SessionManager(task_dir)
    assert mgr.get_session_id("anyone") is None


def test_default_claude_binary_is_claude(monkeypatch, task_dir):
    """When TIGERHARNESS_CLAUDE_BIN is unset, argv starts with 'claude'."""
    monkeypatch.delenv("TIGERHARNESS_CLAUDE_BIN", raising=False)
    mgr = SessionManager(task_dir)
    argv = mgr._build_argv("rukawa", existing_sid="sid-X")
    assert argv[0] == "claude"


def test_empty_env_var_falls_back_to_default(monkeypatch, task_dir):
    monkeypatch.setenv("TIGERHARNESS_CLAUDE_BIN", "   ")
    mgr = SessionManager(task_dir)
    argv = mgr._build_argv("rukawa", existing_sid="sid-X")
    assert argv[0] == "claude"


# --------------------------------------------------------------------------- #
# stdin delivery
# --------------------------------------------------------------------------- #


def test_prompt_is_sent_on_stdin(
    fake_claude, task_dir, personas_dir, tmp_path, monkeypatch
):
    stdin_dump = tmp_path / "stdin.txt"
    monkeypatch.setenv("FAKE_STDIN_DUMP", str(stdin_dump))

    mgr = SessionManager(task_dir)
    mgr.invoke("rukawa", "the prompt goes here", timeout_sec=10)

    assert stdin_dump.read_text() == "the prompt goes here"


# --------------------------------------------------------------------------- #
# Pure-function envelope parser (covers edge cases without subprocess)
# --------------------------------------------------------------------------- #


def test_parse_envelope_success():
    env, err = _parse_envelope('{"a": 1}')
    assert env == {"a": 1}
    assert err is None


def test_parse_envelope_empty():
    env, err = _parse_envelope("   \n\t")
    assert env == {}
    assert err is not None and "empty" in err.lower()


def test_parse_envelope_malformed():
    env, err = _parse_envelope("not json {")
    assert env == {}
    assert err is not None and "malformed" in err.lower()


def test_parse_envelope_not_a_dict():
    env, err = _parse_envelope("42")
    assert env == {}
    assert err is not None


def test_safe_str_coerces_non_string_non_none():
    assert _safe_str(None) == ""
    assert _safe_str("ok") == "ok"
    assert _safe_str(123) == "123"


def test_safe_float_handles_bad_inputs():
    assert _safe_float(None) == 0.0
    assert _safe_float(0.5) == 0.5
    assert _safe_float("nope") == 0.0
    assert _safe_float(object()) == 0.0


def test_decode_partial_handles_bytes_str_and_none():
    """``TimeoutExpired.stdout`` is bytes on current CPython, but the
    docs only promise "whatever was captured" -- be permissive."""
    assert _decode_partial(None) == ""
    assert _decode_partial(b"") == ""
    assert _decode_partial("") == ""
    assert _decode_partial(b"hello") == "hello"
    assert _decode_partial("already-decoded") == "already-decoded"
    # Half-written multi-byte UTF-8 sequence: replace, don't crash.
    assert "\ufffd" in _decode_partial(b"caf\xc3")


# --------------------------------------------------------------------------- #
# Process-group reap on timeout
# --------------------------------------------------------------------------- #


def test_subprocess_starts_new_session_for_group_kill(
    fake_claude, task_dir, personas_dir, monkeypatch
):
    """``start_new_session=True`` is non-negotiable: claude spawns tool
    subprocesses, and a per-step timeout must reap the whole subtree.
    Without it, orphans accumulate across the executor's loop and leak
    FDs / CPU. Pin the seam so a future refactor can't quietly drop it.
    """
    captured: dict = {}
    monkeypatch.setattr(
        sessions_mod, "Popen", _make_spy_popen(capture_kwargs=captured)
    )

    mgr = SessionManager(task_dir)
    mgr.invoke("rukawa", "p", timeout_sec=10)

    assert captured.get("start_new_session") is True


def _proc_is_live(pid: int) -> bool:
    """Linux-only liveness probe.

    Returns False if the process is gone OR is a zombie/exiting (``Z``
    / ``X``). We can't use bare ``os.kill(pid, 0)`` here: a SIGKILL-ed
    grandchild becomes a short-lived zombie reparented to PID 1 and
    ``kill(pid, 0)`` still succeeds for it. ``stuck_watchdog.proc_state``
    reads ``/proc/PID/stat`` and returns ``""`` once the table entry is
    gone, ``"Z"`` / ``"X"`` while exiting -- exactly what we need.
    """
    return stuck_watchdog.proc_state(pid) not in ("", "Z", "X")


def _wait_proc_gone(pid: int, timeout: float) -> bool:
    """Block (poll) until ``pid`` is gone or ``timeout`` elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _proc_is_live(pid):
            return True
        time.sleep(0.05)
    return False


@pytest.mark.skipif(
    not hasattr(os, "fork") or not Path("/proc/self/status").exists(),
    reason="needs POSIX fork() and Linux /proc to probe grandchild liveness",
)
def test_timeout_reaps_grandchild_process_subtree(
    fake_claude, task_dir, personas_dir, tmp_path, monkeypatch
):
    """End-to-end proof that timeout SIGKILLs the *whole subtree*.

    The fake forks a grandchild that announces its pid and hangs. After
    ``invoke`` times out, that pid must be gone from ``/proc`` — proving
    that ``killpg`` (not a bare ``kill``) was used.
    """
    hang_file = tmp_path / "grandchild.pid"
    monkeypatch.setenv("FAKE_FORK_HANG_FILE", str(hang_file))

    mgr = SessionManager(task_dir)
    result = mgr.invoke("rukawa", "p", timeout_sec=2)

    assert result.exit_code == TIMEOUT_EXIT_CODE

    # The fake forks ~immediately, but be defensive about scheduling.
    deadline = time.time() + 3.0
    while not hang_file.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert hang_file.exists(), "fake_claude never forked the grandchild"
    grandchild_pid = int(hang_file.read_text().strip())

    # SIGKILL delivery + init reap is fast but async; give it a budget.
    assert _wait_proc_gone(grandchild_pid, timeout=5.0), (
        f"grandchild pid={grandchild_pid} survived the timeout — "
        "the process-group SIGKILL path did not reap the subtree"
    )


def test_kill_process_group_handles_already_dead_leader(monkeypatch):
    """``_kill_process_group`` must be a no-op when the leader is gone.

    Race: between ``communicate`` raising ``TimeoutExpired`` and us
    calling ``getpgid``, the child can exit on its own. We must not
    raise — just return cleanly.
    """

    class _DeadProc:
        pid = 999999  # very unlikely to exist

    def _raise_lookup(_pid):
        raise ProcessLookupError()

    monkeypatch.setattr(sessions_mod.os, "getpgid", _raise_lookup)
    # Should not raise.
    SessionManager._kill_process_group(_DeadProc())  # type: ignore[arg-type]


def test_kill_process_group_falls_back_to_direct_kill(monkeypatch):
    """If ``killpg`` refuses (sandbox / restricted CI), fall back to a
    plain ``proc.kill()`` so the orphan still gets stopped."""

    monkeypatch.setattr(sessions_mod.os, "getpgid", lambda _pid: 42)

    def _refuse_killpg(_pgid, _sig):
        raise PermissionError("sandbox refuses killpg")

    monkeypatch.setattr(sessions_mod.os, "killpg", _refuse_killpg)

    direct_kill_called = {"value": False}

    class _Proc:
        pid = 12345

        def kill(self):
            direct_kill_called["value"] = True

    SessionManager._kill_process_group(_Proc())  # type: ignore[arg-type]
    assert direct_kill_called["value"] is True


def test_kill_process_group_swallows_direct_kill_errors(monkeypatch):
    """Even ``proc.kill()`` can race ProcessLookupError; must not raise."""

    monkeypatch.setattr(sessions_mod.os, "getpgid", lambda _pid: 42)

    def _refuse_killpg(_pgid, _sig):
        raise OSError("refused")

    monkeypatch.setattr(sessions_mod.os, "killpg", _refuse_killpg)

    class _Proc:
        pid = 12345

        def kill(self):
            raise ProcessLookupError()

    # No assertion needed — just verify no exception escapes.
    SessionManager._kill_process_group(_Proc())  # type: ignore[arg-type]


def test_keyboard_interrupt_reaps_subtree_then_propagates(
    fake_claude, task_dir, personas_dir, monkeypatch
):
    """SIGINT / SIGTERM mid-``communicate`` is the same orphan-leak
    class as a timeout. The ``except BaseException`` clause must reap
    the subtree (via ``killpg``) and then re-raise the original
    exception unchanged. Otherwise a Ctrl-C at the executor would
    silently leak every in-flight claude tool subprocess.
    """
    # Keep the real Popen so we have a real pid for getpgid/killpg,
    # but force ``communicate`` to raise KeyboardInterrupt before it
    # would naturally return. Also keep the fake busy so the pid is
    # definitely alive when killpg fires.
    monkeypatch.setenv("FAKE_SLEEP_SEC", "10")

    killpg_called: dict = {"value": False}
    real_killpg = sessions_mod.os.killpg

    def spy_killpg(pgid, sig):
        killpg_called["value"] = True
        real_killpg(pgid, sig)

    monkeypatch.setattr(sessions_mod.os, "killpg", spy_killpg)

    def _raise_interrupt(_self, _input, _timeout, _call):
        # Pretend the user hit Ctrl-C while we were blocked on claude's
        # reply. Intentionally never calls super(): whether the prompt
        # reached the child isn't what this test pins down.
        raise KeyboardInterrupt("user pressed ctrl-c")

    monkeypatch.setattr(
        sessions_mod,
        "Popen",
        _make_spy_popen(communicate_hook=_raise_interrupt),
    )

    mgr = SessionManager(task_dir)
    with pytest.raises(KeyboardInterrupt):
        mgr.invoke("rukawa", "p", timeout_sec=10)

    assert killpg_called["value"] is True, (
        "process-group SIGKILL was not delivered before propagating "
        "the interrupt — subtree would orphan on executor cancel"
    )


def test_interrupt_path_swallows_drain_timeout(
    fake_claude, task_dir, personas_dir, monkeypatch
):
    """If the post-kill ``communicate(timeout=_DRAIN_TIMEOUT_SEC)``
    drain itself times out (defensive belt-and-braces — pipes / zombie
    should clear fast after SIGKILL of the whole group), we must still
    propagate the original interrupt, not the inner ``TimeoutExpired``.

    Pinning ``communicate`` (not ``wait``) here also pins the
    FD-hygiene choice: the interrupt path drains via ``communicate``
    so stdin/stdout/stderr FDs close on the way out, matching the
    timeout path.
    """
    monkeypatch.setenv("FAKE_SLEEP_SEC", "10")

    timeout_cls = sessions_mod.TimeoutExpired

    def _interrupt_then_stuck_drain(self, _input, timeout, call):
        if call == 1:
            # The communicate inside the try block: interrupt mid-turn.
            raise KeyboardInterrupt("ctrl-c during turn")
        # The drain inside BaseException: stuck zombie / pipe exercises
        # the inner TimeoutExpired swallow.
        raise timeout_cls(cmd=self.args, timeout=timeout)

    monkeypatch.setattr(
        sessions_mod,
        "Popen",
        _make_spy_popen(communicate_hook=_interrupt_then_stuck_drain),
    )

    mgr = SessionManager(task_dir)
    with pytest.raises(KeyboardInterrupt):
        mgr.invoke("rukawa", "p", timeout_sec=10)


def test_drain_after_kill_timeout_does_not_propagate(
    fake_claude, task_dir, personas_dir, tmp_path, monkeypatch
):
    """If the post-kill drain ``communicate(timeout=_DRAIN_TIMEOUT_SEC)``
    itself times out (highly unusual — pipes should close fast once the
    group is dead — but defensively guarded), we still return a clean
    ``InvocationResult``.
    """
    monkeypatch.setenv("FAKE_SLEEP_SEC", "5")

    # Force the second communicate() to also raise TimeoutExpired so we
    # exercise the inner ``except`` branch deterministically.
    timeout_cls = sessions_mod.TimeoutExpired
    real_popen = sessions_mod.Popen

    def _real_first_then_stuck(self, input, timeout, call):
        if call == 1:
            # Mirror the real first-call timeout behaviour.
            return real_popen.communicate(self, input=input, timeout=timeout)
        # Second call: force TimeoutExpired so the drain's except fires.
        raise timeout_cls(cmd=self.args, timeout=timeout)

    monkeypatch.setattr(
        sessions_mod,
        "Popen",
        _make_spy_popen(communicate_hook=_real_first_then_stuck),
    )

    mgr = SessionManager(task_dir)
    result = mgr.invoke("rukawa", "p", timeout_sec=1)

    assert result.exit_code == TIMEOUT_EXIT_CODE
    assert result.error is not None
    assert "timeout" in result.error.lower()


# --------------------------------------------------------------------------- #
# Public API surface
# --------------------------------------------------------------------------- #


def test_public_api_reexport_from_package():
    """The three public names are reachable from the package root.

    The executor (and any future consumer) should be able to write
    ``from tigerharness.workflow_runner import SessionManager`` the
    same way it imports the other Phase 1 primitives. Pinned so a
    future refactor can't silently drop the re-export.
    """
    import tigerharness.workflow_runner as wr

    assert wr.SessionManager is SessionManager
    assert wr.InvocationResult is InvocationResult
    assert wr.TIMEOUT_EXIT_CODE == TIMEOUT_EXIT_CODE
    for name in ("SessionManager", "InvocationResult", "TIMEOUT_EXIT_CODE"):
        assert name in wr.__all__, f"{name} missing from workflow_runner.__all__"

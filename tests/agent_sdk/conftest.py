"""Pytest fixtures shared across the test modules.

The ``asyncio_test`` decorator (used in lieu of ``pytest-asyncio``) lives in
``_helpers.py`` so it can be imported as a normal module. This file holds
fixtures only.

Fake CLI scripts are written into a per-session tempdir; they stand in for
``claude -p`` and emit a deterministic stream-json sequence so we can
exercise every parsing path without a real Claude binary.
"""

from __future__ import annotations

import stat
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

# Note: ``agent_sdk`` is importable because pyproject.toml puts the project
# root on pytest's rootdir, so no explicit sys.path manipulation is needed.


# --- fake-CLI factory --------------------------------------------------------


def _write_fake_cli(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def make_cli(tmp_path: Path) -> Callable[[str, str], Path]:
    """Return a factory that writes an executable fake-claude script.

    Usage:
        cli = make_cli("ok", '''
            import json, sys
            sys.stdin.read()
            print(json.dumps({"type": "system", "subtype": "init", ...}))
        ''')
    """
    counter = {"n": 0}

    def _make(label: str, body: str) -> Path:
        counter["n"] += 1
        target = tmp_path / f"fake-claude-{label}-{counter['n']}"
        return _write_fake_cli(target, body)

    return _make


# Convenience canned scripts ---------------------------------------------------


SUCCESS_TEXT_BODY = """
import json, sys
sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init",
                  "session_id": "sess-ok", "model": "test-model"}), flush=True)
print(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
    {"type": "text", "text": "Hello there."}
]}}), flush=True)
print(json.dumps({"type": "result", "subtype": "success",
                  "session_id": "sess-ok", "result": "Hello there.",
                  "total_cost_usd": 0.001,
                  "usage": {"input_tokens": 5, "output_tokens": 3}}), flush=True)
"""

TOOL_ROUNDTRIP_BODY = """
import json, sys
sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init",
                  "session_id": "sess-tools", "model": "test-model"}), flush=True)
print(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
    {"type": "thinking", "thinking": "Let me think...", "signature": "sig"},
    {"type": "tool_use", "id": "call_1", "name": "Bash", "input": {"command": "ls"}}
]}}), flush=True)
print(json.dumps({"type": "user", "message": {"role": "user", "content": [
    {"type": "tool_result", "tool_use_id": "call_1",
     "content": "a.py\\nb.py", "is_error": False}
]}}), flush=True)
print(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
    {"type": "text", "text": "Two python files."}
]}}), flush=True)
print(json.dumps({"type": "result", "subtype": "success",
                  "session_id": "sess-tools", "result": "Two python files.",
                  "total_cost_usd": 0.005}), flush=True)
"""

MAX_TURNS_BODY = """
import json, sys
sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init",
                  "session_id": "s", "model": "m"}), flush=True)
print(json.dumps({"type": "result", "subtype": "error_max_turns",
                  "session_id": "s", "result": None}), flush=True)
"""

MAX_BUDGET_BODY = """
import json, sys
sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init",
                  "session_id": "s", "model": "m"}), flush=True)
print(json.dumps({"type": "result", "subtype": "error_max_budget_usd",
                  "session_id": "s", "result": None}), flush=True)
"""

GENERIC_ERROR_BODY = """
import json, sys
sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init",
                  "session_id": "s", "model": "m"}), flush=True)
print(json.dumps({"type": "result", "subtype": "error_other",
                  "session_id": "s", "result": "boom"}), flush=True)
"""

NONZERO_EXIT_BODY = """
import sys
sys.stdin.read()
sys.stderr.write("subprocess crashed\\n")
sys.exit(7)
"""

BAD_JSON_BODY = """
import json, sys
sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init",
                  "session_id": "s", "model": "m"}), flush=True)
print("this is not json", flush=True)
print(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
    {"type": "text", "text": "ok"}
]}}), flush=True)
print(json.dumps({"type": "result", "subtype": "success",
                  "session_id": "s", "result": "ok"}), flush=True)
"""

LARGE_STDERR_BODY = """
import json, sys, time
# Emit ~2 MB of stderr in chunks, *interleaved* with stdout writes. Without
# a concurrent drainer this overflows the OS pipe buffer (typically 64 KB,
# up to ~1 MB on modern Linux) and the subprocess blocks on stderr.write,
# never producing the final result event — the test would hang.
sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init",
                  "session_id": "s", "model": "m"}), flush=True)
for _ in range(20):
    sys.stderr.write("Z" * 100_000)
    sys.stderr.flush()
print(json.dumps({"type": "result", "subtype": "success",
                  "session_id": "s", "result": "drained",
                  "structured_output": {"answer": 42},
                  "total_cost_usd": 0.0}), flush=True)
"""

EMPTY_ASSISTANT_BODY = """
import json, sys
sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init",
                  "session_id": "s", "model": "m"}), flush=True)
# Empty content + content as plain string + unknown block type
print(json.dumps({"type": "assistant", "message": {"role": "assistant",
                                                   "content": []}}), flush=True)
print(json.dumps({"type": "user", "message": {"role": "user",
                                               "content": "raw string content"}}), flush=True)
print(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
    {"type": "unknown_block_type", "wat": 1}
]}}), flush=True)
print(json.dumps({"type": "stream_event", "ignored": True}), flush=True)
print(json.dumps({"type": "result", "subtype": "success",
                  "session_id": "s", "result": "ok"}), flush=True)
"""

USER_TEXT_AND_BLANK_BODY = """
import json, sys
sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init",
                  "session_id": "s", "model": "m"}), flush=True)
# A blank/whitespace-only line that the parser should silently skip.
print("", flush=True)
print("   ", flush=True)
# A user message whose content list has a plain `text` block (not tool_result).
print(json.dumps({"type": "user", "message": {"role": "user", "content": [
    {"type": "text", "text": "context note"}
]}}), flush=True)
print(json.dumps({"type": "result", "subtype": "success",
                  "session_id": "s", "result": "ok"}), flush=True)
"""

RESULT_WITHOUT_INIT_BODY = """
import json, sys
sys.stdin.read()
# No `system.init` event — the parser must still capture session_id from
# the result event so the caller's session is populated.
print(json.dumps({"type": "result", "subtype": "success",
                  "session_id": "from-result", "result": "ok"}), flush=True)
"""

SLOW_BODY = """
import json, sys, time, signal
sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init",
                  "session_id": "s", "model": "m"}), flush=True)
def _bye(*a):
    sys.exit(0)
signal.signal(signal.SIGINT, _bye)
for i in range(50):
    print(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": f"chunk {i}"}
    ]}}), flush=True)
    time.sleep(0.05)
print(json.dumps({"type": "result", "subtype": "success",
                  "session_id": "s", "result": "done"}), flush=True)
"""


@pytest.fixture
def cli_success(make_cli: Callable[[str, str], Path]) -> Path:
    return make_cli("success", SUCCESS_TEXT_BODY)


@pytest.fixture
def cli_tools(make_cli: Callable[[str, str], Path]) -> Path:
    return make_cli("tools", TOOL_ROUNDTRIP_BODY)


@pytest.fixture
def cli_max_turns(make_cli: Callable[[str, str], Path]) -> Path:
    return make_cli("max-turns", MAX_TURNS_BODY)


@pytest.fixture
def cli_max_budget(make_cli: Callable[[str, str], Path]) -> Path:
    return make_cli("max-budget", MAX_BUDGET_BODY)


@pytest.fixture
def cli_generic_error(make_cli: Callable[[str, str], Path]) -> Path:
    return make_cli("error", GENERIC_ERROR_BODY)


@pytest.fixture
def cli_nonzero(make_cli: Callable[[str, str], Path]) -> Path:
    return make_cli("nonzero", NONZERO_EXIT_BODY)


@pytest.fixture
def cli_bad_json(make_cli: Callable[[str, str], Path]) -> Path:
    return make_cli("bad-json", BAD_JSON_BODY)


@pytest.fixture
def cli_large_stderr(make_cli: Callable[[str, str], Path]) -> Path:
    return make_cli("large-stderr", LARGE_STDERR_BODY)


@pytest.fixture
def cli_empty_assistant(make_cli: Callable[[str, str], Path]) -> Path:
    return make_cli("empty", EMPTY_ASSISTANT_BODY)


@pytest.fixture
def cli_slow(make_cli: Callable[[str, str], Path]) -> Path:
    return make_cli("slow", SLOW_BODY)


@pytest.fixture
def cli_user_text_and_blank(make_cli: Callable[[str, str], Path]) -> Path:
    return make_cli("user-text", USER_TEXT_AND_BLANK_BODY)


@pytest.fixture
def cli_result_without_init(make_cli: Callable[[str, str], Path]) -> Path:
    return make_cli("no-init", RESULT_WITHOUT_INIT_BODY)


@pytest.fixture
def isolated_registry() -> Any:
    """Snapshot ``factory._REGISTRY`` and restore it after the test.

    Tests that call ``register_backend`` should request this fixture so a
    failure mid-test can't leave the global registry corrupted for the rest
    of the suite.
    """
    from tigerharness.agent_sdk.factory import _REGISTRY

    snapshot = dict(_REGISTRY)
    try:
        yield
    finally:
        _REGISTRY.clear()
        _REGISTRY.update(snapshot)

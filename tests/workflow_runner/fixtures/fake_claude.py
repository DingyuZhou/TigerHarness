"""Fake ``claude`` CLI used by ``test_sessions.py``.

This module is **not** imported by the tests. The ``fake_claude``
fixture reads its source, prepends ``#!{sys.executable}\\n``, writes
it to a tmp path, marks it executable, and points
``TIGERHARNESS_CLAUDE_BIN`` at the result so that
:class:`SessionManager` shells out to it instead of real ``claude``.

Behaviour is steered by env vars set in the test:

* ``FAKE_SLEEP_SEC``        sleep this many seconds before printing.
* ``FAKE_EXIT_CODE``        exit with this code (default 0).
* ``FAKE_STDERR``           print this to stderr.
* ``FAKE_STDOUT_OVERRIDE``  print this verbatim instead of an envelope.
* ``FAKE_SESSION_ID``       ``session_id`` in the emitted envelope
                            (default ``'fresh-sid-001'``).
* ``FAKE_RESULT_TEXT``      assistant text in the envelope
                            (default echoes the stdin payload).
* ``FAKE_COST_USD``         ``total_cost_usd`` in the envelope
                            (default ``0.0123``). Set to ``'__omit__'``
                            to drop the field entirely.
* ``FAKE_ENVELOPE_RAW``     if non-empty, emit this string verbatim
                            (used for malformed-JSON tests).
* ``FAKE_PARTIAL_STDOUT``   if non-empty, print + flush this **before**
                            sleeping (exercises partial-stdout capture
                            on timeout).
* ``FAKE_ARGV_DUMP``        if set, write received argv (JSON) here.
* ``FAKE_STDIN_DUMP``       if set, write stdin (raw) here.
* ``FAKE_FORK_HANG_FILE``   if set, ``fork()`` a hanging grandchild
                            (writes its own pid to this path), then
                            the parent also hangs. Exercises the
                            process-group reap path in
                            :meth:`SessionManager._kill_process_group`.

Zero deps beyond the stdlib so it runs under any interpreter.
"""

from __future__ import annotations

import json
import os
import sys
import time


def _dump(path_env: str, payload: str) -> None:
    path = os.environ.get(path_env, "").strip()
    if not path:
        return
    with open(path, "w") as fh:
        fh.write(payload)


def main() -> int:
    _dump("FAKE_ARGV_DUMP", json.dumps(sys.argv))
    stdin_data = sys.stdin.read()
    _dump("FAKE_STDIN_DUMP", stdin_data)

    partial = os.environ.get("FAKE_PARTIAL_STDOUT", "")
    if partial:
        sys.stdout.write(partial)
        sys.stdout.flush()

    fork_hang_file = os.environ.get("FAKE_FORK_HANG_FILE", "").strip()
    if fork_hang_file:
        # Fork a grandchild that announces its pid then hangs. Parent
        # also hangs so the test driver must time out and reap the
        # whole group via killpg(SIGKILL).
        pid = os.fork()
        if pid == 0:
            # Grandchild: detach from the parent's stdout/stderr so a
            # killpg-induced pipe close doesn't race with our write.
            with open(fork_hang_file, "w") as fh:
                fh.write(str(os.getpid()))
                fh.flush()
                os.fsync(fh.fileno())
            time.sleep(120)
            sys.exit(0)
        # Parent: hang until killed.
        time.sleep(120)

    sleep_for = float(os.environ.get("FAKE_SLEEP_SEC", "0") or "0")
    if sleep_for > 0:
        time.sleep(sleep_for)

    stderr_text = os.environ.get("FAKE_STDERR", "")
    if stderr_text:
        sys.stderr.write(stderr_text)
        sys.stderr.flush()

    override = os.environ.get("FAKE_STDOUT_OVERRIDE", "")
    raw_envelope = os.environ.get("FAKE_ENVELOPE_RAW", "")
    if override:
        sys.stdout.write(override)
    elif raw_envelope:
        sys.stdout.write(raw_envelope)
    else:
        envelope = {
            "type": "result",
            "subtype": "success",
            "result": os.environ.get("FAKE_RESULT_TEXT", stdin_data),
            "session_id": os.environ.get(
                "FAKE_SESSION_ID", "fresh-sid-001"
            ),
        }
        cost = os.environ.get("FAKE_COST_USD", "0.0123")
        if cost != "__omit__":
            envelope["total_cost_usd"] = float(cost)
        sys.stdout.write(json.dumps(envelope))

    return int(os.environ.get("FAKE_EXIT_CODE", "0") or "0")


if __name__ == "__main__":
    sys.exit(main())

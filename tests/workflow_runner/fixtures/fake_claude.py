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
* ``FAKE_PARTIAL_STDERR``   if non-empty, write + flush this to stderr
                            **before** sleeping (exercises partial-
                            stderr capture on timeout).
* ``FAKE_ARGV_DUMP``        if set, write received argv (JSON) here.
* ``FAKE_STDIN_DUMP``       if set, write stdin (raw) here.
* ``FAKE_FORK_HANG_FILE``   if set, ``fork()`` a hanging grandchild
                            (writes its own pid to this path), then
                            the parent also hangs. Exercises the
                            process-group reap path in
                            :meth:`SessionManager._kill_process_group`.

Scripted multi-call mode (used by ``tests/workflow_runner/e2e``):

* ``FAKE_CLAUDE_SCRIPT``    path to a JSON file with shape
                            ``{"responses": [<entry>, ...]}``. Each
                            invocation pops the next entry off the
                            list (tracked via a sidecar
                            ``<script>.counter`` integer file) and
                            emits the corresponding envelope.

                            Entry fields (all optional unless noted):
                              ``trailer``     -- *required*; verbatim
                                                 ``WORKFLOW: ...`` line
                                                 appended after the body.
                              ``body``        -- optional prose preamble
                                                 (default: a short stub).
                              ``cost_usd``    -- float (default 0.0).
                              ``session_id``  -- override the envelope sid.
                                                 If unset, the fake echoes
                                                 the ``--resume <sid>`` arg
                                                 when present, else mints
                                                 ``sid-fresh-{N}``.
                              ``persona``     -- documentation only.
                              ``iter``        -- documentation only.

                            Over-run (counter past the list end) emits
                            ``WORKFLOW: BLOCK: ran off fake-claude script``
                            with a clearly-marked body, so a test that
                            under-counted its responses fails loudly
                            rather than hangs.

                            When ``FAKE_CLAUDE_SCRIPT`` is unset the
                            scripted branch is skipped entirely; the
                            ``FAKE_*`` env-var path above is preserved
                            verbatim so existing tests are unaffected.

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


def _resume_sid_from_argv(argv: list[str]) -> str | None:
    """Return the value passed to ``--resume`` on argv, or ``None``.

    Used by the scripted branch to preserve session-id continuity for
    free: when the workflow-runner calls back with ``--resume <sid>``,
    the fake echoes that same sid in its envelope unless the script
    entry explicitly overrides it. Keeps end-to-end tests realistic
    without forcing the scripter to track session-id rotation.
    """
    try:
        idx = argv.index("--resume")
    except ValueError:
        return None
    if idx + 1 >= len(argv):
        return None
    return argv[idx + 1]


def _scripted_envelope(stdin_data: str) -> str | None:
    """Return a JSON envelope built from ``FAKE_CLAUDE_SCRIPT``, or ``None``.

    When ``FAKE_CLAUDE_SCRIPT`` is unset we return ``None`` and the
    caller falls back to the legacy env-var-driven behaviour. This
    keeps every existing test that uses the fake claude binary
    completely unaffected.
    """
    script_path = os.environ.get("FAKE_CLAUDE_SCRIPT", "").strip()
    if not script_path:
        return None

    try:
        with open(script_path, "r") as fh:
            script = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        # Surface the failure as a visible BLOCK so the test that
        # owns this script sees a clean assertion miss rather than a
        # mysterious silent hang.
        return json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "result": (
                    "[fake-claude] failed to load script "
                    f"{script_path!r}: {exc}\n\n"
                    "WORKFLOW: BLOCK: script load failed"
                ),
                "session_id": "sid-script-load-error",
                "total_cost_usd": 0.0,
            }
        )

    responses = script.get("responses") if isinstance(script, dict) else None
    if not isinstance(responses, list) or not responses:
        return json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "result": (
                    "[fake-claude] script missing/empty 'responses' "
                    f"in {script_path!r}\n\n"
                    "WORKFLOW: BLOCK: script malformed"
                ),
                "session_id": "sid-script-malformed",
                "total_cost_usd": 0.0,
            }
        )

    counter_path = script_path + ".counter"
    try:
        with open(counter_path, "r") as fh:
            idx = int(fh.read().strip() or "0")
    except (OSError, ValueError):
        idx = 0

    # Bump the counter *before* emitting so a crash-mid-emit still
    # advances the cursor on retry; that's friendlier than infinite
    # replay of the same response.
    try:
        with open(counter_path, "w") as fh:
            fh.write(str(idx + 1))
    except OSError:
        # If we can't write the counter, the test will hang in a
        # replay loop -- surface that as a BLOCK on this call.
        return json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "result": (
                    "[fake-claude] cannot write counter "
                    f"{counter_path!r}\n\n"
                    "WORKFLOW: BLOCK: counter write failed"
                ),
                "session_id": "sid-counter-error",
                "total_cost_usd": 0.0,
            }
        )

    if idx >= len(responses):
        # Ran past the end of the scripted plan: emit a visible BLOCK
        # rather than hang or silently repeat. The script author
        # should have provided one more response for this invocation.
        return json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "result": (
                    "[fake-claude] ran off the end of the script "
                    f"(call #{idx + 1}, only {len(responses)} responses "
                    f"defined in {script_path!r})\n\n"
                    "WORKFLOW: BLOCK: ran off fake-claude script"
                ),
                "session_id": "sid-script-overrun",
                "total_cost_usd": 0.0,
            }
        )

    entry = responses[idx]
    if not isinstance(entry, dict):
        return json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "result": (
                    f"[fake-claude] response #{idx} is not a dict: "
                    f"{type(entry).__name__}\n\n"
                    "WORKFLOW: BLOCK: bad response entry"
                ),
                "session_id": "sid-bad-entry",
                "total_cost_usd": 0.0,
            }
        )

    trailer = entry.get("trailer")
    if not isinstance(trailer, str) or not trailer.strip():
        return json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "result": (
                    f"[fake-claude] response #{idx} missing 'trailer'\n\n"
                    "WORKFLOW: BLOCK: missing trailer"
                ),
                "session_id": "sid-missing-trailer",
                "total_cost_usd": 0.0,
            }
        )

    body = entry.get("body")
    if not isinstance(body, str):
        # Default body acknowledges the prompt and tags the call index
        # so a failing assertion can immediately tell which scripted
        # turn produced it.
        body = (
            f"[fake-claude] scripted reply #{idx + 1} "
            f"(persona={entry.get('persona', '?')}, "
            f"iter={entry.get('iter', '?')})"
        )

    cost = entry.get("cost_usd", 0.0)
    try:
        cost_f = float(cost)
    except (TypeError, ValueError):
        cost_f = 0.0

    sid_override = entry.get("session_id")
    if isinstance(sid_override, str) and sid_override.strip():
        sid: str = sid_override
    else:
        resumed = _resume_sid_from_argv(sys.argv)
        sid = resumed if resumed else f"sid-fresh-{idx + 1}"

    # The trailer must be on its own line (or at least visibly
    # separated) so the parser's "scan all lines" rule finds it.
    result_text = f"{body}\n\n{trailer}"

    # Stash extra metadata in the envelope so tests can introspect.
    envelope = {
        "type": "result",
        "subtype": "success",
        "result": result_text,
        "session_id": sid,
        "total_cost_usd": cost_f,
    }
    return json.dumps(envelope)


def main() -> int:
    _dump("FAKE_ARGV_DUMP", json.dumps(sys.argv))
    stdin_data = sys.stdin.read()
    _dump("FAKE_STDIN_DUMP", stdin_data)

    partial = os.environ.get("FAKE_PARTIAL_STDOUT", "")
    if partial:
        sys.stdout.write(partial)
        sys.stdout.flush()

    partial_err = os.environ.get("FAKE_PARTIAL_STDERR", "")
    if partial_err:
        sys.stderr.write(partial_err)
        sys.stderr.flush()

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
    scripted = _scripted_envelope(stdin_data)
    if override:
        sys.stdout.write(override)
    elif raw_envelope:
        sys.stdout.write(raw_envelope)
    elif scripted is not None:
        # Scripted branch wins over the legacy single-shot envelope
        # because tests that opt into a script are doing so precisely
        # to drive multi-call behaviour; any stray FAKE_RESULT_TEXT in
        # the environment would otherwise mask the scripted reply.
        sys.stdout.write(scripted)
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

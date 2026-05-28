"""Per-persona Claude session manager for the workflow-runner.

This is the load-bearing seam between deterministic Python (the
executor) and the AI side. For one task, each persona owns one
``claude`` session whose id is persisted in ``sessions.json`` so that
the persona remembers prior iterations across loop turns.

Design picks
------------

* **Single-shot envelope, synchronous subprocess.** The workflow-runner
  drives one persona at a time and waits for a single
  ``--output-format json`` envelope per turn. There's no need for the
  streaming/async machinery in ``agent_sdk.backends.claude_p``; a
  plain :class:`subprocess.Popen` keeps the code surface tiny and
  trivially testable with a fake CLI on disk.

* **Subtree-safe timeout.** ``claude`` spawns tool subprocesses
  (Bash, MCP servers, ...). A naive ``subprocess.run(timeout=...)``
  only SIGKILLs the direct child, orphaning the rest. We instead
  spawn with ``start_new_session=True`` and ``os.killpg`` the whole
  group on timeout so the executor's loop never leaks descendant
  processes across iterations.

* **Reuse, don't reinvent.**

  - Atomic JSON I/O comes from
    :mod:`tigerharness.workflow_runner.atomic`.
  - The typed view of ``sessions.json`` is
    :class:`tigerharness.workflow_runner.models.SessionMap`.
  - Persona prompt lookup and the ``_SUDO_DENY`` floor come from
    :mod:`tigerharness.task_runner.personas`. Reusing the floor is
    not cosmetic: it keeps the workflow-runner's per-turn argv
    consistent with the rest of the harness so a persona that can't
    run sudo in task-runner mode can't suddenly run it here.

* **Errors become data, not exceptions.** Per the brief: on timeout,
  non-zero exit, or malformed JSON, we populate :class:`InvocationResult`
  with an ``error`` string and let the executor decide how to react.
  Configuration errors (missing persona prompt file) propagate as
  ``FileNotFoundError`` — those are setup bugs that should fail loud.

* **Claude binary is injectable.** ``TIGERHARNESS_CLAUDE_BIN`` lets the
  tests point at a Python fake without monkeypatching ``subprocess``.
  Production callers leave it unset → defaults to ``"claude"`` on
  ``$PATH``.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from tigerharness.workflow_runner.atomic import read_json, write_json_atomic
from tigerharness.workflow_runner.models import SessionMap

log = logging.getLogger("tigerharness.workflow_runner.sessions")


# --------------------------------------------------------------------------- #
# Sentinels
# --------------------------------------------------------------------------- #

#: Returned in ``InvocationResult.exit_code`` when the subprocess timed out.
#: Out of band from POSIX signal-encoded returncodes (which are in
#: ``-1..-64`` for ``-SIGNUM``) so callers can distinguish "we killed it
#: for timing out" from "the OS killed it with SIGHUP/SIGINT/etc."
TIMEOUT_EXIT_CODE = -1001

#: Wall-clock cap for draining buffered stdout *after* SIGKILL-ing the
#: child's process group. Once the group is dead, the pipes close fast;
#: a few seconds is plenty of headroom for a stuck FS or scheduler.
_DRAIN_TIMEOUT_SEC = 5


# --------------------------------------------------------------------------- #
# Argv flags (named so a grep for the flag turns up here, not a string literal)
# --------------------------------------------------------------------------- #

_FLAG_PRINT = "-p"
_FLAG_OUTPUT_FORMAT = "--output-format"
_OUTPUT_FORMAT_JSON = "json"
_FLAG_RESUME = "--resume"
_FLAG_APPEND_SYSTEM_PROMPT = "--append-system-prompt"
_FLAG_DISALLOWED_TOOLS = "--disallowedTools"


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class InvocationResult:
    """Outcome of one ``SessionManager.invoke`` call.

    The executor consumes this and decides whether the iteration
    succeeded, needs re-prompting, or should escalate. ``error`` is
    ``None`` on success and a short human-readable string otherwise.
    """

    stdout: str
    session_id: str
    cost_usd: float
    exit_code: int
    error: Optional[str]
    raw_envelope: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Session manager
# --------------------------------------------------------------------------- #


class SessionManager:
    """Owns the (persona -> claude_session_id) map for one task.

    One instance per task. Not thread-safe; serialise calls externally
    if you ever drive multiple personas concurrently (Phase 1 doesn't).
    """

    _SESSIONS_FILENAME = "sessions.json"

    def __init__(self, task_dir: Path) -> None:
        self._task_dir = Path(task_dir)
        self._sessions_path = self._task_dir / self._SESSIONS_FILENAME

    # ------------------------------------------------------------------ #
    # sessions.json I/O
    # ------------------------------------------------------------------ #

    def _load(self) -> SessionMap:
        """Read ``sessions.json``; missing file -> empty map."""
        try:
            raw = read_json(self._sessions_path)
        except FileNotFoundError:
            return SessionMap()
        return SessionMap.from_dict(raw)

    def _save(self, smap: SessionMap) -> None:
        write_json_atomic(self._sessions_path, smap.to_dict(), sort_keys=True)

    def get_session_id(self, persona: str) -> Optional[str]:
        """Return the persisted session id for ``persona`` or ``None``."""
        return self._load().get(persona)

    # ------------------------------------------------------------------ #
    # Invocation
    # ------------------------------------------------------------------ #

    def invoke(
        self,
        persona: str,
        prompt: str,
        *,
        timeout_sec: int,
        log_dir: Optional[Path] = None,
    ) -> InvocationResult:
        """Run one ``claude -p`` turn for ``persona`` with ``prompt``.

        First call for a persona spawns a fresh session
        (``--append-system-prompt <persona-prompt>``); subsequent calls
        resume the persisted session id (``--resume <sid>``).

        Parameters
        ----------
        persona:
            Persona name; used both to look up the prompt file and as
            the key in ``sessions.json``.
        prompt:
            The user-side text to send on the subprocess's stdin.
        timeout_sec:
            Wall-clock limit. On timeout the result reports
            ``exit_code=TIMEOUT_EXIT_CODE`` and a populated ``error``;
            we do **not** raise.
        log_dir:
            If provided, ``prompt.txt`` / ``envelope.json`` /
            ``stdout.txt`` are written here. Caller owns directory
            creation; we make it if absent.
        """
        if timeout_sec <= 0:
            raise ValueError(
                f"timeout_sec must be > 0, got {timeout_sec!r}"
            )

        # Single load per invoke: the executor's task lock guarantees
        # we're the sole writer for this task, so the smap we read at
        # the top is still authoritative when we go to persist below.
        smap = self._load()
        existing_sid = smap.get(persona)
        argv = self._build_argv(persona, existing_sid)

        # ``start_new_session=True`` makes ``proc.pid`` the leader of a
        # fresh session/group so we can ``killpg`` the *entire subtree*
        # on timeout. Without it ``subprocess.run``'s timeout path only
        # SIGKILLs the direct child, leaving claude's tool subprocesses
        # (Bash, MCP servers, etc.) orphaned to leak FDs/CPU across the
        # executor's loop.
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(
                input=prompt, timeout=timeout_sec
            )
            returncode = proc.returncode
        except subprocess.TimeoutExpired as exc:
            self._kill_process_group(proc)
            pre_kill = _decode_partial(exc.stdout)
            # After group SIGKILL the pipes close fast; drain any
            # buffered bytes so partial output survives in stdout.txt
            # and we don't leak the FDs.
            try:
                leftover_stdout, _ = proc.communicate(
                    timeout=_DRAIN_TIMEOUT_SEC
                )
            except subprocess.TimeoutExpired:
                leftover_stdout = ""
            partial = pre_kill + _decode_partial(leftover_stdout)
            if log_dir is not None:
                self._capture_timeout(
                    log_dir, prompt=prompt, partial_stdout=partial
                )
            return InvocationResult(
                stdout="",
                session_id=existing_sid or "",
                cost_usd=0.0,
                exit_code=TIMEOUT_EXIT_CODE,
                error=f"timeout after {timeout_sec} seconds",
                raw_envelope={},
            )
        except BaseException:
            # SIGINT / SIGTERM / executor shutdown mid-turn: same
            # orphan-leak risk as a timeout. Reap the subtree *before*
            # propagating so the surrounding cleanup can't leave
            # claude tool subprocesses running. Re-raises the original
            # exception (including KeyboardInterrupt / SystemExit) so
            # callers see the failure they expected.
            #
            # NB: ``communicate`` (not ``wait``) so the stdin / stdout
            # / stderr pipe FDs get closed on the way out. ``wait``
            # only reaps the zombie; the pipes would linger until GC.
            # Symmetric with the timeout path above.
            self._kill_process_group(proc)
            try:
                proc.communicate(timeout=_DRAIN_TIMEOUT_SEC)
            except subprocess.TimeoutExpired:
                pass
            raise

        envelope, parse_error = _parse_envelope(stdout)

        # Non-zero exit overrides any extractable session data; we still
        # try to parse the envelope so cost / sid (if present) survive.
        error: Optional[str] = None
        if returncode != 0:
            stderr_excerpt = (stderr or "").strip()
            error = (
                f"claude exited with code {returncode}"
                + (f": {stderr_excerpt}" if stderr_excerpt else "")
            )
        elif parse_error is not None:
            error = parse_error

        envelope_sid = _safe_str(envelope.get("session_id"))
        # Prefer the envelope sid (the CLI may rotate it on resume);
        # fall back to existing on parse failure so we don't lose track.
        session_id = envelope_sid or (existing_sid or "")

        if (
            "total_cost_usd" not in envelope
            and returncode == 0
            and parse_error is None
        ):
            log.warning(
                "claude envelope for persona=%r missing 'total_cost_usd'; "
                "defaulting cost to 0.0",
                persona,
            )
        cost_usd = _safe_float(envelope.get("total_cost_usd"))

        stdout_text = _safe_str(
            envelope.get("result") or envelope.get("structured_output") or ""
        )

        if envelope_sid and envelope_sid != existing_sid:
            smap.set(persona, envelope_sid)
            self._save(smap)

        if log_dir is not None:
            self._capture_success(
                log_dir,
                prompt=prompt,
                envelope=envelope,
                stdout_text=stdout_text,
            )

        return InvocationResult(
            stdout=stdout_text,
            session_id=session_id,
            cost_usd=cost_usd,
            exit_code=returncode,
            error=error,
            raw_envelope=envelope,
        )

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _build_argv(
        self, persona: str, existing_sid: Optional[str]
    ) -> list[str]:
        binary = os.environ.get("TIGERHARNESS_CLAUDE_BIN", "").strip() or "claude"
        argv: list[str] = [
            binary,
            _FLAG_PRINT,
            _FLAG_OUTPUT_FORMAT,
            _OUTPUT_FORMAT_JSON,
        ]
        # Late import: personas.py runs an autoload side-effect at
        # import time that depends on env vars only set in real
        # task-runner contexts. Importing lazily keeps unrelated
        # workflow_runner consumers light.
        from tigerharness.task_runner.personas import _SUDO_DENY, load_prompt

        argv += [_FLAG_DISALLOWED_TOOLS, ",".join(_SUDO_DENY)]

        if existing_sid:
            argv += [_FLAG_RESUME, existing_sid]
        else:
            argv += [_FLAG_APPEND_SYSTEM_PROMPT, load_prompt(persona)]
        return argv

    @staticmethod
    def _kill_process_group(proc: subprocess.Popen) -> None:
        """SIGKILL the child's whole process group.

        ``start_new_session=True`` made ``proc.pid`` the group leader,
        so ``killpg`` reaches every descendant (claude tool subprocesses,
        MCP servers, etc.) — not just the direct child. Falls back to a
        plain ``proc.kill()`` if the leader is already gone or the OS
        refuses (sandboxes, restricted CI runners, etc.); even that's
        best-effort, hence the broad except.
        """
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            return  # Already gone.
        try:
            os.killpg(pgid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass

    @staticmethod
    def _capture_success(
        log_dir: Path,
        *,
        prompt: str,
        envelope: dict[str, Any],
        stdout_text: str,
    ) -> None:
        """Write ``prompt.txt`` / ``envelope.json`` / ``stdout.txt`` for
        a completed turn."""
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        write_json_atomic(log_dir / "envelope.json", envelope)
        (log_dir / "stdout.txt").write_text(stdout_text, encoding="utf-8")

    @staticmethod
    def _capture_timeout(
        log_dir: Path, *, prompt: str, partial_stdout: str
    ) -> None:
        """Write the three log files when ``claude`` timed out.

        ``envelope.json`` is the empty object (no envelope was parsed)
        and ``stdout.txt`` holds whatever bytes the subprocess managed
        to flush before we killed it — invaluable when debugging a
        wedged turn.
        """
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        write_json_atomic(log_dir / "envelope.json", {})
        (log_dir / "stdout.txt").write_text(partial_stdout, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Envelope parsing helpers
# --------------------------------------------------------------------------- #


def _parse_envelope(text: str) -> tuple[dict[str, Any], Optional[str]]:
    """Parse the claude ``--output-format json`` envelope.

    Returns ``(envelope, error)``. On success, ``error`` is ``None``.
    On parse failure, ``envelope`` is an empty dict and ``error`` is a
    short diagnostic. We deliberately don't raise: the caller wants
    every failure expressed as an ``InvocationResult``.
    """
    stripped = text.strip()
    if not stripped:
        return {}, "claude produced empty stdout"
    try:
        loaded = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return {}, f"malformed JSON envelope: {exc.msg}"
    if not isinstance(loaded, dict):
        return {}, (
            "claude envelope must be a JSON object, "
            f"got {type(loaded).__name__}"
        )
    return loaded, None


def _safe_str(value: Any) -> str:
    """Coerce ``value`` to ``str`` defensively (``None`` -> ``""``)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _decode_partial(value: bytes | str | None) -> str:
    """Coerce ``TimeoutExpired.stdout`` to ``str``.

    Bytes -> utf-8 decode with ``errors="replace"`` so a half-written
    multi-byte char never crashes log capture. Str -> passthrough.
    ``None`` / empty -> ``""``.
    """
    if not value:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _safe_float(value: Any) -> float:
    """Coerce ``value`` to ``float``; bad / missing inputs return 0.0."""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


__all__ = [
    "InvocationResult",
    "SessionManager",
    "TIMEOUT_EXIT_CODE",
]

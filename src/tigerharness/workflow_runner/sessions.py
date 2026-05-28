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
  streaming/async machinery in ``agent_sdk.backends.claude_p``;
  ``subprocess.run`` keeps the code surface tiny and trivially
  testable with a fake CLI on disk.

* **Reuse, don't reinvent.**

  - Atomic JSON I/O comes from
    :mod:`tigerharness.workflow_runner.atomic`.
  - The typed view of ``sessions.json`` is
    :class:`tigerharness.workflow_runner.models.SessionMap`.
  - Persona prompt lookup goes through
    :func:`tigerharness.task_runner.personas.load_prompt`, which
    already encodes the ``TIGERHARNESS_PERSONAS_DIR`` lookup
    convention used everywhere else.

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
TIMEOUT_EXIT_CODE = -1


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

        existing_sid = self.get_session_id(persona)
        argv = self._build_argv(persona, existing_sid)

        try:
            completed = subprocess.run(
                argv,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            result = InvocationResult(
                stdout="",
                session_id=existing_sid or "",
                cost_usd=0.0,
                exit_code=TIMEOUT_EXIT_CODE,
                error=f"timeout after {timeout_sec} seconds",
                raw_envelope={},
            )
            if log_dir is not None:
                self._capture(
                    log_dir,
                    prompt=prompt,
                    envelope={},
                    stdout_text="",
                    timeout_stdout=(exc.stdout or b""),
                )
            return result

        envelope, parse_error = _parse_envelope(completed.stdout)

        # Non-zero exit overrides any extractable session data; we still
        # try to parse the envelope so cost / sid (if present) survive.
        error: Optional[str] = None
        if completed.returncode != 0:
            stderr_excerpt = (completed.stderr or "").strip()
            error = (
                f"claude exited with code {completed.returncode}"
                + (f": {stderr_excerpt}" if stderr_excerpt else "")
            )
        elif parse_error is not None:
            error = parse_error

        # Pull fields from the envelope (defensively).
        envelope_sid = _safe_str(envelope.get("session_id"))
        # Prefer the envelope sid (the CLI may rotate it on resume);
        # fall back to existing on parse failure so we don't lose track.
        session_id = envelope_sid or (existing_sid or "")

        if "total_cost_usd" not in envelope and completed.returncode == 0 and parse_error is None:
            log.warning(
                "claude envelope for persona=%r missing 'total_cost_usd'; "
                "defaulting cost to 0.0",
                persona,
            )
        cost_usd = _safe_float(envelope.get("total_cost_usd"))

        stdout_text = _safe_str(
            envelope.get("result") or envelope.get("structured_output") or ""
        )

        # Persist new sid back if we got one and it changed.
        if envelope_sid and envelope_sid != existing_sid:
            self._persist_sid(persona, envelope_sid)

        result = InvocationResult(
            stdout=stdout_text,
            session_id=session_id,
            cost_usd=cost_usd,
            exit_code=completed.returncode,
            error=error,
            raw_envelope=envelope,
        )

        if log_dir is not None:
            self._capture(
                log_dir,
                prompt=prompt,
                envelope=envelope,
                stdout_text=stdout_text,
            )

        return result

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _build_argv(
        self, persona: str, existing_sid: Optional[str]
    ) -> list[str]:
        binary = os.environ.get("TIGERHARNESS_CLAUDE_BIN", "").strip() or "claude"
        argv: list[str] = [binary, "-p", "--output-format", "json"]
        if existing_sid:
            argv += ["--resume", existing_sid]
        else:
            # Late import: personas.py runs an autoload side-effect at
            # import time that depends on env vars only set in real
            # task-runner contexts. Importing lazily keeps unrelated
            # workflow_runner consumers light.
            from tigerharness.task_runner.personas import load_prompt

            persona_prompt = load_prompt(persona)
            argv += ["--append-system-prompt", persona_prompt]
        return argv

    def _persist_sid(self, persona: str, sid: str) -> None:
        # Re-read under the assumption that this process is the sole
        # writer for this task (the executor's task lock guarantees
        # that); the re-read keeps us correct if other personas in the
        # same task were updated between this invoke's start and end.
        smap = self._load()
        smap.set(persona, sid)
        self._save(smap)

    @staticmethod
    def _capture(
        log_dir: Path,
        *,
        prompt: str,
        envelope: dict[str, Any],
        stdout_text: str,
        timeout_stdout: bytes | str | None = b"",
    ) -> None:
        """Write the three per-iteration log files.

        ``prompt.txt`` — what we sent on stdin (verbatim).
        ``envelope.json`` — the parsed JSON envelope (empty dict on
        timeout / parse failure; the raw text is preserved nowhere
        else, so on timeout we drop any partial stdout into
        ``stdout.txt`` to aid debugging).
        ``stdout.txt`` — the assistant's textual response (the
        ``result`` field of the envelope), or the raw partial output
        on timeout. ``timeout_stdout`` is typed permissively because
        ``TimeoutExpired.stdout`` is bytes on CPython today (the
        timeout fires before stdout is decoded) but the docs only
        promise "whatever was captured", so we defensively handle str
        too.
        """
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        write_json_atomic(log_dir / "envelope.json", envelope)
        if stdout_text:
            (log_dir / "stdout.txt").write_text(stdout_text, encoding="utf-8")
        else:
            partial = _decode_partial(timeout_stdout)
            (log_dir / "stdout.txt").write_text(partial, encoding="utf-8")


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

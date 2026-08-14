"""Transport-health sidecar for :mod:`tigerharness.slack_bridge.notify`.

The notifier is the one subsystem whose failure cannot announce itself --
when its TLS handshake dies, the WARNING it logs goes into a file nobody
reads. So it also writes a tiny JSON sidecar next to the journal, and
``autodrive status`` reads it back: the counter surfaces where the operator
already looks when he asks "why have I seen no heartbeats?".

The data travels writer -> file -> reader precisely because ``slack_bridge``
must not import ``autodrive``. Both halves anchor to the *driven* journal:
the writer via ``default_journal_root()`` (pinned into the daemon's
environment as ``TIGERHARNESS_JOURNAL_DIR`` by ``autodrive``'s
``daemon_env``), the reader via ``autodrive``'s ``_resolve_journal_root``,
which additionally honours an explicit ``--journal-dir``.

Nothing here may raise into the notify path: a notifier that crashes
because it could not record that it failed is worse than the bug this
instruments.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


log = logging.getLogger("tigerharness.slack_bridge.notify_health")


SIDECAR_NAME = ".notify_health.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_atomic(path: Path, payload: dict) -> None:
    """Replace ``path`` atomically -- more than one process posts.

    ``mkstemp`` creates at ``0600`` and ``os.replace`` carries that mode
    onto the target. Writer and reader are the same user by construction
    (the daemon runs as the operator), so this is correct rather than a
    downgrade to work around -- but the file is not world-readable.
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=SIDECAR_NAME, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)


def record_transport(ok: bool, error: str = "") -> None:
    """Update the consecutive-failure counter for one transport attempt.

    ``ok=True`` resets the count; ``ok=False`` increments it. A missing
    journal root is a no-op, and every failure to record is logged and
    swallowed -- never propagated to the caller.
    """
    try:
        from tigerharness.journal.paths import default_journal_root

        root = default_journal_root()
        if not root.is_dir():
            return
        path = root / SIDECAR_NAME
        previous = _read_raw(path)
        if ok:
            # Nothing to reset: stay off disk entirely on a healthy host
            # rather than rewriting the sidecar on every successful post.
            if previous is None or previous[0] == 0:
                return
            _write_atomic(path, {
                "consecutive_failures": 0,
                "last_error": "",
                "updated_at": _utc_now(),
            })
            return
        count = 1 if previous is None else previous[0] + 1
        _write_atomic(path, {
            "consecutive_failures": count,
            "last_error": error,
            "updated_at": _utc_now(),
        })
    except OSError as exc:
        log.warning("notify: could not record transport health (%r)", exc)


def _read_raw(path: Path) -> tuple[int, str, str] | None:
    """``(count, last_error, updated_at)``, or ``None`` when the sidecar is
    absent, unreadable, or not the shape this module writes."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        count = int(data["consecutive_failures"])
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return count, str(data.get("last_error") or ""), str(data.get("updated_at") or "")


def status_lines(journal_root: Path) -> list[str]:
    """Rendered ``autodrive status`` lines for this journal's notify health.

    Empty when the sidecar is absent, corrupt, or reports a healthy
    notifier -- ``status`` stays working and silent in every one of those
    cases. The labels are deliberately not ``last_error:``, which
    ``cmd_status`` already prints for an unrelated field.
    """
    parsed = _read_raw(journal_root / SIDECAR_NAME)
    if parsed is None:
        return []
    count, last_error, updated_at = parsed
    if count <= 0:
        return []
    lines = [f"  notify_failures:   {count} (last {updated_at or 'unknown'})"]
    if last_error:
        lines.append(f"  notify_last_error: {last_error}")
    return lines

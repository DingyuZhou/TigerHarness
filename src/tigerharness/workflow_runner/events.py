"""``events.jsonl`` append-only writer + tail reader.

The event stream is the machine-truth log of every orchestrator
decision: step launches, parsed verdicts, cost deltas, escalations,
errors. The spec calls it out as a dedicated file (separate from
``status.json``) precisely so that an audit / diagnose tool can
replay history without having to interpret the live state file.

Design notes:

* **One JSON object per line.** Strict JSONL -- no comments, no
  multi-line records, no trailing commas. Newline-terminated so a
  partial last line is detectable.
* **fsync per write.** Spec requirement; trades a little throughput
  for the guarantee that a crash mid-task leaves a coherent log.
* **No flock.** Append-mode ``write()`` under POSIX is atomic for
  payloads under ``PIPE_BUF`` bytes (4096 on Linux). Our event
  records are tiny; if we ever produce one larger than 4 KiB we
  should split it. The spec's expected event shapes are well under
  this ceiling.
* **Lock-free reads.** The reader opens the file in text mode and
  iterates -- a concurrent writer's appends are seen on next read.
  Partial last lines are tolerated (silently skipped) so that a tail
  during a write doesn't crash the diagnose CLI.
"""

from __future__ import annotations

import json
import os
from collections import deque
from pathlib import Path
from typing import Any, Iterable

from tigerharness.workflow_runner.models import Event, now_iso


def append_event(
    events_path: Path | str,
    kind: str,
    *,
    ts: str | None = None,
    **fields: Any,
) -> Event:
    """Append a single event to ``events_path``.

    Parameters
    ----------
    events_path:
        Target ``events.jsonl`` file. Created (with parents) if absent.
    kind:
        Event kind label (e.g. ``"step_started"``, ``"task_completed"``).
    ts:
        Override timestamp. Defaults to :func:`now_iso`.
    **fields:
        Arbitrary per-kind payload. Must not collide with ``ts`` or
        ``kind`` (:class:`Event` validates this and raises
        :class:`WorkflowModelError` if it does).

    Returns the constructed :class:`Event` -- handy for logging /
    tests.

    Implementation: open append, write a single JSON line, ``fsync``,
    close. The directory entry update is the only thing visible to a
    concurrent reader; readers tolerate a not-yet-newline-terminated
    line by skipping it.
    """
    evt = Event(
        ts=ts if ts is not None else now_iso(),
        kind=kind,
        extra=dict(fields),
    )
    line = json.dumps(evt.to_dict(), separators=(",", ":")) + "\n"

    p = Path(events_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # ``"a"`` mode -> ``O_APPEND`` -> kernel guarantees the offset is
    # taken under the file's internal lock per ``write()`` call, so
    # concurrent appends never interleave (for payloads < PIPE_BUF,
    # which our events are).
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())
    return evt


def read_events(events_path: Path | str) -> list[Event]:
    """Read and parse every event in ``events_path``.

    Returns an empty list if the file does not exist. Tolerates a
    final line lacking a newline (treats it as a complete record if
    parseable, skips it if not).

    Records that fail JSON parsing are silently skipped so a single
    corrupt line doesn't poison the entire diagnose flow. (In Phase 2+
    we may want to surface these via an `events_corrupt` event count,
    but for now lenient-read keeps the diagnose CLI usable.)
    """
    return list(_iter_events(events_path))


def tail_events(
    events_path: Path | str,
    n: int,
) -> list[Event]:
    """Return the last ``n`` events (oldest-first within the window).

    Streams the file once with a bounded ``deque`` so memory usage is
    ``O(n)`` regardless of file size. ``n`` must be >= 0; ``n == 0``
    yields an empty list.
    """
    if n < 0:
        raise ValueError(f"n must be >= 0, got {n}")
    if n == 0:
        return []
    window: deque[Event] = deque(maxlen=n)
    for evt in _iter_events(events_path):
        window.append(evt)
    return list(window)


def _iter_events(events_path: Path | str) -> Iterable[Event]:
    p = Path(events_path)
    if not p.exists():
        return
    with open(p, "r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                # Partial / corrupt line. Skip; see the lenient-read
                # rationale in :func:`read_events`.
                continue
            if not isinstance(data, dict):
                continue
            try:
                yield Event.from_dict(data)
            except Exception:
                # Any model-level validation failure: skip the row.
                # Better to keep the diagnose tool working on a
                # mostly-intact log than crash on one bad event.
                continue

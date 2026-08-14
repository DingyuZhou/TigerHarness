"""Deferred-task inbox: the cheap Slack-side scheduling rail.

Scheduling from Slack bills API tokens, so the Slack side must do the
MINIMUM: ``journal defer`` copies the Operator's conversation VERBATIM
into the team journal's ``deferred/`` inbox plus a tiny JSON sidecar of
fields the bridge already knows (title, team, requester, thread_ts) --
no playbook read, no compile, no LLM. The expensive part (scaffold +
persona preflight + compile) happens later, on the subscription rail:
``journal materialize`` inside a drive turns an inbox entry into a real
``kind=workflow`` task that is indistinguishable from a ``journal new``
scaffold -- same status.json shape, same gates -- and the drive then
claims it like any other task.

Failure contract (repo exit-code rules): malformed inbox entries fail
at materialization with exit 1 and a JSON envelope on stdout; they
never half-scaffold (the entry is consumed only after the scaffolder
succeeded).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from tigerharness.journal.ids import is_safe_task_id, new_task_id
from tigerharness.journal.paths import JournalPaths

log = logging.getLogger("tigerharness.journal.deferred")


class DeferredError(ValueError):
    """A malformed deferred entry (missing payload, bad sidecar, bad
    id). Maps to exit 1 + envelope at the CLI layer -- content
    failure, not operator error."""


@dataclass(frozen=True)
class DeferredEntry:
    id: str
    title: str
    team: str
    playbook: str
    requester: str
    thread_ts: str
    created_at: str
    path: Path
    payload: str = ""
    # Slack channel the origin thread lives in. Optional: old sidecars
    # predate the field, and a defer may come from outside Slack.
    channel: str = ""
    # Lane routing. "workflow" is the pre-field meaning: every entry
    # written before these keys existed could only produce a workflow,
    # so that is what a sidecar without them still means.
    kind: str = "workflow"
    persona: str = ""


def defer_entry(
    paths: JournalPaths,
    *,
    title: str,
    team: str,
    payload_text: str,
    playbook: str = "default",
    requester: str = "",
    thread_ts: str = "",
    channel: str = "",
    kind: str = "workflow",
    persona: str = "",
) -> DeferredEntry:
    """Write one deferred entry. Deliberately dumb: no playbook read,
    no roster validation -- those run at materialization on the
    subscription rail. Only emptiness is rejected here (an empty
    payload can never materialize, so failing the cheap side fast is
    kinder than parking garbage)."""
    if not payload_text.strip():
        raise DeferredError("payload is empty -- nothing to defer")
    if not title.strip():
        raise DeferredError("title is empty")
    if not team.strip():
        raise DeferredError("team is empty")
    entry_id = new_task_id(title)
    entry_dir = paths.deferred / entry_id
    entry_dir.mkdir(parents=True, exist_ok=False)
    created_at = _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    (entry_dir / "payload.md").write_text(payload_text, encoding="utf-8")
    sidecar = {
        "id": entry_id,
        "title": title,
        "team": team,
        "playbook": playbook,
        "requester": requester,
        "thread_ts": thread_ts,
        "channel": channel,
        "kind": kind,
        "persona": persona,
        "created_at": created_at,
        # Provenance: where this entry was meant to live. Carried
        # forward into the materialized task so a misplaced entry is
        # detectable forever (item 5's provenance design).
        "journal_root": str(paths.root.resolve()),
    }
    (entry_dir / "deferred.json").write_text(
        json.dumps(sidecar, indent=2) + "\n", encoding="utf-8"
    )
    log.info("deferred entry %s written (team=%s)", entry_id, team)
    return DeferredEntry(
        id=entry_id, title=title, team=team, playbook=playbook,
        requester=requester, thread_ts=thread_ts, channel=channel,
        kind=kind, persona=persona,
        created_at=created_at, path=entry_dir,
    )


def read_entry(paths: JournalPaths, entry_id: str) -> DeferredEntry:
    """Load + validate one inbox entry. Every malformation is a
    :class:`DeferredError` naming the file and the problem."""
    if not is_safe_task_id(entry_id):
        raise DeferredError(f"unsafe deferred id {entry_id!r}")
    entry_dir = paths.deferred / entry_id
    sidecar_path = entry_dir / "deferred.json"
    payload_path = entry_dir / "payload.md"
    if not entry_dir.is_dir():
        raise DeferredError(
            f"no deferred entry {entry_id!r} under {paths.deferred}"
        )
    try:
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DeferredError(
            f"{sidecar_path} unreadable: {exc}"
        ) from exc
    except ValueError as exc:
        raise DeferredError(
            f"{sidecar_path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(sidecar, dict):
        raise DeferredError(f"{sidecar_path} must hold a JSON object")
    missing = [
        k for k in ("title", "team") if not str(sidecar.get(k, "")).strip()
    ]
    if missing:
        raise DeferredError(
            f"{sidecar_path} missing required field(s): "
            + ", ".join(missing)
        )
    kind = str(sidecar.get("kind") or "workflow")
    if kind not in ("workflow", "task"):
        raise DeferredError(
            f"{sidecar_path} has kind {kind!r}; expected 'workflow' or "
            "'task'"
        )
    try:
        payload = payload_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DeferredError(
            f"{payload_path} unreadable: {exc}"
        ) from exc
    if not payload.strip():
        raise DeferredError(f"{payload_path} is empty")
    return DeferredEntry(
        id=entry_id,
        title=str(sidecar["title"]),
        team=str(sidecar["team"]),
        playbook=str(sidecar.get("playbook") or "default"),
        requester=str(sidecar.get("requester", "")),
        thread_ts=str(sidecar.get("thread_ts", "")),
        channel=str(sidecar.get("channel", "")),
        kind=kind,
        persona=str(sidecar.get("persona", "")),
        created_at=str(sidecar.get("created_at", "")),
        path=entry_dir,
        payload=payload,
    )


def list_deferred(paths: JournalPaths) -> list[str]:
    """Inbox entry ids, oldest first (ids sort chronologically)."""
    if not paths.deferred.is_dir():
        return []
    return sorted(
        p.name for p in paths.deferred.iterdir()
        if p.is_dir() and is_safe_task_id(p.name)
    )


def consume_entry(entry: DeferredEntry, task_dir: Path) -> None:
    """Archive the inbox entry into the materialized task's dir (audit
    trail) and remove it from the inbox. Called only AFTER the
    scaffolder succeeded -- a failed materialization leaves the inbox
    entry untouched for retry."""
    shutil.copy2(
        entry.path / "deferred.json", task_dir / "deferred_origin.json"
    )
    shutil.rmtree(entry.path)
    log.info("deferred entry %s consumed into %s", entry.id, task_dir)

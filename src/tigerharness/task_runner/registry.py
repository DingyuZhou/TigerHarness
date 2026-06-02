"""Filesystem-backed job registry under XDG state.

Layout (`~/.local/state/tigerharness-tasks/`):

    jobs.json                Registry: { job_id: JobMeta-as-dict }
    <job_id>/
        prompt.txt           The original task prompt (full text)
        state.json           (reserved -- currently unused; meta is in jobs.json)
        run.log              Newline-delimited JSON, one entry per turn/event
        result.txt           Latest iteration's final_output (overwritten each turn)
        cancel               If exists, the runner exits at the next iter
                             boundary. Content is the unix-ts of the request.
        stdout.log
        stderr.log           Captured from the detached child.

Atomic writes
-------------
`jobs.json` is rewritten on every state change (tmp file + rename). The
per-job files (`run.log`, `result.txt`) are append/overwrite -- no
attempt at atomicity because they're owned by exactly one writer (the
job's runner process).

Concurrency
-----------
Multiple processes may read+write `jobs.json`: the CLI on `assign` /
`cancel`, the runner child on every iteration. There's no lock -- race
conditions can lose at most one update (last writer wins). For the
expected usage (few concurrent jobs, infrequent meta changes), this
is fine. If you want strict serialisability later, wrap reads in
`fcntl.flock`.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# State directory
# ---------------------------------------------------------------------------

_STATE_DIR_NAME = "tigerharness-tasks"


def default_state_path() -> Path:
    """`$TIGERHARNESS_STATE_DIR` or `$XDG_STATE_HOME/tigerharness-tasks`
    or `~/.local/state/tigerharness-tasks`."""
    override = os.environ.get("TIGERHARNESS_STATE_DIR", "").strip()
    if override:
        return Path(override)
    base = os.environ.get("XDG_STATE_HOME") or str(
        Path.home() / ".local" / "state"
    )
    return Path(base) / _STATE_DIR_NAME


@dataclass
class JobMeta:
    job_id: str
    persona: str
    prompt_chars: int
    max_iters: int  # 0 = forever (the runner caps at HARD_CEIL)
    compact_every: int  # 0 = never
    continuation: str
    name: str
    cwd: str
    started_at: float
    status: str  # pending | running | cancelled | done | error
    pid: int | None
    current_iter: int
    session_id: str
    last_update: float
    error: str | None = None
    total_cost_usd: float | None = None
    notify: bool = True  # False -> skip Slack DM on job end (--quiet)
    slack_thread_ts: str = ""  # If set, completion DM replies in this thread
    early_exit: bool = False  # If True, enable early-exit on consecutive stale iterations
    # Per-iteration stuck-watchdog timeout in seconds. 0 disables.
    # Default 1200 (20 min): if a turn runs longer, the runner samples
    # claude's process tree, decides STUCK/WORKING/UNCLEAR via heuristic
    # (+ optional agent fallback), and on STUCK SIGTERMs the iteration's
    # claude. The job auto-continues with the next iter unless this was
    # the final iteration. See stuck_watchdog.py for full details.
    stuck_timeout: int = 1200
    # Absolute path to a git repo. When set, the runner creates an
    # isolated worktree at ``<worktree_repo>/.worktrees/<job_id>/`` on
    # startup, tells the persona to ``cd`` into it for git operations,
    # and removes the worktree on job exit (best-effort). Lets multiple
    # background jobs operate on the same project concurrently without
    # racing on HEAD/index/working tree. Empty = legacy behaviour
    # (no worktree, persona shares whatever tree the cwd resolves to).
    worktree_repo: str = ""


def new_job_id() -> str:
    """8 hex chars. Collision-rare, easy to type, prefix-matchable."""
    return secrets.token_hex(4)


class JobStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.registry = root / "jobs.json"
        root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # Registry-level ops
    # ------------------------------------------------------------------ #

    def all(self) -> dict[str, JobMeta]:
        if not self.registry.exists():
            return {}
        try:
            raw = json.loads(self.registry.read_text() or "{}")
        except json.JSONDecodeError:
            # Corrupt file: behave like an empty registry rather than
            # crashing the CLI. The next write replaces it.
            return {}
        out: dict[str, JobMeta] = {}
        for job_id, meta in raw.items():
            try:
                out[job_id] = JobMeta(**meta)
            except TypeError:
                # Forward-compat: silently skip rows from a future schema.
                continue
        return out

    def get(self, job_id: str) -> JobMeta | None:
        return self.all().get(job_id)

    def set(self, meta: JobMeta) -> None:
        all_ = self.all()
        all_[meta.job_id] = meta
        self._write_atomic({jid: asdict(m) for jid, m in all_.items()})

    def delete(self, job_id: str) -> None:
        all_ = self.all()
        if job_id in all_:
            del all_[job_id]
            self._write_atomic({jid: asdict(m) for jid, m in all_.items()})

    def _write_atomic(self, data: dict[str, dict[str, Any]]) -> None:
        text = json.dumps(data, indent=2) + "\n"
        # NamedTemporaryFile in the same dir guarantees the rename is
        # atomic on the same filesystem.
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            dir=str(self.root),
            prefix=".jobs.json.",
            suffix=".tmp",
            delete=False,
        )
        try:
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.replace(tmp.name, self.registry)
        finally:
            try:
                os.unlink(tmp.name)
            except FileNotFoundError:
                pass  # replaced successfully

    # ------------------------------------------------------------------ #
    # Per-job paths
    # ------------------------------------------------------------------ #

    def job_dir(self, job_id: str) -> Path:
        d = self.root / job_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def cancel_flag(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "cancel"

    def run_log(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "run.log"

    def result_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "result.txt"

    def prompt_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "prompt.txt"

    # ------------------------------------------------------------------ #
    # Cancel
    # ------------------------------------------------------------------ #

    def request_cancel(self, job_id: str) -> None:
        self.cancel_flag(job_id).write_text(f"{time.time()}\n")

    def is_cancel_requested(self, job_id: str) -> bool:
        return self.cancel_flag(job_id).exists()

    # ------------------------------------------------------------------ #
    # Prefix resolution (like git short SHAs)
    # ------------------------------------------------------------------ #

    def resolve_prefix(self, prefix: str) -> JobMeta:
        prefix = prefix.strip().lower()
        all_ = self.all()
        if prefix in all_:
            return all_[prefix]
        hits = [jid for jid in all_ if jid.startswith(prefix)]
        if not hits:
            raise KeyError(f"no job matches prefix {prefix!r}")
        if len(hits) > 1:
            raise KeyError(
                f"ambiguous prefix {prefix!r}: matches {sorted(hits)}"
            )
        return all_[hits[0]]

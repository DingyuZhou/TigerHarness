"""tigerharness.workflow_runner -- multi-persona workflow orchestration.

Phase 1 sub-step #1: the foundation module. Provides:

* Path-layout helpers for the per-task journal
  (`paths.TaskPaths`, `paths.default_journal_root`, `paths.new_task_id`).
* Step-id sanitizer shared by models + paths
  (`ids.validate_step_id`, `ids.STEP_ID_PATTERN`).
* Typed data models for the on-disk JSON files
  (`models.StepFrontmatter`, `models.Orchestration`, `models.Status`,
  `models.SessionMap`, `models.Event`, ...).
* Atomic JSON read / write primitives with a POSIX-flock-based
  serialisation context manager (`atomic.read_json`,
  `atomic.write_json_atomic`, `atomic.flocked`).
* Task-level lock + pid + heartbeat primitives
  (`locks.acquire_task_lock`, `locks.write_pid`, `locks.heartbeat`,
  `locks.read_pid_info`, `locks.is_stale`).
* `events.jsonl` append-only writer + tail reader
  (`events.append_event`, `events.read_events`, `events.tail_events`).

The compile phase, executor, trailer parser, session manager, and CLI
land in later sub-steps; this module is the surface they all sit on.

See ``docs/workflow-runner.md`` for the full design and
``docs/adr/0001-workflow-runner.md`` for the decision log.
"""

from tigerharness.workflow_runner.atomic import (
    flocked,
    read_json,
    write_json_atomic,
)
from tigerharness.workflow_runner.events import (
    append_event,
    read_events,
    tail_events,
)
from tigerharness.workflow_runner.ids import (
    STEP_ID_PATTERN,
    validate_step_id,
)
from tigerharness.workflow_runner.locks import (
    LockHeldError,
    PidInfo,
    acquire_task_lock,
    heartbeat,
    is_stale,
    read_pid_info,
    write_pid,
)
from tigerharness.workflow_runner.models import (
    Event,
    Orchestration,
    SessionMap,
    Status,
    StepEdges,
    StepFrontmatter,
    StepHistoryEntry,
    WorkflowConfig,
    WorkflowModelError,
)
from tigerharness.workflow_runner.paths import (
    TaskPaths,
    default_journal_root,
    new_task_id,
)

# TODO(anzai): re-export trailer types once Mitsui's parser branch
# merges (deferred per Akagi review / Anzai adjudication).

__all__ = [
    # paths
    "TaskPaths",
    "default_journal_root",
    "new_task_id",
    # ids
    "STEP_ID_PATTERN",
    "validate_step_id",
    # models
    "Event",
    "Orchestration",
    "SessionMap",
    "Status",
    "StepEdges",
    "StepFrontmatter",
    "StepHistoryEntry",
    "WorkflowConfig",
    "WorkflowModelError",
    # atomic
    "flocked",
    "read_json",
    "write_json_atomic",
    # locks
    "LockHeldError",
    "PidInfo",
    "acquire_task_lock",
    "heartbeat",
    "is_stale",
    "read_pid_info",
    "write_pid",
    # events
    "append_event",
    "read_events",
    "tail_events",
]

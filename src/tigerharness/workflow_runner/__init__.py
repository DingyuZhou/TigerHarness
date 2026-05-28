"""tigerharness.workflow_runner -- multi-persona workflow orchestration.

Phase 1 sub-steps shipped so far provide:

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
* Per-persona Claude session manager
  (`sessions.SessionManager`, `sessions.InvocationResult`,
  `sessions.TIMEOUT_EXIT_CODE`).
* Trailer parser + verdict ADT
  (`trailer.parse_trailer`, `trailer.Verdict`, `trailer.Approve`,
  `trailer.Revise`, `trailer.Block`, `trailer.ParseError`).
* Basic CLI entrypoints
  (`cli.main`, `cli.build_parser`).

The compile phase and the sequential executor land in later
sub-steps; this module is the surface they all sit on.

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
from tigerharness.workflow_runner.sessions import (
    TIMEOUT_EXIT_CODE,
    InvocationResult,
    SessionManager,
)
from tigerharness.workflow_runner.trailer import (
    Approve,
    Block,
    ParseError,
    Revise,
    Verdict,
    parse_trailer,
)
from tigerharness.workflow_runner.cli import (
    build_parser,
    main,
)

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
    # sessions
    "InvocationResult",
    "SessionManager",
    "TIMEOUT_EXIT_CODE",
    # trailer
    "Approve",
    "Block",
    "ParseError",
    "Revise",
    "Verdict",
    "parse_trailer",
    # cli
    "build_parser",
    "main",
]

"""Bridge-layer idle compaction (ADR 0004 implementation).

After a completed turn, when the team has opted in AND the journal
queue is genuinely idle AND the just-finished turn's usage puts the
session's context over the configured fraction, the bridge sends one
``/compact`` turn to the same session — the mechanism ADR 0004
proved against Claude Code 2.1.140.

Hard rules (the task's constraints, enforced here):

- **Off by default.** The feature acts on live sessions; enabling is
  one env var (opt-in is the conservative ship).
- **Explicit journal root or nothing.** The idle check sweeps the
  journal named by config; an absent/invalid root DISABLES the check
  rather than guessing — a guessed (empty) root would read
  idle=true and compact while real work runs elsewhere.
- **Never mid-task.** The idle predicate (nothing pending, nothing
  in progress) is the guard, and the hook runs only at the bridge's
  turn boundary.
- **One per idle period.** The caller carries a per-thread flag:
  set after a compact, cleared by the next real turn.
- **Fail-soft.** Any error — malformed usage, sweep failure, a CLI
  that stops honoring ``/compact`` — logs and skips. A broken
  compact must never break a turn.

Env surface (documented in docs/slack-bridge.md, the single home):

- ``TIGERHARNESS_IDLE_COMPACT``: ``1``/``true`` to enable.
- ``TIGERHARNESS_IDLE_COMPACT_JOURNAL``: the journal root path
  (REQUIRED for the feature to do anything).
- ``TIGERHARNESS_IDLE_COMPACT_THRESHOLD``: fraction, default 0.30.
- ``TIGERHARNESS_IDLE_COMPACT_WINDOW``: tokens, default 200000.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .persistence import ThreadStore, default_state_path

log = logging.getLogger("tigerharness.slack_bridge.idle_compact")

_DEFAULT_THRESHOLD = 0.30
_DEFAULT_WINDOW = 200_000
_DEFAULT_MIN_QUIET_SECONDS = 120
#: Hard ceiling on a single /compact turn -- a wedged claude subprocess
#: must never hang the calling drive indefinitely.
_DEFAULT_COMPACT_TIMEOUT_SECONDS = 600


@dataclass(frozen=True)
class IdleCompactConfig:
    enabled: bool = False
    journal_root: Path | None = None
    threshold_fraction: float = _DEFAULT_THRESHOLD
    context_window_tokens: int = _DEFAULT_WINDOW

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "IdleCompactConfig":
        """Parse the env surface. Bad values disable the feature with
        a warning — config mistakes must never crash the bridge."""
        e = os.environ if env is None else env
        raw_enabled = e.get("TIGERHARNESS_IDLE_COMPACT", "").strip().lower()
        if raw_enabled not in ("1", "true", "yes", "on"):
            return cls()  # disabled; nothing else matters
        raw_root = e.get("TIGERHARNESS_IDLE_COMPACT_JOURNAL", "").strip()
        if not raw_root:
            log.warning(
                "idle-compact enabled but TIGERHARNESS_IDLE_COMPACT_"
                "JOURNAL is unset; feature disabled (explicit root or "
                "nothing -- a guessed root could compact during real "
                "work)"
            )
            return cls()
        root = Path(raw_root)
        try:
            threshold = float(e.get(
                "TIGERHARNESS_IDLE_COMPACT_THRESHOLD",
                str(_DEFAULT_THRESHOLD),
            ))
            window = int(e.get(
                "TIGERHARNESS_IDLE_COMPACT_WINDOW", str(_DEFAULT_WINDOW),
            ))
        except ValueError as exc:
            log.warning(
                "idle-compact config invalid (%s); feature disabled", exc,
            )
            return cls()
        return cls._enabled_or_disabled(
            journal_root=root, threshold=threshold, window=window, source="",
        )

    @classmethod
    def _enabled_or_disabled(
        cls,
        *,
        journal_root: Path,
        threshold: float,
        window: int,
        source: str,
    ) -> "IdleCompactConfig":
        """Shared tail for :meth:`from_env` and :meth:`for_lane`: apply
        the journal-exists and range guards, returning the ENABLED config
        only when both pass and a DISABLED one (with a warning) otherwise.
        Fail-soft by construction -- a missing journal or a nonsense
        threshold never raises, it just turns the feature off. ``source``
        is appended to the warning so logs say which path (env vs a named
        lane) tripped."""
        if not (journal_root / "active").is_dir():
            log.warning(
                "idle-compact journal root %s has no active/ dir; "
                "feature disabled%s", journal_root, source,
            )
            return cls()
        if not (0.0 < threshold < 1.0) or window <= 0:
            log.warning(
                "idle-compact config invalid (threshold=%s window=%s); "
                "feature disabled%s", threshold, window, source,
            )
            return cls()
        return cls(
            enabled=True,
            journal_root=journal_root,
            threshold_fraction=threshold,
            context_window_tokens=window,
        )

    @classmethod
    def for_lane(
        cls,
        *,
        enabled: bool,
        journal_root: Path,
        threshold_fraction: float = _DEFAULT_THRESHOLD,
        context_window_tokens: int = _DEFAULT_WINDOW,
        where: str = "",
    ) -> "IdleCompactConfig":
        """Build a per-lane config for multi-team mode.

        Why this exists alongside :meth:`from_env`: one bridge process
        serves many lanes, but ``from_env`` reads the single process-wide
        ``os.environ`` and so can describe only ONE journal. Multi-team
        config therefore comes per-lane from each team's
        ``slack-bridge.yaml`` fragment; the caller passes the resolved
        on/off flag plus the journal root it auto-resolves to
        ``<team>/journal`` (so an operator never hand-writes a path).
        Same fail-soft guards as ``from_env``: a disabled flag, a journal
        without ``active/``, or an out-of-range threshold/window all yield
        a disabled config -- this never raises, so one lane's bad config
        can never abort the whole multi-lane bridge."""
        if not enabled:
            return cls()
        return cls._enabled_or_disabled(
            journal_root=journal_root,
            threshold=threshold_fraction,
            window=context_window_tokens,
            source=f" ({where})" if where else "",
        )


def context_fraction(usage: dict[str, Any] | None, window_tokens: int) -> float:
    """Approximate context load from a turn's usage payload (ADR 0004
    accounting: input + cache_creation + cache_read over the window).
    Missing or malformed usage reads as 0.0 — it can never trigger."""
    if not isinstance(usage, dict) or window_tokens <= 0:
        return 0.0
    total = 0
    for key in (
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        value = usage.get(key, 0)
        if isinstance(value, (int, float)) and value > 0:
            total += int(value)
    return total / window_tokens


def journal_is_idle(journal_root: Path) -> bool:
    """True iff the journal has nothing actionable and nothing
    running: no pending, no in-progress (busy, idle, or crashed), no
    blocked-with-attached-session. Failure to read = NOT idle
    (fail-soft: when unsure, don't compact)."""
    try:
        from tigerharness.journal.paths import JournalPaths
        from tigerharness.journal.sweep import sweep

        result = sweep(JournalPaths(root=journal_root))
        # A blocked task with a session still attached counts as NOT
        # idle (conservative: someone may be mid-escalation on it) --
        # the code now matches this docstring's promise.
        blocked_attached = any(
            s.session_ref for s in result.blocked
        )
        return not (
            result.pending
            or result.in_progress_idle
            or result.in_progress_busy
            or result.in_progress_crashed
            or blocked_attached
        )
    except Exception:  # noqa: BLE001 -- never break the turn
        log.exception("idle-compact: sweep failed; treating as busy")
        return False


def should_compact(
    cfg: IdleCompactConfig,
    usage: dict[str, Any] | None,
    *,
    already_compacted: bool,
) -> bool:
    """The trigger predicate, cheap checks first. The journal sweep
    runs only when everything else already says yes."""
    if not cfg.enabled or cfg.journal_root is None or already_compacted:
        return False
    fraction = context_fraction(usage, cfg.context_window_tokens)
    if fraction < cfg.threshold_fraction:
        return False
    return journal_is_idle(cfg.journal_root)


def _parse_iso(value: str | None) -> "datetime | None":
    """Tolerant ISO-8601 parse; bad/missing input reads as None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


async def compact_idle_once(
    team_dir: Path,
    *,
    min_quiet_seconds: int = _DEFAULT_MIN_QUIET_SECONDS,
    send: Any = None,
    now: "datetime | None" = None,
    compact_timeout_seconds: float = _DEFAULT_COMPACT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """The external, run-once idle-compaction pass (``slack-bridge
    compact-idle``).

    The bridge's own hook (:func:`maybe_compact`) only fires at a lane
    turn boundary, so a lane left heavy while the journal was busy stays
    heavy until its next Slack turn. This pass closes that gap from the
    *driver* side: an idle drive (autodrive tick or a manual
    drive-journal) calls it after the queue drains. It is model-free
    orchestration -- the single ``/compact`` turn it may send per lane is
    the same ADR 0004 mechanism the bridge uses.

    Safety gates, in order, all fail-soft (skip, never raise):

    - team fragment must exist and set ``idle_compact: true``
      (:func:`IdleCompactConfig.for_lane` -- identical config to the
      bridge's own hook);
    - a record must carry this team's name (stamped by the bridge at its
      turn boundary; older records are invisible and skipped);
    - ``in_flight`` records are skipped (a live bridge turn owns the
      session);
    - records whose last turn is younger than ``min_quiet_seconds`` are
      skipped (extra margin against racing a turn that is just landing);
    - the stamped usage must put the session over the threshold;
    - the journal must be idle, and every gate is RE-CHECKED against
      fresh disk state immediately before each (slow) ``/compact`` send
      -- a lane that went active mid-pass is skipped (``went_active``);
    - one pass per team at a time (non-blocking flock lease; a
      concurrent invocation exits with reason ``busy``);
    - each ``/compact`` turn is bounded by ``compact_timeout_seconds``
      (default 600) so a wedged subprocess cannot hang the drive;
    - one compact per idle period: a compacted record's ``last_usage``
      is cleared (only if the session id is unchanged), so the pass
      cannot re-fire until a real turn restamps it.

    ``send`` is an async callable ``(session_id) -> None`` injected by
    tests; the default resolves the ``claude_p`` backend pinned to the
    lane's agent_cwd (from the fragment), because ``--resume`` only
    finds a session from the same project directory the bridge opened
    it under -- so the pass itself may be invoked from anywhere.
    """
    report: dict[str, Any] = {
        "ran": False,
        "team": team_dir.name,
        "checked": 0,
        "compacted": [],
        "skipped": {},
    }

    def _skip(reason: str, count: int = 1) -> None:
        report["skipped"][reason] = report["skipped"].get(reason, 0) + count

    fragment = team_dir / "configs" / "slack-bridge.yaml"
    if not fragment.exists():
        report["reason"] = "no_fragment"
        return report
    try:
        from .multi import _build_idle_compact, _load_yaml, _resolve

        spec = _load_yaml(fragment)
        cfg = _build_idle_compact(
            spec, team_dir, f"compact-idle ({fragment})"
        )
    except Exception:  # noqa: BLE001 -- fail-soft, like the bridge hook
        log.exception("compact-idle: could not load %s; skipping", fragment)
        report["reason"] = "bad_fragment"
        return report
    if not cfg.enabled or cfg.journal_root is None:
        report["reason"] = "disabled"
        return report

    state_dir_raw = str(spec.get("state_dir") or "").strip()
    state_path = (
        (_resolve(state_dir_raw, team_dir) / "threads.json").resolve()
        if state_dir_raw
        else default_state_path()
    )
    # One pass per team at a time: a manual drive and an autodrive tick
    # can both hit the idle tail; without a lease they would each compact
    # every candidate. Non-blocking flock -- the loser exits "busy".
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lease_path = state_path.with_name("compact-idle.lock")
    lease = open(lease_path, "w", encoding="utf-8")
    try:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            report["reason"] = "busy"
            return report

        return await _scan_and_compact(
            report, _skip,
            team_dir=team_dir, spec=spec, cfg=cfg, state_path=state_path,
            min_quiet_seconds=min_quiet_seconds, send=send, now=now,
            compact_timeout_seconds=compact_timeout_seconds,
            _resolve=_resolve,
        )
    finally:
        lease.close()


def _eligible(
    rec: Any, team_name: str, moment: "datetime", min_quiet_seconds: int,
    cfg: IdleCompactConfig, _skip: Any,
) -> bool:
    """The per-record cheap gates, shared by the scan and the pre-send
    recheck. Increments a skip reason and returns False when any fails."""
    if rec.team != team_name:
        _skip("other_team")
        return False
    if rec.in_flight:
        _skip("in_flight")
        return False
    last_turn = _parse_iso(rec.last_turn_at)
    if last_turn is None:
        _skip("no_turn_stamp")
        return False
    if last_turn.tzinfo is None:
        last_turn = last_turn.replace(tzinfo=timezone.utc)
    if (moment - last_turn).total_seconds() < min_quiet_seconds:
        _skip("too_recent")
        return False
    fraction = context_fraction(rec.last_usage, cfg.context_window_tokens)
    if fraction < cfg.threshold_fraction:
        _skip("below_threshold")
        return False
    return True


async def _scan_and_compact(
    report: dict[str, Any], _skip: Any, *,
    team_dir: Path, spec: dict, cfg: IdleCompactConfig, state_path: Path,
    min_quiet_seconds: int, send: Any, now: "datetime | None",
    compact_timeout_seconds: float, _resolve: Any,
) -> dict[str, Any]:
    """The lease-holding body of :func:`compact_idle_once`."""
    store = ThreadStore(state_path)
    # The lane's sessions were opened by a `claude` subprocess running in
    # the lane's agent_cwd (bridge builds its backend with that cwd), and
    # `--resume` only finds a session from the same project directory.
    # Resolve it the same way the lane loader does, so this pass works
    # regardless of where the CLI itself was invoked.
    agent_cwd = str(_resolve(str(spec.get("agent_cwd") or "."), team_dir))

    moment = now if now is not None else datetime.now(timezone.utc)
    candidates: list[tuple[str, Any]] = []
    for thread_ts, rec in sorted(store.records().items()):
        report["checked"] += 1
        if _eligible(rec, team_dir.name, moment, min_quiet_seconds, cfg, _skip):
            candidates.append((thread_ts, rec))

    report["ran"] = True
    if not candidates:
        return report

    if not journal_is_idle(cfg.journal_root):
        report["reason"] = "journal_busy"
        _skip("journal_busy", len(candidates))
        return report

    if send is None:
        send = _default_send(
            cwd=agent_cwd, timeout_seconds=compact_timeout_seconds,
        )

    for index, (thread_ts, rec) in enumerate(candidates):
        # A /compact turn can take minutes, so the scan-time gates are
        # stale by the time later candidates send. Re-check everything
        # against fresh disk state immediately before each send: the
        # record (a turn may be running or just landed; the session id
        # may have changed) and, after the first send, the journal.
        if index > 0 and not journal_is_idle(cfg.journal_root):
            report["reason"] = "journal_busy"
            _skip("journal_busy", len(candidates) - index)
            break
        fresh = ThreadStore(state_path).get_record(thread_ts)
        if (
            fresh is None
            or fresh.session_id != rec.session_id
            or not _eligible(
                fresh, team_dir.name,
                datetime.now(timezone.utc) if now is None else now,
                min_quiet_seconds, cfg, lambda _r: None,
            )
        ):
            _skip("went_active")
            continue
        try:
            await send(fresh.session_id)
        except Exception:  # noqa: BLE001 -- one bad lane never stops the pass
            log.exception(
                "compact-idle: /compact failed for thread=%s; skipping",
                thread_ts,
            )
            _skip("send_failed")
            continue
        # One-per-idle-period latch: clear the stamped usage so only a
        # real future turn can make this lane eligible again. Written
        # through a fresh store (lock-protected read-merge-patch), and
        # only if the session id is still the one we compacted -- a turn
        # that landed during the send restamped everything and owns the
        # record now.
        try:
            latch = ThreadStore(state_path)
            cur = latch.get_record(thread_ts)
            if cur is not None and cur.session_id == fresh.session_id:
                latch.set(thread_ts, cur.session_id, last_usage=None)
        except Exception:  # noqa: BLE001 -- a failed latch must not kill the pass
            log.exception(
                "compact-idle: latch write failed for thread=%s", thread_ts,
            )
            _skip("latch_failed")
        report["compacted"].append(thread_ts)
        log.info(
            "compact-idle: compacted thread=%s (team=%s)",
            thread_ts, team_dir.name,
        )
    return report


def _default_send(
    *,
    cwd: str | None = None,
    timeout_seconds: float = _DEFAULT_COMPACT_TIMEOUT_SECONDS,
) -> Any:
    """Build the real ``/compact`` sender over the ``claude_p`` backend
    (the ADR 0004 mechanism: one prompt turn on a resumed session).

    ``cwd`` must be the lane's agent_cwd: the claude CLI resolves
    ``--resume`` against the project directory derived from the
    subprocess cwd, so it has to match where the bridge opened the
    session. The run is bounded by ``timeout_seconds``, and a non-clean
    ``stop_reason`` RAISES: claude_p reports a failed CLI (e.g. "No
    conversation found" from a stale session id) as an error *result*,
    not an exception -- treating that as success would clear the latch
    on a lane that was never compacted."""
    from tigerharness.agent_sdk import AgentConfig, get_backend

    backend = get_backend("claude_p", cwd=cwd)
    agent_cfg = AgentConfig(
        name="compact-idle",
        extra={"permission_mode": "bypassPermissions"},
    )

    async def send(session_id: str) -> None:
        session = await backend.open_session(resume_id=session_id)
        try:
            result = await asyncio.wait_for(
                backend.run(agent_cfg, "/compact", session=session),
                timeout=timeout_seconds,
            )
            stop = getattr(result, "stop_reason", None)
            if stop not in (None, "end_turn"):
                raise RuntimeError(
                    f"/compact did not complete cleanly "
                    f"(stop_reason={stop!r})"
                )
        finally:
            await session.close()

    return send


def main(argv: "list[str] | None" = None) -> int:
    """``tigerharness slack-bridge compact-idle`` -- run one external
    idle-compaction pass for the team rooted at ``--team-dir`` (default:
    the current directory) and print the JSON report."""
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(
        prog="tigerharness slack-bridge compact-idle",
        description=(
            "Compact heavy, quiet Slack bridge lanes once, if the team "
            "opted in (idle_compact: true) and the journal is idle. "
            "Model-free except the single /compact turn per eligible "
            "lane. Safe to run repeatedly; every gate is a cheap no-op."
        ),
    )
    parser.add_argument(
        "--team-dir",
        default=".",
        help="team root (contains configs/slack-bridge.yaml); default cwd",
    )
    parser.add_argument(
        "--min-quiet-seconds",
        type=int,
        default=_DEFAULT_MIN_QUIET_SECONDS,
        help=(
            "skip lanes whose last turn finished more recently than this "
            f"(default {_DEFAULT_MIN_QUIET_SECONDS}s)"
        ),
    )
    parser.add_argument(
        "--compact-timeout-seconds",
        type=float,
        default=_DEFAULT_COMPACT_TIMEOUT_SECONDS,
        help=(
            "hard ceiling on a single /compact turn "
            f"(default {_DEFAULT_COMPACT_TIMEOUT_SECONDS}s)"
        ),
    )
    args = parser.parse_args(argv)

    report = asyncio.run(
        compact_idle_once(
            Path(args.team_dir).resolve(),
            min_quiet_seconds=args.min_quiet_seconds,
            compact_timeout_seconds=args.compact_timeout_seconds,
        )
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


async def maybe_compact(
    send_turn: Any,
    cfg: IdleCompactConfig,
    usage: dict[str, Any] | None,
    *,
    already_compacted: bool,
    label: str = "",
) -> bool:
    """Send one ``/compact`` turn when the predicate fires. Returns
    True iff a compact was sent. NEVER raises.

    ``send_turn`` is an async callable taking the prompt string --
    the bridge passes a closure over its own backend/session pair
    (Session is an opaque handle; turns always go through the
    backend's run path)."""
    try:
        if not should_compact(cfg, usage, already_compacted=already_compacted):
            return False
        log.info("idle-compact: compacting %s (fraction over %.2f, "
                 "journal idle)", label, cfg.threshold_fraction)
        await send_turn("/compact")
        return True
    except Exception:  # noqa: BLE001 -- fail-soft, per ADR 0004
        log.exception(
            "idle-compact: compact turn failed for %s; skipping "
            "(future-CLI fail-soft)", label,
        )
        return False

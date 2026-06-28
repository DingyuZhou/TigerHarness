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

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("tigerharness.slack_bridge.idle_compact")

_DEFAULT_THRESHOLD = 0.30
_DEFAULT_WINDOW = 200_000


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

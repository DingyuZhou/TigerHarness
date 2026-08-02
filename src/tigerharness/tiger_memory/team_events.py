"""Team-wide event log (ADR 0008) — a lazy, self-compacting activity ledger.

One file per team, beside the per-persona stores:

    <team>/memories/team/events.md

The file IS the durable store: one ``## <period>`` section per period,
newest first, each holding ``- <Persona> <did thing>.`` bullets. Period
granularity encodes the compaction tier — ``YYYY-MM-DD`` (raw daily
entries), ``YYYY-MM`` (a compacted month), ``YYYY`` (a compacted year).
Exact repeats within a day collapse to a ``(xN)`` count suffix.

Write path: the sweep's ``ingest-staged`` glue appends each session's
``@@TEAM_EVENTS@@`` lines under the session's end date (the persona name
is prefixed by harness code — attribution is structural, never guessed).
Appends from different personas' ingest processes may race, so every
mutation runs under an O_EXCL lock with an atomic replace.

Read path: **lazy only.** No briefing loads this file; each persona's
briefing README carries a pointer, opened when a session actually needs
cross-team awareness.

Compaction (age-tiered, staged like ADR 0007):

- :func:`compact_plan` (non-AI) first runs the deterministic size
  backstop (over ``overflow_limit`` → drop oldest year, then month,
  sections until back under ``max`` — daily sections are never
  backstop-dropped), then stages one prompt per aged-out period fold:
  a month whose end is older than ``recent_days`` (fold its day
  sections), a year whose end is older than ``year_after_days`` (fold
  its month sections).
- Task sub-agents (subscription rail) write one ``<key>.card.md`` each,
  per the prompt's strict ``@@TEAM_EVENTS@@`` contract.
- :func:`compact_apply` (non-AI) validates each card, replaces the
  source sections atomically (bullets appended between plan and apply
  survive — snapshot semantics), and hard-trims an oversized card
  deterministically, so convergence never depends on the model.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import re
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Iterator

from .config import Config
from .state import iso_now
from .store import reclaim_lockfile, release_lockfile

log = logging.getLogger("tigerharness.tiger_memory.team_events")

TEAM_DIR_NAME = "team"
EVENTS_FILENAME = "events.md"
LOCK_FILENAME = ".events.lock"
STAGING_DIR_NAME = ".compact-staging"
CARD_SUFFIX = ".card.md"

MARK_TEAM_EVENTS = "@@TEAM_EVENTS@@"

# Hard cap on EVENT lines accepted from one extraction card — the contract
# asks for 0-3; enforcing it here keeps the day window (which the size
# backstop deliberately never touches) bounded by real activity.
MAX_EVENTS_PER_APPEND = 3

KIND_DAY = "day"
KIND_MONTH = "month"
KIND_YEAR = "year"

_HEADER = """\
# Team event log

What each team member did, by date — newest first. Sections older than
the daily window are compacted to month, then year, summaries. Generated
by tiger-memory (ADR 0008); never hand-edit.
"""

_PERIOD_RE = re.compile(r"^## (\d{4}(?:-\d{2}(?:-\d{2})?)?)\s*$")
_COUNT_RE = re.compile(r"^(?P<body>- .*?)(?: \(x(?P<n>\d+)\))?$")


class TeamEventsError(ValueError):
    """A team-events card or state file didn't satisfy its contract."""


class TeamEventsLockHeld(RuntimeError):
    """Another live process holds the team event log lock."""


@dataclass
class PeriodSection:
    """One ``## <period>`` section: the period string + its bullet lines."""

    period: str
    lines: list[str] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return {10: KIND_DAY, 7: KIND_MONTH, 4: KIND_YEAR}[len(self.period)]


# ----- paths ----------------------------------------------------------------


def team_dir(cfg: Config) -> Path:
    """``<team>/memories/team/`` — the team-scoped log dir (beside the
    per-persona stores, like the sweep-state file)."""
    return cfg.store.root.parent / TEAM_DIR_NAME


def events_path(cfg: Config) -> Path:
    return team_dir(cfg) / EVENTS_FILENAME


def _staging_dir(cfg: Config) -> Path:
    return team_dir(cfg) / STAGING_DIR_NAME


# ----- load / render (the file is the store) --------------------------------


def load_sections(path: Path) -> list[PeriodSection]:
    """Parse ``events.md`` into period sections (file order, tolerant).

    Anything before the first ``## <period>`` heading (the generated
    header) is dropped — it is re-rendered on save. Non-bullet lines
    inside a section are preserved as-is (no silent loss on save).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    sections: list[PeriodSection] = []
    current: PeriodSection | None = None
    for raw in text.split("\n"):
        m = _PERIOD_RE.match(raw)
        if m:
            current = PeriodSection(period=m.group(1))
            sections.append(current)
            continue
        if current is not None and raw.strip():
            current.lines.append(raw.rstrip())
    return sections


def render(sections: list[PeriodSection]) -> str:
    """Render the full file: header + non-empty sections, newest first.

    Period strings order lexicographically across granularities (a day
    ``2026-07-15`` sorts above its month ``2026-07``, a month above its
    year), so one descending sort yields the read order.
    """
    ordered = sorted(
        (s for s in sections if s.lines),
        key=lambda s: s.period,
        reverse=True,
    )
    parts = [_HEADER]
    for s in ordered:
        parts.append(f"## {s.period}\n" + "\n".join(s.lines) + "\n")
    return "\n".join(parts)


def _save(cfg: Config, sections: list[PeriodSection]) -> None:
    import uuid
    path = events_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique tmp per writer (audit F2): a fixed tmp name is not safe under
    # the cross-persona concurrency this file explicitly supports.
    tmp = path.with_name(
        f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    )
    tmp.write_text(render(sections), encoding="utf-8")
    os.replace(tmp, path)


# ----- lock (cross-persona appends may race) --------------------------------


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - exists, owned elsewhere
        return True
    return True


def _try_acquire(path: Path) -> bool:
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(f"{os.getpid()} {time.time():.0f}")
        return True
    except OSError as exc:
        if exc.errno != errno.EEXIST:  # pragma: no cover - unexpected fs error
            raise
    # Lock exists: reclaim only a dead holder's — via the TOCTOU-safe
    # rename reclaim (audit F4), never a bare unlink.
    try:
        holder_pid = int(path.read_text().split()[0])
    except (ValueError, OSError, IndexError):
        holder_pid = -1
    if holder_pid > 0 and _pid_alive(holder_pid):
        return False
    reclaim_lockfile(path)
    return False  # caller retries the O_EXCL create


@contextmanager
def _lock(cfg: Config, *, retries: int = 40, delay: float = 0.05) -> Iterator[None]:
    """Exclusive team-log lock; raises :class:`TeamEventsLockHeld` when a
    live holder keeps it through every retry (~2s by default — sized so a
    concurrent persona's whole append, which is milliseconds, never wins
    a spurious drop)."""
    path = team_dir(cfg) / LOCK_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    for _ in range(retries):
        if _try_acquire(path):
            acquired = True
            break
        time.sleep(delay)
    if not acquired:
        raise TeamEventsLockHeld(
            f"team event log locked by another live process ({path})"
        )
    try:
        yield
    finally:
        release_lockfile(path)


# ----- append (the sweep's ingest hook) -------------------------------------


def _normalize_key(persona: str, body: str) -> str:
    """Dedup key for one bullet: persona + whitespace/case/period-insensitive
    event text, count suffix ignored."""
    text = " ".join(body.split()).lower().rstrip(".")
    return f"{persona.lower()}|{text}"


def _bullet(persona: str, event: str) -> str:
    text = " ".join(event.split()).rstrip(".")
    return f"- {persona} {text}."


def append_events(
    cfg: Config,
    *,
    persona: str,
    day: str,
    events: list[str],
    now: str | None = None,
) -> int:
    """Append *events* (verb-first clauses, no persona name) under *day*.

    Returns how many event lines landed (new bullets + count bumps). A
    repeat of an existing bullet for the same day bumps its ``(xN)``
    count — the "did yyy 3 times" form — instead of duplicating. A held
    lock is logged and skipped (the log is awareness, not the ledger of
    record; the persona stores already captured the session), so an
    ingest never fails on the team log.
    """
    if not cfg.memory.team_events.enabled or not events:
        return 0
    if len(events) > MAX_EVENTS_PER_APPEND:
        # The extraction contract asks for 0-3 EVENT lines; enforce it so
        # a chatty card cannot balloon the (backstop-exempt) day window
        # (audit: bounds finding 1).
        log.warning(
            "team-events append: %d events capped to %d",
            len(events), MAX_EVENTS_PER_APPEND,
        )
        events = events[:MAX_EVENTS_PER_APPEND]
    now = now or iso_now()
    try:
        date.fromisoformat(day)
    except (TypeError, ValueError):
        day = now[:10]
    try:
        with _lock(cfg):
            sections = load_sections(events_path(cfg))
            section = next((s for s in sections if s.period == day), None)
            if section is None:
                section = PeriodSection(period=day)
                sections.append(section)
            by_key: dict[str, int] = {}
            for i, line in enumerate(section.lines):
                m = _COUNT_RE.match(line)
                if m:  # non-bullet lines (preserved verbatim) never dedup
                    by_key[_normalize_key("", m.group("body"))] = i
            landed = 0
            for event in events:
                if not event.strip():
                    continue
                bullet = _bullet(persona, event)
                key = _normalize_key("", bullet)
                at = by_key.get(key)
                if at is None:
                    by_key[key] = len(section.lines)
                    section.lines.append(bullet)
                else:
                    m = _COUNT_RE.match(section.lines[at])
                    count = int(m.group("n") or 1) + 1
                    section.lines[at] = f"{m.group('body')} (x{count})"
                landed += 1
            if landed:
                _save(cfg, sections)
            return landed
    except TeamEventsLockHeld as exc:
        log.warning("team-events append skipped (%s); events not recorded", exc)
        return 0


# ----- compaction plan (non-AI) ---------------------------------------------


def _month_end(month: str) -> date:
    y, m = int(month[:4]), int(month[5:7])
    if m == 12:
        return date(y, 12, 31)
    return date(y, m + 1, 1) - timedelta(days=1)


def _folded_chars(sections: list[PeriodSection]) -> int:
    """Rendered size of the FOLDED tiers only (month + year sections)."""
    return len(render([s for s in sections if s.kind != KIND_DAY]))


def _backstop_trim(
    cfg: Config, sections: list[PeriodSection]
) -> tuple[list[PeriodSection], list[str]]:
    """Deterministic size backstop over the **folded tiers only**: while
    the rendered month+year sections are at/over ``overflow_limit``, drop
    the oldest year section, then the oldest month section, until back
    at/under ``max_length``.

    Daily sections — the Operator's uncompacted window — are exempt from
    both the measurement and the drops. Counting them against the bound
    while forbidding their removal made the backstop unsatisfiable at
    ordinary activity levels: it deleted every folded summary and still
    reported over-bound forever (audit: bounds finding 1). Scoped to the
    folded tiers it is always convergent — dropping everything it may
    drop reaches the empty render, which is under any valid ``max``.
    The day window is bounded by real team activity plus the per-append
    cap, by design.
    """
    te = cfg.memory.team_events
    if _folded_chars(sections) < te.overflow_limit:
        return sections, []
    survivors = list(sections)
    dropped: list[str] = []
    for kind in (KIND_YEAR, KIND_MONTH):
        victims = sorted(
            (s for s in survivors if s.kind == kind), key=lambda s: s.period
        )
        for victim in victims:
            if _folded_chars(survivors) <= te.max_length:
                return survivors, dropped
            survivors.remove(victim)
            dropped.append(victim.period)
    return survivors, dropped


def _fill(cfg: Config, name: str, **kwargs) -> str:
    from .lifecycle import _fill_prompt, _prompts_root
    return _fill_prompt(_prompts_root(cfg) / name, **kwargs)


def _stage_fold(
    cfg: Config,
    staging: Path,
    *,
    kind: str,
    period: str,
    sources: list[PeriodSection],
    max_chars: int,
) -> dict:
    key = f"{kind}.{period}"
    content = "\n".join(
        f"## {s.period}\n" + "\n".join(s.lines) + "\n" for s in sources
    )
    prompt_path = staging / f"{key}.prompt.md"
    prompt_path.write_text(
        _fill(
            cfg, "compact_team_events.md",
            kind=kind,
            period=period,
            current_chars=len(content),
            max_chars=max_chars,
            content=content,
        ),
        encoding="utf-8",
    )
    return {
        "kind": kind,
        "key": key,
        "period": period,
        "prompt_path": str(prompt_path),
        "card_path": str(prompt_path.with_name(f"{key}{CARD_SUFFIX}")),
        "source_periods": [s.period for s in sources],
        "snapshot": {s.period: list(s.lines) for s in sources},
    }


def compact_plan(cfg: Config, *, now: str | None = None) -> dict:
    """Stage one fold prompt per aged-out period; return the manifest.

    Age, not size, triggers the folds (the ADR 0008 directive): day
    sections fold once their whole month is older than ``recent_days``;
    month sections fold once their year end is older than
    ``year_after_days`` (and no stray day sections remain in that year —
    those fold to months first, so a year converges over two sweeps).
    The deterministic size backstop runs first. ``targets: []`` means
    nothing aged out (the common case).
    """
    now = now or iso_now()
    today = date.fromisoformat(now[:10])
    te = cfg.memory.team_events
    staging = _staging_dir(cfg)
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    dropped: list[str] = []
    try:
        # Load + trim + save all under the lock (audit F3): a lockless
        # load followed by a locked save of the stale snapshot would erase
        # any append that landed in between.
        with _lock(cfg):
            sections = load_sections(events_path(cfg))
            trimmed, dropped = _backstop_trim(cfg, sections)
            if dropped:
                _save(cfg, trimmed)
                sections = trimmed
                log.info(
                    "team-events compact-plan: backstop dropped %d "
                    "section(s): %s", len(dropped), ", ".join(dropped),
                )
    except TeamEventsLockHeld:
        log.warning(
            "team-events compact-plan: lock held; backstop trim skipped"
        )
        sections = load_sections(events_path(cfg))  # read-only staging is fine

    targets: list[dict] = []

    day_cutoff = today - timedelta(days=te.recent_days)
    by_month: dict[str, list[PeriodSection]] = {}
    for s in sections:
        if s.kind == KIND_DAY:
            by_month.setdefault(s.period[:7], []).append(s)
    for month in sorted(by_month):
        if _month_end(month) >= day_cutoff:
            continue
        sources = sorted(by_month[month], key=lambda s: s.period)
        existing = next(
            (s for s in sections if s.period == month), None
        )
        if existing is not None:
            sources.insert(0, existing)
        targets.append(
            _stage_fold(
                cfg, staging, kind=KIND_MONTH, period=month,
                sources=sources, max_chars=te.month_max_chars,
            )
        )

    year_cutoff = today - timedelta(days=te.year_after_days)
    months_by_year: dict[str, list[PeriodSection]] = {}
    for s in sections:
        if s.kind == KIND_MONTH:
            months_by_year.setdefault(s.period[:4], []).append(s)
    years_with_days = {s.period[:4] for s in sections if s.kind == KIND_DAY}
    for year in sorted(months_by_year):
        if year in years_with_days or date(int(year), 12, 31) >= year_cutoff:
            continue
        sources = sorted(months_by_year[year], key=lambda s: s.period)
        existing = next((s for s in sections if s.period == year), None)
        if existing is not None:
            sources.insert(0, existing)
        targets.append(
            _stage_fold(
                cfg, staging, kind=KIND_YEAR, period=year,
                sources=sources, max_chars=te.year_max_chars,
            )
        )

    manifest = {
        "generated_at": now,
        "dropped_periods": dropped,
        "targets": targets,
    }
    (staging / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    log.info("team-events compact-plan: staged %d target(s)", len(targets))
    return manifest


# ----- compaction apply (non-AI, deterministic convergence) ------------------


def _parse_card(text: str) -> list[str]:
    """The bullet lines under ``@@TEAM_EVENTS@@`` (whole-line marker match).

    Raises :class:`TeamEventsError` on a missing marker, a non-bullet
    line, or an empty section — a fold card must always carry content.
    """
    if not text or not text.strip():
        raise TeamEventsError("empty team-events card")
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip() == MARK_TEAM_EVENTS:
            section = [x.rstrip() for x in lines[i + 1:] if x.strip()]
            if not section:
                raise TeamEventsError("team-events card has no bullets")
            bad = [x for x in section if not x.startswith("- ")]
            if bad:
                raise TeamEventsError(
                    f"team-events card line is not a '- ' bullet: {bad[0]!r}"
                )
            return section
    raise TeamEventsError(f"missing marker {MARK_TEAM_EVENTS}")


def _trim_bullets(bullets: list[str], max_chars: int) -> tuple[list[str], bool]:
    """Keep leading bullets within *max_chars* total (always at least one)."""
    kept: list[str] = []
    running = 0
    for b in bullets:
        if kept and running + len(b) + 1 > max_chars:
            return kept, True
        kept.append(b)
        running += len(b) + 1
    return kept, False


def compact_apply(cfg: Config, *, now: str | None = None) -> dict:
    """Validate + apply every staged fold card (one process, race-free).

    A malformed card is reported and kept (the fold re-stages next
    sweep). Snapshot semantics: bullets appended to a source section
    between plan and apply are carried into the folded section verbatim,
    never silently lost. An oversized card is hard-trimmed (leading
    bullets kept) — deterministic convergence, like ADR 0007.

    Raises ``FileNotFoundError`` when no plan manifest exists.
    """
    staging = _staging_dir(cfg)
    manifest_path = staging / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"no team-events manifest at {manifest_path}; "
            "run team-events-compact-plan first"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    te = cfg.memory.team_events
    report: dict = {
        "applied": [], "skipped_no_card": [], "malformed": [],
        "forced_trims": [], "locked": [], "still_over": False,
    }
    for item in manifest.get("targets", []):
        key = item["key"]
        card_path = Path(item["card_path"])
        if not card_path.exists():
            report["skipped_no_card"].append(key)
            continue
        try:
            bullets = _parse_card(card_path.read_text(encoding="utf-8"))
        except TeamEventsError as exc:
            report["malformed"].append({"key": key, "error": str(exc)})
            continue
        max_chars = (
            te.month_max_chars if item["kind"] == KIND_MONTH
            else te.year_max_chars
        )
        snapshot: dict[str, list[str]] = item.get("snapshot") or {}
        snapshot_keys = {
            _normalize_key("", line)
            for lines in snapshot.values()
            for line in lines
        }
        source_periods = set(item["source_periods"])
        try:
            with _lock(cfg):
                sections = load_sections(events_path(cfg))
                survivors: list[str] = []
                kept: list[PeriodSection] = []
                for s in sections:
                    if s.period == item["period"] and s.period not in source_periods:
                        # A same-period section that was NOT a fold source
                        # exists only after a crashed earlier apply — merge
                        # into it instead of appending a duplicate section
                        # (audit: pipeline finding 3 / drift finding 9).
                        survivors.extend(s.lines)
                        continue
                    if s.period not in source_periods:
                        kept.append(s)
                        continue
                    # Snapshot survival by NORMALIZED key: an exact-line
                    # compare resurrects a bullet whose ``(xN)`` count was
                    # bumped between plan and apply (audit: pipeline
                    # finding 5) — the fold already covers it; only
                    # genuinely new lines survive.
                    survivors.extend(
                        x for x in s.lines
                        if _normalize_key("", x) not in snapshot_keys
                    )
                deduped: list[str] = []
                seen_keys: set[str] = set()
                for line in bullets + survivors:
                    k = _normalize_key("", line)
                    if k in seen_keys:
                        continue
                    seen_keys.add(k)
                    deduped.append(line)
                merged, trimmed = _trim_bullets(deduped, max_chars)
                if trimmed:
                    report["forced_trims"].append(key)
                kept.append(PeriodSection(period=item["period"], lines=merged))
                _save(cfg, kept)
        except TeamEventsLockHeld as exc:
            # Do not abort the whole apply over one contended target
            # (audit F7); it re-stages next sweep.
            log.warning("team-events compact-apply: %s skipped (%s)", key, exc)
            report["locked"].append(key)
            continue
        Path(item["prompt_path"]).unlink(missing_ok=True)
        card_path.unlink(missing_ok=True)
        report["applied"].append(key)
    # Consume the manifest once nothing actionable remains: a stale
    # manifest makes a driver's blind re-apply look "clean" (exit 0) while
    # doing nothing (audit: drift finding 6). Malformed/locked targets keep
    # it so a targeted retry stays possible; skipped-no-card items re-stage
    # from a fresh plan anyway.
    if not report["malformed"] and not report["locked"]:
        manifest_path.unlink(missing_ok=True)
    report["still_over"] = (
        _folded_chars(load_sections(events_path(cfg))) >= te.overflow_limit
    )
    log.info(
        "team-events compact-apply: %d applied, %d skipped, %d malformed",
        len(report["applied"]), len(report["skipped_no_card"]),
        len(report["malformed"]),
    )
    return report

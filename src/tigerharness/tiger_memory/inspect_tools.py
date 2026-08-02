"""Operator read/fix loop for tiger-memory (practicality audit).

The stores had writers and compactors but no operator-grade readers: no way
to ask "where does the team remember X?", no sanctioned way to remove one
wrong memory, and no single health view. This module supplies the three
verbs the CLI exposes for that loop:

- **search** — case-insensitive substring search across one persona's three
  journal stores (or, with ``--team``, every roster persona's) plus the
  team event log. Read-only, lockless (``BoundedStore.load`` is lenient).
- **forget** — operator-authority removal of ONE entry under the per-store
  lock. Never silent: the removed entry's full serialized block is appended
  to a ``<store>.forgotten.md`` audit sidecar BEFORE the save, and the
  briefing is rebuilt immediately so the read surface matches. An
  ``operator_explicit`` directive MAY be forgotten here — the Operator
  outranks the compaction forget-guard (which protects against *automated*
  loss, not against the Operator), so the removal deliberately bypasses
  :meth:`~tigerharness.tiger_memory.bounded_store.BoundedStore.forget`.
- **doctor** — a team-wide health table: store bounds, staging backlogs,
  rejected quarantines, sweep/ingest freshness, briefing presence, and
  cross-persona topic-slug collisions (audit S5 — fragmentation signal).
  Exit 1 when anything is flagged, so a cron can alert on it.

Plus the tiny persistence bridge doctor reads: the compact-apply and
ingest-staged glue record their last outcome into
``<store-root>/.last-sweep-report.json`` (:func:`record_sweep_report`),
consumed tolerantly by :func:`doctor_report`.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from . import sweep, team_events
from .bounded_store import BoundedStore, _serialize
from .briefing import README_NAME, rebuild_briefing
from .config import Config, ConfigError, load_config
from .cursor import load_cursors
from .entries import (
    KIND_OPERATOR_EXPLICIT,
    STORE_MUST_REMEMBER,
    STORE_NAMES,
    STORE_SKILLS,
    STORE_TOPICS,
    BaseEntry,
    MustRememberEntry,
    TopicEntry,
)
from .state import compute_state, iso_now
from .store import Store

log = logging.getLogger("tigerharness.tiger_memory.inspect_tools")

#: Pseudo-store name for the team event log in ``search`` (the three real
#: store names come from ``entries.STORE_NAMES``).
STORE_EVENTS = "events"

#: Audit sidecar suffix for ``forget`` (``topics.md`` → ``topics.forgotten.md``).
FORGOTTEN_SUFFIX = ".forgotten.md"

#: Last-sweep outcome file (store root), written by compact-apply and
#: ingest-staged, read tolerantly by doctor.
REPORT_FILENAME = ".last-sweep-report.json"

# Entry fields searched per store, in match-priority order. ``text`` is the
# memo body for must_remember, the procedure body for skills, and the dated
# detail body for topics.
_SEARCH_FIELDS = {
    STORE_SKILLS: ("name", "trigger", "procedure", "text"),
    STORE_MUST_REMEMBER: ("text",),
    STORE_TOPICS: ("name", "slug", "summary", "text"),
}


# ----- search ---------------------------------------------------------------


@dataclass(frozen=True)
class SearchHit:
    """One search match: where it lives + the first matching line."""

    persona: str
    store: str  # one of STORE_NAMES or STORE_EVENTS
    ref: str    # entry id (skills/must_remember), slug (topics), period (events)
    line: str   # first matching line, trimmed


def _trim(line: str, width: int = 120) -> str:
    line = line.strip()
    if len(line) <= width:
        return line
    return line[: width - 1] + "…"


def _first_matching_line(
    entry: BaseEntry, fields: tuple[str, ...], needle: str
) -> str | None:
    """The first line of the first field of *entry* containing *needle*."""
    for name in fields:
        for line in getattr(entry, name).split("\n"):
            if needle in line.lower():
                return line
    return None


def _search_persona(
    pcfg: Config, persona: str, needle: str, store_filter: str | None
) -> list[SearchHit]:
    """Search one persona's three journal stores (lenient, lockless)."""
    bstore = BoundedStore(pcfg, Store(pcfg.store.root))
    out: list[SearchHit] = []
    for store_name in STORE_NAMES:
        if store_filter is not None and store_filter != store_name:
            continue
        for entry in bstore.load(store_name):
            line = _first_matching_line(
                entry, _SEARCH_FIELDS[store_name], needle
            )
            if line is None:
                continue
            ref = entry.slug if isinstance(entry, TopicEntry) else entry.id
            out.append(SearchHit(persona, store_name, ref, _trim(line)))
    return out


def _search_events(cfg: Config, needle: str) -> list[SearchHit]:
    """Search the (team-level) event log; ref is the ``## <period>``."""
    out: list[SearchHit] = []
    for section in team_events.load_sections(team_events.events_path(cfg)):
        for line in section.lines:
            if needle in line.lower():
                out.append(
                    SearchHit("team", STORE_EVENTS, section.period, _trim(line))
                )
    return out


def search_memory(
    cfg: Config, term: str, *, team: bool = False, store: str | None = None
) -> list[SearchHit]:
    """Case-insensitive substring search over the journal stores + event log.

    Without *team*: only THIS config's persona stores. With *team*: every
    roster persona's (a persona whose config fails to load is skipped with
    a warning — search must not die on one broken config). The team event
    log is searched exactly once either way (it is team-level), when
    *store* is ``None`` or ``"events"``. Zero matches is not an error.
    """
    needle = term.lower()
    hits: list[SearchHit] = []
    if team:
        for target in sweep.enumerate_persona_configs(cfg.store.root.parent):
            try:
                pcfg = load_config(target.config_path)
            except ConfigError as exc:
                log.warning(
                    "search: skipping persona %s (config error: %s)",
                    target.name, exc,
                )
                continue
            hits.extend(_search_persona(pcfg, target.name, needle, store))
    else:
        hits.extend(_search_persona(cfg, cfg.agent.name, needle, store))
    if store in (None, STORE_EVENTS):
        hits.extend(_search_events(cfg, needle))
    return hits


# ----- forget ---------------------------------------------------------------


def _append_forgotten(store: Store, store_name: str, entry: BaseEntry) -> None:
    """Append *entry*'s full serialized block to the audit sidecar.

    ``<store>.forgotten.md`` in the journal dir, one dated separator per
    removal. Runs inside the store lock, so a plain append is safe (same
    discipline as check.py's quarantine sidecar).
    """
    path = store.paths.journal / f"{store_name}{FORGOTTEN_SUFFIX}"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    block = f"<!-- forgotten {iso_now()} -->\n{_serialize([entry])}"
    path.write_text(existing + block, encoding="utf-8")


def forget_entry(
    cfg: Config,
    store: Store,
    *,
    store_name: str,
    entry_id: str | None = None,
    slug: str | None = None,
) -> BaseEntry | None:
    """Remove ONE entry with operator authority; ``None`` if nothing matched.

    Locked read-modify-write; the removed entry is appended to the
    ``.forgotten.md`` sidecar BEFORE the save (no silent loss), and the
    briefing is rebuilt afterwards so the read surface updates immediately.

    An ``operator_explicit`` directive is removable here (logged loudly):
    the compaction forget-guard exists to stop *automated* loss, not the
    Operator — so this deliberately filters the list directly instead of
    routing through ``BoundedStore.forget``.

    Raises :class:`~tigerharness.tiger_memory.bounded_store.StoreLockHeld`
    when a live holder outlasts the wait.
    """
    bstore = BoundedStore(cfg, store)
    with bstore.store_lock_wait(store_name):
        entries = bstore.load(store_name)
        if slug is not None:
            victim = next(
                (e for e in entries
                 if isinstance(e, TopicEntry) and e.slug == slug),
                None,
            )
        else:
            victim = next((e for e in entries if e.id == entry_id), None)
        if victim is None:
            return None
        if (
            isinstance(victim, MustRememberEntry)
            and victim.kind == KIND_OPERATOR_EXPLICIT
        ):
            log.warning(
                "forget: removing operator_explicit directive %r on operator "
                "authority (the compaction forget-guard protects against "
                "automated loss, not the Operator)", victim.id,
            )
        _append_forgotten(store, store_name, victim)
        bstore.save_atomic(
            store_name, [e for e in entries if e is not victim]
        )
    rebuild_briefing(cfg, store)
    return victim


# ----- last-sweep report persistence (doctor's input) -----------------------


def sweep_report_path(store: Store) -> Path:
    return store.root / REPORT_FILENAME


def load_sweep_report(store: Store) -> dict:
    """Tolerant read of the last-sweep report. ``{}`` on any problem."""
    try:
        data = json.loads(sweep_report_path(store).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def record_sweep_report(store: Store, key: str, payload: dict) -> None:
    """Persist one glue verb's outcome under *key* (+ an ``at`` stamp).

    Read-merge-write over the existing file so ``compact_apply`` and
    ``ingest`` coexist; atomic via ``store.atomic_write``.
    """
    data = load_sweep_report(store)
    data[key] = {**payload, "at": iso_now()}
    store.atomic_write(
        sweep_report_path(store),
        json.dumps(data, indent=2, sort_keys=True) + "\n",
    )


# ----- doctor ---------------------------------------------------------------


def _count_staged(staging: Path) -> int:
    """Staged prompt/card files in *staging* (``manifest.json`` excluded)."""
    if not staging.is_dir():
        return 0
    return sum(
        1 for p in staging.iterdir()
        if p.is_file() and p.name != "manifest.json"
    )


def _norm_slug(slug: str) -> str:
    """Collision-normalized slug form: separators dropped, lowercased —
    ``topic-store`` and ``topicstore`` are the same fragmenting topic."""
    return re.sub(r"[^a-z0-9]", "", slug.lower())


def _persona_payload(
    name: str, pcfg: Config, done_at: dict[str, str]
) -> tuple[dict, list[str]]:
    """One persona's doctor row + its anomaly flags."""
    from .compaction import _staging_dir as _compact_staging_dir
    from .lifecycle import _sweep_staging_dir

    pstore = Store(pcfg.store.root)
    st = compute_state(pcfg, pstore)
    flags: list[str] = []
    for store_name, s in st["stores"].items():
        if s["over_overflow"]:
            flags.append(
                f"{name}: {store_name} over_overflow "
                f"({s['chars']} chars, max {s['max']})"
            )
    rejected = sorted(
        p.name for p in pstore.paths.journal.glob("*.rejected.md")
    )
    if rejected:
        flags.append(f"{name}: rejected file(s): {', '.join(rejected)}")
    persona_done = done_at.get(name)
    if persona_done is None:
        flags.append(f"{name}: never swept (no done_at recorded)")
    data_through = None
    for c in load_cursors(pstore).values():
        if data_through is None or c.last_event_at > data_through:
            data_through = c.last_event_at
    report = load_sweep_report(pstore)
    last_compact = report.get("compact_apply")
    last_compact = last_compact if isinstance(last_compact, dict) else None
    last_ingest = report.get("ingest")
    last_ingest = last_ingest if isinstance(last_ingest, dict) else None
    if last_compact and last_compact.get("still_over"):
        flags.append(
            f"{name}: last compact-apply left still_over: "
            f"{', '.join(last_compact['still_over'])}"
        )
    briefing_present = (pstore.paths.briefing / README_NAME).exists()
    if not briefing_present:
        flags.append(f"{name}: briefing missing (run rebuild)")
    payload = {
        "persona": name,
        "stores": st["stores"],
        "last_rebuild_at": st["last_rebuild_at"],
        "staged": {
            "sweep": _count_staged(_sweep_staging_dir(pstore)),
            "compact": _count_staged(_compact_staging_dir(pstore)),
        },
        "rejected": rejected,
        "done_at": persona_done,
        "data_through": data_through,
        "last_compact_apply": last_compact,
        "last_ingest": last_ingest,
        "briefing_present": briefing_present,
    }
    return payload, flags


def doctor_report(cfg: Config) -> dict:
    """The team-wide health structure (``tiger-memory doctor``).

    Driven from any one persona's config: the team dir is the store root's
    parent (same convention as the sweep gating). A persona whose config
    fails to load is flagged, not fatal.
    """
    team_dir = cfg.store.root.parent
    done_at = sweep.persona_done_at(team_dir)
    personas: list[dict] = []
    flags: list[str] = []
    slug_map: dict[str, list[tuple[str, str]]] = {}
    for target in sweep.enumerate_persona_configs(team_dir):
        try:
            pcfg = load_config(target.config_path)
        except ConfigError as exc:
            flags.append(f"{target.name}: config error ({exc})")
            continue
        payload, pflags = _persona_payload(target.name, pcfg, done_at)
        personas.append(payload)
        flags.extend(pflags)
        bstore = BoundedStore(pcfg, Store(pcfg.store.root))
        loaded = bstore.load(STORE_TOPICS)
        for e in [t for t in loaded if isinstance(t, TopicEntry)]:
            slug_map.setdefault(_norm_slug(e.slug), []).append(
                (target.name, e.slug)
            )

    collisions: list[dict] = []
    for norm in sorted(slug_map):
        owners = sorted({p for p, _ in slug_map[norm]})
        if len(owners) < 2:
            continue
        slugs = sorted({s for _, s in slug_map[norm]})
        collisions.append({"slugs": slugs, "personas": owners})
        flags.append(
            f"topic slug collision: {'/'.join(slugs)} "
            f"across {', '.join(owners)}"
        )

    state = sweep.read_sweep_state(team_dir)
    try:
        event_log_chars = len(
            team_events.events_path(cfg).read_text(encoding="utf-8")
        )
    except OSError:
        event_log_chars = 0
    team = {
        "last_sweep_at": state.get("last_sweep_at"),
        "claim_held": bool(state.get("claim_token")),
        "progress": sorted(sweep.sweep_progress(team_dir)),
        "event_log_chars": event_log_chars,
        "event_log_sections": len(
            team_events.load_sections(team_events.events_path(cfg))
        ),
        "topic_slug_collisions": collisions,
    }
    return {
        "generated_at": iso_now(),
        "personas": personas,
        "team": team,
        "flags": flags,
    }


def _store_cell(s: dict) -> str:
    over = "!" if s["over_overflow"] else ""
    return f"{s['count']}x {s['chars']}/{s['max']}{over}"


def _ts_cell(ts: str | None) -> str:
    return ts[:16] if ts else "never"


def render_doctor(report: dict) -> str:
    """The human table + team block + FLAGS section for ``doctor``."""
    rows = [(
        "PERSONA", "SKILLS", "MUST_REMEMBER", "TOPICS",
        "STAGED", "REJECTED", "DONE_AT", "DATA_THROUGH",
    )]
    for p in report["personas"]:
        rows.append((
            p["persona"],
            _store_cell(p["stores"]["skills"]),
            _store_cell(p["stores"]["must_remember"]),
            _store_cell(p["stores"]["topics"]),
            f"{p['staged']['sweep']}+{p['staged']['compact']}",
            str(len(p["rejected"])) if p["rejected"] else "-",
            _ts_cell(p["done_at"]),
            _ts_cell(p["data_through"]),
        ))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = [
        "  ".join(cell.ljust(w) for cell, w in zip(r, widths)).rstrip()
        for r in rows
    ]
    team = report["team"]
    progress = (
        f" (progress: {', '.join(team['progress'])})" if team["progress"]
        else ""
    )
    lines += [
        "",
        "TEAM:",
        f"  last_sweep_at: {team['last_sweep_at'] or 'never'}",
        f"  claim_held: {'yes' if team['claim_held'] else 'no'}{progress}",
        f"  event log: {team['event_log_sections']} section(s), "
        f"{team['event_log_chars']} chars",
    ]
    if team["topic_slug_collisions"]:
        lines.append("  topic slug collisions:")
        for c in team["topic_slug_collisions"]:
            lines.append(
                f"    {'/'.join(c['slugs'])}: {', '.join(c['personas'])}"
            )
    lines.append("")
    if report["flags"]:
        lines.append("FLAGS:")
        lines.extend(f"  - {f}" for f in report["flags"])
    else:
        lines.append("FLAGS: none")
    return "\n".join(lines)

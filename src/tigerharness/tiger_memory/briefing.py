"""Session-start briefing assembly (design §6; topic-store revamp ADR 0007).

The session-start working set is deliberately small — **indexes only**:

- the **full must_remember** store (bounded tight, so cheap to load whole);
- the **skill index** — one compact line-block per skill (design §4.1
  progressive disclosure: only the index loads; each skill's procedure is a
  separate file under ``briefing/skills/``, read on demand);
- the **topic index** — one compact block per topic, freshest first; each
  topic's dated detail body is a separate file under ``briefing/topics/``,
  read on demand;
- the **unprocessed/active-session notice** (design §6).

The retired chronological rollup layers and the diary/fuzzy stores are gone
(design §3; ADR 0007). The briefing is rebuilt atomically into
``briefing/`` via a temp-dir folder swap.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from . import indexes
from .bounded_store import BoundedStore
from .config import Config
from .entries import (
    STORE_MUST_REMEMBER,
    STORE_SKILLS,
    STORE_TOPICS,
    MustRememberEntry,
    SkillEntry,
    TopicEntry,
)
from .state import iso_now
from .store import Store

log = logging.getLogger("tigerharness.tiger_memory.briefing")

# Files written into the assembled briefing/ working set.
README_NAME = "README.md"
MUST_REMEMBER_NAME = "must_remember.md"
SKILL_INDEX_NAME = "skill_index.md"
TOPIC_INDEX_NAME = "topic_index.md"
MANIFEST_NAME = "MANIFEST.md"
NOTICE_NAME = "UNPROCESSED.md"
FINGERPRINT_NAME = ".fingerprint"

# The store files whose content drives the briefing — a fingerprint over them
# powers the no-op shortcut (skip the rebuild when nothing changed).
_SOURCE_STORE_FILES = ("skills.md", "must_remember.md", "topics.md")


def rebuild_briefing(cfg: Config, store: Store) -> None:
    """Atomically (re)assemble ``briefing/`` from the three bounded stores.

    No-op shortcut: if the source store files are unchanged since the last
    rebuild (fingerprint match), skip. Otherwise stage everything in a temp
    dir next to ``briefing/`` and swap it in atomically (design §6 rebuild).
    """
    if _briefing_up_to_date(store):
        log.info("briefing rebuild: no-op (stores unchanged)")
        return
    log.info("briefing rebuild: starting (stores changed)")
    bstore = BoundedStore(cfg, store)

    parent = store.paths.briefing.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="briefing.tmp.", dir=parent))
    try:
        (tmp / README_NAME).write_text(_render_readme(cfg), encoding="utf-8")
        (tmp / NOTICE_NAME).write_text(
            _render_notice(cfg, store), encoding="utf-8"
        )

        must = [
            e for e in bstore.load(STORE_MUST_REMEMBER)
            if isinstance(e, MustRememberEntry)
        ]
        (tmp / MUST_REMEMBER_NAME).write_text(
            _render_must_remember(must), encoding="utf-8"
        )

        skills = [
            e for e in bstore.load(STORE_SKILLS) if isinstance(e, SkillEntry)
        ]
        (tmp / SKILL_INDEX_NAME).write_text(
            indexes.render_skill_index(skills), encoding="utf-8"
        )
        skills_dir = tmp / indexes.SKILLS_DETAIL_DIR
        skills_dir.mkdir()
        for s in skills:
            (skills_dir / indexes.skill_detail_filename(s)).write_text(
                indexes.render_skill_detail(s), encoding="utf-8"
            )

        topics = [
            e for e in bstore.load(STORE_TOPICS) if isinstance(e, TopicEntry)
        ]
        (tmp / TOPIC_INDEX_NAME).write_text(
            indexes.render_topic_index(topics), encoding="utf-8"
        )
        topics_dir = tmp / indexes.TOPICS_DETAIL_DIR
        topics_dir.mkdir()
        for t in topics:
            (topics_dir / indexes.topic_detail_filename(t)).write_text(
                indexes.render_topic_detail(t), encoding="utf-8"
            )

        (tmp / MANIFEST_NAME).write_text(
            _render_manifest(cfg, store, must, skills, topics),
            encoding="utf-8",
        )
        (tmp / FINGERPRINT_NAME).write_text(
            _compute_fingerprint(store), encoding="utf-8"
        )

        store.atomic_swap_dir(tmp, store.paths.briefing)
    except Exception:
        if tmp.exists():  # pragma: no branch  # mkdtemp always creates the dir
            shutil.rmtree(tmp, ignore_errors=True)
        raise
    # Stamp the rebuild time. Nothing wrote ``last_rebuild_at`` on the
    # post-ADR-0007 path, so MANIFEST and `state` reported a weeks-stale
    # date — a false "this memory is ancient" signal to every reader
    # (practicality audit, three independent sightings).
    state_payload = store.read_state() or {}
    state_payload["last_rebuild_at"] = iso_now()
    store.write_state(state_payload)


# ----- no-op shortcut -------------------------------------------------------


def _briefing_up_to_date(store: Store) -> bool:
    """True iff the briefing exists and its fingerprint still matches the stores."""
    fingerprint_path = store.paths.briefing / FINGERPRINT_NAME
    manifest = store.paths.briefing / MANIFEST_NAME
    if not manifest.exists() or not fingerprint_path.exists():
        return False
    try:
        saved = fingerprint_path.read_text(encoding="utf-8")
    except OSError:  # pragma: no cover - best-effort read
        return False
    return saved == _compute_fingerprint(store)


def _compute_fingerprint(store: Store) -> str:
    """``<name>:<mtime_ns>:<size>`` per source store file (absent → 0:0).

    Both mtime and size are included so a content change is detected even when
    a rapid back-to-back rewrite lands within the same mtime tick.
    """
    lines = []
    for name in _SOURCE_STORE_FILES:
        p = store.paths.journal / name
        try:
            st = p.stat()
            lines.append(f"{name}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            lines.append(f"{name}:0:0")
    return "\n".join(lines) + "\n"


# ----- README + unprocessed notice ------------------------------------------


def _render_readme(cfg: Config) -> str:
    template_path = Path(__file__).parent / "templates" / "briefing_readme.md"
    template = template_path.read_text(encoding="utf-8")
    from .config import _slugify
    return template.format_map(_SafeFormat({
        "agent_name": cfg.agent.name,
        "agent_slug": _slugify(cfg.agent.name),
    }))


class _SafeFormat(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _render_notice(cfg: Config, store: Store) -> str:
    """The unprocessed/active-session awareness notice (design §6).

    Includes a machine-generated **data-through** line so a persona can
    STATE its staleness instead of silently misleading — memory here is
    structurally up to ~a day behind (idle threshold + staleness floor),
    and the practicality audit found "where are we?" answered from a
    briefing missing the whole weekend with no way to say so.
    """
    from .cursor import load_cursors

    data_through = None
    for c in load_cursors(store).values():
        if data_through is None or c.last_event_at > data_through:
            data_through = c.last_event_at
    through_line = (
        f"**This briefing reflects ingested sessions through "
        f"{data_through[:16].replace('T', ' ')}Z** (session end time). "
        "Anything newer — including all still-active work — is not in "
        "memory yet; find it in the journal, worklogs, or git.\n\n"
        if data_through
        else "**Nothing has been ingested into this memory yet** — treat "
             "every store as empty and rely on the journal, worklogs, and "
             "git for state.\n\n"
    )
    return (
        "# Unprocessed sessions — read this rule\n\n"
        f"You are **{cfg.agent.name}**. Memory is built from sessions only "
        "after they go idle, so a **still-active or very recent session may "
        "not be reflected in this briefing yet**.\n\n"
        + through_line +
        "**Rule:** if the Operator references something you do not recognise, "
        "do NOT claim ignorance immediately. First check this memory "
        "(must_remember + skill index + topic index, then the relevant "
        "detail files), then check for unprocessed / active recent "
        "sessions. Only then say you don't know. When your answer is about "
        "recent project state, SAY how fresh your memory is (the "
        "data-through line above).\n"
    )


# ----- must_remember view ---------------------------------------------------


def _render_must_remember(entries: list[MustRememberEntry]) -> str:
    """Full must_remember store: operator directives first, then by
    recurrence and freshness.

    Each line carries the item's salience signals — ``last`` is the day a
    sweep last TOUCHed it (or it was written) and ``×N`` how often it has
    recurred. An item untouched past ``must_remember.forget_days`` becomes
    forget-eligible at compaction, so the date is load-bearing, not
    decoration. (There is no importance scalar — recurrence + freshness
    ARE the ranking.)
    """
    if not entries:
        return "# Must remember\n\n_(empty)_\n"
    from .entries import KIND_OPERATOR_EXPLICIT
    # Stable two-pass sort: freshest first within each recurrence bucket,
    # then protected directives first, most-recurrent next.
    by_freshness = sorted(entries, key=lambda e: e.last_used, reverse=True)
    ordered = sorted(
        by_freshness,
        key=lambda e: (
            e.kind != KIND_OPERATOR_EXPLICIT,   # protected directives first
            -e.repeat_count,                     # most-recurrent next
        ),
    )
    lines = ["# Must remember (read first, always load-bearing)", ""]
    for e in ordered:
        lines.append(
            f"- **[{e.kind}]** (last {e.last_used[:10]} · {e.repeat_count}×) "
            f"{e.text}"
        )
    return "\n".join(lines) + "\n"


# ----- MANIFEST -------------------------------------------------------------


def _render_manifest(
    cfg: Config,
    store: Store,
    must: list[MustRememberEntry],
    skills: list[SkillEntry],
    topics: list[TopicEntry],
) -> str:
    # The manifest is rendered DURING a rebuild — its rebuild time is now,
    # not whatever a previous era last stamped into state.
    last_rebuild = iso_now()
    parts = [
        "# Briefing manifest",
        "",
        f"- Agent: {cfg.agent.name}",
        f"- Last rebuild: {last_rebuild}",
        f"- must_remember: {len(must)} entries",
        f"- skills: {len(skills)} indexed "
        f"(details under `{indexes.SKILLS_DETAIL_DIR}/`)",
        f"- topics: {len(topics)} indexed "
        f"(details under `{indexes.TOPICS_DETAIL_DIR}/`)",
        "",
        "## Read order (initial load = the notice + three small indexes ONLY)",
        f"1. `{NOTICE_NAME}` — the unprocessed-session rule.",
        f"2. `{MUST_REMEMBER_NAME}` — external directives (load-bearing).",
        f"3. `{SKILL_INDEX_NAME}` — reusable lessons (index only).",
        f"4. `{TOPIC_INDEX_NAME}` — project knowledge (index only).",
        "",
        "Open a detail file under "
        f"`{indexes.SKILLS_DETAIL_DIR}/` or `{indexes.TOPICS_DETAIL_DIR}/` "
        "only when its index line is relevant to the work at hand.",
        "",
    ]
    return "\n".join(parts) + "\n"

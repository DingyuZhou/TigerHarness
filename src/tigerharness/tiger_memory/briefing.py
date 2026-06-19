"""Session-start briefing assembly (bounded-store revamp; design §6; plan §2 dev-3).

The session-start working set is now exactly:

- the **full must_remember** store (bounded, so cheap to load whole);
- an **emotional view** — the top entries by ``|weight|`` (strong feelings,
  positive or negative, survive; near-neutral/decayed items rank lower);
- the **skill index** — name + trigger + a one-line summary per skill,
  rebuilt by Python at session start (design §4.1 progressive disclosure: only
  the index loads; the persona pulls a full skill on demand);
- the **unprocessed/active-session notice** (design §6): a short note that a
  still-active recent session may not be in memory yet, with the rule to check
  memory first, then unprocessed sessions, before claiming ignorance.

The retired chronological rollup layers (shorts / daily / weekly / monthly /
``longer_memory``) and the drill/search affordances are gone (design §3). The
briefing is rebuilt atomically into ``briefing/`` via a temp-dir folder swap.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from . import fuzzy_store
from .bounded_store import BoundedStore
from .config import Config
from .entries import (
    STORE_DIARY,
    STORE_MUST_REMEMBER,
    STORE_SKILLS,
    DiaryEntry,
    MustRememberEntry,
    SkillEntry,
)
from .state import iso_now
from .store import Store

log = logging.getLogger("tigerharness.tiger_memory.briefing")

# Files written into the assembled briefing/ working set.
README_NAME = "README.md"
MUST_REMEMBER_NAME = "must_remember.md"
DIARY_NAME = "diary.md"
FUZZY_NAME = "fuzzy.md"
SKILL_INDEX_NAME = "skill_index.md"
MANIFEST_NAME = "MANIFEST.md"
NOTICE_NAME = "UNPROCESSED.md"
FINGERPRINT_NAME = ".fingerprint"

# The store files whose content drives the briefing — a fingerprint over them
# powers the no-op shortcut (skip the rebuild when nothing changed). The fuzzy
# store (4-store model) loads whole at session start too, so it is included.
_SOURCE_STORE_FILES = ("skills.md", "must_remember.md", "diary.md", "fuzzy.md")


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
        (tmp / NOTICE_NAME).write_text(_render_notice(cfg), encoding="utf-8")

        must = bstore.load(STORE_MUST_REMEMBER)
        (tmp / MUST_REMEMBER_NAME).write_text(
            _render_must_remember(must), encoding="utf-8"
        )

        diary = bstore.load(STORE_DIARY)
        (tmp / DIARY_NAME).write_text(
            _render_diary(diary),
            encoding="utf-8",
        )

        skills = bstore.load(STORE_SKILLS)
        (tmp / SKILL_INDEX_NAME).write_text(
            _render_skill_index(skills), encoding="utf-8"
        )

        fuzzy = fuzzy_store.load_fuzzy(store)
        (tmp / FUZZY_NAME).write_text(_render_fuzzy(fuzzy), encoding="utf-8")

        (tmp / MANIFEST_NAME).write_text(
            _render_manifest(cfg, store, must, diary, skills, fuzzy),
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


def _render_notice(cfg: Config) -> str:
    """The unprocessed/active-session awareness notice (design §6)."""
    return (
        "# Unprocessed sessions — read this rule\n\n"
        f"You are **{cfg.agent.name}**. Memory is built from sessions only "
        "after they go idle, so a **still-active or very recent session may "
        "not be reflected in this briefing yet**.\n\n"
        "**Rule:** if the Operator references something you do not recognise, "
        "do NOT claim ignorance immediately. First check this memory "
        "(must_remember + skill index + emotional), then check for "
        "unprocessed / active recent sessions. Only then say you don't know.\n"
    )


# ----- must_remember view ---------------------------------------------------


def _render_must_remember(entries: list[MustRememberEntry]) -> str:
    """Full must_remember store, highest-importance first."""
    if not entries:
        return "# Must remember\n\n_(empty)_\n"
    ordered = sorted(entries, key=lambda e: float(e.importance), reverse=True)
    lines = ["# Must remember (read first, always load-bearing)", ""]
    for e in ordered:
        lines.append(
            f"- **[{e.kind}]** (importance {float(e.importance):.1f}) {e.text}"
        )
    return "\n".join(lines) + "\n"


# ----- emotional view (top-by-|weight|) -------------------------------------


def _render_diary(entries: list[DiaryEntry]) -> str:
    """Diary view, strongest feelings first (by ``|weight|``).

    The diary is loaded WHOLE — every entry is shown (forgetting, not a display
    cap, keeps it bounded). A signed weight: positive = liked/for, negative =
    disliked/against; magnitude is how strongly it is felt.
    """
    if not entries:
        return "# Diary\n\n_(empty)_\n"
    ordered = sorted(entries, key=lambda e: abs(float(e.weight)), reverse=True)
    lines = ["# Diary (strongest feelings first)", ""]
    for e in ordered:
        sign = "+" if e.weight >= 0 else ""
        lines.append(
            f"- **({sign}{float(e.weight):.1f})** {e.text}"
        )
    return "\n".join(lines) + "\n"


def _render_fuzzy(text: str) -> str:
    """Fuzzy-memory view (4-store model): the coarsened, grouped older memory.

    The fuzzy store is free text the meditation re-compaction produced, loaded
    whole. Shown verbatim under a heading; empty when nothing has aged out yet.
    """
    body = text.strip()
    if not body:
        return "# Fuzzy memory\n\n_(empty)_\n"
    return f"# Fuzzy memory (coarsened older memory)\n\n{body}\n"


# ----- skill index (Python-rebuilt at session start) ------------------------


def _render_skill_index(entries: list[SkillEntry]) -> str:
    """The skill index: name + trigger + one-line summary per skill (design §4.1).

    Only this index loads at session start; the persona pulls the full skill
    (its procedure) on demand. Ordered most-important first.
    """
    if not entries:
        return (
            "# Skill index\n\n_(no skills learned yet)_\n\n"
            "Skills are learned lessons you can reuse. As you work, the sweep "
            "extracts them; they appear here once learned.\n"
        )
    ordered = sorted(entries, key=lambda e: float(e.importance), reverse=True)
    lines = [
        "# Skill index",
        "",
        "Learned, reusable lessons. Only this index is loaded; read the full "
        "skill in `skills.md` (the journal store) when its trigger applies.",
        "",
    ]
    for e in ordered:
        lines.append(f"## {e.name}")
        lines.append(f"- **When:** {e.trigger}")
        lines.append(
            f"- **Lesson:** {_one_line(e.procedure)} "
            f"(used {e.usage_count}×, importance {float(e.importance):.2f})"
        )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _one_line(text: str, limit: int = 100) -> str:
    """First non-empty line of *text*, trimmed to *limit* chars."""
    for line in text.splitlines():
        s = line.strip()
        if s:
            return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"
    return ""


# ----- MANIFEST -------------------------------------------------------------


def _render_manifest(
    cfg: Config,
    store: Store,
    must: list[MustRememberEntry],
    diary: list[DiaryEntry],
    skills: list[SkillEntry],
    fuzzy: str,
) -> str:
    saved = store.read_state() or {}
    last_rebuild = saved.get("last_rebuild_at") or iso_now()
    parts = [
        "# Briefing manifest",
        "",
        f"- Agent: {cfg.agent.name}",
        f"- Last rebuild: {last_rebuild}",
        f"- must_remember: {len(must)} entries",
        f"- diary: {len(diary)} entries",
        f"- skills: {len(skills)} indexed",
        f"- fuzzy: {len(fuzzy)} chars",
        "",
        "## Read order",
        f"1. `{NOTICE_NAME}` — the unprocessed-session rule.",
        f"2. `{MUST_REMEMBER_NAME}` — external directives (load-bearing).",
        f"3. `{SKILL_INDEX_NAME}` — reusable lessons (load full skill on demand).",
        f"4. `{DIARY_NAME}` — your reactions, strongest first.",
        f"5. `{FUZZY_NAME}` — coarsened older memory (the gist).",
        "",
    ]
    return "\n".join(parts) + "\n"

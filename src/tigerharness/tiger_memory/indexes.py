"""Index + detail renderers for the skills and topics stores (ADR 0007).

Single source of truth for what a persona actually loads:

- the **skill index** — one compact line-block per skill (name, trigger,
  one-line lesson) plus a pointer to its detail file;
- the **topic index** — one compact block per topic (name, slug, freshness,
  summary), ordered most-recently-touched first;
- the per-skill / per-topic **detail files** — the full procedure or the
  dated topic body, loaded on demand.

The store bounds (``index_max_length`` / ``index_overflow_limit`` and the
per-entry ``detail_*`` bounds) are measured over EXACTLY these rendered
strings, so "the index fits in N characters" means the same thing to the
compactor, the ``state`` snapshot, and the reader. Pure functions — no I/O,
no config; ordering is deterministic (score desc, then id) so re-rendering
is stable.
"""
from __future__ import annotations

from .entries import (
    EntryError,
    MustRememberEntry,
    SkillEntry,
    TopicEntry,
    topic_slug,
)

# Detail files land under these briefing subdirectories.
SKILLS_DETAIL_DIR = "skills"
TOPICS_DETAIL_DIR = "topics"


# ----- filenames -------------------------------------------------------------


def skill_detail_filename(entry: SkillEntry) -> str:
    """Stable, human-readable, collision-free detail filename for one skill.

    Skill names are not unique until compaction merges near-duplicates, so
    the entry id is part of the name. A name with no sluggable characters
    (blocked at extraction since ADR 0007, but a pre-existing store may
    carry one) falls back to a bare id filename rather than poisoning
    every rebuild with an EntryError.
    """
    try:
        slug = topic_slug(entry.name)
    except EntryError:
        slug = "skill"
    return f"{slug}-{entry.id}.md"


def topic_detail_filename(entry: TopicEntry) -> str:
    """Detail filename for one topic — the slug is unique by construction."""
    return f"{entry.slug}.md"


# ----- skill index + detail ---------------------------------------------------


def _skills_ordered(entries: list[SkillEntry]) -> list[SkillEntry]:
    return sorted(
        entries, key=lambda e: (-float(e.importance), e.id)
    )


def render_skill_index(entries: list[SkillEntry]) -> str:
    """The skill index (design §4.1 progressive disclosure, ADR 0007).

    Only this index loads at session start; each skill's procedure lives in
    its own detail file, read on demand when the trigger applies.
    """
    if not entries:
        return (
            "# Skill index\n\n_(no skills learned yet)_\n\n"
            "Skills are learned lessons you can reuse. As you work, the sweep "
            "extracts them; they appear here once learned.\n"
        )
    lines = [
        "# Skill index",
        "",
        "Learned, reusable lessons. Only this index is loaded; when a trigger "
        f"applies, read the skill's detail file under `{SKILLS_DETAIL_DIR}/`.",
        "",
    ]
    for e in _skills_ordered(entries):
        lines.append(f"- **{e.name}** — {e.trigger}")
        lines.append(f"  ↳ `{SKILLS_DETAIL_DIR}/{skill_detail_filename(e)}`")
    return "\n".join(lines) + "\n"


def render_skill_detail(entry: SkillEntry) -> str:
    """One skill's detail file: the full procedure, loaded on demand."""
    return (
        f"# {entry.name}\n\n"
        f"- **When:** {entry.trigger}\n"
        f"- **Used:** {entry.usage_count}× · importance "
        f"{float(entry.importance):.2f} · last used {entry.last_used[:10]}\n\n"
        f"{entry.procedure.rstrip()}\n"
    )


# ----- topic index + detail ----------------------------------------------------


def _topics_ordered(entries: list[TopicEntry]) -> list[TopicEntry]:
    """Most-recently-touched first (freshness order), id as the stable tie-break."""
    return sorted(entries, key=lambda e: (e.last_used, e.id), reverse=True)


def render_topic_index(entries: list[TopicEntry]) -> str:
    """The topic index (ADR 0007): the ONLY topic surface loaded at session start.

    One compact block per topic — name, slug (the address the sweep routes
    new material to), freshness, and the concise summary. Ordered
    most-recently-touched first so fresh topics lead.
    """
    if not entries:
        return (
            "# Topic index\n\n_(no topics yet)_\n\n"
            "Topics are named bodies of project knowledge. The sweep files "
            "new facts into them; they appear here as they form.\n"
        )
    lines = [
        "# Topic index (fresh first)",
        "",
        "Only this index is loaded. When a topic matters to the work at "
        f"hand, read its detail file under `{TOPICS_DETAIL_DIR}/<slug>.md`.",
        "",
    ]
    for e in _topics_ordered(entries):
        lines.append(
            f"- **{e.name}** (`{e.slug}`) · last {e.last_used[:10]} · "
            f"{e.touch_count}×"
        )
        lines.append(f"  {e.summary}")
    return "\n".join(lines) + "\n"


def render_topic_routing_list(entries: list[TopicEntry]) -> str:
    """The compact existing-topic list embedded in the extraction prompt.

    This is what lets a sweep summarizer route new facts into an existing
    topic instead of minting a near-duplicate: one line per topic — slug
    (the address to emit), name, summary, last-touched. Freshest first.
    """
    if not entries:
        return "(no topics exist yet — every topic you emit will be NEW)"
    lines = []
    for e in _topics_ordered(entries):
        lines.append(
            f"- `{e.slug}` — {e.name} (last {e.last_used[:10]}): {e.summary}"
        )
    return "\n".join(lines)


def render_must_remember_touch_list(entries: list[MustRememberEntry]) -> str:
    """The compact must-remember list embedded in the extraction prompt.

    This is what lets a sweep summarizer TOUCH items the session relates to
    (refreshing their ``last_used`` freshness anchor): one line per item —
    id (the address to emit), kind, memo. An item nobody touches for
    ``must_remember.forget_days`` becomes forget-eligible at compaction.
    """
    if not entries:
        return "(no must-remember items yet)"
    lines = []
    for e in entries:
        lines.append(f"- `{e.id}` [{e.kind}] {e.text}")
    return "\n".join(lines)


def render_topic_detail(entry: TopicEntry) -> str:
    """One topic's detail file: summary header + the dated detail body."""
    return (
        f"# {entry.name} (`{entry.slug}`)\n\n"
        f"_Last touched {entry.last_used[:10]} · {entry.touch_count}× · "
        f"{entry.summary}_\n\n"
        f"{entry.text.rstrip()}\n"
    )

"""Entry schemas for the three bounded memory stores (ADR 0007).

The topic-store revamp (ADR 0007) keeps exactly three per-persona stores,
each a list of small markdown entries with a YAML frontmatter carrying the
structured fields:

- **skills** (design §4.1): a learned, invokable lesson — ``name``,
  ``trigger``, ``procedure``, ``usage_count``, ``importance``. Loaded via a
  small rendered index; each skill's detail is a separate briefing file.
- **must_remember** (design §4.2): an external directive — ``kind``
  (``operator_explicit`` / ``preference`` / ``decision`` / ``incident``),
  ``importance``.
- **topics** (ADR 0007): a named, growing body of project knowledge —
  ``name``, ``slug``, ``summary``, ``touch_count``; the entry ``text`` is
  the topic's dated detail body. Only the topic index (slug + summary +
  freshness) loads at session start; details are separate briefing files.

Every entry shares a **base** shape: ``id``, ``text``, ``created_at``,
``last_used``, ``source``. The per-store subclasses add their fields. For a
topic, ``last_used`` doubles as ``last_touched`` — the freshness anchor that
orders the index and drives forget-eligibility.

These dataclasses are the frozen cross-module interface. They are pure data
+ validation — no I/O (that is ``store.py``), no scoring (that is
``skills.py``). Validation is fail-fast: a malformed entry raises
``EntryError`` so a bad write is caught at construction, never silently
persisted.

Length is measured in CHARACTERS, never tokens (vendor-neutral, design §8).
"""
from __future__ import annotations

import logging
import re
import uuid as _uuid
from dataclasses import asdict, dataclass, field
from typing import Any

log = logging.getLogger("tigerharness.tiger_memory.entries")


class EntryError(ValueError):
    """Raised when an entry's fields are missing, malformed, or out of range."""


# ----- store names (the frozen ``store_name`` enum) ------------------------

STORE_SKILLS = "skills"
STORE_MUST_REMEMBER = "must_remember"
STORE_TOPICS = "topics"
#: The three ENTRY-based stores (typed entries; driven by BoundedStore).
#: The diary and fuzzy stores are RETIRED (ADR 0007) — there is no
#: free-text store any more.
STORE_NAMES = (STORE_SKILLS, STORE_MUST_REMEMBER, STORE_TOPICS)

# must_remember kinds (design §4.2). ``operator_explicit`` is the elevated
# directive the forget-guard protects until the relevance-check runs (§5).
KIND_OPERATOR_EXPLICIT = "operator_explicit"
KIND_PREFERENCE = "preference"
KIND_DECISION = "decision"
KIND_INCIDENT = "incident"
VALID_KINDS = (
    KIND_OPERATOR_EXPLICIT,
    KIND_PREFERENCE,
    KIND_DECISION,
    KIND_INCIDENT,
)

#: Legacy must_remember ``kind`` values mapped to their current names on READ.
#: ``owner_explicit`` was renamed to ``operator_explicit`` (Operator-mandated);
#: stores written before the rename still carry the old value, so we normalize
#: it at load time — otherwise those elevated directives would fail validation
#: and be silently dropped (no silent loss). Write side always uses the new name.
_LEGACY_KIND_ALIASES = {"owner_explicit": KIND_OPERATOR_EXPLICIT}


def normalize_kind(kind: str) -> str:
    """Map a legacy must_remember ``kind`` value to its current name (read-side)."""
    return _LEGACY_KIND_ALIASES.get(kind, kind)


def new_id() -> str:
    """A fresh entry id — short, stable, collision-free in practice."""
    return _uuid.uuid4().hex[:12]


# ----- base entry ----------------------------------------------------------


@dataclass
class BaseEntry:
    """Fields shared by every store entry (plan §1).

    ``text`` is the human-readable body; ``created_at`` / ``last_used`` are
    ISO-8601 UTC strings (``state.iso_now()``); ``source`` is free-text
    provenance (e.g. ``"pin"``, ``"extract"``, ``"merge"``).
    """

    text: str
    created_at: str
    last_used: str
    source: str
    id: str = field(default_factory=new_id)

    # The store this entry belongs to. Concrete subclasses set it; the base
    # is abstract and never persisted directly.
    store_name: str = field(default="", init=False, repr=False)

    def validate(self) -> None:
        """Validate the shared fields. Subclasses call ``super().validate()``."""
        if not isinstance(self.id, str) or not self.id:
            raise EntryError("entry.id must be a non-empty string.")
        if not isinstance(self.text, str) or not self.text.strip():
            raise EntryError("entry.text must be a non-empty string.")
        if not isinstance(self.created_at, str) or not self.created_at:
            raise EntryError("entry.created_at must be a non-empty ISO string.")
        if not isinstance(self.last_used, str) or not self.last_used:
            raise EntryError("entry.last_used must be a non-empty ISO string.")
        if not isinstance(self.source, str) or not self.source:
            raise EntryError("entry.source must be a non-empty string.")

    def frontmatter(self) -> dict[str, Any]:
        """Structured fields for the markdown frontmatter (excludes ``text``).

        ``text`` is rendered as the markdown body, not the frontmatter, so
        the file stays human-readable. Subclasses extend the base dict.
        """
        return {
            "id": self.id,
            "store": self.store_name,
            "created_at": self.created_at,
            "last_used": self.last_used,
            "source": self.source,
        }

    def to_dict(self) -> dict[str, Any]:
        """Full dict form (including ``text`` + ``store_name``)."""
        d = asdict(self)
        d["store_name"] = self.store_name
        return d


# ----- skills (§4.1) -------------------------------------------------------


@dataclass
class SkillEntry(BaseEntry):
    """A learned, invokable skill (design §4.1).

    ``importance`` is (re)derived from ``usage_count`` + ``last_used`` at
    meditation time (Rukawa's scoring) — stored so the index/keep-rank can
    read it without recomputing.
    """

    name: str = ""
    trigger: str = ""
    procedure: str = ""
    usage_count: int = 0
    importance: float = 0.0

    def __post_init__(self) -> None:
        self.store_name = STORE_SKILLS

    def validate(self) -> None:
        super().validate()
        if not isinstance(self.name, str) or not self.name.strip():
            raise EntryError("skill.name must be a non-empty string.")
        if not isinstance(self.trigger, str) or not self.trigger.strip():
            raise EntryError("skill.trigger must be a non-empty string.")
        if not isinstance(self.procedure, str) or not self.procedure.strip():
            raise EntryError("skill.procedure must be a non-empty string.")
        if not isinstance(self.usage_count, int) or self.usage_count < 0:
            raise EntryError("skill.usage_count must be a non-negative int.")
        if isinstance(self.importance, bool) or not isinstance(
            self.importance, (int, float)
        ):
            raise EntryError("skill.importance must be a number.")

    def frontmatter(self) -> dict[str, Any]:
        fm = super().frontmatter()
        fm.update(
            {
                "name": self.name,
                "trigger": self.trigger,
                "procedure": self.procedure,
                "usage_count": self.usage_count,
                "importance": float(self.importance),
            }
        )
        return fm


# ----- must_remember (§4.2) ------------------------------------------------


@dataclass
class MustRememberEntry(BaseEntry):
    """An external directive (design §4.2).

    ``kind`` is one of ``VALID_KINDS``. ``operator_explicit`` directives start
    elevated; meditation's relevance-check may downgrade a stale one to a
    normal kind (``decision``), after which it rejoins the decay pool (§4.2).
    """

    kind: str = KIND_PREFERENCE
    importance: float = 0.0
    #: Reinforcement count — how many times this fact has recurred across
    #: sessions (the 4-store importance signal, brief §store roster). Starts at
    #: 1; the meditation merge increments it (and derives ``importance`` from it)
    #: so a repeated directive ranks higher, not lower.
    repeat_count: int = 1

    def __post_init__(self) -> None:
        self.store_name = STORE_MUST_REMEMBER

    def validate(self) -> None:
        super().validate()
        if self.kind not in VALID_KINDS:
            raise EntryError(
                f"must_remember.kind must be one of {VALID_KINDS}; "
                f"got {self.kind!r}."
            )
        if isinstance(self.importance, bool) or not isinstance(
            self.importance, (int, float)
        ):
            raise EntryError("must_remember.importance must be a number.")
        if isinstance(self.repeat_count, bool) or not isinstance(
            self.repeat_count, int
        ) or self.repeat_count < 1:
            raise EntryError("must_remember.repeat_count must be an int >= 1.")

    def frontmatter(self) -> dict[str, Any]:
        fm = super().frontmatter()
        fm.update({
            "kind": self.kind,
            "importance": float(self.importance),
            "repeat_count": self.repeat_count,
        })
        return fm


# ----- topics (ADR 0007) ----------------------------------------------------

# Slug shape enforced on write: lowercase, digits, single-hyphen separated.
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def topic_slug(name: str) -> str:
    """Derive the canonical topic slug from a human topic *name*.

    Lowercased; runs of non-alphanumerics collapse to single hyphens; leading
    and trailing hyphens are stripped. An empty result (a name with no
    alphanumerics at all) raises ``EntryError`` — a topic must be addressable.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        raise EntryError(f"topic name {name!r} yields an empty slug.")
    return slug


@dataclass
class TopicEntry(BaseEntry):
    """A named, growing body of project knowledge (ADR 0007).

    ``slug`` is the stable address the sweep contract routes new details to;
    ``summary`` is the one-to-two-sentence index line (the ONLY part loaded
    at session start); the entry ``text`` is the detail body — dated ``##
    YYYY-MM-DD`` sections of appended facts. ``last_used`` is the freshness
    anchor (`last_touched`): it orders the index and, past
    ``topics.forget_days``, makes the topic forget-eligible. ``touch_count``
    grows every time a sweep routes new material here — a repeat signal for
    compaction's keep-ranking.
    """

    name: str = ""
    slug: str = ""
    summary: str = ""
    touch_count: int = 1

    def __post_init__(self) -> None:
        self.store_name = STORE_TOPICS
        if not self.slug and self.name.strip():
            self.slug = topic_slug(self.name)

    def validate(self) -> None:
        super().validate()
        if not isinstance(self.name, str) or not self.name.strip():
            raise EntryError("topic.name must be a non-empty string.")
        if not isinstance(self.slug, str) or not _SLUG_RE.match(self.slug):
            raise EntryError(
                f"topic.slug must be a lowercase hyphenated slug; "
                f"got {self.slug!r}."
            )
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise EntryError("topic.summary must be a non-empty string.")
        if isinstance(self.touch_count, bool) or not isinstance(
            self.touch_count, int
        ) or self.touch_count < 1:
            raise EntryError("topic.touch_count must be an int >= 1.")

    def frontmatter(self) -> dict[str, Any]:
        fm = super().frontmatter()
        fm.update({
            "name": self.name,
            "slug": self.slug,
            "summary": self.summary,
            "touch_count": self.touch_count,
        })
        return fm


# ----- dict <-> entry (the load/save bridge) -------------------------------

# Subclass per store name, for ``entry_from_frontmatter`` dispatch.
_ENTRY_CLASSES: dict[str, type[BaseEntry]] = {
    STORE_SKILLS: SkillEntry,
    STORE_MUST_REMEMBER: MustRememberEntry,
    STORE_TOPICS: TopicEntry,
}


def entry_class_for(store_name: str) -> type[BaseEntry]:
    """Return the entry dataclass for *store_name* (raises on unknown)."""
    try:
        return _ENTRY_CLASSES[store_name]
    except KeyError:
        raise EntryError(f"unknown store_name: {store_name!r}") from None


def _coerce_int(value: Any, field: str) -> int:
    """``int(value)`` but a bad value raises the contracted ``EntryError``.

    Frontmatter comes from external files; a corrupt numeric (``usage_count:
    not-an-int``) must surface as ``EntryError`` so ``load`` can skip the one
    bad entry rather than let a raw ``ValueError`` abort the whole store.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        raise EntryError(f"{field} must be an integer, got {value!r}.") from None


def _coerce_float(value: Any, field: str) -> float:
    """``float(value)`` but a bad value raises the contracted ``EntryError``."""
    try:
        return float(value)
    except (TypeError, ValueError):
        raise EntryError(f"{field} must be a number, got {value!r}.") from None


def entry_from_frontmatter(
    store_name: str, fm: dict[str, Any], text: str
) -> BaseEntry:
    """Reconstruct a typed entry from a parsed frontmatter dict + body text.

    Used by ``store.load``. Unknown frontmatter keys are ignored (forward
    compatible); missing structured fields fall back to the dataclass
    default and are caught by ``validate()`` if required.
    """
    cls = entry_class_for(store_name)
    base_kwargs: dict[str, Any] = {
        "id": str(fm.get("id") or new_id()),
        "text": text,
        "created_at": str(fm.get("created_at", "")),
        "last_used": str(fm.get("last_used", "")),
        "source": str(fm.get("source", "")),
    }
    if cls is SkillEntry:
        return SkillEntry(
            name=str(fm.get("name", "")),
            trigger=str(fm.get("trigger", "")),
            procedure=str(fm.get("procedure", "")),
            usage_count=_coerce_int(fm.get("usage_count", 0), "skill.usage_count"),
            importance=_coerce_float(fm.get("importance", 0.0), "skill.importance"),
            **base_kwargs,
        )
    if cls is MustRememberEntry:
        return MustRememberEntry(
            kind=normalize_kind(str(fm.get("kind", KIND_PREFERENCE))),
            importance=_coerce_float(
                fm.get("importance", 0.0), "must_remember.importance"
            ),
            repeat_count=_coerce_int(
                fm.get("repeat_count", 1), "must_remember.repeat_count"
            ),
            **base_kwargs,
        )
    # Only TopicEntry remains.
    return TopicEntry(
        name=str(fm.get("name", "")),
        slug=str(fm.get("slug", "")),
        summary=str(fm.get("summary", "")),
        touch_count=_coerce_int(fm.get("touch_count", 1), "topic.touch_count"),
        **base_kwargs,
    )

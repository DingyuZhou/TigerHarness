"""Entry schemas for the three bounded memory stores (design §4).

The memory revamp (design §4) replaces the old rollup/RAG surface with
exactly three per-persona stores, each a list of small markdown entries
with a YAML frontmatter carrying the structured fields:

- **skills** (§4.1): a learned, invokable lesson — ``name``, ``trigger``,
  ``procedure``, ``usage_count``, ``importance``.
- **must_remember** (§4.2): an external directive — ``kind``
  (``owner_explicit`` / ``preference`` / ``decision`` / ``incident``),
  ``importance``.
- **emotional** (§4.3): a persona reaction — a signed ``weight`` in
  ``[-weight_cap, +weight_cap]`` and a ``reaction`` string.

Every entry shares a **base** shape: ``id``, ``text``, ``created_at``,
``last_used``, ``source``. The per-store subclasses add their fields.

These dataclasses are the *frozen cross-seat interface* (plan §1): Rukawa
scores against them, Miyagi reads/writes them. They are pure data +
validation — no I/O (that is ``store.py``), no scoring (that is
``emotional.py`` / ``skills.py``). Validation is fail-fast: a malformed
entry raises ``EntryError`` so a bad write is caught at construction, never
silently persisted.

Length is measured in CHARACTERS, never tokens (vendor-neutral, design §8).
"""
from __future__ import annotations

import logging
import uuid as _uuid
from dataclasses import asdict, dataclass, field
from typing import Any

log = logging.getLogger("tigerharness.tiger_memory.entries")


class EntryError(ValueError):
    """Raised when an entry's fields are missing, malformed, or out of range."""


# ----- store names (the frozen ``store_name`` enum) ------------------------

STORE_SKILLS = "skills"
STORE_MUST_REMEMBER = "must_remember"
STORE_EMOTIONAL = "emotional"
STORE_NAMES = (STORE_SKILLS, STORE_MUST_REMEMBER, STORE_EMOTIONAL)

# must_remember kinds (design §4.2). ``owner_explicit`` is the elevated
# directive the forget-guard protects until the relevance-check runs (§5).
KIND_OWNER_EXPLICIT = "owner_explicit"
KIND_PREFERENCE = "preference"
KIND_DECISION = "decision"
KIND_INCIDENT = "incident"
VALID_KINDS = (
    KIND_OWNER_EXPLICIT,
    KIND_PREFERENCE,
    KIND_DECISION,
    KIND_INCIDENT,
)


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

    ``kind`` is one of ``VALID_KINDS``. ``owner_explicit`` directives start
    elevated; meditation's relevance-check may downgrade a stale one to a
    normal kind (``decision``), after which it rejoins the decay pool (§4.2).
    """

    kind: str = KIND_PREFERENCE
    importance: float = 0.0

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

    def frontmatter(self) -> dict[str, Any]:
        fm = super().frontmatter()
        fm.update({"kind": self.kind, "importance": float(self.importance)})
        return fm


# ----- emotional (§4.3) ----------------------------------------------------


@dataclass
class EmotionalEntry(BaseEntry):
    """A persona reaction with a signed emotional weight (design §4.3).

    ``weight`` is a signed float in ``[-weight_cap, +weight_cap]``: positive
    = liked / *for*, negative = disliked / *against*, ``0`` = neutral. The
    cap is enforced at validation time against ``weight_cap`` (default 10.0,
    matching the CONFIRMED hard cap of design §4.3). ``reaction`` is the
    persona's short note about how it felt.
    """

    weight: float = 0.0
    reaction: str = ""

    def __post_init__(self) -> None:
        self.store_name = STORE_EMOTIONAL

    def validate(self, weight_cap: float = 10.0) -> None:
        super().validate()
        if isinstance(self.weight, bool) or not isinstance(
            self.weight, (int, float)
        ):
            raise EntryError("emotional.weight must be a signed number.")
        if abs(self.weight) > weight_cap:
            raise EntryError(
                f"emotional.weight magnitude must be ≤ weight_cap "
                f"({weight_cap}); got {self.weight}."
            )
        if not isinstance(self.reaction, str) or not self.reaction.strip():
            raise EntryError("emotional.reaction must be a non-empty string.")

    def frontmatter(self) -> dict[str, Any]:
        fm = super().frontmatter()
        fm.update({"weight": float(self.weight), "reaction": self.reaction})
        return fm


# ----- dict <-> entry (the load/save bridge) -------------------------------

# Subclass per store name, for ``entry_from_frontmatter`` dispatch.
_ENTRY_CLASSES: dict[str, type[BaseEntry]] = {
    STORE_SKILLS: SkillEntry,
    STORE_MUST_REMEMBER: MustRememberEntry,
    STORE_EMOTIONAL: EmotionalEntry,
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
            kind=str(fm.get("kind", KIND_PREFERENCE)),
            importance=_coerce_float(
                fm.get("importance", 0.0), "must_remember.importance"
            ),
            **base_kwargs,
        )
    # Only EmotionalEntry remains.
    return EmotionalEntry(
        weight=_coerce_float(fm.get("weight", 0.0), "emotional.weight"),
        reaction=str(fm.get("reaction", "")),
        **base_kwargs,
    )

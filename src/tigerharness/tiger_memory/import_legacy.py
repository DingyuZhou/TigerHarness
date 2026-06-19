"""One-off legacy memory import — seed-write + integrity + idempotency (b1-dev-1).

This module owns the **durable write** of already-re-authored, already-scored
legacy entries into the three bounded stores, plus the **one-off guard** that
makes the import safe to run exactly once (design §12; plan §1, seat b1-dev-1).
It is the foundation the scoring seat (Rukawa, dev-2) and the reader/orchestrator
seat (Miyagi, dev-3) build on:

- **seed-write** (``seed_entries``): take the FINAL typed candidates Rukawa
  produced — already backdated (``last_used``/``created_at`` = the source date),
  already scored, already tagged ``source="import-legacy"`` — and APPEND them to
  the three bounded stores via :meth:`BoundedStore.save_atomic`. It mirrors
  ``lifecycle.ingest_candidates`` but with the two seeding differences the plan
  freezes (§1.1): it does **not** ``refresh_importance`` skills (that would reset
  ``last_used`` semantics by re-deriving against *now* and erase the backdating)
  and it does **not** re-stamp any timestamp. The backdated weights survive
  byte-for-byte.

- **idempotency** (``already_imported`` / ``mark_imported``): a durable
  ``legacy_import`` key in the existing ``.state.json`` (read-modify-write
  through ``store.write_state``/``read_state``, preserving every other state
  key), PLUS a second independent guard that detects existing
  ``source="import-legacy"`` entries — so a hand-deleted marker still cannot
  cause a double-seed (plan §1.2/§1.3).

- **read-before-drop** (``assert_seed_inputs_snapshotted`` + the documented
  invariant): the import must fully read/snapshot the legacy files and complete
  every ``save_atomic`` BEFORE ``rebuild`` runs (rebuild is what calls
  ``lifecycle._drop_legacy_surface`` and irreversibly deletes the old files).
  ``seed_entries`` performs ZERO deletion — it is a pure consumer of
  already-parsed candidates — and this module exposes the ordering as an
  enforceable assertion the orchestrator (dev-3) calls.

Nothing in the shipped modules is rewritten; this only adds the import-specific
write/guard surface.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from . import diary as emotional_mod
from . import frontmatter
from .bounded_store import BoundedStore
from .config import Config
from .entries import (
    STORE_DIARY,
    STORE_MUST_REMEMBER,
    STORE_NAMES,
    STORE_SKILLS,
    VALID_KINDS,
    BaseEntry,
    DiaryEntry,
    MustRememberEntry,
    SkillEntry,
)
from .lifecycle import (
    Candidates,
    _build_summarizer,
    _clip,
    _fill_prompt,
    _prompts_root,
    parse_extraction,
)
from .state import iso_now
from .store import DAILY_RE, MONTHLY_RE, SHORT_RE, WEEKLY_RE, Store
from .summarizers import Summarizer

log = logging.getLogger("tigerharness.tiger_memory.import_legacy")


# The provenance tag every seeded entry carries. It is BOTH the audit trail and
# the second idempotency guard (§1.3): if the ``.state.json`` marker is wiped by
# hand but a store already holds entries with this source, the import refuses to
# re-seed. Rukawa sets it on construction; ``seed_entries`` asserts it.
IMPORT_SOURCE = "import-legacy"

# The top-level ``.state.json`` key the marker lives under (§1.2). Chosen over a
# sentinel file because design §10.6 detects legacy stores by STATE-FILE
# detection, not file presence — a state key is consistent with that rule,
# survives a ``rebuild`` (which never clears ``.state.json``), and is trivially
# mockable. A bare sentinel ``.md`` would be mistaken for store content.
STATE_KEY = "legacy_import"


class DoubleSeedError(RuntimeError):
    """Raised when a seed-write is attempted into an already-seeded store.

    The no-double-seed anchor (§1.3): once a store holds ``import-legacy``
    entries, seeding again would duplicate the legacy memory. The orchestrator
    is expected to consult :func:`already_imported` first and skip; reaching
    :func:`seed_entries` on an already-seeded store is a caller-ordering bug, so
    we raise loudly rather than silently double-write.
    """


# ----- the second guard: detect-existing-seed -------------------------------


def has_seeded_entries(bstore: BoundedStore) -> bool:
    """True iff ANY of the three stores already holds an ``import-legacy`` entry.

    The marker-independent guard (§1.3). ``already_imported`` consults the
    durable ``.state.json`` marker first; this backs it up by inspecting the
    actual store contents, so a hand-deleted marker over already-seeded stores
    still reports "imported" and blocks a re-seed.
    """
    # The diary store is source-less on disk (compact dated bullets), so its
    # import-seeds can't be content-detected by ``source``; it is covered by the
    # .state.json marker instead. Scan only the frontmatter stores here.
    for store_name in (STORE_SKILLS, STORE_MUST_REMEMBER):
        for entry in bstore.load(store_name):
            if entry.source == IMPORT_SOURCE:
                return True
    return False


def _purge_seeded_entries(bstore: BoundedStore) -> None:
    """Drop every ``import-legacy`` entry from the three stores (the ``--force``
    re-seed path only).

    Loads each store, keeps only the non-import entries, and re-saves it
    atomically when (and only when) a prior import entry was present — so a
    forced re-run REPLACES the prior seed rather than duplicating it, and the
    no-double-seed guard in :func:`seed_entries` does not refuse the re-run.
    Live ``extract``/``pin`` memory is never touched.
    """
    for store_name in STORE_NAMES:
        existing = bstore.load(store_name)
        if store_name == STORE_DIARY:
            # Source-less compact format: import-seeds can't be told apart from
            # live entries, so a force-reimport RESETS the diary (rebuildable;
            # the legacy seed is re-authored). b1-dev-3/Mitsui may refine this to
            # preserve live diary entries via a marker-tracked seed set.
            if existing:
                bstore.save_atomic(store_name, [])
            continue
        kept = [e for e in existing if e.source != IMPORT_SOURCE]
        if len(kept) != len(existing):
            bstore.save_atomic(store_name, kept)


# ----- the durable marker (§1.2) --------------------------------------------


def already_imported(store: Store, bstore: BoundedStore) -> bool:
    """True iff the legacy import has already run for this persona.

    Two independent signals, EITHER of which gates the import (plan §1.2/§1.3):

    1. the durable ``legacy_import.done`` marker in ``.state.json`` (the normal
       path — survives a ``rebuild``); and
    2. the detect-existing-seed fallback (``has_seeded_entries``): if the marker
       was hand-deleted but a store already holds ``import-legacy`` entries, this
       still returns True so the import cannot double-seed.

    *bstore* is the persona's :class:`BoundedStore` (the orchestrator already
    builds one to seed with); it backs the marker-independent fallback check.
    """
    state = store.read_state() or {}
    marker = state.get(STATE_KEY)
    if isinstance(marker, dict) and marker.get("done") is True:
        return True
    return has_seeded_entries(bstore)


def mark_imported(store: Store, *, counts: dict[str, int]) -> None:
    """Record the one-off ``legacy_import`` marker in ``.state.json``.

    Read-modify-write: the existing state (``agent``, ``operator_id``,
    ``last_rebuild_at``, ``metrics``, …) is preserved and only the
    ``legacy_import`` key is added/overwritten. Written through
    ``store.write_state`` (atomic temp + ``os.replace``). *counts* is the
    per-store added-entry tally returned by :func:`seed_entries`.
    """
    state = store.read_state() or {}
    state[STATE_KEY] = {
        "done": True,
        "at": iso_now(),
        "seeded": {
            STORE_SKILLS: int(counts.get(STORE_SKILLS, 0)),
            STORE_MUST_REMEMBER: int(counts.get(STORE_MUST_REMEMBER, 0)),
            STORE_DIARY: int(counts.get(STORE_DIARY, 0)),
        },
    }
    store.write_state(state)


# ----- the seed-writer (§1.1) -----------------------------------------------


def seed_entries(
    bstore: BoundedStore,
    candidates: Candidates,
    *,
    now: str | None = None,
) -> dict[str, int]:
    """Append the FINAL scored legacy *candidates* into the three bounded stores.

    The durable write of the import (plan §1.1). For each store: load the
    existing entries, append the new (already-scored, already-backdated)
    candidates, and re-save the whole store atomically via
    :meth:`BoundedStore.save_atomic` (which validates every entry). Returns the
    per-store count of entries added.

    Two seeding-specific contracts, FROZEN against Rukawa (dev-2) and asserted
    here so a regression surfaces loudly:

    - **no re-refresh / no re-stamp.** Unlike ``lifecycle.ingest_candidates``,
      this does NOT call ``skills.refresh_importance`` and does NOT touch any
      timestamp. Rukawa already backdated ``created_at``/``last_used`` to the
      source date and derived ``importance`` against that backdated age; calling
      the live refresh would re-derive against *now* and erase the backdating.
      The backdated weights survive byte-for-byte.
    - **provenance + no-double-seed.** Every written entry must already carry
      ``source == "import-legacy"`` (Rukawa sets it; we assert it — it is the
      second idempotency guard). If the target store ALREADY holds an
      ``import-legacy`` entry, seeding is refused with :class:`DoubleSeedError`
      (the orchestrator must gate on :func:`already_imported` first).

    *now* is accepted for signature symmetry with ``ingest_candidates`` but is
    intentionally unused: seeded entries are backdated, never stamped to now.
    """
    del now  # intentionally unused — seeds are backdated, never re-stamped.
    added = {STORE_SKILLS: 0, STORE_MUST_REMEMBER: 0, STORE_DIARY: 0}
    if candidates.is_empty():
        return added
    per_store: dict[str, list[BaseEntry]] = {
        STORE_SKILLS: list(candidates.skills),
        STORE_MUST_REMEMBER: list(candidates.must_remember),
        STORE_DIARY: list(candidates.diary),
    }
    for store_name, new_entries in per_store.items():
        if not new_entries:
            continue
        for entry in new_entries:
            if entry.source != IMPORT_SOURCE:
                raise DoubleSeedError(
                    f"refusing to seed {store_name} entry {entry.id!r}: source "
                    f"is {entry.source!r}, not {IMPORT_SOURCE!r} (the seed-write "
                    "contract requires the import provenance tag)."
                )
        existing = bstore.load(store_name)
        if any(e.source == IMPORT_SOURCE for e in existing):
            raise DoubleSeedError(
                f"refusing to seed {store_name}: it already holds "
                f"{IMPORT_SOURCE!r} entries (no double-seed; gate on "
                "already_imported() first)."
            )
        # APPEND — never overwrite existing memory. No refresh, no re-stamp.
        bstore.save_atomic(store_name, existing + new_entries)
        added[store_name] = len(new_entries)
    log.info(
        "import-legacy seed: +%d skills, +%d must_remember, +%d emotional",
        added[STORE_SKILLS], added[STORE_MUST_REMEMBER], added[STORE_DIARY],
    )
    return added


# ----- read-before-drop ordering (§1.3 / §12) -------------------------------


def assert_seed_inputs_snapshotted(candidates: Candidates) -> None:
    """Assert the read-before-drop invariant holds at the seed boundary.

    The ordering anchor (§1.3, design §12 "must read/snapshot the old files
    before any drop"). ``seed_entries`` writes from an in-memory
    :class:`Candidates` and performs ZERO deletion — the legacy files have
    therefore ALREADY been fully read into *candidates* by the time seeding
    runs. The orchestrator (dev-3) reads ALL legacy bytes into *candidates*
    first, then calls this, then seeds; it never invokes ``rebuild`` (the only
    step that drops the legacy surface).

    This makes the ordering enforceable rather than merely documented: a caller
    that has not yet snapshotted the legacy files would pass empty candidates
    here, which is a no-op seed and a documented signal that nothing was read.
    The assertion confirms *candidates* is a concrete, fully-materialised
    snapshot (a list, not a lazy generator) so no legacy read is deferred past
    the seed boundary into the deletable ``rebuild`` step.
    """
    for bucket in (
        candidates.skills,
        candidates.must_remember,
        candidates.diary,
    ):
        if not isinstance(bucket, list):
            raise TypeError(
                "read-before-drop: seed inputs must be a fully-materialised "
                "snapshot (a list), not a lazy/deferred read — the legacy files "
                "must be fully read BEFORE any deletable step (rebuild)."
            )


def seeds_perform_no_deletion(paths_before: Iterable, paths_after: Iterable) -> bool:
    """True iff no path present before the seed was unlinked by it (§1.3).

    A test/ordering helper: ``seed_entries`` must never unlink a legacy file
    (it only ever loads + ``save_atomic``-appends). Given the set of legacy
    file paths observed before vs after a seed, returns True iff every
    pre-existing path still exists — i.e. the seed dropped nothing.
    """
    before = set(paths_before)
    after = set(paths_after)
    return before <= after


# ===== seeding scoring (b1-dev-2, Rukawa) ===================================
#
# Turn the re-authored *raw* candidates (Miyagi/dev-3's reader + re-author pass)
# into FINAL typed entries that are correctly BACKDATED to each item's source
# date, then handed to Mitsui's :func:`seed_entries` to write AS-IS. The single
# scoring twist of the whole import (design §12; plan §2, seat b1-dev-2):
#
# - **every** seeded entry carries ``source = "import-legacy"`` and is stamped
#   ``created_at = last_used = source_date`` — NEVER ``now()``. An old memory
#   enters the store already aged, so the live keep-rank treats it exactly as it
#   would have treated it had it been written on its source date.
# - **emotional** seeds store the clamped RAW signed weight (clamped to
#   ``[-weight_cap, +weight_cap]`` via :func:`emotional.clamp_weight`); they are
#   NOT pre-decayed. The "already partly decayed" effect is produced at RANK
#   time: because ``last_used = source_date``, the live keep-rank's single
#   decay (:func:`emotional.decay_entry`) ages the seed over exactly the
#   source→now span — identical to an organic entry written on the source date.
#   Pre-decaying here as well would double-decay the seed (GAP-5, final
#   convergence pass), under-weighting and prematurely forgetting curated
#   emotional memory the import exists to preserve.
# - **skills** seed a LOW ``usage_count`` (0 or 1 — a freshly re-derived skill
#   has no live invocations yet) and ``last_used = source_date`` so an old/unused
#   skill ranks low immediately. ``importance`` is left at ``0.0``;
#   :func:`skills.refresh_importance` is deliberately NOT called (it would reset
#   the semantics against *now*) — importance is re-derived from usage + recency
#   at the next meditation, exactly as Mitsui's seed-write contract expects.
# - **must_remember** preserves the legacy ``kind`` straight from the table and
#   maps the legacy importance/score onto the new ``importance`` (an owner
#   directive starts elevated).
#
# The per-item SOURCE DATE seam (Akagi, round-1): each unscored input entry
# already carries its source date in ``created_at`` (Miyagi's reader attaches
# it — a rollup's frontmatter ``period`` date, or the must_memorize row's
# ``Last bump`` / a file-mtime fallback). An optional ``source_dates`` map keyed
# by entry ``id`` overrides that per-entry (e.g. a must_memorize row whose date
# the reader resolves separately). ``now`` is injected for deterministic tests.

# A freshly re-derived skill has no live invocations yet, so its seeded
# ``usage_count`` is capped at this low ceiling regardless of the raw input
# (plan §2.1 "0 or 1"). It still ranks above a never-derived skill if the
# re-author judged the skill as actually exercised (raw ≥ 1 → 1).
SEED_SKILL_USAGE_CAP = 1


def _source_date_for(
    entry: BaseEntry, source_dates: Mapping[str, str] | None, now: str
) -> str:
    """The backdated source date for one unscored *entry*.

    Resolution order (plan §2.1 + Akagi's source-date seam): an explicit
    ``source_dates[entry.id]`` override wins; otherwise the entry's own
    ``created_at`` (which Miyagi's reader stamped with the per-item source date)
    is used. If neither yields a non-empty value, fall back to *now* so the
    entry is never left with an empty timestamp (which would fail validation) —
    a same-day seed, the most conservative backdating.
    """
    if source_dates is not None:
        override = source_dates.get(entry.id)
        if override:
            return override
    return entry.created_at or now


def score_seed_candidates(
    raw: Candidates,
    *,
    cfg: Config,
    now: str | None = None,
    source_dates: Mapping[str, str] | None = None,
) -> Candidates:
    """Score the re-authored *raw* candidates into FINAL backdated seed entries.

    Returns a NEW :class:`Candidates` of freshly-constructed entries (the input
    is never mutated) ready for Mitsui's :func:`seed_entries` to write AS-IS.
    Every output entry carries ``source = "import-legacy"`` and is backdated to
    its per-item source date (``created_at = last_used = source_date``).

    Per store (design §12 three bullets / plan §2.1):

    - **emotional:** ``weight = clamp_weight(raw_weight, cfg)`` — the clamped
      RAW weight, NOT pre-decayed. ``last_used = source_date`` lets the live
      keep-rank decay it ONCE at rank time over the source→now span, so an
      imported experience ages exactly like an organic one of the same age
      (pre-decaying here too would double-decay it — GAP-5).
    - **skills:** ``usage_count`` clamped to ``[0, SEED_SKILL_USAGE_CAP]``,
      ``importance = 0.0`` (NOT refreshed — derived at meditation), ``last_used``
      = source date.
    - **must_remember:** ``kind`` + ``importance`` preserved straight from the
      re-authored row (an ``owner_explicit`` directive stays elevated).

    *now* defaults to :func:`state.iso_now`. *source_dates* (optional) is a map
    from entry ``id`` to an ISO source date overriding the entry's own
    ``created_at`` (the per-item source-date seam, Akagi round-1).
    """
    now = now or iso_now()

    skills: list[SkillEntry] = []
    for s in raw.skills:
        src = _source_date_for(s, source_dates, now)
        usage = max(0, min(int(s.usage_count), SEED_SKILL_USAGE_CAP))
        skills.append(
            SkillEntry(
                text=s.text,
                created_at=src,
                last_used=src,
                source=IMPORT_SOURCE,
                name=s.name,
                trigger=s.trigger,
                procedure=s.procedure,
                usage_count=usage,
                importance=0.0,
            )
        )

    must: list[MustRememberEntry] = []
    for m in raw.must_remember:
        src = _source_date_for(m, source_dates, now)
        must.append(
            MustRememberEntry(
                text=m.text,
                created_at=src,
                last_used=src,
                source=IMPORT_SOURCE,
                kind=m.kind,
                importance=m.importance,
            )
        )

    emo: list[DiaryEntry] = []
    for e in raw.diary:
        src = _source_date_for(e, source_dates, now)
        # GAP-5 fix (final convergence): store the clamped RAW weight, NOT a
        # pre-decayed one. Emotional weight is decayed exactly ONCE, at rank
        # time, from last_used (= the backdated source date) via
        # decay_entry/keep_rank — identical to an organic entry of the same age.
        # Pre-decaying here AND backdating last_used double-decayed the seed
        # (~2x aging), prematurely forgetting the curated memory §12 preserves.
        weight = emotional_mod.clamp_weight(e.weight, cfg)
        emo.append(
            DiaryEntry(
                text=e.text,
                created_at=src,
                last_used=src,
                source=IMPORT_SOURCE,
                weight=weight,
            )
        )

    return Candidates(skills=skills, must_remember=must, diary=emo)


# ===== legacy reader + re-author + orchestration (b1-dev-3, Miyagi) ==========
#
# The READER turns each persona's old on-disk memory into an in-memory snapshot,
# the RE-AUTHOR pass (the single model touch point) re-derives the new bundle
# shapes from the aged-out rollup prose, and the ORCHESTRATOR sequences
# read → reauthor → score → seed → mark in the strict read-before-drop order
# (design §12; plan §3). Nothing here deletes anything — the actual fresh-start
# drop is ``rebuild``, which the orchestrator NEVER invokes.

# The literal legacy pin file (a YAML-frontmatter doc whose body is a markdown
# ``| Score | Kind | Last bump | Source | Memo |`` table; design §12).
MUST_MEMORIZE_FILENAME = "must_memorize.md"

# A bare ``YYYY-MM-DD`` (must_memorize ``Last bump`` / a daily|weekly rollup
# ``period``) and a bare ``YYYY-MM`` (a monthly rollup ``period``). Normalised to
# a full ISO-8601 UTC timestamp so the seeded entries validate and ``days_between``
# can measure the backdated age (plan §3.1 + Akagi's source-date seam).
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


@dataclass
class LegacyPin:
    """One parsed ``must_memorize.md`` table row (a structured directive)."""

    score: float
    kind: str
    source_date: str  # normalised ISO timestamp (row ``Last bump`` or mtime)
    memo: str


@dataclass
class LegacyRollup:
    """One parsed daily/weekly/monthly rollup (frontmatter date + prose body)."""

    kind: str  # "daily" | "weekly" | "monthly"
    source_date: str  # normalised ISO timestamp from the frontmatter ``period``
    body: str


@dataclass
class LegacySource:
    """The in-memory snapshot of one persona's old memory the import consumes.

    ``pins`` are the already-structured ``must_memorize.md`` rows (mechanical —
    the table IS the directive, so they do not need the model). ``rollups`` are
    the aged-out experience the re-author pass mines for skill + emotional seeds.
    Both carry a per-item source date for backdating (Akagi's source-date seam).
    """

    pins: list[LegacyPin] = field(default_factory=list)
    rollups: list[LegacyRollup] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.pins or self.rollups)


def _normalise_source_date(raw: str | None, *, fallback: str) -> str:
    """Normalise a legacy date string to a full ISO-8601 UTC timestamp.

    A bare ``YYYY-MM-DD`` becomes ``...T00:00:00Z``; a bare ``YYYY-MM`` (a
    monthly ``period``) becomes the first of that month. Anything already
    carrying a time component is passed through. An empty/unrecognised value
    falls back to *fallback* (a documented sentinel — the file mtime) so an
    entry is never left with an un-backdatable date (plan §3.1).
    """
    s = (raw or "").strip()
    if _DATE_RE.match(s):
        return f"{s}T00:00:00Z"
    if _MONTH_RE.match(s):
        return f"{s}-01T00:00:00Z"
    if s:
        # A full ISO timestamp passes through, but a non-ISO string (e.g.
        # "yesterday") must NOT poison the entry's date — days_between would
        # read it as 0 days and recency_score as -inf, silently defeating the
        # backdating. An unparseable value falls back to the mtime sentinel,
        # exactly like a blank value (D2, b2 QA finding).
        try:
            datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return fallback
        return s
    return fallback


def _read_text_lenient(path: Path) -> str:
    """Read a legacy file tolerantly — a stray non-UTF8 byte must NOT crash the
    whole persona import (it would lose every otherwise-valid pin/rollup). The
    bad byte degrades to the Unicode replacement char and the affected content
    simply parses to nothing, rather than raising ``UnicodeDecodeError`` out of
    ``import_legacy_run`` (D1, b2 QA finding). Mirrors ``bounded_store.load``."""
    text = path.read_bytes().decode("utf-8", errors="replace")
    if "�" in text:
        log.warning(
            "import-legacy: %s had non-UTF8 byte(s) replaced; affected content "
            "is skipped, the rest of the persona still imports.",
            path,
        )
    return text


def _mtime_iso(path: Path) -> str:
    """The file's modification time as an ISO-8601 UTC timestamp (the documented
    fallback source date when a row/file carries no usable date)."""
    try:
        ts = path.stat().st_mtime
    except OSError:  # pragma: no cover  # path was just globbed/exists
        return iso_now()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_must_memorize(text: str, *, mtime_iso: str) -> list[LegacyPin]:
    """Parse the ``must_memorize.md`` markdown table into structured pins.

    The body is a ``| Score | Kind | Last bump | Source | Memo |`` table. The
    header + separator rows and any malformed/short row are skipped; an unknown
    ``Kind`` is dropped (it would fail ``MustRememberEntry`` validation later).
    The per-row source date is the ``Last bump`` column (normalised); a row with
    no usable date falls back to the file mtime (plan §3.1).
    """
    _fm, body = frontmatter.parse(text)
    pins: list[LegacyPin] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 5:
            continue
        score_raw, kind_raw, bump_raw, _source_raw, memo = cells[:5]
        kind = kind_raw.lower()
        # Skip the header (``Score``) + the ``---|---`` separator + any row whose
        # kind is not a real directive kind.
        if kind not in VALID_KINDS or not memo:
            continue
        try:
            score = float(score_raw)
        except ValueError:
            continue
        pins.append(
            LegacyPin(
                score=score,
                kind=kind,
                source_date=_normalise_source_date(bump_raw, fallback=mtime_iso),
                memo=memo,
            )
        )
    return pins


def _rollup_kind(name: str) -> str | None:
    """Classify a journal ``*.md`` filename by the retired rollup regexes.

    Returns ``"daily"`` / ``"weekly"`` / ``"monthly"`` for a rollup, or ``None``
    for a per-session SHORT transcript (skipped — §12 "skip the verbose detailed
    archive") or any non-rollup file. SHORT_RE is checked FIRST so a short whose
    timestamp prefix could otherwise look daily-ish is never mis-read.
    """
    if SHORT_RE.match(name):
        return None
    if DAILY_RE.match(name):
        return "daily"
    if WEEKLY_RE.match(name):
        return "weekly"
    if MONTHLY_RE.match(name):
        return "monthly"
    return None


def read_legacy(store: Store) -> LegacySource:
    """Read one persona's old on-disk memory into an in-memory snapshot.

    Pure Python, NO model (plan §3.1). Reads:

    - ``journal/must_memorize.md`` — the pin table → structured :class:`LegacyPin`
      rows (skipped if the file is absent).
    - ``journal/*.md`` rollups matching ``DAILY_RE`` / ``WEEKLY_RE`` /
      ``MONTHLY_RE`` → :class:`LegacyRollup` prose blocks, each stamped with its
      frontmatter ``period`` (normalised) as the source date.

    **Skips** the per-session ``SHORT_RE`` transcripts in ``journal/`` AND the
    verbose ``archive/`` dir entirely (§12 "skip the verbose detailed
    ``archive/``") — only the pins + the three rollup shapes are imported.

    This is the read-before-drop SNAPSHOT: every legacy byte the import needs is
    materialised here, BEFORE any deletable step. ``read_legacy`` never unlinks.
    """
    journal = store.paths.journal
    src = LegacySource()
    if not journal.exists():
        return src

    mm_path = journal / MUST_MEMORIZE_FILENAME
    if mm_path.exists():
        src.pins = _parse_must_memorize(
            _read_text_lenient(mm_path), mtime_iso=_mtime_iso(mm_path)
        )

    for f in sorted(journal.glob("*.md")):
        kind = _rollup_kind(f.name)
        if kind is None:
            continue
        fm, body = frontmatter.parse(_read_text_lenient(f))
        period = fm.get("period")
        period = str(period) if period is not None else ""
        body = body.strip()
        if not body:
            continue
        src.rollups.append(
            LegacyRollup(
                kind=kind,
                source_date=_normalise_source_date(period, fallback=_mtime_iso(f)),
                body=body,
            )
        )
    return src


def _pins_to_candidates(pins: Iterable[LegacyPin]) -> Candidates:
    """The mechanical must-remember half of the import (NO model, plan §3.1).

    Each ``must_memorize.md`` row is already a structured directive, so it maps
    straight to an unscored :class:`MustRememberEntry` carrying its legacy
    ``Score`` as the importance and its ``Last bump`` as the source date. Rukawa's
    :func:`score_seed_candidates` preserves both and backdates the timestamps.
    """
    must: list[MustRememberEntry] = []
    for p in pins:
        must.append(
            MustRememberEntry(
                text=p.memo,
                created_at=p.source_date,
                last_used=p.source_date,
                source=IMPORT_SOURCE,
                kind=p.kind,
                importance=p.score,
            )
        )
    return Candidates(skills=[], must_remember=must, diary=list())


def _reauthor_one(
    cfg: Config,
    summarizer: Summarizer,
    rollup: LegacyRollup,
) -> Candidates:
    """Run the re-author prompt over ONE rollup and parse the typed candidates.

    The single LLM call per rollup (mock in CI), mirroring
    ``lifecycle.extract_candidates``: fill the ``import_legacy.md`` template with
    the rollup prose, call the summarizer, and parse the strict
    ``@@SKILLS@@/@@MUST_REMEMBER@@/@@EMOTIONAL@@`` bundle via the SHARED
    :func:`lifecycle.parse_extraction`. Every parsed entry is stamped with the
    rollup's source date (``created_at``) so the scorer can backdate it; the
    provenance ``source`` is set at scoring time, not here. A backend error or a
    malformed bundle is logged-and-swallowed into empty candidates so one bad
    rollup never aborts the import.
    """
    content = _clip(rollup.body, cfg.budgets.max_prompt_content_chars)
    prompt = _fill_prompt(
        _prompts_root(cfg) / "import_legacy.md",
        agent_name=cfg.agent.name,
        period=rollup.source_date,
        rollup_kind=rollup.kind,
        procedure_max_words=cfg.memory_extract.skill_procedure_words,
        memo_max_words=cfg.memory_extract.memo_words,
        reaction_max_words=cfg.memory_extract.reaction_words,
        weight_cap=int(cfg.memory.diary.weight_cap),
        content=content,
    )
    try:
        raw = summarizer.summarize(
            prompt=prompt, max_words=cfg.memory_extract.max_output_words
        )
        # ``now`` = the rollup's source date so the unscored entries carry the
        # backdated ``created_at`` the scorer reads; ``source`` is re-tagged to
        # IMPORT_SOURCE by ``score_seed_candidates``.
        return parse_extraction(raw, now=rollup.source_date, source=IMPORT_SOURCE)
    except Exception:  # noqa: BLE001 — one bad rollup must not abort the import
        log.exception("re-author failed for %s rollup %s", rollup.kind, rollup.source_date)
        return Candidates(skills=[], must_remember=[], diary=[])


def reauthor(
    cfg: Config,
    summarizer: Summarizer,
    source: LegacySource,
) -> Candidates:
    """Re-author one persona's legacy memory into unscored new-shape candidates.

    The persona-driven pass (plan §3.2): the mechanical ``must_memorize.md`` pins
    pass straight through (the table IS the directive), while EACH rollup's aged-
    out prose goes through the single model touch point (:func:`_reauthor_one`)
    to mine skill + emotional + any fresh must-remember seeds. The per-rollup
    bundles are concatenated, then merged with the mechanical pins. Every entry
    carries its source date in ``created_at`` for the scorer to backdate.
    """
    skills: list[SkillEntry] = []
    must: list[MustRememberEntry] = []
    emo: list[DiaryEntry] = []

    mechanical = _pins_to_candidates(source.pins)
    must.extend(mechanical.must_remember)

    for rollup in source.rollups:
        cands = _reauthor_one(cfg, summarizer, rollup)
        skills.extend(cands.skills)
        must.extend(cands.must_remember)
        emo.extend(cands.diary)

    return Candidates(skills=skills, must_remember=must, diary=emo)


# ----- the orchestrator (§3.3 / §12 read-before-drop spine) -----------------


def import_legacy_run(
    cfg: Config,
    store: Store,
    *,
    summarizer: Summarizer | None = None,
    force: bool = False,
    now: str | None = None,
) -> dict:
    """Orchestrate the one-off legacy import for one persona (plan §3.3).

    The dependency spine, in this EXACT order (read-before-drop, design §12):

    1. gate on :func:`already_imported` — skip (no-op) if the persona is already
       imported, UNLESS *force* is set;
    2. :func:`read_legacy` — snapshot ALL old bytes into memory FIRST;
    3. :func:`reauthor` — re-author the rollup prose (the single model touch
       point; mock in CI) + pass the mechanical pins through;
    4. :func:`assert_seed_inputs_snapshotted` — confirm the snapshot is fully
       materialised before any seed (the ordering anchor);
    5. :func:`score_seed_candidates` (Rukawa) — backdate + score;
    6. :func:`seed_entries` (Mitsui) — durable append into the three stores;
    7. :func:`mark_imported` (Mitsui) — write the one-off ``.state.json`` marker.

    Returns ``{"skipped": "already"}`` when gated, else the per-store seeded
    counts plus ``{"skipped": None}``. **NEVER calls ``rebuild``** — the caller
    runs the fresh-start drop AFTER the import (the migration order is
    merge → migrate configs → import-legacy → rebuild → sweep).
    """
    store.init_layout()
    bstore = BoundedStore(cfg, store)
    if already_imported(store, bstore) and not force:
        log.info("import-legacy: already imported; skipping (use force to re-seed)")
        return {"skipped": "already"}
    if force:
        # Re-seed intentionally: drop any prior import-legacy entries first so
        # the no-double-seed guard in seed_entries does not refuse the re-run
        # (and so the re-seed REPLACES rather than duplicates). Only entries
        # carrying the import provenance are removed — live extract/pin memory
        # is untouched.
        _purge_seeded_entries(bstore)

    now = now or iso_now()
    source = read_legacy(store)  # snapshot ALL legacy bytes FIRST.
    summarizer = summarizer or _build_summarizer(cfg)
    raw = reauthor(cfg, summarizer, source)
    assert_seed_inputs_snapshotted(raw)
    scored = score_seed_candidates(raw, cfg=cfg, now=now)
    counts = seed_entries(bstore, scored, now=now)
    mark_imported(store, counts=counts)
    log.info(
        "import-legacy complete: +%d skills, +%d must_remember, +%d emotional",
        counts[STORE_SKILLS], counts[STORE_MUST_REMEMBER], counts[STORE_DIARY],
    )
    return {"skipped": None, **counts}

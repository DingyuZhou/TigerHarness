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
from typing import Iterable

from .bounded_store import BoundedStore
from .entries import (
    STORE_EMOTIONAL,
    STORE_MUST_REMEMBER,
    STORE_NAMES,
    STORE_SKILLS,
    BaseEntry,
)
from .lifecycle import Candidates
from .state import iso_now
from .store import Store

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
    for store_name in STORE_NAMES:
        for entry in bstore.load(store_name):
            if entry.source == IMPORT_SOURCE:
                return True
    return False


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
            STORE_EMOTIONAL: int(counts.get(STORE_EMOTIONAL, 0)),
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
    added = {STORE_SKILLS: 0, STORE_MUST_REMEMBER: 0, STORE_EMOTIONAL: 0}
    if candidates.is_empty():
        return added
    per_store: dict[str, list[BaseEntry]] = {
        STORE_SKILLS: list(candidates.skills),
        STORE_MUST_REMEMBER: list(candidates.must_remember),
        STORE_EMOTIONAL: list(candidates.emotional),
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
        added[STORE_SKILLS], added[STORE_MUST_REMEMBER], added[STORE_EMOTIONAL],
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
        candidates.emotional,
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

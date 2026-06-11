"""Task-id generation: ``<YYYYMMDD>-<HHmmSS>-<slug>-<uuid8>``.

The timestamp (UTC) carries the date AND time of day so same-day task
ids sort in their scheduled order under a plain lexicographic sort.
Legacy ids minted before the time component existed
(``<YYYYMMDD>-<slug>-<uuid8>``) remain fully valid: nothing parses the
timestamp back out of an id, so old and new formats coexist in one
journal with no migration.

Mirrors ``wfcore.ids`` in spirit but lives in its own module so
the journal package has no dependency on the workflow-runner: this
backend should keep working even if the workflow-runner is renamed,
deleted, or reorganised.
"""

from __future__ import annotations

import datetime as _dt
import re
import secrets
from collections.abc import Callable


_SLUG_MAX_LEN = 40
# Pattern of characters to KEEP when slugifying (after lowercasing).
# Everything else collapses to a hyphen.
_SLUG_KEEP = re.compile(r"[a-z0-9]+")
# Reject ids whose slug is empty, "..", or starts with a hidden-file dot.
_FORBIDDEN_SLUGS: frozenset[str] = frozenset({"", ".", ".."})


class JournalIdError(ValueError):
    """Raised on slug/id sanitisation failures (path traversal attempt,
    completely-empty slug source, ...). Distinct from generic ValueError
    so callers can pattern-match the journal layer specifically."""


def slugify(text: str, *, max_len: int = _SLUG_MAX_LEN) -> str:
    """Produce a path-safe ASCII slug.

    Pipeline: lowercase -> keep [a-z0-9]+ tokens -> join with ``-`` ->
    truncate to ``max_len`` -> trim trailing hyphens. Falls back to
    ``"task"`` if the input has no usable characters at all (so a PRD
    titled "!!!" still produces a valid id).

    Raises ``JournalIdError`` only if ``max_len < 1`` -- the slug itself
    is always non-empty by construction.
    """
    if max_len < 1:
        raise JournalIdError(f"max_len must be >= 1; got {max_len}")
    tokens = _SLUG_KEEP.findall(text.lower())
    if not tokens:
        return "task"
    slug = "-".join(tokens)[:max_len].rstrip("-")
    # Defensive: with the current `_SLUG_KEEP` regex (``[a-z0-9]+``) the
    # joined+truncated+rstripped result cannot land in the forbidden set
    # (it can only contain ``[a-z0-9-]`` and is non-empty by construction
    # because ``tokens`` is). The guard stays in case ``_SLUG_KEEP`` is
    # ever widened to accept dots; see ``test_slug_kept_safe_under_edit``.
    if slug in _FORBIDDEN_SLUGS:  # pragma: no cover -- defensive
        return "task"
    return slug


def _short_uuid() -> str:
    """8 hex chars from a CSPRNG. 32 bits; collision-rare at journal
    scale (humans have a few hundred tasks/year). Lowercase hex matches
    the example in the design doc."""
    return secrets.token_hex(4)


def new_task_id(
    title_or_slug: str,
    *,
    now: _dt.datetime | None = None,
    slug_overrider: str | None = None,
    exists_check: Callable[[str], bool] | None = None,
) -> str:
    """Mint a fresh ``<YYYYMMDD>-<HHmmSS>-<slug>-<uuid8>`` task id.

    The timestamp is UTC; date and time come from the same clock
    reading, so the id's lexicographic order is its creation order
    (within new-format ids).

    Parameters
    ----------
    title_or_slug:
        The thing to slugify. Typically the task title or the first H1
        of the PRD; callers can also pre-slugify and pass the result
        in (it'll be re-slugified, idempotently).
    now:
        Inject a clock for tests. Defaults to ``datetime.now(UTC)``.
    slug_overrider:
        If non-empty, use this verbatim (after slugify) instead of
        ``title_or_slug``. Maps to a ``--slug`` CLI flag.
    exists_check:
        Optional callable that returns True if the proposed id already
        exists on disk. The scaffolder injects a real filesystem check;
        on collision we regenerate the uuid8 exactly once and then
        hard-error. Path tests inject a stub.
    """
    when = now or _dt.datetime.now(_dt.timezone.utc)
    stamp = when.strftime("%Y%m%d-%H%M%S")
    slug_source = slug_overrider if slug_overrider else title_or_slug
    slug = slugify(slug_source)
    for attempt in range(2):  # original + one regenerate-on-collision
        uuid8 = _short_uuid()
        candidate = f"{stamp}-{slug}-{uuid8}"
        if exists_check is None or not exists_check(candidate):
            return candidate
        # Loop -- regenerate uuid8 once.
    raise JournalIdError(
        f"could not mint a unique id for slug {slug!r} after 2 attempts; "
        "the journal is probably very full or the clock is stuck"
    )


def is_safe_task_id(task_id: str) -> bool:
    """Reject a task id as path-unsafe if it contains *any* path
    separator, control character, parent-dir traversal, hidden-file
    prefix, or leading/trailing whitespace, or if it is empty / blank.

    Used by the path layer when consuming externally-provided ids
    (CLI args, status.json on disk). Intentionally strict: a *minted*
    id from ``new_task_id`` only ever contains ``[a-z0-9-]`` plus
    embedded hyphens, so this filter biases toward rejecting anything
    a malicious or corrupted source could try.
    """
    if not task_id or not task_id.strip():
        return False
    if task_id != task_id.strip():
        return False  # leading/trailing whitespace
    if task_id in _FORBIDDEN_SLUGS:
        return False
    if task_id.startswith(".") or task_id.startswith("-"):
        return False
    if "/" in task_id or "\\" in task_id:
        return False
    if ".." in task_id:
        return False
    # Reject NUL, every C0 control char, DEL, and characters that
    # commonly cause grief on platform A or B (colon = Windows alt
    # stream; whitespace = shell quoting hazard).
    for ch in task_id:
        if ord(ch) < 0x20 or ord(ch) == 0x7f:
            return False
        if ch in (":", " ", "\t"):
            return False
    return True

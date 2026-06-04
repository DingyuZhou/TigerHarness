"""Scaffolders for both journal task kinds: ``new_task`` (task mode,
Phase 1) and ``new_workflow_task`` (workflow mode, Phase 1.5).

Task-mode scaffolder produces:

- ``active/<task-id>/task.md`` -- the PRD content verbatim.
- ``active/<task-id>/status.json`` -- the seeded ``Status`` in
  ``state=pending``, written atomically.
- ``active/<task-id>/progress.md`` -- empty starter file with a single
  H1 so the driver can append to a real file rather than create it.
- ``active/<task-id>/artifacts/`` -- empty subdirectory the task can
  fill at will.

Workflow-mode scaffolder additionally produces:

- ``active/<task-id>/task_brief.md`` -- the brief content verbatim
  (the workflow-runner convention; replaces task.md for workflow tasks).
- ``active/<task-id>/playbook_snapshot.md`` -- the playbook content
  verbatim, frozen at scaffold time so an in-flight compile is not
  perturbed by a later edit to the team's playbook source.
- ``active/<task-id>/status.json`` with ``kind=workflow``,
  ``compile_pending=true``, ``compile_phase="pending"``.

The workflow scaffolder does NOT call any LLM (Option C: compile is
deferred to the first ``drive-journal`` invocation). It DOES do
synchronous persona validation: Anzai, Akagi, Ayako must exist in
``teams/<team>/personas/<name>/prompt.md`` (these are the three
compile-time personas), and every persona name regex-extracted from
the playbook must also exist. Failure exits before any journal
artifact is written.

Both scaffolders land ``OPERATING.md`` at the journal root on first
use so the driver can read the vendor-neutral protocol.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from tigerharness.journal.ids import JournalIdError, new_task_id
from tigerharness.journal.models import JournalModelError, Status
from tigerharness.journal.operating_template import OPERATING_MD
from tigerharness.journal.paths import JournalPaths


# The three personas the in-session compile sub-protocol adopts (Anzai
# for the drafter turn, Akagi for the execution-critic lens, Ayako for
# the QA-critic lens). Default mapping in Phase 1.5; Phase 2
# additionally lets a team override these via
# ``teams/<team>/configs/workflow.yaml`` (key: ``compile_personas``).
COMPILE_PERSONAS: tuple[str, ...] = ("Anzai", "Akagi", "Ayako")

# Phase 2: role -> persona-name mapping. The CLI args (``--kind
# drafter|akagi|ayako``) and the critic prompt templates are keyed by
# the role tokens "drafter" / "akagi" / "ayako" -- those stay constant.
# The persona NAMES that play each role are configurable; the default
# happens to use the role tokens as the persona names (drafter=Anzai
# is the only non-identity mapping).
_DEFAULT_COMPILE_PERSONAS: dict[str, str] = {
    "drafter": "Anzai",
    "akagi": "Akagi",
    "ayako": "Ayako",
}

_COMPILE_ROLES: tuple[str, ...] = ("drafter", "akagi", "ayako")

# Regex pattern for a playbook name (mirrors workflow_runner.cli's
# _PLAYBOOK_NAME_RE): bare name, no path separators, conservative
# charset. Lets us reject ``--playbook ../../etc`` at CLI time.
_PLAYBOOK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Regex used to best-effort extract referenced persona names from a
# playbook source. Matches a leading-capital alphabetic word that is
# at least 3 characters long; not perfect, but catches the common
# case (the playbook says "Mitsui will handle ..." somewhere).
_PERSONA_REF_RE = re.compile(r"\b[A-Z][a-zA-Z]{2,}\b")


class JournalScaffoldError(ValueError):
    """Raised when the scaffolder cannot create a task (collision after
    retry, unreadable PRD, ...). Distinct from generic ValueError so
    callers can pattern-match the journal layer specifically."""


class MissingPersonaError(JournalScaffoldError):
    """Raised when the workflow scaffolder cannot find one or more
    required personas on disk. Carries the missing list so the CLI can
    surface a clear error.

    Workflow compile requires Anzai/Akagi/Ayako at minimum; the user's
    playbook may name additional personas, all of which are validated
    at scaffold time so a bad playbook does not consume any compile
    budget."""

    def __init__(self, team_root: Path, missing: list[str]) -> None:
        super().__init__(
            f"workflow compile requires personas {sorted(missing)}; "
            f"team root {team_root} is missing prompt.md for: "
            f"{sorted(missing)}"
        )
        self.team_root = team_root
        self.missing = list(missing)


@dataclass(frozen=True)
class ScaffoldResult:
    """What the scaffolder produced. Returned to the CLI for the human-
    readable summary."""

    task_id: str
    task_dir: Path
    status: Status


def _first_h1(text: str) -> str:
    """Extract the first H1 heading line from a markdown PRD, or the
    empty string if none. Used to seed ``title`` when ``--title`` is
    not provided."""
    for raw in text.splitlines():
        m = re.match(r"^\s*#\s+(.+?)\s*$", raw)
        if m:
            return m.group(1).strip()
    return ""


def _write_atomic(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` via a same-directory temp file +
    rename. Guarantees a reader never sees a half-written file even if
    the writer is SIGKILLed mid-write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, path)
    finally:
        try:
            os.unlink(tmp.name)
        except FileNotFoundError:
            pass  # replaced successfully


def _ensure_operating_md(paths: JournalPaths) -> None:
    """Write OPERATING.md at the journal root if it's not there yet.
    Idempotent. Once written, the file is the contract -- subsequent
    scaffolder runs leave it alone so a human edit isn't overwritten."""
    if paths.operating_md.is_file():
        return
    paths.root.mkdir(parents=True, exist_ok=True)
    _write_atomic(paths.operating_md, OPERATING_MD)


def new_task(
    *,
    prd_text: str,
    persona: str,
    paths: JournalPaths,
    title: str | None = None,
    kind: str = "task",
    max_sessions: int = 5,
    slug: str | None = None,
) -> ScaffoldResult:
    """Create a new task in ``paths.active``. Returns ``ScaffoldResult``.

    Workflow:

    1. Derive ``title`` from the ``--title`` arg, else first H1 of the
       PRD, else fall back to ``"task"``.
    2. Mint a task-id via :func:`new_task_id`; collision-check against
       both ``active/`` and ``done/`` so a recently-archived task
       cannot collide.
    3. Build a fresh ``Status`` (validates ``kind``, ``persona``,
       ``max_sessions``).
    4. Atomically write ``task.md`` (the PRD verbatim) and
       ``status.json`` (the seeded Status). Create ``progress.md`` with
       a single H1 + ``artifacts/`` empty.
    5. First-use only: write the canonical ``OPERATING.md`` at the
       journal root.
    """
    if not prd_text.strip():
        raise JournalScaffoldError("PRD is empty; nothing to scaffold")

    paths.ensure()

    effective_title = (title or "").strip() or _first_h1(prd_text) or "task"

    def _exists(candidate: str) -> bool:
        # A candidate id is "taken" if it's in active/ OR done/. Either
        # would create human confusion (re-archival collision later) or
        # a hard-error in JournalPaths.archive.
        return (
            paths.task_exists(candidate, archived=False)
            or paths.task_exists(candidate, archived=True)
        )

    try:
        task_id = new_task_id(
            effective_title,
            slug_overrider=(slug.strip() if slug else None),
            exists_check=_exists,
        )
    except JournalIdError as exc:
        raise JournalScaffoldError(
            f"could not mint a task id: {exc}"
        ) from exc

    try:
        status = Status.new(
            id=task_id,
            title=effective_title,
            persona=persona,
            kind=kind,
            max_sessions=max_sessions,
        )
    except JournalModelError as exc:
        raise JournalScaffoldError(
            f"could not build status.json: {exc}"
        ) from exc

    task_dir = paths.task_dir(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    paths.artifacts(task_id).mkdir(parents=True, exist_ok=True)

    # Write order matters: ``status.json`` must land LAST. The sweep's
    # visibility gate is ``status.json.is_file()`` (paths.list_active_ids),
    # so a SIGKILL or crash between writes must not leave a half-built
    # task visible to the driver. By the time status.json exists on
    # disk, task.md and progress.md already exist.
    _write_atomic(paths.task_md(task_id), prd_text)
    # progress.md is a single H1 starter; not atomic because torn write
    # is harmless (worst case: empty file), and it lands before
    # status.json so the driver's later append targets a real file.
    paths.progress_md(task_id).write_text(
        f"# Progress: {task_id}\n\n", encoding="utf-8",
    )
    # OPERATING.md is at the journal root, not the task dir -- order
    # vs. status.json doesn't matter for task visibility, but it should
    # exist before any drive-journal session reads it.
    _ensure_operating_md(paths)
    # Finally: the status.json that makes the task visible to the sweep.
    _write_atomic(paths.status_json(task_id), status.to_json())

    return ScaffoldResult(task_id=task_id, task_dir=task_dir, status=status)


# ---------------------------------------------------------------------------
# Workflow-mode scaffolder (Phase 1.5)
# ---------------------------------------------------------------------------

def resolve_team_root(team: str) -> Path:
    """Resolve the on-disk team root for ``team``.

    Resolution order mirrors ``workflow_runner.cli._resolve_team_root``
    so the two backends locate the same directory:

    1. ``$TIGERHARNESS_TEAMS_DIR/<team>`` if the env var is set --
       the explicit override (and the test seam).
    2. ``<cwd>`` if cwd is itself a team root
       (``configs/personas.yaml`` present): the "run from inside the
       team folder" convention.
    3. ``<cwd>/teams/<team>`` otherwise -- the documented layout.

    Returns a Path; does NOT verify the directory exists (the caller's
    persona validation does that).
    """
    override = os.environ.get("TIGERHARNESS_TEAMS_DIR", "").strip()
    if override:
        return Path(override) / team
    cwd = Path.cwd()
    if (cwd / "configs" / "personas.yaml").is_file():
        return cwd
    return cwd / "teams" / team


def extract_persona_refs_from_playbook(text: str) -> set[str]:
    """Best-effort extraction of capitalized-word candidates that
    *might* be persona references in the playbook.

    Greedy: anything matching ``[A-Z][a-zA-Z]{2,}`` is returned. The
    actual persona-ref set is the *intersection* of this candidate set
    with the team's roster (from ``personas.yaml``) -- a capitalized
    word that isn't a registered persona is treated as English prose,
    not a typo. See :func:`_required_workflow_personas` for the
    intersection logic.

    Used by the workflow scaffolder to enforce the "every persona
    named in the playbook must exist on disk" guarantee at scaffold
    time, so a real typo surfaces immediately rather than from a
    Tier 1 roster error mid-compile.
    """
    return set(_PERSONA_REF_RE.findall(text))


def read_team_roster(team_root: Path) -> set[str]:
    """Read the team's persona roster from
    ``<team_root>/configs/personas.yaml``. Returns the set of canonical
    persona names. An unreadable or malformed config returns an empty
    set -- the scaffolder's COMPILE_PERSONAS gate still fires.

    The yaml shape is the same as ``task_runner.personas.load_personas_config``:
    a top-level ``personas:`` list whose entries each have a ``name``
    field.
    """
    yaml_path = team_root / "configs" / "personas.yaml"
    if not yaml_path.is_file():
        return set()
    try:
        import yaml
        with yaml_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:  # pragma: no cover - defensive
        # yaml may be unavailable in a stripped install; we'd rather
        # let the COMPILE_PERSONAS check fire on missing prompt.md
        # than crash at scaffold time on a malformed yaml.
        return set()
    if not isinstance(data, dict):
        return set()
    entries = data.get("personas") or []
    out: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if isinstance(name, str) and name.strip():
            out.add(name.strip())
    return out


def _normalize_persona_key(name: str) -> str:
    """Normalize a persona name for case- + separator-insensitive
    matching. Mirrors :func:`task_runner.personas.resolve`:
    lowercased, with spaces and underscores collapsed to hyphens.
    """
    return name.strip().lower().replace(" ", "-").replace("_", "-")


def read_team_alias_map(team_root: Path) -> dict[str, str]:
    """Read ``configs/personas.yaml`` and return an
    ``alias -> canonical`` mapping (alias key is normalized via
    :func:`_normalize_persona_key`).

    Each persona entry's canonical name self-maps; any additional
    ``aliases:`` list is layered on top. A missing or malformed yaml
    yields an empty mapping (the canonical-roster behaviour from
    :func:`read_team_roster` then takes over).

    Conflict policy (Phase 2 hardening):

    - **Canonical names always win** over alias entries. If persona A
      declares an alias that happens to match persona B's canonical
      name (even when B appears earlier in the file), the canonical
      name's self-mapping wins, so a lookup of B's canonical name
      still resolves to B. This is implemented by collecting all alias
      entries first and overlaying canonical self-mappings on top.
    - Among aliases that collide with each other, **last-defined
      wins** -- whichever entry appears later in personas.yaml takes
      the alias. The tests pin this so the precedence is observable.

    Convention: matches ``task_runner.personas`` -- aliases include
    the canonical name when explicit, but a persona without an
    ``aliases`` field still self-maps via the canonical layer. Case-
    and separator-insensitive lookup.
    """
    yaml_path = team_root / "configs" / "personas.yaml"
    if not yaml_path.is_file():
        return {}
    try:
        import yaml
        with yaml_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:  # pragma: no cover - defensive
        return {}
    if not isinstance(data, dict):
        return {}
    entries = data.get("personas") or []
    aliases_map: dict[str, str] = {}
    canonicals_map: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not (isinstance(name, str) and name.strip()):
            continue
        canonical = name.strip()
        canonicals_map[_normalize_persona_key(canonical)] = canonical
        aliases = entry.get("aliases") or []
        if not isinstance(aliases, list):
            continue
        for alias in aliases:
            if isinstance(alias, str) and alias.strip():
                aliases_map[_normalize_persona_key(alias)] = canonical
    # Aliases first, canonical self-mappings overlay so a canonical
    # name is never shadowed by another persona's alias.
    out = dict(aliases_map)
    out.update(canonicals_map)
    return out


def canonicalize_persona(team_root: Path, given_name: str) -> str:
    """Resolve ``given_name`` (which may be a canonical name OR any
    alias) to its canonical persona name as declared in
    ``personas.yaml``.

    Returns ``given_name`` unchanged if no mapping is found (the team
    config is missing, malformed, or the name simply isn't declared) --
    callers can then surface a missing-persona error themselves.
    """
    return read_team_alias_map(team_root).get(
        _normalize_persona_key(given_name), given_name,
    )


_PLAYBOOK_META_BLOCK_RE = re.compile(
    r"<!--\s*\n(.*?)\n\s*-->",
    re.DOTALL,
)

# Whitelist of keys the journal scaffolder reads out of playbook
# metadata blocks. Anything else (e.g. the existing ``workflow_config:``
# block consumed by the api-backed workflow_runner) is parsed by the
# yaml loader but DROPPED here, so journal-side code can't accidentally
# consume runner-only config and the surface stays explicit.
_PLAYBOOK_META_KNOWN_KEYS: frozenset[str] = frozenset({"default_captain"})


def extract_playbook_meta(playbook_text: str) -> dict:
    """Parse every ``<!-- ... -->`` HTML comment in the playbook as
    YAML and return a dict of the known journal-side metadata keys.

    The HTML-comment YAML convention is shared with the api-backed
    workflow_runner (see Shohoku's ``workflow/default.md``: the
    ``workflow_config:`` block is the runner's example). To keep
    surface minimal and intentional, this function filters the merged
    dict down to keys in :data:`_PLAYBOOK_META_KNOWN_KEYS`. Adding a
    new playbook-meta knob is therefore a deliberate code change here
    + a doc change, never a silent dependency on whatever the playbook
    author dropped into a comment.

    Format requirements:

    - The HTML comment delimiters must each be on their own line:
      ``<!--`` followed by ``\\n``, then the YAML body, then ``\\n``
      and ``-->``. Single-line comments like
      ``<!-- foo: bar -->`` are treated as narrative prose and
      skipped, so a playbook author can drop a stray comment without
      breaking the scaffolder.
    - Blocks that aren't valid YAML or don't parse to a dict are
      skipped silently. Later blocks override earlier ones on key
      collision (shallow last-wins merge).
    """
    merged: dict = {}
    for match in _PLAYBOOK_META_BLOCK_RE.finditer(playbook_text):
        try:
            import yaml
            data = yaml.safe_load(match.group(1))
        except Exception:  # pragma: no cover - defensive
            continue
        if isinstance(data, dict):
            merged.update(data)
    return {k: v for k, v in merged.items() if k in _PLAYBOOK_META_KNOWN_KEYS}


def resolve_playbook_default_captain(
    playbook_text: str, team_root: Path,
) -> str | None:
    """Read ``default_captain:`` from the playbook's metadata block(s)
    and return the canonical persona name (alias-resolved), or
    ``None`` if absent / blank / malformed.

    The CLI uses this as a fallback when ``--captain`` is omitted on
    ``journal new --kind workflow``, so a playbook can declare "the
    accountable owner for this kind of work is X" once and have it
    apply to every task scaffolded from it.
    """
    meta = extract_playbook_meta(playbook_text)
    raw = meta.get("default_captain")
    if not (isinstance(raw, str) and raw.strip()):
        return None
    return canonicalize_persona(team_root, raw.strip())


def resolve_default_persona(team_root: Path) -> str | None:
    """Read ``configs/personas.yaml``'s top-level ``default_persona:``
    key and return its canonical name (after alias resolution), or
    ``None`` if the key is absent / blank / malformed.

    The CLI uses this as a fallback when ``--persona`` is omitted for
    ``--kind task`` scaffolds, so a team can declare "this is the
    default helper" once and not type ``--persona`` every time.

    A missing-but-readable yaml returns ``None`` rather than raising,
    so existing teams without the key keep their explicit-``--persona``
    flow unchanged.
    """
    yaml_path = team_root / "configs" / "personas.yaml"
    if not yaml_path.is_file():
        return None
    try:
        import yaml
        with yaml_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:  # pragma: no cover - defensive
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("default_persona")
    if not (isinstance(raw, str) and raw.strip()):
        return None
    # Resolve through aliases so a team can write
    # `default_persona: Mumu` even if Mumu is an alias of Kogure.
    return canonicalize_persona(team_root, raw.strip())


def resolve_compile_personas(team_root: Path) -> dict[str, str]:
    """Phase 2: resolve the role -> persona-name mapping for the three
    compile-time roles (``drafter``, ``akagi``, ``ayako``).

    Reads ``<team_root>/configs/workflow.yaml``'s ``compile_personas``
    key:

    .. code:: yaml

        compile_personas:
          drafter: Anzai
          akagi:   Akagi
          ayako:   Ayako

    Each key is optional; missing keys fall back to the Phase 1.5
    defaults (Anzai / Akagi / Ayako). Unknown role keys are silently
    ignored -- a Phase 3 addition might add new roles, and an older
    journal install should still load the file.

    A malformed yaml or missing file returns the all-defaults mapping.
    The scaffolder's :func:`validate_personas` gate fires on the
    resolved persona names, so a config that points at a non-existent
    persona surfaces at scaffold time.
    """
    out = dict(_DEFAULT_COMPILE_PERSONAS)
    yaml_path = team_root / "configs" / "workflow.yaml"
    if not yaml_path.is_file():
        return out
    try:
        import yaml
        with yaml_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:  # pragma: no cover - defensive
        return out
    if not isinstance(data, dict):
        return out
    overrides = data.get("compile_personas")
    if not isinstance(overrides, dict):
        return out
    for role in _COMPILE_ROLES:
        val = overrides.get(role)
        if isinstance(val, str) and val.strip():
            out[role] = val.strip()
    return out


def _required_workflow_personas(
    playbook_text: str, team_root: Path,
) -> set[str]:
    """The set of CANONICAL persona names the workflow scaffolder
    requires on disk: the team-configured compile-role personas PLUS
    any playbook reference that matches the team's roster (including
    aliases). Candidates the playbook mentions but that aren't in the
    roster are treated as English prose, not persona typos.

    Aliases are resolved before the intersection check so a playbook
    that addresses a persona by alias (e.g. ``Mumu`` for the
    Kogure-aliased persona) is still recognised as a real reference.
    The compile-role names are canonicalised through the same path,
    so a ``workflow.yaml`` override like ``akagi: Mumu`` lands on the
    canonical ``Kogure`` for prompt-file validation.
    """
    candidates = extract_persona_refs_from_playbook(playbook_text)
    alias_map = read_team_alias_map(team_root)

    def _canonical(name: str) -> str:
        return alias_map.get(_normalize_persona_key(name), name)

    real_refs = {
        _canonical(c) for c in candidates
        if _normalize_persona_key(c) in alias_map
    }
    compile_personas = {
        _canonical(v) for v in resolve_compile_personas(team_root).values()
    }
    return compile_personas | real_refs


def _persona_prompt_path(team_root: Path, persona: str) -> Path:
    """Path to a persona's ``prompt.md``. Resolves aliases to the
    canonical name before constructing the path -- so a caller passing
    ``Mumu`` for a Kogure-aliased persona reads
    ``personas/Kogure/prompt.md``."""
    canonical = canonicalize_persona(team_root, persona)
    return team_root / "personas" / canonical / "prompt.md"


def validate_personas(team_root: Path, required: set[str]) -> list[str]:
    """Return the subset of ``required`` whose ``prompt.md`` is missing
    or empty under ``team_root/personas/<name>/``. Empty list means
    all are present. Does NOT raise -- the caller decides how to
    handle the result (the scaffolder raises ``MissingPersonaError``;
    a CLI ``validate-personas`` subcommand just prints)."""
    missing: list[str] = []
    for name in sorted(required):
        prompt = _persona_prompt_path(team_root, name)
        try:
            if not prompt.is_file():
                missing.append(name)
                continue
            if prompt.stat().st_size == 0:
                missing.append(name)
        except OSError:
            missing.append(name)
    return missing


def new_workflow_task(
    *,
    brief_text: str,
    playbook_text: str,
    playbook_name: str,
    team_root: Path,
    paths: JournalPaths,
    title: str | None = None,
    captain: str | None = None,
    max_sessions: int = 10,
    slug: str | None = None,
) -> ScaffoldResult:
    """Scaffold a new ``kind=workflow`` task. No LLM calls -- the
    compile pipeline is deferred to the first ``drive-journal``
    invocation per Option C.

    Workflow:

    1. Validate ``playbook_name`` against ``_PLAYBOOK_NAME_RE`` (the
       caller is expected to have already resolved + read the playbook
       file; the name is carried separately for the
       ``orchestration.playbook`` field a future compile will set).
    2. Derive title from ``--title`` arg, else first H1 of the brief,
       else fall back to ``"workflow"``.
    3. Validate every required persona exists on disk under the team
       root: the hard-coded compile trio (Anzai/Akagi/Ayako) plus the
       playbook-extracted references. Raise ``MissingPersonaError``
       on any miss -- no journal artifact is written.
    4. Mint a task-id; collision-check both ``active/`` and ``done/``.
    5. Build a fresh ``Status`` via ``Status.new_workflow``.
    6. Write the task dir + ``task_brief.md`` + ``playbook_snapshot.md``
       + ``progress.md`` + ``artifacts/`` + ``OPERATING.md`` on first
       use. ``status.json`` lands LAST per the Phase 1 write-order
       invariant (sweep's visibility gate is ``status.json``
       existence).
    """
    if not brief_text.strip():
        raise JournalScaffoldError(
            "brief is empty; nothing to scaffold"
        )
    if not playbook_text.strip():
        raise JournalScaffoldError(
            "playbook is empty; nothing to scaffold"
        )
    if not _PLAYBOOK_NAME_RE.match(playbook_name):
        raise JournalScaffoldError(
            f"playbook name {playbook_name!r} must match "
            f"[A-Za-z0-9][A-Za-z0-9._-]* (no path separators)"
        )

    # Validate personas BEFORE creating any artifact. A scaffold that
    # mints a task dir and then fails on persona validation would
    # leave a half-built workflow on disk -- not the contract.
    required = _required_workflow_personas(playbook_text, team_root)
    missing = validate_personas(team_root, required)
    if missing:
        raise MissingPersonaError(team_root=team_root, missing=missing)

    paths.ensure()

    effective_title = (
        (title or "").strip()
        or _first_h1(brief_text)
        or "workflow"
    )

    def _exists(candidate: str) -> bool:
        return (
            paths.task_exists(candidate, archived=False)
            or paths.task_exists(candidate, archived=True)
        )

    try:
        task_id = new_task_id(
            effective_title,
            slug_overrider=(slug.strip() if slug else None),
            exists_check=_exists,
        )
    except JournalIdError as exc:
        raise JournalScaffoldError(
            f"could not mint a task id: {exc}"
        ) from exc

    try:
        status = Status.new_workflow(
            id=task_id,
            title=effective_title,
            playbook_name=playbook_name,
            captain=captain,
            max_sessions=max_sessions,
        )
    except JournalModelError as exc:
        raise JournalScaffoldError(
            f"could not build status.json: {exc}"
        ) from exc

    task_dir = paths.task_dir(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    paths.artifacts(task_id).mkdir(parents=True, exist_ok=True)

    # Write order: brief + playbook snapshot + progress + OPERATING
    # all land before status.json (the visibility gate). Same invariant
    # as Phase 1 -- a SIGKILL between writes never leaves a half-built
    # workflow task visible to the sweep.
    _write_atomic(task_dir / "task_brief.md", brief_text)
    _write_atomic(task_dir / "playbook_snapshot.md", playbook_text)
    paths.progress_md(task_id).write_text(
        f"# Progress: {task_id}\n\n", encoding="utf-8",
    )
    _ensure_operating_md(paths)
    _write_atomic(paths.status_json(task_id), status.to_json())

    return ScaffoldResult(task_id=task_id, task_dir=task_dir, status=status)

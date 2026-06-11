"""Interactive teardown of a team or persona.

Removes filesystem state, edits the multi-team index (if present), and
- for the *last* team in a multi-team setup - stops & disables the
``slack-bridge-multi.service`` systemd user unit and deletes its env
file + the multi-team index.

The Slack app on ``api.slack.com`` is **out of scope** -- the command
prints a reminder for the user to delete it by hand.

Flow:

    picker -> target -> dry-run preview -> backup confirm -> type-name
    confirm -> execute -> manual-step reminder

``--dry-run`` prints the plan and exits without prompting or deleting.

The YAML reads/writes are deliberately line-based (no pyyaml dep) so
that dismiss works in any install profile, and so that edits preserve
comments and formatting in files the user has personalized.

**Block-style YAML only.** The line-based parsers expect block-style
``lanes:`` / ``personas:`` lists (one ``- entry`` per line) -- the
form ``tigerharness init`` writes. Flow-style lists
(``lanes: [shohoku, tigers]``) are not recognized; converting the
index to flow style will cause dismiss to silently treat the team as
single-tenant. Stick with block style and edits remain safe.
"""
from __future__ import annotations

import logging

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .init import discover_teams, list_personas_in_team

log = logging.getLogger("tigerharness.dismiss")

# Names appear in shell snippets and paths -- enforce the same alphabet
# as init.
# All value-line regexes terminate with an optional ``# ...`` trailing
# comment so a hand-edited row like ``- shohoku  # primary lane`` or
# ``state_dir: ~/state/x  # custom`` is still parsed. Without the
# tolerance the line silently fails to match -- the lane stays in the
# index, state_dir cleanup is skipped, the default_persona refusal
# check misses, etc.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_LANE_LINE_RE = re.compile(r"^\s*-\s*(\S+)\s*(?:#.*)?$")
_DEFAULT_PERSONA_LINE_RE = re.compile(
    r"^default_persona\s*:\s*(\S+)\s*(?:#.*)?$"
)
_LEGACY_PERSONA_LINE_RE = re.compile(r"^persona\s*:\s*(\S+)\s*(?:#.*)?$")
# state_dir uses ``.+?`` because the value can contain spaces (e.g.
# ``~/My Folder/state``). Anchor the trailing-comment match with a
# whitespace-required boundary so a literal ``#`` inside a path
# isn't accidentally treated as a comment.
_STATE_DIR_LINE_RE = re.compile(
    r"^state_dir\s*:\s*(.+?)\s*(?:\s#.*)?$"
)
_PERSONA_ENTRY_START_RE = re.compile(r"^  - name:\s*(\S+)\s*(?:#.*)?$")
_ENV_FILE_LINE_RE = re.compile(r"^EnvironmentFile\s*=\s*(.+?)\s*(?:\s#.*)?$")

# Must exceed the unit template's `TimeoutStopSec=120` so a legitimate
# 120s drain has room to complete and report success before our
# subprocess.run gives up and surfaces a spurious timeout. The
# connection-to-systemd timeout for a broken systemd is on the order
# of seconds, not minutes, so the high ceiling here only matters in
# the slow-drain case where it's exactly what we want.
_SYSTEMCTL_TIMEOUT_S = 180.0


def _unquote(value: str) -> str:
    """Strip a single layer of matching surrounding quotes.

    YAML readers in this module use ``(\\S+)`` / ``(.+?)`` captures
    that include any surrounding quotes a user may have written
    (``default_persona: "ayako"`` is valid YAML). Without stripping,
    the captured value is ``"ayako"`` -- not equal to ``ayako`` -- and
    downstream comparisons silently miss.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


# ---------------------------------------------------------------------------
# Plan data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FileRemoval:
    path: Path
    description: str


@dataclass(frozen=True)
class FileEdit:
    path: Path
    description: str
    new_content: str  # pre-computed during planning so dry-run is exact


@dataclass(frozen=True)
class ServiceAction:
    # "stop_disable_unit" | "remove_unit_file" | "daemon_reload"
    kind: str
    target: str
    description: str


@dataclass(frozen=True)
class DismissPlan:
    kind: str  # "team" | "persona"
    target_name: str  # "shohoku" or "shohoku/ayako"
    removals: tuple[FileRemoval, ...]
    edits: tuple[FileEdit, ...]
    service_actions: tuple[ServiceAction, ...]
    manual_reminders: tuple[str, ...]


# ---------------------------------------------------------------------------
# Line-based YAML readers / editors
# ---------------------------------------------------------------------------

def _read_lanes_from_index(index_path: Path) -> list[str]:
    """Return the lane names from a slack-bridge.yaml index, or []
    when the file doesn't exist or has no ``lanes:`` section.

    Tracks an ``in_lanes`` flag so a stray ``- something`` line outside
    the ``lanes:`` block isn't mistaken for a lane.
    """
    if not index_path.exists():
        return []
    text = index_path.read_text(encoding="utf-8")
    in_lanes = False
    lanes: list[str] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("#"):
            continue
        if raw_line.startswith("lanes:"):
            in_lanes = True
            continue
        if in_lanes:
            if raw_line and not raw_line[0].isspace():
                in_lanes = False
                continue
            m = _LANE_LINE_RE.match(raw_line)
            if m:
                lanes.append(_unquote(m.group(1)))
    return lanes


def _remove_lane_from_index(index_text: str, team: str) -> str:
    """Strip ``  - <team>`` from the lanes list in a slack-bridge.yaml
    index. Lines outside the ``lanes:`` block are preserved verbatim,
    so an entry that happens to share a name with another YAML list
    elsewhere can't be misidentified.
    """
    out_lines: list[str] = []
    in_lanes = False
    for line in index_text.splitlines(keepends=True):
        stripped = line.lstrip()
        if line.startswith("lanes:"):
            in_lanes = True
            out_lines.append(line)
            continue
        # A non-indented non-comment line ends the lanes block.
        if (
            in_lanes
            and line
            and not line[0].isspace()
            and not stripped.startswith("#")
        ):
            in_lanes = False
        if in_lanes:
            m = _LANE_LINE_RE.match(line.rstrip("\n"))
            if m and _unquote(m.group(1)) == team:
                continue
        out_lines.append(line)
    return "".join(out_lines)


def _read_default_persona(fragment_path: Path) -> str | None:
    """Return ``default_persona`` from a per-team slack-bridge fragment,
    or None when the file doesn't exist or the field is absent.
    Accepts the legacy ``persona:`` field name as a fallback.
    """
    if not fragment_path.exists():
        return None
    for raw_line in fragment_path.read_text(encoding="utf-8").splitlines():
        if raw_line.strip().startswith("#"):
            continue
        m = _DEFAULT_PERSONA_LINE_RE.match(raw_line) \
            or _LEGACY_PERSONA_LINE_RE.match(raw_line)
        if m:
            return _unquote(m.group(1).strip())
    return None


def _read_state_dir(
    fragment_path: Path, *, base: Path | None = None,
) -> Path | None:
    """Return the per-team state_dir from a slack-bridge fragment,
    with ``~`` expanded and -- when *base* is supplied -- relative
    paths resolved against *base*.

    Init writes absolute paths, but the bridge's loader resolves
    relative ones against the fragment's team directory; mirroring
    that here keeps dismiss in lockstep with the bridge so a hand-
    edited relative ``state_dir`` doesn't escape cleanup.

    Returns None if the field is absent or the file is missing.
    """
    if not fragment_path.exists():
        return None
    for raw_line in fragment_path.read_text(encoding="utf-8").splitlines():
        if raw_line.strip().startswith("#"):
            continue
        m = _STATE_DIR_LINE_RE.match(raw_line)
        if m:
            raw = _unquote(m.group(1).strip())
            p = Path(raw).expanduser()
            if p.is_absolute() or base is None:
                return p
            return (base / p).resolve()
    return None


def _has_persona_entry(yaml_text: str, persona: str) -> bool:
    """Return True iff *yaml_text* contains a ``- name: <persona>`` entry.

    Uses a line-anchored regex tolerant of:
      - non-canonical whitespace around the colon (``- name:  ayako``)
      - YAML-quoted names (``- name: "ayako"`` / ``- name: 'ayako'``)
      - trailing comments (``- name: ayako  # the manager``)

    All three forms are valid YAML that init never writes but a user
    might hand-edit into the file. Without the tolerant match, the
    detector misses the entry and dismiss leaves a stale row behind
    pointing at a deleted persona dir.
    """
    name_re = re.escape(persona)
    pattern = (
        r"^  - name:\s*[\"']?" + name_re
        + r"[\"']?\s*(?:#.*)?$"
    )
    return re.search(pattern, yaml_text, re.MULTILINE) is not None


def _read_env_files_from_unit(unit_path: Path) -> list[Path]:
    """Parse all ``EnvironmentFile=`` paths out of a systemd unit.

    The gen-service template writes exactly one such line, but a user
    who customized via ``gen-service --env-file ...`` (or by hand) may
    have moved the file elsewhere -- relying on the hardcoded
    ``<teams-root>/multi-bridge.env`` default leaks it. This reads the
    unit-of-truth.
    """
    if not unit_path.exists():
        return []
    paths: list[Path] = []
    for raw_line in unit_path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("#"):
            continue
        m = _ENV_FILE_LINE_RE.match(stripped)
        if m:
            paths.append(Path(m.group(1)).expanduser())
    return paths


def _is_safe_state_dir(state_dir: Path, team: str) -> bool:
    """Defense in depth before ``rmtree``-ing a path that came out of a
    user-edited YAML file.

    Init's template writes ``state_dir: ~/.local/state/slack-bridge/<team>``
    -- the last path component matches the team name. We require that
    invariant: if the user re-pointed state_dir at something else (root
    of $HOME, ``/tmp``, the actual team dir), refuse to auto-delete it
    and emit a manual reminder instead. The team operator can still
    nuke it by hand; we just won't do it on their behalf without that
    "this is shaped like a state directory" signal.
    """
    resolved = state_dir.resolve()
    if len(resolved.parts) < 4:
        return False
    return resolved.name == team


def _remove_persona_entry_from_yaml(yaml_text: str, persona: str) -> str:
    """Remove the ``  - name: <persona>`` block (and its indented body)
    from personas.yaml. The block ends at the next ``  - name: ...`` or
    end-of-file. Other entries and surrounding content are preserved.

    The match unwraps optional surrounding quotes on the name so a
    quoted entry (``- name: "ayako"``) is detected too -- otherwise
    we'd remove the persona dir + memory but leave the quoted YAML row
    referring to a now-missing persona.
    """
    lines = yaml_text.splitlines(keepends=True)
    out_lines: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _PERSONA_ENTRY_START_RE.match(line.rstrip("\n"))
        if m and _unquote(m.group(1)) == persona:
            i += 1
            while i < len(lines):
                next_m = _PERSONA_ENTRY_START_RE.match(lines[i].rstrip("\n"))
                if next_m:
                    break
                i += 1
            continue
        out_lines.append(line)
        i += 1
    return "".join(out_lines)


# ---------------------------------------------------------------------------
# Plan builders
# ---------------------------------------------------------------------------


def _read_persona_aliases(yaml_text: str, persona: str) -> list[str]:
    """Return the alias list for ``persona`` from personas.yaml text.

    Light-touch line parser matching the init-scaffolded shape
    (``- name: X`` then an optional ``aliases: [a, b]`` inside the
    entry). Returns [] when the entry or alias list is absent --
    callers treat aliases as best-effort enrichment, never as a gate.
    """
    aliases: list[str] = []
    in_entry = False
    for line in yaml_text.splitlines():
        m = re.match(r"^\s*-\s*name\s*:\s*(\S+)\s*$", line)
        if m:
            in_entry = m.group(1).strip("'\"") == persona
            continue
        if in_entry:
            am = re.match(r"^\s*aliases\s*:\s*\[(.*)\]\s*$", line)
            if am:
                aliases = [
                    a.strip().strip("'\"")
                    for a in am.group(1).split(",") if a.strip()
                ]
                break
    return aliases


def _workflow_yaml_mentions(
    workflow_path: Path, names: list[str],
) -> list[str]:
    """Names from ``names`` referenced in configs/workflow.yaml's
    ``compile_personas`` mapping (value position). Best-effort line
    scan; [] when the file is absent."""
    if not workflow_path.exists():
        return []
    hits: list[str] = []
    try:
        text = workflow_path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0]
        m = re.match(r"^\s*(drafter|akagi|ayako)\s*:\s*(\S+)\s*$", stripped)
        if m:
            value = m.group(2).strip("'\"")
            if value in names and value not in hits:
                hits.append(value)
    return hits


def _active_tasks_assigned_to(team_dir: Path, persona: str) -> list[str]:
    """Task ids under <team>/journal/active/ whose status.json assigns
    ``persona``. Read-only, tolerant of malformed entries."""
    active = team_dir / "journal" / "active"
    if not active.is_dir():
        return []
    import json as _json
    hits: list[str] = []
    for status_path in sorted(active.glob("*/status.json")):
        try:
            data = _json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("persona") == persona:
            hits.append(status_path.parent.name)
    return hits


def build_team_plan(
    *,
    team: str,
    teams_root: Path,
    home: Path | None = None,
) -> DismissPlan:
    """Build a complete dismissal plan for a team.

    Raises ValueError when the team doesn't exist or the name is
    syntactically invalid.

    Known limitation: only the multi-team unit
    (``slack-bridge-multi.service``) is handled. A legacy
    single-tenant ``slack-bridge.service`` predates the layout this
    tool scaffolds, cannot be attributed to one team safely, and is
    left untouched (audit T9, deliberate).
    """
    team = team.strip()
    if not _NAME_RE.fullmatch(team):
        raise ValueError(f"invalid team name {team!r}")
    team_dir = (teams_root / team).resolve()
    if (
        not team_dir.is_dir()
        or not (team_dir / "configs" / "personas.yaml").exists()
    ):
        raise ValueError(
            f"team {team!r} not found under {teams_root} "
            f"(expected {team_dir}/configs/personas.yaml)"
        )

    home = home or Path.home()
    index_path = (teams_root / "slack-bridge.yaml").resolve()
    fragment_path = team_dir / "configs" / "slack-bridge.yaml"
    # Relative state_dir resolves against team_dir, matching
    # slack_bridge.multi._build_lane's behavior.
    state_dir = _read_state_dir(fragment_path, base=team_dir)
    lanes = _read_lanes_from_index(index_path)

    removals: list[FileRemoval] = [
        FileRemoval(
            team_dir,
            "team directory (configs, personas, memory data)",
        ),
    ]
    manual_reminders: list[str] = []
    # Flag a .git/ in the team dir loudly. If the user committed their
    # team configs and didn't push elsewhere, rmtree wipes the history
    # along with the working tree.
    if (team_dir / ".git").exists():
        manual_reminders.append(
            f"team dir contains a git repo at {team_dir / '.git'} -- "
            f"any unpushed commits will be lost. Push or back up "
            f"before confirming."
        )
    if state_dir is not None and state_dir.exists():
        if _is_safe_state_dir(state_dir, team):
            removals.append(FileRemoval(
                state_dir,
                "bridge runtime state (threads.json, etc.)",
            ))
        else:
            # User pointed state_dir at something that doesn't look like
            # a per-team state directory. Refuse to delete it; punt to
            # the operator.
            manual_reminders.append(
                f"state_dir at {state_dir} doesn't look like a per-team "
                f"state directory (expected last component '{team}'). "
                f"Refusing to auto-delete -- remove it by hand if "
                f"appropriate."
            )

    edits: list[FileEdit] = []
    service_actions: list[ServiceAction] = []

    # Track whether the team has any Slack-related artifacts -- only then
    # is the api.slack.com reminder relevant.
    has_slack = (
        (team_dir / "configs" / ".env").exists()
        or fragment_path.exists()
    )
    if has_slack:
        manual_reminders.append(
            "If this team had its own Slack app, delete it at "
            "https://api.slack.com/apps (self-service; can't be done "
            "from the CLI)."
        )

    if index_path.exists() and team in lanes:
        new_index = _remove_lane_from_index(
            index_path.read_text(encoding="utf-8"), team
        )
        remaining = [other for other in lanes if other != team]
        if remaining:
            edits.append(FileEdit(
                index_path,
                f"remove lane '{team}' from {index_path.name}",
                new_index,
            ))
        else:
            # Last team -- full multi-bridge teardown.
            removals.append(FileRemoval(
                index_path,
                "multi-bridge index (no lanes remain)",
            ))
            unit_path = (
                home / ".config" / "systemd" / "user"
                / "slack-bridge-multi.service"
            )
            # Prefer the unit's own EnvironmentFile= -- if the user
            # generated their unit with a custom --env-file path, the
            # default <teams-root>/multi-bridge.env would leak it.
            env_paths_from_unit = _read_env_files_from_unit(unit_path)
            env_paths_seen: set[Path] = set()
            for env_path in env_paths_from_unit:
                if env_path in env_paths_seen or not env_path.exists():
                    continue
                env_paths_seen.add(env_path)
                removals.append(FileRemoval(
                    env_path,
                    "multi-bridge env file (from unit's EnvironmentFile)",
                ))
            # Fall back to the canonical default if the unit doesn't
            # exist yet (rare: user has the index but never installed
            # the service) or didn't reference any env file.
            if not env_paths_seen:
                multi_env = teams_root / "multi-bridge.env"
                if multi_env.exists():
                    removals.append(FileRemoval(
                        multi_env,
                        "multi-bridge env file",
                    ))
            if unit_path.exists():
                service_actions.extend([
                    ServiceAction(
                        "stop_disable_unit",
                        "slack-bridge-multi.service",
                        "stop & disable slack-bridge-multi.service",
                    ),
                    ServiceAction(
                        "remove_unit_file",
                        str(unit_path),
                        f"remove unit file {unit_path}",
                    ),
                    ServiceAction(
                        "daemon_reload",
                        "",
                        "run `systemctl --user daemon-reload`",
                    ),
                ])
                manual_reminders.append(
                    "journalctl entries for slack-bridge-multi remain "
                    "on disk. Run `journalctl --user --vacuum-time=1s "
                    "--unit=slack-bridge-multi` if you want them "
                    "removed."
                )

    # The global/XDG journal root is shared across teams -- never
    # auto-delete it on a team dismissal; remind so the operator can
    # prune team-specific tasks by hand if any landed there.
    from .journal.paths import default_journal_root
    xdg_root = default_journal_root()
    if xdg_root.exists() and xdg_root != (team_dir / "journal"):
        manual_reminders.append(
            f"A global journal root exists at {xdg_root} (used when "
            f"journal commands run outside a team dir). It is shared "
            f"across teams, so dismiss does not touch it -- prune "
            f"{team}-specific tasks there by hand if any."
        )

    return DismissPlan(
        kind="team",
        target_name=team,
        removals=tuple(removals),
        edits=tuple(edits),
        service_actions=tuple(service_actions),
        manual_reminders=tuple(manual_reminders),
    )


def build_persona_plan(
    *,
    team: str,
    persona: str,
    teams_root: Path,
) -> DismissPlan:
    """Build a dismissal plan for one persona inside a team.

    Refuses (ValueError) when the persona is the team's only persona, or
    when the persona is the team's bridge ``default_persona`` -- in both
    cases the user must take an explicit prior step.
    """
    team = team.strip()
    persona = persona.strip()
    if not _NAME_RE.fullmatch(team):
        raise ValueError(f"invalid team name {team!r}")
    if not _NAME_RE.fullmatch(persona):
        raise ValueError(f"invalid persona name {persona!r}")
    team_dir = (teams_root / team).resolve()
    if (
        not team_dir.is_dir()
        or not (team_dir / "configs" / "personas.yaml").exists()
    ):
        raise ValueError(f"team {team!r} not found under {teams_root}")

    persona_dir = team_dir / "personas" / persona
    if not (persona_dir / "prompt.md").exists():
        raise ValueError(
            f"persona {persona!r} not found in team {team!r} "
            f"(expected {persona_dir}/prompt.md)"
        )

    rest = [p for p in list_personas_in_team(team_dir) if p != persona]
    if not rest:
        raise ValueError(
            f"persona {persona!r} is the only persona in team {team!r}. "
            f"Run `tigerharness dismiss` and pick team-level dismissal "
            f"to remove the whole team, or add another persona first."
        )

    fragment_path = team_dir / "configs" / "slack-bridge.yaml"
    default = _read_default_persona(fragment_path)
    if default == persona:
        raise ValueError(
            f"persona {persona!r} is team {team!r}'s default_persona "
            f"in {fragment_path}. Pick a new default first (edit that "
            f"file), then re-run dismiss."
        )

    # Same refusal for the JOURNAL default: personas.yaml's top-level
    # ``default_persona`` is what ``journal new`` falls back to when
    # --persona is omitted (and what hand-started sessions bootstrap
    # as). Dismissing it would leave both pointing at a ghost.
    yaml_path_early = team_dir / "configs" / "personas.yaml"
    journal_default = _read_default_persona(yaml_path_early)
    if journal_default == persona:
        raise ValueError(
            f"persona {persona!r} is team {team!r}'s default_persona "
            f"in {yaml_path_early}. Pick a new default first (edit "
            f"that file), then re-run dismiss."
        )

    removals: list[FileRemoval] = [
        FileRemoval(persona_dir, "persona prompt directory"),
    ]
    mem_dir = team_dir / "memories" / persona
    if mem_dir.exists():
        removals.append(FileRemoval(
            mem_dir,
            "persona memory store (archive, journal, briefing, cache)",
        ))

    edits: tuple[FileEdit, ...] = ()
    # personas.yaml is guaranteed to exist (validated above).
    yaml_path = team_dir / "configs" / "personas.yaml"
    yaml_text = yaml_path.read_text(encoding="utf-8")
    # Detect via the same regex the editor uses, so non-canonical
    # whitespace (e.g. "  - name:  ayako") still triggers the edit
    # instead of leaving a stale entry behind.
    if _has_persona_entry(yaml_text, persona):
        new_text = _remove_persona_entry_from_yaml(yaml_text, persona)
        edits = (FileEdit(
            yaml_path,
            f"remove persona '{persona}' from personas.yaml",
            new_text,
        ),)

    manual_reminders: list[str] = []
    # Dangling compile_personas mapping (direct name or alias) would
    # make the next workflow scaffold's preflight fail -- choosing the
    # replacement is a human call, so remind rather than auto-edit.
    alias_names = [persona] + _read_persona_aliases(yaml_text, persona)
    workflow_path = team_dir / "configs" / "workflow.yaml"
    wf_hits = _workflow_yaml_mentions(workflow_path, alias_names)
    if wf_hits:
        hit_list = ", ".join(wf_hits)
        manual_reminders.append(
            f"configs/workflow.yaml's compile_personas references "
            f"{hit_list} -- repoint those roles or the next workflow "
            f"scaffold preflight will fail."
        )
    # Active journal tasks assigned to the persona become zombies (the
    # done-gate still stamps the dead name; its memory store is gone).
    # The journal layer owns its state machine -- remind, never edit.
    zombie_tasks = _active_tasks_assigned_to(team_dir, persona)
    if zombie_tasks:
        task_list = ", ".join(zombie_tasks)
        manual_reminders.append(
            f"journal/active/ has task(s) assigned to {persona!r}: "
            f"{task_list}. Reassign or finish them first (the journal "
            f"CLI owns that state; dismiss will not touch it)."
        )
    # Prose mentions (roster, knowledge) are curation, not teardown --
    # remind only when the team actually keeps such docs.
    if (team_dir / "charter" / "roster.md").exists() or (
        team_dir / "knowledge"
    ).is_dir():
        manual_reminders.append(
            f"Prose mentions of {persona!r} (e.g. charter/roster.md, "
            f"knowledge/) are not auto-edited -- curate them by hand."
        )

    return DismissPlan(
        kind="persona",
        target_name=f"{team}/{persona}",
        removals=tuple(removals),
        edits=edits,
        service_actions=(),
        manual_reminders=tuple(manual_reminders),
    )


# ---------------------------------------------------------------------------
# Preview rendering
# ---------------------------------------------------------------------------

def _format_path(p: Path, base: Path | None) -> str:
    """Render *p* relative to *base* when both are absolute and *base*
    is a parent; otherwise fall back to the absolute path. Keeps the
    preview compact for users running from inside their teams dir
    without lying about paths that escape it (e.g. a state_dir under
    ``~/.local/state``)."""
    if base is None:
        return str(p)
    try:
        return str(p.relative_to(base))
    except ValueError:
        return str(p)


def render_preview(
    plan: DismissPlan,
    *,
    teams_root: Path | None = None,
) -> str:
    """Return a human-readable dry-run preview of *plan*. The output
    deliberately lists EVERY action -- the user is about to commit to
    something irreversible.

    When *teams_root* is given, paths under it are rendered relative
    to it; everything else stays absolute.
    """
    lines: list[str] = []
    lines.append(f"=== Dismiss plan: {plan.kind} '{plan.target_name}' ===")
    lines.append("")
    lines.append("Files / directories to REMOVE:")
    if not plan.removals:
        lines.append("  (none)")
    else:
        for r in plan.removals:
            lines.append(f"  - {_format_path(r.path, teams_root)}")
            lines.append(f"      ({r.description})")
    if plan.edits:
        lines.append("")
        lines.append("Files to EDIT:")
        for e in plan.edits:
            lines.append(f"  - {_format_path(e.path, teams_root)}")
            lines.append(f"      ({e.description})")
    if plan.service_actions:
        lines.append("")
        lines.append("Systemd actions:")
        for s in plan.service_actions:
            lines.append(f"  - {s.description}")
    if plan.manual_reminders:
        lines.append("")
        lines.append("Out of scope (manual steps after this command):")
        for r in plan.manual_reminders:
            lines.append(f"  - {r}")
    lines.append("")
    lines.append("This action is IRREVERSIBLE. Back up anything you want")
    lines.append("to keep before proceeding.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

# Type alias kept loose so tests can pass a simple lambda mock without
# matching subprocess.run's exact signature.
SubprocessRunner = Callable[..., object]


def execute_plan(
    plan: DismissPlan,
    *,
    run_subprocess: SubprocessRunner = subprocess.run,
    out: Callable[[str], None] = print,
    err: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
) -> int:
    """Execute *plan*. Returns the number of errors encountered.

    Ordering matters: systemd actions first (so the unit isn't using
    files we're about to remove), then file edits, then removals (so a
    failed edit doesn't leave the team in an unrecoverable half-deleted
    state).
    """
    errors = 0

    for action in plan.service_actions:
        try:
            if action.kind == "stop_disable_unit":
                # Blocking stop: we're about to delete the unit's
                # state_dir + env file, and a racing bridge process can
                # still hold open handles on them. systemd's drain
                # budget (TimeoutStopSec=120 in the gen-service
                # template) bounds how long this can take. The
                # `dismiss-from-inside-the-bridge` deadlock scenario
                # isn't realistic for this command -- type-the-team-
                # name confirmation is interactive, not Slack-DM-able.
                run_subprocess(
                    [
                        "systemctl", "--user",
                        "stop", action.target,
                    ],
                    check=False, timeout=_SYSTEMCTL_TIMEOUT_S,
                )
                run_subprocess(
                    [
                        "systemctl", "--user",
                        "disable", action.target,
                    ],
                    check=False, timeout=_SYSTEMCTL_TIMEOUT_S,
                )
                out(f"  stopped & disabled {action.target}")
            elif action.kind == "remove_unit_file":
                p = Path(action.target)
                if p.exists():
                    p.unlink()
                    out(f"  removed {p}")
            elif action.kind == "daemon_reload":
                run_subprocess(
                    ["systemctl", "--user", "daemon-reload"],
                    check=False, timeout=_SYSTEMCTL_TIMEOUT_S,
                )
                out("  ran systemctl --user daemon-reload")
            else:
                err(f"  unknown service action kind: {action.kind!r}")
                errors += 1
        except (OSError, subprocess.TimeoutExpired) as exc:
            err(f"  error during {action.description}: {exc}")
            errors += 1

    for edit in plan.edits:
        if not edit.path.exists():
            continue
        # Atomic write: tmp + os.replace. A SIGKILL between the write
        # and the replace leaves the original file untouched, so a
        # half-applied dismiss can't corrupt the multi-bridge index.
        # If os.replace fails (permissions, cross-device, etc.) we
        # must remove the orphan tmp so a retry isn't blocked by
        # leftover state.
        tmp = edit.path.parent / (edit.path.name + ".dismiss-tmp")
        try:
            tmp.write_text(edit.new_content, encoding="utf-8")
            os.replace(str(tmp), str(edit.path))
            out(f"  edited {edit.path}")
        except OSError as exc:
            err(f"  error editing {edit.path}: {exc}")
            errors += 1
            # Best-effort cleanup. If the unlink itself fails (e.g.
            # tmp never got created because write_text was the OSError
            # source), there's nothing to remove.
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    for rm in plan.removals:
        try:
            if not rm.path.exists():
                continue
            if rm.path.is_dir():
                shutil.rmtree(rm.path)
            else:
                rm.path.unlink()
            out(f"  removed {rm.path}")
        except OSError as exc:
            err(f"  error removing {rm.path}: {exc}")
            errors += 1

    return errors


# ---------------------------------------------------------------------------
# Interactive picker + prompts
# ---------------------------------------------------------------------------

InputFn = Callable[[str], str]
OutFn = Callable[[str], None]


def _pick_target(
    teams_root: Path,
    *,
    input_fn: InputFn = input,
    out: OutFn = print,
) -> tuple[str, str, str | None]:
    """Interactive picker. Returns ``(kind, team, persona|None)``.

    Raises ValueError when there's nothing to dismiss; EOFError /
    KeyboardInterrupt propagates so ``main`` can convert it to the
    standard abort exit code.
    """
    teams = discover_teams(teams_root)
    if not teams:
        raise ValueError(
            f"no teams found under {teams_root}. Nothing to dismiss."
        )
    # Refuse the flat layout (teams_root IS itself a team) -- dismissing
    # the cwd is a foot-gun nobody wants.
    if len(teams) == 1 and teams[0] == teams_root.resolve():
        raise ValueError(
            f"{teams_root} is itself a team directory (contains "
            f"configs/personas.yaml). Run dismiss from the parent "
            f"directory, or remove the team manually."
        )

    out("What would you like to dismiss? "
        "(type 'q' to abort at any prompt)")
    out("  1) An entire team "
        "(removes all personas, configs, memory, bridge lane)")
    out("  2) A single persona "
        "(keeps the team and other personas)")
    while True:
        choice = input_fn("Selection [1/2/q]: ").strip()
        _maybe_abort(choice)
        if choice == "1":
            kind = "team"
            break
        if choice == "2":
            kind = "persona"
            break
        out("  Please enter 1 or 2 (or 'q' to abort).")

    # Single-team case: prompting "Team [1-1]:" wastes the user's time.
    # Show what's being targeted, then move on.
    if len(teams) == 1:
        team_dir = teams[0]
        out("")
        personas = list_personas_in_team(team_dir)
        names_str = ", ".join(personas) if personas else "(none)"
        out(f"Only one team available: {team_dir.name} "
            f"({len(personas)} persona(s): {names_str})")
    else:
        out("")
        out("Available teams:")
        for i, t in enumerate(teams, 1):
            personas = list_personas_in_team(t)
            names_str = ", ".join(personas) if personas else "(none)"
            out(f"  {i}. {t.name} "
                f"({len(personas)} persona(s): {names_str})")
        while True:
            sel = input_fn(f"Team [1-{len(teams)}/q]: ").strip()
            _maybe_abort(sel)
            if sel.isdigit() and 1 <= int(sel) <= len(teams):
                team_dir = teams[int(sel) - 1]
                break
            out(f"  Please enter a number 1-{len(teams)} (or 'q').")
    team = team_dir.name

    if kind == "team":
        return "team", team, None

    personas = list_personas_in_team(team_dir)
    if not personas:
        raise ValueError(
            f"team {team!r} has no personas. Nothing to dismiss at "
            f"persona level."
        )
    out("")
    out(f"Personas in team {team}:")
    for i, p in enumerate(personas, 1):
        out(f"  {i}. {p}")
    while True:
        sel = input_fn(f"Persona [1-{len(personas)}/q]: ").strip()
        _maybe_abort(sel)
        if sel.isdigit() and 1 <= int(sel) <= len(personas):
            persona = personas[int(sel) - 1]
            break
        out(f"  Please enter a number 1-{len(personas)} (or 'q').")
    return "persona", team, persona


def _maybe_abort(value: str) -> None:
    """Raise KeyboardInterrupt if *value* is a quit token. Lets users
    bail out of the picker without ctrl-c. main() catches it and
    returns the standard 130 exit code."""
    if value.lower() in ("q", "quit", "exit"):
        raise KeyboardInterrupt


def _prompt_yes_no(
    question: str,
    *,
    default: bool = False,
    input_fn: InputFn = input,
    out: OutFn = print,
) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        line = input_fn(f"{question} [{suffix}]: ").strip().lower()
        if not line:
            return default
        if line in ("y", "yes"):
            return True
        if line in ("n", "no"):
            return False
        out("  please answer 'y' or 'n'.")


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """CLI entry point for ``tigerharness dismiss``."""
    parser = argparse.ArgumentParser(
        prog="tigerharness dismiss",
        description=(
            "Interactively dismiss (destroy) a team or persona. "
            "DESTRUCTIVE: removes files, edits configs, and (for the "
            "last team in a multi-team setup) stops the slack-bridge-"
            "multi systemd user unit."
        ),
    )
    parser.add_argument(
        "--dir", default=".",
        help="Teams root directory (default: current directory).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the dismissal plan and exit without prompting or "
             "deleting anything.",
    )
    args = parser.parse_args(argv)
    teams_root = Path(args.dir).resolve()

    # Pass input/print explicitly so monkeypatching builtins.input in
    # tests reaches _pick_target's prompt loop. Default values on inner
    # helpers are bound at function-def time, not at lookup time, so a
    # bare `_pick_target(teams_root)` would skip the monkeypatch.
    try:
        kind, team, persona = _pick_target(
            teams_root, input_fn=input, out=print,
        )
        if kind == "team":
            plan = build_team_plan(team=team, teams_root=teams_root)
        else:
            assert persona is not None
            plan = build_persona_plan(
                team=team, persona=persona, teams_root=teams_root,
            )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\naborted.", file=sys.stderr)
        return 130

    print(render_preview(plan, teams_root=teams_root))

    if args.dry_run:
        print("(dry-run -- nothing was changed)")
        return 0

    try:
        if not _prompt_yes_no(
            "Have you backed up everything you want to keep?",
            default=False,
            input_fn=input,
            out=print,
        ):
            print("aborted -- back up first, then re-run.")
            return 1
        typed = input(
            f"Type the {plan.kind} name '{plan.target_name}' to confirm: "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        print("\naborted.", file=sys.stderr)
        return 130

    if typed != plan.target_name:
        print(
            f"aborted -- you typed {typed!r}, expected "
            f"{plan.target_name!r}.",
            file=sys.stderr,
        )
        return 1

    print("")
    print(f"Executing dismissal of {plan.kind} '{plan.target_name}'...")
    errors = execute_plan(plan)
    print("")
    if errors:
        print(
            f"completed with {errors} error(s) -- see messages above.",
            file=sys.stderr,
        )
    else:
        print("done.")
    if plan.manual_reminders:
        print("")
        print("Remaining manual steps:")
        for r in plan.manual_reminders:
            print(f"  - {r}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

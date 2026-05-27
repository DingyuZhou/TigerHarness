"""Scaffold a new tigerharness persona inside a team folder.

Every persona belongs to a team. A team is a self-contained directory
with this layout::

    <team>/
        configs/
            personas.yaml                    -- team's persona registry
            .env                             -- slack tokens (optional, gitignored)
        skills/                              -- team-shared skills (.md)
        personas/
            <persona>/
                prompt.md                    -- the persona's system prompt
        memories/
            <persona>/
                tiger-memory.config.yaml     -- per-persona memory config
                # store data (archive/, journal/, briefing/) accumulates
                # in this directory at runtime.

`tigerharness init` walks the user through:
    1. Pick a persona name.
    2. Pick an existing team (auto-discovered from the cwd) or create one.
    3. Generate templates: prompt, memory config, slack .env, registry.

Non-interactive use::

    tigerharness init --persona chief --team tigers --yes
    tigerharness init --persona scout --team tigers --no-memory --no-slack
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Valid persona/team names: letters, digits, dash, underscore. The
# folder names show up in paths and shell commands, so we keep them
# strict rather than POSIX-permissive.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

_PERSONA_TEMPLATE = """\
You are {persona}, part of team {team}.

Your job is to help the user accomplish their goals thoroughly and
present clear results.

## Working style

- Start each task by clarifying the request.
- Search for primary sources before secondary ones.
- Present findings as structured markdown with headers.
- Flag uncertainty explicitly: "I'm not sure about X because..."

## Output format

Use markdown. Start with a one-paragraph summary, then detail sections.
End with "Next steps" if the work suggests follow-up.
"""

_ENV_TEMPLATE = """\
# Slack bridge environment variables (legacy single-tenant template).
# Fill in your tokens from https://api.slack.com/apps

# Required: Slack app-level token (starts with xapp-)
SLACK_APP_TOKEN=xapp-1-your-app-token

# Required: Slack bot token (starts with xoxb-)
SLACK_BOT_TOKEN=xoxb-your-bot-token

# Required: comma-separated Slack user IDs allowed to interact
ALLOWED_SLACK_USER_IDS=U0123ABCDEF

# Optional: working directory for the Claude agent
TIGERHARNESS_AGENT_CWD=.
"""

# Multi-team .env: tokens only. The allowlist lives in
# `configs/slack-bridge.yaml`'s `allowed_user_ids` field -- one source
# of truth in multi-team mode. The notify CLI also reads from there
# (via cwd/configs/slack-bridge.yaml auto-discovery).
_ENV_TEMPLATE_MULTI_TEAM = """\
# Slack bridge environment variables (multi-team mode).
# Fill in your tokens from https://api.slack.com/apps.
# In this mode, the allowlist lives in configs/slack-bridge.yaml --
# this file is for SECRETS only.

# Required: Slack app-level token (starts with xapp-)
SLACK_APP_TOKEN=xapp-1-your-app-token

# Required: Slack bot token (starts with xoxb-)
SLACK_BOT_TOKEN=xoxb-your-bot-token
"""

_PERSONAS_YAML_HEADER = """\
# Team: {team}
# Personas registry for tigerharness.task_runner.
#
# Load this file by pointing the env var at it (absolute or relative path):
#     export TIGERHARNESS_PERSONAS_CONFIG=path/to/{team}/configs/personas.yaml
#
# `personas_dir`, `cwd`, and `extra.add_dirs` are resolved relative to
# THIS FILE'S directory, not your cwd at load time, so the team folder
# can be moved or invoked from anywhere.
#
# Convention used by `tigerharness init`:
#   - `personas_dir: ../personas` -- prompts live next to this folder
#   - `cwd: ..` on every persona  -- agents run from the team root
#   - uncomment `extra.add_dirs: [../skills]` to expose team skills
# Schema: tigerharness.task_runner.personas.load_personas_config.

personas_dir: ../personas

personas:
"""

_PERSONA_ENTRY = """\
  - name: {persona}
    cwd: ..
    prompt_file: {persona}/prompt
    description: "{description}"
    # extra:
    #   add_dirs: [../skills]   # uncomment to expose team-shared skills
"""

_MEMORY_CONFIG_TEMPLATE = """\
# tiger-memory config for persona '{persona}' (team '{team}').
# Docs: https://github.com/DingyuZhou/TigerHarness/blob/main/docs/tiger-memory.md

agent:
  name: {persona}
  role: "A helpful assistant on team {team}."

store:
  # Memory store lives in this directory (memories/{persona}/).
  root: .

sources:
{sources_block}\
summarizer:
  backend: anthropic
  model: claude-sonnet-4-6
  prompts: default/v1

rebuild:
  lock_path: /tmp/tiger-memory-{team}-{persona}.lock
  idle_threshold_hours: 1
  resummarize_window_days: 7
  rebuild_timeout_minutes: 60
"""

_PROJECT_PATH_DETECTED_COMMENT = (
    "    # ^ Auto-detected: Claude Code transcripts dir for this team.\n"
    "    #   Override if this persona reads from a different project.\n"
)
_PROJECT_PATH_EXPECTED_COMMENT = (
    "    # ^ Where Claude Code WILL write transcripts for this team.\n"
    "    #   The dir is created on the first claude_p dispatch.\n"
)

_SKILLS_README = """\
# Team skills

Drop team-shared skills here as `<skill-name>.md` files. Each persona
in `../configs/personas.yaml` has a commented `extra.add_dirs` hint --
uncomment it (per persona, or for the whole team) to expose this
folder to that agent.
"""

_GITIGNORE = """\
# Tigerharness team -- secrets and runtime memory state
configs/.env
memories/*/archive/
memories/*/journal/
memories/*/briefing/
memories/*/cache/
memories/*/state.json
"""

# .claude/settings.json -- env vars that Claude Code injects into agent
# subprocesses. The persona config path lets the task-runner and its
# detached children auto-discover the team's persona registry.
_CLAUDE_SETTINGS_TEMPLATE = """\
{{
  "env": {{
    "TIGERHARNESS_PERSONAS_CONFIG": "{personas_config_abs}"
  }}
}}
"""

# Multi-lane slack-bridge fragment. Generated only when a top-level
# slack-bridge.yaml index exists in the search root -- i.e., the user
# has opted into multi-team mode. See `multi.load_multi` for the loader.
_SLACK_BRIDGE_FRAGMENT_TEMPLATE = """\
# Multi-lane slack-bridge fragment for team '{team}'.
# Loaded by tigerharness.slack_bridge.multi.load_multi() via the
# top-level slack-bridge.yaml index.

# Persona used when the user's first DM doesn't address a specific
# team member. ANY persona in this team's configs/personas.yaml is
# reachable -- a user can DM "Hi <name>" to talk to that member.
# This default kicks in only when no name was addressed.
default_persona: {persona}

# Slack user IDs allowed to DM this team's bot. Required (non-empty list).
# Find your user ID at api.slack.com/methods/users.list, or the Slack
# app -> Profile -> ... menu -> Copy member ID.
allowed_user_ids: []  # TODO: add at least one user ID before starting the bridge

# Where this lane persists its thread -> session map. Must be unique
# across lanes (the multi loader rejects duplicates).
state_dir: ~/.local/state/slack-bridge/{team}

# Optional overrides (defaults shown):
# env: configs/.env
# agent_cwd: .
"""


# ---------------------------------------------------------------------------
# Name validation
# ---------------------------------------------------------------------------

def _validate_name(value: str, *, kind: str) -> str:
    """Raise ValueError if *value* is unsafe to use as a folder name.

    Folder names appear in paths, shell snippets, and YAML keys.
    Restrict to ``[A-Za-z0-9_-]`` starting with an alphanumeric.
    """
    v = (value or "").strip()
    if not v:
        raise ValueError(f"{kind} name cannot be empty")
    if not _NAME_RE.fullmatch(v):
        raise ValueError(
            f"invalid {kind} name {value!r}: use letters, digits, '-', "
            f"and '_' only (must start with a letter or digit)"
        )
    return v


# ---------------------------------------------------------------------------
# Command-prefix detection for "Next steps" output
# ---------------------------------------------------------------------------

def _command_prefix() -> str:
    """Return ``"uv run "`` when ``tigerharness`` isn't on ``PATH``.

    A user who installed via ``uv add tigerharness`` (the recommended
    "scoped to this folder" recipe) will have the entrypoint inside
    ``.venv/bin`` but no ``.venv/bin`` on their shell ``PATH`` -- so a
    bare ``tigerharness ...`` line in our "Next steps" output would
    fail with "command not found". Prefixing with ``uv run`` fixes it.

    Cases handled correctly:
      - ``uv add`` inside a uv project    -> prefix is "uv run "
      - ``uv tool install tigerharness``  -> on PATH, prefix is ""
      - ``pip install`` in active venv    -> on PATH, prefix is ""
      - ``pipx install tigerharness``     -> on PATH, prefix is ""
    """
    return "" if shutil.which("tigerharness") else "uv run "


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

def _write_if_missing(path: Path, content: str) -> bool:
    """Write *content* to *path* iff it doesn't already exist.

    Returns True when a file was created, False when one already existed.
    """
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def discover_teams(search_root: Path) -> list[Path]:
    """Return team directories under *search_root*.

    A directory is a team iff it contains ``configs/personas.yaml``.
    If *search_root* itself is a team, returns just ``[search_root]``;
    otherwise scans its immediate children.
    """
    if (search_root / "configs" / "personas.yaml").exists():
        return [search_root]
    teams: list[Path] = []
    if not search_root.is_dir():
        return teams
    for child in sorted(search_root.iterdir()):
        if child.is_dir() and (child / "configs" / "personas.yaml").exists():
            teams.append(child)
    return teams


def list_personas_in_team(team_dir: Path) -> list[str]:
    """List persona names by scanning the team's personas/ directory."""
    personas_dir = team_dir / "personas"
    if not personas_dir.is_dir():
        return []
    return sorted(
        child.name
        for child in personas_dir.iterdir()
        if child.is_dir() and (child / "prompt.md").exists()
    )


def detect_claude_project_path(
    project_root: Path, *, home: Path | None = None
) -> Path | None:
    """Locate the Claude Code transcripts directory for *project_root*.

    Claude Code stores per-project transcripts under
    ``~/.claude/projects/<encoded>/`` where ``<encoded>`` is the
    absolute project path with ``/`` replaced by ``-``. Returns the
    directory if it exists, else None.
    """
    abs_root = project_root.resolve()
    encoded = str(abs_root).replace("/", "-")
    candidate = (home or Path.home()) / ".claude" / "projects" / encoded
    return candidate if candidate.is_dir() else None


def expected_claude_project_path(
    project_root: Path, *, home: Path | None = None
) -> Path:
    """Compute where Claude Code WILL write transcripts for *project_root*,
    whether or not the directory exists yet.

    Useful when scaffolding a fresh team: the agent hasn't run yet, so
    the encoded dir doesn't exist, but we know exactly where it will
    appear once the bridge dispatches its first message. Writing the
    real path now means the user never has to come back and edit it.
    """
    abs_root = project_root.resolve()
    encoded = str(abs_root).replace("/", "-")
    return (home or Path.home()) / ".claude" / "projects" / encoded


# ---------------------------------------------------------------------------
# Team / persona scaffolding
# ---------------------------------------------------------------------------

def _scaffold_claude_dir(team_dir: Path) -> list[Path]:
    """Create ``.claude/settings.json`` and ``.claude/skills/`` for a team.

    Claude Code reads ``.claude/settings.json`` from the project root
    to inject env vars into ``claude -p`` subprocesses, and discovers
    skills from ``.claude/skills/<name>/SKILL.md``. Scaffolding these
    at ``tigerharness init`` time means every new team gets:

    - ``TIGERHARNESS_PERSONAS_CONFIG`` wired up automatically, so the
      task-runner and its detached children find the team's personas.
    - ``slack-notify`` and ``assign-task`` skills, so agents know how
      to send Slack messages and assign background tasks.

    Skills are read from the ``skills/`` directory shipped inside the
    tigerharness package. If a skill file already exists on disk, it's
    left untouched (idempotent, user edits preserved).
    """
    created: list[Path] = []

    # settings.json
    personas_cfg_abs = str((team_dir / "configs" / "personas.yaml").resolve())
    settings_path = team_dir / ".claude" / "settings.json"
    content = _CLAUDE_SETTINGS_TEMPLATE.format(
        personas_config_abs=personas_cfg_abs,
    )
    if _write_if_missing(settings_path, content):
        created.append(settings_path)

    # Skills: copy from tigerharness package's _bundled_skills/ directory.
    # Each skill lives at _bundled_skills/<name>/SKILL.md, shipped as
    # package data so they're available in installed wheels too.
    pkg_skills_dir = Path(__file__).resolve().parent / "_bundled_skills"
    if pkg_skills_dir.is_dir():
        for skill_dir in sorted(pkg_skills_dir.iterdir()):
            skill_src = skill_dir / "SKILL.md"
            if not skill_src.is_file():
                continue
            dest = team_dir / ".claude" / "skills" / skill_dir.name / "SKILL.md"
            if _write_if_missing(dest, skill_src.read_text(encoding="utf-8")):
                created.append(dest)

    return created


def create_team(
    team_dir: Path, *, include_slack: bool, multi_team: bool = False,
) -> list[Path]:
    """Create the empty scaffold for a team. Returns paths created.

    *multi_team* selects which .env template to use: the multi-team
    one carries tokens only (allowlist lives in the yaml fragment),
    the legacy one bundles the allowlist via ``ALLOWED_SLACK_USER_IDS``.
    """
    created: list[Path] = []

    gi = team_dir / ".gitignore"
    if _write_if_missing(gi, _GITIGNORE):
        created.append(gi)

    pyaml = team_dir / "configs" / "personas.yaml"
    if _write_if_missing(pyaml, _PERSONAS_YAML_HEADER.format(team=team_dir.name)):
        created.append(pyaml)

    if include_slack:
        env = team_dir / "configs" / ".env"
        template = _ENV_TEMPLATE_MULTI_TEAM if multi_team else _ENV_TEMPLATE
        if _write_if_missing(env, template):
            created.append(env)

    skills = team_dir / "skills" / "README.md"
    if _write_if_missing(skills, _SKILLS_README):
        created.append(skills)

    # .claude/ directory: settings.json + skills from the package.
    created.extend(_scaffold_claude_dir(team_dir))

    return created


def _append_persona_to_yaml(
    yaml_path: Path,
    persona: str,
    description: str,
    team_name: str,
) -> bool:
    """Append a persona entry to personas.yaml.

    Returns True when an entry was appended, False when the persona was
    already present (idempotent).
    """
    entry = _PERSONA_ENTRY.format(persona=persona, description=description)
    if yaml_path.exists():
        text = yaml_path.read_text()
        # Match the exact form `_PERSONA_ENTRY` produces, anchored to the
        # leading indent so a stray substring in a description can't be
        # mistaken for an existing entry.
        if f"  - name: {persona}\n" in text:
            return False
        if not text.endswith("\n"):
            text += "\n"
        yaml_path.write_text(text + entry)
        return True
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    yaml_path.write_text(
        _PERSONAS_YAML_HEADER.format(team=team_name) + entry
    )
    return True


def _render_memory_config(
    *, persona: str, team: str, project_root: Path,
    multi_team: bool = False,
    home: Path | None = None,
) -> str:
    """Render the per-persona tiger-memory config.

    Always produces a real project_path (auto-detected if the Claude
    Code transcripts dir already exists, or computed-from-team-root if
    not). The user never has to come back and fix a placeholder.

    When *multi_team* is True (i.e. the slack-bridge multi-mode index
    exists), also adds the ``persona:`` filter on the claude_code
    source and a ``slack_thread`` source pointing at the bridge's
    threads.json -- so the memory store ingests only this persona's
    Slack conversations.
    """
    detected = detect_claude_project_path(project_root, home=home)
    if detected is not None:
        project_path = f"{detected}/"
        comment = _PROJECT_PATH_DETECTED_COMMENT
    else:
        # Even when the transcripts dir doesn't exist yet, emit the
        # correct expected path -- it'll exist as soon as the agent runs.
        project_path = f"{expected_claude_project_path(project_root, home=home)}/"
        comment = _PROJECT_PATH_EXPECTED_COMMENT

    if multi_team:
        sources_block = (
            f"  - kind: claude_code\n"
            f"    project_path: {project_path}\n"
            f"{comment}"
            f"    # Filter: only ingest threads where the bridge stored\n"
            f"    # persona == \"{persona}\". Excludes other personas' threads\n"
            f"    # and unattributed local `claude -p` sessions (strict mode).\n"
            f"    persona: {persona}\n"
            f"  - kind: slack_thread\n"
            f"    # The bridge's per-team state file -- provides the\n"
            f"    # session_id -> (thread_ts, persona) reverse map used\n"
            f"    # by the per-persona filter above.\n"
            f"    threads_json: ~/.local/state/slack-bridge/{team}/threads.json\n"
        )
    else:
        sources_block = (
            f"  - kind: claude_code\n"
            f"    project_path: {project_path}\n"
            f"{comment}"
        )

    return _MEMORY_CONFIG_TEMPLATE.format(
        persona=persona, team=team,
        sources_block=sources_block,
    )


def add_persona(
    team_dir: Path,
    persona: str,
    *,
    include_memory: bool,
    description: str = "",
    multi_team: bool = False,
    home: Path | None = None,
) -> list[Path]:
    """Add a persona to a team. Returns paths created or updated.

    *home* overrides ``~`` for Claude transcripts detection (testing).
    *multi_team* indicates the slack-bridge index exists -- when True,
    the per-persona memory config is generated with the ``persona:``
    filter + a ``slack_thread`` source pointing at the bridge's state.

    Raises ValueError if the persona's prompt already exists.
    """
    persona_dir = team_dir / "personas" / persona
    prompt_path = persona_dir / "prompt.md"
    if prompt_path.exists():
        raise ValueError(
            f"persona {persona!r} already exists in team {team_dir.name!r} "
            f"(prompt at {prompt_path}). Remove it to recreate."
        )

    created: list[Path] = []

    # prompt.md is guaranteed missing (asserted above).
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(
        _PERSONA_TEMPLATE.format(persona=persona, team=team_dir.name),
        encoding="utf-8",
    )
    created.append(prompt_path)

    if include_memory:
        mem_cfg = team_dir / "memories" / persona / "tiger-memory.config.yaml"
        if _write_if_missing(
            mem_cfg,
            _render_memory_config(
                persona=persona, team=team_dir.name,
                project_root=team_dir, multi_team=multi_team, home=home,
            ),
        ):
            created.append(mem_cfg)

    yaml_path = team_dir / "configs" / "personas.yaml"
    desc = description or f"{persona} on team {team_dir.name}"
    if _append_persona_to_yaml(yaml_path, persona, desc, team_dir.name):
        created.append(yaml_path)

    return created


# ---------------------------------------------------------------------------
# Multi-lane slack-bridge integration
# ---------------------------------------------------------------------------

def _append_lane_to_slack_bridge_index(
    index_path: Path, team_name: str
) -> bool:
    """Append ``  - {team_name}\\n`` to the top-level slack-bridge.yaml
    index. Idempotent. Returns True if the file was modified.

    Handles three pre-existing states of the index file:
      - Has ``lanes:`` header + entries -> append new entry at the end.
      - Has no ``lanes:`` header (empty / comments only) -> write the
        header and the entry.
      - Already contains the entry -> no-op.
    """
    needle = f"  - {team_name}\n"
    content = index_path.read_text(encoding="utf-8")
    if needle in content:
        return False
    if "lanes:" not in content:
        if content and not content.endswith("\n"):
            content += "\n"
        content += "lanes:\n" + needle
    else:
        if not content.endswith("\n"):
            content += "\n"
        content += needle
    index_path.write_text(content, encoding="utf-8")
    return True


def _maybe_register_slack_bridge_lane(
    search_root: Path, team_dir: Path, team: str, persona: str,
    *, enabled: bool = True,
) -> list[Path]:
    """When multi-team mode is enabled AND the top-level
    ``slack-bridge.yaml`` index exists in *search_root*, write the
    per-team fragment (idempotent) and append the team to the index
    (idempotent).

    *enabled* short-circuits the entire operation. This matters when an
    index exists from a previous run but the caller explicitly opted
    THIS team out (``--no-multi-team``): without ``enabled=False`` we'd
    register the team as a lane while ``create_team`` / ``add_persona``
    wrote single-tenant artifacts -- a half-and-half state the bridge
    can't safely load.
    """
    if not enabled:
        return []
    index_path = search_root / "slack-bridge.yaml"
    if not index_path.exists():
        return []
    created: list[Path] = []
    fragment_path = team_dir / "configs" / "slack-bridge.yaml"
    if _write_if_missing(
        fragment_path,
        _SLACK_BRIDGE_FRAGMENT_TEMPLATE.format(persona=persona, team=team),
    ):
        created.append(fragment_path)
    if _append_lane_to_slack_bridge_index(index_path, team):
        created.append(index_path)
    return created


# ---------------------------------------------------------------------------
# Interactive prompts (stdlib only -- no extra deps)
# ---------------------------------------------------------------------------

def _prompt_text(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        line = input(f"{question}{suffix}: ").strip()
        if line:
            return line
        if default:
            return default


def _prompt_optional_text(question: str) -> str:
    """Prompt for free-form text; empty input returns ``""``. Unlike
    ``_prompt_text``, doesn't loop on empty input -- used for opt-in
    fields where ``[skip]`` is a valid answer."""
    return input(f"{question}: ").strip()


def _prompt_yes_no(question: str, default: bool = True) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        line = input(f"{question} [{suffix}]: ").strip().lower()
        if not line:
            return default
        if line in ("y", "yes"):
            return True
        if line in ("n", "no"):
            return False


def _prompt_choice(
    question: str, options: list[str], default_idx: int = 0
) -> int:
    print(question)
    for i, opt in enumerate(options, 1):
        marker = " (default)" if i - 1 == default_idx else ""
        print(f"  {i}. {opt}{marker}")
    while True:
        line = input(f"Selection [{default_idx + 1}]: ").strip()
        if not line:
            return default_idx
        if line.isdigit():
            n = int(line)
            if 1 <= n <= len(options):
                return n - 1
        print(f"  Please enter a number 1-{len(options)}.")


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def init(
    *,
    persona: str | None = None,
    team: str | None = None,
    team_dir: Path | None = None,
    include_memory: bool | None = None,
    include_slack: bool | None = None,
    include_multi_team: bool | None = None,
    search_root: Path | None = None,
    home: Path | None = None,
) -> tuple[Path, str, list[Path]]:
    """Run the init flow.

    Any non-None argument suppresses the matching interactive prompt.
    *home* overrides ``~`` for Claude transcripts auto-detect (testing).
    Returns ``(team_dir, persona, created_paths)``.

    Raises:
        ValueError: persona already exists in the team.
    """
    root = (search_root or Path.cwd()).resolve()
    created: list[Path] = []

    # 0. multi-team mode opt-in.
    # When *include_multi_team* is True and no top-level slack-bridge.yaml
    # index exists yet, touch it so `_maybe_register_slack_bridge_lane`
    # downstream picks up the team. ``None`` (scripted-default) preserves
    # legacy single-tenant behavior; ``False`` is the explicit opt-out.
    # Interactive prompting happens in `main()` so callers (tests,
    # importing scripts) of `init()` don't hit stdin reads.
    index_path = root / "slack-bridge.yaml"
    if include_multi_team and not index_path.exists():
        index_path.write_text("", encoding="utf-8")
        created.append(index_path)

    # 1. persona name
    if persona is None:
        persona = _prompt_text("Persona name", "assistant")
    persona = _validate_name(persona, kind="persona")

    # 2. team selection / creation
    if team is None and team_dir is None:
        existing = discover_teams(root)
        if existing:
            options: list[str] = []
            for t in existing:
                names = list_personas_in_team(t)
                names_str = ", ".join(names) if names else "no personas yet"
                rel = "." if t == root else t.name
                options.append(f"{t.name} ({rel} -- {names_str})")
            options.append("<Create new team>")
            idx = _prompt_choice(
                "\nChoose a team:", options, default_idx=0
            )
            if idx < len(existing):
                final_team_dir = existing[idx]
                team = final_team_dir.name
            else:
                team = _validate_name(
                    _prompt_text("New team name", "tigers"), kind="team"
                )
                final_team_dir = root / team
        else:
            print("\nNo existing teams found in this directory.")
            team = _validate_name(
                _prompt_text("New team name", "tigers"), kind="team"
            )
            final_team_dir = root / team
    elif team_dir is not None:
        final_team_dir = team_dir.resolve()
        if team is None:
            team = final_team_dir.name
        team = _validate_name(team, kind="team")
    else:
        team = _validate_name(team, kind="team")
        final_team_dir = (root / team).resolve()

    # 3. options
    env_path = final_team_dir / "configs" / ".env"
    if include_slack is None:
        if env_path.exists():
            # Don't ask -- nothing for us to write.
            include_slack = False
        else:
            include_slack = _prompt_yes_no(
                "Generate Slack-bridge .env template?", default=True
            )

    # Multi-team mode is conditional on Slack -- no point asking a
    # non-Slack user about a Slack feature. When include_multi_team is
    # None (scripted default OR interactive-no-flag), gate the decision
    # on whether Slack is going to be set up:
    #   - No slack  -> no multi-team (deterministic, no prompt).
    #   - Slack on  -> check existing index, else prompt.
    # Callers can pass `include_multi_team=True` to force an opt-in
    # even without Slack (advanced: pre-creating the index before
    # Slack apps are ready).
    if include_multi_team is None:
        if not include_slack:
            include_multi_team = False
        elif index_path.exists():
            include_multi_team = True
        else:
            try:
                include_multi_team = _prompt_yes_no(
                    "Enable multi-team Slack mode? (One bridge serves "
                    "many teams; recommended)",
                    default=True,
                )
            except (EOFError, KeyboardInterrupt):
                # Re-raise so main()'s handler catches it like other
                # interrupted prompts.
                raise

    if include_multi_team and not include_slack:
        # Unusual but allowed: pre-creating the multi-team index before
        # Slack apps are configured. Surface a warning so a typo doesn't
        # silently land the user in a half-set-up state they can't run.
        print(
            "warning: --multi-team is on but --no-slack is set. The "
            "index will be created, but the bridge can't run without "
            "Slack tokens. Enable Slack later (manually fill in "
            "configs/.env) or re-run init for this team with `--slack`.",
            file=sys.stderr,
        )

    if include_multi_team and not index_path.exists():
        index_path.write_text("", encoding="utf-8")
        created.append(index_path)

    if include_memory is None:
        include_memory = _prompt_yes_no(
            f"Initialize tiger-memory for {persona}?", default=True
        )

    # 4. create
    is_new_team = not (final_team_dir / "configs" / "personas.yaml").exists()
    is_multi_team = include_multi_team
    if is_new_team:
        created.extend(create_team(
            final_team_dir,
            include_slack=include_slack,
            multi_team=is_multi_team,
        ))
    elif include_slack:
        env_template = _ENV_TEMPLATE_MULTI_TEAM if is_multi_team else _ENV_TEMPLATE
        if _write_if_missing(env_path, env_template):
            created.append(env_path)

    # Memory config wiring depends on whether multi-team mode is on:
    # when on, the per-persona memory config gets the ``persona:`` filter
    # + slack_thread source so each persona's memory store only ingests
    # threads belonging to that persona. (`is_multi_team` was already
    # computed above in the create-team step.)
    for p in add_persona(
        final_team_dir, persona,
        include_memory=include_memory,
        multi_team=is_multi_team,
        home=home,
    ):
        if p not in created:
            created.append(p)

    # 5. multi-lane slack-bridge auto-registration. Gated on
    # *include_multi_team* (and the index file's existence) so that
    # `--no-multi-team` doesn't half-register a team into an existing
    # multi-team index while writing single-tenant artifacts elsewhere.
    for p in _maybe_register_slack_bridge_lane(
        root, final_team_dir, team, persona,
        enabled=bool(include_multi_team),
    ):
        if p not in created:
            created.append(p)

    # 6. Auto-init the persona's memory store. The bridge starts firing
    # rebuilds on the very first thread, so the archive/journal/briefing
    # dirs need to exist BEFORE the bridge dispatches anything. Doing
    # this here saves the user from running `tiger-memory init` by hand.
    if include_memory:
        mem_cfg = (
            final_team_dir / "memories" / persona / "tiger-memory.config.yaml"
        )
        if mem_cfg.exists():
            _auto_init_tiger_memory(mem_cfg)

    return final_team_dir, persona, created


def _inject_allowed_user_ids(fragment_path: Path, raw_csv: str) -> None:
    """Replace the ``allowed_user_ids: []`` placeholder in *fragment_path*
    with a real YAML list parsed from *raw_csv*. Idempotent: if the line
    already has a populated list, this is a no-op."""
    ids = [u.strip() for u in raw_csv.split(",") if u.strip()]
    if not ids:
        return
    text = fragment_path.read_text(encoding="utf-8")
    placeholder = (
        "allowed_user_ids: []  "
        "# TODO: add at least one user ID before starting the bridge"
    )
    if placeholder not in text:
        return  # already populated or template diverged -- don't touch
    yaml_list = "allowed_user_ids:\n" + "\n".join(f"  - {u}" for u in ids)
    text = text.replace(placeholder, yaml_list)
    fragment_path.write_text(text, encoding="utf-8")


def _auto_init_tiger_memory(mem_cfg: Path) -> None:
    """Run ``tiger-memory init`` via subprocess for *mem_cfg*.

    Subprocess (not direct import) keeps init.py decoupled from
    tiger-memory's lifecycle internals and means we use the exact
    same code path users invoke by hand. Failures are logged but
    non-fatal -- the user can always re-run `tiger-memory init`.
    """
    try:
        subprocess.run(
            [
                sys.executable, "-m", "tigerharness", "tiger-memory",
                "--config", str(mem_cfg), "init",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        # Non-fatal: print a hint so the user can fix it manually.
        # We use print not logging so this surfaces alongside the
        # interactive scaffolder's other output.
        print(
            f"warning: auto-init of tiger-memory store failed "
            f"({type(exc).__name__}); run by hand: "
            f"tigerharness tiger-memory --config {mem_cfg} init",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _format_path(p: Path, base: Path) -> str:
    """Format *p* relative to *base* when possible, else as-is."""
    try:
        return str(p.relative_to(base))
    except ValueError:
        return str(p)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for ``tigerharness init``."""
    parser = argparse.ArgumentParser(
        prog="tigerharness init",
        description="Create a persona inside a tigerharness team folder.",
    )
    parser.add_argument("--persona", help="Persona name (skips the prompt).")
    parser.add_argument("--team", help="Team name (skips the team picker).")
    parser.add_argument(
        "--team-dir",
        help="Custom team directory (default: <search-root>/<team>).",
    )
    parser.add_argument(
        "--no-memory",
        action="store_true",
        help="Skip the per-persona tiger-memory config.",
    )
    parser.add_argument(
        "--no-slack",
        action="store_true",
        help="Skip the Slack-bridge .env template.",
    )
    parser.add_argument(
        "--multi-team",
        action="store_true",
        help="Enable multi-team Slack mode (creates the top-level "
             "slack-bridge.yaml index). Skips the prompt.",
    )
    parser.add_argument(
        "--no-multi-team",
        action="store_true",
        help="Stay in legacy single-tenant mode. Skips the prompt.",
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Accept defaults non-interactively. Defaults to persona "
             "'assistant' and team 'tigers' if neither is provided.",
    )
    parser.add_argument(
        "--dir",
        default=".",
        help="Directory to search for existing teams "
             "(default: current directory).",
    )
    args = parser.parse_args(argv)

    search_root = Path(args.dir).resolve()

    persona_kw = args.persona
    team_kw = args.team
    team_dir_kw = Path(args.team_dir).resolve() if args.team_dir else None
    memory_kw: bool | None
    if args.no_memory:
        memory_kw = False
    elif args.yes:
        memory_kw = True
    else:
        memory_kw = None
    slack_kw: bool | None
    if args.no_slack:
        slack_kw = False
    elif args.yes:
        slack_kw = True
    else:
        slack_kw = None
    if args.yes:
        if not persona_kw:
            persona_kw = "assistant"
        if not team_kw and team_dir_kw is None:
            existing = discover_teams(search_root)
            # With --yes, fall back to deterministic defaults rather than
            # prompting: first existing team, or "tigers" if none exist.
            team_kw = existing[0].name if existing else "tigers"

    # Multi-team mode resolution: only honor explicit flags here; the
    # interactive prompt (and the no-slack auto-skip) live in init() so
    # the question is asked AFTER slack has been decided.
    multi_team_kw: bool | None
    if args.no_multi_team:
        multi_team_kw = False
    elif args.multi_team:
        multi_team_kw = True
    elif args.yes:
        # --yes accepts the recommended default, but only if Slack is
        # also being set up. `--yes --no-slack` opts out of both.
        multi_team_kw = (slack_kw is not False)
    else:
        # Interactive: defer to init()'s slack-gated decision.
        multi_team_kw = None

    try:
        team_dir, persona, created = init(
            persona=persona_kw,
            team=team_kw,
            team_dir=team_dir_kw,
            include_memory=memory_kw,
            include_slack=slack_kw,
            include_multi_team=multi_team_kw,
            search_root=search_root,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\naborted.", file=sys.stderr)
        return 130

    if not created:
        print("Nothing to do -- everything already exists.")
        return 0

    print(f"\nCreated {len(created)} item(s) under {_format_path(team_dir, search_root)}/:")
    for p in created:
        print(f"  {_format_path(p, search_root)}")

    # Interactive prompt for Slack user IDs when we just generated a
    # multi-bridge fragment. Skip in --yes mode (scripted callers) and
    # when the fragment already existed (don't re-prompt). Empty input
    # leaves the placeholder; user can fill it in later by hand.
    sb_fragment = team_dir / "configs" / "slack-bridge.yaml"
    if not args.yes and sb_fragment in created:
        try:
            ids_raw = _prompt_optional_text(
                "\nSlack user IDs allowed to DM this bot "
                "(comma-separated, blank to skip)"
            )
        except (EOFError, KeyboardInterrupt):
            ids_raw = ""
        if ids_raw:
            _inject_allowed_user_ids(sb_fragment, ids_raw)

    print()
    print("Next steps:")
    prefix = _command_prefix()
    steps: list[str] = [
        f"Edit {_format_path(team_dir / 'personas' / persona / 'prompt.md', search_root)}"
    ]
    env_path = team_dir / "configs" / ".env"
    if env_path.exists():
        steps.append(f"Fill in {_format_path(env_path, search_root)} (Slack tokens)")
    mem_cfg = team_dir / "memories" / persona / "tiger-memory.config.yaml"
    if mem_cfg.exists():
        mem_rel = _format_path(mem_cfg, search_root)
        # project_path is auto-detected (or filled with the expected path)
        # and the memory store has been auto-init'd. Tell the user it's
        # ready -- "review" is opt-in, not required.
        steps.append(
            f"(Optional) review {mem_rel} -- agent name/role, "
            f"summarizer model, idle threshold"
        )
    # Multi-lane slack-bridge: nudge the user to fill in the fragment.
    sb_fragment = team_dir / "configs" / "slack-bridge.yaml"
    if sb_fragment.exists():
        steps.append(
            f"Edit {_format_path(sb_fragment, search_root)} -- "
            f"add allowed_user_ids before starting the multi-bridge"
        )
    for i, step in enumerate(steps, 1):
        print(f"  {i}. {step}")

    personas_cfg = team_dir / "configs" / "personas.yaml"
    print()
    print("  To run tasks:")
    print(f"    export TIGERHARNESS_PERSONAS_CONFIG={_format_path(personas_cfg, search_root)}")
    print(f"    {prefix}tigerharness task-runner assign --to {persona} --prompt '...' --iters 5")
    return 0

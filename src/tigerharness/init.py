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
# Slack bridge environment variables
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
  - kind: claude_code
    project_path: {project_path}
{project_path_comment}\
summarizer:
  backend: anthropic
  model: claude-sonnet-4-6
  prompts: default/v1

rebuild:
  lock_path: /tmp/tiger-memory-{team}-{persona}.lock
  idle_threshold_hours: 2
  resummarize_window_days: 7
  rebuild_timeout_minutes: 60
"""

_PROJECT_PATH_PLACEHOLDER = "~/.claude/projects/-home-user-myproject/"
_PROJECT_PATH_PLACEHOLDER_COMMENT = (
    "    # ^ Replace with your Claude Code project path.\n"
    "    #   Find yours under ~/.claude/projects/ -- the directory name\n"
    "    #   encodes the absolute project path with dashes replacing slashes.\n"
)
_PROJECT_PATH_DETECTED_COMMENT = (
    "    # ^ Auto-detected from the team root. Override if the persona\n"
    "    #   reads transcripts from a different Claude Code project.\n"
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


# ---------------------------------------------------------------------------
# Team / persona scaffolding
# ---------------------------------------------------------------------------

def create_team(team_dir: Path, *, include_slack: bool) -> list[Path]:
    """Create the empty scaffold for a team. Returns paths created."""
    created: list[Path] = []

    gi = team_dir / ".gitignore"
    if _write_if_missing(gi, _GITIGNORE):
        created.append(gi)

    pyaml = team_dir / "configs" / "personas.yaml"
    if _write_if_missing(pyaml, _PERSONAS_YAML_HEADER.format(team=team_dir.name)):
        created.append(pyaml)

    if include_slack:
        env = team_dir / "configs" / ".env"
        if _write_if_missing(env, _ENV_TEMPLATE):
            created.append(env)

    skills = team_dir / "skills" / "README.md"
    if _write_if_missing(skills, _SKILLS_README):
        created.append(skills)

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
    home: Path | None = None,
) -> str:
    """Render the per-persona tiger-memory config.

    If a Claude Code transcripts directory exists for *project_root*,
    fill it in; otherwise emit the placeholder + 'fill me in' comment.
    """
    detected = detect_claude_project_path(project_root, home=home)
    if detected is not None:
        project_path = f"{detected}/"
        comment = _PROJECT_PATH_DETECTED_COMMENT
    else:
        project_path = _PROJECT_PATH_PLACEHOLDER
        comment = _PROJECT_PATH_PLACEHOLDER_COMMENT
    return _MEMORY_CONFIG_TEMPLATE.format(
        persona=persona, team=team,
        project_path=project_path,
        project_path_comment=comment,
    )


def add_persona(
    team_dir: Path,
    persona: str,
    *,
    include_memory: bool,
    description: str = "",
    home: Path | None = None,
) -> list[Path]:
    """Add a persona to a team. Returns paths created or updated.

    *home* overrides ``~`` for Claude transcripts detection (testing).

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
                project_root=team_dir, home=home,
            ),
        ):
            created.append(mem_cfg)

    yaml_path = team_dir / "configs" / "personas.yaml"
    desc = description or f"{persona} on team {team_dir.name}"
    if _append_persona_to_yaml(yaml_path, persona, desc, team_dir.name):
        created.append(yaml_path)

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
    if include_memory is None:
        include_memory = _prompt_yes_no(
            f"Initialize tiger-memory for {persona}?", default=True
        )

    # 4. create
    created: list[Path] = []
    is_new_team = not (final_team_dir / "configs" / "personas.yaml").exists()
    if is_new_team:
        created.extend(create_team(final_team_dir, include_slack=include_slack))
    elif include_slack:
        if _write_if_missing(env_path, _ENV_TEMPLATE):
            created.append(env_path)

    for p in add_persona(
        final_team_dir, persona,
        include_memory=include_memory,
        home=home,
    ):
        if p not in created:
            created.append(p)

    return final_team_dir, persona, created


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

    try:
        team_dir, persona, created = init(
            persona=persona_kw,
            team=team_kw,
            team_dir=team_dir_kw,
            include_memory=memory_kw,
            include_slack=slack_kw,
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
        steps.append(f"Edit {mem_rel} -- set sources.project_path")
        # `--config` is a top-level option, must precede the `init` subcommand.
        steps.append(
            f"Initialize memory: {prefix}tigerharness tiger-memory --config {mem_rel} init"
        )
    for i, step in enumerate(steps, 1):
        print(f"  {i}. {step}")

    personas_cfg = team_dir / "configs" / "personas.yaml"
    print()
    print("  To run tasks:")
    print(f"    export TIGERHARNESS_PERSONAS_CONFIG={_format_path(personas_cfg, search_root)}")
    print(f"    {prefix}tigerharness task-runner assign --to {persona} --prompt '...' --iters 5")
    return 0

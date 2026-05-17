"""Scaffold a new tigerharness project.

Creates the minimal config files a user needs to get started:
    personas/<name>.md    — sample persona prompt
    .env                  — Slack bridge env template
    tiger-memory.config.yaml — memory config (if --memory)

Usage:
    tigerharness init [--name NAME] [--memory] [--dir DIR]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# --- templates (inline to avoid pkg_resources / importlib.resources complexity) ---

_PERSONA_TEMPLATE = """\
You are {name}. Your job is to help the user accomplish their goals
thoroughly and present clear results.

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

# Optional: path to the agent's system prompt
TIGERHARNESS_AGENT_PROMPT=./personas/{name}.md

# Optional: working directory for the Claude agent
TIGERHARNESS_AGENT_CWD=.
"""

_MEMORY_CONFIG_TEMPLATE = """\
# tiger-memory configuration
# Docs: https://github.com/DingyuZhou/TigerHarness/blob/main/docs/tiger-memory.md

agent:
  name: {name}
  role: "A helpful assistant."

store:
  root: ~/.local/share/tiger-memory/{name_lower}

sources:
  - kind: claude_code
    project_path: ~/.claude/projects/-home-user-myproject/
    # ^ Replace with your actual Claude Code project path.
    #   Find it under ~/.claude/projects/ — the dirname encodes the
    #   absolute path with dashes replacing slashes.

summarizer:
  backend: anthropic
  model: claude-sonnet-4-6
  prompts: default/v1

rebuild:
  lock_path: /tmp/tiger-memory-{name_lower}.lock
  idle_threshold_hours: 2
  resummarize_window_days: 7
  rebuild_timeout_minutes: 60
"""


def _write_if_missing(path: Path, content: str) -> bool:
    """Write *content* to *path* if the file doesn't already exist.

    Returns True if the file was created, False if it already existed.
    """
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def init(
    *,
    name: str = "assistant",
    target_dir: Path | None = None,
    include_memory: bool = False,
) -> list[str]:
    """Scaffold config files into *target_dir* (default: cwd).

    Returns a list of created file paths (relative to target_dir).
    """
    root = target_dir or Path.cwd()
    created: list[str] = []

    # .gitignore — prevent committing secrets
    gitignore = root / ".gitignore"
    if _write_if_missing(gitignore, ".env\n"):
        created.append(str(gitignore.relative_to(root)))

    persona = root / "personas" / f"{name}.md"
    if _write_if_missing(persona, _PERSONA_TEMPLATE.format(name=name)):
        created.append(str(persona.relative_to(root)))

    env = root / ".env"
    if _write_if_missing(env, _ENV_TEMPLATE.format(name=name)):
        created.append(str(env.relative_to(root)))

    if include_memory:
        mem_cfg = root / "tiger-memory.config.yaml"
        if _write_if_missing(
            mem_cfg,
            _MEMORY_CONFIG_TEMPLATE.format(
                name=name, name_lower=name.lower()
            ),
        ):
            created.append(str(mem_cfg.relative_to(root)))

    return created


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for ``tigerharness init``."""
    parser = argparse.ArgumentParser(
        prog="tigerharness init",
        description="Scaffold a new tigerharness project.",
    )
    parser.add_argument(
        "--name",
        default="assistant",
        help="Persona name (default: assistant).",
    )
    parser.add_argument(
        "--memory",
        action="store_true",
        help="Also generate tiger-memory.config.yaml.",
    )
    parser.add_argument(
        "--dir",
        default=".",
        help="Target directory (default: current directory).",
    )
    args = parser.parse_args(argv)

    target = Path(args.dir).resolve()
    created = init(name=args.name, target_dir=target, include_memory=args.memory)

    if not created:
        print("Nothing to do -- all files already exist.")
        return 0

    print(f"Created {len(created)} file(s) in {target}:")
    for f in created:
        print(f"  {f}")
    print()
    print("Next steps:")
    print(f"  1. Edit personas/{args.name}.md with your agent's instructions")
    print("  2. Fill in .env with your Slack tokens")
    if args.memory:
        print("  3. Edit tiger-memory.config.yaml — set sources.project_path")
        print(f"  4. Run: tigerharness tiger-memory init --config tiger-memory.config.yaml")
    print()
    print("  Then:")
    print(f"  export TIGERHARNESS_PERSONAS_DIR=./personas")
    print(f"  tigerharness task-runner assign --to {args.name} --prompt '...' --iters 5")
    return 0

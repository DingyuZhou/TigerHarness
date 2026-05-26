"""Persona registry -- name -> `agent_sdk.AgentConfig` + working directory.

Configuration-driven: personas are defined via environment or a config
file, not hardcoded. Users register their own personas for their project.

Configuration sources (checked in order):

1. `TIGERHARNESS_PERSONAS_CONFIG` env var -> path to a YAML config file
   (see `load_personas_config` for schema). Loaded automatically at
   module import so every `python -m tigerharness.task_runner ...`
   invocation -- including the detached children the runner fork-execs
   -- sees the registry without extra boilerplate.
2. `TIGERHARNESS_PERSONAS_DIR` env var -> directory of `<name>.md` prompts
   (used implicitly by `load_prompt` when no `personas_dir` is given).
3. Programmatic registration via `register_persona()`.

Design picks
------------

1. **Built on `agent_sdk`, not raw subprocess.** Uses `AgentConfig` for
   clean invocation with permission_mode, disallowed_tools, etc.

2. **No default personas.** Unlike an internal predecessor which
   hardcoded named personas, this package ships empty. Users define
   their personas for their project.

3. **Prompt files are the source of truth.** Each persona reads its
   system prompt from a markdown file. Edit the file, not the code.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tigerharness.agent_sdk.types import AgentConfig

log = logging.getLogger("tigerharness.task_runner.personas")


# ---------------------------------------------------------------------------
# Filesystem anchors (configurable)
# ---------------------------------------------------------------------------

def _get_personas_dir() -> Path | None:
    """Return the personas directory from env, or None if not configured."""
    env = os.environ.get("TIGERHARNESS_PERSONAS_DIR", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return None


# ---------------------------------------------------------------------------
# Common tool denials
# ---------------------------------------------------------------------------

_SUDO_DENY: tuple[str, ...] = ("Bash(sudo:*)", "Bash(sudo)")
"""Hard-blocked for every persona. Root commands should be queued, not run."""


# ---------------------------------------------------------------------------
# Persona data
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Persona:
    """A persona name + its canonical config builder + the cwd to run in."""

    name: str
    aliases: tuple[str, ...]
    cwd: Path
    build_config: Callable[[], AgentConfig]
    description: str


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------

def load_prompt(role: str, personas_dir: Path | None = None) -> str:
    """Read `<role>.md` from the personas directory.

    Raises FileNotFoundError if the file doesn't exist.
    """
    directory = personas_dir or _get_personas_dir()
    if directory is None:
        raise FileNotFoundError(
            f"No personas directory configured. Set TIGERHARNESS_PERSONAS_DIR "
            f"or pass personas_dir explicitly. Looking for: {role}.md"
        )
    path = directory / f"{role}.md"
    if not path.exists():
        raise FileNotFoundError(
            f"Persona prompt missing for role={role!r}: expected {path}"
        )
    return path.read_text()


def build_persona_config(
    name: str,
    *,
    prompt: str | None = None,
    prompt_file: str | None = None,
    personas_dir: Path | None = None,
    permission_mode: str = "bypassPermissions",
    disallowed_tools: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> AgentConfig:
    """Build an AgentConfig for a persona.

    Supply either `prompt` (raw text) or `prompt_file` (name without .md
    extension, resolved from personas_dir).

    ``disallowed_tools`` extends the always-denied ``_SUDO_DENY`` list
    rather than replacing it -- sudo is a hard floor for every persona,
    not a default someone can accidentally opt out of. Duplicate entries
    (e.g. a caller who passes ``"Bash(sudo:*)"`` explicitly) are merged
    so the final list stays clean. Changed in 0.1.2; in 0.1.x the
    argument was *replace*, which made it easy to silently re-enable
    sudo by passing a non-sudo deny list.
    """
    if prompt is None:
        role = prompt_file or name
        prompt = load_prompt(role, personas_dir)

    # Sudo is a hard floor; callers' lists extend it. Order: sudo entries
    # first (so the structural deny is visible at the top), then caller's
    # entries in their original order, deduped.
    seen: set[str] = set()
    tools_deny: list[str] = []
    for tool in list(_SUDO_DENY) + list(disallowed_tools or ()):
        if tool not in seen:
            seen.add(tool)
            tools_deny.append(tool)
    cfg_extra: dict[str, Any] = {
        "permission_mode": permission_mode,
        "disallowed_tools": tools_deny,
    }
    if extra:
        cfg_extra.update(extra)

    return AgentConfig(
        name=f"{name}-task",
        instructions=prompt,
        extra=cfg_extra,
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Persona] = {}
_ALIAS_INDEX: dict[str, Persona] = {}


def register_persona(
    name: str,
    *,
    aliases: tuple[str, ...] = (),
    cwd: Path | str = ".",
    prompt: str | None = None,
    prompt_file: str | None = None,
    personas_dir: Path | None = None,
    permission_mode: str = "bypassPermissions",
    disallowed_tools: list[str] | None = None,
    extra: dict[str, Any] | None = None,
    description: str = "",
) -> Persona:
    """Register a persona in the global registry.

    Returns the Persona object. Can be called multiple times for the same
    name (last registration wins).
    """
    cwd_path = Path(cwd).expanduser().resolve()

    # Capture closure variables for lazy config building
    _prompt = prompt
    _prompt_file = prompt_file
    _personas_dir = personas_dir
    _permission_mode = permission_mode
    _disallowed_tools = disallowed_tools
    _extra = extra

    def _build() -> AgentConfig:
        return build_persona_config(
            name,
            prompt=_prompt,
            prompt_file=_prompt_file,
            personas_dir=_personas_dir,
            permission_mode=_permission_mode,
            disallowed_tools=_disallowed_tools,
            extra=_extra,
        )

    persona = Persona(
        name=name,
        aliases=aliases or (name,),
        cwd=cwd_path,
        build_config=_build,
        description=description,
    )

    _REGISTRY[name] = persona
    # Rebuild alias index
    _ALIAS_INDEX.clear()
    for p in _REGISTRY.values():
        for alias in p.aliases:
            _ALIAS_INDEX[alias.lower()] = p

    return persona


def resolve(name: str) -> Persona:
    """Look up a persona by name or alias. Case- and separator-insensitive."""
    key = name.strip().lower().replace(" ", "-").replace("_", "-")
    if key not in _ALIAS_INDEX:
        known = sorted(_REGISTRY.keys())
        raise KeyError(
            f"unknown persona {name!r}; known canonical names: {known}; "
            f"all aliases: {sorted(_ALIAS_INDEX)}"
        )
    return _ALIAS_INDEX[key]


def list_personas() -> list[Persona]:
    """Return all registered personas."""
    return list(_REGISTRY.values())


def clear_registry() -> None:
    """Clear all registered personas. Useful for testing."""
    _REGISTRY.clear()
    _ALIAS_INDEX.clear()


# ---------------------------------------------------------------------------
# YAML config loader
# ---------------------------------------------------------------------------

# Keys accepted under each persona entry. Anything else raises ValueError so
# typos surface immediately instead of silently doing the wrong thing.
_PERSONA_KEYS: frozenset[str] = frozenset({
    "name",
    "aliases",
    "cwd",
    "prompt",
    "prompt_file",
    "personas_dir",
    "permission_mode",
    "disallowed_tools",
    "extra",
    "description",
})


def _resolve_relative(path: str | Path, base: Path) -> Path:
    """Resolve `path` relative to `base` if it is not already absolute."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = (base / p).resolve()
    return p


def load_personas_config(config_path: str | Path) -> list[Persona]:
    """Load personas from a YAML file and register each one.

    Schema (top-level)::

        personas_dir: ./personas         # optional; default base for prompt_file lookups.
                                          # relative paths resolve against the config file's directory.

        personas:                         # required; list of persona specs.
          - name: sai
            aliases: [sai]                # optional; defaults to (name,).
            cwd: .                        # required-ish; defaults to "." (resolved against config dir).
            prompt_file: sai              # optional; defaults to name. Looked up in personas_dir.
            # OR:
            # prompt: "raw system prompt text"
            disallowed_tools:             # optional; merged with the sudo block in register_persona's default.
              - "Bash(sudo:*)"
            permission_mode: bypassPermissions  # optional; default shown.
            extra:                        # optional; passed through to AgentConfig.extra.
              add_dirs: [/extra/path]
            description: "..."            # optional; one-line summary shown in `task-runner personas`.

    All relative paths (`personas_dir`, `cwd`, `extra.add_dirs`) resolve
    relative to the **config file's directory**, not the cwd at load time,
    so configs are portable.

    Returns the list of registered Persona objects, in declaration order.

    Raises:
        FileNotFoundError: config file does not exist.
        ValueError: schema violations (missing `personas`, unknown keys,
            missing `name`, non-list `personas`).
        ImportError: pyyaml is not installed.
    """
    path = Path(config_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"personas config not found: {path}")

    try:
        import yaml
    except ImportError as e:  # pragma: no cover  (pyyaml ships with tigerharness[memory]; absence is an install bug)
        raise ImportError(
            "Loading TIGERHARNESS_PERSONAS_CONFIG requires pyyaml. "
            "Install with: pip install tigerharness[memory] (or pyyaml directly)."
        ) from e

    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top-level must be a mapping, got {type(raw).__name__}")

    base = path.parent
    default_personas_dir: Path | None = None
    if "personas_dir" in raw:
        default_personas_dir = _resolve_relative(raw["personas_dir"], base)

    personas_entry = raw.get("personas")
    if not isinstance(personas_entry, list):
        raise ValueError(f"{path}: 'personas' must be a list, got {type(personas_entry).__name__}")

    registered: list[Persona] = []
    for i, spec in enumerate(personas_entry):
        if not isinstance(spec, dict):
            raise ValueError(f"{path}: personas[{i}] must be a mapping, got {type(spec).__name__}")
        unknown = set(spec) - _PERSONA_KEYS
        if unknown:
            raise ValueError(
                f"{path}: personas[{i}] has unknown keys {sorted(unknown)}; "
                f"allowed: {sorted(_PERSONA_KEYS)}"
            )
        if "name" not in spec:
            raise ValueError(f"{path}: personas[{i}] missing required 'name'")

        kwargs: dict[str, Any] = {"name": spec["name"]}
        if "aliases" in spec:
            kwargs["aliases"] = tuple(spec["aliases"])
        kwargs["cwd"] = _resolve_relative(spec.get("cwd", "."), base)
        if "prompt" in spec:
            kwargs["prompt"] = spec["prompt"]
        if "prompt_file" in spec:
            kwargs["prompt_file"] = spec["prompt_file"]
        if "personas_dir" in spec:
            kwargs["personas_dir"] = _resolve_relative(spec["personas_dir"], base)
        elif default_personas_dir is not None:
            kwargs["personas_dir"] = default_personas_dir
        if "permission_mode" in spec:
            kwargs["permission_mode"] = spec["permission_mode"]
        if "disallowed_tools" in spec:
            kwargs["disallowed_tools"] = list(spec["disallowed_tools"])
        if "extra" in spec:
            extra = dict(spec["extra"])
            # Known path-list keys get resolved relative to the config dir,
            # symmetric with `cwd` / `personas_dir`. Other keys pass through
            # untouched — we don't know which strings are meant as paths.
            if "add_dirs" in extra and isinstance(extra["add_dirs"], list):
                extra["add_dirs"] = [
                    str(_resolve_relative(d, base)) for d in extra["add_dirs"]
                ]
            kwargs["extra"] = extra
        if "description" in spec:
            kwargs["description"] = spec["description"]

        registered.append(register_persona(**kwargs))

    return registered


def _autoload_from_env() -> None:
    """If `TIGERHARNESS_PERSONAS_CONFIG` is set, load it now.

    Called at module import. Errors during load are logged but do not
    raise, so a misconfigured env var cannot break unrelated Python
    invocations (e.g. `tiger-memory` CLI starting up).
    """
    config_env = os.environ.get("TIGERHARNESS_PERSONAS_CONFIG", "").strip()
    if not config_env:
        return  # pragma: no cover — runs at import time before coverage starts
    try:
        load_personas_config(config_env)
    except Exception as e:
        log.warning(
            "failed to autoload TIGERHARNESS_PERSONAS_CONFIG=%s: %s: %s",
            config_env, type(e).__name__, e,
        )


_autoload_from_env()

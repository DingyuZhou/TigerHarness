"""Model vendors: which agent CLI, and which model, a persona runs on.

A *vendor* is the human-facing name of an agentic CLI that the harness
can drive through the :mod:`tigerharness.agent_sdk` registry. Two ship
today:

===========  =================  ==========================================
vendor       agent_sdk backend  what it spawns
===========  =================  ==========================================
``claude``   ``claude_p``       ``claude -p`` (Claude Code, Anthropic)
``chatgpt``  ``codex_exec``     ``codex exec`` (Codex CLI, OpenAI)
===========  =================  ==========================================

Both bill the vendor's *subscription* through its own CLI, which is the
whole point of the subscription rail (``docs/subscription-backend.md``):
the harness never holds an API key for either.

Where the choice lives
----------------------

``configs/personas.yaml`` carries it, next to the roster it applies to::

    default_vendor: claude        # team default: claude | chatgpt
    default_model: ""             # team default model; blank = the CLI's own

    personas:
      - name: Rukawa
        vendor: chatgpt           # this persona's override (optional)
        model: gpt-6-astra        # this persona's override (optional)

Resolution, per persona (:func:`resolve_model_policy`):

1. ``vendor`` -- the persona's own value if set, else the team's
   ``default_vendor``, else ``claude``. The literal ``default`` (any
   case) and a blank value both mean "not set".
2. ``model`` -- the persona's own value if set. Otherwise the team's
   ``default_model`` **only when the persona runs on the team's vendor**;
   a persona that switched vendor and named no model gets that vendor
   CLI's own default, because a model id belongs to one vendor and the
   team's default may name the other's.

Every consumer resolves through this module (the Slack bridge per
persona, autodrive for its driver persona, idle compaction to know
which sessions speak ``/compact``), so the mapping is defined once.

The reader is tolerant in the same way the journal's roster readers are
(``journal.scaffold``): a missing file or a stripped install without
pyyaml yields the built-in default with a logged warning rather than a
crash -- a *malformed* vendor value, however, raises ``ValueError``, so
a typo like ``vendor: chatgtp`` is a loud startup failure, not a silent
fallback to the other vendor's bill.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("tigerharness.vendors")

#: vendor name -> agent_sdk backend name.
VENDOR_BACKENDS: dict[str, str] = {
    "claude": "claude_p",
    "chatgpt": "codex_exec",
}

#: Accepted spellings, folded to the canonical vendor name. Backend names
#: are accepted too so ``--backend codex_exec`` and ``vendor: codex_exec``
#: mean the same thing as ``chatgpt``.
_VENDOR_ALIASES: dict[str, str] = {
    "claude": "claude",
    "anthropic": "claude",
    "claude-code": "claude",
    "claude_code": "claude",
    "claude_p": "claude",
    "chatgpt": "chatgpt",
    "openai": "chatgpt",
    "codex": "chatgpt",
    "codex_exec": "chatgpt",
    "codex-exec": "chatgpt",
}

DEFAULT_VENDOR = "claude"
CLAUDE_BACKEND = VENDOR_BACKENDS["claude"]
CODEX_BACKEND = VENDOR_BACKENDS["chatgpt"]

#: Values that mean "not set, inherit" wherever a vendor or model is read.
_INHERIT = frozenset({"", "default", "inherit", "team"})

#: The vendor CLI executable per vendor (for install hints / PATH checks).
VENDOR_CLIS: dict[str, str] = {
    "claude": "claude",
    "chatgpt": "codex",
}


@dataclass(frozen=True)
class ModelPolicy:
    """The resolved answer to "what does this persona run on?".

    ``source`` says which layer decided the vendor -- ``"persona"``,
    ``"team"``, or ``"builtin"`` -- so a log line can show the Operator
    *why* a session came up on a given vendor.
    """

    vendor: str
    backend: str
    model: str | None
    source: str = "builtin"

    @property
    def cli(self) -> str:
        return VENDOR_CLIS[self.vendor]


def _is_unset(raw: object) -> bool:
    return raw is None or (isinstance(raw, str) and raw.strip().lower() in _INHERIT)


def normalize_vendor(raw: object, *, where: str = "vendor") -> str | None:
    """Fold *raw* to a canonical vendor name, or ``None`` when it means
    "inherit". Raises ``ValueError`` on an unknown spelling."""
    if _is_unset(raw):
        return None
    if not isinstance(raw, str):
        raise ValueError(
            f"{where}: expected a vendor name string, got {type(raw).__name__}"
        )
    key = raw.strip().lower()
    vendor = _VENDOR_ALIASES.get(key)
    if vendor is None:
        raise ValueError(
            f"{where}: unknown model vendor {raw!r}. "
            f"Known vendors: {', '.join(sorted(VENDOR_BACKENDS))} "
            f"(aliases: {', '.join(sorted(_VENDOR_ALIASES))})."
        )
    return vendor


def normalize_model(raw: object, *, where: str = "model") -> str | None:
    """A model id string, or ``None`` when *raw* means "inherit"."""
    if _is_unset(raw):
        return None
    if not isinstance(raw, str):
        raise ValueError(
            f"{where}: expected a model id string, got {type(raw).__name__}"
        )
    return raw.strip()


def backend_for_vendor(vendor: str) -> str:
    """agent_sdk backend name for a (canonical or aliased) vendor name."""
    canon = normalize_vendor(vendor)
    if canon is None:
        canon = DEFAULT_VENDOR
    return VENDOR_BACKENDS[canon]


def vendor_for_backend(backend: str) -> str | None:
    """Inverse lookup; ``None`` for a backend no vendor maps to (e.g. a
    custom registration)."""
    for vendor, name in VENDOR_BACKENDS.items():
        if name == backend:
            return vendor
    return None


def is_vendor_name(raw: object) -> bool:
    """True iff *raw* spells a known vendor (any accepted alias)."""
    return isinstance(raw, str) and raw.strip().lower() in _VENDOR_ALIASES


# ---------------------------------------------------------------------------
# personas.yaml reading
# ---------------------------------------------------------------------------

def personas_yaml_path(team_root: Path) -> Path:
    return Path(team_root) / "configs" / "personas.yaml"


def read_personas_yaml(team_root: Path | None) -> dict[str, Any] | None:
    """Parse ``<team_root>/configs/personas.yaml`` into a dict.

    Returns ``None`` when there is no team root, no file, no pyyaml, or
    the file is not a mapping -- every one of those is logged (the
    pyyaml case as a warning, since it silently costs the team its
    configured vendor) and resolves to the built-in default upstream.
    A file that is present but *malformed YAML* raises ``ValueError``
    naming the path, because a broken roster file is a real configuration
    error, not an absent one.
    """
    if team_root is None:
        return None
    path = personas_yaml_path(team_root)
    if not path.is_file():
        return None
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        log.warning(
            "pyyaml is not installed; %s cannot be read, so every persona "
            "resolves to the built-in vendor %r. Install with "
            "`pip install 'tigerharness[memory]'`.",
            path, DEFAULT_VENDOR,
        )
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: not valid YAML: {exc}") from exc
    if data is None:
        return None
    if not isinstance(data, dict):
        log.warning("%s is not a mapping; ignoring it for vendor resolution", path)
        return None
    return data


def _find_persona_entry(data: dict[str, Any], persona: str) -> dict[str, Any] | None:
    """The roster entry for *persona*: exact name, then case-insensitive
    name, then any alias (same order the bridge's router accepts)."""
    raw = data.get("personas")
    if not isinstance(raw, list):
        return None
    entries = [e for e in raw if isinstance(e, dict)]
    for entry in entries:
        if entry.get("name") == persona:
            return entry
    folded = persona.strip().casefold()
    for entry in entries:
        name = entry.get("name")
        if isinstance(name, str) and name.strip().casefold() == folded:
            return entry
    for entry in entries:
        aliases = entry.get("aliases")
        if isinstance(aliases, list) and any(
            isinstance(a, str) and a.strip().casefold() == folded for a in aliases
        ):
            return entry
    return None


def team_default_policy(
    team_root: Path | None, *, data: dict[str, Any] | None = None
) -> ModelPolicy:
    """The team-level default (top-level ``default_vendor`` /
    ``default_model``), or the built-in default when unset."""
    if data is None:
        data = read_personas_yaml(team_root)
    if not data:
        return ModelPolicy(
            vendor=DEFAULT_VENDOR,
            backend=VENDOR_BACKENDS[DEFAULT_VENDOR],
            model=None,
            source="builtin",
        )
    where = f"{personas_yaml_path(team_root) if team_root else 'personas.yaml'}: default_vendor"
    vendor = normalize_vendor(data.get("default_vendor"), where=where)
    model = normalize_model(
        data.get("default_model"), where=where.replace("default_vendor", "default_model")
    )
    if vendor is None:
        # No team vendor set: a bare default_model would be a model id
        # with no vendor to belong to. Bind it to the built-in vendor.
        return ModelPolicy(
            vendor=DEFAULT_VENDOR,
            backend=VENDOR_BACKENDS[DEFAULT_VENDOR],
            model=model,
            source="builtin",
        )
    return ModelPolicy(
        vendor=vendor, backend=VENDOR_BACKENDS[vendor], model=model, source="team"
    )


def resolve_model_policy(
    team_root: Path | None,
    persona: str | None,
    *,
    data: dict[str, Any] | None = None,
) -> ModelPolicy:
    """Resolve the vendor + model *persona* runs on (module docstring
    rules). ``data`` lets a caller that already parsed personas.yaml
    (the bridge loader, an idle-compaction pass over many threads)
    avoid re-reading it per persona.

    A ``None`` persona, or one absent from the roster, gets the team
    default -- the same answer the no-identity autodrive fallback and a
    pre-routing Slack thread would get.
    """
    if data is None:
        data = read_personas_yaml(team_root)
    team = team_default_policy(team_root, data=data)
    if not data or not persona:
        return team
    entry = _find_persona_entry(data, persona)
    if entry is None:
        return team
    where = (
        f"{personas_yaml_path(team_root) if team_root else 'personas.yaml'}: "
        f"persona {persona!r}"
    )
    own_vendor = normalize_vendor(entry.get("vendor"), where=f"{where} vendor")
    own_model = normalize_model(entry.get("model"), where=f"{where} model")
    if own_vendor is None:
        # Inherits the team vendor; may still pin its own model on it.
        return ModelPolicy(
            vendor=team.vendor,
            backend=team.backend,
            model=own_model if own_model is not None else team.model,
            source="persona" if own_model is not None else team.source,
        )
    if own_model is None and own_vendor == team.vendor:
        model = team.model
    else:
        model = own_model
    return ModelPolicy(
        vendor=own_vendor,
        backend=VENDOR_BACKENDS[own_vendor],
        model=model,
        source="persona",
    )


def describe(policy: ModelPolicy) -> str:
    """One short token for logs: ``chatgpt/gpt-6-astra`` or ``claude``."""
    return f"{policy.vendor}/{policy.model}" if policy.model else policy.vendor

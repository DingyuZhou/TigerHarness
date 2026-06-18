"""Config loading + validation for tiger-memory.

The config is a YAML file at ``$TIGER_MEMORY_CONFIG`` (or supplied
via ``--config``). Loader validates required fields, enforces minimums
(per design doc §3.2), and expands ``~`` in paths.

Validation is fail-fast: any structural problem raises ``ConfigError``
with a clear message, so a bad config is caught at ``tiger-memory init``
rather than mid-rebuild.
"""
from __future__ import annotations

import logging

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("tigerharness.tiger_memory.config")


class ConfigError(ValueError):
    """Raised when the config file is missing, malformed, or violates
    a minimum-value rule (e.g., dailies_working_days < 7)."""


# ----- typed config sections -----------------------------------------------


@dataclass(frozen=True)
class AgentConfig:
    name: str
    role: str
    pronouns: str = ""


@dataclass(frozen=True)
class StoreConfig:
    root: Path  # expanded + absolute


@dataclass(frozen=True)
class SourceConfig:
    kind: str  # "claude_code" | "slack_thread" | "docs"
    fields: dict[str, Any]  # kind-specific opaque payload


@dataclass(frozen=True)
class SummarizerConfig:
    backend: str
    model: str
    prompts: str  # path under summarizers/prompts/ (e.g., "default/v1")
    retry_max_attempts: int = 3
    retry_backoff: str = "exponential"


@dataclass(frozen=True)
class BudgetsConfig:
    # Max characters of transcript content fed to a single in-process
    # extraction summarizer prompt. ~120 KB ≈ ~30 K tokens, well under
    # the context window with headroom for the prompt+output. A transcript
    # larger than this is clipped (head+tail) before the in-process call.
    max_prompt_content_chars: int = 120_000
    # Max characters of transcript content staged into a sub-agent prompt
    # file (the subscription-billed in-session path, which has no
    # in-process summarizer to chunk-and-reduce with). The sub-agent owns
    # the reduce, so this ceiling is sized to its context window (~300 KB
    # ≈ ~75 K tokens) rather than the smaller per-API-call budget above.
    # Only a transcript exceeding *this* is clipped, as a last resort.
    max_staged_content_chars: int = 300_000
    # Sweep stacking (subscription-billed in-session path). The plan groups
    # staged transcripts into "stacks" -- one per summarize sub-agent -- so a
    # backlog fan-out amortizes per-sub-agent setup WITHOUT any single context
    # accumulating every transcript (the worse-than-linear cost of one looping
    # agent re-reading all prior transcripts each turn). A stack closes when
    # adding the next transcript would push summed (clipped) content over
    # ``sweep_stack_content_chars``, or once it already holds
    # ``sweep_stack_max_items`` transcripts. A single transcript heavier than
    # the char budget becomes its own solo stack (never split). See
    # ``docs/tiger-memory-sweep-protocol.md``.
    sweep_stack_content_chars: int = 200_000
    sweep_stack_max_items: int = 8


@dataclass(frozen=True)
class MemoryExtractConfig:
    """Word budgets for the extraction prompt's three store sections.

    The extraction prompt (``summarizers/prompts/.../extract_memory.md``)
    fills these as per-section length hints. ``max_output_words`` caps the
    whole bundle the summarizer may return.
    """

    skill_procedure_words: int = 120
    memo_words: int = 25
    reaction_words: int = 40
    max_output_words: int = 600


@dataclass(frozen=True)
class RebuildConfig:
    trigger: str = "lazy"
    idle_threshold_hours: float = 1.0
    lock_path: Path = Path("/tmp/tiger-memory.lock")
    # Maximum wall-clock minutes a rebuild may hold the lock before
    # the NEXT trigger reclaims it. Default 60 — generous for a real
    # bootstrap (~30 min for 95 sessions). The rebuild touches its
    # own lockfile every minute, so a healthy long rebuild stays alive
    # via mtime refresh; only a *hung* process gets reclaimed.
    rebuild_timeout_minutes: int = 60


@dataclass(frozen=True)
class BriefingConfig:
    """Session-start briefing assembly (bounded-store revamp, design §6).

    ``emotional_top`` caps how many emotional entries the session-start
    view shows (top-by-|weight|); ``0`` shows all.
    """

    emotional_top: int = 20


@dataclass(frozen=True)
class PrefilterConfig:
    """Transcript pre-filter knobs (P1.1 / Lever 1.2).

    Conservative-on by default: every flag is an independent off-switch
    so aggressiveness is tunable per persona. See ``prefilter.py``.
    """

    enabled: bool = True
    drop_tool_results: bool = True
    drop_system_reminders: bool = True


@dataclass(frozen=True)
class CapConfig:
    """Per-wake cap knobs (retained for forward-compat; tuning hints).

    Sensible upper bounds a caller may consult when batching extraction work
    across sweep wakes. Parsed for config stability; the per-wake transcript
    cap itself is enforced by ``plan_extraction(max_sessions=...)``.
    """

    max_sessions_per_rebuild: int = 10
    max_usd_per_rebuild: float = 20.0


@dataclass(frozen=True)
class CollapseConfig:
    """Retained config stub (forward-compat).

    The collapsed-summary pass is retired with the rollup lifecycle; this
    stays parseable so an older config carrying ``collapse:`` still loads.
    """

    enabled: bool = False


# ----- memory revamp config (design §7) ------------------------------------
#
# The three bounded stores (design §4). Each store carries a two-number
# bound (``max`` + ``overflow_limit``) giving hysteresis: a store may drift
# up to ``overflow_limit``, and only then does meditation fire and compact
# it back below ``max`` (design §4 — prevents meditate-every-session thrash).
# Length is measured in CHARACTERS, never tokens (vendor-neutral, design §8).

# The only length unit we accept. Token units are vendor-specific and are
# rejected at load time (design §7 / §8).
VALID_LENGTH_UNIT = "characters"


@dataclass(frozen=True)
class SkillsStoreConfig:
    """Bound for the skills store — count-based (design §4.1)."""

    max_count: int = 40
    overflow_limit: int = 50


@dataclass(frozen=True)
class MustRememberStoreConfig:
    """Bound for the must-remember store — length-based (design §4.2)."""

    max_length: int = 8000
    overflow_limit: int = 10000


@dataclass(frozen=True)
class EmotionalDecayConfig:
    """Signed-weight decay rate for the emotional log (design §4.3)."""

    magnitude_per_day: float = 0.1


@dataclass(frozen=True)
class EmotionalStoreConfig:
    """Bound + signed-weight cap + decay for the emotional log (design §4.3)."""

    max_length: int = 12000
    overflow_limit: int = 15000
    weight_cap: float = 10.0
    decay: EmotionalDecayConfig = field(default_factory=EmotionalDecayConfig)


@dataclass(frozen=True)
class MemoryConfig:
    """The ``memory:`` block (design §7).

    ``length_unit`` is CONFIRMED final: ``characters``, never tokens.
    The store bounds and decay rate are sensible defaults the Operator
    approved tuning later (design §10.2).
    """

    length_unit: str = VALID_LENGTH_UNIT
    skills: SkillsStoreConfig = field(default_factory=SkillsStoreConfig)
    must_remember: MustRememberStoreConfig = field(
        default_factory=MustRememberStoreConfig
    )
    emotional_log: EmotionalStoreConfig = field(
        default_factory=EmotionalStoreConfig
    )


@dataclass(frozen=True)
class Config:
    agent: AgentConfig
    store: StoreConfig
    sources: list[SourceConfig]
    summarizer: SummarizerConfig
    budgets: BudgetsConfig
    rebuild: RebuildConfig
    briefing: BriefingConfig
    prefilter: PrefilterConfig = field(default_factory=PrefilterConfig)
    cap: CapConfig = field(default_factory=CapConfig)
    collapse: CollapseConfig = field(default_factory=CollapseConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    memory_extract: MemoryExtractConfig = field(
        default_factory=MemoryExtractConfig
    )
    env_var: str = "TIGER_MEMORY_CONFIG"
    # Resolved at load time so tests can introspect.
    source_path: Path | None = None


# ----- loader --------------------------------------------------------------


def load_config(path: str | Path | None = None) -> Config:
    """Load config from *path* (or ``$TIGER_MEMORY_CONFIG`` if None).

    When a team-level defaults file is found (auto-discovered or
    explicitly referenced via the ``defaults:`` key), its values are
    deep-merged *under* the per-persona config: persona wins on any
    key it sets, defaults fill in the rest.

    Raises ``ConfigError`` on missing file, YAML parse error, or any
    schema/minimum violation.
    """
    if path is None:
        env = os.environ.get("TIGER_MEMORY_CONFIG")
        if not env:
            raise ConfigError(
                "No config path given and TIGER_MEMORY_CONFIG is not set."
            )
        path = env
    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not parse YAML at {path}: {exc}") from exc

    if isinstance(raw, dict):
        defaults_raw = _load_defaults(raw, path)
        if defaults_raw:
            raw = _deep_merge(defaults_raw, raw)

    return _from_dict(raw, source_path=path)


# ----- team-level defaults discovery + merge ---------------------------------


def _load_defaults(raw: dict[str, Any], config_path: Path) -> dict[str, Any]:
    """Locate and load the team-level defaults file, if any.

    Discovery order:
      1. Explicit ``defaults:`` key in *raw* (path resolved relative to
         the persona config's directory).
      2. Auto-discovery: ``../../configs/tiger-memory.defaults.yaml``
         relative to the persona config (matches the standard team
         layout ``memories/<persona>/tiger-memory.config.yaml``).

    Returns an empty dict when no defaults file is found.
    """
    anchor = config_path.parent

    explicit = raw.pop("defaults", None)
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_absolute():
            p = (anchor / p).resolve()
        if not p.exists():
            raise ConfigError(
                f"Explicit defaults file not found: {p} "
                f"(referenced from {config_path})"
            )
        try:
            return yaml.safe_load(p.read_text()) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(
                f"Could not parse defaults YAML at {p}: {exc}"
            ) from exc

    # Auto-discover: <team>/configs/tiger-memory.defaults.yaml
    auto = (anchor / ".." / "configs" / "tiger-memory.defaults.yaml").resolve()
    if not auto.exists():
        # Also try the alternative: the persona config IS inside
        # memories/<persona>/ which is two levels below the team root.
        auto = (anchor / ".." / ".." / "configs" / "tiger-memory.defaults.yaml").resolve()
    if auto.exists():
        try:
            return yaml.safe_load(auto.read_text()) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(
                f"Could not parse defaults YAML at {auto}: {exc}"
            ) from exc

    return {}


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *overlay* on top of *base*. Returns a new dict.

    - Dict values are merged recursively (overlay keys win).
    - All other types (str, list, int, ...) are replaced wholesale by
      the overlay value when present.
    """
    merged = dict(base)
    for key, val in overlay.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(val, dict)
        ):
            merged[key] = _deep_merge(merged[key], val)
        else:
            merged[key] = val
    return merged


def _from_dict(raw: dict[str, Any], source_path: Path | None = None) -> Config:
    agent_raw = _require(raw, "agent", dict)
    agent = AgentConfig(
        name=_require(agent_raw, "name", str),
        role=_require(agent_raw, "role", str),
        pronouns=agent_raw.get("pronouns", "") or "",
    )

    store_raw = _require(raw, "store", dict)
    store_root_raw = _require(store_raw, "root", str)
    store_root = Path(store_root_raw).expanduser()
    # Resolve relative paths against the config file's directory (so
    # `./memory/` next to the config does the right thing).
    if not store_root.is_absolute():
        anchor = source_path.parent if source_path else Path.cwd()
        store_root = (anchor / store_root).resolve()
    # Auto-append the agent slug so multiple agents in the same repo
    # don't collide (memory/sai/, memory/scout/, ...). Case-insensitive
    # match so `./memory/SAI` with agent Sai doesn't double-append.
    agent_slug = _slugify(agent.name)
    if store_root.name.lower() != agent_slug:
        store_root = store_root / agent_slug
    store = StoreConfig(root=store_root)

    sources_raw = _require(raw, "sources", list)
    if not sources_raw:
        raise ConfigError("config.sources must contain at least one source.")
    sources = []
    for s in sources_raw:
        if not isinstance(s, dict) or "kind" not in s:
            raise ConfigError(f"Invalid source entry: {s!r}")
        kind = s["kind"]
        if kind not in {
            "claude_code", "slack_thread", "docs", "auto_memory",
            "journal_worklog",
        }:
            raise ConfigError(
                f"Unknown source kind: {kind!r}. "
                "Allowed: claude_code, slack_thread, docs, auto_memory, "
                "journal_worklog."
            )
        fields = {k: _expand_path_if_pathy(v) for k, v in s.items() if k != "kind"}
        sources.append(SourceConfig(kind=kind, fields=fields))

    summ_raw = _require(raw, "summarizer", dict)
    retry_raw = summ_raw.get("retry") or {}
    summarizer = SummarizerConfig(
        backend=_require(summ_raw, "backend", str),
        model=_require(summ_raw, "model", str),
        prompts=_require(summ_raw, "prompts", str),
        retry_max_attempts=int(retry_raw.get("max_attempts", 3)),
        retry_backoff=str(retry_raw.get("backoff", "exponential")),
    )

    budgets_raw = raw.get("budgets") or {}
    budgets = BudgetsConfig(
        max_prompt_content_chars=int(
            budgets_raw.get("max_prompt_content_chars", 120_000)
        ),
        max_staged_content_chars=int(
            budgets_raw.get("max_staged_content_chars", 300_000)
        ),
        sweep_stack_content_chars=int(
            budgets_raw.get("sweep_stack_content_chars", 200_000)
        ),
        sweep_stack_max_items=int(
            budgets_raw.get("sweep_stack_max_items", 8)
        ),
    )

    extract_raw = raw.get("memory_extract") or {}
    memory_extract = MemoryExtractConfig(
        skill_procedure_words=int(extract_raw.get("skill_procedure_words", 120)),
        memo_words=int(extract_raw.get("memo_words", 25)),
        reaction_words=int(extract_raw.get("reaction_words", 40)),
        max_output_words=int(extract_raw.get("max_output_words", 600)),
    )

    rebuild_raw = raw.get("rebuild") or {}
    rebuild = RebuildConfig(
        trigger=str(rebuild_raw.get("trigger", "lazy")),
        idle_threshold_hours=float(rebuild_raw.get("idle_threshold_hours", 1.0)),
        lock_path=Path(
            rebuild_raw.get("lock_path", "/tmp/tiger-memory.lock")
        ).expanduser(),
        rebuild_timeout_minutes=int(
            rebuild_raw.get("rebuild_timeout_minutes", 60)
        ),
    )

    briefing_raw = raw.get("briefing") or {}
    emotional_top = int(briefing_raw.get("emotional_top", 20))
    if emotional_top < 0:
        raise ConfigError(
            f"briefing.emotional_top must be ≥ 0; got {emotional_top}."
        )
    briefing = BriefingConfig(emotional_top=emotional_top)

    prefilter_raw = raw.get("prefilter") or {}
    prefilter = PrefilterConfig(
        enabled=bool(prefilter_raw.get("enabled", True)),
        drop_tool_results=bool(prefilter_raw.get("drop_tool_results", True)),
        drop_system_reminders=bool(
            prefilter_raw.get("drop_system_reminders", True)
        ),
    )

    cap_raw = raw.get("cap") or {}
    cap = CapConfig(
        max_sessions_per_rebuild=int(
            cap_raw.get("max_sessions_per_rebuild", 10)
        ),
        max_usd_per_rebuild=float(cap_raw.get("max_usd_per_rebuild", 20.0)),
    )

    collapse_raw = raw.get("collapse") or {}
    collapse = CollapseConfig(
        enabled=bool(collapse_raw.get("enabled", False)),
    )

    memory = _parse_memory(raw.get("memory") or {})

    return Config(
        agent=agent,
        store=store,
        sources=sources,
        summarizer=summarizer,
        budgets=budgets,
        rebuild=rebuild,
        briefing=briefing,
        prefilter=prefilter,
        cap=cap,
        collapse=collapse,
        memory=memory,
        memory_extract=memory_extract,
        env_var=str(raw.get("env_var", "TIGER_MEMORY_CONFIG")),
        source_path=source_path,
    )


# ----- memory revamp parsing + validation (design §7) ---------------------


def _cfg_int(value: Any, field: str) -> int:
    """``int(value)`` but a non-numeric value raises the contracted ``ConfigError``.

    The ``memory:`` block comes from a user-edited YAML file; a non-numeric
    bound (``max_count: lots``) must fail fast with an actionable ConfigError,
    not leak a raw ``ValueError`` (QI-4).
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{field} must be an integer; got {value!r}.") from None


def _cfg_float(value: Any, field: str) -> float:
    """``float(value)`` but a non-numeric value raises the contracted ``ConfigError``."""
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{field} must be a number; got {value!r}.") from None


def _parse_memory(memory_raw: dict[str, Any]) -> MemoryConfig:
    """Parse + validate the ``memory:`` block (design §7).

    All keys are optional (sensible defaults, design §10.2) EXCEPT the
    vendor-neutrality invariants: ``length_unit`` must be ``characters``
    (token units are rejected, design §8), bounds must be positive with
    ``overflow_limit > max`` (the hysteresis band, design §4), and the
    emotional ``weight_cap`` / decay rate must be positive.
    """
    length_unit = str(memory_raw.get("length_unit", VALID_LENGTH_UNIT))
    if length_unit != VALID_LENGTH_UNIT:
        raise ConfigError(
            f"memory.length_unit must be {VALID_LENGTH_UNIT!r} "
            f"(token units are vendor-specific and rejected); "
            f"got {length_unit!r}."
        )

    skills_raw = memory_raw.get("skills") or {}
    skills = SkillsStoreConfig(
        max_count=_cfg_int(skills_raw.get("max_count", 40), "memory.skills.max_count"),
        overflow_limit=_cfg_int(
            skills_raw.get("overflow_limit", 50), "memory.skills.overflow_limit"
        ),
    )
    _validate_bound(
        "memory.skills", "max_count", skills.max_count, skills.overflow_limit
    )

    mr_raw = memory_raw.get("must_remember") or {}
    must_remember = MustRememberStoreConfig(
        max_length=_cfg_int(
            mr_raw.get("max_length", 8000), "memory.must_remember.max_length"
        ),
        overflow_limit=_cfg_int(
            mr_raw.get("overflow_limit", 10000), "memory.must_remember.overflow_limit"
        ),
    )
    _validate_bound(
        "memory.must_remember",
        "max_length",
        must_remember.max_length,
        must_remember.overflow_limit,
    )

    el_raw = memory_raw.get("emotional_log") or {}
    decay_raw = el_raw.get("decay") or {}
    emotional_log = EmotionalStoreConfig(
        max_length=_cfg_int(
            el_raw.get("max_length", 12000), "memory.emotional_log.max_length"
        ),
        overflow_limit=_cfg_int(
            el_raw.get("overflow_limit", 15000), "memory.emotional_log.overflow_limit"
        ),
        weight_cap=_cfg_float(
            el_raw.get("weight_cap", 10.0), "memory.emotional_log.weight_cap"
        ),
        decay=EmotionalDecayConfig(
            magnitude_per_day=_cfg_float(
                decay_raw.get("magnitude_per_day", 0.1),
                "memory.emotional_log.decay.magnitude_per_day",
            ),
        ),
    )
    _validate_bound(
        "memory.emotional_log",
        "max_length",
        emotional_log.max_length,
        emotional_log.overflow_limit,
    )
    if emotional_log.weight_cap <= 0:
        raise ConfigError(
            f"memory.emotional_log.weight_cap must be > 0; "
            f"got {emotional_log.weight_cap}."
        )
    if emotional_log.decay.magnitude_per_day < 0:
        raise ConfigError(
            f"memory.emotional_log.decay.magnitude_per_day must be ≥ 0; "
            f"got {emotional_log.decay.magnitude_per_day}."
        )

    return MemoryConfig(
        length_unit=length_unit,
        skills=skills,
        must_remember=must_remember,
        emotional_log=emotional_log,
    )


def _validate_bound(
    label: str, max_key: str, max_value: int, overflow_limit: int
) -> None:
    """A store bound is valid iff ``0 < max < overflow_limit`` (design §4).

    The two-number hysteresis band only exists when overflow sits strictly
    above max; an inverted or collapsed band would make meditation thrash
    (fire while still under max) or never compact.
    """
    if max_value <= 0:
        raise ConfigError(f"{label}.{max_key} must be > 0; got {max_value}.")
    if overflow_limit <= max_value:
        raise ConfigError(
            f"{label}.overflow_limit must be > {max_key} ({max_value}) "
            f"to form a hysteresis band; got {overflow_limit}."
        )


# ----- helpers -------------------------------------------------------------


def _require(d: dict[str, Any], key: str, typ: type) -> Any:
    if key not in d:
        raise ConfigError(f"config.{key} is required.")
    val = d[key]
    if typ is list:
        if not isinstance(val, list):
            raise ConfigError(f"config.{key} must be a list.")
    elif typ is dict:
        if not isinstance(val, dict):
            raise ConfigError(f"config.{key} must be a mapping.")
    elif typ is str:
        if not isinstance(val, str) or not val:
            raise ConfigError(f"config.{key} must be a non-empty string.")
    return val


def _slugify(name: str) -> str:
    """Lowercase, replace any non-alphanumeric run with a single underscore.

    Strips leading/trailing underscores. Empty result → "agent".
    """
    import re
    s = re.sub(r"[^a-zA-Z0-9]+", "_", name.strip().lower()).strip("_")
    return s or "agent"


def _expand_path_if_pathy(value: Any) -> Any:
    """Expand ``~`` for any string that looks like a path."""
    if isinstance(value, str) and (value.startswith("~") or "/" in value):
        return str(Path(value).expanduser())
    return value

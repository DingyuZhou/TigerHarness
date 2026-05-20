"""Config loading + validation for tiger-memory.

The config is a YAML file at ``$TIGER_MEMORY_CONFIG`` (or supplied
via ``--config``). Loader validates required fields, enforces minimums
(per design doc §3.2), and expands ``~`` in paths.

Validation is fail-fast: any structural problem raises ``ConfigError``
with a clear message, so a bad config is caught at ``tiger-memory init``
rather than mid-rebuild.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when the config file is missing, malformed, or violates
    a minimum-value rule (e.g., dailies_working_days < 7)."""


# ----- minimums per design doc §3.2 ----------------------------------------

MIN_DAILIES_WORKING_DAYS = 7
MIN_WEEKLIES_WORKING_DAYS = 28
MIN_FULL_SHORTS_WORKING_DAYS = 1


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
    short_summary_words: int = 400
    detailed_summary_words: int = 4000
    daily_words: int = 600
    weekly_words: int = 1000
    monthly_words: int = 1500
    longer_memory_words: int = 2500
    must_memorize_rows: int = 60
    must_memorize_memo_words: int = 25
    repeat_detection_similarity: float = 0.7
    # Max characters of transcript content fed to a single prompt before
    # head/tail elision kicks in. ~120 KB ≈ ~30 K tokens, well under
    # Opus 4.7's context window with headroom for the prompt+output.
    max_prompt_content_chars: int = 120_000


@dataclass(frozen=True)
class DecayConfig:
    preference_days_per_point: int = 7
    decision_days_per_point: int = 14
    incident_days_per_point: int = 28
    # owner_explicit is always locked — not configurable.


@dataclass(frozen=True)
class RebuildConfig:
    trigger: str = "lazy"
    idle_threshold_hours: float = 1.0
    resummarize_window_days: int = 7
    lock_path: Path = Path("/tmp/tiger-memory.lock")
    # Maximum wall-clock minutes a rebuild may hold the lock before
    # the NEXT trigger reclaims it. Default 60 — generous for a real
    # bootstrap (~30 min for 95 sessions). The rebuild touches its
    # own lockfile every minute, so a healthy long rebuild stays alive
    # via mtime refresh; only a *hung* process gets reclaimed.
    rebuild_timeout_minutes: int = 60


@dataclass(frozen=True)
class WalkingConfig:
    full_shorts_working_days: int = 2
    dailies_working_days: int = 7
    weeklies_working_days: int = 28
    monthlies_working_days: int = 90


@dataclass(frozen=True)
class BriefingConfig:
    walking: WalkingConfig = field(default_factory=WalkingConfig)
    always_first: str = "must_memorize.md"
    order: str = "oldest_to_newest"


@dataclass(frozen=True)
class Config:
    agent: AgentConfig
    store: StoreConfig
    sources: list[SourceConfig]
    summarizer: SummarizerConfig
    budgets: BudgetsConfig
    decay: DecayConfig
    rebuild: RebuildConfig
    briefing: BriefingConfig
    env_var: str = "TIGER_MEMORY_CONFIG"
    # Resolved at load time so tests can introspect.
    source_path: Path | None = None


# ----- loader --------------------------------------------------------------


def load_config(path: str | Path | None = None) -> Config:
    """Load config from *path* (or ``$TIGER_MEMORY_CONFIG`` if None).

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

    return _from_dict(raw, source_path=path)


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
        if kind not in {"claude_code", "slack_thread", "docs", "auto_memory"}:
            raise ConfigError(
                f"Unknown source kind: {kind!r}. "
                "Allowed: claude_code, slack_thread, docs, auto_memory."
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
        short_summary_words=int(budgets_raw.get("short_summary_words", 400)),
        detailed_summary_words=int(budgets_raw.get("detailed_summary_words", 4000)),
        daily_words=int(budgets_raw.get("daily_words", 600)),
        weekly_words=int(budgets_raw.get("weekly_words", 1000)),
        monthly_words=int(budgets_raw.get("monthly_words", 1500)),
        longer_memory_words=int(budgets_raw.get("longer_memory_words", 2500)),
        must_memorize_rows=int(budgets_raw.get("must_memorize_rows", 60)),
        must_memorize_memo_words=int(budgets_raw.get("must_memorize_memo_words", 25)),
        repeat_detection_similarity=float(
            budgets_raw.get("repeat_detection_similarity", 0.7)
        ),
    )

    decay_raw = raw.get("decay") or {}
    decay = DecayConfig(
        preference_days_per_point=int(
            (decay_raw.get("preference") or {}).get("days_per_point", 7)
        ),
        decision_days_per_point=int(
            (decay_raw.get("decision") or {}).get("days_per_point", 14)
        ),
        incident_days_per_point=int(
            (decay_raw.get("incident") or {}).get("days_per_point", 28)
        ),
    )

    rebuild_raw = raw.get("rebuild") or {}
    rebuild = RebuildConfig(
        trigger=str(rebuild_raw.get("trigger", "lazy")),
        idle_threshold_hours=float(rebuild_raw.get("idle_threshold_hours", 1.0)),
        resummarize_window_days=int(rebuild_raw.get("resummarize_window_days", 7)),
        lock_path=Path(
            rebuild_raw.get("lock_path", "/tmp/tiger-memory.lock")
        ).expanduser(),
        rebuild_timeout_minutes=int(
            rebuild_raw.get("rebuild_timeout_minutes", 60)
        ),
    )

    briefing_raw = raw.get("briefing") or {}
    walking_raw = briefing_raw.get("walking") or {}
    walking = WalkingConfig(
        full_shorts_working_days=int(
            walking_raw.get("full_shorts_working_days", 2)
        ),
        dailies_working_days=int(walking_raw.get("dailies_working_days", 7)),
        weeklies_working_days=int(walking_raw.get("weeklies_working_days", 28)),
        monthlies_working_days=int(walking_raw.get("monthlies_working_days", 90)),
    )
    _validate_walking(walking)
    briefing = BriefingConfig(
        walking=walking,
        always_first=str(briefing_raw.get("always_first", "must_memorize.md")),
        order=str(briefing_raw.get("order", "oldest_to_newest")),
    )

    return Config(
        agent=agent,
        store=store,
        sources=sources,
        summarizer=summarizer,
        budgets=budgets,
        decay=decay,
        rebuild=rebuild,
        briefing=briefing,
        env_var=str(raw.get("env_var", "TIGER_MEMORY_CONFIG")),
        source_path=source_path,
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


def _validate_walking(w: WalkingConfig) -> None:
    if w.full_shorts_working_days < MIN_FULL_SHORTS_WORKING_DAYS:
        raise ConfigError(
            f"briefing.walking.full_shorts_working_days must be ≥ "
            f"{MIN_FULL_SHORTS_WORKING_DAYS}; got {w.full_shorts_working_days}."
        )
    if w.dailies_working_days < MIN_DAILIES_WORKING_DAYS:
        raise ConfigError(
            f"briefing.walking.dailies_working_days must be ≥ "
            f"{MIN_DAILIES_WORKING_DAYS} (memory-gap guard); got "
            f"{w.dailies_working_days}."
        )
    if w.weeklies_working_days < MIN_WEEKLIES_WORKING_DAYS:
        raise ConfigError(
            f"briefing.walking.weeklies_working_days must be ≥ "
            f"{MIN_WEEKLIES_WORKING_DAYS} (memory-gap guard); got "
            f"{w.weeklies_working_days}."
        )
    # No min on monthlies.

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
    # Chunk-and-reduce (ADR 0006 Part 1). A transcript (or incremental slice)
    # over ``max_staged_content_chars`` is no longer lossy-clipped: it is split
    # on line boundaries into chunks each ``<= chunk_content_chars`` (the map
    # step's per-chunk budget), each chunk is condensed to a neutral digest by
    # the sub-agent, and the concatenated digests are reduced into the final
    # card. ``max_reduce_depth`` caps how many times the digest concatenation
    # may itself be re-condensed before the bounded ``_clip`` last-resort guard
    # fires, so a pathological input can't loop forever. See
    # ``docs/tiger-memory-sweep-protocol.md``.
    chunk_content_chars: int = 80_000
    max_reduce_depth: int = 2
    # Incremental sweep (ADR 0006 Part 2). ``overlap_turns`` is the number of
    # already-processed turns prepended to an incremental slice as *read-only*
    # continuity context (not re-extracted). ``active_slice_threshold_chars`` is
    # the Q3 trigger: a still-active session is extracted early once its
    # unprocessed slice (measured on PREFILTERED content) exceeds this, cutting
    # at the last completed turn boundary so the leak ADR 0006 closes can't
    # re-open while a long session stays live. See
    # ``docs/tiger-memory-sweep-protocol.md``.
    overlap_turns: int = 4
    active_slice_threshold_chars: int = 100_000


@dataclass(frozen=True)
class MemoryExtractConfig:
    """Word budgets for the extraction prompt's three store sections.

    The extraction prompt (``summarizers/prompts/.../extract_memory.md``)
    fills these as per-section length hints. ``max_output_words`` caps the
    whole bundle the summarizer may return.
    """

    skill_procedure_words: int = 120
    memo_words: int = 25
    topic_summary_words: int = 25
    topic_detail_words: int = 80
    team_event_words: int = 15
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
    """Session-start briefing assembly (design §6; ADR 0007).

    The initial load is index-only (must_remember + skill index + topic
    index); detail files sit alongside and load on demand, so there is no
    top-N knob.
    """


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
    """Bounds for the skills store (ADR 0007) — all lengths in characters.

    The *index* (the rendered name+trigger+one-line listing, the only part
    loaded at session start) is bounded by ``index_max_length`` /
    ``index_overflow_limit``. Each skill's *detail* (its procedure body,
    a separate briefing file loaded on demand) is bounded per-skill by
    ``detail_max_length`` / ``detail_overflow_limit``.
    """

    index_max_length: int = 2000
    index_overflow_limit: int = 3000
    detail_max_length: int = 4000
    detail_overflow_limit: int = 6000


@dataclass(frozen=True)
class MustRememberStoreConfig:
    """Bound + freshness for the must-remember store (design §4.2, ADR 0007).

    Tightened by the topic-store revamp: the store loads whole at session
    start, so it must stay small (Operator-set 2000/3000, 2026-07-23).

    ``forget_days``: sweeps TOUCH items related to the session they process
    (refreshing ``last_used``); an item not touched for this long is
    forget-eligible when compaction needs the space. Stale normal items drop
    first; a stale ``operator_explicit`` drops only as the last resort,
    after the relevance check and every stale normal item.
    """

    max_length: int = 2000
    overflow_limit: int = 3000
    forget_days: int = 30


@dataclass(frozen=True)
class TopicsStoreConfig:
    """Bounds + freshness knobs for the topics store (ADR 0007).

    The *index* (slug + freshness + summary per topic — the only part loaded
    at session start) is bounded by ``index_max_length`` /
    ``index_overflow_limit``. Each topic's *detail* body (dated appended
    facts, a separate briefing file loaded on demand) is bounded per-topic by
    ``detail_max_length`` / ``detail_overflow_limit``.

    ``fresh_days``: a topic touched within this window is protected from
    forget/merge during compaction. ``forget_days``: a topic NOT touched for
    this long is forget-eligible; when the index is over its bound, stale
    topics are dropped oldest-first before any AI compaction is staged.
    """

    index_max_length: int = 2000
    index_overflow_limit: int = 3000
    detail_max_length: int = 4000
    detail_overflow_limit: int = 6000
    fresh_days: int = 7
    forget_days: int = 60


@dataclass(frozen=True)
class TeamEventsConfig:
    """Knobs for the team-wide event log (ADR 0008) — one shared file at
    ``<team>/memories/team/events.md``, appended by every persona's sweep
    ingest and read lazily (pointer-only; never part of a briefing load).

    ``recent_days``: daily sections younger than this are never compacted
    (the Operator's uncompacted window). ``year_after_days``: a month
    section whose year has ended longer ago than this folds into a year
    section. ``month_max_chars`` / ``year_max_chars``: target size of one
    folded section (the deterministic trim bound). ``max_length`` /
    ``overflow_limit``: hysteresis bounds over the whole rendered file —
    a size backstop that drops the oldest year/month sections, never the
    daily window. ``enabled: false`` keeps the card contract intact but
    drops the section at ingest (no team log is written).
    """

    enabled: bool = True
    recent_days: int = 30
    year_after_days: int = 400
    month_max_chars: int = 700
    year_max_chars: int = 1000
    max_length: int = 8000
    overflow_limit: int = 12000


@dataclass(frozen=True)
class MemoryConfig:
    """The ``memory:`` block (design §7; stores per ADR 0007).

    ``length_unit`` is CONFIRMED final: ``characters``, never tokens.
    The store bounds are Operator-set defaults (2026-07-22 directive).
    """

    length_unit: str = VALID_LENGTH_UNIT
    skills: SkillsStoreConfig = field(default_factory=SkillsStoreConfig)
    must_remember: MustRememberStoreConfig = field(
        default_factory=MustRememberStoreConfig
    )
    topics: TopicsStoreConfig = field(default_factory=TopicsStoreConfig)
    team_events: TeamEventsConfig = field(default_factory=TeamEventsConfig)


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
        chunk_content_chars=int(
            budgets_raw.get("chunk_content_chars", 80_000)
        ),
        max_reduce_depth=int(
            budgets_raw.get("max_reduce_depth", 2)
        ),
        overlap_turns=int(
            budgets_raw.get("overlap_turns", 4)
        ),
        active_slice_threshold_chars=int(
            budgets_raw.get("active_slice_threshold_chars", 100_000)
        ),
    )

    extract_raw = raw.get("memory_extract") or {}
    memory_extract = MemoryExtractConfig(
        skill_procedure_words=int(extract_raw.get("skill_procedure_words", 120)),
        memo_words=int(extract_raw.get("memo_words", 25)),
        topic_summary_words=int(extract_raw.get("topic_summary_words", 25)),
        topic_detail_words=int(extract_raw.get("topic_detail_words", 80)),
        team_event_words=int(extract_raw.get("team_event_words", 15)),
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

    briefing = BriefingConfig()

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


def _parse_memory(memory_raw: dict[str, Any]) -> MemoryConfig:
    """Parse + validate the ``memory:`` block (design §7).

    All keys are optional (sensible defaults, design §10.2) EXCEPT the
    vendor-neutrality invariants: ``length_unit`` must be ``characters``
    (token units are rejected, design §8), bounds must be positive with
    ``overflow_limit > max`` (the hysteresis band, design §4), and the
    topics freshness windows must be coherent (``forget_days`` must sit at
    or above ``fresh_days`` — a topic cannot be simultaneously protected
    and forget-eligible).
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
        index_max_length=_cfg_int(
            skills_raw.get("index_max_length", 2000),
            "memory.skills.index_max_length",
        ),
        index_overflow_limit=_cfg_int(
            skills_raw.get("index_overflow_limit", 3000),
            "memory.skills.index_overflow_limit",
        ),
        detail_max_length=_cfg_int(
            skills_raw.get("detail_max_length", 4000),
            "memory.skills.detail_max_length",
        ),
        detail_overflow_limit=_cfg_int(
            skills_raw.get("detail_overflow_limit", 6000),
            "memory.skills.detail_overflow_limit",
        ),
    )
    _validate_bound(
        "memory.skills",
        "index_max_length",
        skills.index_max_length,
        skills.index_overflow_limit,
    )
    _validate_bound(
        "memory.skills",
        "detail_max_length",
        skills.detail_max_length,
        skills.detail_overflow_limit,
    )

    mr_raw = memory_raw.get("must_remember") or {}
    must_remember = MustRememberStoreConfig(
        max_length=_cfg_int(
            mr_raw.get("max_length", 2000), "memory.must_remember.max_length"
        ),
        overflow_limit=_cfg_int(
            mr_raw.get("overflow_limit", 3000), "memory.must_remember.overflow_limit"
        ),
        forget_days=_cfg_int(
            mr_raw.get("forget_days", 30), "memory.must_remember.forget_days"
        ),
    )
    _validate_bound(
        "memory.must_remember",
        "max_length",
        must_remember.max_length,
        must_remember.overflow_limit,
    )
    if must_remember.forget_days < 0:
        raise ConfigError(
            f"memory.must_remember.forget_days must be ≥ 0; "
            f"got {must_remember.forget_days}."
        )

    tp_raw = memory_raw.get("topics") or {}
    topics = TopicsStoreConfig(
        index_max_length=_cfg_int(
            tp_raw.get("index_max_length", 2000),
            "memory.topics.index_max_length",
        ),
        index_overflow_limit=_cfg_int(
            tp_raw.get("index_overflow_limit", 3000),
            "memory.topics.index_overflow_limit",
        ),
        detail_max_length=_cfg_int(
            tp_raw.get("detail_max_length", 4000),
            "memory.topics.detail_max_length",
        ),
        detail_overflow_limit=_cfg_int(
            tp_raw.get("detail_overflow_limit", 6000),
            "memory.topics.detail_overflow_limit",
        ),
        fresh_days=_cfg_int(
            tp_raw.get("fresh_days", 7), "memory.topics.fresh_days"
        ),
        forget_days=_cfg_int(
            tp_raw.get("forget_days", 60), "memory.topics.forget_days"
        ),
    )
    _validate_bound(
        "memory.topics",
        "index_max_length",
        topics.index_max_length,
        topics.index_overflow_limit,
    )
    _validate_bound(
        "memory.topics",
        "detail_max_length",
        topics.detail_max_length,
        topics.detail_overflow_limit,
    )
    if topics.fresh_days < 0:
        raise ConfigError(
            f"memory.topics.fresh_days must be ≥ 0; got {topics.fresh_days}."
        )
    if topics.forget_days < topics.fresh_days:
        raise ConfigError(
            f"memory.topics.forget_days ({topics.forget_days}) must be ≥ "
            f"fresh_days ({topics.fresh_days}); a topic cannot be both "
            "protected-fresh and forget-eligible."
        )

    tev_raw = memory_raw.get("team_events") or {}
    team_events = TeamEventsConfig(
        enabled=bool(tev_raw.get("enabled", True)),
        recent_days=_cfg_int(
            tev_raw.get("recent_days", 30), "memory.team_events.recent_days"
        ),
        year_after_days=_cfg_int(
            tev_raw.get("year_after_days", 400),
            "memory.team_events.year_after_days",
        ),
        month_max_chars=_cfg_int(
            tev_raw.get("month_max_chars", 700),
            "memory.team_events.month_max_chars",
        ),
        year_max_chars=_cfg_int(
            tev_raw.get("year_max_chars", 1000),
            "memory.team_events.year_max_chars",
        ),
        max_length=_cfg_int(
            tev_raw.get("max_length", 8000), "memory.team_events.max_length"
        ),
        overflow_limit=_cfg_int(
            tev_raw.get("overflow_limit", 12000),
            "memory.team_events.overflow_limit",
        ),
    )
    _validate_bound(
        "memory.team_events",
        "max_length",
        team_events.max_length,
        team_events.overflow_limit,
    )
    if team_events.recent_days < 0:
        raise ConfigError(
            f"memory.team_events.recent_days must be ≥ 0; "
            f"got {team_events.recent_days}."
        )
    if team_events.year_after_days < team_events.recent_days:
        raise ConfigError(
            f"memory.team_events.year_after_days "
            f"({team_events.year_after_days}) must be ≥ recent_days "
            f"({team_events.recent_days}); a section cannot fold to a year "
            "while still inside the daily window."
        )
    if team_events.month_max_chars <= 0 or team_events.year_max_chars <= 0:
        raise ConfigError(
            "memory.team_events.month_max_chars and year_max_chars must be "
            f"> 0; got {team_events.month_max_chars} / "
            f"{team_events.year_max_chars}."
        )

    return MemoryConfig(
        length_unit=length_unit,
        skills=skills,
        must_remember=must_remember,
        topics=topics,
        team_events=team_events,
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

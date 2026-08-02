"""Staged compaction for the three bounded stores (ADR 0007).

Replaces the retired in-process meditation engine with the same
subscription-rail shape the sweep's extraction uses:

- :func:`compact_plan` (non-AI) scans the stores; every surface at/over its
  ``overflow_limit`` gets one prompt staged under ``.compact-staging/`` plus
  a manifest entry. Deterministic pre-passes that need no judgement run
  here: topics stale beyond ``forget_days`` are dropped outright (oldest
  first) when the topic index is over its ``max``.
- Task sub-agents (spawned by the sweep skill) each read one prompt and
  write one ``<target>.card.md`` — the compacted replacement, per the
  prompt's strict marker contract.
- :func:`compact_apply` (non-AI) validates each card and applies it
  atomically. Convergence is guaranteed deterministically: a surface still
  over its ``max`` after the card is applied is trimmed by keep-rank /
  freshness rules — except protected content (operator-explicit directives,
  fresh topics), which is never force-dropped; a surface that cannot shrink
  without touching protected content stays over-max and is reported.

Prompts and cards are markdown files; the bulky store content transits only
the sub-agent's context, never the driver's (billing model B8).
"""
from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .bounded_store import BoundedStore, StoreLockHeld
from .config import Config
from .entries import (
    KIND_DECISION,
    KIND_OPERATOR_EXPLICIT,
    STORE_MUST_REMEMBER,
    STORE_SKILLS,
    STORE_TOPICS,
    VALID_KINDS,
    EntryError,
    MustRememberEntry,
    SkillEntry,
    TopicEntry,
)
from .ranking import days_between
from .skills import refresh_importance, skills_keep_rank
from .state import iso_now
from .store import Store

log = logging.getLogger("tigerharness.tiger_memory.compaction")

STAGING_DIR_NAME = ".compact-staging"
CARD_SUFFIX = ".card.md"

# Card contract markers, one per target kind.
MARK_MUST_REMEMBER = "@@MUST_REMEMBER@@"
MARK_SKILLS = "@@SKILLS@@"
MARK_TOPIC_ROSTER = "@@TOPIC_ROSTER@@"
MARK_TOPIC_DETAIL = "@@TOPIC_DETAIL@@"

KIND_MUST_REMEMBER = "must_remember"
KIND_SKILLS = "skills"
KIND_TOPIC_ROSTER = "topic_roster"
KIND_TOPIC_DETAIL = "topic_detail"
KIND_SKILL_DETAIL = "skill_detail"


class CompactionParseError(ValueError):
    """A compaction card didn't satisfy its marker contract."""


def _staging_dir(store: Store) -> Path:
    return store.root / STAGING_DIR_NAME


def _prompts_root(cfg: Config) -> Path:
    from .lifecycle import _prompts_root as lifecycle_prompts_root
    return lifecycle_prompts_root(cfg)


def _fill(cfg: Config, name: str, **kwargs) -> str:
    from .lifecycle import _fill_prompt
    return _fill_prompt(_prompts_root(cfg) / name, **kwargs)


def _age_days(last_used: str, now: str) -> float:
    """Age of a freshness anchor in days; UNPARSEABLE → infinitely old.

    ``days_between`` returns ``0.0`` for an unparseable timestamp, which
    read as "touched right now": a corrupt ``last_used`` made an entry
    eternally fresh — immune to stale-forget, merge, and the forced trim,
    a permanent still_over poison pill (audit: bounds finding 5 /
    pipeline finding 6). Treating corruption as infinitely old matches
    ``ranking.recency_score``'s convention (forgotten first).
    """
    from datetime import datetime

    try:
        datetime.fromisoformat(str(last_used).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return float("inf")
    return days_between(last_used, now)


def _is_fresh(entry: TopicEntry, now: str, fresh_days: int) -> bool:
    return _age_days(entry.last_used, now) <= fresh_days


def _is_stale(entry: TopicEntry, now: str, forget_days: int) -> bool:
    return _age_days(entry.last_used, now) > forget_days


# ----- plan -------------------------------------------------------------------


def _forget_stale_topics(
    bstore: BoundedStore, topics: list[TopicEntry], now: str
) -> tuple[list[TopicEntry], list[str]]:
    """Deterministic pre-pass: while the topic index is over ``max``, drop
    topics stale beyond ``forget_days``, oldest first. Never touches a topic
    inside the stale window; returns survivors + the dropped slugs."""
    cfgt = bstore.memory.topics
    dropped: list[str] = []
    survivors = list(topics)
    stale = sorted(
        (t for t in survivors if _is_stale(t, now, cfgt.forget_days)),
        key=lambda t: t.last_used,
    )
    for victim in stale:
        if bstore.index_chars(STORE_TOPICS, survivors) <= cfgt.index_max_length:
            break
        survivors.remove(victim)
        dropped.append(victim.slug)
    return survivors, dropped


def _dedup_must_remember(
    entries: list[MustRememberEntry],
) -> tuple[list[MustRememberEntry], int]:
    """Merge EXACT duplicate memos (same kind + whitespace/case-normalized
    text) into their first occurrence, summing ``repeat_count`` and keeping
    the freshest ``last_used``. Deterministic and
    loss-free — dropping an identical copy loses nothing, and the repeat
    signal is preserved. Sweeps re-capture the same directive verbatim
    session after session, so this is the cheap first shrink."""
    seen: dict[tuple[str, str], MustRememberEntry] = {}
    survivors: list[MustRememberEntry] = []
    dropped = 0
    for e in entries:
        key = (e.kind, " ".join(e.text.split()).lower())
        first = seen.get(key)
        if first is None:
            seen[key] = e
            survivors.append(e)
        else:
            first.repeat_count += e.repeat_count
            first.last_used = max(first.last_used, e.last_used)
            dropped += 1
    return survivors, dropped


def _dedup_skills(entries: list[SkillEntry]) -> tuple[list[SkillEntry], int]:
    """Merge EXACT duplicate skills (same normalized name + trigger +
    procedure), summing ``usage_count`` and keeping the freshest
    ``last_used``. Same loss-free rationale as the memo dedup."""
    seen: dict[tuple[str, str, str], SkillEntry] = {}
    survivors: list[SkillEntry] = []
    dropped = 0
    for e in entries:
        key = (
            " ".join(e.name.split()).lower(),
            " ".join(e.trigger.split()).lower(),
            " ".join(e.procedure.split()).lower(),
        )
        first = seen.get(key)
        if first is None:
            seen[key] = e
            survivors.append(e)
        else:
            first.usage_count += e.usage_count
            first.last_used = max(first.last_used, e.last_used)
            dropped += 1
    return survivors, dropped


def _render_mr_blocks(
    entries: list[MustRememberEntry],
    *,
    with_ids: bool = False,
    now: str | None = None,
    forget_days: int | None = None,
) -> str:
    """Render memo blocks for a compaction prompt.

    When *now* + *forget_days* are given, an item whose ``last_used`` is
    older than the forget window is annotated ``[forget-eligible]`` with its
    age — the sweep's TOUCH mechanism has not refreshed it, so the card
    author should drop it unless it is still clearly valuable.
    """
    if not entries:
        return "(none)"
    out = []
    for e in entries:
        lines = []
        if with_ids:
            lines.append(f"ID: {e.id}")
        lines.append(f"KIND: {e.kind}")
        lines.append(f"MEMO: {e.text}")
        if now is not None and forget_days is not None:
            age = _age_days(e.last_used, now)
            if age > forget_days:
                age_label = (
                    "unknown (corrupt timestamp)" if age == float("inf")
                    else f"{int(age)} days"
                )
                lines.append(
                    f"AGE: untouched {age_label} [forget-eligible]"
                )
        out.append("\n".join(lines))
    return "\n\n".join(out)


def _render_skill_blocks(entries: list[SkillEntry]) -> str:
    out = []
    for e in entries:
        out.append(
            f"NAME: {e.name}\nTRIGGER: {e.trigger}\n"
            f"PROCEDURE: {e.procedure}\n"
            f"(usage {e.usage_count}×, importance {float(e.importance):.2f}, "
            f"last used {e.last_used[:10]})"
        )
    return "\n\n".join(out)


def _render_roster(
    topics: list[TopicEntry], now: str, fresh_days: int
) -> str:
    out = []
    for e in sorted(topics, key=lambda t: t.last_used, reverse=True):
        fresh = " [fresh]" if _is_fresh(e, now, fresh_days) else ""
        out.append(
            f"- `{e.slug}` — {e.name}{fresh} · last {e.last_used[:10]} · "
            f"{e.touch_count}× · detail {len(e.text)} chars\n  {e.summary}"
        )
    return "\n".join(out)


def _locked_prepass(bstore: BoundedStore, store_name: str, mutate):
    """Run a plan pre-pass as lock → FRESH load → mutate → save.

    *mutate* receives the freshly-loaded entries and returns
    ``(new_entries, meta, changed)``; the save happens only when
    *changed*. Returns ``(entries, meta)`` — on a held lock, ``None``
    (the pre-pass is an optimization; plan proceeds with the unmutated
    store).

    The load MUST happen inside the lock (audit F3): the old shape
    loaded lockless, computed, then took the lock only for the save —
    anything written between the load and the locked save (a pin, a
    concurrent ingest) was clobbered by the stale snapshot.
    """
    try:
        with bstore.store_lock(store_name):
            entries = bstore.load(store_name)
            new_entries, meta, changed = mutate(entries)
            if changed:
                bstore.save_atomic(store_name, new_entries)
            return new_entries, meta
    except StoreLockHeld:
        log.warning(
            "compact-plan: %s locked by a live session; skipping this "
            "wake's pre-pass", store_name,
        )
        return None


def compact_plan(cfg: Config, store: Store, *, now: str | None = None) -> dict:
    """Stage one compaction prompt per over-bound surface; return the manifest.

    Runs the deterministic stale-topic forget first (no AI needed for
    "not refreshed for a while"), then stages prompts only for surfaces
    still at/over their ``overflow_limit``. Writes
    ``.compact-staging/manifest.json``; an empty ``targets`` list means
    nothing needs compacting.
    """
    now = now or iso_now()
    bstore = BoundedStore(cfg, store)
    staging = _staging_dir(store)
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    targets: list[dict] = []
    dropped_stale: list[str] = []

    # --- topics: deterministic stale forget, then roster + detail targets ---
    topics = [
        e for e in bstore.load(STORE_TOPICS) if isinstance(e, TopicEntry)
    ]
    if bstore.is_over_overflow(STORE_TOPICS, topics):
        def _forget_mut(entries):
            fresh = [e for e in entries if isinstance(e, TopicEntry)]
            survivors, dropped = _forget_stale_topics(bstore, fresh, now)
            return survivors, dropped, bool(dropped)

        res = _locked_prepass(bstore, STORE_TOPICS, _forget_mut)
        if res is not None:
            topics, dropped_stale = res
            if dropped_stale:
                log.info(
                    "compact-plan: forgot %d stale topic(s): %s",
                    len(dropped_stale), ", ".join(dropped_stale),
                )
    if bstore.is_over_overflow(STORE_TOPICS, topics):
        prompt_path = staging / "topic_roster.prompt.md"
        prompt_path.write_text(
            _fill(
                cfg, "compact_topic_roster.md",
                agent_name=cfg.agent.name,
                current_chars=bstore.index_chars(STORE_TOPICS, topics),
                max_chars=cfg.memory.topics.index_max_length,
                fresh_days=cfg.memory.topics.fresh_days,
                summary_max_words=cfg.memory_extract.topic_summary_words,
                roster=_render_roster(
                    topics, now, cfg.memory.topics.fresh_days
                ),
            ),
            encoding="utf-8",
        )
        targets.append(_target(KIND_TOPIC_ROSTER, "topic_roster", prompt_path))
    roster_staged = any(t["kind"] == KIND_TOPIC_ROSTER for t in targets)
    if roster_staged:
        # Detail targets are DEFERRED whenever the roster itself is being
        # compacted: a roster merge folds one topic's body into another, and
        # a detail card authored against the pre-merge body would overwrite
        # the merged-in knowledge. The still-oversized detail re-stages next
        # sweep against the settled store.
        deferred = [t.slug for t in topics if bstore.is_detail_over_overflow(t)]
        if deferred:
            log.info(
                "compact-plan: deferring %d topic detail target(s) behind "
                "the roster compaction: %s", len(deferred), ", ".join(deferred),
            )
    for t in ([] if roster_staged else topics):
        if bstore.is_detail_over_overflow(t):
            key = f"topic_detail.{t.slug}"
            prompt_path = staging / f"{key}.prompt.md"
            prompt_path.write_text(
                _fill(
                    cfg, "compact_topic_detail.md",
                    agent_name=cfg.agent.name,
                    topic_name=t.name,
                    topic_slug=t.slug,
                    current_chars=bstore.detail_chars(t),
                    max_chars=cfg.memory.topics.detail_max_length,
                    body=t.text,
                ),
                encoding="utf-8",
            )
            targets.append(
                _target(KIND_TOPIC_DETAIL, key, prompt_path, slug=t.slug)
            )

    # --- skills: exact-dup merge, then index target + detail targets ---
    def _dedup_skills_mut(entries):
        fresh = [e for e in entries if isinstance(e, SkillEntry)]
        merged, deduped = _dedup_skills(fresh)
        if deduped:
            for e in merged:
                refresh_importance(e, now, cfg)
        return merged, deduped, bool(deduped)

    res = _locked_prepass(bstore, STORE_SKILLS, _dedup_skills_mut)
    if res is not None:
        skills, deduped_skills = res
        if deduped_skills:
            log.info(
                "compact-plan: merged %d exact-duplicate skill(s)",
                deduped_skills,
            )
    else:
        skills = [e for e in bstore.load(STORE_SKILLS) if isinstance(e, SkillEntry)]
        deduped_skills = 0
    skills_index_staged = False
    if bstore.is_over_overflow(STORE_SKILLS, skills):
        skills_index_staged = True
        prompt_path = staging / "skills.prompt.md"
        prompt_path.write_text(
            _fill(
                cfg, "compact_skills.md",
                agent_name=cfg.agent.name,
                current_chars=bstore.index_chars(STORE_SKILLS, skills),
                max_chars=cfg.memory.skills.index_max_length,
                procedure_max_words=cfg.memory_extract.skill_procedure_words,
                entries=_render_skill_blocks(
                    sorted(
                        skills,
                        key=lambda e: skills_keep_rank(e, now, cfg),
                        reverse=True,
                    )
                ),
            ),
            encoding="utf-8",
        )
        targets.append(
            _target(
                KIND_SKILLS, "skills", prompt_path,
                snapshot_ids=[s.id for s in skills],
            )
        )
    if skills_index_staged:
        deferred_skills = [
            s.id for s in skills if bstore.is_detail_over_overflow(s)
        ]
        if deferred_skills:
            # Same deferral rule as topics: the index card is a full
            # replacement roster (procedures authored at plan time), so a
            # detail rewrite applied in the same run would either dangle or
            # be overwritten. Re-stages next sweep against the settled store.
            log.info(
                "compact-plan: deferring %d skill detail target(s) behind "
                "the index compaction", len(deferred_skills),
            )
    for s in ([] if skills_index_staged else skills):
        if bstore.is_detail_over_overflow(s):
            key = f"skill_detail.{s.id}"
            prompt_path = staging / f"{key}.prompt.md"
            prompt_path.write_text(
                _fill(
                    cfg, "compact_skill_detail.md",
                    agent_name=cfg.agent.name,
                    skill_name=s.name,
                    skill_trigger=s.trigger,
                    current_chars=bstore.detail_chars(s),
                    max_chars=cfg.memory.skills.detail_max_length,
                    procedure=s.procedure,
                ),
                encoding="utf-8",
            )
            targets.append(
                _target(KIND_SKILL_DETAIL, key, prompt_path, entry_id=s.id)
            )

    # --- must_remember: exact-dup merge, then the compact target ---
    def _dedup_mr_mut(entries):
        fresh = [e for e in entries if isinstance(e, MustRememberEntry)]
        merged, deduped = _dedup_must_remember(fresh)
        return merged, deduped, bool(deduped)

    res = _locked_prepass(bstore, STORE_MUST_REMEMBER, _dedup_mr_mut)
    if res is not None:
        must, deduped_mr = res
        if deduped_mr:
            log.info(
                "compact-plan: merged %d exact-duplicate memo(s)", deduped_mr
            )
    else:
        must = [
            e for e in bstore.load(STORE_MUST_REMEMBER)
            if isinstance(e, MustRememberEntry)
        ]
        deduped_mr = 0
    if bstore.is_over_overflow(STORE_MUST_REMEMBER, must):
        protected = [e for e in must if e.kind == KIND_OPERATOR_EXPLICIT]
        compactable = [e for e in must if e.kind != KIND_OPERATOR_EXPLICIT]
        budget = cfg.memory.must_remember.max_length - bstore.length_chars(
            protected
        )
        from .lifecycle import team_mission_text
        mission = team_mission_text(cfg).strip() or "(no charter mission found)"
        forget_days = cfg.memory.must_remember.forget_days
        prompt_path = staging / "must_remember.prompt.md"
        prompt_path.write_text(
            _fill(
                cfg, "compact_must_remember.md",
                agent_name=cfg.agent.name,
                current_chars=bstore.length_chars(must),
                # Floor the advertised budget: when protected entries alone
                # exceed max_length the arithmetic goes ≤ 0 and the card
                # author is told "compact to 0 chars" — a nonsense
                # instruction that breeds malformed cards in exactly the
                # stuck state that needs a clean card most (audit: bounds
                # finding 8b).
                max_chars=max(200, budget),
                forget_days=forget_days,
                protected=_render_mr_blocks(
                    protected, with_ids=True, now=now, forget_days=forget_days
                ),
                entries=_render_mr_blocks(
                    compactable, now=now, forget_days=forget_days
                ),
                mission=mission,
            ),
            encoding="utf-8",
        )
        targets.append(
            _target(
                KIND_MUST_REMEMBER, "must_remember", prompt_path,
                snapshot_ids=[e.id for e in must],
            )
        )

    manifest = {
        "generated_at": now,
        "dropped_stale_topics": dropped_stale,
        "deduped_skills": deduped_skills,
        "deduped_must_remember": deduped_mr,
        "targets": targets,
    }
    (staging / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    log.info("compact-plan: staged %d target(s)", len(targets))
    return manifest


def _target(kind: str, key: str, prompt_path: Path, **extra) -> dict:
    card_path = prompt_path.with_name(f"{key}{CARD_SUFFIX}")
    item = {
        "kind": kind,
        "key": key,
        "prompt_path": str(prompt_path),
        "card_path": str(card_path),
    }
    item.update(extra)
    return item


# ----- card parsing -------------------------------------------------------------


def _section_after_marker(text: str, marker: str) -> str:
    """The card body under *marker* (whole-line match). Raises on a missing
    marker or an empty card."""
    if not text or not text.strip():
        raise CompactionParseError("empty compaction card")
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip() == marker:
            return "\n".join(lines[i + 1:]).strip()
    raise CompactionParseError(f"missing marker {marker}")


def _blocks(section: str) -> list[dict[str, str]]:
    from .lifecycle import _section_blocks
    return _section_blocks(section)


def _ref(raw: str | None) -> str:
    """Backtick/whitespace-tolerant id/slug reference.

    Compaction prompts display addresses backticked (`` `slug` ``); a card
    echoing the displayed form must still resolve — a silently-missed lookup
    would turn the model's judgement into a no-op and hand the decision to
    the deterministic trim instead.
    """
    from .lifecycle import clean_ref
    return clean_ref(raw)


# ----- apply -------------------------------------------------------------------


@dataclass
class ApplyReport:
    applied: list[str]
    skipped_no_card: list[str]
    malformed: list[dict]
    forced_trims: list[str]
    still_over: list[str]
    locked: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "applied": self.applied,
            "skipped_no_card": self.skipped_no_card,
            "malformed": self.malformed,
            "forced_trims": self.forced_trims,
            "still_over": self.still_over,
            "locked": self.locked,
        }


def compact_apply(cfg: Config, store: Store, *, now: str | None = None) -> ApplyReport:
    """Validate + apply every staged compaction card (one process, race-free).

    Cards are applied per target; a malformed card is reported and skipped
    (the surface stays over-bound and re-stages next sweep). After each
    apply, the deterministic convergence pass trims any surface still over
    its ``max`` — except protected content, which is never force-dropped.
    """
    now = now or iso_now()
    staging = _staging_dir(store)
    manifest_path = staging / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"no compaction manifest at {manifest_path}; run compact-plan first"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bstore = BoundedStore(cfg, store)
    report = ApplyReport([], [], [], [], [])

    for item in manifest.get("targets", []):
        key = item["key"]
        card_path = Path(item["card_path"])
        if not card_path.exists():
            report.skipped_no_card.append(key)
            continue
        text = card_path.read_text(encoding="utf-8")
        try:
            _apply_one(bstore, cfg, item, text, now, report)
        except (CompactionParseError, EntryError) as exc:
            report.malformed.append({"key": key, "error": str(exc)})
            continue
        except StoreLockHeld as exc:
            # One contended store must not abort the whole apply with the
            # other targets' consumed cards left on disk — a blind re-run
            # would re-apply them and mint duplicates (audit F7 / pipeline
            # finding 3). Skip just this target; it re-stages next sweep.
            log.warning("compact-apply: %s skipped (%s)", key, exc)
            report.locked.append(key)
            continue
        report.applied.append(key)
        # Drop this target's staging files IMMEDIATELY after its store
        # commit — a crash later in the loop must not leave an
        # already-applied card behind for a duplicating re-run.
        Path(item["prompt_path"]).unlink(missing_ok=True)
        card_path.unlink(missing_ok=True)

    # Consume the manifest once nothing actionable remains: a stale
    # manifest lets a mis-sequenced later `compact-apply` exit 0 "clean"
    # against 10-day-old targets instead of the loud exit-2 the protocol
    # promises (audit: drift finding 6). Malformed/locked targets keep it
    # for a targeted retry.
    if not report.malformed and not report.locked:
        manifest_path.unlink(missing_ok=True)

    log.info(
        "compact-apply: %d applied, %d skipped, %d locked, %d malformed, "
        "%d forced trims",
        len(report.applied), len(report.skipped_no_card),
        len(report.locked), len(report.malformed), len(report.forced_trims),
    )
    return report


def _apply_one(
    bstore: BoundedStore,
    cfg: Config,
    item: dict,
    text: str,
    now: str,
    report: ApplyReport,
) -> None:
    kind = item["kind"]
    if kind == KIND_MUST_REMEMBER:
        _apply_must_remember(
            bstore, cfg, text, now, report,
            snapshot_ids=set(item.get("snapshot_ids") or []),
        )
    elif kind == KIND_SKILLS:
        _apply_skills(
            bstore, cfg, text, now, report,
            snapshot_ids=set(item.get("snapshot_ids") or []),
        )
    elif kind == KIND_TOPIC_ROSTER:
        _apply_topic_roster(bstore, cfg, text, now, report)
    elif kind == KIND_TOPIC_DETAIL:
        _apply_topic_detail(bstore, cfg, item["slug"], text, now, report)
    elif kind == KIND_SKILL_DETAIL:
        _apply_skill_detail(bstore, cfg, item["entry_id"], text, now, report)
    else:
        raise CompactionParseError(f"unknown target kind {kind!r}")


def _apply_must_remember(
    bstore: BoundedStore, cfg: Config, text: str, now: str, report: ApplyReport,
    *, snapshot_ids: set[str] = frozenset(),
) -> None:
    section = _section_after_marker(text, MARK_MUST_REMEMBER)
    new_entries: list[MustRememberEntry] = []
    stale_ids: set[str] = set()
    for b in _blocks(section):
        if "STALE" in b:
            # The relevance-check verdict on a protected directive: the id
            # names an operator_explicit entry judged no longer relevant to
            # the live mission. It is DOWNGRADED (to `decision`, rejoining
            # the normal decay pool) — never directly dropped.
            stale_ids.add(_ref(b["STALE"]))
            if not (b.get("KIND") or b.get("MEMO")):
                continue
            # A sloppy card merged a memo and a STALE verdict into one
            # block (no blank line) — keep BOTH rather than silently
            # dropping the memo (mirrors the TOUCH parser's tolerance).
        kind = (b.get("KIND") or "").lower()
        memo = b.get("MEMO")
        if kind not in VALID_KINDS or not memo:
            raise CompactionParseError(f"bad must_remember block: {b!r}")
        if kind == KIND_OPERATOR_EXPLICIT:
            # Protected entries are carried over automatically; a card must
            # not mint new operator directives out of a compaction.
            continue
        new_entries.append(
            MustRememberEntry(
                text=memo, created_at=now, last_used=now, source="compact",
                kind=kind,
            )
        )
    with bstore.store_lock(STORE_MUST_REMEMBER):
        current = [
            e for e in bstore.load(STORE_MUST_REMEMBER)
            if isinstance(e, MustRememberEntry)
        ]
        protected: list[MustRememberEntry] = []
        downgraded: list[MustRememberEntry] = []
        post_plan: list[MustRememberEntry] = []
        forget_days = cfg.memory.must_remember.forget_days
        for e in current:
            if e.kind != KIND_OPERATOR_EXPLICIT:
                # The card replaces only what its prompt SAW (the plan-time
                # snapshot). Anything written between plan and apply — a pin,
                # a concurrent ingest — survives; the trim below still
                # enforces the bound.
                if snapshot_ids and e.id not in snapshot_ids:
                    post_plan.append(e)
                continue
            if e.id in stale_ids:
                if _age_days(e.last_used, now) <= forget_days:
                    # The documented contract is that only a directive
                    # untouched past forget_days may be relevance-
                    # downgraded; a card must not strip a FRESH
                    # operator_explicit of its protection (audit: drift
                    # finding 10).
                    log.warning(
                        "compact-apply: STALE verdict on FRESH operator "
                        "directive %r refused (touched within %d days)",
                        e.id, forget_days,
                    )
                    protected.append(e)
                    continue
                e.kind = KIND_DECISION
                downgraded.append(e)
            else:
                protected.append(e)
        if downgraded:
            log.info(
                "compact-apply: relevance-check downgraded %d operator "
                "directive(s) to decision", len(downgraded),
            )
        # Freshness carry-over: a memo the card KEPT (same kind + normalized
        # text as a snapshot entry) inherits that entry's identity and
        # signals — compaction must not reset the TOUCH clock or the repeat
        # count of a surviving memo.
        by_key = {
            (e.kind, " ".join(e.text.split()).lower()): e
            for e in current
            if e.kind != KIND_OPERATOR_EXPLICIT
            and (not snapshot_ids or e.id in snapshot_ids)
        }
        for e in new_entries:
            old = by_key.pop((e.kind, " ".join(e.text.split()).lower()), None)
            if old is not None:
                e.id = old.id
                e.created_at = old.created_at
                e.last_used = old.last_used
                e.repeat_count = old.repeat_count
        merged: list[MustRememberEntry] = (
            protected + downgraded + new_entries + post_plan
        )
        # Deterministic convergence, in the ADR 0007 drop order: stale
        # normal entries first (oldest untouched first — the sweep's TOUCH
        # mechanism kept everything that still comes up fresh), then fresh
        # normal entries by keep-rank, and only as the very last resort a
        # STALE protected directive (untouched past forget_days). A fresh
        # operator_explicit is never dropped.
        max_len = cfg.memory.must_remember.max_length
        if bstore.length_chars(merged) > max_len:
            report.forced_trims.append(STORE_MUST_REMEMBER)
            for victim in _mr_drop_order(
                merged, now, cfg.memory.must_remember.forget_days
            ):
                if bstore.length_chars(merged) <= max_len:
                    break
                if victim.kind == KIND_OPERATOR_EXPLICIT:
                    log.warning(
                        "compact-apply: forgetting stale operator directive "
                        "%r (untouched past forget_days; last resort)",
                        victim.id,
                    )
                merged.remove(victim)
        if bstore.length_chars(merged) > max_len:
            report.still_over.append(STORE_MUST_REMEMBER)
        bstore.save_atomic(STORE_MUST_REMEMBER, merged)


def _mr_drop_order(
    entries: list[MustRememberEntry], now: str, forget_days: int
) -> list[MustRememberEntry]:
    """The deterministic forget order for must_remember convergence.

    1. stale normal entries, oldest ``last_used`` first;
    2. fresh normal entries, lowest (repeat_count, recency) first;
    3. stale ``operator_explicit`` directives, oldest first (last resort).

    Fresh ``operator_explicit`` entries are absent — never droppable.
    """
    def stale(e: MustRememberEntry) -> bool:
        return _age_days(e.last_used, now) > forget_days

    normal = [e for e in entries if e.kind != KIND_OPERATOR_EXPLICIT]
    stale_normal = sorted(
        (e for e in normal if stale(e)), key=lambda e: e.last_used
    )
    fresh_normal = sorted(
        (e for e in normal if not stale(e)),
        key=lambda e: (e.repeat_count, e.last_used),
    )
    stale_protected = sorted(
        (
            e for e in entries
            if e.kind == KIND_OPERATOR_EXPLICIT and stale(e)
        ),
        key=lambda e: e.last_used,
    )
    return stale_normal + fresh_normal + stale_protected


def _apply_skills(
    bstore: BoundedStore, cfg: Config, text: str, now: str, report: ApplyReport,
    *, snapshot_ids: set[str] = frozenset(),
) -> None:
    section = _section_after_marker(text, MARK_SKILLS)
    new_entries: list[SkillEntry] = []
    for b in _blocks(section):
        name, trigger, proc = b.get("NAME"), b.get("TRIGGER"), b.get("PROCEDURE")
        if not (name and trigger and proc):
            raise CompactionParseError(f"bad skill block: {b!r}")
        new_entries.append(
            SkillEntry(
                text=proc, created_at=now, last_used=now, source="compact",
                name=name, trigger=trigger, procedure=proc,
            )
        )
    with bstore.store_lock(STORE_SKILLS):
        current = [
            e for e in bstore.load(STORE_SKILLS) if isinstance(e, SkillEntry)
        ]
        # Carry identity + usage/recency forward for kept skills (matched by
        # name, case-insensitive, within the plan-time snapshot) so
        # compaction does not reset the keep-rank — and does not re-mint ids
        # (detail filenames and future detail targets are id-addressed).
        by_name = {
            e.name.strip().lower(): e
            for e in current
            if not snapshot_ids or e.id in snapshot_ids
        }
        for e in new_entries:
            old = by_name.pop(e.name.strip().lower(), None)
            if old is not None:
                e.id = old.id
                e.usage_count = old.usage_count
                e.created_at = old.created_at
                e.last_used = old.last_used
            refresh_importance(e, now, cfg)
        # Skills written between plan and apply (concurrent ingest) survive
        # the card's replacement roster — the trim below still enforces the
        # bound.
        post_plan = [
            e for e in current
            if snapshot_ids and e.id not in snapshot_ids
        ]
        merged: list[SkillEntry] = list(new_entries) + post_plan
        max_len = cfg.memory.skills.index_max_length
        if bstore.index_chars(STORE_SKILLS, merged) > max_len:
            report.forced_trims.append(STORE_SKILLS)
            for victim in sorted(
                list(merged), key=lambda e: skills_keep_rank(e, now, cfg)
            ):
                if bstore.index_chars(STORE_SKILLS, merged) <= max_len:
                    break
                merged.remove(victim)
        if bstore.index_chars(STORE_SKILLS, merged) > max_len:
            report.still_over.append(STORE_SKILLS)
        bstore.save_atomic(STORE_SKILLS, merged)


def _apply_topic_roster(
    bstore: BoundedStore, cfg: Config, text: str, now: str, report: ApplyReport
) -> None:
    section = _section_after_marker(text, MARK_TOPIC_ROSTER)
    directives = _blocks(section)
    cfgt = cfg.memory.topics
    with bstore.store_lock(STORE_TOPICS):
        topics = [
            e for e in bstore.load(STORE_TOPICS) if isinstance(e, TopicEntry)
        ]
        by_slug = {t.slug: t for t in topics}
        for d in directives:
            action = (d.get("ACTION") or "").lower()
            if action == "forget":
                slug = _ref(d.get("TOPIC"))
                victim = by_slug.get(slug)
                if victim is None:
                    continue
                if _is_fresh(victim, now, cfgt.fresh_days):
                    log.warning(
                        "compact-apply: refusing to forget fresh topic %r", slug
                    )
                    continue
                topics.remove(victim)
                del by_slug[slug]
            elif action == "merge":
                into_slug = _ref(d.get("INTO"))
                into = by_slug.get(into_slug)
                if into is None:
                    continue
                for from_slug in (d.get("FROM") or "").split():
                    src = by_slug.get(_ref(from_slug))
                    if src is None or src is into:
                        continue
                    if _is_fresh(src, now, cfgt.fresh_days):
                        log.warning(
                            "compact-apply: refusing to merge away fresh "
                            "topic %r", src.slug
                        )
                        continue
                    into.text = f"{into.text.rstrip()}\n\n{src.text.strip()}"
                    into.touch_count += src.touch_count
                    into.last_used = max(into.last_used, src.last_used)
                    topics.remove(src)
                    del by_slug[src.slug]
                summary = (d.get("SUMMARY") or "").strip()
                if summary:
                    into.summary = summary
            elif action == "summary":
                slug = _ref(d.get("TOPIC"))
                summary = (d.get("SUMMARY") or "").strip()
                target = by_slug.get(slug)
                if target is not None and summary:
                    target.summary = summary
            else:
                raise CompactionParseError(f"bad roster directive: {d!r}")
        # Deterministic convergence: still over max → forget non-fresh topics,
        # oldest-touched first. Fresh topics are never force-dropped.
        max_len = cfgt.index_max_length
        if bstore.index_chars(STORE_TOPICS, topics) > max_len:
            report.forced_trims.append(STORE_TOPICS)
            for victim in sorted(topics, key=lambda t: t.last_used):
                if bstore.index_chars(STORE_TOPICS, topics) <= max_len:
                    break
                if _is_fresh(victim, now, cfgt.fresh_days):
                    continue
                topics.remove(victim)
        if bstore.index_chars(STORE_TOPICS, topics) > max_len:
            report.still_over.append(STORE_TOPICS)
        bstore.save_atomic(STORE_TOPICS, topics)


_DATE_SECTION_RE = re.compile(
    r"(?m)^## \d{4}-\d{2}-\d{2}\s*$"
)


def _split_dated_sections(body: str) -> list[str]:
    """Split a topic body into its dated sections (each starts at a ``##``
    date heading). Content before the first heading (e.g. an "earlier"
    digest) stays attached to the first section."""
    marks = [m.start() for m in _DATE_SECTION_RE.finditer(body)]
    if len(marks) <= 1:
        return [body]
    cuts = [0] + marks[1:] + [len(body)]
    return [body[cuts[i]:cuts[i + 1]] for i in range(len(cuts) - 1)]


def _apply_topic_detail(
    bstore: BoundedStore, cfg: Config, slug: str, text: str, now: str,
    report: ApplyReport,
) -> None:
    body = _section_after_marker(text, MARK_TOPIC_DETAIL)
    if not body:
        raise CompactionParseError("empty topic detail body")
    if body.strip().upper() == "NONE":
        # Sibling card contracts teach "write exactly NONE", but a topic
        # detail card has no NONE option — accepting it would replace the
        # topic's whole history with the literal string. Malformed instead.
        raise CompactionParseError(
            "topic detail card has no NONE option — emit the full rewritten body"
        )
    with bstore.store_lock(STORE_TOPICS):
        topics = [
            e for e in bstore.load(STORE_TOPICS) if isinstance(e, TopicEntry)
        ]
        target = next((t for t in topics if t.slug == slug), None)
        if target is None:
            # The topic vanished between plan and apply (e.g. the roster
            # card merged/forgot it). Nothing to do.
            return
        target.text = body
        # Deterministic convergence: drop oldest dated sections until the
        # detail file fits its max — the same "oldest goes first" rule the
        # roster uses.
        max_len = cfg.memory.topics.detail_max_length
        if bstore.detail_chars(target) > max_len:
            report.forced_trims.append(f"topic_detail.{slug}")
            sections = _split_dated_sections(target.text)
            while len(sections) > 1:
                candidate = "".join(sections[1:]).lstrip("\n")
                target.text = candidate
                sections = sections[1:]
                if bstore.detail_chars(target) <= max_len:
                    break
        # Last resort for a card whose body has no droppable dated sections
        # (bullets-only, malformed headings, or one giant section): trim
        # oldest lines from the top, then hard-truncate — the convergence
        # guarantee must not depend on the card's formatting discipline.
        while bstore.detail_chars(target) > max_len and "\n" in target.text:
            target.text = target.text.split("\n", 1)[1].lstrip("\n")
        if bstore.detail_chars(target) > max_len:
            overshoot = bstore.detail_chars(target) - max_len
            keep = max(1, len(target.text) - overshoot - 1)
            target.text = "…" + target.text[-keep:]
        if bstore.detail_chars(target) > max_len:
            report.still_over.append(f"topic_detail.{slug}")
        bstore.save_atomic(STORE_TOPICS, topics)


def _apply_skill_detail(
    bstore: BoundedStore, cfg: Config, entry_id: str, text: str, now: str,
    report: ApplyReport,
) -> None:
    section = _section_after_marker(text, MARK_SKILLS)
    blocks = _blocks(section)
    if len(blocks) != 1:
        raise CompactionParseError(
            f"skill detail card must contain exactly one block; got {len(blocks)}"
        )
    b = blocks[0]
    name, trigger, proc = b.get("NAME"), b.get("TRIGGER"), b.get("PROCEDURE")
    if not (name and trigger and proc):
        raise CompactionParseError(f"bad skill block: {b!r}")
    with bstore.store_lock(STORE_SKILLS):
        skills = [
            e for e in bstore.load(STORE_SKILLS) if isinstance(e, SkillEntry)
        ]
        target = next((s for s in skills if s.id == entry_id), None)
        if target is None:
            return  # merged/dropped by the roster card between plan and apply
        target.name = name
        target.trigger = trigger
        target.procedure = proc
        target.text = proc
        max_len = cfg.memory.skills.detail_max_length
        if bstore.detail_chars(target) > max_len:
            report.forced_trims.append(f"skill_detail.{entry_id}")
            overshoot = bstore.detail_chars(target) - max_len
            keep = max(1, len(target.procedure) - overshoot - 1)
            target.procedure = target.procedure[:keep].rstrip() + "…"
            target.text = target.procedure
        if bstore.detail_chars(target) > max_len:
            report.still_over.append(f"skill_detail.{entry_id}")
        bstore.save_atomic(STORE_SKILLS, skills)

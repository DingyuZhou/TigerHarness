"""B2 QA-defense hardening tests for the tiger-memory revamp (Sakuragi).

These tests ATTACK the assumed-away edges of a self-pruning memory system
with NO safety net, mapping to the b2-qa-sakuragi attack surface:

1. forget-order with nothing safe to forget (the no-safety-net anchor);
2. decay boundaries (exact 0, crossing 0, ±cap, tiny, large day-counts);
3. relevance-downgrade ordering (downgrade BEFORE forget; rejoin pool);
4. concurrent meditation (StoreLockHeld; crashed/stale lock);
5. skill-index edges (zero / exactly-max / overflow band / past overflow);
6. character-length edges (unicode/multibyte, empty, hysteresis boundary);
7. idempotency (no-op under max; running meditate twice is stable);
8. malformed input (corrupt frontmatter / bad types / missing fields).

Everything runs under a scripted mock summarizer (plan §5b) — ZERO
live-model calls. Where the build genuinely holds, these stay as hardening
regression locks. One concrete robustness gap on the lenient-read contract
is documented as an ``xfail`` (see ``test_load_bad_numeric_type_*``).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from textwrap import dedent

import pytest

from tigerharness.tiger_memory.bounded_store import (
    BoundedStore,
    ForgetGuardError,
    StoreLockHeld,
)
from tigerharness.tiger_memory.briefing import _render_skill_index
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.emotional import clamp_weight, decay_entry, decay_weight
from tigerharness.tiger_memory.entries import (
    KIND_DECISION,
    KIND_OWNER_EXPLICIT,
    KIND_PREFERENCE,
    EmotionalEntry,
    EntryError,
    MustRememberEntry,
    SkillEntry,
)
from tigerharness.tiger_memory.meditation import keep_rank, meditate
from tigerharness.tiger_memory.store import Store
from tigerharness.tiger_memory.summarizers.base import Summarizer

NOW = "2026-06-17T00:00:00Z"
OLD = "2025-01-01T00:00:00Z"
# A timestamp AFTER NOW (clock-skew / out-of-order): days_between floors at 0.
FUTURE = "2027-01-01T00:00:00Z"
MISSION = "Ship the bounded memory revamp."


# ----- scripted mock summarizer (plan §5b; mirrors test_meditation.py) ------


class ScriptedSummarizer(Summarizer):
    """Deterministic verdicts keyed off entry text — no model call."""

    name = "scripted"
    version = "v1"

    def __init__(
        self,
        *,
        similar_pairs=(),
        stale_texts=(),
        compact_map=None,
    ) -> None:
        super().__init__()
        self.similar_pairs = {frozenset(p) for p in similar_pairs}
        self.stale_texts = set(stale_texts)
        self.compact_map = dict(compact_map or {})
        self.calls: list[str] = []

    def summarize(self, *, prompt: str, max_words: int) -> str:
        self.calls.append(prompt)
        if prompt.startswith("Are these two memory entries"):
            a = prompt.split("ENTRY A:\n", 1)[1].split("\n\nENTRY B:", 1)[0]
            b = prompt.split("ENTRY B:\n", 1)[1].rstrip("\n")
            return "YES" if frozenset({a, b}) in self.similar_pairs else "NO"
        if "STALE" in prompt:
            d = prompt.split("DIRECTIVE:\n", 1)[1].rstrip("\n")
            return "YES" if d in self.stale_texts else "NO"
        body = prompt.split("Return ONLY the rewritten text.\n\n", 1)[1].rstrip("\n")
        return self.compact_map.get(body, body)


# ----- fixtures -------------------------------------------------------------


def _make_store(tmp_path: Path, **memory) -> BoundedStore:
    mem_yaml = dedent(
        f"""\
        memory:
          skills:
            max_count: {memory.get('skills_max', 2)}
            overflow_limit: {memory.get('skills_overflow', 3)}
          must_remember:
            max_length: {memory.get('mr_max', 30)}
            overflow_limit: {memory.get('mr_overflow', 50)}
          emotional_log:
            max_length: {memory.get('emo_max', 30)}
            overflow_limit: {memory.get('emo_overflow', 50)}
            weight_cap: {memory.get('cap', 10)}
            decay:
              magnitude_per_day: {memory.get('rate', 0.1)}
        """
    )
    p = tmp_path / "cfg.yaml"
    p.write_text(
        dedent(
            f"""\
            agent:
              name: Sakuragi
              role: qa
            store:
              root: {tmp_path}/memory
            sources:
              - kind: claude_code
                project_path: {tmp_path}/p/
            summarizer:
              backend: anthropic
              model: m
              prompts: default/v1
            """
        )
        + mem_yaml
    )
    cfg = load_config(p)
    store = Store(cfg.store.root)
    store.init_layout()
    return BoundedStore(cfg, store)


def _mr(kind, text, last_used=NOW, imp=0.0):
    return MustRememberEntry(
        text=text, created_at=NOW, last_used=last_used, source="pin",
        kind=kind, importance=imp,
    )


def _emo(weight, text, last_used=NOW):
    return EmotionalEntry(
        text=text, created_at=NOW, last_used=last_used, source="extract",
        weight=weight, reaction="r",
    )


def _skill(name, usage=0, last_used=NOW, text="b"):
    return SkillEntry(
        text=text, created_at=NOW, last_used=last_used, source="extract",
        name=name, trigger="t", procedure="p", usage_count=usage,
    )


# ====================================================================
# 1. Forget-order with nothing safe to forget (the no-safety-net anchor)
# ====================================================================


def test_all_owner_directives_over_max_left_intact_no_force_drop(
    tmp_path: Path,
) -> None:
    """3 still-relevant owner directives, all over max, none similar/stale:
    NOTHING is force-dropped; over_max warns; every directive survives."""
    bs = _make_store(tmp_path, mr_max=20, mr_overflow=30)
    owners = [
        _mr(KIND_OWNER_EXPLICIT, "never push without approval here"),
        _mr(KIND_OWNER_EXPLICIT, "always run the full test suite!!"),
        _mr(KIND_OWNER_EXPLICIT, "commit messages must use a heredoc"),
    ]
    bs.save_atomic("must_remember", owners)
    summ = ScriptedSummarizer()  # nothing similar, nothing stale
    log = meditate("must_remember", "ctx", MISSION, summ, bs.cfg, bs)
    assert log.over_max is True
    assert log.forgotten == []
    survivors = bs.load("must_remember")
    assert len(survivors) == 3
    assert all(e.kind == KIND_OWNER_EXPLICIT for e in survivors)
    assert {e.id for e in survivors} == {o.id for o in owners}


def test_guard_skips_protected_owner_drops_droppable_neighbor(
    tmp_path: Path,
) -> None:
    """A still-relevant owner directive is the LOWEST keep-rank candidate, but
    a droppable preference also overflows. The forget-guard must SKIP the
    protected owner (not force-drop) and drop the preference instead, then
    leave the still-over-max owner intact with over_max set."""
    bs = _make_store(tmp_path, mr_max=20, mr_overflow=30)
    # owner has the LOWER importance, so it sorts first in forget order, but
    # the guard must refuse it and move on to the preference.
    owner = _mr(KIND_OWNER_EXPLICIT, "protected owner directive!!", imp=0.0)
    pref = _mr(KIND_PREFERENCE, "droppable preference text!!", imp=5.0)
    bs.save_atomic("must_remember", [owner, pref])
    summ = ScriptedSummarizer()
    log = meditate("must_remember", "ctx", MISSION, summ, bs.cfg, bs)
    assert pref.id in log.forgotten
    assert owner.id not in log.forgotten
    assert log.over_max is True
    survivors = bs.load("must_remember")
    assert [e.id for e in survivors] == [owner.id]


def test_meditation_logs_irreversible_mutations_for_audit(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """b2g logs gate: a meditation that forgets MUST emit an auditable INFO
    log naming what was forgotten/merged/downgraded/compacted. Forgetting is
    irreversible with no safety net, so the loss cannot be invisible to an
    auditor (b2g-logs REVISE -> b1-dev-2 fix)."""
    bs = _make_store(tmp_path, mr_max=20, mr_overflow=30)
    owner = _mr(KIND_OWNER_EXPLICIT, "protected owner directive!!", imp=9.0)
    pref = _mr(KIND_PREFERENCE, "droppable preference text!!", imp=0.0)
    bs.save_atomic("must_remember", [owner, pref])
    summ = ScriptedSummarizer()
    with caplog.at_level(
        logging.INFO, logger="tigerharness.tiger_memory.meditation"
    ):
        log = meditate("must_remember", "ctx", MISSION, summ, bs.cfg, bs)
    assert pref.id in log.forgotten
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "meditation must_remember" in m and pref.id in m for m in msgs
    ), "the forgotten id must be auditable in the logs"


def test_forget_guard_raises_on_direct_unchecked_owner_drop(
    tmp_path: Path,
) -> None:
    """The guard primitive itself: dropping an owner directive whose id is NOT
    in relevance_checked_ids raises ForgetGuardError (no silent loss)."""
    bs = _make_store(tmp_path)
    owner = _mr(KIND_OWNER_EXPLICIT, "ship friday")
    with pytest.raises(ForgetGuardError, match="relevance-check"):
        bs.forget("must_remember", [owner], [owner.id])


def test_merge_into_owner_then_terminal_over_max_protects_survivor(
    tmp_path: Path,
) -> None:
    """A duplicate folds INTO an owner survivor (importance bumped); the lone
    owner survivor is still over max but still-relevant -> the guard protects
    it and the terminal over_max path fires (no force-drop of a merged owner)."""
    bs = _make_store(tmp_path, mr_max=20, mr_overflow=30)
    owner = _mr(KIND_OWNER_EXPLICIT, "ship the revamp now ok")  # 22 > max 20
    dup = _mr(KIND_PREFERENCE, "ship the revamp now ok")
    bs.save_atomic("must_remember", [owner, dup])
    summ = ScriptedSummarizer(similar_pairs=[(owner.text, dup.text)])
    log = meditate("must_remember", "ctx", MISSION, summ, bs.cfg, bs)
    assert dup.id in log.merged and log.merged[dup.id] == owner.id
    assert log.forgotten == []
    assert log.over_max is True
    survivors = bs.load("must_remember")
    assert len(survivors) == 1
    assert survivors[0].kind == KIND_OWNER_EXPLICIT
    assert survivors[0].importance == 1.0  # merge bump


# ====================================================================
# 2. Decay boundaries
# ====================================================================


def test_decay_exactly_zero_stays_zero_no_sign_flip(tmp_path: Path) -> None:
    bs = _make_store(tmp_path, rate=0.1)
    out = decay_weight(0.0, 5, bs.cfg)
    assert out == 0.0
    assert str(out) == "0.0"  # never -0.0


def test_decay_crossing_zero_clamps_no_opposite_sign(tmp_path: Path) -> None:
    """A weight that would mathematically pass through 0 pins at 0, never the
    opposite sign — from both directions, and at a tiny magnitude."""
    bs = _make_store(tmp_path, rate=0.1)
    assert decay_weight(0.05, 1, bs.cfg) == 0.0   # 0.05 - 0.1 = -0.05 -> 0
    assert decay_weight(-0.05, 1, bs.cfg) == 0.0
    # float-arithmetic landing: 0.3 - 0.1*3 is a hair below 0.
    out = decay_weight(0.3, 3, bs.cfg)
    assert out == 0.0
    assert str(out) == "0.0"


def test_decay_at_plus_minus_cap_clamps_first(tmp_path: Path) -> None:
    """An over-cap input is clamped to ±cap before decay, even at days=0."""
    bs = _make_store(tmp_path, cap=10, rate=0.1)
    assert decay_weight(99.0, 0, bs.cfg) == 10.0
    assert decay_weight(-99.0, 0, bs.cfg) == -10.0
    # exactly at the cap, no decay
    assert decay_weight(10.0, 0, bs.cfg) == 10.0
    assert decay_weight(-10.0, 0, bs.cfg) == -10.0


def test_decay_tiny_magnitude_preserves_sign(tmp_path: Path) -> None:
    """A magnitude that survives one tiny step keeps its sign (no -0.0)."""
    bs = _make_store(tmp_path, rate=0.1)
    out = decay_weight(0.10000000001, 1, bs.cfg)
    assert out > 0.0  # survived, still positive
    neg = decay_weight(-0.10000000001, 1, bs.cfg)
    assert neg < 0.0


def test_decay_huge_day_count_pins_at_zero(tmp_path: Path) -> None:
    bs = _make_store(tmp_path, rate=0.1)
    assert decay_weight(9.99, 1e12, bs.cfg) == 0.0
    assert decay_weight(-9.99, 1e12, bs.cfg) == 0.0


def test_clamp_collapses_negative_zero(tmp_path: Path) -> None:
    bs = _make_store(tmp_path)
    out = clamp_weight(-0.0, bs.cfg)
    assert out == 0.0
    assert str(out) == "0.0"


def test_decay_entry_future_last_used_floors_days_no_growth(
    tmp_path: Path,
) -> None:
    """An entry whose last_used is AFTER now (clock skew) decays 0 days — the
    magnitude is NOT grown by a negative span."""
    bs = _make_store(tmp_path, rate=0.1)
    e = _emo(5.0, "x", last_used=FUTURE)
    assert decay_entry(e, NOW, bs.cfg) == 5.0


def test_decay_entry_unparseable_last_used_is_clamped_identity(
    tmp_path: Path,
) -> None:
    """A corrupt last_used -> 0 elapsed days -> clamped identity (no crash)."""
    bs = _make_store(tmp_path, rate=0.1)
    e = _emo(5.0, "x", last_used="not-a-date")
    assert decay_entry(e, NOW, bs.cfg) == 5.0


def test_merge_clamps_at_cap_on_combine(tmp_path: Path) -> None:
    """Merging two strong same-sign feelings clamps the survivor at the cap —
    repeated merges can never inflate past ±cap (design §4.3)."""
    bs = _make_store(tmp_path, emo_max=40, emo_overflow=50, cap=10)
    # Share content words so the QI-2 prefilter lets the pair reach the
    # (scripted) summarizer — a real near-duplicate always shares tokens.
    a = _emo(8.0, "loved the clean api design")
    b = _emo(7.0, "loved that clean api so much")
    bs.save_atomic("emotional", [a, b])
    summ = ScriptedSummarizer(similar_pairs=[(a.text, b.text)])
    meditate("emotional", "ctx", MISSION, summ, bs.cfg, bs)
    survivors = bs.load("emotional")
    assert len(survivors) == 1
    assert survivors[0].weight == 10.0  # clamped at cap


# ====================================================================
# 3. Relevance-downgrade ordering (downgrade BEFORE forget; rejoin pool)
# ====================================================================


def test_stale_owner_downgraded_then_forgotten_no_guard_error(
    tmp_path: Path,
) -> None:
    """Step 2 (downgrade) precedes step 4 (forget): a stale owner directive is
    downgraded to `decision`, joins relevance_checked, and is dropped without
    the forget-guard tripping."""
    bs = _make_store(tmp_path, mr_max=15, mr_overflow=25)
    keep = _mr(KIND_PREFERENCE, "keep me short", imp=5.0)
    stale = _mr(KIND_OWNER_EXPLICIT, "stale directive to be dropped", OLD)
    bs.save_atomic("must_remember", [keep, stale])
    summ = ScriptedSummarizer(stale_texts=[stale.text])
    log = meditate("must_remember", "ctx", MISSION, summ, bs.cfg, bs)
    assert stale.id in log.downgraded
    assert stale.id in log.forgotten
    survivors = bs.load("must_remember")
    assert [e.id for e in survivors] == [keep.id]
    assert all(e.kind != KIND_OWNER_EXPLICIT for e in survivors)


def test_downgraded_directive_rejoins_pool_but_ranks_by_importance(
    tmp_path: Path,
) -> None:
    """After downgrade, the (now-decision) directive is ranked like any normal
    entry: a higher-importance downgraded directive outranks a weak preference
    and is dropped LAST, proving it genuinely rejoined the ordinary pool."""
    bs = _make_store(tmp_path, mr_max=30, mr_overflow=40)
    # both ~14 chars; total 28 < max(30) AFTER one drop. weak pref imp 0,
    # downgraded-but-important owner imp 9 -> weak goes first.
    weak = _mr(KIND_PREFERENCE, "weak little x", imp=0.0)
    big = _mr(KIND_OWNER_EXPLICIT, "big important", imp=9.0)
    extra = _mr(KIND_PREFERENCE, "filler entry x", imp=1.0)
    bs.save_atomic("must_remember", [weak, big, extra])  # ~40 chars > max 30
    summ = ScriptedSummarizer(stale_texts=[big.text])  # big is downgraded
    log = meditate("must_remember", "ctx", MISSION, summ, bs.cfg, bs)
    assert big.id in log.downgraded
    survivors = bs.load("must_remember")
    survivor_ids = {e.id for e in survivors}
    # weak (imp 0) dropped first; the downgraded-but-important `big` survives.
    assert big.id in survivor_ids
    assert weak.id not in survivor_ids


def test_relevant_owner_not_added_to_checked_set(tmp_path: Path) -> None:
    """A still-relevant owner directive is NOT downgraded and NOT added to the
    relevance-checked set, so the guard keeps protecting it (policy reading
    Rukawa flagged: relevant directives are never licensed to drop)."""
    bs = _make_store(tmp_path, mr_max=20, mr_overflow=30)
    relevant = _mr(KIND_OWNER_EXPLICIT, "still relevant directive x")
    droppable = _mr(KIND_PREFERENCE, "droppable filler entry zz")
    bs.save_atomic("must_remember", [relevant, droppable])
    summ = ScriptedSummarizer()  # nothing stale
    log = meditate("must_remember", "ctx", MISSION, summ, bs.cfg, bs)
    assert relevant.id not in log.downgraded
    survivors = bs.load("must_remember")
    # the relevant owner is protected; the preference is the only legal drop.
    assert any(e.id == relevant.id and e.kind == KIND_OWNER_EXPLICIT
               for e in survivors)


# ====================================================================
# 4. Concurrent meditation (store_lock)
# ====================================================================


def test_second_live_holder_refused_with_store_lock_held(
    tmp_path: Path,
) -> None:
    bs = _make_store(tmp_path, mr_max=20, mr_overflow=30)
    bs.save_atomic("must_remember", [_mr(KIND_PREFERENCE, "x" * 40)])
    lock_file = bs.store.paths.journal / ".must_remember.lock"
    lock_file.write_text(f"{os.getpid()} 0")  # our PID = live holder
    summ = ScriptedSummarizer()
    with pytest.raises(StoreLockHeld):
        meditate("must_remember", "ctx", MISSION, summ, bs.cfg, bs)
    # The foreign lock was NOT removed (we never owned it).
    assert lock_file.exists()
    lock_file.unlink()


def test_crashed_holder_lock_is_reclaimed(tmp_path: Path) -> None:
    """A stale lock from a dead PID is reclaimed so meditation proceeds."""
    bs = _make_store(tmp_path, emo_max=20, emo_overflow=30)
    bs.save_atomic("emotional", [_emo(0.5, "a" * 15), _emo(9.0, "b" * 15)])
    lock_file = bs.store.paths.journal / ".emotional.lock"
    lock_file.write_text("999999 0")  # dead PID
    summ = ScriptedSummarizer()
    log = meditate("emotional", "ctx", MISSION, summ, bs.cfg, bs)
    assert log.changed is True  # proceeded after reclaiming the stale lock
    assert not lock_file.exists()  # released on exit


def test_lock_released_on_no_op_under_max(tmp_path: Path) -> None:
    """Even a no-op (under max) acquires and then RELEASES the lock cleanly."""
    bs = _make_store(tmp_path, mr_max=100, mr_overflow=200)
    bs.save_atomic("must_remember", [_mr(KIND_PREFERENCE, "short")])
    lock_file = bs.store.paths.journal / ".must_remember.lock"
    summ = ScriptedSummarizer()
    log = meditate("must_remember", "ctx", MISSION, summ, bs.cfg, bs)
    assert log.skipped_no_op is True
    assert not lock_file.exists()


# ====================================================================
# 5. Skill-index edges (zero / exactly-max / overflow band / past)
# ====================================================================


def test_skill_index_zero_skills_renders_placeholder(tmp_path: Path) -> None:
    out = _render_skill_index([])
    assert "no skills learned yet" in out


def test_skill_index_only_loads_index_not_full_procedure(tmp_path: Path) -> None:
    """The index shows name+trigger+a ONE-LINE lesson; the rest of a multi-line
    procedure is NOT inlined (progressive disclosure, design §4.1) — only the
    first line surfaces, the full skill is loaded on demand from skills.md."""
    s = _skill("DeployFix")
    s.procedure = (
        "One-line summary that may show.\n"
        "HIDDEN multi-step detail that must stay out of the index.\n"
        "HIDDEN third step too."
    )
    out = _render_skill_index([s])
    assert "DeployFix" in out
    assert "HIDDEN multi-step detail" not in out  # full procedure not inlined
    assert "HIDDEN third step" not in out
    assert "skills.md" in out  # points the persona to load the full skill


def test_skill_store_exactly_at_max_is_no_op(tmp_path: Path) -> None:
    """Exactly max_count skills (under overflow) is a no-op meditation."""
    bs = _make_store(tmp_path, skills_max=2, skills_overflow=3)
    bs.save_atomic("skills", [_skill("A", usage=1), _skill("B", usage=2)])
    summ = ScriptedSummarizer()
    log = meditate("skills", "ctx", MISSION, summ, bs.cfg, bs)
    assert log.skipped_no_op is True
    assert len(bs.load("skills")) == 2


def test_skill_store_in_overflow_band_under_max_is_no_op(
    tmp_path: Path,
) -> None:
    """A skills store IN the [max, overflow) band but at/under max-count is a
    no-op: meditation never drops a skill while inside the hysteresis band."""
    bs = _make_store(tmp_path, skills_max=3, skills_overflow=5)
    bs.save_atomic("skills", [_skill(f"S{i}", usage=i + 1) for i in range(3)])
    # 3 == max(3) -> over_max is FALSE -> no-op even though caller could fire.
    summ = ScriptedSummarizer()
    log = meditate("skills", "ctx", MISSION, summ, bs.cfg, bs)
    assert log.skipped_no_op is True
    assert len(bs.load("skills")) == 3


def test_skill_store_past_overflow_forgets_least_used_first(
    tmp_path: Path,
) -> None:
    """Past overflow: the least-used skill is dropped first; compaction is
    skipped (count-bounded store)."""
    bs = _make_store(tmp_path, skills_max=1, skills_overflow=2)
    least = _skill("Least", usage=1)
    most = _skill("Most", usage=99)
    bs.save_atomic("skills", [least, most])
    summ = ScriptedSummarizer()  # nothing similar
    log = meditate("skills", "ctx", MISSION, summ, bs.cfg, bs)
    assert log.compacted == []  # count-bounded -> no compaction
    survivors = bs.load("skills")
    assert len(survivors) == 1 and survivors[0].name == "Most"
    assert least.id in log.forgotten


def test_skill_index_rebuild_orders_by_importance(tmp_path: Path) -> None:
    """Rebuild orders the index most-important first (only the index loaded)."""
    low = _skill("Low")
    low.importance = 0.5
    high = _skill("High")
    high.importance = 9.0
    out = _render_skill_index([low, high])
    assert out.index("## High") < out.index("## Low")


# ====================================================================
# 6. Character-length edges (unicode, empty, hysteresis boundary)
# ====================================================================


def test_length_chars_counts_unicode_codepoints_not_bytes(
    tmp_path: Path,
) -> None:
    """Multibyte unicode is counted by code points (vendor-neutral chars), not
    UTF-8 bytes — so an emoji counts as one character."""
    bs = _make_store(tmp_path)
    e = _emo(1.0, "café☕日本語")  # 8 code points, many more bytes
    assert bs.length_chars([e]) == len("café☕日本語") + len("r")


def test_empty_store_length_is_zero_and_not_over_overflow(
    tmp_path: Path,
) -> None:
    bs = _make_store(tmp_path)
    assert bs.length_chars([]) == 0
    assert bs.is_over_overflow("emotional", []) is False
    assert bs.count([]) == 0


def test_is_over_overflow_exactly_at_overflow_limit_is_true(
    tmp_path: Path,
) -> None:
    """At/above overflow_limit -> True (meditation fires); inside the band ->
    False (hysteresis, no thrash)."""
    bs = _make_store(tmp_path, mr_max=30, mr_overflow=50)
    in_band = [_mr(KIND_PREFERENCE, "x" * 40)]  # 30 <= 40 < 50
    assert bs.is_over_overflow("must_remember", in_band) is False
    at_limit = [_mr(KIND_PREFERENCE, "x" * 50)]  # == overflow_limit
    assert bs.is_over_overflow("must_remember", at_limit) is True


def test_skills_overflow_exactly_at_limit_by_count(tmp_path: Path) -> None:
    bs = _make_store(tmp_path, skills_max=3, skills_overflow=5)
    four = [_skill(f"s{i}") for i in range(4)]  # 4 in band -> False
    assert bs.is_over_overflow("skills", four) is False
    five = four + [_skill("s4")]  # == overflow_limit -> True
    assert bs.is_over_overflow("skills", five) is True


def test_unicode_entry_roundtrips_through_save_load(tmp_path: Path) -> None:
    """A multibyte/emoji entry survives the atomic save+load roundtrip intact."""
    bs = _make_store(tmp_path)
    e = _emo(3.0, "決定: ship 🚀 — café")
    bs.save_atomic("emotional", [e])
    got = bs.load("emotional")
    assert len(got) == 1 and got[0].text == "決定: ship 🚀 — café"


# ====================================================================
# 7. Idempotency
# ====================================================================


def test_meditate_under_max_is_noop_no_summarizer_calls(tmp_path: Path) -> None:
    bs = _make_store(tmp_path, mr_max=100, mr_overflow=200)
    bs.save_atomic("must_remember", [_mr(KIND_PREFERENCE, "short")])
    summ = ScriptedSummarizer()
    log = meditate("must_remember", "ctx", MISSION, summ, bs.cfg, bs)
    assert log.skipped_no_op is True
    assert log.changed is False
    assert summ.calls == []


def test_meditate_twice_is_stable(tmp_path: Path) -> None:
    """Running meditate twice: the second pass is a no-op and the store is
    byte-for-byte stable (idempotent compaction)."""
    bs = _make_store(tmp_path, emo_max=20, emo_overflow=30)
    bs.save_atomic("emotional", [_emo(0.5, "a" * 15), _emo(9.0, "b" * 15)])
    summ1 = ScriptedSummarizer()
    log1 = meditate("emotional", "ctx", MISSION, summ1, bs.cfg, bs)
    assert log1.changed is True
    after1 = (bs.store.paths.journal / "emotional.md").read_text()

    summ2 = ScriptedSummarizer()
    log2 = meditate("emotional", "ctx", MISSION, summ2, bs.cfg, bs)
    assert log2.skipped_no_op is True
    assert log2.changed is False
    assert summ2.calls == []  # no LLM work on the stable second pass
    after2 = (bs.store.paths.journal / "emotional.md").read_text()
    assert after1 == after2  # byte-for-byte stable


def test_terminal_over_max_is_idempotent(tmp_path: Path) -> None:
    """A store of all still-relevant owner directives over max stays intact on
    repeated meditation — over_max each time, never erodes."""
    bs = _make_store(tmp_path, mr_max=20, mr_overflow=30)
    owners = [
        _mr(KIND_OWNER_EXPLICIT, "directive one cannot drop"),
        _mr(KIND_OWNER_EXPLICIT, "directive two cannot drop"),
    ]
    bs.save_atomic("must_remember", owners)
    for _ in range(3):
        log = meditate("must_remember", "ctx", MISSION, ScriptedSummarizer(),
                       bs.cfg, bs)
        assert log.over_max is True
        assert log.forgotten == []
    survivors = bs.load("must_remember")
    assert {e.id for e in survivors} == {o.id for o in owners}


# ====================================================================
# 8. Malformed input
# ====================================================================


def test_save_rejects_bad_weight_type_with_entry_error(tmp_path: Path) -> None:
    bs = _make_store(tmp_path)
    e = _emo(1.0, "x")
    e.weight = "heavy"  # type: ignore[assignment]
    with pytest.raises(EntryError, match="weight"):
        bs.save_atomic("emotional", [e])
    assert not (bs.store.paths.journal / "emotional.md").exists()


def test_save_rejects_bool_weight_with_entry_error(tmp_path: Path) -> None:
    """A bool is not a valid signed number (Python bool is an int subclass —
    validation must reject it explicitly)."""
    bs = _make_store(tmp_path)
    e = _emo(1.0, "x")
    e.weight = True  # type: ignore[assignment]
    with pytest.raises(EntryError, match="weight"):
        bs.save_atomic("emotional", [e])


def test_save_rejects_missing_required_skill_field(tmp_path: Path) -> None:
    bs = _make_store(tmp_path)
    s = _skill("N")
    s.trigger = ""  # missing required field
    with pytest.raises(EntryError, match="trigger"):
        bs.save_atomic("skills", [s])


def test_save_rejects_invalid_kind(tmp_path: Path) -> None:
    bs = _make_store(tmp_path)
    m = _mr(KIND_PREFERENCE, "x")
    m.kind = "bogus_kind"
    with pytest.raises(EntryError, match="kind"):
        bs.save_atomic("must_remember", [m])


def test_load_skips_block_with_no_frontmatter(tmp_path: Path) -> None:
    """A block with no parseable frontmatter is skipped, the good one kept
    (lenient read, per load()'s contract)."""
    bs = _make_store(tmp_path)
    good = _skill("Good")
    bs.save_atomic("skills", [good])
    path = bs.store.paths.journal / "skills.md"
    path.write_text(
        path.read_text()
        + "\n<!-- tiger-memory-entry -->\njunk with no frontmatter\n"
    )
    got = bs.load("skills")
    assert len(got) == 1 and got[0].name == "Good"


def test_load_corrupt_yaml_block_is_skipped(tmp_path: Path) -> None:
    """A block whose frontmatter is invalid YAML is skipped, not a crash."""
    bs = _make_store(tmp_path)
    good = _skill("Good")
    bs.save_atomic("skills", [good])
    path = bs.store.paths.journal / "skills.md"
    bad_block = (
        "<!-- tiger-memory-entry -->\n"
        "---\n"
        "id: x\n"
        ": : not valid yaml : :\n"
        "---\n"
        "body\n"
    )
    path.write_text(path.read_text() + "\n" + bad_block)
    got = bs.load("skills")
    assert len(got) == 1 and got[0].name == "Good"


def test_load_bad_numeric_type_should_skip_not_crash_whole_store(
    tmp_path: Path,
) -> None:
    """A good entry followed by one with a bad numeric type: the good entry
    still loads (lenient read).

    FIXED (b2 REVISE → b1-dev-1): ``entry_from_frontmatter`` now coerces
    numerics through ``_coerce_int`` / ``_coerce_float``, raising a clean
    ``EntryError`` on a bad value; ``BoundedStore.load`` catches it per-block,
    skips+logs the corrupt entry, and keeps its good siblings. No raw
    ``ValueError`` escapes and no good entry is lost. See b2-sakuragi.md."""
    bs = _make_store(tmp_path)
    path = bs.store.paths.journal / "must_remember.md"
    bs.store.paths.journal.mkdir(parents=True, exist_ok=True)
    path.write_text(
        dedent(
            """\
            ---
            id: good1
            store: must_remember
            created_at: 2026-06-17T00:00:00Z
            last_used: 2026-06-17T00:00:00Z
            source: pin
            kind: preference
            importance: 1.0
            ---
            good entry body
            <!-- tiger-memory-entry -->
            ---
            id: bad1
            store: must_remember
            created_at: 2026-06-17T00:00:00Z
            last_used: 2026-06-17T00:00:00Z
            source: pin
            kind: preference
            importance: WAT
            ---
            bad entry body
            """
        )
    )
    # CONTRACT: the good entry survives; the corrupt one is skipped (or a
    # clean EntryError is raised). Today neither holds — a raw ValueError
    # escapes and the good entry is lost with it.
    try:
        got = bs.load("must_remember")
    except EntryError:
        return  # a clean EntryError would also be acceptable
    assert any(e.id == "good1" for e in got), (
        "good entry must survive a sibling's bad numeric type"
    )


def test_load_bad_int_usage_count_skips_not_crashes(tmp_path: Path) -> None:
    """Sibling case of the b2 finding for the int path: a skill with a bad
    ``usage_count`` raises a clean ``EntryError`` from ``_coerce_int``, which
    ``load`` catches and skips — the good skill survives."""
    bs = _make_store(tmp_path)
    path = bs.store.paths.journal / "skills.md"
    bs.store.paths.journal.mkdir(parents=True, exist_ok=True)
    path.write_text(
        dedent(
            """\
            ---
            id: skillgood
            store: skills
            created_at: 2026-06-17T00:00:00Z
            last_used: 2026-06-17T00:00:00Z
            source: extraction
            name: good skill
            trigger: when X
            procedure: do Y
            usage_count: 3
            importance: 1.0
            ---
            good skill body
            <!-- tiger-memory-entry -->
            ---
            id: skillbad
            store: skills
            created_at: 2026-06-17T00:00:00Z
            last_used: 2026-06-17T00:00:00Z
            source: extraction
            name: bad skill
            trigger: when Z
            procedure: do W
            usage_count: not-an-int
            importance: 1.0
            ---
            bad skill body
            """
        )
    )
    try:
        got = bs.load("skills")
    except EntryError:
        return  # a clean EntryError would also be acceptable
    assert any(e.id == "skillgood" for e in got), (
        "good skill must survive a sibling's bad usage_count"
    )
    assert not any(e.id == "skillbad" for e in got), (
        "the corrupt skill must be skipped, not loaded"
    )


def test_load_non_utf8_byte_does_not_crash_store(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """GAP-4: a single non-UTF8 byte in one block must NOT raise
    ``UnicodeDecodeError`` and deny the whole store. The file is decoded with
    ``errors="replace"`` so the mangled block degrades to a skip while the
    good sibling still loads; a warning is logged."""
    bs = _make_store(tmp_path)
    good = _skill("Good")
    bs.save_atomic("skills", [good])
    path = bs.store.paths.journal / "skills.md"
    # Append a second block carrying a raw non-UTF8 byte in its body.
    raw = path.read_bytes()
    bad_block = (
        b"\n<!-- tiger-memory-entry -->\n"
        b"---\n"
        b"id: badbyte\n"
        b"store: skills\n"
        b"created_at: 2026-06-17T00:00:00Z\n"
        b"last_used: 2026-06-17T00:00:00Z\n"
        b"source: extract\n"
        b"name: byte skill\n"
        b"trigger: when X\n"
        b"procedure: do \xff Y\n"  # <- non-UTF8 byte
        b"importance: 1.0\n"
        b"---\n"
        b"body \xff text\n"
    )
    path.write_bytes(raw + bad_block)
    with caplog.at_level(logging.WARNING):
        got = bs.load("skills")
    # The good sibling survives; no exception was raised.
    assert any(e.name == "Good" for e in got)
    assert any("non-UTF8" in r.getMessage() for r in caplog.records)


def test_load_skips_block_failing_validate(tmp_path: Path) -> None:
    """QI-1: an entry that PARSES but is schema-invalid (empty ``reaction``)
    must be skipped on load, not silently kept. Load is now symmetric with
    ``save_atomic`` — the good sibling survives, the invalid one is dropped."""
    bs = _make_store(tmp_path)
    path = bs.store.paths.journal / "emotional.md"
    bs.store.paths.journal.mkdir(parents=True, exist_ok=True)
    path.write_text(
        dedent(
            """\
            ---
            id: emogood
            store: emotional
            created_at: 2026-06-17T00:00:00Z
            last_used: 2026-06-17T00:00:00Z
            source: extract
            weight: 2.0
            reaction: glad it shipped
            ---
            good emotional body
            <!-- tiger-memory-entry -->
            ---
            id: emobad
            store: emotional
            created_at: 2026-06-17T00:00:00Z
            last_used: 2026-06-17T00:00:00Z
            source: extract
            weight: 1.0
            reaction: ""
            ---
            bad emotional body
            """
        )
    )
    got = bs.load("emotional")
    assert any(e.id == "emogood" for e in got)
    assert not any(e.id == "emobad" for e in got), (
        "an empty-reaction entry must be skipped on load (load/save symmetry)"
    )


def test_load_skips_nan_weight_entry(tmp_path: Path) -> None:
    """QI-1 / GAP-3 load side: a ``weight: .nan`` block parses but fails
    ``validate`` (non-finite), so load skips it — a NaN entry can never reach
    the keep-rank ordering or the next save."""
    bs = _make_store(tmp_path)
    path = bs.store.paths.journal / "emotional.md"
    bs.store.paths.journal.mkdir(parents=True, exist_ok=True)
    path.write_text(
        dedent(
            """\
            ---
            id: emofinite
            store: emotional
            created_at: 2026-06-17T00:00:00Z
            last_used: 2026-06-17T00:00:00Z
            source: extract
            weight: 9.0
            reaction: strongest feeling
            ---
            finite body
            <!-- tiger-memory-entry -->
            ---
            id: emonan
            store: emotional
            created_at: 2026-06-17T00:00:00Z
            last_used: 2026-06-17T00:00:00Z
            source: extract
            weight: .nan
            reaction: poison
            ---
            nan body
            """
        )
    )
    got = bs.load("emotional")
    assert [e.id for e in got] == ["emofinite"]


def test_meditate_does_not_crash_on_preexisting_invalid_entry(
    tmp_path: Path,
) -> None:
    """QI-1: a store carrying a previously-tolerated schema-invalid entry
    (empty ``reaction``) used to crash meditation's FINAL ``save_atomic`` after
    merge/forget already mutated state. With validate-on-load the invalid entry
    is skipped on load, so meditation runs cleanly over only the good entries
    and persists without raising."""
    # emotional max_length=30, overflow_limit=50; push over overflow so
    # meditation actually runs (not a no-op).
    bs = _make_store(tmp_path, emo_max=30, emo_overflow=50)
    path = bs.store.paths.journal / "emotional.md"
    bs.store.paths.journal.mkdir(parents=True, exist_ok=True)
    path.write_text(
        dedent(
            """\
            ---
            id: emoA
            store: emotional
            created_at: 2026-06-17T00:00:00Z
            last_used: 2026-06-17T00:00:00Z
            source: extract
            weight: 2.0
            reaction: reaction about the long first emotional memory entry
            ---
            the first emotional memory body that is fairly long
            <!-- tiger-memory-entry -->
            ---
            id: emoB
            store: emotional
            created_at: 2026-06-17T00:00:00Z
            last_used: 2026-06-17T00:00:00Z
            source: extract
            weight: 3.0
            reaction: reaction about the long second emotional memory entry
            ---
            the second emotional memory body that is also fairly long
            <!-- tiger-memory-entry -->
            ---
            id: emoinvalid
            store: emotional
            created_at: 2026-06-17T00:00:00Z
            last_used: 2026-06-17T00:00:00Z
            source: extract
            weight: 1.0
            reaction: ""
            ---
            invalid entry with empty reaction
            """
        )
    )
    summ = ScriptedSummarizer()  # nothing similar/stale
    # Must NOT raise EntryError from the final save_atomic.
    log = meditate("emotional", "ctx", MISSION, summ, bs.cfg, bs)
    survivors = bs.load("emotional")
    # The invalid entry never reached meditation; good entries persisted.
    assert not any(e.id == "emoinvalid" for e in survivors)
    assert all(e.reaction.strip() for e in survivors)
    assert log.skipped_no_op is False


def test_keep_rank_corrupt_timestamp_sinks_to_bottom(tmp_path: Path) -> None:
    """A corrupt last_used yields a -inf recency, so the entry sorts to the
    very bottom of the keep-rank (forgotten first), never raising."""
    bs = _make_store(tmp_path)
    corrupt = _emo(0.0, "x", last_used="garbage")
    fresh = _emo(0.0, "y", last_used=NOW)
    ranked = sorted([fresh, corrupt], key=lambda e: keep_rank(e, NOW, bs.cfg))
    assert ranked[0] is corrupt  # corrupt-timestamp entry is the first to go

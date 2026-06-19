"""Tests for the meditation engine (meditation.py, design §5; plan §2 dev-2).

Runs entirely under a scripted mock summarizer (plan §5b) — zero live-model
calls. Covers the ordered recipe, the relevance-downgrade-before-forget
ordering, the terminal "nothing safe to forget" path, idempotent no-op,
hysteresis (no meditation in the [max, overflow) band), merge clamping, and
compaction.
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from tigerharness.tiger_memory.bounded_store import BoundedStore, StoreLockHeld
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.entries import (
    KIND_DECISION,
    KIND_OWNER_EXPLICIT,
    KIND_PREFERENCE,
    DiaryEntry,
    MustRememberEntry,
    SkillEntry,
)
from tigerharness.tiger_memory.meditation import (
    MeditationLog,
    keep_rank,
    meditate,
)
from tigerharness.tiger_memory.store import Store
from tigerharness.tiger_memory.summarizers.base import Summarizer

NOW = "2026-06-17T00:00:00Z"
OLD = "2025-01-01T00:00:00Z"
MISSION = "Ship the bounded memory revamp."


# ----- scripted mock summarizer (plan §5b) ----------------------------------


class ScriptedSummarizer(Summarizer):
    """A deterministic summarizer driven by scripted verdicts (no model).

    - ``similar_pairs``: set of frozenset({textA, textB}) the merge step should
      treat as near-duplicates (everything else -> NO).
    - ``stale_texts``: set of directive texts the relevance step should judge
      stale (-> downgrade); everything else -> still relevant.
    - ``compact_map``: text -> shorter rewrite for the compact step.
    - ``raw_override``: if set, returned verbatim (to test tolerant parsing).
    """

    name = "scripted"
    version = "v1"

    def __init__(
        self,
        *,
        similar_pairs=(),
        stale_texts=(),
        compact_map=None,
        raw_override=None,
    ) -> None:
        super().__init__()
        self.similar_pairs = {frozenset(p) for p in similar_pairs}
        self.stale_texts = set(stale_texts)
        self.compact_map = dict(compact_map or {})
        self.raw_override = raw_override
        self.calls: list[str] = []

    def summarize(self, *, prompt: str, max_words: int) -> str:
        self.calls.append(prompt)
        if self.raw_override is not None:
            return self.raw_override
        if prompt.startswith("Are these two memory entries"):
            return self._judge_similarity(prompt)
        if "STALE" in prompt:
            return self._judge_stale(prompt)
        # compact prompt
        body = prompt.split("Return ONLY the rewritten text.\n\n", 1)[1].rstrip("\n")
        return self.compact_map.get(body, body)

    def _judge_similarity(self, prompt: str) -> str:
        a = prompt.split("ENTRY A:\n", 1)[1].split("\n\nENTRY B:", 1)[0]
        b = prompt.split("ENTRY B:\n", 1)[1].rstrip("\n")
        return "YES" if frozenset({a, b}) in self.similar_pairs else "NO"

    def _judge_stale(self, prompt: str) -> str:
        directive = prompt.split("DIRECTIVE:\n", 1)[1].rstrip("\n")
        return "YES" if directive in self.stale_texts else "NO"


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
          diary:
            max_length: {memory.get('emo_max', 30)}
            overflow_limit: {memory.get('emo_overflow', 50)}
            weight_cap: 10
        """
    )
    p = tmp_path / "cfg.yaml"
    p.write_text(
        dedent(
            f"""\
            agent:
              name: Rukawa
              role: r
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


def _mr(kind: str, text: str, last_used: str = NOW, imp: float = 0.0):
    return MustRememberEntry(
        text=text, created_at=NOW, last_used=last_used, source="pin",
        kind=kind, importance=imp,
    )


def _emo(weight: float, text: str, last_used: str = NOW):
    return DiaryEntry(
        text=text, created_at=NOW, last_used=last_used, source="extract",
        weight=weight, reaction="r",
    )


def _skill(name: str, usage: int = 0, last_used: str = NOW):
    return SkillEntry(
        text="b", created_at=NOW, last_used=last_used, source="extract",
        name=name, trigger="t", procedure="p", usage_count=usage,
    )


# ----- idempotent no-op + hysteresis ----------------------------------------


def test_under_max_is_noop(tmp_path: Path) -> None:
    bs = _make_store(tmp_path, mr_max=100, mr_overflow=200)
    bs.save_atomic("must_remember", [_mr(KIND_PREFERENCE, "short")])
    summ = ScriptedSummarizer()
    log = meditate("must_remember", "ctx", MISSION, summ, bs.cfg, bs)
    assert log.skipped_no_op is True
    assert log.changed is False
    assert summ.calls == []  # no LLM calls in a no-op


def test_in_hysteresis_band_is_noop(tmp_path: Path) -> None:
    """A store in [max, overflow) is over max-target only after the caller
    fires it; meditation still treats already-under-max as a no-op, but a
    store the caller fired in-band (over max) compacts. Here we assert the
    pure no-op when under max even if the caller fired it."""
    bs = _make_store(tmp_path, mr_max=50, mr_overflow=80)
    bs.save_atomic("must_remember", [_mr(KIND_PREFERENCE, "x" * 40)])
    summ = ScriptedSummarizer()
    log = meditate("must_remember", "ctx", MISSION, summ, bs.cfg, bs)
    # 40 chars <= max(50) -> no-op.
    assert log.skipped_no_op is True


# ----- merge (raises survivor scalar, clamped) ------------------------------


def test_merge_emotional_raises_magnitude_clamped(tmp_path: Path) -> None:
    # Two 25-char entries (50 > max 40) merge into one ~25-char survivor
    # (< max 40) so forget never runs — isolating the merge+clamp behavior.
    # The bodies share content words ("loved the clean api ...") so the QI-2
    # prefilter lets the pair reach the (scripted) summarizer — a realistic
    # near-duplicate always shares tokens.
    bs = _make_store(tmp_path, emo_max=40, emo_overflow=50)
    a = _emo(8.0, "loved the clean api design")
    b = _emo(7.0, "loved that clean api so much")
    bs.save_atomic("diary", [a, b])
    summ = ScriptedSummarizer(similar_pairs=[(a.text, b.text)])
    log = meditate("diary", "ctx", MISSION, summ, bs.cfg, bs)
    assert b.id in log.merged and log.merged[b.id] == a.id
    assert log.forgotten == []  # survivor fits; no forget
    survivors = bs.load("diary")
    assert len(survivors) == 1
    # merged magnitude (8+7=15) clamps to the cap of 10.
    assert survivors[0].weight == 10.0


def test_merge_skills_accrues_usage(tmp_path: Path) -> None:
    bs = _make_store(tmp_path, skills_max=1, skills_overflow=2)
    a = _skill("Alpha", usage=2)
    b = _skill("Beta", usage=3)
    bs.save_atomic("skills", [a, b])
    summ = ScriptedSummarizer(similar_pairs=[(a.text, b.text)])
    log = meditate("skills", "ctx", MISSION, summ, bs.cfg, bs)
    survivors = bs.load("skills")
    assert len(survivors) == 1
    # usage 2 + (3 + 1) = 6, importance re-derived (>0).
    assert survivors[0].usage_count == 6
    assert survivors[0].importance > 0.0
    assert b.id in log.merged


def test_merge_must_remember_bumps_importance(tmp_path: Path) -> None:
    # max sized so the single merged survivor fits (isolate merge bump).
    bs = _make_store(tmp_path, mr_max=40, mr_overflow=50)
    a = _mr(KIND_PREFERENCE, "use tabs not spaces here ok", imp=1.0)
    b = _mr(KIND_PREFERENCE, "tabs over spaces please now")
    bs.save_atomic("must_remember", [a, b])
    summ = ScriptedSummarizer(similar_pairs=[(a.text, b.text)])
    log = meditate("must_remember", "ctx", MISSION, summ, bs.cfg, bs)
    assert log.forgotten == []
    survivors = bs.load("must_remember")
    assert len(survivors) == 1
    assert survivors[0].importance == 2.0  # 1.0 + 1.0 bump


# ----- relevance-check + downgrade BEFORE forget ----------------------------


def test_relevance_downgrades_stale_owner_directive(tmp_path: Path) -> None:
    bs = _make_store(tmp_path, mr_max=20, mr_overflow=30)
    stale = _mr(KIND_OWNER_EXPLICIT, "old retired feature directive xx", OLD)
    bs.save_atomic("must_remember", [stale])
    summ = ScriptedSummarizer(stale_texts=[stale.text])
    log = meditate("must_remember", "ctx", MISSION, summ, bs.cfg, bs)
    assert stale.id in log.downgraded
    survivors = bs.load("must_remember")
    # Downgraded to a normal kind and then forgotten (it was the lowest-rank).
    assert all(e.kind != KIND_OWNER_EXPLICIT for e in survivors)


def test_relevance_keeps_relevant_owner_then_terminal_overmax(
    tmp_path: Path,
) -> None:
    """A store of all still-relevant owner directives over max stays intact
    and emits the over-max warning (terminal nothing-safe-to-forget)."""
    bs = _make_store(tmp_path, mr_max=20, mr_overflow=30)
    o1 = _mr(KIND_OWNER_EXPLICIT, "ship the revamp by friday hard")
    o2 = _mr(KIND_OWNER_EXPLICIT, "never push to remote without approval")
    bs.save_atomic("must_remember", [o1, o2])
    # No similar pairs, none stale -> nothing merges, nothing downgrades.
    summ = ScriptedSummarizer()
    log = meditate("must_remember", "ctx", MISSION, summ, bs.cfg, bs)
    assert log.over_max is True
    assert log.forgotten == []
    survivors = bs.load("must_remember")
    # Both still present, both still owner_explicit (nothing force-dropped).
    assert len(survivors) == 2
    assert all(e.kind == KIND_OWNER_EXPLICIT for e in survivors)


def test_ordering_relevance_before_forget_allows_downgraded_drop(
    tmp_path: Path,
) -> None:
    """A stale owner directive is downgraded (step 2) then forgotten (step 4)
    without the forget-guard tripping — proves the ordering invariant."""
    bs = _make_store(tmp_path, mr_max=15, mr_overflow=25)
    keep = _mr(KIND_PREFERENCE, "keep me short", NOW, imp=5.0)
    stale = _mr(KIND_OWNER_EXPLICIT, "stale directive to be dropped", OLD)
    bs.save_atomic("must_remember", [keep, stale])
    summ = ScriptedSummarizer(stale_texts=[stale.text])
    log = meditate("must_remember", "ctx", MISSION, summ, bs.cfg, bs)
    assert stale.id in log.downgraded
    assert stale.id in log.forgotten  # downgraded THEN dropped, no guard error
    survivors = bs.load("must_remember")
    assert [e.id for e in survivors] == [keep.id]


# ----- compaction -----------------------------------------------------------


def test_compact_shortens_verbose_survivor(tmp_path: Path) -> None:
    bs = _make_store(tmp_path, emo_max=20, emo_overflow=30)
    long = _emo(9.0, "x" * 40)  # over max, strong feeling (kept)
    bs.save_atomic("diary", [long])
    summ = ScriptedSummarizer(compact_map={"x" * 40: "short"})
    log = meditate("diary", "ctx", MISSION, summ, bs.cfg, bs)
    assert long.id in log.compacted
    survivors = bs.load("diary")
    assert survivors[0].text == "short"
    assert log.over_max is False


def test_compact_rejected_when_not_shorter(tmp_path: Path) -> None:
    bs = _make_store(tmp_path, emo_max=20, emo_overflow=30)
    long = _emo(9.0, "y" * 40)
    bs.save_atomic("diary", [long])
    # compact returns same text -> not accepted; falls through to forget.
    summ = ScriptedSummarizer(compact_map={})  # echoes body unchanged
    log = meditate("diary", "ctx", MISSION, summ, bs.cfg, bs)
    assert long.id not in log.compacted
    # Single strong entry can't be forgotten below max either (only entry):
    # it is dropped because emotional has no guard -> store empties.
    assert long.id in log.forgotten


def test_skills_store_skips_compaction(tmp_path: Path) -> None:
    """Skills are count-bounded: compaction is skipped (no LLM compact calls)."""
    bs = _make_store(tmp_path, skills_max=1, skills_overflow=2)
    a = _skill("Alpha", usage=1)
    b = _skill("Beta", usage=10)
    bs.save_atomic("skills", [a, b])
    summ = ScriptedSummarizer()  # nothing similar
    log = meditate("skills", "ctx", MISSION, summ, bs.cfg, bs)
    assert log.compacted == []
    survivors = bs.load("skills")
    # count over max(1) -> forget the least-used (Alpha).
    assert len(survivors) == 1 and survivors[0].name == "Beta"


# ----- forget ordering (lowest keep-rank first) -----------------------------


def test_forget_drops_lowest_ranked_first(tmp_path: Path) -> None:
    bs = _make_store(tmp_path, emo_max=20, emo_overflow=30)
    weak = _emo(0.5, "a" * 15)
    strong = _emo(9.0, "b" * 15)
    bs.save_atomic("diary", [weak, strong])  # 30 chars > max 20
    summ = ScriptedSummarizer()
    log = meditate("diary", "ctx", MISSION, summ, bs.cfg, bs)
    survivors = bs.load("diary")
    assert weak.id in log.forgotten
    assert [e.id for e in survivors] == [strong.id]


# ----- QI-2: merge prefilter keeps summarizer calls sub-quadratic -----------


def _similarity_calls(summ: ScriptedSummarizer) -> int:
    """Count only the merge-similarity judgement prompts (QI-2's hot path)."""
    return sum(
        1 for p in summ.calls if p.startswith("Are these two memory entries")
    )


# A bank of genuinely-varied, semantically-distinct prose entries. Each is a
# different feeling about a different subject, the way a real persona's
# emotional store reads — they share only function words (the/a/i/to/was/...),
# never a subject word. The realistic workload QI-3 is about: the OLD gate
# (raw-token overlap, no stopword stripping) matched almost every pair on those
# stopwords and stayed O(n²); the QI-3 gate strips stopwords and gates them out.
_PROSE_BANK = [
    "felt proud when the deploy pipeline finally went green after days of red",
    "was annoyed the standup ran twenty minutes over again this morning",
    "loved how the new caching layer cut page loads almost in half",
    "worried the schema migration would lock the table during peak hours",
    "grateful a teammate spotted the off-by-one before it reached customers",
    "frustrated by a flaky test that only fails on remote runners not here",
    "relieved the rollback script worked exactly as documented under pressure",
    "excited about moving search onto a proper inverted index next quarter",
    "tired of chasing memory leaks in the long running worker process",
    "happy the docs finally explain how to rotate the signing keys safely",
    "uneasy about how much config now lives in environment variables",
    "proud the rate limiter held during the unexpected traffic surge today",
    "irritated that the linter and formatter disagree on import ordering",
    "thankful the on call runbook covered the exact failure we hit at dawn",
    "curious whether batching the webhook deliveries would reduce server load",
    "disappointed the demo crashed on a path nobody had exercised before",
    "satisfied after pairing to untangle the gnarly retry backoff logic",
    "anxious the backup job silently skipped a database for a whole week",
    "delighted a small refactor made the parser roughly twice as fast",
    "concerned the alerting is so noisy that people ignore real pages now",
    "glad we finally deleted the dead feature flag and all its old branches",
    "stressed the release froze waiting on a slow dependency security audit",
    "amused the bug turned out to be a timezone offset by one single hour",
    "motivated after reading how another team structures their large monorepo",
    "embarrassed the typo in the email template went out to every subscriber",
]


def _raw_token_overlap_calls(texts: list[str]) -> int:
    """Pairs the OLD prefilter (raw tokens, NO stopword stripping) would pass.

    Models the pre-QI-3 ``_might_be_similar``: any shared lowercase
    alphanumeric run (stopwords included) → the pair reaches the LLM. This is
    the regression baseline the new test must beat.
    """
    import re

    word_re = re.compile(r"[0-9a-z]+")
    raw = [frozenset(word_re.findall(t.lower())) for t in texts]
    return sum(
        1
        for i in range(len(raw))
        for j in range(i + 1, len(raw))
        if raw[i] & raw[j]
    )


def test_merge_prefilter_subquadratic_on_prose_sharing_stopwords(
    tmp_path: Path,
) -> None:
    """QI-3: realistic prose sharing stopwords must NOT cost O(n²) LLM calls.

    The entries are genuinely-distinct prose (different feeling about a
    different subject) that share only function words like ``the``/``a``/``i``/
    ``to``. This is the workload the old all-token prefilter FAILED to gate:
    every pair shared a stopword, so it deferred every pair to the model and
    stayed quadratic. The QI-3 stopword-stripping gate keys on subject words,
    so these pairs are gated out and the similarity-judgement call count is
    sub-quadratic (<= c*n), FAR under the naive n·(n−1)/2.

    Hard guard against the regression: we assert the count is well below what
    the OLD raw-token gate would have produced on this very data — so this test
    would FAIL against the pre-QI-3 prefilter (which gated nothing here).
    """
    n = len(_PROSE_BANK)  # 25
    bs = _make_store(tmp_path, emo_max=40, emo_overflow=50)
    entries = [_emo(1.0 + i * 0.1, _PROSE_BANK[i]) for i in range(n)]
    bs.save_atomic("diary", entries)
    summ = ScriptedSummarizer()  # none scripted similar; gate decides call count
    meditate("diary", "ctx", MISSION, summ, bs.cfg, bs)

    quadratic = n * (n - 1) // 2  # 300 — the naive pairwise loop
    old_gate_calls = _raw_token_overlap_calls(_PROSE_BANK)
    calls = _similarity_calls(summ)

    # The OLD prefilter would have passed almost every pair on shared stopwords
    # (proving the regression existed on this data) ...
    assert old_gate_calls > quadratic // 2  # >150 of 300 — effectively quadratic
    # ... while the QI-3 gate keeps it sub-quadratic and far below the old gate.
    assert calls <= 2 * n  # <= 50: bounded by c*n, not n²
    assert calls < old_gate_calls // 2  # decisively beats the pre-QI-3 gate


def test_merge_prefilter_disjoint_tokens_gates_all(tmp_path: Path) -> None:
    """Token-disjoint entries (no shared content word at all) cost ~0 LLM calls
    — the prefilter's best case, preserved from QI-2."""
    n = 20
    bs = _make_store(tmp_path, emo_max=40, emo_overflow=50)
    entries = [_emo(1.0 + i * 0.1, f"alpha{i}word") for i in range(n)]
    bs.save_atomic("diary", entries)
    summ = ScriptedSummarizer()
    meditate("diary", "ctx", MISSION, summ, bs.cfg, bs)

    quadratic = n * (n - 1) // 2  # 190
    calls = _similarity_calls(summ)
    assert calls == 0  # all pairs gated (zero shared tokens)
    assert calls < quadratic


def test_merge_prefilter_lets_similar_pairs_merge(tmp_path: Path) -> None:
    """The prefilter must not block a genuine merge: two entries sharing
    content words reach the summarizer and (when it judges YES) still merge."""
    bs = _make_store(tmp_path, emo_max=40, emo_overflow=50)
    a = _emo(8.0, "shipped the bounded memory revamp")
    b = _emo(7.0, "bounded memory revamp shipped today")
    bs.save_atomic("diary", [a, b])
    summ = ScriptedSummarizer(similar_pairs=[(a.text, b.text)])
    log = meditate("diary", "ctx", MISSION, summ, bs.cfg, bs)
    # The shared words ("bounded memory revamp shipped") passed the gate, the
    # scripted summarizer judged YES, so b folded into a (and clamped to cap).
    assert b.id in log.merged and log.merged[b.id] == a.id
    assert _similarity_calls(summ) >= 1  # the pair DID reach the LLM
    survivors = bs.load("diary")
    assert len(survivors) == 1 and survivors[0].weight == 10.0


def test_merge_similar_prose_sharing_stopwords_still_merges(
    tmp_path: Path,
) -> None:
    """No over-gating (QI-3): two genuinely-similar prose entries that share
    BOTH stopwords AND subject words must still pass the gate and merge.

    This is the safety direction of the QI-3 fix: stripping stopwords must not
    gate out a real near-duplicate. Both entries are about the same deploy
    going green; they share the content words ``deploy``/``green`` (plus
    stopwords), so the gate defers them to the LLM, which judges YES and merges.
    """
    bs = _make_store(tmp_path, emo_max=40, emo_overflow=50)
    a = _emo(8.0, "the deploy finally went green")
    b = _emo(7.0, "so glad the deploy was green")
    bs.save_atomic("diary", [a, b])
    summ = ScriptedSummarizer(similar_pairs=[(a.text, b.text)])
    log = meditate("diary", "ctx", MISSION, summ, bs.cfg, bs)
    # Shared content words (deploy/green) passed the gate despite the entries
    # also sharing stopwords; the scripted summarizer judged YES, so b merged.
    assert b.id in log.merged and log.merged[b.id] == a.id
    assert _similarity_calls(summ) >= 1  # the pair DID reach the LLM
    survivors = bs.load("diary")
    assert len(survivors) == 1 and survivors[0].weight == 10.0


def test_might_be_similar_gate_unit(tmp_path: Path) -> None:
    """Unit-cover the gate: no shared CONTENT token -> skip; shared content
    token -> defer; shared only-stopwords -> skip (QI-3); an entry with no
    content tokens (all stopwords / punctuation) -> defer (never silently
    gated out)."""
    from tigerharness.tiger_memory.meditation import (
        _might_be_similar,
        _norm_tokens,
    )

    share = _emo(1.0, "loved the clean api")
    other = _emo(1.0, "loved that clean api too")
    disjoint = _emo(1.0, "hated verbose yaml configs")
    stopword_only = _emo(1.0, "i was at the desk")  # only stopwords overlap
    all_stop = _emo(1.0, "i was at the and to")  # no content tokens at all
    empty_tokens = _emo(1.0, "!!! ??? ...")  # punctuation-only -> no tokens

    # Stopwords are stripped: "the" in `share` and `stopword_only` does NOT
    # count as shared content.
    assert _norm_tokens(stopword_only.text) == frozenset({"desk"})
    assert _norm_tokens(all_stop.text) == frozenset()  # all stripped

    assert _might_be_similar(share, other) is True   # share "loved/clean/api"
    assert _might_be_similar(share, disjoint) is False  # zero shared content
    assert _might_be_similar(share, stopword_only) is False  # only "the" shared
    assert _might_be_similar(all_stop, share) is True  # no content -> defer
    assert _might_be_similar(empty_tokens, share) is True  # defer, don't gate
    assert _might_be_similar(share, empty_tokens) is True  # symmetric defer


# ----- lock backoff ---------------------------------------------------------


def test_meditate_backs_off_when_locked(tmp_path: Path) -> None:
    import os

    bs = _make_store(tmp_path, mr_max=20, mr_overflow=30)
    bs.save_atomic("must_remember", [_mr(KIND_PREFERENCE, "x" * 40)])
    lock_file = bs.store.paths.journal / ".must_remember.lock"
    lock_file.write_text(f"{os.getpid()} 0")  # live holder
    summ = ScriptedSummarizer()
    with pytest.raises(StoreLockHeld):
        meditate("must_remember", "ctx", MISSION, summ, bs.cfg, bs)
    lock_file.unlink()


# ----- keep_rank dispatch ---------------------------------------------------


def test_keep_rank_dispatch_by_store(tmp_path: Path) -> None:
    bs = _make_store(tmp_path)
    cfg = bs.cfg
    e = _emo(5.0, "feeling")
    s = _skill("Sk", usage=3)
    m = _mr(KIND_PREFERENCE, "memo", imp=2.0)
    assert keep_rank(e, NOW, cfg)[0] == 5.0
    assert keep_rank(s, NOW, cfg)[0] > 0.0
    assert keep_rank(m, NOW, cfg)[0] == 2.0


# ----- tolerant verdict parsing ---------------------------------------------


def test_unparseable_similarity_verdict_defaults_to_not_similar(
    tmp_path: Path,
) -> None:
    """A backend that returns neither YES nor NO -> safe default (not similar)."""
    bs = _make_store(tmp_path, emo_max=20, emo_overflow=30)
    a = _emo(9.0, "c" * 15)
    b = _emo(8.0, "d" * 15)
    bs.save_atomic("diary", [a, b])
    summ = ScriptedSummarizer(raw_override="I am not sure about that")
    log = meditate("diary", "ctx", MISSION, summ, bs.cfg, bs)
    # Nothing merged (default NO); compaction echoes body (raw_override is the
    # same string but longer-or-equal so not accepted) -> falls to forget.
    assert log.merged == {}


def test_meditation_log_changed_flag() -> None:
    log = MeditationLog(store_name="diary")
    assert log.changed is False
    log.forgotten.append("x")
    assert log.changed is True


# ----- internal helpers: _ask_yes_no tolerant parsing -----------------------


def test_ask_yes_no_neither_token_returns_default() -> None:
    from tigerharness.tiger_memory.meditation import _ask_yes_no

    summ = ScriptedSummarizer(raw_override="hmm, unclear")  # no YES, no NO
    assert _ask_yes_no(summ, "p", default=True) is True
    assert _ask_yes_no(summ, "p", default=False) is False


def test_ask_yes_no_both_tokens_earliest_wins() -> None:
    from tigerharness.tiger_memory.meditation import _ask_yes_no

    yes_first = ScriptedSummarizer(raw_override="YES, definitely NOT otherwise")
    assert _ask_yes_no(yes_first, "p", default=False) is True
    no_first = ScriptedSummarizer(raw_override="NO, this is not a YES case")
    assert _ask_yes_no(no_first, "p", default=True) is False


def test_ask_yes_no_only_yes_or_only_no() -> None:
    from tigerharness.tiger_memory.meditation import _ask_yes_no

    assert _ask_yes_no(
        ScriptedSummarizer(raw_override="affirmative YES"), "p", default=False
    ) is True
    # "NO" alone (avoid words containing it) -> False.
    assert _ask_yes_no(
        ScriptedSummarizer(raw_override="answer: NO"), "p", default=True
    ) is False


# ----- internal helpers: _absorb_emotional sign-follows-larger --------------


def test_absorb_emotional_dropped_larger_positive(tmp_path: Path) -> None:
    from tigerharness.tiger_memory.meditation import _absorb_emotional

    bs = _make_store(tmp_path)
    target = _emo(2.0, "t")
    dropped = _emo(6.0, "d")  # larger magnitude, positive
    _absorb_emotional(target, dropped, bs.cfg)
    # sign follows dropped (positive); magnitude = max(2,6,8)=8.
    assert target.weight == 8.0


def test_absorb_emotional_dropped_larger_negative(tmp_path: Path) -> None:
    from tigerharness.tiger_memory.meditation import _absorb_emotional

    bs = _make_store(tmp_path)
    target = _emo(2.0, "t")
    dropped = _emo(-6.0, "d")  # larger magnitude, negative
    _absorb_emotional(target, dropped, bs.cfg)
    # sign follows dropped (negative); magnitude = max(2,6,|2-6|=4)=6.
    assert target.weight == -6.0


def test_absorb_emotional_target_larger_negative(tmp_path: Path) -> None:
    from tigerharness.tiger_memory.meditation import _absorb_emotional

    bs = _make_store(tmp_path)
    target = _emo(-7.0, "t")  # larger magnitude, negative
    dropped = _emo(3.0, "d")
    _absorb_emotional(target, dropped, bs.cfg)
    assert target.weight == -7.0  # max(7,3,|-4|)=7, sign from target (neg)

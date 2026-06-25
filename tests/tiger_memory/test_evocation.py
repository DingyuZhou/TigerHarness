"""Tests for the associative-evocation pass (evocation.py) — b1-dev-2 (Miyagi).

Drives the pass with a controllable StubEvocationSummarizer (the default mock
yields no evocations). Covers 0/1/2-evocation, cross-store evocation, the diary
weight+recency reinforcement, the count bumps, the persisted concise reference,
the >2 clamp, the unlocatable-index and zero-new-notes error paths, and the
no-NOTE-lines (mock) determinism.
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import pytest

from tigerharness.tiger_memory.bounded_store import BoundedStore
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.entries import (
    STORE_DIARY,
    STORE_MUST_REMEMBER,
    STORE_SKILLS,
    DiaryEntry,
    MustRememberEntry,
    SkillEntry,
)
from tigerharness.tiger_memory.evocation import (
    _parse_response,
    _split_new_diary,
    evoke_and_reinforce,
)
from tigerharness.tiger_memory.store import Store
from tigerharness.tiger_memory.summarizers.base import Summarizer

NOW = "2026-06-22T12:00:00Z"


class StubEvocationSummarizer(Summarizer):
    """Returns a fixed crafted evocation reply; records that it was called."""
    name = "stub-evocation"
    version = "v1"

    def __init__(self, response: str):
        self._response = response
        self.calls = 0

    def summarize(self, *, prompt: str, max_words: int) -> str:
        self.calls += 1
        return self._response


class RaisingSummarizer(Summarizer):
    """Fails the test if summarize() is ever called (proves the no-call paths)."""
    name = "raising"
    version = "v1"

    def summarize(self, *, prompt: str, max_words: int) -> str:  # pragma: no cover
        raise AssertionError("summarize must not be called")


def _store(tmp_path: Path):
    p = tmp_path / "cfg.yaml"
    p.write_text(dedent(f"""\
        agent: {{name: Anzai, role: r}}
        store: {{root: {tmp_path}/m}}
        sources: [{{kind: claude_code, project_path: {tmp_path}/p/}}]
        summarizer: {{backend: anthropic, model: m, prompts: default/v1}}
        memory:
          diary: {{max_length: 6000, overflow_limit: 8000, evocation_enabled: true}}
    """))
    cfg = load_config(p)
    store = Store(cfg.store.root)
    store.init_layout()
    return cfg, store, BoundedStore(cfg, store)


def _d(text, weight, day):
    ts = f"2026-06-{day:02d}T00:00:00Z"
    return DiaryEntry(id=f"d{day}", text=text, created_at=ts, last_used=ts,
                      source="diary", weight=weight)


def _m(text, repeat_count=1):
    e = MustRememberEntry(id="m1", text=text, created_at="2026-06-10T00:00:00Z",
                          last_used="2026-06-10T00:00:00Z", source="s",
                          kind="preference")
    e.repeat_count = repeat_count
    return e


def _s(name, usage_count=1):
    return SkillEntry(id="s1", text="t", created_at="2026-06-10T00:00:00Z",
                      last_used="2026-06-10T00:00:00Z", source="s", name=name,
                      trigger="trig", procedure="proc", usage_count=usage_count)


def _seed(bstore, *, diary, must=(), skills=()):
    bstore.save_atomic(STORE_DIARY, list(diary))
    bstore.save_atomic(STORE_MUST_REMEMBER, list(must))
    bstore.save_atomic(STORE_SKILLS, list(skills))


def _cands(diary=(), skills=(), must=()):
    return SimpleNamespace(diary=list(diary), skills=list(skills),
                           must_remember=list(must))


def test_cross_store_evocation_reinforces_and_references(tmp_path: Path):
    cfg, store, bs = _store(tmp_path)
    d1 = _d("old diary one", 2.0, 10)
    d2 = _d("old diary two", -3.0, 11)
    new = _d("new event happened", 1.0, 22)
    m1 = _m("operator prefers -F over -m")
    s1 = _s("commit via -F")
    _seed(bs, diary=[d1, d2, new], must=[m1], skills=[s1])
    # context = [d1, d2, m1, s1] -> evoke the must_remember (2) and skill (3)
    summ = StubEvocationSummarizer("NOTE 0: 2, 3")
    log = evoke_and_reinforce(bs, cfg, _cands(diary=[new]), summ, now=NOW)

    assert summ.calls == 1
    assert log.referenced == 1
    must = bs.load(STORE_MUST_REMEMBER)
    assert must[0].repeat_count == 2          # count bump
    skills = bs.load(STORE_SKILLS)
    assert skills[0].usage_count == 2          # count bump
    diary = bs.load(STORE_DIARY)
    note = next(e for e in diary if e.text.startswith("new event happened"))
    assert "recalls:" in note.text
    assert 'skill "commit via -F"' in note.text
    assert "must_remember/preference" in note.text


def test_diary_target_reinforced_weight_and_recency(tmp_path: Path):
    cfg, store, bs = _store(tmp_path)
    d1 = _d("a strong old diary memory", 2.0, 10)
    new = _d("new note", 0.0, 22)
    _seed(bs, diary=[d1, new])
    summ = StubEvocationSummarizer("NOTE 0: 0")   # evoke d1
    evoke_and_reinforce(bs, cfg, _cands(diary=[new]), summ, now=NOW)
    diary = bs.load(STORE_DIARY)
    d1r = next(e for e in diary if e.text == "a strong old diary memory")
    assert d1r.weight == 3.0                       # +1 toward +
    assert d1r.last_used[:10] == "2026-06-22"      # re-dated to the evoking day


def test_zero_evocations_leaves_everything_unchanged(tmp_path: Path):
    cfg, store, bs = _store(tmp_path)
    d1 = _d("old", 2.0, 10)
    new = _d("new", 1.0, 22)
    _seed(bs, diary=[d1, new])
    summ = StubEvocationSummarizer("NOTE 0: NONE")
    log = evoke_and_reinforce(bs, cfg, _cands(diary=[new]), summ, now=NOW)
    assert log.referenced == 0
    diary = bs.load(STORE_DIARY)
    assert {e.text for e in diary} == {"old", "new"}   # no reference appended


def test_more_than_two_targets_clamped(tmp_path: Path):
    cfg, store, bs = _store(tmp_path)
    d1, d2, d3 = _d("o1", 1.0, 10), _d("o2", 1.0, 11), _d("o3", 1.0, 12)
    new = _d("new", 1.0, 22)
    _seed(bs, diary=[d1, d2, d3, new])
    summ = StubEvocationSummarizer("NOTE 0: 0, 1, 2")    # 3 -> clamp to 2
    log = evoke_and_reinforce(bs, cfg, _cands(diary=[new]), summ, now=NOW)
    assert len(log.reinforced) == 2
    diary = bs.load(STORE_DIARY)
    o3 = next(e for e in diary if e.text == "o3")
    assert o3.weight == 1.0                              # 3rd target NOT bumped


def test_unlocatable_index_skipped_no_crash(tmp_path: Path):
    cfg, store, bs = _store(tmp_path)
    d1 = _d("old", 2.0, 10)
    new = _d("new", 1.0, 22)
    _seed(bs, diary=[d1, new])
    summ = StubEvocationSummarizer("NOTE 0: 99")          # out of range
    log = evoke_and_reinforce(bs, cfg, _cands(diary=[new]), summ, now=NOW)
    assert log.reinforced == []
    diary = bs.load(STORE_DIARY)
    assert next(e for e in diary if e.text == "old").weight == 2.0


def test_no_new_diary_notes_skips_model_call(tmp_path: Path):
    cfg, store, bs = _store(tmp_path)
    _seed(bs, diary=[_d("old", 1.0, 10)])
    assert evoke_and_reinforce(bs, cfg, _cands(diary=[]), RaisingSummarizer(),
                               now=NOW) is None


def test_empty_context_skips_model_call(tmp_path: Path):
    cfg, store, bs = _store(tmp_path)
    new = _d("only new", 1.0, 22)
    _seed(bs, diary=[new])                                # nothing old to evoke
    assert evoke_and_reinforce(bs, cfg, _cands(diary=[new]), RaisingSummarizer(),
                               now=NOW) is None


def test_no_note_lines_yields_no_evocations(tmp_path: Path):
    cfg, store, bs = _store(tmp_path)
    d1 = _d("old", 2.0, 10)
    new = _d("new", 1.0, 22)
    _seed(bs, diary=[d1, new])
    summ = StubEvocationSummarizer("a long fuzzy summary, no structured reply")
    log = evoke_and_reinforce(bs, cfg, _cands(diary=[new]), summ, now=NOW)
    assert summ.calls == 1
    assert log.referenced == 0


def test_long_context_text_snippeted_in_prompt(tmp_path: Path):
    cfg, store, bs = _store(tmp_path)
    d1 = _d("x" * 300, 2.0, 10)                           # exercises _snippet cut
    new = _d("new", 1.0, 22)
    _seed(bs, diary=[d1, new])
    summ = StubEvocationSummarizer("NOTE 0: NONE")
    evoke_and_reinforce(bs, cfg, _cands(diary=[new]), summ, now=NOW)
    assert summ.calls == 1


def test_split_new_diary_claims_distinct_twins():
    """Two new candidates with an identical (text, weight, day) signature claim
    two distinct loaded entries — the second skips the already-claimed first."""
    a, b = _d("dup", 1.0, 22), _d("dup", 1.0, 22)
    new1, new2 = _d("dup", 1.0, 22), _d("dup", 1.0, 22)
    unmatchable = _d("no such loaded entry", 9.0, 1)   # matches nothing -> skipped
    new_loaded, old_loaded = _split_new_diary([a, b], [new1, new2, unmatchable])
    assert len(new_loaded) == 2
    assert old_loaded == []


def test_parse_response_out_of_range_note_index_ignored():
    assert _parse_response("NOTE 9: 0", n_notes=1, n_context=3) == {}


def test_parse_response_non_digit_tokens_skipped():
    assert _parse_response("NOTE 0: foo 1 bar", n_notes=1, n_context=3) == {0: [1]}


def test_new_skill_and_mr_excluded_from_context(tmp_path: Path):
    """A brand-new skill/must (same ingest) is not an evocation target."""
    cfg, store, bs = _store(tmp_path)
    new = _d("new note", 1.0, 22)
    new_skill = _s("brand new skill")
    _seed(bs, diary=[new], skills=[new_skill])
    # candidates include the new skill -> excluded from context -> empty context
    assert evoke_and_reinforce(
        bs, cfg, _cands(diary=[new], skills=[new_skill]),
        RaisingSummarizer(), now=NOW,
    ) is None

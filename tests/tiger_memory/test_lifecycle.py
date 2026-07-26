"""Tests for the extraction lifecycle (lifecycle.py, topic-store revamp ADR 0007).

Covers the extraction-bundle parser (@@SKILLS@@ / @@MUST_REMEMBER@@ /
@@TOPICS@@ marker contract + topic-block grammar), topic routing
(_append_topic_detail / _route_topic_candidates), the in-process
extract→ingest path (scripted summarizer — no live model), the idle/extract
decision, the staging planner (routing list embedded in the prompt), the
fresh-start rebuild, pin → must_remember, mission-text sourcing, and the
clip/stack helpers.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent

import pytest

from tigerharness.tiger_memory import lifecycle as lc
from tigerharness.tiger_memory.bounded_store import BoundedStore
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.entries import (
    KIND_OPERATOR_EXPLICIT,
    KIND_PREFERENCE,
    STORE_MUST_REMEMBER,
    STORE_SKILLS,
    STORE_TOPICS,
    TopicEntry,
)
from tigerharness.tiger_memory.sources.base import SourceRecord
from tigerharness.tiger_memory.store import Store
from tigerharness.tiger_memory.summarizers.base import Summarizer

NOW = "2026-06-17T00:00:00Z"


# ----- scripted extraction summarizer ---------------------------------------


class ScriptedExtractor(Summarizer):
    """Returns a fixed bundle (or raises) — no model, deterministic."""

    name = "scripted-extract"
    version = "v1"

    def __init__(self, bundle: str = "", *, raises: Exception | None = None):
        super().__init__()
        self._bundle = bundle
        self._raises = raises
        self.last_prompt: str | None = None

    def summarize(self, *, prompt: str, max_words: int) -> str:
        self.last_prompt = prompt
        if self._raises is not None:
            raise self._raises
        return self._bundle


def _cfg(tmp_path: Path, extra: str = "") -> object:
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(dedent(f"""\
        agent:
          name: TestTiger
          role: t
        store:
          root: {tmp_path}/memory
        sources:
          - kind: claude_code
            project_path: {tmp_path}/proj/
        summarizer:
          backend: anthropic
          model: m
          prompts: default/v1
        rebuild:
          lock_path: {tmp_path}/lock
    """) + extra)
    return load_config(cfg_path)


def _rec(content: str = "hi", *, activity_mtime: float = 0.0) -> SourceRecord:
    dt = datetime(2026, 6, 1, tzinfo=timezone.utc)
    return SourceRecord(
        conversation_uuid="conv-1", source="claude_code", source_id="sid",
        first_event_at=dt, last_event_at=dt, activity_mtime=activity_mtime,
        content=content, raw_path=Path("/raw"),
    )


def _topic(
    slug: str = "bounded-store",
    *,
    name: str = "Bounded store",
    summary: str = "the bounded-store substrate",
    text: str = "## 2026-06-01\n- first fact",
    touch_count: int = 1,
    last_used: str = NOW,
) -> TopicEntry:
    return TopicEntry(
        text=text, created_at=NOW, last_used=last_used, source="extract",
        name=name, slug=slug, summary=summary, touch_count=touch_count,
    )


_FULL_BUNDLE = dedent("""\
    @@SKILLS@@
    NAME: Bound a markdown store
    TRIGGER: when a store grows unbounded
    PROCEDURE: write one entry per block; rewrite the whole file atomically

    @@MUST_REMEMBER@@
    KIND: operator_explicit
    MEMO: never push without asking

    KIND: preference
    MEMO: targeted git add, never -A

    @@TOPICS@@
    TOPIC: NEW
    NAME: Bounded store revamp
    SUMMARY: three bounded stores replace the rollup lifecycle
    DETAIL: landed the bounded-store substrate clean and green
""")

_EMPTY_BUNDLE = "@@SKILLS@@\nNONE\n@@MUST_REMEMBER@@\nNONE\n@@TOPICS@@\nNONE\n"


# ----- bundle parsing -------------------------------------------------------


def test_parse_full_bundle() -> None:
    c = lc.parse_extraction(_FULL_BUNDLE, now=NOW, source="claude_code")
    assert len(c.skills) == 1
    assert c.skills[0].name == "Bound a markdown store"
    assert c.skills[0].procedure.startswith("write one entry")
    assert [e.kind for e in c.must_remember] == [KIND_OPERATOR_EXPLICIT, KIND_PREFERENCE]
    assert len(c.topics) == 1
    t = c.topics[0]
    assert t.slug == ""  # NEW → slug minted at routing time
    assert t.name == "Bounded store revamp"
    assert t.summary == "three bounded stores replace the rollup lifecycle"
    assert t.detail == "landed the bounded-store substrate clean and green"
    assert not c.is_empty()
    assert c.total() == 4


def test_parse_all_none() -> None:
    c = lc.parse_extraction(_EMPTY_BUNDLE, now=NOW, source="x")
    assert c.is_empty()
    assert c.total() == 0


def test_parse_missing_marker_raises() -> None:
    with pytest.raises(lc.ExtractionParseError, match="missing"):
        lc.parse_extraction("@@SKILLS@@\nNONE\n@@TOPICS@@\nNONE\n", now=NOW, source="x")


def test_parse_empty_raises() -> None:
    with pytest.raises(lc.ExtractionParseError, match="empty"):
        lc.parse_extraction("   ", now=NOW, source="x")


def test_parse_out_of_order_raises() -> None:
    bundle = "@@TOPICS@@\nNONE\n@@SKILLS@@\nNONE\n@@MUST_REMEMBER@@\nNONE\n"
    with pytest.raises(lc.ExtractionParseError, match="out of order"):
        lc.parse_extraction(bundle, now=NOW, source="x")


def test_parse_inline_echoed_marker_does_not_split() -> None:
    # A marker token quoted INSIDE a line (untrusted transcript echo) is not a
    # whole-line marker, so it must not mis-split the bundle.
    bundle = dedent("""\
        @@SKILLS@@
        NONE
        @@MUST_REMEMBER@@
        NONE
        @@TOPICS@@
        TOPIC: NEW
        NAME: Marker echo
        SUMMARY: quoting markers inline is safe
        DETAIL: the transcript quoted @@SKILLS@@ inline and nothing split
    """)
    c = lc.parse_extraction(bundle, now=NOW, source="x")
    assert c.skills == [] and c.must_remember == []
    assert len(c.topics) == 1
    assert "@@SKILLS@@" in c.topics[0].detail


def test_parse_duplicate_standalone_marker_is_malformed() -> None:
    # A second standalone occurrence of any marker makes the split ambiguous
    # (the classic case: the card echoed the prompt's contract sample before
    # its real output — first-wins would drop ALL real content while the
    # cursor advanced). Malformed is the safe verdict: the card is re-asked.
    bundle = dedent("""\
        @@SKILLS@@
        NONE
        @@MUST_REMEMBER@@
        KIND: decision
        MEMO: markers split on first standalone occurrence
        @@TOPICS@@
        NONE
        @@SKILLS@@
    """)
    with pytest.raises(lc.ExtractionParseError, match="duplicate standalone"):
        lc.parse_extraction(bundle, now=NOW, source="x")


def test_parse_contract_echo_bundle_is_malformed() -> None:
    # The exact F3 shape: a card that echoes the whole three-marker contract
    # sample first, then emits the real bundle. First-wins parsing would
    # return zero candidates "successfully" and lose the card silently.
    echo = "@@SKILLS@@\nNONE\n@@MUST_REMEMBER@@\nNONE\n@@TOPICS@@\nNONE\n"
    real = dedent("""\
        @@SKILLS@@
        NONE
        @@MUST_REMEMBER@@
        KIND: operator_explicit
        MEMO: never lose this
        @@TOPICS@@
        NONE
    """)
    with pytest.raises(lc.ExtractionParseError, match="duplicate standalone"):
        lc.parse_extraction(echo + real, now=NOW, source="x")


def test_parse_skips_malformed_blocks() -> None:
    bundle = dedent("""\
        @@SKILLS@@
        NAME: only a name, no trigger or procedure

        @@MUST_REMEMBER@@
        KIND: bogus_kind
        MEMO: should be skipped

        KIND: decision
        MEMO:

        @@TOPICS@@
        DETAIL: detail with no TOPIC line

        TOPIC: NEW
        SUMMARY: new but nameless
        DETAIL: dropped for missing NAME
    """)
    c = lc.parse_extraction(bundle, now=NOW, source="x")
    assert c.skills == []           # missing trigger/procedure
    assert c.must_remember == []    # bad kind + empty memo
    assert c.topics == []           # missing TOPIC + NEW without NAME


def test_parse_topic_new_requires_summary() -> None:
    bundle = dedent("""\
        @@SKILLS@@
        NONE
        @@MUST_REMEMBER@@
        NONE
        @@TOPICS@@
        TOPIC: NEW
        NAME: Named but summaryless
        DETAIL: dropped — a NEW topic must be index-worthy
    """)
    assert lc.parse_extraction(bundle, now=NOW, source="x").topics == []


def test_parse_topic_new_unsluggable_name_dropped() -> None:
    """A NEW block whose NAME has no sluggable characters (all symbols /
    non-Latin) is dropped at parse time — it must never survive to routing,
    where minting its slug would raise mid-ingest after other stores saved."""
    bundle = dedent("""\
        @@SKILLS@@
        NONE
        @@MUST_REMEMBER@@
        NONE
        @@TOPICS@@
        TOPIC: NEW
        NAME: 日本語
        SUMMARY: unrepresentable in an ascii slug
        DETAIL: dropped, not crashed

        TOPIC: NEW
        NAME: !!!
        SUMMARY: symbols only
        DETAIL: dropped, not crashed

        TOPIC: NEW
        NAME: Survivor Topic
        SUMMARY: the good sibling still lands
        DETAIL: kept
    """)
    c = lc.parse_extraction(bundle, now=NOW, source="x")
    assert [t.name for t in c.topics] == ["Survivor Topic"]


def test_parse_skill_unsluggable_name_dropped() -> None:
    """A skill NAME with no sluggable characters would persist fine but
    poison every later index/detail render (detail filenames slug the
    name) — the block is dropped at parse time, never the bundle."""
    bundle = dedent("""\
        @@SKILLS@@
        NAME: 日本語
        TRIGGER: non-latin name
        PROCEDURE: dropped, not crashed

        NAME: !!!
        TRIGGER: symbols only
        PROCEDURE: dropped, not crashed

        NAME: Survivor Skill
        TRIGGER: the good sibling
        PROCEDURE: still lands

        @@MUST_REMEMBER@@
        NONE
        @@TOPICS@@
        NONE
    """)
    c = lc.parse_extraction(bundle, now=NOW, source="x")
    assert [s.name for s in c.skills] == ["Survivor Skill"]


def test_parse_topic_existing_requires_detail() -> None:
    bundle = dedent("""\
        @@SKILLS@@
        NONE
        @@MUST_REMEMBER@@
        NONE
        @@TOPICS@@
        TOPIC: bounded-store
        SUMMARY: a refresh with nothing to append
    """)
    assert lc.parse_extraction(bundle, now=NOW, source="x").topics == []


def test_parse_topic_bad_slug_dropped() -> None:
    # A TOPIC target with no alphanumerics yields an empty slug → EntryError
    # → that block (only) is dropped, never the bundle.
    bundle = dedent("""\
        @@SKILLS@@
        NONE
        @@MUST_REMEMBER@@
        NONE
        @@TOPICS@@
        TOPIC: !!!
        DETAIL: unaddressable

        TOPIC: bounded-store
        DETAIL: this one still lands
    """)
    c = lc.parse_extraction(bundle, now=NOW, source="x")
    assert [t.slug for t in c.topics] == ["bounded-store"]


def test_parse_topic_existing_slug_normalized() -> None:
    bundle = dedent("""\
        @@SKILLS@@
        NONE
        @@MUST_REMEMBER@@
        NONE
        @@TOPICS@@
        TOPIC: Bounded Store
        SUMMARY: refreshed summary
        DETAIL: a new fact
    """)
    c = lc.parse_extraction(bundle, now=NOW, source="x")
    assert len(c.topics) == 1
    t = c.topics[0]
    assert t.slug == "bounded-store"   # normalized to canonical slug form
    assert t.summary == "refreshed summary"
    assert t.detail == "a new fact"


def test_parse_topic_existing_summary_optional() -> None:
    bundle = dedent("""\
        @@SKILLS@@
        NONE
        @@MUST_REMEMBER@@
        NONE
        @@TOPICS@@
        TOPIC: bounded-store
        DETAIL: append without touching the summary
    """)
    c = lc.parse_extraction(bundle, now=NOW, source="x")
    assert c.topics[0].summary == ""


def test_parse_multiline_value_continuation() -> None:
    bundle = dedent("""\
        @@SKILLS@@
        NAME: Multi
        TRIGGER: always
        PROCEDURE: first line
        second line continues the procedure

        @@MUST_REMEMBER@@
        NONE
        @@TOPICS@@
        NONE
    """)
    c = lc.parse_extraction(bundle, now=NOW, source="x")
    assert "second line" in c.skills[0].procedure


def test_section_blocks_leading_blank_and_continuation() -> None:
    # Leading blank line (empty current → continue), then a continuation
    # line before any key (last_key is None → ignored), then a real block.
    section = "\n\nstray line before key\nNAME: real\nTRIGGER: t\nPROCEDURE: p"
    blocks = lc._section_blocks(section)
    assert len(blocks) == 1
    assert blocks[0]["NAME"] == "real"


def test_section_blocks_none_only() -> None:
    assert lc._section_blocks("NONE — nothing here") == []


def test_section_blocks_trailing_blank_flushes() -> None:
    # A trailing blank line flushes the block, so the final `if current`
    # sees an empty current.
    section = "NAME: x\nTRIGGER: t\nPROCEDURE: p\n\n"
    blocks = lc._section_blocks(section)
    assert len(blocks) == 1 and blocks[0]["NAME"] == "x"


def test_safe_format_dict_missing_key() -> None:
    out = "{a} {b}".format_map(lc._SafeFormatDict({"a": "X"}))
    assert out == "X {b}"  # missing key rendered literally


def test_drop_legacy_surface_no_archive(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.paths.journal.mkdir(parents=True, exist_ok=True)
    # No archive dir created → the rmtree branch is skipped.
    lc._drop_legacy_surface(store)
    assert not store.paths.archive.exists()


# ----- extract (model touch point) ------------------------------------------


def test_extract_candidates_parses(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    c = lc.extract_candidates(cfg, ScriptedExtractor(_FULL_BUNDLE), _rec(), now=NOW)
    assert c.total() == 4


def test_extract_candidates_embeds_topic_index(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    s = ScriptedExtractor(_FULL_BUNDLE)
    routing = "- `seed-topic` — Seed topic (last 2026-06-17): seeded"
    lc.extract_candidates(cfg, s, _rec(), now=NOW, topic_index=routing)
    assert s.last_prompt is not None
    assert routing in s.last_prompt
    assert "{topic_index}" not in s.last_prompt


def test_extract_candidates_parse_error_yields_empty(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    c = lc.extract_candidates(cfg, ScriptedExtractor("garbage no markers"), _rec())
    assert c.is_empty()


def test_extract_candidates_backend_error_yields_empty(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    boom = ScriptedExtractor(raises=RuntimeError("backend down"))
    assert lc.extract_candidates(cfg, boom, _rec()).is_empty()


def test_extract_candidates_prefilter_off(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, "prefilter:\n  enabled: false\n")
    c = lc.extract_candidates(cfg, ScriptedExtractor(_FULL_BUNDLE), _rec(), now=NOW)
    assert c.total() == 4


def test_fill_extract_prompt_fills_topic_placeholders(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    routing = "- `seed-topic` — Seed topic (last 2026-06-17): seeded"
    prompt = lc._fill_extract_prompt(
        cfg, lc._prompts_root(cfg), _rec(), "THE-CONTENT", topic_index=routing
    )
    assert "THE-CONTENT" in prompt
    assert routing in prompt
    assert "TestTiger" in prompt
    for marker in ("@@SKILLS@@", "@@MUST_REMEMBER@@", "@@TOPICS@@"):
        assert marker in prompt
    for placeholder in (
        "{topic_index}", "{topic_summary_max_words}", "{topic_detail_max_words}",
        "{procedure_max_words}", "{memo_max_words}", "{content}",
    ):
        assert placeholder not in prompt


# ----- _append_topic_detail ---------------------------------------------------


def test_append_topic_detail_empty_body() -> None:
    assert lc._append_topic_detail("", "2026-06-17", "first fact") == (
        "## 2026-06-17\n- first fact"
    )
    # Whitespace-only body counts as empty too.
    assert lc._append_topic_detail("  \n", "2026-06-17", "f") == "## 2026-06-17\n- f"


def test_append_topic_detail_same_day_appends_bullet() -> None:
    body = "## 2026-06-17\n- first"
    out = lc._append_topic_detail(body, "2026-06-17", "second")
    assert out == "## 2026-06-17\n- first\n- second"


def test_append_topic_detail_new_day_opens_section() -> None:
    body = "## 2026-06-16\n- old"
    out = lc._append_topic_detail(body, "2026-06-17", "new")
    assert out == "## 2026-06-16\n- old\n\n## 2026-06-17\n- new"


def test_append_topic_detail_headerless_body_opens_section() -> None:
    # A body with no dated headers at all (legacy/hand-edited) still gets a
    # fresh dated section rather than a stray bullet.
    out = lc._append_topic_detail("some prose", "2026-06-17", "fact")
    assert out == "some prose\n\n## 2026-06-17\n- fact"


# ----- _route_topic_candidates ------------------------------------------------


def test_route_existing_appends_touches_and_refreshes_summary() -> None:
    entry = _topic(last_used="2026-06-01T00:00:00Z")
    cand = lc.TopicCandidate(
        slug="bounded-store", name="", summary="fresher summary", detail="new fact"
    )
    merged, landed = lc._route_topic_candidates(
        [entry], [cand], now=NOW, source="extract"
    )
    assert landed == 1 and merged == [entry]
    assert entry.touch_count == 2
    assert entry.last_used == NOW
    assert entry.summary == "fresher summary"
    assert entry.text.endswith(f"## {NOW[:10]}\n- new fact")


def test_route_existing_without_summary_keeps_old() -> None:
    entry = _topic(summary="original")
    cand = lc.TopicCandidate(slug="bounded-store", name="", summary="", detail="d")
    lc._route_topic_candidates([entry], [cand], now=NOW, source="extract")
    assert entry.summary == "original"


def test_route_new_mints_topic() -> None:
    cand = lc.TopicCandidate(
        slug="", name="Sweep Staging!", summary="how staging works", detail="d1"
    )
    merged, landed = lc._route_topic_candidates([], [cand], now=NOW, source="extract")
    assert landed == 1 and len(merged) == 1
    t = merged[0]
    assert isinstance(t, TopicEntry)
    assert t.slug == "sweep-staging"       # minted from the name
    assert t.name == "Sweep Staging!"
    assert t.summary == "how staging works"
    assert t.touch_count == 1
    assert t.text == f"## {NOW[:10]}\n- d1"
    assert t.source == "extract" and t.last_used == NOW


def test_route_unknown_existing_slug_revives() -> None:
    # An "existing" slug the store no longer has: revived as a new topic,
    # name derived from the slug, summary falling back to the detail.
    cand = lc.TopicCandidate(slug="lost-topic", name="", summary="", detail="the fact")
    merged, landed = lc._route_topic_candidates([], [cand], now=NOW, source="extract")
    assert landed == 1 and len(merged) == 1
    t = merged[0]
    assert t.slug == "lost-topic"
    assert t.name == "lost topic"
    assert t.summary == "the fact"
    assert t.touch_count == 1


def test_route_unknown_existing_slug_revive_keeps_given_fields() -> None:
    cand = lc.TopicCandidate(
        slug="lost-topic", name="Lost Topic", summary="its summary", detail="d"
    )
    merged, _ = lc._route_topic_candidates([], [cand], now=NOW, source="extract")
    assert merged[0].name == "Lost Topic"
    assert merged[0].summary == "its summary"


def test_route_new_slug_collision_merges_as_touch() -> None:
    # A NEW candidate whose minted slug collides with an existing topic must
    # merge into it (no duplicate topics by construction).
    entry = _topic("bounded-store", touch_count=3)
    cand = lc.TopicCandidate(
        slug="", name="Bounded Store", summary="near-duplicate", detail="extra"
    )
    merged, landed = lc._route_topic_candidates(
        [entry], [cand], now=NOW, source="extract"
    )
    assert landed == 1 and merged == [entry]
    assert entry.touch_count == 4
    assert "- extra" in entry.text


def test_route_two_new_same_name_single_mint() -> None:
    # Two NEW candidates minting the same slug in one batch: the second lands
    # as a touch on the first (by_slug is updated as topics mint).
    c1 = lc.TopicCandidate(slug="", name="One Topic", summary="s1", detail="d1")
    c2 = lc.TopicCandidate(slug="", name="one topic", summary="s2", detail="d2")
    merged, landed = lc._route_topic_candidates([], [c1, c2], now=NOW, source="x")
    assert landed == 2 and len(merged) == 1
    t = merged[0]
    assert t.touch_count == 2
    assert "- d1" in t.text and "- d2" in t.text
    assert t.summary == "s2"  # the second block's summary refreshes


# ----- ingest ---------------------------------------------------------------


def test_ingest_writes_three_stores(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    c = lc.parse_extraction(_FULL_BUNDLE, now=NOW, source="x")
    added = lc.ingest_candidates(BoundedStore(cfg, store), cfg, c, now=NOW)
    assert added == {STORE_SKILLS: 1, STORE_MUST_REMEMBER: 2, STORE_TOPICS: 1,
                     'touched': 0}
    bstore = BoundedStore(cfg, store)
    assert len(bstore.load(STORE_SKILLS)) == 1
    assert len(bstore.load(STORE_MUST_REMEMBER)) == 2
    topics = bstore.load(STORE_TOPICS)
    assert len(topics) == 1
    assert topics[0].slug == "bounded-store-revamp"
    # Skill importance refreshed (>=0; log1p(0)=0 for usage_count 0).
    assert bstore.load(STORE_SKILLS)[0].importance >= 0.0


def test_ingest_empty_is_noop(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    empty = lc.parse_extraction(_EMPTY_BUNDLE, now=NOW, source="x")
    added = lc.ingest_candidates(BoundedStore(cfg, store), cfg, empty)
    assert added == {STORE_SKILLS: 0, STORE_MUST_REMEMBER: 0, STORE_TOPICS: 0,
                     'touched': 0}


def test_ingest_reingest_routes_into_same_topic(tmp_path: Path) -> None:
    # Re-ingesting the same bundle appends to the SAME topic (slug routing),
    # never a duplicate — skills/must_remember append as before.
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    c = lc.parse_extraction(_FULL_BUNDLE, now=NOW, source="x")
    lc.ingest_candidates(BoundedStore(cfg, store), cfg, c, now=NOW)
    c2 = lc.parse_extraction(_FULL_BUNDLE, now=NOW, source="x")
    lc.ingest_candidates(BoundedStore(cfg, store), cfg, c2, now=NOW)
    bstore = BoundedStore(cfg, store)
    topics = bstore.load(STORE_TOPICS)
    assert len(topics) == 1
    assert topics[0].touch_count == 2
    assert len(bstore.load(STORE_MUST_REMEMBER)) == 4


def test_ingest_topics_only(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    c = lc.Candidates(
        skills=[], must_remember=[],
        topics=[lc.TopicCandidate(slug="", name="Solo", summary="s", detail="d")],
    )
    added = lc.ingest_candidates(BoundedStore(cfg, store), cfg, c, now=NOW)
    assert added == {STORE_SKILLS: 0, STORE_MUST_REMEMBER: 0, STORE_TOPICS: 1,
                     'touched': 0}
    assert not BoundedStore(cfg, store).load(STORE_SKILLS)


def test_ingest_no_topics_skips_topic_store(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    bundle = dedent("""\
        @@SKILLS@@
        NONE
        @@MUST_REMEMBER@@
        KIND: decision
        MEMO: only a memo
        @@TOPICS@@
        NONE
    """)
    c = lc.parse_extraction(bundle, now=NOW, source="x")
    added = lc.ingest_candidates(BoundedStore(cfg, store), cfg, c, now=NOW)
    assert added == {STORE_SKILLS: 0, STORE_MUST_REMEMBER: 1, STORE_TOPICS: 0,
                     'touched': 0}
    assert BoundedStore(cfg, store).load(STORE_TOPICS) == []


def test_extract_and_ingest(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    added = lc.extract_and_ingest(cfg, store, ScriptedExtractor(_FULL_BUNDLE), _rec())
    assert added[STORE_SKILLS] == 1
    assert added[STORE_TOPICS] == 1


def test_extract_and_ingest_routes_to_existing_topic(tmp_path: Path) -> None:
    # End-to-end: the store's routing list is embedded in the prompt, and a
    # bundle addressing an existing slug lands as a touch, not a new topic.
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    BoundedStore(cfg, store).save_atomic(STORE_TOPICS, [_topic("bounded-store")])
    bundle = dedent("""\
        @@SKILLS@@
        NONE
        @@MUST_REMEMBER@@
        NONE
        @@TOPICS@@
        TOPIC: bounded-store
        DETAIL: routed straight into the seed topic
    """)
    s = ScriptedExtractor(bundle)
    added = lc.extract_and_ingest(cfg, store, s, _rec(), now=NOW)
    assert added[STORE_TOPICS] == 1
    assert s.last_prompt is not None and "`bounded-store`" in s.last_prompt
    topics = BoundedStore(cfg, store).load(STORE_TOPICS)
    assert len(topics) == 1
    assert topics[0].touch_count == 2
    assert "routed straight into the seed topic" in topics[0].text


# ----- decide ---------------------------------------------------------------


def test_decide_active_vs_extract(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    now = 1_000_000.0
    fresh = _rec(activity_mtime=now)            # just touched → active
    idle = _rec(activity_mtime=now - 100_000)   # long idle → extract
    decisions = lc._decide([fresh, idle], cfg, now=now)
    assert decisions[0].action == lc.SKIP_ACTIVE
    assert decisions[1].action == lc.EXTRACT


# ----- plan_extraction (staging) --------------------------------------------


def test_plan_extraction_stages_idle(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    monkeypatch.setattr(
        lc, "_discover", lambda c, **kw: [_rec(content="abc", activity_mtime=0.0)]
    )
    items = lc.plan_extraction(cfg, store)
    assert len(items) == 1
    staging = lc._sweep_staging_dir(store)
    assert (staging / "manifest.json").exists()
    assert (staging / "conv-1.prompt.md").exists()
    prompt = (staging / "conv-1.prompt.md").read_text()
    assert "extract" in prompt
    # No topics yet → the routing list is the explicit "everything is NEW" note.
    assert "(no topics exist yet" in prompt
    assert "{topic_index}" not in prompt


def test_plan_extraction_prompt_embeds_routing_list(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    BoundedStore(cfg, store).save_atomic(
        STORE_TOPICS, [_topic("seed-topic", name="Seed topic", summary="seeded")]
    )
    monkeypatch.setattr(
        lc, "_discover", lambda c, **kw: [_rec(content="abc", activity_mtime=0.0)]
    )
    items = lc.plan_extraction(cfg, store)
    assert len(items) == 1
    prompt = (lc._sweep_staging_dir(store) / "conv-1.prompt.md").read_text()
    assert "`seed-topic`" in prompt
    assert "seeded" in prompt


def test_plan_extraction_skips_active(tmp_path: Path, monkeypatch) -> None:
    import time as _t
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    monkeypatch.setattr(
        lc, "_discover",
        lambda c, **kw: [_rec(content="abc", activity_mtime=_t.time())],
    )
    items = lc.plan_extraction(cfg, store)
    assert items == []


def test_plan_extraction_max_sessions_cap(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    recs = [
        SourceRecord(
            conversation_uuid=f"c{i}", source="claude_code", source_id="s",
            first_event_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            last_event_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            activity_mtime=0.0, content="x", raw_path=Path("/r"),
        )
        for i in range(3)
    ]
    monkeypatch.setattr(lc, "_discover", lambda c, **kw: recs)
    items = lc.plan_extraction(cfg, store, max_sessions=2)
    assert len(items) == 2


def test_plan_extraction_clears_stale_staging(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    staging = lc._sweep_staging_dir(store)
    staging.mkdir(parents=True)
    (staging / "old.prompt.md").write_text("stale")
    monkeypatch.setattr(lc, "_discover", lambda c, **kw: [])
    lc.plan_extraction(cfg, store)
    assert not (staging / "old.prompt.md").exists()


def test_plan_extraction_prefilter_off(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, "prefilter:\n  enabled: false\n")
    store = Store(cfg.store.root)
    monkeypatch.setattr(lc, "_discover", lambda c, **kw: [_rec(activity_mtime=0.0)])
    assert len(lc.plan_extraction(cfg, store)) == 1


# ----- pack_stacks ----------------------------------------------------------


def test_pack_stacks_groups_by_budget() -> None:
    weighted = [("a", 60), ("b", 60), ("c", 10)]
    stacks = lc._pack_stacks(weighted, char_budget=100, max_items=10)
    assert stacks == [["a"], ["b", "c"]]


def test_pack_stacks_max_items() -> None:
    weighted = [("a", 1), ("b", 1), ("c", 1)]
    stacks = lc._pack_stacks(weighted, char_budget=1000, max_items=2)
    assert stacks == [["a", "b"], ["c"]]


def test_pack_stacks_empty() -> None:
    assert lc._pack_stacks([], char_budget=10, max_items=2) == []


def test_pack_stacks_oversized_solo() -> None:
    stacks = lc._pack_stacks([("big", 500)], char_budget=100, max_items=10)
    assert stacks == [["big"]]


# ----- rebuild (fresh start) ------------------------------------------------


def test_rebuild_drops_legacy_surface(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    journal = store.paths.journal
    (journal / "must_memorize.md").write_text("old table")
    (journal / "longer_memory.md").write_text("old history")
    (journal / "20260101-120000-abc.md").write_text("a short")
    (journal / "20260101-daily-abc.md").write_text("a daily")
    (journal / "20260101-week-abc.md").write_text("a weekly")
    (journal / "202601-month-abc.md").write_text("a monthly")
    # Simulate a pre-existing legacy archive dir from an old store. The live
    # layout no longer creates archive/ (GAP-1), so a real migration would
    # only see it when an old store carried one — recreate that here.
    store.paths.archive.mkdir(parents=True, exist_ok=True)
    (store.paths.archive / "x.md").write_text("archive entry")
    # New-store files must survive the fresh start.
    bstore = BoundedStore(cfg, store)
    bstore.save_atomic(STORE_TOPICS, [_topic("keep-me", name="Keep me")])

    assert lc.rebuild(cfg, store) == 0
    assert not (journal / "must_memorize.md").exists()
    assert not (journal / "longer_memory.md").exists()
    assert not (journal / "20260101-120000-abc.md").exists()
    assert not (journal / "20260101-daily-abc.md").exists()
    assert not (store.paths.archive / "x.md").exists()
    kept = BoundedStore(cfg, store).load(STORE_TOPICS)  # preserved
    assert [t.slug for t in kept] == ["keep-me"]
    assert store.paths.briefing.exists()     # regenerated


def test_rebuild_idempotent_no_legacy(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    assert lc.rebuild(cfg, store) == 0
    assert lc.rebuild(cfg, store) == 0  # no legacy, no archive dir → fine


def test_rebuild_runs_format_gate(tmp_path: Path) -> None:
    """The per-persona rebuild runs `check --fix`: a non-canonical topics
    store is mechanically repaired before the briefing is assembled from it."""
    from tigerharness.tiger_memory.check import check_all
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    BoundedStore(cfg, store).save_atomic(STORE_TOPICS, [_topic("drifty")])
    topics_path = store.paths.journal / "topics.md"
    # Parseable but non-canonical (trailing blank drift) — the gate rewrites.
    topics_path.write_text(topics_path.read_text() + "\n\n\n")
    assert not check_all(cfg, store).ok
    assert lc.rebuild(cfg, store) == 0
    assert check_all(cfg, store).ok
    assert [t.slug for t in BoundedStore(cfg, store).load(STORE_TOPICS)] == ["drifty"]


# ----- pin ------------------------------------------------------------------


def test_pin_writes_must_remember(tmp_path: Path, capsys) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    assert lc.pin(cfg, store, memo="never force push", kind="operator_explicit") == 0
    entries = BoundedStore(cfg, store).load(STORE_MUST_REMEMBER)
    assert len(entries) == 1
    assert entries[0].kind == KIND_OPERATOR_EXPLICIT
    assert entries[0].repeat_count == 1
    assert not hasattr(entries[0], "importance")  # no importance scalar
    assert "pinned" in capsys.readouterr().out


def test_pin_preference_kind(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    lc.pin(cfg, store, memo="use uv", kind="preference")
    (entry,) = BoundedStore(cfg, store).load(STORE_MUST_REMEMBER)
    assert entry.kind == "preference" and entry.repeat_count == 1


def test_pin_unknown_kind(tmp_path: Path, capsys) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    assert lc.pin(cfg, store, memo="x", kind="bogus") == 2
    assert "unknown kind" in capsys.readouterr().out


def test_pin_appends(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    lc.pin(cfg, store, memo="one", kind="preference")
    lc.pin(cfg, store, memo="two", kind="decision")
    assert len(BoundedStore(cfg, store).load(STORE_MUST_REMEMBER)) == 2


# ----- mission text sourcing ------------------------------------------------


def test_team_mission_text_reads_charter(tmp_path: Path) -> None:
    # store.root == <team>/memories/<persona>; charter is <team>/charter/README.md
    team = tmp_path / "team"
    (team / "charter").mkdir(parents=True)
    (team / "charter" / "README.md").write_text("## Mission\nShip it.")
    cfg = _cfg(tmp_path)
    # Point the store root at <team>/memories/<persona>.
    object.__setattr__(cfg.store, "root", team / "memories" / "TestTiger")
    assert "Ship it." in lc.team_mission_text(cfg)


def test_team_mission_text_missing_returns_empty(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    object.__setattr__(cfg.store, "root", tmp_path / "no" / "memories" / "p")
    assert lc.team_mission_text(cfg) == ""


# ----- clip -----------------------------------------------------------------


def test_clip_under_budget_unchanged() -> None:
    assert lc._clip("short", 100) == "short"


def test_clip_over_budget_elides_middle() -> None:
    out = lc._clip("x" * 1000, 200)
    assert len(out) <= 200
    assert "elided" in out


def test_clip_tiny_budget_hard_truncate() -> None:
    out = lc._clip("x" * 100, 5)
    assert len(out) == 5


# ----- adapters / discover --------------------------------------------------


def test_build_adapters_claude_and_journal(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, dedent(f"""\
        env_var: TIGER_MEMORY_CONFIG
    """))
    # The default config has a claude_code source.
    adapters = lc._build_adapters(cfg)
    assert any(type(a).__name__ == "ClaudeTranscriptAdapter" for a in adapters)


def test_build_adapters_journal_worklog(tmp_path: Path) -> None:
    jr = tmp_path / "journal"
    jr.mkdir()
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(dedent(f"""\
        agent: {{name: T, role: t}}
        store: {{root: {tmp_path}/memory}}
        sources:
          - kind: journal_worklog
            journal_root: {jr}
            persona: T
            team: TeamX
        summarizer: {{backend: anthropic, model: m, prompts: default/v1}}
    """))
    cfg = load_config(cfg_path)
    adapters = lc._build_adapters(cfg)
    assert any(type(a).__name__ == "JournalWorklogAdapter" for a in adapters)


def test_build_adapters_slack_thread_sets_threads_json(tmp_path: Path) -> None:
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(dedent(f"""\
        agent: {{name: T, role: t}}
        store: {{root: {tmp_path}/memory}}
        sources:
          - kind: slack_thread
            threads_json: {tmp_path}/threads.json
          - kind: claude_code
            project_path: {tmp_path}/proj/
        summarizer: {{backend: anthropic, model: m, prompts: default/v1}}
    """))
    cfg = load_config(cfg_path)
    adapters = lc._build_adapters(cfg)
    # slack_thread carries no adapter; claude_code does.
    assert len(adapters) == 1


def test_resolve_journal_root_relative(tmp_path: Path) -> None:
    out = lc._resolve_journal_root("sub", tmp_path)
    assert out == (tmp_path / "sub").resolve()


def test_build_summarizer_mock(tmp_path: Path) -> None:
    from tigerharness.tiger_memory.summarizers.mock import MockSummarizer
    assert isinstance(lc._build_summarizer(_cfg(tmp_path), mock=True), MockSummarizer)


def test_build_summarizer_registry(tmp_path: Path) -> None:
    from tigerharness.tiger_memory.summarizers import AnthropicSummarizer
    s = lc._build_summarizer(_cfg(tmp_path))
    assert isinstance(s, AnthropicSummarizer)


def test_discover_runs_adapters(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    # No real transcripts on disk → discover yields nothing, but exercises the loop.
    assert lc._discover(cfg) == []


# ----- must_remember freshness touches (TOUCH blocks) -------------------------


def test_parse_touch_blocks() -> None:
    bundle = dedent("""\
        @@SKILLS@@
        NONE
        @@MUST_REMEMBER@@
        TOUCH: abc123

        KIND: decision
        MEMO: a real new memo

        TOUCH: def456

        TOUCH:
        @@TOPICS@@
        NONE
    """)
    c = lc.parse_extraction(bundle, now=NOW, source="x")
    assert c.touches == ["abc123", "def456"]  # empty TOUCH value dropped
    assert len(c.must_remember) == 1
    assert not c.is_empty()          # touches alone make a bundle non-empty


def test_clean_ref_strips_backticks_and_whitespace() -> None:
    # Prompt listings display addresses backticked (`slug`); a card that
    # echoes the displayed form must still resolve.
    assert lc.clean_ref("`abc123`") == "abc123"
    assert lc.clean_ref("  `topic-slug`  ") == "topic-slug"
    assert lc.clean_ref("plain") == "plain"
    assert lc.clean_ref("  spaced  ") == "spaced"
    assert lc.clean_ref(None) == ""
    assert lc.clean_ref("  ``  ") == ""


def test_parse_touch_backticked_id_resolves() -> None:
    bundle = dedent("""\
        @@SKILLS@@
        NONE
        @@MUST_REMEMBER@@
        TOUCH: `abc123`
        @@TOPICS@@
        NONE
    """)
    c = lc.parse_extraction(bundle, now=NOW, source="x")
    assert c.touches == ["abc123"]  # backticked echo of the prompt listing


def test_parse_touch_merged_with_memo_keeps_both() -> None:
    # A sloppy card merged a memo and a touch into ONE block (no blank
    # line between them): the touch registers AND the memo lands — the
    # memo must not be silently dropped.
    bundle = dedent("""\
        @@SKILLS@@
        NONE
        @@MUST_REMEMBER@@
        TOUCH: abc123
        KIND: decision
        MEMO: memo sharing the touch block
        @@TOPICS@@
        NONE
    """)
    c = lc.parse_extraction(bundle, now=NOW, source="x")
    assert c.touches == ["abc123"]
    assert [m.text for m in c.must_remember] == ["memo sharing the touch block"]
    assert c.must_remember[0].kind == "decision"


def test_ingest_touch_refreshes_last_used_and_repeat(tmp_path: Path) -> None:
    from tigerharness.tiger_memory.entries import MustRememberEntry
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    bstore = BoundedStore(cfg, store)
    old = MustRememberEntry(
        text="targeted git add, never -A", created_at="2026-01-01T00:00:00Z",
        last_used="2026-01-01T00:00:00Z", source="pin", kind=KIND_PREFERENCE,
    )
    bstore.save_atomic(STORE_MUST_REMEMBER, [old])
    c = lc.Candidates(
        skills=[], must_remember=[], topics=[],
        touches=[old.id, old.id, "unknown-id"],   # dup + unknown are safe
    )
    added = lc.ingest_candidates(bstore, cfg, c, now=NOW)
    assert added["touched"] == 1
    (loaded,) = bstore.load(STORE_MUST_REMEMBER)
    assert loaded.last_used == NOW               # freshness refreshed
    assert loaded.repeat_count == 2              # dup touch counted once
    assert loaded.created_at == "2026-01-01T00:00:00Z"


def test_ingest_touch_only_unknown_id_saves_nothing(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    bstore = BoundedStore(cfg, store)
    c = lc.Candidates(skills=[], must_remember=[], topics=[], touches=["nope"])
    added = lc.ingest_candidates(bstore, cfg, c, now=NOW)
    assert added["touched"] == 0
    assert not (store.paths.journal / "must_remember.md").exists()


def test_plan_embeds_must_remember_touch_list(tmp_path: Path, monkeypatch) -> None:
    from tigerharness.tiger_memory.entries import MustRememberEntry
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    bstore = BoundedStore(cfg, store)
    memo = MustRememberEntry(
        text="never push main", created_at=NOW, last_used=NOW,
        source="pin", kind=KIND_OPERATOR_EXPLICIT,
    )
    bstore.save_atomic(STORE_MUST_REMEMBER, [memo])
    monkeypatch.setattr(
        lc, "_discover", lambda c, **kw: [_rec(content="abc", activity_mtime=0.0)]
    )
    items = lc.plan_extraction(cfg, store)
    assert len(items) == 1
    staged = Path(items[0]["prompt_path"]).read_text(encoding="utf-8")
    assert f"`{memo.id}` [operator_explicit] never push main" in staged
    assert "{must_remember_index}" not in staged


def test_fill_extract_prompt_defaults_mr_placeholder(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    rec = _rec()
    filled = lc._fill_extract_prompt(
        cfg, lc._prompts_root(cfg), rec, "content", topic_index="(none)"
    )
    assert "(no must-remember items yet)" in filled

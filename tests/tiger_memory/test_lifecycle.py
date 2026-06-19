"""Tests for the extraction lifecycle (lifecycle.py, bounded-store revamp).

Covers the extraction-bundle parser, the in-process extract→ingest path (under
the mock summarizer — no live model), the idle/extract decision, the staging
planner, the fresh-start rebuild, pin → must_remember, mission-text sourcing,
and the clip/stack helpers.
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
    KIND_OWNER_EXPLICIT,
    KIND_PREFERENCE,
    STORE_DIARY,
    STORE_MUST_REMEMBER,
    STORE_SKILLS,
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

    def summarize(self, *, prompt: str, max_words: int) -> str:
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


_FULL_BUNDLE = dedent("""\
    @@SKILLS@@
    NAME: Bound a markdown store
    TRIGGER: when a store grows unbounded
    PROCEDURE: write one entry per block; rewrite the whole file atomically

    @@MUST_REMEMBER@@
    KIND: owner_explicit
    MEMO: never push without asking

    KIND: preference
    MEMO: targeted git add, never -A

    @@EMOTIONAL@@
    WEIGHT: 7
    TEXT: landed the bounded-store substrate clean and green
""")


# ----- bundle parsing -------------------------------------------------------


def test_parse_full_bundle() -> None:
    c = lc.parse_extraction(_FULL_BUNDLE, now=NOW, source="claude_code")
    assert len(c.skills) == 1
    assert c.skills[0].name == "Bound a markdown store"
    assert c.skills[0].procedure.startswith("write one entry")
    assert [e.kind for e in c.must_remember] == [KIND_OWNER_EXPLICIT, KIND_PREFERENCE]
    assert len(c.diary) == 1
    assert c.diary[0].weight == 7.0
    assert c.diary[0].text == "landed the bounded-store substrate clean and green"
    assert not c.is_empty()
    assert c.total() == 4


def test_parse_all_none() -> None:
    c = lc.parse_extraction(
        "@@SKILLS@@\nNONE\n@@MUST_REMEMBER@@\nNONE\n@@EMOTIONAL@@\nNONE\n",
        now=NOW, source="x",
    )
    assert c.is_empty()


def test_parse_missing_marker_raises() -> None:
    with pytest.raises(lc.ExtractionParseError, match="missing"):
        lc.parse_extraction("@@SKILLS@@\nNONE\n@@EMOTIONAL@@\nNONE\n", now=NOW, source="x")


def test_parse_empty_raises() -> None:
    with pytest.raises(lc.ExtractionParseError, match="empty"):
        lc.parse_extraction("   ", now=NOW, source="x")


def test_parse_out_of_order_raises() -> None:
    bundle = "@@EMOTIONAL@@\nNONE\n@@SKILLS@@\nNONE\n@@MUST_REMEMBER@@\nNONE\n"
    with pytest.raises(lc.ExtractionParseError, match="out of order"):
        lc.parse_extraction(bundle, now=NOW, source="x")


def test_parse_skips_malformed_blocks() -> None:
    bundle = dedent("""\
        @@SKILLS@@
        NAME: only a name, no trigger or procedure

        @@MUST_REMEMBER@@
        KIND: bogus_kind
        MEMO: should be skipped

        KIND: decision
        MEMO:

        @@EMOTIONAL@@
        WEIGHT: not-a-number
        TEXT: y

        WEIGHT: 3
    """)
    c = lc.parse_extraction(bundle, now=NOW, source="x")
    assert c.skills == []           # missing trigger/procedure
    assert c.must_remember == []    # bad kind + empty memo
    assert c.diary == []        # bad weight + missing TEXT


def test_parse_multiline_value_continuation() -> None:
    bundle = dedent("""\
        @@SKILLS@@
        NAME: Multi
        TRIGGER: always
        PROCEDURE: first line
        second line continues the procedure

        @@MUST_REMEMBER@@
        NONE
        @@EMOTIONAL@@
        NONE
    """)
    c = lc.parse_extraction(bundle, now=NOW, source="x")
    assert "second line" in c.skills[0].procedure


def test_parse_weight_empty_string() -> None:
    assert lc._parse_weight("") is None
    assert lc._parse_weight(None) is None
    assert lc._parse_weight("garbage") is None
    assert lc._parse_weight("5 stars") == 5.0


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
    # sees an empty current (the 170->172 branch).
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


# ----- ingest ---------------------------------------------------------------


def test_ingest_writes_three_stores(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    c = lc.parse_extraction(_FULL_BUNDLE, now=NOW, source="x")
    added = lc.ingest_candidates(BoundedStore(cfg, store), cfg, c, now=NOW)
    assert added == {STORE_SKILLS: 1, STORE_MUST_REMEMBER: 2, STORE_DIARY: 1}
    bstore = BoundedStore(cfg, store)
    assert len(bstore.load(STORE_SKILLS)) == 1
    assert len(bstore.load(STORE_MUST_REMEMBER)) == 2
    assert len(bstore.load(STORE_DIARY)) == 1
    # Skill importance refreshed (>=0; log1p(0)=0 for usage_count 0).
    assert bstore.load(STORE_SKILLS)[0].importance >= 0.0


def test_ingest_empty_is_noop(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    empty = lc.parse_extraction(
        "@@SKILLS@@\nNONE\n@@MUST_REMEMBER@@\nNONE\n@@EMOTIONAL@@\nNONE\n",
        now=NOW, source="x",
    )
    added = lc.ingest_candidates(BoundedStore(cfg, store), cfg, empty)
    assert added == {STORE_SKILLS: 0, STORE_MUST_REMEMBER: 0, STORE_DIARY: 0}


def test_ingest_appends_to_existing(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    c = lc.parse_extraction(_FULL_BUNDLE, now=NOW, source="x")
    lc.ingest_candidates(BoundedStore(cfg, store), cfg, c, now=NOW)
    lc.ingest_candidates(BoundedStore(cfg, store), cfg, c, now=NOW)
    assert len(BoundedStore(cfg, store).load(STORE_DIARY)) == 2


def test_extract_and_ingest(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    added = lc.extract_and_ingest(cfg, store, ScriptedExtractor(_FULL_BUNDLE), _rec())
    assert added[STORE_SKILLS] == 1


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
    assert "extract" in (staging / "conv-1.prompt.md").read_text()


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
    # A new-store file must survive the fresh start.
    (journal / "skills.md").write_text("---\nid: x\nstore: skills\n---\nkeep me\n")

    assert lc.rebuild(cfg, store) == 0
    assert not (journal / "must_memorize.md").exists()
    assert not (journal / "longer_memory.md").exists()
    assert not (journal / "20260101-120000-abc.md").exists()
    assert not (journal / "20260101-daily-abc.md").exists()
    assert not (store.paths.archive / "x.md").exists()
    assert (journal / "skills.md").exists()  # preserved
    assert store.paths.briefing.exists()     # regenerated


def test_rebuild_idempotent_no_legacy(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    assert lc.rebuild(cfg, store) == 0
    assert lc.rebuild(cfg, store) == 0  # no legacy, no archive dir → fine


# ----- pin ------------------------------------------------------------------


def test_pin_writes_must_remember(tmp_path: Path, capsys) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    assert lc.pin(cfg, store, memo="never force push", kind="owner_explicit") == 0
    entries = BoundedStore(cfg, store).load(STORE_MUST_REMEMBER)
    assert len(entries) == 1
    assert entries[0].kind == KIND_OWNER_EXPLICIT
    assert entries[0].importance == 5.0
    assert "pinned" in capsys.readouterr().out


def test_pin_preference_lower_importance(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    lc.pin(cfg, store, memo="use uv", kind="preference")
    assert BoundedStore(cfg, store).load(STORE_MUST_REMEMBER)[0].importance == 1.0


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

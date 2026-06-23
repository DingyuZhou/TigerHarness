"""Wiring tests: the evocation pass runs (only) when enabled, via both ingest
drivers — lifecycle.extract_and_ingest and executor.ingest_extraction. b1-dev-2.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent

from tigerharness.tiger_memory import lifecycle as lc
from tigerharness.tiger_memory.bounded_store import BoundedStore
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.entries import STORE_SKILLS, SkillEntry
from tigerharness.tiger_memory.executor import ingest_extraction
from tigerharness.tiger_memory.sources.base import SourceRecord
from tigerharness.tiger_memory.store import Store
from tigerharness.tiger_memory.summarizers.base import Summarizer

_BUNDLE = dedent("""\
    @@SKILLS@@
    NAME: a freshly learned skill
    TRIGGER: when something happens
    PROCEDURE: do the thing carefully

    @@MUST_REMEMBER@@
    KIND: preference
    MEMO: a brand new preference

    @@DIARY@@
    WEIGHT: 4
    TEXT: shipped the evocation pass today
""")


class DualSummarizer(Summarizer):
    """Returns the extraction bundle for extraction prompts and a crafted
    evocation reply for the evocation prompt (which contains 联想)."""
    name = "dual"
    version = "v1"

    def __init__(self, bundle: str, note_response: str):
        self._bundle = bundle
        self._note = note_response
        self.evocation_calls = 0

    def summarize(self, *, prompt: str, max_words: int) -> str:
        if "联想" in prompt:
            self.evocation_calls += 1
            return self._note
        return self._bundle


def _cfg(tmp_path: Path, *, evocation: bool):
    p = tmp_path / "cfg.yaml"
    p.write_text(dedent(f"""\
        agent: {{name: T, role: r}}
        store: {{root: {tmp_path}/m}}
        sources: [{{kind: claude_code, project_path: {tmp_path}/p/}}]
        summarizer: {{backend: anthropic, model: m, prompts: default/v1}}
        memory:
          diary: {{evocation_enabled: {str(evocation).lower()}}}
    """))
    return load_config(p)


def _rec():
    dt = datetime(2026, 6, 22, tzinfo=timezone.utc)
    return SourceRecord(
        conversation_uuid="conv-1", source="claude_code", source_id="sid",
        first_event_at=dt, last_event_at=dt, activity_mtime=0.0,
        content="hi", raw_path=Path("/raw"),
    )


def _old_skill():
    return SkillEntry(id="old1", text="t", created_at="2026-06-10T00:00:00Z",
                      last_used="2026-06-10T00:00:00Z", source="s",
                      name="an old reusable skill", trigger="x", procedure="y",
                      usage_count=1)


def test_extract_and_ingest_runs_evocation_when_enabled(tmp_path: Path):
    cfg = _cfg(tmp_path, evocation=True)
    store = Store(cfg.store.root)
    store.init_layout()
    bs = BoundedStore(cfg, store)
    bs.save_atomic(STORE_SKILLS, [_old_skill()])      # an OLD item to evoke
    summ = DualSummarizer(_BUNDLE, "NOTE 0: 0")        # new note evokes the old skill
    lc.extract_and_ingest(cfg, store, summ, _rec())
    assert summ.evocation_calls == 1
    skills = bs.load(STORE_SKILLS)
    old = next(e for e in skills if e.name == "an old reusable skill")
    assert old.usage_count == 2                         # reinforced via the pipeline


def test_extract_and_ingest_skips_evocation_when_disabled(tmp_path: Path):
    cfg = _cfg(tmp_path, evocation=False)
    store = Store(cfg.store.root)
    store.init_layout()
    bs = BoundedStore(cfg, store)
    bs.save_atomic(STORE_SKILLS, [_old_skill()])
    summ = DualSummarizer(_BUNDLE, "NOTE 0: 0")
    lc.extract_and_ingest(cfg, store, summ, _rec())
    assert summ.evocation_calls == 0                    # gate off -> no evocation call
    old = next(e for e in bs.load(STORE_SKILLS) if e.name == "an old reusable skill")
    assert old.usage_count == 1                         # untouched


def test_ingest_extraction_runs_evocation_when_enabled(tmp_path: Path):
    cfg = _cfg(tmp_path, evocation=True)
    store = Store(cfg.store.root)
    store.init_layout()
    bs = BoundedStore(cfg, store)
    bs.save_atomic(STORE_SKILLS, [_old_skill()])
    summ = DualSummarizer(_BUNDLE, "NOTE 0: 0")
    ingest_extraction(store, cfg, conversation_uuid="u", source="claude_code",
                      bundle_text=_BUNDLE, summarizer=summ)
    assert summ.evocation_calls == 1
    old = next(e for e in bs.load(STORE_SKILLS) if e.name == "an old reusable skill")
    assert old.usage_count == 2


def test_ingest_extraction_no_evocation_when_summarizer_absent(tmp_path: Path):
    cfg = _cfg(tmp_path, evocation=True)        # enabled, but no summarizer passed
    store = Store(cfg.store.root)
    store.init_layout()
    bs = BoundedStore(cfg, store)
    bs.save_atomic(STORE_SKILLS, [_old_skill()])
    ingest_extraction(store, cfg, conversation_uuid="u", source="claude_code",
                      bundle_text=_BUNDLE)        # summarizer=None default
    old = next(e for e in bs.load(STORE_SKILLS) if e.name == "an old reusable skill")
    assert old.usage_count == 1                  # no evocation without a summarizer

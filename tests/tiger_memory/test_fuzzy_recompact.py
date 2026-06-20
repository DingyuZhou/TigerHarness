"""Tests for fuzzy re-compaction (4-store model, b1-dev-1/Anzai prompt).

Covers the no-aged no-op (no LLM call), the existing-fuzzy fold, mr-only /
diary-only / both bundles, and the empty-output fallback to 100% branch coverage.
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from tigerharness.tiger_memory import fuzzy_recompact as fr
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.entries import DiaryEntry, MustRememberEntry
from tigerharness.tiger_memory.summarizers.base import Summarizer


class MockSummarizer(Summarizer):
    name = "mock"
    version = "v1"

    def __init__(self, out: str):
        super().__init__()
        self._out = out
        self.calls = 0
        self.last_prompt = ""

    def summarize(self, *, prompt: str, max_words: int) -> str:
        self.calls += 1
        self.last_prompt = prompt
        return self._out


def _cfg(tmp_path: Path, fuzzy_max: int = 4000):
    p = tmp_path / "cfg.yaml"
    p.write_text(dedent(f"""\
        agent: {{name: A, role: r}}
        store: {{root: {tmp_path}/m}}
        sources: [{{kind: claude_code, project_path: {tmp_path}/p/}}]
        summarizer: {{backend: anthropic, model: m, prompts: default/v1}}
        memory:
          fuzzy: {{max_length: {fuzzy_max}, overflow_limit: {fuzzy_max + 2000}}}
    """))
    return load_config(p)


def _d(w, text="aged diary note"):
    return DiaryEntry(id=f"d{w}", text=text, created_at="2026-06-01T00:00:00Z",
                      last_used="2026-06-01T00:00:00Z", source="diary", weight=w)


def _m(text="aged fact"):
    return MustRememberEntry(id=f"m{len(text)}", text=text,
                             created_at="2026-06-01T00:00:00Z",
                             last_used="2026-06-01T00:00:00Z", source="s",
                             kind="preference")


def test_no_aged_items_is_noop(tmp_path: Path):
    summ = MockSummarizer("SHOULD NOT BE USED")
    out = fr.recompact_fuzzy(summ, [], [], "## existing\n- gist\n", _cfg(tmp_path))
    assert out == "## existing\n- gist\n" and summ.calls == 0


def test_diary_only_no_existing(tmp_path: Path):
    summ = MockSummarizer("- compacted gist")
    out = fr.recompact_fuzzy(summ, [_d(1.0)], [], "", _cfg(tmp_path))
    assert out == "- compacted gist" and summ.calls == 1
    assert "AGING DIARY ITEMS" in summ.last_prompt
    assert "EXISTING FUZZY MEMORY" not in summ.last_prompt


def test_mr_only_with_existing(tmp_path: Path):
    summ = MockSummarizer("- merged coarse gist")
    out = fr.recompact_fuzzy(summ, [], [_m()], "## old\n- prior\n", _cfg(tmp_path))
    assert out == "- merged coarse gist"
    assert "AGING FACTS" in summ.last_prompt
    assert "EXISTING FUZZY MEMORY" in summ.last_prompt
    assert "AGING DIARY ITEMS" not in summ.last_prompt


def test_both_diary_and_mr(tmp_path: Path):
    summ = MockSummarizer("- everything coarsened")
    out = fr.recompact_fuzzy(summ, [_d(-2.0)], [_m("fact two")], "", _cfg(tmp_path))
    assert out == "- everything coarsened"
    assert "AGING FACTS" in summ.last_prompt and "AGING DIARY ITEMS" in summ.last_prompt


def test_empty_summary_falls_back_to_existing(tmp_path: Path):
    summ = MockSummarizer("   ")  # blank -> must not blank the store
    out = fr.recompact_fuzzy(summ, [_d(1.0)], [], "## keep\n- this\n", _cfg(tmp_path))
    assert out == "## keep\n- this\n" and summ.calls == 1

"""ADR 0006 Part 1 — lossless chunk-and-reduce on the sub-agent staging path.

Covers the restored ``_split_on_boundaries`` (lossless split), the reduce
helpers (``_concat_digests`` / ``_reduce_digests`` with the injectable condense
seam + depth-cap bounded-clip guard), the per-chunk word budget, and
``plan_extraction``'s single vs. map_reduce staging + manifest ``kind``
discriminator. The whole point is acceptance #1: no path silently drops the
middle of an oversized transcript — only the bounded last-resort guard remains.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent

import pytest

from tigerharness.tiger_memory import lifecycle as lc
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.sources.base import SourceRecord
from tigerharness.tiger_memory.store import Store


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


def _rec(content: str, *, uuid: str = "conv-1", activity_mtime: float = 0.0) -> SourceRecord:
    dt = datetime(2026, 6, 1, tzinfo=timezone.utc)
    return SourceRecord(
        conversation_uuid=uuid, source="claude_code", source_id="sid",
        first_event_at=dt, last_event_at=dt, activity_mtime=activity_mtime,
        content=content, raw_path=Path("/raw"),
    )


# ----- _split_on_boundaries (lossless) --------------------------------------


def test_split_rejects_nonpositive_max() -> None:
    with pytest.raises(ValueError):
        lc._split_on_boundaries("anything", 0)


def test_split_within_budget_is_single_chunk() -> None:
    assert lc._split_on_boundaries("abc", 10) == ["abc"]


def test_split_on_line_boundaries_is_lossless() -> None:
    text = "aaaa\nbbbb\ncccc\n"
    chunks = lc._split_on_boundaries(text, 6)
    assert "".join(chunks) == text                  # lossless
    assert all(len(c) <= 6 for c in chunks)         # respects budget
    assert len(chunks) > 1                           # actually split


def test_split_overlong_line_with_empty_buffer_divides_evenly() -> None:
    # A single over-long line with NO preceding buffer (the `if buf` is False
    # branch) that divides evenly into max_chars pieces, so nothing remains
    # buffered at the end either (the closing `if buf` is also False).
    text = "x" * 20
    chunks = lc._split_on_boundaries(text, 10)
    assert chunks == ["x" * 10, "x" * 10]
    assert "".join(chunks) == text                    # lossless


def test_split_hard_splits_overlong_line_with_preceding_buffer() -> None:
    # "ab\n" fills the buffer; the 25-char line forces a buffer flush (the
    # `if buf` branch) then a hard split into two full 10-char pieces + a
    # 5-char remainder that buffers and flushes at the end.
    text = "ab\n" + ("x" * 25)
    chunks = lc._split_on_boundaries(text, 10)
    assert "".join(chunks) == text                  # lossless even mid-line
    assert all(len(c) <= 10 for c in chunks)
    assert "ab\n" in chunks                           # buffer flushed intact
    assert chunks.count("x" * 10) == 2                # two full hard pieces


# ----- _concat_digests / _reduce_digests ------------------------------------


def test_concat_digests_marks_each_part() -> None:
    out = lc._concat_digests(["one", "two"])
    assert "[transcript part 1/2]" in out
    assert "[transcript part 2/2]" in out
    assert "one" in out and "two" in out


def test_reduce_fits_immediately_never_condenses(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, "budgets:\n  max_staged_content_chars: 10000\n")
    calls: list = []

    def condense(piece, idx, total):  # pragma: no cover - asserted never called
        calls.append((idx, total))
        return piece

    out = lc._reduce_digests(["short a", "short b"], cfg, condense=condense)
    assert calls == []                                # under ceiling, no map round
    assert "short a" in out and "short b" in out
    assert lc._CLIP_MARKER not in out                 # not clipped


def test_reduce_shrinks_within_budget(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, dedent("""\
        budgets:
          max_staged_content_chars: 120
          chunk_content_chars: 50
          max_reduce_depth: 3
    """))
    # Two large parts -> concat over ceiling; condense shrinks each piece, so
    # a round or two brings it under budget (no clip).
    parts = ["A" * 200, "B" * 200]

    def condense(piece, idx, total):
        return piece[:10]                             # strong shrink

    out = lc._reduce_digests(parts, cfg, condense=condense)
    assert len(out) <= 120
    assert lc._CLIP_MARKER not in out                 # terminated by shrinking, not the guard


def test_reduce_hits_depth_cap_then_bounded_clip(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, dedent("""\
        budgets:
          max_staged_content_chars: 120
          chunk_content_chars: 50
          max_reduce_depth: 2
    """))
    parts = ["A" * 400, "B" * 400]
    rounds: list = []

    def condense(piece, idx, total):
        rounds.append(idx)
        return piece                                  # pathological: never shrinks

    out = lc._reduce_digests(parts, cfg, condense=condense)
    assert len(out) <= 120                            # guard enforced the ceiling
    assert lc._CLIP_MARKER in out                     # last-resort bounded clip fired
    assert rounds                                     # condense was attempted (depth rounds ran)


def test_reduce_zero_depth_goes_straight_to_clip(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, dedent("""\
        budgets:
          max_staged_content_chars: 50
          max_reduce_depth: 0
    """))

    def condense(piece, idx, total):  # pragma: no cover - never called at depth 0
        raise AssertionError("condense must not run when max_reduce_depth=0")

    out = lc._reduce_digests(["X" * 200], cfg, condense=condense)
    assert len(out) <= 50
    assert lc._CLIP_MARKER in out


# ----- _per_chunk_words -----------------------------------------------------


def test_per_chunk_words_floor(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, "budgets:\n  max_staged_content_chars: 1200\n")
    assert lc._per_chunk_words(cfg, 1000) == 120      # floored
    assert lc._per_chunk_words(cfg, 0) >= 120         # guards div-by-zero


def test_per_chunk_words_scales_up(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, "budgets:\n  max_staged_content_chars: 1200000\n")
    assert lc._per_chunk_words(cfg, 1) > 120          # big budget, few chunks


# ----- plan_extraction: single vs map_reduce staging ------------------------


def test_plan_extraction_single_item_kind(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    monkeypatch.setattr(lc, "_discover", lambda c, **kw: [_rec("small content")])
    items = lc.plan_extraction(cfg, store)
    assert len(items) == 1
    assert items[0]["kind"] == "single"
    assert "prompt_path" in items[0]
    assert "chunk_prompts" not in items[0]
    staging = lc._sweep_staging_dir(store)
    assert (staging / "conv-1.prompt.md").exists()


def test_plan_extraction_map_reduce_when_oversized(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, dedent("""\
        budgets:
          max_staged_content_chars: 100
          chunk_content_chars: 40
    """))
    store = Store(cfg.store.root)
    # 6 lines * ~30 chars each -> ~180 chars > 100 ceiling -> map_reduce.
    content = "".join(f"line number {i} has some words\n" for i in range(6))
    monkeypatch.setattr(lc, "_discover", lambda c, **kw: [_rec(content)])
    items = lc.plan_extraction(cfg, store)
    assert len(items) == 1
    item = items[0]
    assert item["kind"] == "map_reduce"
    assert "prompt_path" not in item
    assert item["reduce_with"] == "extract_memory.md"
    assert len(item["chunk_prompts"]) == len(item["digest_paths"]) >= 2
    staging = lc._sweep_staging_dir(store)
    for cp in item["chunk_prompts"]:
        assert Path(cp).exists()
    # digests are produced by the sub-agent later — not at plan time
    for dp in item["digest_paths"]:
        assert not Path(dp).exists()
    # manifest persists the kind discriminator
    import json
    manifest = json.loads((staging / "manifest.json").read_text())
    assert manifest["items"][0]["kind"] == "map_reduce"


def test_map_reduce_chunks_reconstruct_full_content(tmp_path: Path, monkeypatch) -> None:
    """Acceptance #1 (structural coverage): the staged chunk prompts together
    embed EVERY region of the oversized transcript — including a sentinel in
    the exact middle that the old lossy ``_clip`` would have dropped."""
    cfg = _cfg(tmp_path, dedent("""\
        budgets:
          max_staged_content_chars: 100
          chunk_content_chars: 40
        prefilter:
          enabled: false
    """))
    store = Store(cfg.store.root)
    sentinel = "MIDDLE_SENTINEL_TOKEN"
    lines = [f"pre filler line {i}\n" for i in range(5)]
    lines.append(f"{sentinel}\n")                      # the middle
    lines += [f"post filler line {i}\n" for i in range(5)]
    content = "".join(lines)
    monkeypatch.setattr(lc, "_discover", lambda c, **kw: [_rec(content)])
    item = lc.plan_extraction(cfg, store)[0]
    staged_text = "".join(Path(cp).read_text() for cp in item["chunk_prompts"])
    assert sentinel in staged_text                     # middle preserved, not elided


def test_chunk_prompt_is_plain_prose_not_the_3store_contract(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, dedent("""\
        budgets:
          max_staged_content_chars: 60
          chunk_content_chars: 30
    """))
    store = Store(cfg.store.root)
    content = "".join(f"some words on line {i}\n" for i in range(6))
    monkeypatch.setattr(lc, "_discover", lambda c, **kw: [_rec(content)])
    item = lc.plan_extraction(cfg, store)[0]
    first_chunk = Path(item["chunk_prompts"][0]).read_text()
    # The map step must be the condense prompt, NOT the single-sourced 3-store
    # extraction contract. (The condense prompt *mentions* the markers in its
    # "Do NOT emit ..." instruction, so a bare `@@SKILLS@@ not in` check is the
    # wrong discriminator — key off the extract contract's emit directive.)
    assert "condensing ONE part" in first_chunk          # it IS the condense prompt
    assert "Output contract — STRICT" not in first_chunk  # not the extract contract
    assert "Emit exactly the three markers" not in first_chunk


# ----- build_reduce_prompt: the reduce step ---------------------------------


def _staged_map_reduce_item(tmp_path: Path, monkeypatch, extra: str):
    """Plan an oversized record so it stages as map_reduce; return (cfg, store,
    item) with the map phase NOT yet run (digests absent)."""
    cfg = _cfg(tmp_path, extra)
    store = Store(cfg.store.root)
    content = "abcdefghij" * 30                          # 300 chars, one long line
    monkeypatch.setattr(lc, "_discover", lambda c, **kw: [_rec(content)])
    item = lc.plan_extraction(cfg, store)[0]
    assert item["kind"] == "map_reduce"                  # precondition for this path
    return cfg, store, item


def test_build_reduce_prompt_assembles_single_sourced_contract(tmp_path, monkeypatch) -> None:
    cfg, store, item = _staged_map_reduce_item(tmp_path, monkeypatch, dedent("""\
        budgets:
          max_staged_content_chars: 200
          chunk_content_chars: 120
        prefilter:
          enabled: false
    """))
    # Simulate the map phase: the sub-agent writes one digest per chunk.
    for i, dp in enumerate(item["digest_paths"]):
        Path(dp).write_text(f"digest {i} SENTINEL_{i}\n")
    path = lc.build_reduce_prompt(cfg, store, item)
    assert path is not None
    assert path.endswith("conv-1.prompt.md")             # same name the single path uses
    text = Path(path).read_text()
    # the reduce prompt IS the single-sourced 3-store extract contract...
    assert "@@SKILLS@@" in text and "@@MUST_REMEMBER@@" in text and "@@TOPICS@@" in text
    # ...filled with EVERY chunk digest — no middle dropped (acceptance #1).
    for i in range(len(item["digest_paths"])):
        assert f"SENTINEL_{i}" in text
    assert lc._CLIP_MARKER not in text                   # under ceiling, no guard


def test_build_reduce_prompt_pending_when_a_digest_is_missing(tmp_path, monkeypatch) -> None:
    cfg, store, item = _staged_map_reduce_item(tmp_path, monkeypatch, dedent("""\
        budgets:
          max_staged_content_chars: 200
          chunk_content_chars: 120
        prefilter:
          enabled: false
    """))
    # Map phase incomplete: write all but the last digest.
    for dp in item["digest_paths"][:-1]:
        Path(dp).write_text("partial digest\n")
    assert lc.build_reduce_prompt(cfg, store, item) is None
    assert not (lc._sweep_staging_dir(store) / "conv-1.prompt.md").exists()


def test_build_reduce_prompt_clips_when_digests_overflow_ceiling(tmp_path, monkeypatch) -> None:
    cfg, store, item = _staged_map_reduce_item(tmp_path, monkeypatch, dedent("""\
        budgets:
          max_staged_content_chars: 100
          chunk_content_chars: 120
        prefilter:
          enabled: false
    """))
    # Pathological map phase: digests far exceed the ceiling once concatenated,
    # so the bounded last-resort guard must clip them.
    for dp in item["digest_paths"]:
        Path(dp).write_text("Z" * 500)
    path = lc.build_reduce_prompt(cfg, store, item)
    assert path is not None
    assert lc._CLIP_MARKER in Path(path).read_text()     # bounded guard fired


# ----- chunk_condense.md placeholder re-fit ---------------------------------


def test_chunk_condense_prompt_fills_with_no_leftover_placeholders(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    prompts_root = lc._prompts_root(cfg)
    filled = lc._fill_prompt(
        prompts_root / "chunk_condense.md",
        agent_name="TestTiger",
        chunk_index=2,
        chunk_total=5,
        max_words=200,
        content="the conversation text",
    )
    assert "{" not in filled and "}" not in filled    # every placeholder re-fit
    assert "TestTiger" in filled
    assert "part 2 of 5" in filled
    assert "the conversation text" in filled

"""ADR 0006 Part 2 — per-session high-water-mark (incremental sweep).

Covers the cursor store (``cursor.py``), the turn-parsing + slice computation
in ``lifecycle.py`` (post-cursor slice, bounded read-only overlap window, the
count guard, the Q3 active-session threshold trigger + completed-turn cut, the
single-giant-turn hard case, oversized-slice→chunker composition), and the
crash-between recovery semantics (re-process, never skip).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent

from tigerharness.tiger_memory import lifecycle as lc
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.cursor import (
    Cursor,
    load_cursor,
    load_cursors,
    on_slice_ingested,
    save_cursor,
)
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


def _turns(rows: list[tuple[str, str, str]]) -> str:
    """Render (ts, role, body) rows the way claude_transcript.py dumps them."""
    return "".join(f"[{ts}] {role}:\n{body}\n" for ts, role, body in rows)


def _rec(content: str, *, uuid: str = "conv-1", active: bool = False,
         last: str = "2026-06-29T12:00:00Z") -> SourceRecord:
    first = datetime(2026, 6, 29, tzinfo=timezone.utc)
    last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
    # activity_mtime drives lifecycle._decide: "now" => active (SKIP_ACTIVE),
    # far past => idle (EXTRACT). idle_threshold default is 1h.
    mtime = time.time() if active else 0.0
    return SourceRecord(
        conversation_uuid=uuid, source="claude_code", source_id="sid",
        first_event_at=first, last_event_at=last_dt, activity_mtime=mtime,
        content=content, raw_path=Path("/raw"),
    )


def _plan(cfg, store, rec, monkeypatch) -> list[dict]:
    monkeypatch.setattr(lc, "_discover", lambda c, **kw: [rec])
    return lc.plan_extraction(cfg, store)


def _staged(item: dict) -> str:
    return Path(item["prompt_path"]).read_text()


# ===== cursor store =========================================================


def test_load_cursors_missing_file_is_empty(tmp_path: Path) -> None:
    store = Store(_cfg(tmp_path).store.root)
    assert load_cursors(store) == {}


def test_load_cursors_corrupt_is_empty(tmp_path: Path) -> None:
    store = Store(_cfg(tmp_path).store.root)
    store.init_layout()
    (store.root / ".sweep-cursors.json").write_text("{not json")
    assert load_cursors(store) == {}


def test_load_cursors_non_dict_is_empty(tmp_path: Path) -> None:
    store = Store(_cfg(tmp_path).store.root)
    store.init_layout()
    (store.root / ".sweep-cursors.json").write_text("[1, 2, 3]")
    assert load_cursors(store) == {}


def test_load_cursors_skips_malformed_entries(tmp_path: Path) -> None:
    store = Store(_cfg(tmp_path).store.root)
    store.init_layout()
    (store.root / ".sweep-cursors.json").write_text(dedent("""\
        {
          "good": {"last_event_at": "2026-06-29T00:00:00+00:00", "processed_events": 3},
          "not_a_dict": 5,
          "missing_key": {"processed_events": 1},
          "bad_count": {"last_event_at": "x", "processed_events": "not-an-int"}
        }
    """))
    cursors = load_cursors(store)
    assert set(cursors) == {"good"}
    assert cursors["good"] == Cursor("2026-06-29T00:00:00+00:00", 3)


def test_save_and_load_cursor_round_trip(tmp_path: Path) -> None:
    store = Store(_cfg(tmp_path).store.root)
    save_cursor(store, "c1", Cursor("2026-06-29T01:00:00+00:00", 7))
    assert load_cursor(store, "c1") == Cursor("2026-06-29T01:00:00+00:00", 7)
    assert load_cursor(store, "absent") is None


def test_save_cursor_merges_existing(tmp_path: Path) -> None:
    store = Store(_cfg(tmp_path).store.root)
    save_cursor(store, "c1", Cursor("2026-06-29T01:00:00+00:00", 1))
    save_cursor(store, "c2", Cursor("2026-06-29T02:00:00+00:00", 2))
    assert set(load_cursors(store)) == {"c1", "c2"}


def test_on_slice_ingested_advances(tmp_path: Path) -> None:
    store = Store(_cfg(tmp_path).store.root)
    on_slice_ingested(store, "c1", slice_end_event_at="2026-06-29T03:00:00+00:00",
                      processed_events=4)
    assert load_cursor(store, "c1") == Cursor("2026-06-29T03:00:00+00:00", 4)


# ===== _parse_iso / _parse_turns ===========================================


def test_parse_iso_variants() -> None:
    assert lc._parse_iso(None) is None
    assert lc._parse_iso("") is None
    assert lc._parse_iso("not-a-date") is None
    z = lc._parse_iso("2026-06-29T00:00:00Z")
    assert z is not None and z.tzinfo is not None
    naive = lc._parse_iso("2026-06-29T00:00:00")
    assert naive is not None and naive.tzinfo == timezone.utc


def test_parse_turns_splits_losslessly() -> None:
    content = _turns([
        ("2026-06-29T00:00:00Z", "user", "hello\nworld"),
        ("2026-06-29T01:00:00Z", "assistant", "hi there"),
    ])
    turns = lc._parse_turns(content)
    assert len(turns) == 2
    assert "".join(t.text for t in turns) == content       # lossless
    assert turns[0].event_at.hour == 0 and turns[1].event_at.hour == 1


def test_parse_turns_preamble_before_first_header() -> None:
    content = "leading junk\n" + _turns([("2026-06-29T00:00:00Z", "user", "hi")])
    turns = lc._parse_turns(content)
    assert turns[0].event_at is None                        # preamble has no ts
    assert "leading junk" in turns[0].text
    assert "".join(t.text for t in turns) == content


def test_parse_turns_empty() -> None:
    assert lc._parse_turns("") == []


# ===== slice computation via plan_extraction ===============================


def test_idle_first_pass_stages_all_and_records_boundary(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    content = _turns([
        ("2026-06-29T00:00:00Z", "user", "U1"),
        ("2026-06-29T00:05:00Z", "assistant", "A1"),
        ("2026-06-29T00:10:00Z", "user", "U2"),
    ])
    items = _plan(cfg, store, _rec(content), monkeypatch)
    assert len(items) == 1
    item = items[0]
    assert item["cursor_event_at"] == "2026-06-29T00:10:00+00:00"
    assert item["cursor_events"] == 3
    staged = _staged(item)
    assert "U1" in staged and "U2" in staged               # whole transcript
    assert lc._OVERLAP_OPEN not in staged                  # no overlap on first pass


def test_post_cursor_slice_excludes_pre_cursor_with_overlap(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)                                    # overlap_turns default 4
    store = Store(cfg.store.root)
    store.init_layout()
    content = _turns([
        ("2026-06-29T00:00:00Z", "user", "ALPHA"),
        ("2026-06-29T00:05:00Z", "assistant", "BETA"),
        ("2026-06-29T00:10:00Z", "user", "GAMMA"),
        ("2026-06-29T00:15:00Z", "assistant", "DELTA"),
    ])
    # cursor sits after BETA (2 turns processed).
    save_cursor(store, "conv-1", Cursor("2026-06-29T00:05:00+00:00", 2))
    item = _plan(cfg, store, _rec(content), monkeypatch)[0]
    staged = _staged(item)
    assert lc._OVERLAP_OPEN in staged
    close = staged.index(lc._OVERLAP_CLOSE)
    assert staged.index("ALPHA") < close                   # overlap region
    assert staged.index("BETA") < close
    assert close < staged.index("GAMMA")                   # extraction target
    assert close < staged.index("DELTA")
    assert item["cursor_event_at"] == "2026-06-29T00:15:00+00:00"
    assert item["cursor_events"] == 4


def test_overlap_window_is_bounded(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, "budgets:\n  overlap_turns: 2\n")
    store = Store(cfg.store.root)
    store.init_layout()
    rows = [(f"2026-06-29T00:0{i}:00Z", "user", f"ROW{i}") for i in range(6)]
    content = _turns(rows)
    # cursor after ROW3 (4 turns processed) -> overlap is the last 2 pre: ROW2,ROW3.
    save_cursor(store, "conv-1", Cursor("2026-06-29T00:03:00+00:00", 4))
    item = _plan(cfg, store, _rec(content, last="2026-06-29T00:05:00Z"), monkeypatch)[0]
    staged = _staged(item)
    close = staged.index(lc._OVERLAP_CLOSE)
    overlap = staged[:close]
    assert "ROW2" in overlap and "ROW3" in overlap         # bounded to 2 turns
    assert "ROW0" not in overlap and "ROW1" not in overlap  # older turns dropped
    assert "ROW4" in staged[close:] and "ROW5" in staged[close:]


def test_overlap_zero_disables_window(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path, "budgets:\n  overlap_turns: 0\n")
    store = Store(cfg.store.root)
    store.init_layout()
    content = _turns([
        ("2026-06-29T00:00:00Z", "user", "OLD"),
        ("2026-06-29T00:05:00Z", "user", "NEW"),
    ])
    save_cursor(store, "conv-1", Cursor("2026-06-29T00:00:00+00:00", 1))
    item = _plan(cfg, store, _rec(content, last="2026-06-29T00:05:00Z"), monkeypatch)[0]
    assert lc._OVERLAP_OPEN not in _staged(item)


def test_count_guard_mismatch_forces_full_pass(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    content = _turns([
        ("2026-06-29T00:00:00Z", "user", "ALPHA"),
        ("2026-06-29T00:05:00Z", "assistant", "BETA"),
        ("2026-06-29T00:10:00Z", "user", "GAMMA"),
    ])
    # processed_events is wrong (99 != 1 actual pre-cursor turn) -> drop cursor.
    save_cursor(store, "conv-1", Cursor("2026-06-29T00:00:00+00:00", 99))
    item = _plan(cfg, store, _rec(content), monkeypatch)[0]
    staged = _staged(item)
    assert "ALPHA" in staged                                # pre-cursor re-included
    assert lc._OVERLAP_OPEN not in staged                  # full pass, no overlap
    assert item["cursor_events"] == 3


def test_unparseable_stored_cursor_forces_full_pass(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    content = _turns([
        ("2026-06-29T00:00:00Z", "user", "ALPHA"),
        ("2026-06-29T00:05:00Z", "user", "BETA"),
    ])
    save_cursor(store, "conv-1", Cursor("garbage-timestamp", 1))
    item = _plan(cfg, store, _rec(content, last="2026-06-29T00:05:00Z"), monkeypatch)[0]
    assert "ALPHA" in _staged(item)                         # cursor ignored, full pass
    assert lc._OVERLAP_OPEN not in _staged(item)


def test_no_new_turns_skips(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    content = _turns([("2026-06-29T00:00:00Z", "user", "ONLY")])
    save_cursor(store, "conv-1", Cursor("2026-06-29T00:00:00+00:00", 1))
    assert _plan(cfg, store, _rec(content, last="2026-06-29T00:00:00Z"), monkeypatch) == []


def test_boundary_falls_back_when_no_timestamps(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    # Worklog-style content: no `[ts] role:` headers -> one ts-less turn.
    content = "just some worklog prose\nno headers here\n"
    item = _plan(cfg, store, _rec(content, last="2026-06-29T09:00:00Z"), monkeypatch)[0]
    assert item["cursor_event_at"] == "2026-06-29T09:00:00+00:00"
    assert item["cursor_events"] == 1


# ===== Q3 active-session trigger ===========================================


def _active_cfg(tmp_path: Path) -> object:
    return _cfg(tmp_path, dedent("""\
        budgets:
          active_slice_threshold_chars: 50
        prefilter:
          enabled: false
    """))


def test_active_under_threshold_skips(tmp_path, monkeypatch) -> None:
    cfg = _active_cfg(tmp_path)
    store = Store(cfg.store.root)
    content = _turns([
        ("2026-06-29T00:00:00Z", "user", "small"),
        ("2026-06-29T00:05:00Z", "assistant", "also small"),
    ])
    rec = _rec(content, active=True, last="2026-06-29T00:05:00Z")
    assert _plan(cfg, store, rec, monkeypatch) == []        # SKIP_ACTIVE


def test_active_over_threshold_stages_completed_holds_tail(tmp_path, monkeypatch) -> None:
    cfg = _active_cfg(tmp_path)
    store = Store(cfg.store.root)
    content = _turns([
        ("2026-06-29T00:00:00Z", "user", "X" * 80),         # completed, > 50
        ("2026-06-29T00:05:00Z", "assistant", "LIVETAIL"),  # live tail, held back
    ])
    rec = _rec(content, active=True, last="2026-06-29T00:05:00Z")
    item = _plan(cfg, store, rec, monkeypatch)[0]
    staged = _staged(item)
    assert "LIVETAIL" not in staged                          # tail held for idle pass
    assert item["cursor_event_at"] == "2026-06-29T00:00:00+00:00"
    assert item["cursor_events"] == 1


def test_active_cut_is_whole_turn_boundary(tmp_path, monkeypatch) -> None:
    cfg = _active_cfg(tmp_path)
    store = Store(cfg.store.root)
    content = _turns([
        ("2026-06-29T00:00:00Z", "user", "A" * 40),
        ("2026-06-29T00:05:00Z", "assistant", "B" * 40),    # completed sum > 50
        ("2026-06-29T00:10:00Z", "user", "TAIL"),
    ])
    rec = _rec(content, active=True, last="2026-06-29T00:10:00Z")
    item = _plan(cfg, store, rec, monkeypatch)[0]
    staged = _staged(item)
    assert "A" * 40 in staged and "B" * 40 in staged        # whole completed turns
    assert "TAIL" not in staged                             # cut before the live tail
    assert item["cursor_event_at"] == "2026-06-29T00:05:00+00:00"


def test_active_single_giant_turn_extracts_not_waits(tmp_path, monkeypatch) -> None:
    cfg = _active_cfg(tmp_path)
    store = Store(cfg.store.root)
    # One turn that ALONE exceeds the threshold -> no completed boundary below
    # it -> extract now (don't wait, or the leak re-opens).
    content = _turns([("2026-06-29T00:00:00Z", "user", "Y" * 100)])
    rec = _rec(content, active=True, last="2026-06-29T00:00:00Z")
    items = _plan(cfg, store, rec, monkeypatch)
    assert len(items) == 1                                  # extracted, did not wait
    assert items[0]["cursor_event_at"] == "2026-06-29T00:00:00+00:00"


def test_oversized_slice_composes_with_chunker(tmp_path, monkeypatch) -> None:
    # Q4: an oversized post-cursor slice runs Part 1's chunk-and-reduce.
    cfg = _cfg(tmp_path, dedent("""\
        budgets:
          max_staged_content_chars: 100
          chunk_content_chars: 40
        prefilter:
          enabled: false
    """))
    store = Store(cfg.store.root)
    content = _turns([("2026-06-29T00:00:00Z", "user", "Z" * 300)])  # idle, oversized
    item = _plan(cfg, store, _rec(content, last="2026-06-29T00:00:00Z"), monkeypatch)[0]
    assert item["kind"] == "map_reduce"
    assert item["cursor_event_at"] == "2026-06-29T00:00:00+00:00"


# ===== crash-between recovery (re-process, never skip) =====================


def test_crash_between_reprocesses_same_slice(tmp_path, monkeypatch) -> None:
    """ingest succeeded but the process died before on_slice_ingested ran: the
    cursor still points at the OLD boundary, so the next plan re-stages the SAME
    slice (idempotent re-process) and never skips it."""
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    content = _turns([
        ("2026-06-29T00:00:00Z", "user", "OLD"),
        ("2026-06-29T00:05:00Z", "user", "NEW1"),
        ("2026-06-29T00:10:00Z", "user", "NEW2"),
    ])
    save_cursor(store, "conv-1", Cursor("2026-06-29T00:00:00+00:00", 1))
    first = _plan(cfg, store, _rec(content), monkeypatch)[0]
    # Simulate the crash: do NOT advance the cursor. Re-plan with the SAME
    # (pre-advance) cursor -> the same slice is staged again, not skipped.
    second = _plan(cfg, store, _rec(content), monkeypatch)[0]
    assert first["cursor_event_at"] == second["cursor_event_at"]
    assert "NEW1" in _staged(second) and "NEW2" in _staged(second)

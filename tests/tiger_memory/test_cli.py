"""Tests for the trimmed tiger-memory CLI (cli.py, topic-store revamp).

The retired subcommands (search/drill/tree/raw/bootstrap/resummarize,
import-legacy, migrate-emotional-to-diary) are gone; this covers init /
rebuild / pin / state / migrate-to-topics, the in-session executor glue
(plan / ingest-extraction / build-reduce-prompts / ingest-staged) and the
staged compaction glue (compact-plan / compact-apply).
"""
from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

from tigerharness.tiger_memory import lifecycle as lc
from tigerharness.tiger_memory.bounded_store import BoundedStore
from tigerharness.tiger_memory.cli import main
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.cursor import Cursor, load_cursor
from tigerharness.tiger_memory.entries import (
    STORE_MUST_REMEMBER,
    STORE_SKILLS,
    STORE_TOPICS,
)
from tigerharness.tiger_memory.sources.base import SourceRecord
from tigerharness.tiger_memory.store import Store
from datetime import datetime, timezone


def _cfg_path(tmp_path: Path) -> Path:
    p = tmp_path / "cfg.yaml"
    p.write_text(dedent(f"""\
        agent: {{name: T, role: t}}
        store: {{root: {tmp_path}/memory}}
        sources:
          - kind: claude_code
            project_path: {tmp_path}/proj/
        summarizer: {{backend: anthropic, model: m, prompts: default/v1}}
        rebuild:
          lock_path: {tmp_path}/lock
    """))
    return p


_BUNDLE = dedent("""\
    @@SKILLS@@
    NAME: S
    TRIGGER: t
    PROCEDURE: p
    @@MUST_REMEMBER@@
    KIND: decision
    MEMO: d
    @@TOPICS@@
    TOPIC: NEW
    NAME: Store Revamp
    SUMMARY: the topic-store revamp
    DETAIL: switched the bundle to the topics contract
""")


# ----- config error ---------------------------------------------------------


def test_main_config_error(tmp_path: Path, capsys) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("agent: [unclosed")
    assert main(["--config", str(bad), "state"]) == 2
    assert "config error" in capsys.readouterr().err


# ----- init / state / pin / rebuild -----------------------------------------


def test_init(tmp_path: Path, capsys) -> None:
    cfg_path = _cfg_path(tmp_path)
    assert main(["--config", str(cfg_path), "init"]) == 0
    out = capsys.readouterr().out
    assert "initialised" in out and "journal/" in out


def test_state(tmp_path: Path, capsys) -> None:
    cfg_path = _cfg_path(tmp_path)
    main(["--config", str(cfg_path), "init"])
    capsys.readouterr()  # drain the init output
    assert main(["--config", str(cfg_path), "state"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "stores" in payload and STORE_SKILLS in payload["stores"]


def test_pin(tmp_path: Path, capsys) -> None:
    cfg_path = _cfg_path(tmp_path)
    assert main(["--config", str(cfg_path), "pin", "never push",
                 "--kind", "operator_explicit"]) == 0
    assert "pinned" in capsys.readouterr().out
    cfg = load_config(cfg_path)
    store = Store(cfg.store.root)
    assert len(BoundedStore(cfg, store).load(STORE_MUST_REMEMBER)) == 1


def test_rebuild(tmp_path: Path) -> None:
    cfg_path = _cfg_path(tmp_path)
    assert main(["--config", str(cfg_path), "rebuild"]) == 0
    cfg = load_config(cfg_path)
    assert Store(cfg.store.root).paths.briefing.exists()


# ----- plan / ingest --------------------------------------------------------


def _rec(uuid: str) -> SourceRecord:
    dt = datetime(2026, 6, 1, tzinfo=timezone.utc)
    return SourceRecord(
        conversation_uuid=uuid, source="claude_code", source_id="s",
        first_event_at=dt, last_event_at=dt, activity_mtime=0.0,
        content="transcript", raw_path=Path("/r"),
    )


def test_plan_prints_manifest(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg_path = _cfg_path(tmp_path)
    monkeypatch.setattr(lc, "_discover", lambda c, **kw: [_rec("c1")])
    assert main(["--config", str(cfg_path), "plan"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["items"][0]["conversation_uuid"] == "c1"
    assert payload["stacks"] == [["c1"]]


def test_ingest_extraction_via_stdin(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg_path = _cfg_path(tmp_path)
    monkeypatch.setattr(lc, "_discover", lambda c, **kw: [_rec("c1")])
    main(["--config", str(cfg_path), "plan"])
    capsys.readouterr()
    monkeypatch.setattr("sys.stdin", _Stdin(_BUNDLE))
    assert main(["--config", str(cfg_path), "ingest-extraction", "--uuid", "c1"]) == 0
    out = capsys.readouterr().out
    assert "ingested c1" in out
    assert "1 topic detail(s)" in out
    cfg = load_config(cfg_path)
    store = Store(cfg.store.root)
    bstore = BoundedStore(cfg, store)
    assert len(bstore.load(STORE_SKILLS)) == 1
    assert len(bstore.load(STORE_TOPICS)) == 1


def test_ingest_extraction_unknown_uuid(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg_path = _cfg_path(tmp_path)
    monkeypatch.setattr(lc, "_discover", lambda c, **kw: [_rec("c1")])
    main(["--config", str(cfg_path), "plan"])
    capsys.readouterr()
    monkeypatch.setattr("sys.stdin", _Stdin(_BUNDLE))
    assert main(["--config", str(cfg_path), "ingest-extraction", "--uuid", "nope"]) == 2
    assert "not in plan manifest" in capsys.readouterr().err


def test_ingest_extraction_no_manifest(tmp_path: Path, capsys) -> None:
    cfg_path = _cfg_path(tmp_path)
    load_config(cfg_path)  # ensure store dir computed
    Store(load_config(cfg_path).store.root).init_layout()
    assert main(["--config", str(cfg_path), "ingest-extraction", "--uuid", "x"]) == 2
    assert "no plan manifest" in capsys.readouterr().err


def test_ingest_extraction_malformed_bundle(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg_path = _cfg_path(tmp_path)
    monkeypatch.setattr(lc, "_discover", lambda c, **kw: [_rec("c1")])
    main(["--config", str(cfg_path), "plan"])
    capsys.readouterr()
    monkeypatch.setattr("sys.stdin", _Stdin("no markers"))
    assert main(["--config", str(cfg_path), "ingest-extraction", "--uuid", "c1"]) == 1
    assert "malformed" in capsys.readouterr().err


# ----- ingest-staged --------------------------------------------------------


def test_ingest_staged_processes_cards(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg_path = _cfg_path(tmp_path)
    cfg = load_config(cfg_path)
    store = Store(cfg.store.root)
    monkeypatch.setattr(
        lc, "_discover",
        lambda c, **kw: [_rec("c1"), _rec_distinct("c2")],
    )
    main(["--config", str(cfg_path), "plan"])
    capsys.readouterr()
    # Drop a good card for c1, a malformed card for c2.
    lc._sweep_card_path(store, "c1").write_text(_BUNDLE)
    lc._sweep_card_path(store, "c2").write_text("garbage no markers")
    rc = main(["--config", str(cfg_path), "ingest-staged"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1  # one malformed
    assert out["ingested"] == 1
    assert out["malformed"] == ["c2"]


def test_ingest_staged_skips_missing_cards(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg_path = _cfg_path(tmp_path)
    monkeypatch.setattr(lc, "_discover", lambda c, **kw: [_rec("c1")])
    main(["--config", str(cfg_path), "plan"])
    capsys.readouterr()
    rc = main(["--config", str(cfg_path), "ingest-staged"])  # no cards written
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["ingested"] == 0 and out["skipped_no_card"] == 1


def test_ingest_staged_missing_card_does_not_advance_cursor(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """ADR 0006 Part 2 liveness: a sweep that finds no card for a planned uuid
    must leave that session's cursor UNMOVED, so the post-cursor slice is
    retried next sweep — never silently skipped (the data-loss hazard ADR 0006
    closes). The later sweep that DOES find the card advances the cursor exactly
    to the boundary the plan recorded. This pins the cli-layer advance/liveness
    invariant that ``test_ingest_staged_skips_missing_cards`` checks only at the
    count level."""
    cfg_path = _cfg_path(tmp_path)
    cfg = load_config(cfg_path)
    store = Store(cfg.store.root)
    monkeypatch.setattr(lc, "_discover", lambda c, **kw: [_rec("c1")])
    main(["--config", str(cfg_path), "plan"])
    capsys.readouterr()

    # First sweep: no card staged → skipped, and the cursor must NOT advance.
    assert main(["--config", str(cfg_path), "ingest-staged"]) == 0
    assert json.loads(capsys.readouterr().out)["skipped_no_card"] == 1
    assert load_cursor(store, "c1") is None  # liveness: slice stays retryable

    # Second sweep, same manifest, card now present → ingests and advances once,
    # to exactly the boundary the plan attached (ts-less record → fallback to the
    # record's last_event_at, full turn count = 1).
    lc._sweep_card_path(store, "c1").write_text(_BUNDLE)
    assert main(["--config", str(cfg_path), "ingest-staged"]) == 0
    assert json.loads(capsys.readouterr().out)["ingested"] == 1
    assert load_cursor(store, "c1") == Cursor("2026-06-01T00:00:00+00:00", 1)


def test_ingest_staged_no_manifest(tmp_path: Path, capsys) -> None:
    cfg_path = _cfg_path(tmp_path)
    Store(load_config(cfg_path).store.root).init_layout()
    assert main(["--config", str(cfg_path), "ingest-staged"]) == 2


# ----- build-reduce-prompts (ADR 0006 Part 1 reduce step) -------------------


def _big_rec(uuid: str) -> SourceRecord:
    dt = datetime(2026, 6, 1, tzinfo=timezone.utc)
    return SourceRecord(
        conversation_uuid=uuid, source="claude_code", source_id="s",
        first_event_at=dt, last_event_at=dt, activity_mtime=0.0,
        content="abcdefghij" * 30, raw_path=Path("/r"),   # 300 chars -> map_reduce
    )


def _map_reduce_cfg_path(tmp_path: Path) -> Path:
    p = tmp_path / "cfg.yaml"
    p.write_text(dedent(f"""\
        agent: {{name: T, role: t}}
        store: {{root: {tmp_path}/memory}}
        sources:
          - kind: claude_code
            project_path: {tmp_path}/proj/
        summarizer: {{backend: anthropic, model: m, prompts: default/v1}}
        budgets:
          max_staged_content_chars: 200
          chunk_content_chars: 120
        prefilter:
          enabled: false
        rebuild:
          lock_path: {tmp_path}/lock
    """))
    return p


def test_build_reduce_prompts_no_manifest(tmp_path: Path) -> None:
    cfg_path = _cfg_path(tmp_path)
    Store(load_config(cfg_path).store.root).init_layout()
    assert main(["--config", str(cfg_path), "build-reduce-prompts"]) == 2


def test_build_reduce_prompts_builds_pending_and_skips_single(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    cfg_path = _map_reduce_cfg_path(tmp_path)
    cfg = load_config(cfg_path)
    store = Store(cfg.store.root)
    # small -> single (exercises the kind!=map_reduce skip); big1 fully mapped
    # -> built; big2 missing a digest -> pending.
    monkeypatch.setattr(
        lc, "_discover",
        lambda c, **kw: [_rec("small"), _big_rec("big1"), _big_rec("big2")],
    )
    main(["--config", str(cfg_path), "plan"])
    capsys.readouterr()
    staging = lc._sweep_staging_dir(store)
    manifest = json.loads((staging / "manifest.json").read_text())
    by_uuid = {it["conversation_uuid"]: it for it in manifest["items"]}
    assert by_uuid["small"]["kind"] == "single"
    for dp in by_uuid["big1"]["digest_paths"]:
        Path(dp).write_text("mapped digest\n")
    for dp in by_uuid["big2"]["digest_paths"][:-1]:        # one digest missing
        Path(dp).write_text("mapped digest\n")
    rc = main(["--config", str(cfg_path), "build-reduce-prompts"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["built"] == ["big1"]
    assert out["pending"] == ["big2"]
    assert (staging / "big1.prompt.md").exists()          # reduce prompt assembled
    assert not (staging / "big2.prompt.md").exists()      # still pending


def test_ingest_item_bundle_map_reduce_drops_chunks_and_digests(tmp_path: Path) -> None:
    """A map_reduce item has no ``prompt_path`` (the falsy slot) but carries
    chunk prompts + digests; ingesting it must drop every one of them so no
    transcript content is left at rest (ADR 0006 Part 1)."""
    from tigerharness.tiger_memory.cli import _ingest_item_bundle

    cfg = load_config(_cfg_path(tmp_path))
    store = Store(cfg.store.root)
    store.init_layout()
    chunks = [tmp_path / f"c1.chunk{i:02d}.prompt.md" for i in range(2)]
    digests = [tmp_path / f"c1.chunk{i:02d}.digest.md" for i in range(2)]
    for f in (*chunks, *digests):
        f.write_text("staged transcript content")
    item = {
        "conversation_uuid": "c1",
        "source": "claude_code",
        "kind": "map_reduce",
        # NOTE: no "prompt_path" key -> item.get(...) is None -> the falsy slot.
        "chunk_prompts": [str(c) for c in chunks],
        "digest_paths": [str(d) for d in digests],
    }
    _ingest_item_bundle(cfg, store, item, _BUNDLE)
    for f in (*chunks, *digests):
        assert not f.exists()                          # all staged files consumed


# ----- migrate-to-topics (ADR 0007 one-off) ---------------------------------


def test_migrate_to_topics_dry_run_then_apply(tmp_path: Path, capsys) -> None:
    cfg_path = _cfg_path(tmp_path)
    cfg = load_config(cfg_path)
    store = Store(cfg.store.root)
    store.init_layout()
    diary = store.paths.journal / "diary.md"
    diary.write_text("old diary content\n")

    # Dry-run (no --apply): reports the plan, moves/creates nothing.
    assert main(["--config", str(cfg_path), "migrate-to-topics"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["applied"] is False
    assert out["retired"] == ["diary.md"]
    assert out["topics_created"] is True
    assert diary.exists()                                # untouched
    assert not (store.paths.journal / "topics.md").exists()

    # --apply: retires the file and creates the topics store.
    assert main(["--config", str(cfg_path), "migrate-to-topics", "--apply"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["applied"] is True and out["retired"] == ["diary.md"]
    assert not diary.exists()
    assert (store.root / "retired" / "diary.md").read_text() == \
        "old diary content\n"
    assert (store.paths.journal / "topics.md").exists()

    # Idempotent: a re-run is a no-op report.
    assert main(["--config", str(cfg_path), "migrate-to-topics", "--apply"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["retired"] == [] and out["topics_created"] is False


# ----- compact-plan / compact-apply (ADR 0007 staged compaction) -------------


def _compact_cfg_path(tmp_path: Path) -> Path:
    """Config with a tight must_remember bound so one pinned memo overflows."""
    p = tmp_path / "cfg.yaml"
    p.write_text(dedent(f"""\
        agent: {{name: T, role: t}}
        store: {{root: {tmp_path}/memory}}
        sources:
          - kind: claude_code
            project_path: {tmp_path}/proj/
        summarizer: {{backend: anthropic, model: m, prompts: default/v1}}
        memory:
          must_remember:
            max_length: 20
            overflow_limit: 30
        rebuild:
          lock_path: {tmp_path}/lock
    """))
    return p


def test_compact_plan_empty_when_under_bounds(tmp_path: Path, capsys) -> None:
    cfg_path = _cfg_path(tmp_path)
    main(["--config", str(cfg_path), "init"])
    capsys.readouterr()
    assert main(["--config", str(cfg_path), "compact-plan"]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["targets"] == []
    assert manifest["dropped_stale_topics"] == []


def test_compact_plan_stages_target_when_over(tmp_path: Path, capsys) -> None:
    cfg_path = _compact_cfg_path(tmp_path)
    memo = "a memo well past the thirty char overflow limit"
    main(["--config", str(cfg_path), "pin", memo, "--kind", "decision"])
    capsys.readouterr()
    assert main(["--config", str(cfg_path), "compact-plan"]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert [t["kind"] for t in manifest["targets"]] == ["must_remember"]
    target = manifest["targets"][0]
    assert Path(target["prompt_path"]).exists()          # prompt staged
    assert not Path(target["card_path"]).exists()        # card is the agent's job


def test_compact_apply_no_manifest_exits_2(tmp_path: Path, capsys) -> None:
    cfg_path = _cfg_path(tmp_path)
    Store(load_config(cfg_path).store.root).init_layout()
    assert main(["--config", str(cfg_path), "compact-apply"]) == 2
    assert "no compaction manifest" in capsys.readouterr().err


def test_compact_apply_malformed_card_exits_1(tmp_path: Path, capsys) -> None:
    cfg_path = _compact_cfg_path(tmp_path)
    memo = "a memo well past the thirty char overflow limit"
    main(["--config", str(cfg_path), "pin", memo, "--kind", "decision"])
    capsys.readouterr()
    main(["--config", str(cfg_path), "compact-plan"])
    manifest = json.loads(capsys.readouterr().out)
    target = manifest["targets"][0]
    Path(target["card_path"]).write_text("garbage, no marker")
    assert main(["--config", str(cfg_path), "compact-apply"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["applied"] == []
    assert report["malformed"][0]["key"] == "must_remember"
    assert Path(target["card_path"]).exists()            # kept for inspection


def test_compact_apply_clean_card_exits_0(tmp_path: Path, capsys) -> None:
    cfg_path = _compact_cfg_path(tmp_path)
    memo = "a memo well past the thirty char overflow limit"
    main(["--config", str(cfg_path), "pin", memo, "--kind", "decision"])
    capsys.readouterr()
    main(["--config", str(cfg_path), "compact-plan"])
    manifest = json.loads(capsys.readouterr().out)
    target = manifest["targets"][0]
    Path(target["card_path"]).write_text(dedent("""\
        @@MUST_REMEMBER@@
        KIND: decision
        MEMO: short memo
    """))
    assert main(["--config", str(cfg_path), "compact-apply"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["applied"] == ["must_remember"]
    assert report["malformed"] == [] and report["skipped_no_card"] == []
    # Applied target's staging files are consumed.
    assert not Path(target["prompt_path"]).exists()
    assert not Path(target["card_path"]).exists()
    cfg = load_config(cfg_path)
    entries = BoundedStore(cfg, Store(cfg.store.root)).load(STORE_MUST_REMEMBER)
    assert [e.text for e in entries] == ["short memo"]


def test_compact_apply_skipped_no_card_exits_0(tmp_path: Path, capsys) -> None:
    cfg_path = _compact_cfg_path(tmp_path)
    memo = "a memo well past the thirty char overflow limit"
    main(["--config", str(cfg_path), "pin", memo, "--kind", "decision"])
    main(["--config", str(cfg_path), "compact-plan"])
    capsys.readouterr()
    assert main(["--config", str(cfg_path), "compact-apply"]) == 0  # no card yet
    report = json.loads(capsys.readouterr().out)
    assert report["skipped_no_card"] == ["must_remember"]


# ----- check ------------------------------------------------------------------


def test_check_clean_store(tmp_path: Path, capsys) -> None:
    cfg_path = _cfg_path(tmp_path)
    main(["--config", str(cfg_path), "init"])
    capsys.readouterr()
    assert main(["--config", str(cfg_path), "check"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert {s["store"] for s in payload["stores"]} == \
        {STORE_SKILLS, STORE_MUST_REMEMBER, STORE_TOPICS}


def _rec_distinct(uuid: str) -> SourceRecord:
    dt = datetime(2026, 6, 2, tzinfo=timezone.utc)
    return SourceRecord(
        conversation_uuid=uuid, source="claude_code", source_id="s2",
        first_event_at=dt, last_event_at=dt, activity_mtime=0.0,
        content="transcript2", raw_path=Path("/r2"),
    )


class _Stdin:
    def __init__(self, text: str):
        self._text = text

    def read(self) -> str:
        return self._text

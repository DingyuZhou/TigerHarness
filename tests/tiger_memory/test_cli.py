"""Tests for the trimmed tiger-memory CLI (cli.py, bounded-store revamp).

The retired subcommands (search/drill/tree/raw/bootstrap/resummarize) are
gone; this covers init / rebuild / pin / state and the in-session executor
glue (plan / ingest-extraction / ingest-staged).
"""
from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import pytest

from tigerharness.tiger_memory import lifecycle as lc
from tigerharness.tiger_memory.bounded_store import BoundedStore
from tigerharness.tiger_memory.cli import main
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.entries import (
    STORE_EMOTIONAL,
    STORE_MUST_REMEMBER,
    STORE_SKILLS,
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
    @@EMOTIONAL@@
    NONE
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
                 "--kind", "owner_explicit"]) == 0
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
    cfg = load_config(cfg_path)
    store = Store(cfg.store.root)
    assert len(BoundedStore(cfg, store).load(STORE_SKILLS)) == 1


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


def test_ingest_staged_no_manifest(tmp_path: Path, capsys) -> None:
    cfg_path = _cfg_path(tmp_path)
    Store(load_config(cfg_path).store.root).init_layout()
    assert main(["--config", str(cfg_path), "ingest-staged"]) == 2


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

"""Practicality-pass branches: source-dating fallbacks, staleness notice,
sweep-complete pending refusal, sweep config validation."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent

import pytest

from tigerharness.tiger_memory import briefing as bf
from tigerharness.tiger_memory import lifecycle as lc
from tigerharness.tiger_memory import sweep
from tigerharness.tiger_memory.config import ConfigError, load_config
from tigerharness.tiger_memory.cursor import Cursor, save_cursor
from tigerharness.tiger_memory.executor import _source_stamp
from tigerharness.tiger_memory.store import Store

NOW = "2026-08-02T12:00:00+00:00"


def _cfg(tmp_path: Path, extra: str = ""):
    p = tmp_path / "cfg.yaml"
    p.write_text(dedent(f"""\
        agent:
          name: Aya
          role: "t"
        store:
          root: {tmp_path}/memory
        sources:
          - kind: claude_code
            project_path: {tmp_path}/fake/
        summarizer:
          backend: anthropic
          model: claude-opus-4-7
          prompts: default/v1
        rebuild:
          lock_path: {tmp_path}/t.lock
        """) + extra)
    return load_config(p)


# ----- executor._source_stamp fallbacks -------------------------------------


def test_source_stamp_paths():
    old = "2026-06-01T10:00:00+00:00"
    assert _source_stamp(old, NOW) == old            # older source wins
    assert _source_stamp(None, NOW) == NOW           # missing → now
    assert _source_stamp("garbage", NOW) == NOW      # unparseable → now
    future = "2027-01-01T00:00:00+00:00"
    assert _source_stamp(future, NOW) == NOW         # future → now


# ----- lifecycle._latest_ts unparseable sides --------------------------------


def test_latest_ts_unparseable_loses():
    good = "2026-08-01T00:00:00+00:00"
    assert lc._latest_ts("garbage", good) == good
    assert lc._latest_ts(good, "garbage") == good
    assert lc._latest_ts(good, "2026-07-01T00:00:00+00:00") == good


# ----- briefing notice: data-through vs nothing-ingested ---------------------


def test_notice_data_through_and_empty(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.store.root)
    store.init_layout()
    bf.rebuild_briefing(cfg, store)
    notice = (store.paths.briefing / bf.NOTICE_NAME).read_text()
    assert "Nothing has been ingested into this memory yet" in notice

    save_cursor(store, "u1", Cursor("2026-07-30T09:00:00+00:00", 3))
    save_cursor(store, "u2", Cursor("2026-08-01T18:30:00+00:00", 5))
    # An older cursor that sorts AFTER the max (cursor files may be
    # key-sorted) exercises the scan's keep-current path.
    save_cursor(store, "u9", Cursor("2026-07-01T00:00:00+00:00", 1))
    # Force a re-render (fingerprint covers store files, not cursors).
    (store.paths.briefing / bf.FINGERPRINT_NAME).unlink()
    bf.rebuild_briefing(cfg, store)
    notice = (store.paths.briefing / bf.NOTICE_NAME).read_text()
    assert "through 2026-08-01 18:30Z" in notice
    assert "SAY how fresh your memory is" in notice


# ----- sweep-complete refuses while personas are pending ---------------------


def _roster(tmp_path: Path, names):
    mem = tmp_path / "mem"
    (tmp_path / "configs").mkdir()
    lines = ["personas:"]
    for n in names:
        d = mem / n
        d.mkdir(parents=True)
        (d / "tiger-memory.config.yaml").write_text("agent:\n  name: " + n)
        lines.append(f"  - name: {n}")
    (tmp_path / "configs" / "personas.yaml").write_text("\n".join(lines))
    return mem


def test_sweep_complete_pending_refusal_and_force(tmp_path, caplog):
    mem = _roster(tmp_path, ["A", "B"])
    now = datetime(2026, 8, 2, tzinfo=timezone.utc)
    sweep.record_persona_done(mem, "A", now=now)
    assert sweep.mark_sweep_complete(mem, now) is False      # B pending
    assert "not recorded done" in caplog.text
    assert sweep.read_sweep_state(mem).get("last_sweep_at") is None
    assert sweep.mark_sweep_complete(mem, now, force=True) is True
    # And with everyone done, no force needed.
    sweep.record_persona_done(mem, "A", now=now)
    sweep.record_persona_done(mem, "B", now=now)
    assert sweep.mark_sweep_complete(mem, now) is True


# ----- sweep: config validation ----------------------------------------------


@pytest.mark.parametrize("body, needle", [
    ("sweep:\n  floor_hours: 0\n", "floor_hours"),
    ("sweep:\n  lease_seconds: -1\n", "lease_seconds"),
    ("sweep:\n  max_personas: 0\n", "max_personas"),
])
def test_sweep_config_validation(tmp_path, body, needle):
    with pytest.raises(ConfigError, match=needle):
        _cfg(tmp_path, body)


def test_sweep_config_values(tmp_path):
    cfg = _cfg(tmp_path, "sweep:\n  floor_hours: 12\n  max_personas: 5\n")
    assert cfg.sweep.floor_hours == 12.0
    assert cfg.sweep.max_personas == 5
    assert cfg.sweep.lease_seconds == 1800.0

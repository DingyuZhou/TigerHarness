"""Coverage for the 2026-08-01 hardening pass.

Targets the residual gaps left by the lock/token/repair hardening in
``tiger_memory``: the TOCTOU-safe lockfile reclaim edges (store.py), the
store-lock wait/retry ladder (bounded_store.py), check --fix's degraded
and repair paths (check.py), the CLI's lock/token failure exits (cli.py),
must_remember card-tolerance + STALE refusal (compaction.py), lock-held
early-outs in pin/rebuild (lifecycle.py), the sweep claim-token guard
(sweep.py), and the team event log's append cap + crashed-apply merge
(team_events.py). Pure Python, no model calls, no real waiting (sleeps
are monkeypatched where a lock ladder would otherwise spin ~2s).
"""
from __future__ import annotations

import io
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from tigerharness.tiger_memory import bounded_store as bs_mod
from tigerharness.tiger_memory import check as chk
from tigerharness.tiger_memory import cli
from tigerharness.tiger_memory import compaction as cp
from tigerharness.tiger_memory import lifecycle as lc
from tigerharness.tiger_memory import sweep as sw
from tigerharness.tiger_memory import team_events as tev
from tigerharness.tiger_memory.bounded_store import BoundedStore, StoreLockHeld
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.entries import (
    STORE_MUST_REMEMBER,
    MustRememberEntry,
)
from tigerharness.tiger_memory.store import (
    Store,
    reclaim_lockfile,
    release_lockfile,
)

NOW = "2026-07-23T12:00:00Z"
NOW_DT = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
STALE_TS = "2026-01-02T00:00:00Z"
DEAD_PID = 999999  # almost certainly not a live process


# ----- helpers ---------------------------------------------------------------


def make_env(tmp_path: Path, memory: dict | None = None):
    """A loaded config + initialized store (mirrors test_compaction idiom)."""
    raw = {
        "agent": {"name": "Aya", "role": "r"},
        "store": {"root": str(tmp_path / "memory")},
        "sources": [{"kind": "claude_code", "project_path": f"{tmp_path}/p/"}],
        "summarizer": {
            "backend": "anthropic", "model": "m", "prompts": "default/v1",
        },
        "rebuild": {"lock_path": str(tmp_path / "test.lock")},
    }
    if memory is not None:
        raw["memory"] = memory
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(yaml.safe_dump(raw))
    cfg = load_config(cfg_path)
    store = Store(cfg.store.root)
    store.init_layout()
    return cfg, cfg_path, store, BoundedStore(cfg, store)


def _memo(
    text: str = "memo",
    *,
    kind: str = "preference",
    last: str = NOW,
    created: str = "2026-01-01T00:00:00Z",
) -> MustRememberEntry:
    return MustRememberEntry(
        text=text, created_at=created, last_used=last, source="test",
        kind=kind,
    )


def _hold_live(path: Path) -> None:
    """Stamp *path* as a lockfile held by THIS (live) process."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()} 0")


@pytest.fixture
def no_sleep(monkeypatch):
    """Collapse lock-wait ladders (40 x 0.05s) to instant retries."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)


# ----- store.py: reclaim_lockfile / release_lockfile edges -------------------


def test_reclaim_lockfile_lost_rename_race_returns_false(tmp_path):
    # Path already gone: another process reclaimed/released it first.
    assert reclaim_lockfile(tmp_path / "absent.lock") is False


def test_reclaim_lockfile_restores_live_holder(tmp_path):
    lock = tmp_path / "x.lock"
    _hold_live(lock)
    assert reclaim_lockfile(lock) is False
    # The live holder's lock is renamed back, tombstone dropped.
    assert lock.read_text() == f"{os.getpid()} 0"
    assert list(tmp_path.glob("*.stale.*")) == []


def test_reclaim_lockfile_live_holder_young_is_not_stolen(tmp_path):
    lock = tmp_path / "x.lock"
    _hold_live(lock)  # fresh mtime: age below the steal threshold
    assert reclaim_lockfile(lock, allow_if_alive_older_than=3600) is False
    assert lock.exists()


def test_reclaim_lockfile_age_based_steal_of_live_holder(tmp_path):
    lock = tmp_path / "x.lock"
    _hold_live(lock)
    old = time.time() - 500
    os.utime(lock, (old, old))
    assert reclaim_lockfile(lock, allow_if_alive_older_than=60) is True
    assert not lock.exists()
    assert list(tmp_path.glob("*.stale.*")) == []


def test_release_lockfile_owner_mismatch_is_noop(tmp_path):
    lock = tmp_path / "x.lock"
    lock.write_text(f"{DEAD_PID} 0")
    release_lockfile(lock)  # not ours: must not unlink
    assert lock.exists()


# ----- bounded_store.py: store_lock_wait / _acquire_store_lock ---------------


def test_store_lock_wait_retries_exhausted_raises(tmp_path, no_sleep):
    _, _, _, bstore = make_env(tmp_path)
    _hold_live(bstore._lock_path(STORE_MUST_REMEMBER))
    with pytest.raises(StoreLockHeld, match="live session"):
        with bstore.store_lock_wait(STORE_MUST_REMEMBER, retries=3,
                                    delay=0.001):
            pytest.fail("must not enter the body")  # pragma: no cover


def test_store_lock_wait_acquires_after_holder_releases(tmp_path, monkeypatch):
    _, _, _, bstore = make_env(tmp_path)
    lock = bstore._lock_path(STORE_MUST_REMEMBER)
    _hold_live(lock)
    sleeps: list[float] = []

    def sleep_and_release(delay: float) -> None:
        sleeps.append(delay)
        lock.unlink(missing_ok=True)  # holder finishes between retries

    monkeypatch.setattr(time, "sleep", sleep_and_release)
    entered = False
    with bstore.store_lock_wait(STORE_MUST_REMEMBER, delay=0.001):
        entered = True
    assert entered and sleeps  # failed at least once, then won the retry
    assert not lock.exists()  # released on exit


def test_store_lock_wait_body_exception_is_not_retried(tmp_path, monkeypatch):
    _, _, _, bstore = make_env(tmp_path)
    monkeypatch.setattr(
        time, "sleep",
        lambda _s: pytest.fail("body-raised StoreLockHeld must not retry"),
    )
    with pytest.raises(StoreLockHeld, match="from the body"):
        with bstore.store_lock_wait(STORE_MUST_REMEMBER):
            raise StoreLockHeld("from the body")


def test_acquire_store_lock_lost_reclaim_race_returns_false(
    tmp_path, monkeypatch
):
    _, _, _, bstore = make_env(tmp_path)
    lock = bstore._lock_path(STORE_MUST_REMEMBER)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(f"{DEAD_PID} 0")  # dead holder -> reclaim attempted
    monkeypatch.setattr(bs_mod, "reclaim_lockfile", lambda _p: False)
    with pytest.raises(StoreLockHeld):
        with bstore.store_lock(STORE_MUST_REMEMBER):
            pytest.fail("lost reclaim race must not acquire")  # pragma: no cover


# ----- check.py: _ts_parseable + --fix repair / degraded paths ---------------


def test_ts_parseable_falsy_on_garbage():
    assert chk._ts_parseable("not-a-timestamp") is False
    assert chk._ts_parseable(NOW) is True


def _corrupt_field(bstore: BoundedStore, field_line: str, garbage_line: str):
    path = bstore._store_path(STORE_MUST_REMEMBER)
    text = path.read_text(encoding="utf-8")
    assert field_line in text
    path.write_text(text.replace(field_line, garbage_line), encoding="utf-8")


def test_check_fix_repairs_unparseable_last_used_to_created_at(tmp_path):
    _, _, _, bstore = make_env(tmp_path)
    e = _memo(created="2026-07-01T00:00:00+00:00",
              last="2026-07-02T00:00:00+00:00")
    bstore.save_atomic(STORE_MUST_REMEMBER, [e])
    _corrupt_field(
        bstore,
        "last_used: '2026-07-02T00:00:00+00:00'",
        "last_used: garbage-ts",
    )
    res = chk.check_store(bstore, STORE_MUST_REMEMBER, fix=True)
    assert res.repaired is True
    assert any("unparseable last_used" in p for p in res.problems)
    [reloaded] = bstore.load(STORE_MUST_REMEMBER)
    assert reloaded.last_used == "2026-07-01T00:00:00+00:00"


def test_check_fix_falls_back_to_iso_now_when_created_at_corrupt(tmp_path):
    _, _, _, bstore = make_env(tmp_path)
    e = _memo(created="2026-07-01T00:00:00+00:00",
              last="2026-07-02T00:00:00+00:00")
    bstore.save_atomic(STORE_MUST_REMEMBER, [e])
    _corrupt_field(
        bstore,
        "created_at: '2026-07-01T00:00:00+00:00'",
        "created_at: also-garbage",
    )
    _corrupt_field(
        bstore,
        "last_used: '2026-07-02T00:00:00+00:00'",
        "last_used: garbage-ts",
    )
    res = chk.check_store(bstore, STORE_MUST_REMEMBER, fix=True)
    assert res.repaired is True
    [reloaded] = bstore.load(STORE_MUST_REMEMBER)
    assert chk._ts_parseable(reloaded.last_used)  # iso_now fallback
    assert reloaded.last_used != "garbage-ts"


def test_check_readonly_reports_unparseable_last_used_without_repair(
    tmp_path,
):
    _, _, _, bstore = make_env(tmp_path)
    e = _memo(created="2026-07-01T00:00:00+00:00",
              last="2026-07-02T00:00:00+00:00")
    bstore.save_atomic(STORE_MUST_REMEMBER, [e])
    _corrupt_field(
        bstore,
        "last_used: '2026-07-02T00:00:00+00:00'",
        "last_used: garbage-ts",
    )
    res = chk.check_store(bstore, STORE_MUST_REMEMBER, fix=False)
    assert res.repaired is False
    assert any("unparseable last_used" in p for p in res.problems)
    # Read-only: the corrupt anchor is reported but left in place.
    [reloaded] = bstore.load(STORE_MUST_REMEMBER)
    assert reloaded.last_used == "garbage-ts"


def test_check_fix_with_live_lock_degrades_to_read_only(
    tmp_path, no_sleep, caplog
):
    _, _, _, bstore = make_env(tmp_path)
    bstore.save_atomic(STORE_MUST_REMEMBER, [_memo("kept")])
    _hold_live(bstore._lock_path(STORE_MUST_REMEMBER))
    with caplog.at_level(logging.WARNING,
                         logger="tigerharness.tiger_memory.check"):
        res = chk.check_store(bstore, STORE_MUST_REMEMBER, fix=True)
    assert res.repaired is False
    assert any("read-only" in r.message for r in caplog.records)


# ----- cli.py: lock/token failure exits --------------------------------------


def _plan_manifest(store: Store, uuid: str = "c1") -> Path:
    staging = store.root / ".sweep-staging"
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "manifest.json").write_text(
        json.dumps({"items": [{"conversation_uuid": uuid}]}),
        encoding="utf-8",
    )
    return staging


def test_cli_ingest_extraction_store_locked_exits_1(
    tmp_path, monkeypatch, capsys
):
    _, cfg_path, store, _ = make_env(tmp_path)
    _plan_manifest(store)

    def raise_locked(_cfg, _store, _item, _bundle):
        raise StoreLockHeld("held by a live session")

    monkeypatch.setattr(cli, "_ingest_item_bundle", raise_locked)
    monkeypatch.setattr("sys.stdin", io.StringIO("a bundle"))
    rc = cli.main(
        ["--config", str(cfg_path), "ingest-extraction", "--uuid", "c1"]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "store locked for c1" in err and "cursor untouched" in err


def test_cli_ingest_staged_locked_lists_uuid_and_warns_zero_ingested(
    tmp_path, monkeypatch, capsys
):
    _, cfg_path, store, _ = make_env(tmp_path)
    staging = _plan_manifest(store)
    card = staging / "c1.extract.md"
    card.write_text("a staged card", encoding="utf-8")

    def raise_locked(_cfg, _store, _item, _bundle):
        raise StoreLockHeld("held by a live session")

    monkeypatch.setattr(cli, "_ingest_item_bundle", raise_locked)
    rc = cli.main(["--config", str(cfg_path), "ingest-staged"])
    assert rc == 0  # locked is not malformed: uuid re-ingests next sweep
    out, err = capsys.readouterr()
    payload = json.loads(out)
    assert payload["locked"] == ["c1"] and payload["ingested"] == 0
    assert "store locked for c1" in err
    assert "warning: nothing ingested" in err and "1 locked" in err
    assert card.exists()  # kept for the retry


def test_cli_sweep_complete_token_mismatch_exits_3(tmp_path, capsys):
    cfg, cfg_path, _, _ = make_env(tmp_path)
    team_dir = cfg.store.root.parent
    sw.write_sweep_state(team_dir, {"claim_token": "owner-token"})
    rc = cli.main([
        "--config", str(cfg_path), "sweep-complete",
        "--now", "2026-08-01T12:00:00+00:00", "--token", "stale-token",
    ])
    assert rc == 3
    assert "sweep-complete refused" in capsys.readouterr().err
    state = sw.read_sweep_state(team_dir)
    assert state.get("claim_token") == "owner-token"
    assert "last_sweep_at" not in state  # watermark untouched


def test_cli_sweep_release_token_mismatch_exits_3(tmp_path, capsys):
    cfg, cfg_path, _, _ = make_env(tmp_path)
    team_dir = cfg.store.root.parent
    sw.write_sweep_state(team_dir, {"claim_token": "owner-token"})
    rc = cli.main(
        ["--config", str(cfg_path), "sweep-release", "--token", "stale-token"]
    )
    assert rc == 3
    assert "sweep-release refused" in capsys.readouterr().err
    assert sw.read_sweep_state(team_dir).get("claim_token") == "owner-token"


# ----- compaction.py: locked target, card tolerance, STALE refusal -----------


def _stage_mr(store: Store, card_text: str):
    staging = store.root / cp.STAGING_DIR_NAME
    staging.mkdir(parents=True, exist_ok=True)
    target = {
        "kind": cp.KIND_MUST_REMEMBER,
        "key": "must_remember",
        "prompt_path": str(staging / "must_remember.prompt.md"),
        "card_path": str(staging / "must_remember.card.md"),
    }
    (staging / "manifest.json").write_text(
        json.dumps({
            "generated_at": NOW, "dropped_stale_topics": [],
            "targets": [target],
        }),
        encoding="utf-8",
    )
    (staging / "must_remember.card.md").write_text(card_text, encoding="utf-8")
    return staging


def test_compact_apply_locked_store_reports_locked_keeps_staging(tmp_path):
    cfg, _, store, bstore = make_env(tmp_path)
    bstore.save_atomic(STORE_MUST_REMEMBER, [_memo("survivor")])
    staging = _stage_mr(
        store, "@@MUST_REMEMBER@@\nKIND: preference\nMEMO: new memo\n"
    )
    _hold_live(bstore._lock_path(STORE_MUST_REMEMBER))
    report = cp.compact_apply(cfg, store, now=NOW)
    assert report.locked == ["must_remember"]
    assert report.applied == []
    # Locked target keeps its card AND the manifest for a targeted retry.
    assert (staging / "must_remember.card.md").exists()
    assert (staging / "manifest.json").exists()
    assert [e.text for e in bstore.load(STORE_MUST_REMEMBER)] == ["survivor"]


def test_apply_block_combining_stale_and_memo_keeps_both(tmp_path):
    cfg, _, store, bstore = make_env(tmp_path, memory={
        "must_remember": {"max_length": 2000, "overflow_limit": 3000},
    })
    op_stale = _memo("stale directive", kind="operator_explicit",
                     last=STALE_TS)
    bstore.save_atomic(STORE_MUST_REMEMBER, [op_stale])
    # One sloppy block: a STALE verdict AND a memo, no blank line between.
    _stage_mr(store, (
        "@@MUST_REMEMBER@@\n"
        f"STALE: {op_stale.id}\n"
        "KIND: preference\n"
        "MEMO: merged memo\n"
    ))
    report = cp.compact_apply(cfg, store, now=NOW)
    assert report.applied == ["must_remember"]
    entries = {e.text: e for e in bstore.load(STORE_MUST_REMEMBER)}
    assert entries["stale directive"].kind == "decision"  # verdict applied
    assert entries["merged memo"].kind == "preference"    # memo NOT dropped


def test_apply_stale_verdict_on_fresh_operator_directive_refused(
    tmp_path, caplog
):
    cfg, _, store, bstore = make_env(tmp_path, memory={
        "must_remember": {"max_length": 2000, "overflow_limit": 3000},
    })
    op_fresh = _memo("fresh directive", kind="operator_explicit", last=NOW)
    bstore.save_atomic(STORE_MUST_REMEMBER, [op_fresh])
    _stage_mr(store, f"@@MUST_REMEMBER@@\nSTALE: {op_fresh.id}\n")
    with caplog.at_level(logging.WARNING,
                         logger="tigerharness.tiger_memory.compaction"):
        report = cp.compact_apply(cfg, store, now=NOW)
    assert report.applied == ["must_remember"]
    [reloaded] = bstore.load(STORE_MUST_REMEMBER)
    assert reloaded.kind == "operator_explicit"  # protection kept
    assert any("refused" in r.message for r in caplog.records)


def test_render_mr_blocks_corrupt_timestamp_age_label():
    corrupt = _memo("corrupt anchor", last="garbage-ts")
    text = cp._render_mr_blocks([corrupt], now=NOW, forget_days=30)
    assert "AGE: untouched unknown (corrupt timestamp) [forget-eligible]" \
        in text


# ----- lifecycle.py: pin / rebuild lock-held early-outs ----------------------


def test_pin_returns_1_when_store_lock_held_by_live_pid(
    tmp_path, no_sleep, capsys
):
    cfg, _, store, bstore = make_env(tmp_path)
    _hold_live(bstore._lock_path(STORE_MUST_REMEMBER))
    rc = lc.pin(cfg, store, memo="urgent note", kind="preference")
    assert rc == 1
    assert "pin failed" in capsys.readouterr().out
    assert bstore.load(STORE_MUST_REMEMBER) == []  # nothing half-written


def test_rebuild_skips_when_rebuild_lock_held_by_live_pid(tmp_path, caplog):
    cfg, _, store, _ = make_env(tmp_path)
    lock = Path(cfg.rebuild.lock_path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()))  # live holder, fresh mtime
    with caplog.at_level(logging.INFO,
                         logger="tigerharness.tiger_memory.lifecycle"):
        assert lc.rebuild(cfg, store) == 0
    assert any("skipping" in r.message for r in caplog.records)
    # The live holder's lock is untouched (not released by the skipper).
    assert lock.read_text() == str(os.getpid())


# ----- sweep.py: claim-token guard + state-shape tolerance --------------------


def test_release_sweep_claim_token_mismatch_refused(tmp_path):
    sw.write_sweep_state(tmp_path, {"claim_token": "owner", "progress": ["A"]})
    assert sw.release_sweep_claim(tmp_path, token="stale") is False
    state = sw.read_sweep_state(tmp_path)
    assert state["claim_token"] == "owner"  # live owner's claim untouched
    assert state["progress"] == ["A"]


def test_mark_sweep_complete_token_mismatch_refused(tmp_path):
    sw.write_sweep_state(tmp_path, {"claim_token": "owner"})
    assert sw.mark_sweep_complete(tmp_path, NOW_DT, token="stale") is False
    state = sw.read_sweep_state(tmp_path)
    assert state["claim_token"] == "owner"
    assert "last_sweep_at" not in state  # watermark not advanced


def test_matching_token_is_honored(tmp_path):
    # The conditional mutation still goes through when the token matches.
    sw.write_sweep_state(tmp_path, {"claim_token": "owner"})
    assert sw.release_sweep_claim(tmp_path, token="owner") is True
    assert "claim_token" not in sw.read_sweep_state(tmp_path)


def test_persona_done_at_tolerates_non_dict_state(tmp_path):
    sw.write_sweep_state(tmp_path, {"done_at": "not-a-dict"})
    assert sw.persona_done_at(tmp_path) == {}


def test_record_persona_done_without_claim_does_not_refresh_lease(tmp_path):
    sw.write_sweep_state(tmp_path, {})  # no claim_token
    sw.record_persona_done(tmp_path, "Aya", now=NOW_DT)
    state = sw.read_sweep_state(tmp_path)
    assert state["progress"] == ["Aya"]
    assert state["done_at"]["Aya"] == NOW_DT.isoformat()
    assert "claim_at" not in state  # no lease to renew


# ----- team_events.py: append cap, locked apply, crashed-apply merge ---------


def test_append_events_caps_at_three_per_card(tmp_path, caplog):
    cfg, _, _, _ = make_env(tmp_path)
    with caplog.at_level(logging.WARNING,
                         logger="tigerharness.tiger_memory.team_events"):
        n = tev.append_events(
            cfg, persona="Aya", day="2026-08-01",
            events=["did one", "did two", "did three", "did four", "did five"],
            now="2026-08-01T12:00:00+00:00",
        )
    assert n == 3
    assert any("capped" in r.message for r in caplog.records)
    text = tev.events_path(cfg).read_text(encoding="utf-8")
    assert "- Aya did three." in text
    assert "did four" not in text and "did five" not in text


def _stage_te_fold(cfg, *, period="2026-06", sources=("2026-06-01",),
                   snapshot=None, card_bullets=("- Aya did x.",)):
    staging = tev._staging_dir(cfg)
    staging.mkdir(parents=True, exist_ok=True)
    key = f"month.{period}"
    item = {
        "kind": tev.KIND_MONTH,
        "key": key,
        "period": period,
        "prompt_path": str(staging / f"{key}.prompt.md"),
        "card_path": str(staging / f"{key}{tev.CARD_SUFFIX}"),
        "source_periods": list(sources),
        "snapshot": snapshot or {},
    }
    (staging / "manifest.json").write_text(
        json.dumps({
            "generated_at": NOW, "dropped_periods": [], "targets": [item],
        }),
        encoding="utf-8",
    )
    Path(item["card_path"]).write_text(
        tev.MARK_TEAM_EVENTS + "\n" + "\n".join(card_bullets) + "\n",
        encoding="utf-8",
    )
    return staging, item


def test_team_events_apply_locked_target_keeps_manifest(tmp_path, no_sleep):
    cfg, _, _, _ = make_env(tmp_path)
    path = tev.events_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Team event log\n\n## 2026-06-01\n- Aya did x.\n", encoding="utf-8"
    )
    staging, item = _stage_te_fold(
        cfg, snapshot={"2026-06-01": ["- Aya did x."]}
    )
    # The lock lands AFTER staging (compact_plan would take it itself).
    _hold_live(tev.team_dir(cfg) / tev.LOCK_FILENAME)
    report = tev.compact_apply(cfg)
    assert report["locked"] == ["month.2026-06"]
    assert report["applied"] == []
    # Locked target keeps card + manifest so a targeted retry is possible.
    assert Path(item["card_path"]).exists()
    assert (staging / "manifest.json").exists()
    assert "## 2026-06-01" in path.read_text(encoding="utf-8")  # untouched


def test_team_events_apply_merges_into_existing_same_period_section(tmp_path):
    # A crashed earlier apply left a folded ``## 2026-06`` behind; the
    # re-apply must merge into it (deduped), never append a duplicate
    # section.
    cfg, _, _, _ = make_env(tmp_path)
    path = tev.events_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Team event log\n\n"
        "## 2026-06-01\n- Aya did x.\n\n"
        "## 2026-06\n- Aya did x.\n- Ben did y.\n",
        encoding="utf-8",
    )
    staging, _ = _stage_te_fold(
        cfg,
        snapshot={"2026-06-01": ["- Aya did x."]},
        card_bullets=("- Aya did x.", "- Cal did z."),
    )
    report = tev.compact_apply(cfg)
    assert report["applied"] == ["month.2026-06"]
    sections = tev.load_sections(path)
    assert [s.period for s in sections] == ["2026-06"]  # single section
    [merged] = sections
    # Card bullets first, then the crashed apply's survivors, deduped.
    assert merged.lines == ["- Aya did x.", "- Cal did z.", "- Ben did y."]
    assert not (staging / "manifest.json").exists()  # clean apply consumed it

"""Split-gate team sweep (own-persona floor bypass) — plan.md §3 matrix.

Covers the claim-scope marker (`scope: "own-only"` vs `"team"`), the
target-composition matrix, the watermark-postponement guard (an own-only
run leaves `last_sweep_at` untouched), the missing-scope compat rule,
the `has_pending_source` pending check (live-session exclusion), and
the `sweep-plan --own-persona / --exclude-session` CLI contract.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from textwrap import dedent

from tigerharness.tiger_memory import lifecycle as lc
from tigerharness.tiger_memory.cli import main
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.cursor import Cursor, save_cursor
from tigerharness.tiger_memory.sources.base import SourceRecord
from tigerharness.tiger_memory.store import Store
from tigerharness.tiger_memory.sweep import (
    mark_sweep_complete,
    maybe_sweep_roster,
    plan_team_sweep,
    read_sweep_state,
    record_persona_done,
    release_sweep_claim,
    try_claim_sweep,
    write_sweep_state,
)

NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)


def _iso(delta_hours: float) -> str:
    return (NOW - timedelta(hours=delta_hours)).isoformat()


_ROSTER = (
    "personas:\n"
    "  - name: Ayako\n"
    "  - name: Anzai\n"
    "  - name: Sakuragi\n"
)


def _make_team(tmp_path: Path, *, with_config=("Ayako", "Anzai", "Sakuragi")):
    (tmp_path / "configs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "configs" / "personas.yaml").write_text(_ROSTER)
    team_memories = tmp_path / "memories"
    for name in with_config:
        d = team_memories / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "tiger-memory.config.yaml").write_text("agent: {name: x}\n")
    team_memories.mkdir(parents=True, exist_ok=True)
    return team_memories


# ----- claim matrix (not_due vs own-pending, plan.md §3) --------------------


def test_inside_floor_no_own_pending_not_due(tmp_path: Path) -> None:
    write_sweep_state(tmp_path, {"last_sweep_at": _iso(1)})
    res = try_claim_sweep(
        tmp_path, now=NOW, token="A", own_persona="Anzai", own_pending=False
    )
    assert (res.claimed, res.reason, res.scope) == (False, "not_due", None)


def test_own_pending_bypasses_floor_scope_own_only(tmp_path: Path) -> None:
    write_sweep_state(tmp_path, {"last_sweep_at": _iso(1)})
    res = try_claim_sweep(
        tmp_path, now=NOW, token="A", own_persona="Anzai", own_pending=True
    )
    assert res.claimed is True and res.scope == "own-only"
    state = read_sweep_state(tmp_path)
    assert state["scope"] == "own-only"
    assert state["own_persona"] == "Anzai"


def test_team_due_without_own_scope_team(tmp_path: Path) -> None:
    res = try_claim_sweep(tmp_path, now=NOW, token="A")
    assert res.claimed is True and res.scope == "team"
    state = read_sweep_state(tmp_path)
    assert state["scope"] == "team"
    assert "own_persona" not in state


def test_team_due_wins_over_own_pending(tmp_path: Path) -> None:
    # Both gates hot -> one scope: "team" (the own persona rides along).
    res = try_claim_sweep(
        tmp_path, now=NOW, token="A", own_persona="Anzai", own_pending=True
    )
    assert res.scope == "team"
    assert "own_persona" not in read_sweep_state(tmp_path)


def test_own_only_claim_makes_team_claim_busy(tmp_path: Path) -> None:
    # Accepted tradeoff (plan.md §3): ONE lease serializes both scopes.
    write_sweep_state(tmp_path, {"last_sweep_at": _iso(1)})
    assert try_claim_sweep(
        tmp_path, now=NOW, token="A", own_persona="Anzai", own_pending=True
    ).claimed is True
    # Second session, team now due (floor override drops it to zero).
    res = try_claim_sweep(tmp_path, now=NOW, token="B", floor_hours=0.5)
    assert (res.claimed, res.reason) == (False, "busy")


# ----- target composition by scope (plan.md §3 matrix) ----------------------


def test_plan_own_only_targets_exactly_own(tmp_path: Path) -> None:
    team = _make_team(tmp_path)
    plan = plan_team_sweep(team, own_persona="Anzai", scope="own-only")
    assert [t.name for t in plan.targets] == ["Anzai"]
    assert plan.remaining == 0
    assert plan.all_personas == 3


def test_plan_own_only_skips_own_already_done(tmp_path: Path) -> None:
    team = _make_team(tmp_path)
    record_persona_done(team, "Anzai", now=NOW)
    plan = plan_team_sweep(team, own_persona="Anzai", scope="own-only")
    assert plan.targets == []


def test_plan_team_own_first_cap_applies_to_others(tmp_path: Path) -> None:
    team = _make_team(tmp_path)
    # LRU says Ayako is oldest — but the pending own persona goes first,
    # and the cap of 1 applies to the OTHERS only.
    write_sweep_state(team, {"done_at": {
        "Ayako": _iso(50), "Anzai": _iso(10), "Sakuragi": _iso(30),
    }})
    plan = plan_team_sweep(
        team, max_personas=1, own_persona="Anzai", scope="team"
    )
    assert [t.name for t in plan.targets] == ["Anzai", "Ayako"]
    assert plan.remaining == 1  # Sakuragi — counted among others only


def test_plan_team_without_own_plain_lru(tmp_path: Path) -> None:
    team = _make_team(tmp_path)
    write_sweep_state(team, {"done_at": {
        "Ayako": _iso(50), "Anzai": _iso(10), "Sakuragi": _iso(30),
    }})
    plan = plan_team_sweep(team, max_personas=2, scope="team")
    assert [t.name for t in plan.targets] == ["Ayako", "Sakuragi"]
    assert plan.remaining == 1


# ----- watermark guard + compat (plan.md §3, criterion 4) -------------------


def test_own_only_complete_leaves_watermark_untouched(tmp_path: Path) -> None:
    team = _make_team(tmp_path)
    write_sweep_state(team, {"last_sweep_at": _iso(1)})
    try_claim_sweep(
        team, now=NOW, token="A", own_persona="Anzai", own_pending=True
    )
    record_persona_done(team, "Anzai", now=NOW)
    assert mark_sweep_complete(team, NOW, token="A") is True
    state = read_sweep_state(team)
    assert state["last_sweep_at"] == _iso(1)  # NOT advanced
    for gone in ("claim_token", "claim_at", "progress", "run_started_at",
                 "scope", "own_persona"):
        assert gone not in state


def test_own_only_complete_refused_until_own_done(tmp_path: Path) -> None:
    team = _make_team(tmp_path)
    write_sweep_state(team, {"last_sweep_at": _iso(1)})
    try_claim_sweep(
        team, now=NOW, token="A", own_persona="Anzai", own_pending=True
    )
    assert mark_sweep_complete(team, NOW, token="A") is False
    # Force overrides, still without advancing the watermark.
    assert mark_sweep_complete(team, NOW, token="A", force=True) is True
    assert read_sweep_state(team)["last_sweep_at"] == _iso(1)


def test_team_complete_advances_watermark_and_pops_scope(
    tmp_path: Path,
) -> None:
    team = _make_team(tmp_path)
    try_claim_sweep(team, now=NOW, token="A")
    for name in ("Ayako", "Anzai", "Sakuragi"):
        record_persona_done(team, name, now=NOW)
    assert mark_sweep_complete(team, NOW, token="A") is True
    state = read_sweep_state(team)
    assert state["last_sweep_at"] == NOW.isoformat()
    assert "scope" not in state


def test_complete_missing_scope_treated_as_team(tmp_path: Path) -> None:
    # Compat rule: a claim record written by pre-scope code has no scope
    # field -> today's semantics (advance the watermark).
    team = _make_team(tmp_path)
    write_sweep_state(team, {"claim_token": "A", "claim_at": NOW.isoformat()})
    for name in ("Ayako", "Anzai", "Sakuragi"):
        record_persona_done(team, name, now=NOW)
    assert mark_sweep_complete(team, NOW, token="A") is True
    assert read_sweep_state(team)["last_sweep_at"] == NOW.isoformat()


def test_release_clears_scope_fields(tmp_path: Path) -> None:
    write_sweep_state(tmp_path, {"last_sweep_at": _iso(1)})
    try_claim_sweep(
        tmp_path, now=NOW, token="A", own_persona="Anzai", own_pending=True
    )
    assert release_sweep_claim(tmp_path, token="A") is True
    state = read_sweep_state(tmp_path)
    assert "scope" not in state and "own_persona" not in state


def test_maybe_sweep_roster_wires_scope_and_own(tmp_path: Path) -> None:
    team = _make_team(tmp_path)
    write_sweep_state(team, {"last_sweep_at": _iso(1)})
    dec = maybe_sweep_roster(
        team, now=NOW, token="A", own_persona="Anzai", own_pending=True
    )
    assert dec.ran is True and dec.scope == "own-only"
    assert [t.name for t in dec.plan.targets] == ["Anzai"]


def test_maybe_sweep_roster_not_claimed_scope_none(tmp_path: Path) -> None:
    team = _make_team(tmp_path)
    write_sweep_state(team, {"last_sweep_at": _iso(1)})
    dec = maybe_sweep_roster(team, now=NOW, token="A")
    assert (dec.ran, dec.reason, dec.scope) == (False, "not_due", None)


# ----- has_pending_source (plan.md §3 pending definition, criterion 3) ------


def _cfg_and_store(tmp_path: Path):
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(dedent(f"""\
        agent: {{name: Anzai, role: coach}}
        store: {{root: {tmp_path}/memory}}
        sources:
          - kind: claude_code
            project_path: {tmp_path}/proj/
        summarizer: {{backend: anthropic, model: m, prompts: default/v1}}
    """))
    cfg = load_config(str(cfg_path))
    return cfg, Store(cfg.store.root)


def _rec(uuid: str, *, last_event: datetime, mtime: float) -> SourceRecord:
    return SourceRecord(
        conversation_uuid=uuid, source="claude_code", source_id="s",
        first_event_at=last_event, last_event_at=last_event,
        activity_mtime=mtime, content="x", raw_path=Path("/r"),
    )


_T0 = 1_754_000_000.0  # arbitrary "now" epoch for mtime math
_IDLE = datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_pending_false_with_no_records(tmp_path, monkeypatch) -> None:
    cfg, store = _cfg_and_store(tmp_path)
    monkeypatch.setattr(lc, "_discover", lambda c, max_age_days=7: [])
    assert lc.has_pending_source(cfg, store, now=_T0) is False


def test_pending_true_for_idle_uncursored_record(tmp_path, monkeypatch) -> None:
    cfg, store = _cfg_and_store(tmp_path)
    rec = _rec("conv-a", last_event=_IDLE, mtime=_T0 - 90_000)
    monkeypatch.setattr(lc, "_discover", lambda c, max_age_days=7: [rec])
    assert lc.has_pending_source(cfg, store, now=_T0) is True


def test_live_session_never_counts_as_pending(tmp_path, monkeypatch) -> None:
    # Criterion 3: the calling session's uuid is excluded outright.
    cfg, store = _cfg_and_store(tmp_path)
    rec = _rec("conv-live", last_event=_IDLE, mtime=_T0 - 90_000)
    monkeypatch.setattr(lc, "_discover", lambda c, max_age_days=7: [rec])
    assert lc.has_pending_source(
        cfg, store, exclude_session="conv-live", now=_T0
    ) is False


def test_active_record_not_pending(tmp_path, monkeypatch) -> None:
    # Fresh mtime (< idle_threshold_hours) -> still being written -> not
    # "completed", the idle heuristic that also guards a mid-write live
    # transcript when --exclude-session is absent (safety clause).
    cfg, store = _cfg_and_store(tmp_path)
    rec = _rec("conv-b", last_event=_IDLE, mtime=_T0 - 60)
    monkeypatch.setattr(lc, "_discover", lambda c, max_age_days=7: [rec])
    assert lc.has_pending_source(cfg, store, now=_T0) is False


def test_cursor_current_not_pending(tmp_path, monkeypatch) -> None:
    cfg, store = _cfg_and_store(tmp_path)
    rec = _rec("conv-c", last_event=_IDLE, mtime=_T0 - 90_000)
    monkeypatch.setattr(lc, "_discover", lambda c, max_age_days=7: [rec])
    save_cursor(store, "conv-c",
                Cursor(last_event_at=_IDLE.isoformat(), processed_events=4))
    assert lc.has_pending_source(cfg, store, now=_T0) is False


def test_cursor_stale_is_pending(tmp_path, monkeypatch) -> None:
    cfg, store = _cfg_and_store(tmp_path)
    rec = _rec("conv-d", last_event=_IDLE, mtime=_T0 - 90_000)
    monkeypatch.setattr(lc, "_discover", lambda c, max_age_days=7: [rec])
    earlier = (_IDLE - timedelta(hours=6)).isoformat()
    save_cursor(store, "conv-d",
                Cursor(last_event_at=earlier, processed_events=2))
    assert lc.has_pending_source(cfg, store, now=_T0) is True


def test_cursor_unparseable_is_pending(tmp_path, monkeypatch) -> None:
    cfg, store = _cfg_and_store(tmp_path)
    rec = _rec("conv-e", last_event=_IDLE, mtime=_T0 - 90_000)
    monkeypatch.setattr(lc, "_discover", lambda c, max_age_days=7: [rec])
    save_cursor(store, "conv-e",
                Cursor(last_event_at="not-a-time", processed_events=1))
    assert lc.has_pending_source(cfg, store, now=_T0) is True


def test_pending_default_now_wallclock(tmp_path, monkeypatch) -> None:
    # now=None falls back to time.time() — idle math still holds for a
    # record idle since long ago.
    cfg, store = _cfg_and_store(tmp_path)
    rec = _rec("conv-f", last_event=_IDLE, mtime=0.0)
    monkeypatch.setattr(lc, "_discover", lambda c, max_age_days=7: [rec])
    assert lc.has_pending_source(cfg, store) is True


# ----- CLI contract (plan.md criterion 2) -----------------------------------


def _cli_setup(tmp_path: Path) -> str:
    mem = tmp_path / "mem"
    for name in ("Anzai", "Ayako"):
        pdir = mem / name
        pdir.mkdir(parents=True)
        (pdir / "tiger-memory.config.yaml").write_text("store: {root: .}\n")
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "personas.yaml").write_text(
        "personas:\n  - name: Anzai\n  - name: Ayako\n"
    )
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(dedent(f"""\
        agent: {{name: Anzai, role: coach}}
        store: {{root: {mem}/Anzai}}
        sources:
          - kind: claude_code
            project_path: {tmp_path}/proj
        summarizer: {{backend: anthropic, model: m, prompts: default/v1}}
    """))
    return str(cfg_path)


def _run(cfg_path: str, *args: str, capsys) -> dict:
    rc = main(["--config", cfg_path, *args])
    assert rc == 0
    return json.loads(capsys.readouterr().out)


def test_cli_own_persona_pending_claims_own_only(
    tmp_path, capsys, monkeypatch
) -> None:
    cfg = _cli_setup(tmp_path)
    seen: dict = {}

    def fake_pending(c, s, *, exclude_session=None):
        seen["exclude"] = exclude_session
        return True

    monkeypatch.setattr(lc, "has_pending_source", fake_pending)
    # Team inside the floor -> only the own gate is hot.
    write_sweep_state(tmp_path / "mem", {"last_sweep_at": NOW.isoformat()})
    out = _run(cfg, "sweep-plan", "--own-persona", "Anzai",
               "--exclude-session", "sess-uuid-1",
               "--now", NOW.isoformat(), capsys=capsys)
    assert out["ran"] is True
    assert out["scope"] == "own-only"
    assert out["own"] == {"persona": "Anzai", "pending": True}
    assert [t["name"] for t in out["targets"]] == ["Anzai"]
    assert seen["exclude"] == "sess-uuid-1"


def test_cli_own_persona_not_pending_inside_floor_not_due(
    tmp_path, capsys, monkeypatch
) -> None:
    cfg = _cli_setup(tmp_path)
    monkeypatch.setattr(
        lc, "has_pending_source",
        lambda c, s, *, exclude_session=None: False,
    )
    write_sweep_state(tmp_path / "mem", {"last_sweep_at": NOW.isoformat()})
    out = _run(cfg, "sweep-plan", "--own-persona", "Anzai",
               "--now", NOW.isoformat(), capsys=capsys)
    assert (out["ran"], out["reason"], out["scope"]) == (
        False, "not_due", None)
    assert out["own"] == {"persona": "Anzai", "pending": False}
    assert out["targets"] == []


def test_cli_plain_call_backcompat_own_null_scope_team(
    tmp_path, capsys
) -> None:
    cfg = _cli_setup(tmp_path)
    out = _run(cfg, "sweep-plan", "--now", NOW.isoformat(), capsys=capsys)
    assert out["ran"] is True
    assert out["scope"] == "team"
    assert out["own"] is None
    assert [t["name"] for t in out["targets"]] == ["Anzai", "Ayako"]

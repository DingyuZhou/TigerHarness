"""B3 team-sweep gating (sweep.py) — staleness floor + soft-lease claim."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from tigerharness.tiger_memory import sweep
from tigerharness.tiger_memory.sweep import (
    enumerate_persona_configs,
    mark_sweep_complete,
    plan_team_sweep,
    read_sweep_state,
    record_persona_done,
    release_sweep_claim,
    sweep_due,
    sweep_progress,
    sweep_state_path,
    try_claim_sweep,
    write_sweep_state,
)

NOW = datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)


def _iso(delta_hours: float) -> str:
    return (NOW - timedelta(hours=delta_hours)).isoformat()


# ----- state IO ------------------------------------------------------------


def test_read_missing_state_is_empty(tmp_path: Path) -> None:
    assert read_sweep_state(tmp_path) == {}


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    team = tmp_path / "memories"  # does not exist yet -> mkdir
    write_sweep_state(team, {"last_sweep_at": "x", "k": 1})
    assert sweep_state_path(team).exists()
    assert read_sweep_state(team) == {"last_sweep_at": "x", "k": 1}


def test_read_malformed_json_is_empty(tmp_path: Path) -> None:
    sweep_state_path(tmp_path).write_text("{not json", encoding="utf-8")
    assert read_sweep_state(tmp_path) == {}


def test_read_non_dict_json_is_empty(tmp_path: Path) -> None:
    sweep_state_path(tmp_path).write_text("[1, 2, 3]", encoding="utf-8")
    assert read_sweep_state(tmp_path) == {}


# ----- staleness floor -----------------------------------------------------


def test_sweep_due_never_swept() -> None:
    assert sweep_due(None, NOW) is True


def test_sweep_due_unparseable_watermark() -> None:
    assert sweep_due("not-a-timestamp", NOW) is True


def test_sweep_due_elapsed_past_floor() -> None:
    assert sweep_due(_iso(25), NOW, floor_hours=24) is True
    assert sweep_due(_iso(24), NOW, floor_hours=24) is True  # boundary


def test_sweep_not_due_within_floor() -> None:
    assert sweep_due(_iso(1), NOW, floor_hours=24) is False


# ----- soft-lease claim ----------------------------------------------------


def test_claim_succeeds_on_fresh_team(tmp_path: Path) -> None:
    res = try_claim_sweep(tmp_path, now=NOW, token="sess-A")
    assert res.claimed is True and res.reason == "claimed"
    state = read_sweep_state(tmp_path)
    assert state["claim_token"] == "sess-A"
    assert state["claim_at"] == NOW.isoformat()


def test_claim_not_due_within_floor(tmp_path: Path) -> None:
    write_sweep_state(tmp_path, {"last_sweep_at": _iso(1)})
    res = try_claim_sweep(tmp_path, now=NOW, token="sess-A")
    assert res.claimed is False and res.reason == "not_due"


def test_claim_busy_when_other_holds_fresh_claim(tmp_path: Path) -> None:
    write_sweep_state(tmp_path, {"claim_token": "sess-A", "claim_at": _iso(0.1)})
    res = try_claim_sweep(tmp_path, now=NOW, token="sess-B")
    assert res.claimed is False and res.reason == "busy"


def test_claim_steals_stale_claim(tmp_path: Path) -> None:
    # Owner crashed ~1h ago; lease is 1800s -> stale, stealable.
    write_sweep_state(tmp_path, {"claim_token": "sess-A", "claim_at": _iso(1)})
    res = try_claim_sweep(tmp_path, now=NOW, token="sess-B")
    assert res.claimed is True and res.reason == "claimed"
    assert read_sweep_state(tmp_path)["claim_token"] == "sess-B"


def test_claim_reentrant_same_token(tmp_path: Path) -> None:
    write_sweep_state(tmp_path, {"claim_token": "sess-A", "claim_at": _iso(0.1)})
    res = try_claim_sweep(tmp_path, now=NOW, token="sess-A")
    assert res.claimed is True and res.reason == "claimed"


# ----- completion ----------------------------------------------------------


def test_mark_complete_advances_watermark_and_clears_claim(tmp_path: Path) -> None:
    try_claim_sweep(tmp_path, now=NOW, token="sess-A")
    later = NOW + timedelta(minutes=5)
    mark_sweep_complete(tmp_path, later)
    state = read_sweep_state(tmp_path)
    assert state["last_sweep_at"] == later.isoformat()
    assert "claim_token" not in state and "claim_at" not in state
    # The freshly-bumped watermark gates the next trigger.
    assert try_claim_sweep(tmp_path, now=later, token="sess-B").reason == "not_due"


def test_module_exposes_defaults() -> None:
    assert sweep.DEFAULT_STALENESS_FLOOR_HOURS == 24.0
    assert sweep.DEFAULT_LEASE_SECONDS == 1800.0
    assert sweep.DEFAULT_MAX_PERSONAS == 3


# ----- roster walk (slice b) -----------------------------------------------


def _make_team(tmp_path: Path, roster_yaml: str, *, with_config) -> Path:
    """Build <tmp>/configs/personas.yaml + <tmp>/memories/<p>/config for
    each persona in *with_config*. Returns team_memories_dir."""
    (tmp_path / "configs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "configs" / "personas.yaml").write_text(roster_yaml)
    team_memories = tmp_path / "memories"
    for name in with_config:
        d = team_memories / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "tiger-memory.config.yaml").write_text("agent: {name: x}\n")
    team_memories.mkdir(parents=True, exist_ok=True)
    return team_memories


_ROSTER = (
    "personas:\n"
    "  - name: Ayako\n"
    "  - name: Anzai\n"
    "  - name: Sakuragi\n"
)

_ROSTER4 = (
    "personas:\n"
    "  - name: Ayako\n"
    "  - name: Anzai\n"
    "  - name: Sakuragi\n"
    "  - name: Rukawa\n"
)


def test_enumerate_keeps_only_personas_with_a_store(tmp_path: Path) -> None:
    team = _make_team(tmp_path, _ROSTER, with_config=["Ayako", "Anzai"])
    targets = enumerate_persona_configs(team)
    assert [t.name for t in targets] == ["Ayako", "Anzai"]  # order preserved
    assert targets[0].config_path.name == "tiger-memory.config.yaml"


def test_enumerate_missing_roster_is_empty(tmp_path: Path) -> None:
    assert enumerate_persona_configs(tmp_path / "memories") == []


def test_enumerate_malformed_roster_is_empty(tmp_path: Path) -> None:
    team = _make_team(tmp_path, "personas: [unclosed\n", with_config=[])
    assert enumerate_persona_configs(team) == []


def test_enumerate_top_level_not_dict_is_empty(tmp_path: Path) -> None:
    team = _make_team(tmp_path, "- just\n- a\n- list\n", with_config=[])
    assert enumerate_persona_configs(team) == []


def test_enumerate_personas_not_a_list_is_empty(tmp_path: Path) -> None:
    team = _make_team(tmp_path, "personas: nope\n", with_config=[])
    assert enumerate_persona_configs(team) == []


def test_enumerate_skips_bad_entries(tmp_path: Path) -> None:
    roster = (
        "personas:\n"
        "  - just-a-string\n"        # not a dict
        "  - name: ''\n"             # empty name
        "  - other: 1\n"             # missing name
        "  - name: Ayako\n"          # valid
    )
    team = _make_team(tmp_path, roster, with_config=["Ayako"])
    assert [t.name for t in enumerate_persona_configs(team)] == ["Ayako"]


def test_plan_unbounded_returns_all_pending(tmp_path: Path) -> None:
    # max_personas=None -> unbounded (the old default); still reachable.
    team = _make_team(tmp_path, _ROSTER, with_config=["Ayako", "Anzai"])
    plan = plan_team_sweep(team, max_personas=None)
    assert [t.name for t in plan.targets] == ["Ayako", "Anzai"]
    assert plan.remaining == 0 and plan.all_personas == 2


def test_plan_cap_limits_and_reports_remaining(tmp_path: Path) -> None:
    team = _make_team(tmp_path, _ROSTER, with_config=["Ayako", "Anzai"])
    plan = plan_team_sweep(team, max_personas=1)
    assert [t.name for t in plan.targets] == ["Ayako"]
    assert plan.remaining == 1


def test_plan_default_caps_at_three(tmp_path: Path) -> None:
    # No max_personas arg -> DEFAULT_MAX_PERSONAS (3); a 4-persona backlog
    # yields 3 this wake, 1 remaining for the next.
    team = _make_team(
        tmp_path, _ROSTER4,
        with_config=["Ayako", "Anzai", "Sakuragi", "Rukawa"],
    )
    plan = plan_team_sweep(team)
    assert [t.name for t in plan.targets] == ["Ayako", "Anzai", "Sakuragi"]
    assert plan.remaining == 1 and plan.all_personas == 4


def test_plan_skips_already_done(tmp_path: Path) -> None:
    team = _make_team(tmp_path, _ROSTER, with_config=["Ayako", "Anzai"])
    record_persona_done(team, "Ayako")
    plan = plan_team_sweep(team)
    assert [t.name for t in plan.targets] == ["Anzai"]


# ----- progress + claim lifecycle ------------------------------------------


def test_sweep_progress_empty_and_corrupt(tmp_path: Path) -> None:
    assert sweep_progress(tmp_path) == set()             # no state
    write_sweep_state(tmp_path, {"progress": "nope"})    # non-list
    assert sweep_progress(tmp_path) == set()


def test_record_persona_done_is_idempotent(tmp_path: Path) -> None:
    write_sweep_state(tmp_path, {"progress": "corrupt"})  # non-list -> reset
    record_persona_done(tmp_path, "Ayako")
    record_persona_done(tmp_path, "Ayako")  # idempotent
    record_persona_done(tmp_path, "Anzai")
    assert sweep_progress(tmp_path) == {"Ayako", "Anzai"}


def test_release_keeps_progress_and_watermark(tmp_path: Path) -> None:
    try_claim_sweep(tmp_path, now=NOW, token="A")
    record_persona_done(tmp_path, "Ayako")
    release_sweep_claim(tmp_path)
    state = read_sweep_state(tmp_path)
    assert "claim_token" not in state
    assert state["progress"] == ["Ayako"]  # preserved for the next wake


def test_complete_clears_progress(tmp_path: Path) -> None:
    record_persona_done(tmp_path, "Ayako")
    mark_sweep_complete(tmp_path, NOW)
    assert sweep_progress(tmp_path) == set()


# ----- run lifecycle: abandoned partial sweep must not poison freshness ----


def test_claim_stamps_run_started_at(tmp_path: Path) -> None:
    try_claim_sweep(tmp_path, now=NOW, token="A")
    assert read_sweep_state(tmp_path)["run_started_at"] == NOW.isoformat()


def test_resume_within_floor_keeps_progress(tmp_path: Path) -> None:
    # Start a run, do Ayako, release (cap hit); a wake an hour later
    # resumes the SAME run and must keep Ayako done.
    try_claim_sweep(tmp_path, now=NOW, token="A")
    record_persona_done(tmp_path, "Ayako")
    release_sweep_claim(tmp_path)
    try_claim_sweep(tmp_path, now=NOW + timedelta(hours=1), token="B")
    assert sweep_progress(tmp_path) == {"Ayako"}  # resumed, not reset


def test_abandoned_run_past_floor_resets_progress(tmp_path: Path) -> None:
    # Start a run, do Ayako, release; team goes silent > floor mid-run.
    try_claim_sweep(tmp_path, now=NOW, token="A")
    record_persona_done(tmp_path, "Ayako")
    release_sweep_claim(tmp_path)
    much_later = NOW + timedelta(hours=25)  # > 24h floor -> abandoned
    res = try_claim_sweep(tmp_path, now=much_later, token="B")
    assert res.claimed is True
    # The abandoned run's progress is cleared so Ayako is re-swept.
    assert sweep_progress(tmp_path) == set()
    assert read_sweep_state(tmp_path)["run_started_at"] == much_later.isoformat()


def test_complete_clears_run_started_at(tmp_path: Path) -> None:
    try_claim_sweep(tmp_path, now=NOW, token="A")
    mark_sweep_complete(tmp_path, NOW)
    assert "run_started_at" not in read_sweep_state(tmp_path)


# ----- maybe_sweep_roster (the shared hook) --------------------------------


def test_maybe_sweep_claims_and_plans(tmp_path: Path) -> None:
    from tigerharness.tiger_memory.sweep import maybe_sweep_roster
    team = _make_team(tmp_path, _ROSTER, with_config=["Ayako", "Anzai"])
    dec = maybe_sweep_roster(team, now=NOW, token="A")
    assert dec.ran is True and dec.reason == "claimed"
    assert [t.name for t in dec.plan.targets] == ["Ayako", "Anzai"]


def test_maybe_sweep_default_caps_at_three(tmp_path: Path) -> None:
    # The DEFAULT_MAX_PERSONAS cap propagates through maybe_sweep_roster.
    from tigerharness.tiger_memory.sweep import maybe_sweep_roster
    team = _make_team(
        tmp_path, _ROSTER4,
        with_config=["Ayako", "Anzai", "Sakuragi", "Rukawa"],
    )
    dec = maybe_sweep_roster(team, now=NOW, token="A")
    assert dec.ran is True
    assert len(dec.plan.targets) == 3
    assert dec.plan.remaining == 1


def test_maybe_sweep_noop_when_not_due(tmp_path: Path) -> None:
    from tigerharness.tiger_memory.sweep import maybe_sweep_roster
    team = _make_team(tmp_path, _ROSTER, with_config=["Ayako"])
    write_sweep_state(team, {"last_sweep_at": _iso(1)})  # within 24h floor
    dec = maybe_sweep_roster(team, now=NOW, token="A")
    assert dec.ran is False and dec.reason == "not_due" and dec.plan is None


def test_maybe_sweep_noop_when_busy(tmp_path: Path) -> None:
    from tigerharness.tiger_memory.sweep import maybe_sweep_roster
    team = _make_team(tmp_path, _ROSTER, with_config=["Ayako"])
    write_sweep_state(team, {"claim_token": "other", "claim_at": _iso(0.1)})
    dec = maybe_sweep_roster(team, now=NOW, token="A")
    assert dec.ran is False and dec.reason == "busy" and dec.plan is None


def test_enumerate_spaced_persona_store(tmp_path: Path) -> None:
    # Persona names may contain single internal spaces; the store dir
    # memories/<Name>/ and its config resolve like any other name.
    roster = (
        "personas:\n"
        "  - name: Chuan Ying\n"
        "  - name: Ayako\n"
    )
    team = _make_team(tmp_path, roster, with_config=["Chuan Ying"])
    targets = enumerate_persona_configs(team)
    assert [t.name for t in targets] == ["Chuan Ying"]
    assert targets[0].config_path.parent.name == "Chuan Ying"

"""CLI for the B3 team-sweep gating wrappers -- sweep-plan / sweep-done /
sweep-complete / sweep-release.

These are thin, non-AI wrappers over ``tiger_memory.sweep`` so an
interactive persona session drives the gating without inline Python
(see docs/tiger-memory-sweep-protocol.md). The fixture lays out a tiny
team:

    <tmp>/configs/personas.yaml          # roster
    <tmp>/mem/anzai/tiger-memory.config.yaml
    <tmp>/mem/ayako/tiger-memory.config.yaml
    <tmp>/cfg.yaml                       # the --config we drive with

team_memories_dir = cfg.store.root.parent = <tmp>/mem.
"""
from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

from tigerharness.tiger_memory.cli import main


def _setup(tmp_path: Path, personas: tuple[str, ...] = ("anzai", "ayako")) -> str:
    mem = tmp_path / "mem"
    for name in personas:
        pdir = mem / name
        pdir.mkdir(parents=True)
        # enumerate_persona_configs only checks existence, never parses it.
        (pdir / "tiger-memory.config.yaml").write_text("store: {root: .}\n")
    roster = "\n".join(f"  - name: {n}" for n in personas)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "personas.yaml").write_text(
        f"personas:\n{roster}\n"
    )
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(dedent(f"""\
        agent: {{name: anzai, role: coach}}
        store: {{root: {mem}/anzai}}
        sources:
          - kind: claude_code
            project_path: {tmp_path}/proj
        summarizer: {{backend: anthropic, model: claude-opus-4-7, prompts: default/v1}}
    """))
    return str(cfg_path)


def _run(cfg_path: str, *args: str, capsys) -> dict:
    rc = main(["--config", cfg_path, *args])
    assert rc == 0
    return json.loads(capsys.readouterr().out)


def test_sweep_plan_claims_and_lists_targets(tmp_path: Path, capsys) -> None:
    cfg = _setup(tmp_path)
    # No --token -> uuid default still claims cleanly.
    out = _run(cfg, "sweep-plan", capsys=capsys)
    assert out["ran"] is True
    assert out["reason"] == "claimed"
    # A uuid token is minted and echoed back when --token is omitted.
    assert out["token"]
    assert {t["name"] for t in out["targets"]} == {"anzai", "ayako"}
    assert out["all_personas"] == 2
    assert out["remaining"] == 0
    # config_path points at the per-persona tiger-memory.config.yaml.
    for t in out["targets"]:
        assert t["config_path"].endswith("tiger-memory.config.yaml")


def test_sweep_plan_respects_max_personas(tmp_path: Path, capsys) -> None:
    cfg = _setup(tmp_path)
    out = _run(cfg, "sweep-plan", "--max-personas", "1", capsys=capsys)
    assert out["ran"] is True
    assert len(out["targets"]) == 1
    assert out["remaining"] == 1
    assert out["all_personas"] == 2


def test_sweep_plan_busy_when_other_holds_fresh_claim(
    tmp_path: Path, capsys
) -> None:
    cfg = _setup(tmp_path)
    _run(cfg, "sweep-plan", "--token", "A", capsys=capsys)
    # A different session, within the lease, sees the claim as busy.
    out = _run(cfg, "sweep-plan", "--token", "B", "--lease-seconds", "1800",
               capsys=capsys)
    assert out["ran"] is False
    assert out["reason"] == "busy"
    assert out["targets"] == []
    assert out["remaining"] == 0
    assert out["all_personas"] == 0


def test_sweep_complete_then_plan_not_due(tmp_path: Path, capsys) -> None:
    cfg = _setup(tmp_path)
    rc = main(["--config", cfg, "sweep-complete"])
    assert rc == 0
    assert "watermark advanced" in capsys.readouterr().out
    out = _run(cfg, "sweep-plan", capsys=capsys)
    assert out["ran"] is False
    assert out["reason"] == "not_due"


def test_sweep_done_skips_persona_on_next_plan(tmp_path: Path, capsys) -> None:
    cfg = _setup(tmp_path)
    _run(cfg, "sweep-plan", "--token", "T", capsys=capsys)
    rc = main(["--config", cfg, "sweep-done", "--persona", "anzai"])
    assert rc == 0
    assert "recorded anzai done" in capsys.readouterr().out
    # Re-claim with the SAME token; progress is preserved across the wake.
    out = _run(cfg, "sweep-plan", "--token", "T", capsys=capsys)
    assert {t["name"] for t in out["targets"]} == {"ayako"}


def test_sweep_release_clears_claim_for_other_session(
    tmp_path: Path, capsys
) -> None:
    cfg = _setup(tmp_path)
    _run(cfg, "sweep-plan", "--token", "A", capsys=capsys)
    rc = main(["--config", cfg, "sweep-release"])
    assert rc == 0
    assert "claim released" in capsys.readouterr().out
    # A different session can now claim (watermark still stale).
    out = _run(cfg, "sweep-plan", "--token", "B", capsys=capsys)
    assert out["ran"] is True
    assert out["reason"] == "claimed"


def test_sweep_plan_now_and_floor_overrides(tmp_path: Path, capsys) -> None:
    cfg = _setup(tmp_path)
    # Watermark stamped at midnight.
    rc = main(["--config", cfg, "sweep-complete",
               "--now", "2026-01-01T00:00:00Z"])
    assert rc == 0
    capsys.readouterr()
    # 12h later with a 6h floor -> due (claimed).
    out = _run(cfg, "sweep-plan", "--now", "2026-01-01T12:00:00Z",
               "--floor-hours", "6", capsys=capsys)
    assert out["ran"] is True
    # Same instant with the default-ish 24h floor -> not due.
    out2 = _run(cfg, "sweep-plan", "--now", "2026-01-01T12:00:00Z",
                "--floor-hours", "24", capsys=capsys)
    assert out2["ran"] is False
    assert out2["reason"] == "not_due"

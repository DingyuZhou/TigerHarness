"""P3 read-side measurement: briefing-size stats + sidecar."""
from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from uuid import uuid4

from tigerharness.tiger_memory import frontmatter
from tigerharness.tiger_memory.briefing import _briefing_stats, rebuild_briefing
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.store import Store


def test_briefing_stats_counts_core_and_layers(tmp_path: Path) -> None:
    bdir = tmp_path / "briefing"
    for sub in ("recent", "daily", "weekly", "monthly"):
        (bdir / sub).mkdir(parents=True)
    (bdir / "must_memorize.md").write_text("one two three\n")   # 14 chars, 3 words
    (bdir / "recent" / "a.md").write_text("four five\n")        # 10 chars, 2 words
    # longer_memory.md intentionally absent -> exercises _wordcount's
    # unreadable-file branch (0, 0).

    stats = _briefing_stats(bdir)
    assert stats["sections"]["must_memorize.md"] == {"chars": 14, "words": 3}
    assert stats["sections"]["longer_memory.md"] == {"chars": 0, "words": 0}
    assert stats["sections"]["recent"] == {"chars": 10, "words": 2}
    assert stats["sections"]["daily"] == {"chars": 0, "words": 0}
    assert stats["total_chars"] == 24
    assert stats["total_words"] == 5


def _setup(tmp_path: Path):
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(dedent(f"""\
        agent: {{name: T, role: T}}
        store: {{root: {tmp_path}/memory}}
        sources:
          - kind: claude_code
            project_path: {tmp_path}/proj/
        summarizer: {{backend: anthropic, model: claude-opus-4-7, prompts: default/v1}}
        rebuild: {{lock_path: {tmp_path}/lock}}
    """))
    cfg = load_config(cfg_path)
    store = Store(cfg.store.root)
    store.init_layout()
    return cfg, store


def test_rebuild_briefing_emits_sidecar_and_manifest_line(tmp_path: Path) -> None:
    cfg, store = _setup(tmp_path)
    uid = str(uuid4())
    (store.paths.journal / f"20260514-082136-{uid}.md").write_text(
        frontmatter.render({"type": "short_summary"}, "A decision was made.\n")
    )
    (store.paths.journal / "must_memorize.md").write_text("- a pinned fact\n")

    rebuild_briefing(cfg, store)

    sidecar = store.paths.briefing / ".briefing_metrics.json"
    assert sidecar.exists()
    data = json.loads(sidecar.read_text())
    assert data["total_words"] > 0
    assert data["total_chars"] > 0
    assert "must_memorize.md" in data["sections"]
    assert data["sections"]["must_memorize.md"]["words"] > 0

    manifest = (store.paths.briefing / "MANIFEST.md").read_text()
    assert "Briefing size:" in manifest
    assert f"{data['total_words']} words" in manifest
    # All layers resident by default -> no drill-on-demand section.
    assert "Drill on demand" not in manifest


def test_lean_core_makes_layers_drill_on_demand(tmp_path: Path) -> None:
    """resident_layers=[] keeps must_memorize resident but drops every
    walking-window layer from the resident set (still drillable)."""
    cfg_path = tmp_path / "lean.yaml"
    cfg_path.write_text(dedent(f"""\
        agent: {{name: T, role: T}}
        store: {{root: {tmp_path}/memory}}
        sources:
          - kind: claude_code
            project_path: {tmp_path}/proj/
        summarizer: {{backend: anthropic, model: claude-opus-4-7, prompts: default/v1}}
        rebuild: {{lock_path: {tmp_path}/lock}}
        briefing:
          resident_layers: []
    """))
    cfg = load_config(cfg_path)
    store = Store(cfg.store.root)
    store.init_layout()
    uid = str(uuid4())
    (store.paths.journal / f"20260514-082136-{uid}.md").write_text(
        frontmatter.render({"type": "short_summary"}, "A recent short.\n")
    )
    (store.paths.journal / "must_memorize.md").write_text("- a pinned fact\n")

    rebuild_briefing(cfg, store)

    # The recent short is NOT copied into the resident briefing.
    assert list((store.paths.briefing / "recent").glob("*.md")) == []
    data = json.loads(
        (store.paths.briefing / ".briefing_metrics.json").read_text()
    )
    assert data["sections"]["recent"]["words"] == 0
    # must_memorize stays resident (load-bearing core).
    assert data["sections"]["must_memorize.md"]["words"] > 0

    manifest = (store.paths.briefing / "MANIFEST.md").read_text()
    assert "Drill on demand" in manifest
    for layer in ("recent", "daily", "weekly", "monthly"):
        assert f"`{layer}/`" in manifest


def test_lean_core_keeps_load_bearing_core_resident(tmp_path: Path) -> None:
    """Recall-safety invariant: even the most aggressive diet
    (`resident_layers: []`) keeps the load-bearing core — must_memorize.md
    (incl. its owner_explicit/locked rows) AND longer_memory.md — resident.
    The core is copied unconditionally; `resident_layers` can only gate the
    four walking-window layers."""
    cfg_path = tmp_path / "lean.yaml"
    cfg_path.write_text(dedent(f"""\
        agent: {{name: T, role: T}}
        store: {{root: {tmp_path}/memory}}
        sources:
          - kind: claude_code
            project_path: {tmp_path}/proj/
        summarizer: {{backend: anthropic, model: claude-opus-4-7, prompts: default/v1}}
        rebuild: {{lock_path: {tmp_path}/lock}}
        briefing:
          resident_layers: []
    """))
    cfg = load_config(cfg_path)
    store = Store(cfg.store.root)
    store.init_layout()
    (store.paths.journal / "must_memorize.md").write_text(
        "| Score | Kind | Last bump | Last decay | Source | Memo |\n"
        "|------:|------|-----------|------------|--------|------|\n"
        "| inf | owner_explicit | 2026-01-01 | 2026-01-01 | pin | never forget X |\n"
    )
    (store.paths.journal / "longer_memory.md").write_text(
        "Compressed project history.\n"
    )
    uid = str(uuid4())
    (store.paths.journal / f"20260514-082136-{uid}.md").write_text(
        frontmatter.render({"type": "short_summary"}, "A recent short.\n")
    )

    rebuild_briefing(cfg, store)

    # BOTH core files are resident regardless of resident_layers=[].
    mm = store.paths.briefing / "must_memorize.md"
    lm = store.paths.briefing / "longer_memory.md"
    assert mm.exists() and "never forget X" in mm.read_text()  # locked row survives
    assert lm.exists() and "project history" in lm.read_text()
    # ...while the walking-window layers are NOT resident (drill-on-demand).
    assert list((store.paths.briefing / "recent").glob("*.md")) == []

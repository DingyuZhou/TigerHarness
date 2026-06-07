"""CLI for the in-session sub-agent executor — `plan` + `ingest-summary`."""
from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

from tigerharness.tiger_memory.cli import main


def _bundle() -> str:
    return (
        "@@SHORT@@\n- decides to ship\n"
        "@@DETAILED@@\n## Intent\nUser wanted it shipped.\n"
        "@@MUST_MEMORIZE@@\nKIND: decision\nMEMO: ship the thing\n"
    )


def _setup(tmp_path: Path) -> tuple[str, str]:
    """Write a config + one past-idle transcript. Returns (cfg_path, uid)."""
    project = tmp_path / "proj"
    project.mkdir()
    uid = "11111111-1111-1111-1111-111111111111"
    f = project / f"{uid}.jsonl"
    ts = "2026-05-14T08:21:36.000Z"
    f.write_text(
        json.dumps({"type": "user", "timestamp": ts,
                    "message": {"role": "user", "content": "ship it?"}}) + "\n"
        + json.dumps({"type": "assistant", "timestamp": ts,
                      "message": {"role": "assistant", "content": "yes"}}) + "\n"
    )
    t = time.time() - 3 * 3600
    os.utime(f, (t, t))
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(dedent(f"""\
        agent: {{name: T, role: T}}
        store: {{root: {tmp_path}/memory}}
        sources:
          - kind: claude_code
            project_path: {project}
        summarizer: {{backend: anthropic, model: claude-opus-4-7, prompts: default/v1}}
        rebuild: {{lock_path: {tmp_path}/lock, idle_threshold_hours: 0}}
    """))
    return str(cfg_path), uid


def test_plan_command_prints_manifest(tmp_path: Path, capsys) -> None:
    cfg_path, uid = _setup(tmp_path)
    rc = main(["--config", cfg_path, "plan"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["items"][0]["conversation_uuid"] == uid


def test_ingest_summary_success(tmp_path: Path, capsys) -> None:
    cfg_path, uid = _setup(tmp_path)
    main(["--config", cfg_path, "plan"])
    with patch("sys.stdin", io.StringIO(_bundle())):
        rc = main(["--config", cfg_path, "ingest-summary", "--uuid", uid])
    assert rc == 0
    assert f"ingested {uid}" in capsys.readouterr().out
    # Store root has the agent slug appended (memory/t/); glob recursively.
    assert any(uid in f.name for f in (tmp_path / "memory").rglob("*.md"))


def test_ingest_summary_malformed_bundle_exits_1(tmp_path: Path) -> None:
    cfg_path, uid = _setup(tmp_path)
    main(["--config", cfg_path, "plan"])
    with patch("sys.stdin", io.StringIO("no markers")):
        rc = main(["--config", cfg_path, "ingest-summary", "--uuid", uid])
    assert rc == 1


def test_ingest_summary_unknown_uuid_exits_2(tmp_path: Path) -> None:
    cfg_path, _uid = _setup(tmp_path)
    main(["--config", cfg_path, "plan"])
    rc = main(["--config", cfg_path, "ingest-summary", "--uuid", "nope"])
    assert rc == 2


def test_ingest_summary_no_manifest_exits_2(tmp_path: Path) -> None:
    cfg_path, uid = _setup(tmp_path)  # no `plan` run -> no manifest
    rc = main(["--config", cfg_path, "ingest-summary", "--uuid", uid])
    assert rc == 2

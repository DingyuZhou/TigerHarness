"""B1 stage-2 plan side — lifecycle.plan_rebuild (staging + manifest)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from textwrap import dedent

from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.lifecycle import _sweep_staging_dir, plan_rebuild
from tigerharness.tiger_memory.store import Store

_BIG = "X" * 3000


def _simple_jsonl(path: Path, uid: str, ts: str = "2026-05-14T08:21:36.000Z") -> None:
    path.write_text(
        json.dumps({"type": "user", "timestamp": ts,
                    "message": {"role": "user", "content": f"hello {uid}"}}) + "\n"
        + json.dumps({"type": "assistant", "timestamp": ts,
                      "message": {"role": "assistant", "content": "ack"}}) + "\n"
    )


def _tool_jsonl(path: Path, uid: str) -> None:
    ts = "2026-05-14T08:21:36.000Z"
    path.write_text("\n".join(json.dumps(r) for r in [
        {"type": "user", "timestamp": ts,
         "message": {"role": "user", "content": "read it"}},
        {"type": "assistant", "timestamp": ts, "message": {"role": "assistant",
         "content": [{"type": "tool_use", "id": "t1", "name": "Read",
                      "input": {"file_path": "/x"}}]}},
        {"type": "user", "timestamp": ts, "message": {"role": "user",
         "content": [{"type": "tool_result", "tool_use_id": "t1", "content": _BIG}]}},
        {"type": "assistant", "timestamp": ts,
         "message": {"role": "assistant", "content": "done"}},
    ]) + "\n")


def _backdate(path: Path, hours: float) -> None:
    t = time.time() - hours * 3600
    os.utime(path, (t, t))


def _setup(tmp_path: Path, *, extra: str = "", idle: float = 0.0):
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(dedent(f"""\
        agent: {{name: T, role: T}}
        store: {{root: {tmp_path}/memory}}
        sources:
          - kind: claude_code
            project_path: {project}
        summarizer: {{backend: anthropic, model: claude-opus-4-7, prompts: default/v1}}
        rebuild:
          lock_path: {tmp_path}/lock
          idle_threshold_hours: {idle}
    """) + extra)
    cfg = load_config(cfg_path)
    return cfg, Store(cfg.store.root), project


def test_plan_emits_manifest_and_prompt_files(tmp_path: Path) -> None:
    cfg, store, project = _setup(tmp_path)
    uid = "11111111-1111-1111-1111-111111111111"
    _simple_jsonl(project / f"{uid}.jsonl", uid)
    _backdate(project / f"{uid}.jsonl", hours=3)

    items = plan_rebuild(cfg, store)
    assert len(items) == 1
    item = items[0]
    assert item["conversation_uuid"] == uid
    assert item["action"] == "summarize_new"
    prompt_path = Path(item["prompt_path"])
    assert prompt_path.exists()
    body = prompt_path.read_text()
    assert "@@SHORT@@" in body and "@@MUST_MEMORIZE@@" in body  # combined prompt
    assert "hello" in body  # the transcript content is embedded

    manifest = json.loads((_sweep_staging_dir(store) / "manifest.json").read_text())
    assert manifest["items"][0]["conversation_uuid"] == uid


def test_plan_applies_prefilter(tmp_path: Path) -> None:
    cfg, store, project = _setup(tmp_path)
    uid = "22222222-2222-2222-2222-222222222222"
    _tool_jsonl(project / f"{uid}.jsonl", uid)
    _backdate(project / f"{uid}.jsonl", hours=3)

    items = plan_rebuild(cfg, store)
    body = Path(items[0]["prompt_path"]).read_text()
    assert "[tool_result elided:" in body
    assert _BIG not in body


def test_plan_prefilter_disabled_keeps_raw(tmp_path: Path) -> None:
    cfg, store, project = _setup(
        tmp_path, extra="prefilter:\n  enabled: false\n")
    uid = "33333333-3333-3333-3333-333333333333"
    _tool_jsonl(project / f"{uid}.jsonl", uid)
    _backdate(project / f"{uid}.jsonl", hours=3)

    items = plan_rebuild(cfg, store)
    body = Path(items[0]["prompt_path"]).read_text()
    assert _BIG in body  # unfiltered


def test_plan_clears_stale_staging(tmp_path: Path) -> None:
    cfg, store, project = _setup(tmp_path)
    uid = "44444444-4444-4444-4444-444444444444"
    _simple_jsonl(project / f"{uid}.jsonl", uid)
    _backdate(project / f"{uid}.jsonl", hours=3)

    plan_rebuild(cfg, store)  # creates staging
    junk = _sweep_staging_dir(store) / "stale.txt"
    junk.write_text("old")
    plan_rebuild(cfg, store)  # staging.exists() -> rmtree
    assert not junk.exists()
    assert (_sweep_staging_dir(store) / "manifest.json").exists()


def test_plan_skips_docs_and_active_sessions(tmp_path: Path) -> None:
    # idle threshold 2h + a FRESH transcript -> SKIP_ACTIVE -> skipped;
    # a docs source -> the DocsAdapter branch is skipped too.
    project = tmp_path / "proj"
    project.mkdir()
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(dedent(f"""\
        agent: {{name: T, role: T}}
        store: {{root: {tmp_path}/memory}}
        sources:
          - kind: claude_code
            project_path: {project}
          - kind: docs
            glob: '*.md'
        summarizer: {{backend: anthropic, model: claude-opus-4-7, prompts: default/v1}}
        rebuild:
          lock_path: {tmp_path}/lock
          idle_threshold_hours: 2.0
    """))
    cfg = load_config(cfg_path)
    store = Store(cfg.store.root)
    fresh = "55555555-5555-5555-5555-555555555555"
    _simple_jsonl(project / f"{fresh}.jsonl", fresh)  # mtime ~now -> active
    items = plan_rebuild(cfg, store)
    assert items == []


def test_plan_caps_at_max_sessions(tmp_path: Path) -> None:
    cfg, store, project = _setup(tmp_path)
    for i in range(3):
        uid = f"6666666{i}-6666-6666-6666-666666666666"
        _simple_jsonl(project / f"{uid}.jsonl", uid)
        _backdate(project / f"{uid}.jsonl", hours=3)
    items = plan_rebuild(cfg, store, max_sessions=2)
    assert len(items) == 2

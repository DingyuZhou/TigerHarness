"""End-to-end: the P1.1 pre-filter + thin metrics hook, via rebuild().

Uses MockSummarizer (no model spend). Builds a transcript that contains
a large ``tool_result`` payload so the pre-filter has something to strip,
then asserts the metrics stamped into state.json reflect the reduction
(filter on) or its absence (filter off).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from textwrap import dedent

from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.lifecycle import rebuild
from tigerharness.tiger_memory.store import Store
from tigerharness.tiger_memory.summarizers import MockSummarizer

_BIG_DUMP = "X" * 4000  # a fat tool_result payload (file/bash dump)


def _make_tool_heavy_jsonl(path: Path, uuid: str) -> None:
    ts = "2026-05-14T08:21:36.000Z"
    rows = [
        {
            "type": "user",
            "timestamp": ts,
            "message": {"role": "user", "content": "Please read config.py"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-05-14T08:21:38.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "On it."},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Read",
                        "input": {"file_path": "/repo/config.py"},
                    },
                ],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-05-14T08:21:39.000Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": _BIG_DUMP},
                ],
            },
        },
        {
            "type": "assistant",
            "timestamp": "2026-05-14T08:21:42.000Z",
            "message": {"role": "assistant", "content": "It sets the budgets."},
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _write_config(tmp_path: Path, project: Path, *, prefilter_enabled: bool) -> Path:
    cfg_path = tmp_path / f"cfg-{prefilter_enabled}.yaml"
    pf = "" if prefilter_enabled else "prefilter:\n  enabled: false\n"
    cfg_path.write_text(dedent(f"""\
        agent:
          name: TestTiger
          role: "Test consumer."
        store:
          root: {tmp_path}/memory-{prefilter_enabled}
        sources:
          - kind: claude_code
            project_path: {project}
        summarizer:
          backend: anthropic
          model: claude-opus-4-7
          prompts: default/v1
        rebuild:
          lock_path: {tmp_path}/test-{prefilter_enabled}.lock
          idle_threshold_hours: 0
    """) + pf)
    return cfg_path


def _backdate(path: Path, hours: float) -> None:
    new_time = time.time() - hours * 3600
    os.utime(path, (new_time, new_time))


def test_prefilter_on_reduces_input_and_records_metrics(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    uid = "11111111-2222-3333-4444-555555555555"
    _make_tool_heavy_jsonl(project / f"{uid}.jsonl", uid)
    _backdate(project / f"{uid}.jsonl", hours=3)

    cfg = load_config(_write_config(tmp_path, project, prefilter_enabled=True))
    store = Store(cfg.store.root)
    rebuild(cfg, store, summarizer_override=MockSummarizer())

    state = store.read_state()
    metrics = state["metrics"]
    assert metrics["sessions_processed"] == 1
    assert metrics["summarize_calls"] == 3  # new session: short+detailed+extractor
    # The 4000-char tool_result dump is elided -> measurable reduction.
    assert metrics["content_chars_filtered"] < metrics["content_chars_raw"]
    assert metrics["content_chars_saved"] > 3000
    assert metrics["prefilter_reduction_pct"] > 0.0


def test_prefilter_off_keeps_raw_input(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    uid = "99999999-8888-7777-6666-555555555555"
    _make_tool_heavy_jsonl(project / f"{uid}.jsonl", uid)
    _backdate(project / f"{uid}.jsonl", hours=3)

    cfg = load_config(_write_config(tmp_path, project, prefilter_enabled=False))
    store = Store(cfg.store.root)
    rebuild(cfg, store, summarizer_override=MockSummarizer())

    state = store.read_state()
    metrics = state["metrics"]
    assert metrics["sessions_processed"] == 1
    # Filter disabled -> raw == filtered, no reduction.
    assert metrics["content_chars_filtered"] == metrics["content_chars_raw"]
    assert metrics["content_chars_saved"] == 0
    assert metrics["prefilter_reduction_pct"] == 0.0

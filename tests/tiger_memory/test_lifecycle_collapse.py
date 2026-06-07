"""End-to-end: the P1.3 collapsed single-pass path, via rebuild().

A collapse-aware summarizer that emits the @@SHORT@@/@@DETAILED@@/
@@MUST_MEMORIZE@@ contract exercises the 1-call success path; the
marker-less MockSummarizer exercises the automatic fallback to the legacy
3-call path. Both must still produce the short + archive artifacts.
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
from tigerharness.tiger_memory.summarizers.base import Summarizer


class _CollapseSummarizer(Summarizer):
    """Returns a well-formed collapsed bundle for every call."""

    name = "fakecollapse"
    version = "v1"

    def summarize(self, *, prompt: str, max_words: int) -> str:
        return (
            "@@SHORT@@\n- decides to ship the thing\n"
            "@@DETAILED@@\n## Intent\nThe user wanted the thing shipped.\n"
            "@@MUST_MEMORIZE@@\nKIND: decision\nMEMO: ship the thing\n"
        )


def _make_jsonl(path: Path, uuid: str) -> None:
    ts = "2026-05-14T08:21:36.000Z"
    path.write_text(
        json.dumps({
            "type": "user", "timestamp": ts,
            "message": {"role": "user", "content": "ship the thing?"},
        }) + "\n"
        + json.dumps({
            "type": "assistant", "timestamp": ts,
            "message": {"role": "assistant", "content": "yes, shipping"},
        }) + "\n"
    )


def _backdate(path: Path, hours: float) -> None:
    new_time = time.time() - hours * 3600
    os.utime(path, (new_time, new_time))


def _config(tmp_path: Path, project: Path) -> Path:
    cfg_path = tmp_path / "collapse.yaml"
    cfg_path.write_text(dedent(f"""\
        agent:
          name: TestTiger
          role: "Test consumer."
        store:
          root: {tmp_path}/memory
        sources:
          - kind: claude_code
            project_path: {project}
        summarizer:
          backend: anthropic
          model: claude-opus-4-7
          prompts: default/v1
        rebuild:
          lock_path: {tmp_path}/test.lock
          idle_threshold_hours: 0
        collapse:
          enabled: true
    """))
    return cfg_path


def _setup(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    uid = "abcd1234-0000-0000-0000-000000000000"
    f = project / f"{uid}.jsonl"
    _make_jsonl(f, uid)
    _backdate(f, hours=3)
    cfg = load_config(_config(tmp_path, project))
    return Store(cfg.store.root), cfg


def test_collapse_success_is_one_call(tmp_path: Path) -> None:
    store, cfg = _setup(tmp_path)
    rebuild(cfg, store, summarizer_override=_CollapseSummarizer())

    archives = list(store.paths.archive.glob("*.md"))
    shorts = [f for f in store.paths.journal.glob("*.md")
              if f.name.startswith("2026")]
    assert len(archives) == 1 and len(shorts) >= 1

    state = store.read_state()
    assert state["metrics"]["sessions_processed"] == 1
    assert state["metrics"]["summarize_calls"] == 1  # collapsed: one call

    # The must-memorize candidate from the SAME call landed.
    mm_text = (store.paths.journal / "must_memorize.md").read_text()
    assert "ship the thing" in mm_text


def test_collapse_falls_back_on_unparseable_output(tmp_path: Path) -> None:
    store, cfg = _setup(tmp_path)
    # MockSummarizer returns bullet text with no @@ markers -> parse fails.
    rebuild(cfg, store, summarizer_override=MockSummarizer())

    archives = list(store.paths.archive.glob("*.md"))
    assert len(archives) == 1  # fallback still wrote the artifacts

    state = store.read_state()
    assert state["metrics"]["sessions_processed"] == 1
    # 1 spent collapse attempt + 3 fallback calls.
    assert state["metrics"]["summarize_calls"] == 4

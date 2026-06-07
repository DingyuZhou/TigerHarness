"""B1 stage-2 executor write-back (executor.ingest_collapsed_summary)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent

import pytest

from tigerharness.tiger_memory.collapse import CollapseParseError
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.executor import (
    SUBAGENT_SUMMARIZER_TAG,
    ingest_collapsed_summary,
)
from tigerharness.tiger_memory.store import Store

_FIRST = datetime(2026, 5, 14, 8, 21, 36, tzinfo=timezone.utc)
_LAST = datetime(2026, 5, 14, 8, 40, 0, tzinfo=timezone.utc)
_UID = "abcd1234-0000-0000-0000-000000000000"


def _bundle(mm_block: str = "KIND: decision\nMEMO: ship the thing") -> str:
    return (
        "@@SHORT@@\n- decides to ship\n"
        "@@DETAILED@@\n## Intent\nUser wanted it shipped.\n"
        f"@@MUST_MEMORIZE@@\n{mm_block}\n"
    )


def _cfg_store(tmp_path: Path, *, mm_rows: int = 60):
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(dedent(f"""\
        agent: {{name: T, role: T}}
        store: {{root: {tmp_path}/memory}}
        sources:
          - kind: claude_code
            project_path: {tmp_path}/proj/
        summarizer: {{backend: anthropic, model: m, prompts: default/v1}}
        rebuild: {{lock_path: {tmp_path}/lock}}
        budgets: {{must_memorize_rows: {mm_rows}}}
    """))
    cfg = load_config(cfg_path)
    store = Store(cfg.store.root)
    store.init_layout()
    return cfg, store


def _ingest(store, cfg, bundle, **kw):
    return ingest_collapsed_summary(
        store, cfg,
        conversation_uuid=_UID, source="claude_code", source_id=_UID,
        first_event_at=_FIRST, last_event_at=_LAST,
        bundle_text=bundle, raw_path=Path("/dev/null"),
        **kw,
    )


def test_ingest_writes_short_archive_and_memo(tmp_path: Path) -> None:
    cfg, store = _cfg_store(tmp_path)
    res = _ingest(store, cfg, _bundle(), today="2026-05-14")
    assert res.conversation_uuid == _UID
    assert res.must_memorize_added == 1

    archives = list(store.paths.archive.glob("*.md"))
    shorts = [f for f in store.paths.journal.glob("*.md")
              if f.name.startswith("2026")]
    assert len(archives) == 1 and any(_UID in f.name for f in archives)
    assert len(shorts) == 1
    # The archive carries the sub-agent summarizer tag + the detailed body.
    archive_text = archives[0].read_text()
    assert SUBAGENT_SUMMARIZER_TAG in archive_text
    assert "## Intent" in archive_text
    # must_memorize merged.
    assert "ship the thing" in (
        store.paths.journal / "must_memorize.md"
    ).read_text()


def test_ingest_none_section_adds_no_memo(tmp_path: Path) -> None:
    cfg, store = _cfg_store(tmp_path)
    # Omit `today` -> exercises the default datetime.now branch.
    res = _ingest(store, cfg, _bundle(mm_block="NONE"))
    assert res.must_memorize_added == 0
    assert len(list(store.paths.archive.glob("*.md"))) == 1


def test_ingest_malformed_bundle_writes_nothing(tmp_path: Path) -> None:
    cfg, store = _cfg_store(tmp_path)
    with pytest.raises(CollapseParseError):
        _ingest(store, cfg, "no markers here", today="2026-05-14")
    assert list(store.paths.archive.glob("*.md")) == []
    assert [f for f in store.paths.journal.glob("*.md")
            if f.name.startswith("2026")] == []


def test_ingest_demotes_when_must_memorize_full(tmp_path: Path) -> None:
    cfg, store = _cfg_store(tmp_path, mm_rows=1)
    # Pre-fill must_memorize at capacity with a high-scored row.
    (store.paths.journal / "must_memorize.md").write_text(
        "---\n---\n"
        "| Score | Kind | Last bump | Last decay | Source | Memo |\n"
        "|------:|------|-----------|------------|--------|------|\n"
        "| 99 | preference | 2026-01-01 | 2026-01-01 | extract | keep me |\n"
    )
    res = _ingest(store, cfg, _bundle(), today="2026-05-14")
    assert res.must_memorize_added == 1
    # The high-scored existing row survives the cap; a dropped log exists.
    mm_text = (store.paths.journal / "must_memorize.md").read_text()
    assert "keep me" in mm_text
    assert (store.paths.journal / ".dropped_memorize.md").exists()

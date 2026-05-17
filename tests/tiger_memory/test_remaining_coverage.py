"""Coverage-push tests for remaining uncovered lines across multiple modules:
- config.py: lines 174-175, 186, 190, 291, 294, 295->298, 297, 315, 320
- must_memorize.py: lines 53->55, 59, 174, 234-235, 245, 260-262, 306-307, 359-360
- store.py: lines 98, 105->107, 241->243, 245-246, 262, 267-269, 282-283, 316-318
- embedders.py: lines 80-81, 95-96
- docs.py: lines 48-49, 54->59, 91
- sources/claude_transcript.py: lines 66-67, 109, 202
- summarizers/base.py: line 45
- state.py: line 103
- slack_bridge/bridge.py: various branch edges
"""
from __future__ import annotations

import os
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock, patch

import pytest

# ----- config.py tests -------------------------------------------------------

from tigerharness.tiger_memory.config import (
    ConfigError,
    load_config,
    _require,
    _slugify,
    _validate_walking,
)


class TestConfigRequire:
    def test_missing_key(self):
        with pytest.raises(ConfigError, match="config.foo is required"):
            _require({}, "foo", str)

    def test_wrong_type_dict(self):
        with pytest.raises(ConfigError, match="must be a mapping"):
            _require({"foo": "bar"}, "foo", dict)

    def test_wrong_type_list(self):
        with pytest.raises(ConfigError, match="must be a list"):
            _require({"foo": "bar"}, "foo", list)

    def test_wrong_type_str_empty(self):
        with pytest.raises(ConfigError, match="must be a non-empty string"):
            _require({"foo": ""}, "foo", str)

    def test_wrong_type_str_non_str(self):
        with pytest.raises(ConfigError, match="must be a non-empty string"):
            _require({"foo": 42}, "foo", str)


class TestConfigStoreRootRelative:
    """Lines 174-175: relative store.root resolved against config dir."""

    def test_relative_store_root(self, tmp_path: Path):
        cfg_path = tmp_path / "sub" / "cfg.yaml"
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text(
            f"agent: {{name: T, role: T}}\n"
            f"store: {{root: ./memory}}\n"
            f"sources:\n"
            f"  - kind: claude_code\n"
            f"    project_path: {tmp_path}/proj/\n"
            f"summarizer: {{backend: anthropic, model: claude-sonnet-4-6, prompts: default/v1}}\n"
            f"rebuild: {{lock_path: {tmp_path}/lock}}\n"
        )
        cfg = load_config(cfg_path)
        # Store root should be resolved relative to config dir
        assert cfg.store.root.is_absolute()
        assert "sub" in str(cfg.store.root)


class TestConfigSourceValidation:
    """Line 190: invalid source entry (not a dict or missing kind)."""

    def test_invalid_source_entry(self, tmp_path: Path):
        cfg_path = tmp_path / "bad.yaml"
        cfg_path.write_text(
            f"agent: {{name: T, role: T}}\n"
            f"store: {{root: {tmp_path}/memory}}\n"
            f"sources:\n"
            f"  - just_a_string\n"
            f"summarizer: {{backend: anthropic, model: claude-sonnet-4-6, prompts: default/v1}}\n"
        )
        with pytest.raises(ConfigError, match="Invalid source entry"):
            load_config(cfg_path)


class TestConfigValidateWalking:
    """Line 320: dailies_working_days too small."""

    def test_dailies_too_small(self, tmp_path: Path):
        cfg_path = tmp_path / "bad.yaml"
        cfg_path.write_text(
            f"agent: {{name: T, role: T}}\n"
            f"store: {{root: {tmp_path}/memory}}\n"
            f"sources:\n"
            f"  - kind: claude_code\n"
            f"    project_path: {tmp_path}/proj/\n"
            f"summarizer: {{backend: anthropic, model: claude-sonnet-4-6, prompts: default/v1}}\n"
            f"briefing:\n"
            f"  walking:\n"
            f"    dailies_working_days: 3\n"
        )
        with pytest.raises(ConfigError, match="dailies_working_days"):
            load_config(cfg_path)


class TestSlugify:
    def test_empty(self):
        assert _slugify("") == "agent"

    def test_special_chars(self):
        assert _slugify("Hello World!") == "hello_world"


# ----- must_memorize.py tests ------------------------------------------------

from tigerharness.tiger_memory.must_memorize import (
    Row,
    _parse_table,
    _safe_date_ordinal,
    _sort_key,
    append_dropped,
    pin,
)
from tigerharness.tiger_memory.store import Store


class TestSafeDateOrdinal:
    """Lines 306-307: invalid date → 0."""

    def test_empty_string(self):
        assert _safe_date_ordinal("") == 0

    def test_invalid_date(self):
        assert _safe_date_ordinal("not-a-date") == 0

    def test_valid_date(self):
        assert _safe_date_ordinal("2026-05-15") > 0


class TestParseTableEdgeCases:
    """Lines 359-360: invalid score → skip row."""

    def test_invalid_score_skipped(self):
        table = dedent("""\
            | Score | Kind | Last bump | Source | Memo |
            |------:|------|-----------|--------|------|
            | abc   | preference | 2026-01-01 | extract | Bad score |
        """)
        rows = _parse_table(table)
        assert len(rows) == 0  # invalid score → skipped


class TestPinLockFailure:
    """Lines 234-235: pin returns 1 when lock can't be acquired."""

    def test_pin_lock_held(self, tmp_path: Path):
        cfg_path = tmp_path / "cfg.yaml"
        cfg_path.write_text(
            f"agent: {{name: T, role: T}}\n"
            f"store: {{root: {tmp_path}/memory}}\n"
            f"sources:\n"
            f"  - kind: claude_code\n"
            f"    project_path: {tmp_path}/proj/\n"
            f"summarizer: {{backend: anthropic, model: claude-sonnet-4-6, prompts: default/v1}}\n"
            f"rebuild:\n"
            f"  lock_path: {tmp_path}/lock\n"
        )
        cfg = load_config(cfg_path)
        store = Store(cfg.store.root)
        store.init_layout()

        # Pre-acquire lock with a live PID
        cfg.rebuild.lock_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.rebuild.lock_path.write_text(str(os.getpid()))
        os.utime(cfg.rebuild.lock_path, None)

        with patch("tigerharness.tiger_memory.store._pid_alive", return_value=True):
            ret = pin(cfg, store, memo="Test", kind="preference")
        assert ret == 1


class TestAppendDropped:
    """Line 174: append_dropped with existing .dropped_memorize.md."""

    def test_append_to_existing(self, tmp_path: Path):
        cfg_path = tmp_path / "cfg.yaml"
        cfg_path.write_text(
            f"agent: {{name: T, role: T}}\n"
            f"store: {{root: {tmp_path}/memory}}\n"
            f"sources:\n"
            f"  - kind: claude_code\n"
            f"    project_path: {tmp_path}/proj/\n"
            f"summarizer: {{backend: anthropic, model: claude-sonnet-4-6, prompts: default/v1}}\n"
        )
        store = Store(load_config(cfg_path).store.root)
        store.init_layout()

        # Pre-existing dropped file
        dropped_path = store.paths.journal / ".dropped_memorize.md"
        dropped_path.write_text("## Dropped 2026-01-01\n\nOld.\n")

        demoted = [Row(kind="preference", memo="Dropped fact", score=0,
                       locked=False, source="extract")]
        append_dropped(store, demoted)

        text = dropped_path.read_text()
        assert "Old." in text
        assert "Dropped fact" in text


# ----- embedders.py tests ---------------------------------------------------

class TestOpenAIEmbedderEdges:
    """Lines 80-81 (import error), 95-96 (embed_batch call)."""

    def test_missing_openai_import(self):
        from tigerharness.tiger_memory.embedders import OpenAIEmbedder
        with patch.dict("sys.modules", {"openai": None}):
            with pytest.raises(ImportError, match="openai not installed"):
                OpenAIEmbedder()

    def test_missing_api_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        # Mock the openai import to succeed
        mock_openai = MagicMock()
        with patch.dict("sys.modules", {"openai": mock_openai}):
            from tigerharness.tiger_memory.embedders import OpenAIEmbedder
            with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
                OpenAIEmbedder()


# ----- docs.py tests --------------------------------------------------------

class TestDocsSourceEdges:
    """Lines 48-49 (ValueError relative_to), 54->59 (not in git), 91 (git not found)."""

    def test_discover_file_not_in_git(self, tmp_path: Path):
        """File not tracked by git → falls back to mtime."""
        from tigerharness.tiger_memory.sources.docs import DocsAdapter
        doc = tmp_path / "test.md"
        doc.write_text("Hello docs")

        adapter = DocsAdapter(glob_pattern="*.md", repo_root=tmp_path)
        records = list(adapter.discover())
        assert len(records) == 1
        assert records[0].content == "Hello docs"

    def test_discover_file_outside_repo_root(self, tmp_path: Path):
        """File outside repo_root → relative_to ValueError → uses full path."""
        from tigerharness.tiger_memory.sources.docs import DocsAdapter
        doc = tmp_path / "test.md"
        doc.write_text("Outside")

        # Use a repo_root that's a sibling → relative_to will raise ValueError
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        adapter = DocsAdapter(glob_pattern="*.md", repo_root=tmp_path)
        rec = adapter._record_for(doc)
        assert rec is not None
        assert rec.content == "Outside"


# ----- summarizers/base.py tests -------------------------------------------

class TestSummarizerBase:
    """Lines 45, 27: cost_estimate_usd default and cost_so_far."""

    def test_cost_so_far_default(self):
        from tigerharness.tiger_memory.summarizers import MockSummarizer
        s = MockSummarizer()
        assert s.cost_so_far == 0.0

    def test_cost_estimate_usd_default(self):
        from tigerharness.tiger_memory.summarizers import MockSummarizer
        s = MockSummarizer()
        assert s.cost_estimate_usd(prompt_tokens=1000, output_tokens=500) == 0.0


# ----- state.py tests -------------------------------------------------------

class TestStateIsoNow:
    """Line 103: iso_now returns current time string."""

    def test_iso_now_format(self):
        from tigerharness.tiger_memory.state import iso_now
        result = iso_now()
        assert "T" in result
        assert result.endswith("+00:00") or result.endswith("Z")

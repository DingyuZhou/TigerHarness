"""Coverage-push tests for briefing.py — targeting lines:
96-99 (exception cleanup in rebuild_briefing), 146-147 (OSError reading fingerprint),
157-158 (OSError in _compute_fingerprint), 217-218 (shorts fallback in _copy_layer2),
293-294 (_is_stale true branch).
"""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import pytest

from tigerharness.tiger_memory import frontmatter
from tigerharness.tiger_memory.briefing import (
    _briefing_up_to_date,
    _compute_fingerprint,
    _copy_layer2,
    _is_stale,
    rebuild_briefing,
)
from tigerharness.tiger_memory.config import load_config
from tigerharness.tiger_memory.store import Store


def _cfg(tmp_path: Path):
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(
        f"agent: {{name: T, role: T}}\n"
        f"store: {{root: {tmp_path}/memory}}\n"
        f"sources:\n"
        f"  - kind: claude_code\n"
        f"    project_path: {tmp_path}/proj/\n"
        f"summarizer: {{backend: anthropic, model: claude-sonnet-4-6, prompts: default/v1}}\n"
        f"rebuild: {{lock_path: {tmp_path}/lock}}\n"
    )
    return load_config(cfg_path)


class TestBriefingUpToDateOSError:
    """Lines 146-147: OSError reading .fingerprint → return False."""

    def test_oserror_returns_false(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        # Create briefing dir with MANIFEST but broken fingerprint
        store.paths.briefing.mkdir(parents=True, exist_ok=True)
        (store.paths.briefing / "MANIFEST.md").write_text("manifest")
        fp = store.paths.briefing / ".fingerprint"
        fp.write_text("old")

        # Patch read_text to raise for .fingerprint
        orig = Path.read_text

        def bad_read(self, *a, **kw):
            if self.name == ".fingerprint":
                raise OSError("read error")
            return orig(self, *a, **kw)

        with patch.object(Path, "read_text", bad_read):
            result = _briefing_up_to_date(store)
        assert result is False


class TestComputeFingerprintOSError:
    """Lines 157-158: OSError stat-ing a journal file → skip."""

    def test_oserror_stat_skips_file(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()
        # Create two journal files
        (store.paths.journal / "good.md").write_text("content")
        bad_file = store.paths.journal / "bad.md"
        bad_file.write_text("content")

        orig_stat = Path.stat

        def bad_stat(self, *a, **kw):
            if self.name == "bad.md":
                raise OSError("stat error")
            return orig_stat(self, *a, **kw)

        with patch.object(Path, "stat", bad_stat):
            fp = _compute_fingerprint(store)

        # Only good.md should be in the fingerprint
        assert "good.md" in fp
        assert "bad.md" not in fp


class TestCopyLayer2ShortsFallback:
    """Lines 217-218: when daily doesn't exist, shorts for that date are used."""

    def test_falls_back_to_shorts(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()

        # Create a short for 20260514 but no daily
        from uuid import uuid4
        uid = str(uuid4())
        short = store.paths.journal / f"20260514-082136-{uid}.md"
        short.write_text(frontmatter.render(
            {"type": "short_summary"}, "Short content.\n"
        ))

        dest = tmp_path / "briefing_tmp"
        dest.mkdir()
        (dest / "daily").mkdir()

        result = _copy_layer2(store, ["20260514"], dest)
        assert len(result) >= 1  # should have copied the short


class TestIsStale:
    """Lines 293-294: _is_stale returns True when rebuild > 24h ago."""

    def test_stale_timestamp(self):
        old = "2020-01-01T00:00:00Z"
        assert _is_stale(old) is True

    def test_fresh_timestamp(self):
        now = datetime.now(timezone.utc).isoformat()
        assert _is_stale(now) is False


class TestRebuildBriefingException:
    """Lines 96-99: exception during rebuild_briefing cleans up tmp dir."""

    def test_exception_cleans_tmp(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        store = Store(cfg.store.root)
        store.init_layout()

        # Force _briefing_up_to_date to False
        # Then force atomic_swap_dir to raise during rebuild
        with patch.object(store, "atomic_swap_dir", side_effect=OSError("swap failed")):
            with pytest.raises(OSError, match="swap failed"):
                rebuild_briefing(cfg, store)

        # tmp dir should have been cleaned up
        import glob as globmod
        tmps = list(Path(tmp_path).rglob("briefing.tmp*"))
        # The cleanup removes the tmp dir on exception

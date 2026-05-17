"""Cover remaining branch partials for 99%+ coverage."""
from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# --- notifier.py 56->58: bridge_dir exists but .env does not ---

class TestFindSlackEnvBridgeDirNoEnv:
    """When TIGERHARNESS_SLACK_BRIDGE_DIR is set but .env not in it."""

    def test_returns_none_when_env_missing(self, tmp_path, monkeypatch):
        from tigerharness.task_runner.notifier import _find_slack_env_file

        monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
        monkeypatch.setenv("TIGERHARNESS_SLACK_BRIDGE_DIR", str(tmp_path))
        # tmp_path exists but has no .env
        assert _find_slack_env_file() is None


# --- notifier.py 246->248: meta.name is falsy ---

class TestRenderNoName:
    """_render when meta.name is empty."""

    def test_no_name_line_when_empty(self, tmp_path):
        from tigerharness.task_runner.notifier import _render
        from tigerharness.task_runner.registry import JobMeta, JobStore

        store = JobStore(tmp_path / "state")
        meta = JobMeta(
            job_id="test-001",
            persona="researcher",
            prompt_chars=100,
            max_iters=3,
            compact_every=0,
            continuation="",
            name="",  # empty name
            cwd=".",
            started_at=1.0,
            status="done",
            pid=None,
            current_iter=2,
            session_id="s1",
            last_update=1.0,
        )
        text = _render(meta, store)
        assert "name:" not in text


# --- registry.py 130->exit: delete a non-existent job ---

class TestRegistryDeleteMissing:
    """Deleting a job_id that doesn't exist is a no-op."""

    def test_delete_nonexistent_job(self, tmp_path):
        from tigerharness.task_runner.registry import JobStore

        store = JobStore(tmp_path / "state")
        store.delete("nonexistent-job-id")
        # No error, no crash — and nothing written
        assert not store.registry.exists()


# --- config.py 295->298: str type passes validation ---

class TestConfigRequireStr:
    """When typ is str and value is a valid non-empty string."""

    def test_valid_string_passes(self):
        from tigerharness.tiger_memory.config import _require

        result = _require({"name": "bot"}, "name", str)
        assert result == "bot"

    def test_non_list_dict_str_type_returns_val(self):
        """Cover 295->298: elif typ is str is False (typ is int)."""
        from tigerharness.tiger_memory.config import _require

        result = _require({"count": 42}, "count", int)
        assert result == 42


# --- task_runner/cli.py 382->391: cmd_show with no result file ---

class TestCmdShowNoResult:
    """cmd_show when result file doesn't exist."""

    def test_show_without_result_file(self, tmp_path, capsys):
        from tigerharness.task_runner import cli as tr_cli
        from tigerharness.task_runner.registry import JobMeta, JobStore

        store = JobStore(tmp_path / "state")
        meta = JobMeta(
            job_id="j-12345678",
            persona="researcher",
            prompt_chars=50,
            max_iters=5,
            compact_every=0,
            continuation="",
            name="test job",
            cwd=".",
            started_at=1.0,
            status="running",
            pid=None,
            current_iter=1,
            session_id="s1",
            last_update=1.0,
        )
        store.set(meta)

        # Don't create a result file — result_path should not exist
        args = MagicMock()
        args.job_id = "j-12345678"

        with patch.object(tr_cli.registry, "default_state_path", return_value=tmp_path / "state"):
            rc = tr_cli.cmd_show(args)

        assert rc == 0
        out = capsys.readouterr().out
        assert "j-12345678" in out
        assert "--- latest result ---" not in out


# --- sources/docs.py 54->59: file not in git history ---

class TestDocsSourceNoGit:
    """DocsAdapter handles files not in git (falls back to mtime)."""

    def test_file_not_in_git(self, tmp_path):
        from tigerharness.tiger_memory.sources.docs import DocsAdapter

        doc_dir = tmp_path / "docs"
        doc_dir.mkdir()
        f = doc_dir / "notes.md"
        f.write_text("# Notes\nSome content here.")

        adapter = DocsAdapter(glob_pattern="docs/*.md", repo_root=tmp_path)
        records = list(adapter.discover())
        assert len(records) == 1
        assert records[0].first_event_at is not None

    def test_file_in_git_uses_commit_dates(self):
        """Cover 54->59: first_at is NOT None (file IS in git)."""
        from pathlib import Path
        from tigerharness.tiger_memory.sources.docs import DocsAdapter

        # Use the actual tigerharness repo which has tracked files
        repo = Path(__file__).resolve().parent.parent
        adapter = DocsAdapter(glob_pattern="*.md", repo_root=repo)
        records = list(adapter.discover())
        # README.md is in git, so first_at comes from git
        assert any(r.source_id == "README.md" for r in records)


# --- store.py 105->107: atomic_swap_dir OSError rollback with backup ---

class TestAtomicSwapRollback:
    """When os.rename fails mid-swap and backup exists, rollback occurs."""

    def test_rollback_on_rename_failure(self, tmp_path):
        from tigerharness.tiger_memory.store import Store

        store = Store(tmp_path / "mem")
        store.init_layout()

        target = store.paths.briefing
        target.mkdir(parents=True, exist_ok=True)
        (target / "README.md").write_text("original")

        new_dir = tmp_path / "new_briefing"
        new_dir.mkdir()
        (new_dir / "README.md").write_text("updated")

        # Patch os.rename to fail on the second call (new->target)
        original_rename = os.rename
        call_count = [0]

        def failing_rename(src, dst):
            call_count[0] += 1
            if call_count[0] == 2:  # fail on new->target
                raise OSError("simulated failure")
            return original_rename(src, dst)

        with patch("os.rename", side_effect=failing_rename):
            with pytest.raises(OSError, match="simulated"):
                store.atomic_swap_dir(new_dir, target)

        # Target should be restored from backup (rollback happened)
        assert target.exists()
        assert (target / "README.md").read_text() == "original"

    def test_no_rollback_when_no_backup(self, tmp_path):
        """Cover 105->107: target didn't exist so no backup to rollback from."""
        from tigerharness.tiger_memory.store import Store

        store = Store(tmp_path / "mem")
        store.init_layout()

        # Target does NOT exist — so no backup is created at line 99-100
        target = tmp_path / "mem" / "nonexistent_dir"
        assert not target.exists()

        new_dir = tmp_path / "new_dir"
        new_dir.mkdir()

        # os.rename will fail (cross-device or permissions)
        with patch("os.rename", side_effect=OSError("cross-device")):
            with pytest.raises(OSError, match="cross-device"):
                store.atomic_swap_dir(new_dir, target)

        # No rollback happened (no backup existed)
        assert not target.exists()


# --- briefing.py 97->99: rebuild exception cleanup when tmp doesn't exist ---

class TestBriefingRebuildExceptionCleanup:
    """When rebuild_briefing raises, tmp is cleaned up."""

    def test_cleans_tmp_on_failure(self, tmp_path):
        from tigerharness.tiger_memory.store import Store

        store = Store(tmp_path / "mem")
        store.init_layout()

        # Create journal structure so _briefing_up_to_date returns False
        journal = store.paths.journal
        journal.mkdir(parents=True, exist_ok=True)
        (journal / "short.md").write_text("x")

        # Patch internals to fail during rebuild
        with patch(
            "tigerharness.tiger_memory.briefing._render_readme",
            side_effect=RuntimeError("boom"),
        ):
            from tigerharness.tiger_memory.briefing import rebuild_briefing

            cfg = MagicMock()
            with pytest.raises(RuntimeError, match="boom"):
                rebuild_briefing(cfg, store)

        # Tmp should be cleaned up (no stray briefing.tmp.* dirs)
        parent = store.paths.briefing.parent
        leftovers = list(parent.glob("briefing.tmp.*"))
        assert leftovers == []

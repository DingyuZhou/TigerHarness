"""Tests for the per-job worktree isolation feature.

When multiple background task-runner jobs target the same project, the
default cwd-shared behaviour causes HEAD/index/working-tree contention
(real incident: Wave 1 of Phase 2 -- three personas all wrote to the
same checkout, HEAD ping-ponged between branches, untracked files
cross-contaminated). The fix: opt-in ``--worktree-repo PATH`` on
``assign``, which makes the runner create a dedicated worktree at
``<repo>/.worktrees/<job-id>/``, tells the persona about it via a
prompt notice, and tears it down on job exit.

This file covers the new helpers + their integration in ``run_job``.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from tigerharness.task_runner.personas import clear_registry, register_persona
from tigerharness.task_runner.registry import JobMeta, JobStore
from tigerharness.task_runner import runner
from tigerharness.task_runner.runner import (
    _build_continuation,
    _build_initial_prompt,
    _create_worktree,
    _get_worktree_notice,
    _remove_worktree,
    _worktree_path,
    run_job,
)


# ---------------------------------------------------------------------------
# Pure helpers (no subprocess)
# ---------------------------------------------------------------------------

class TestWorktreePath:
    def test_path_layout(self):
        assert _worktree_path("/tmp/repo", "abcd1234") == (
            Path("/tmp/repo/.worktrees/abcd1234")
        )

    def test_path_stable_for_same_inputs(self):
        """Same job re-running (via ``tigerharness continue``) must land
        at the same path so the persona's prior session context isn't
        confused by a moving worktree."""
        a = _worktree_path("/home/x/repo", "deadbeef")
        b = _worktree_path("/home/x/repo", "deadbeef")
        assert a == b

    def test_path_differs_per_job(self):
        a = _worktree_path("/x", "job-a")
        b = _worktree_path("/x", "job-b")
        assert a != b


class TestWorktreeNotice:
    def test_notice_contains_cd_instruction_with_path(self):
        notice = _get_worktree_notice(Path("/tmp/wt-xyz"))
        assert "cd /tmp/wt-xyz" in notice

    def test_notice_warns_about_shared_checkout(self):
        """The notice must tell the persona NOT to operate on the
        shared parent checkout -- that's the entire point."""
        notice = _get_worktree_notice(Path("/tmp/wt"))
        assert "main project checkout" in notice or "shared" in notice.lower()

    def test_notice_mentions_branch_convention(self):
        """The persona must know to create a ``work/...`` branch."""
        notice = _get_worktree_notice(Path("/tmp/wt"))
        assert "work/" in notice


class TestPromptBuildersWithWorktree:
    def test_initial_prompt_includes_worktree_notice(self):
        notice = _get_worktree_notice(Path("/wt/job-1"))
        p = _build_initial_prompt("do the thing", worktree_notice=notice)
        assert "/wt/job-1" in p
        assert "do the thing" in p

    def test_initial_prompt_omits_notice_when_empty(self):
        p = _build_initial_prompt("do the thing", worktree_notice="")
        assert "project worktree" not in p

    def test_continuation_prompt_includes_worktree_notice(self):
        """Every continuation must carry the reminder -- ``/compact``
        otherwise drops it from the persona's working memory."""
        notice = _get_worktree_notice(Path("/wt/job-1"))
        p = _build_continuation("keep going", worktree_notice=notice)
        assert "/wt/job-1" in p

    def test_continuation_prompt_omits_notice_when_empty(self):
        p = _build_continuation("keep going", worktree_notice="")
        assert "project worktree" not in p


# ---------------------------------------------------------------------------
# Create / remove against a real git repo
# ---------------------------------------------------------------------------

def _init_repo(path: Path) -> None:
    """Create a minimal git repo with one commit on ``main``. Used as
    the fixture repo for worktree create/remove tests."""
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(path)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True,
    )
    (path / "README.md").write_text("seed\n")
    subprocess.run(
        ["git", "-C", str(path), "add", "README.md"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "seed"],
        check=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A fresh single-commit git repo with main as default branch."""
    r = tmp_path / "repo"
    r.mkdir()
    _init_repo(r)
    return r


class TestCreateWorktree:
    def test_creates_worktree_at_expected_path(self, repo, tmp_path):
        dest = tmp_path / "wt"
        _create_worktree(str(repo), dest)
        assert dest.is_dir()
        assert (dest / "README.md").read_text() == "seed\n"
        # The worktree is registered with git.
        listing = subprocess.run(
            ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
            check=True, capture_output=True, text=True,
        ).stdout
        assert str(dest) in listing

    def test_creates_parent_dir_if_missing(self, repo, tmp_path):
        """``.worktrees/`` won't exist on a fresh repo -- the helper
        must mkdir it before calling git worktree add."""
        dest = tmp_path / "nested" / "wt"
        _create_worktree(str(repo), dest)
        assert dest.is_dir()

    def test_idempotent_if_dest_exists(self, repo, tmp_path):
        """``tigerharness continue`` may re-invoke this on a worktree
        the first job already created -- the helper must no-op rather
        than raise."""
        dest = tmp_path / "wt"
        _create_worktree(str(repo), dest)
        _create_worktree(str(repo), dest)  # must not raise

    def test_raises_on_non_git_repo(self, tmp_path):
        not_a_repo = tmp_path / "not-git"
        not_a_repo.mkdir()
        dest = tmp_path / "wt"
        with pytest.raises(subprocess.CalledProcessError):
            _create_worktree(str(not_a_repo), dest)


class TestRemoveWorktree:
    def test_removes_worktree(self, repo, tmp_path):
        dest = tmp_path / "wt"
        _create_worktree(str(repo), dest)
        assert dest.is_dir()
        _remove_worktree(str(repo), dest)
        assert not dest.exists()

    def test_noop_if_dest_missing(self, repo, tmp_path):
        """Cleanup on a job that never created its worktree (e.g. the
        create call itself failed) must not raise."""
        dest = tmp_path / "wt-never-existed"
        _remove_worktree(str(repo), dest)  # must not raise
        assert not dest.exists()

    def test_swallows_git_failure(self, repo, tmp_path, caplog):
        """If git refuses (e.g. the repo went read-only mid-job), we
        log a warning and continue -- a stale worktree is a small leak,
        a crash in the cleanup path is a big one (it masks the real
        job outcome from the operator)."""
        dest = tmp_path / "wt"
        _create_worktree(str(repo), dest)
        # Corrupt the worktree's metadata so git worktree remove fails.
        gitdir_pointer = dest / ".git"
        gitdir_pointer.write_text("invalid pointer\n")
        # No exception should escape.
        _remove_worktree(str(repo), dest)


# ---------------------------------------------------------------------------
# run_job integration
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_personas():
    clear_registry()
    register_persona("tester", prompt="You are a tester.", cwd="/tmp")
    yield
    clear_registry()


def _make_job(
    store: JobStore,
    *,
    worktree_repo: str = "",
    job_id: str = "deadbeef",
    max_iters: int = 1,
    prompt: str = "do the thing",
    name: str = "wt-test",
) -> JobMeta:
    meta = JobMeta(
        job_id=job_id,
        persona="tester",
        prompt_chars=len(prompt),
        max_iters=max_iters,
        compact_every=0,
        continuation="",
        name=name,
        cwd="/tmp",
        started_at=time.time(),
        status="pending",
        pid=None,
        current_iter=0,
        session_id="",
        last_update=time.time(),
        early_exit=False,
        stuck_timeout=0,
        slack_thread_ts="",
        worktree_repo=worktree_repo,
    )
    store.set(meta)
    store.prompt_path(job_id).write_text(prompt)
    return meta


class _FakeSession:
    def __init__(self) -> None:
        self.id = ""

    async def close(self) -> None:
        return


class _OkBackend:
    """Backend whose every dispatch returns success. Records each prompt
    so tests can assert the worktree notice was injected."""
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def open_session(self, *, resume_id=None) -> _FakeSession:
        return _FakeSession()

    async def run(self, config, prompt, *, session=None, approval=None):
        self.calls.append(prompt)
        if session is not None and not session.id:
            session.id = "fake-sess-1"
        from dataclasses import dataclass

        @dataclass
        class _R:
            final_output: str = "ok"
            stop_reason: str = "end_turn"
            cost_usd: float = 0.01
            transcript: list | None = None
            usage: dict | None = None
        return _R()


def _install_stub_patches(backend):
    async def _stub_classify(*a, **kw):
        return "CONTINUING"

    async def _stub_novelty(*a, **kw):
        return "NEW"

    return patch.multiple(
        "tigerharness.task_runner.runner",
        get_backend=lambda *a, **kw: backend,
        notify_job_start=lambda *a, **kw: "",
        notify_job_end=lambda *a, **kw: False,
        _classify_output=_stub_classify,
        _classify_novelty=_stub_novelty,
    )


class TestRunJobWorktreeIntegration:
    @pytest.mark.asyncio
    async def test_creates_and_removes_worktree_around_run(
        self, repo, tmp_path,
    ):
        """Happy path: worktree appears before the first dispatch and
        is gone by the time run_job returns."""
        state = tmp_path / "state"
        store = JobStore(state)
        _make_job(store, worktree_repo=str(repo))
        backend = _OkBackend()

        with _install_stub_patches(backend):
            rc = await run_job("deadbeef", state_dir=state)

        assert rc == 0
        # Worktree is gone after run_job returns.
        assert not (repo / ".worktrees" / "deadbeef").exists()
        # The structured log captured both create + remove.
        log_text = (state / "deadbeef" / "run.log").read_text()
        assert '"kind": "worktree_created"' in log_text
        assert '"kind": "worktree_removed"' in log_text

    @pytest.mark.asyncio
    async def test_prompt_carries_worktree_notice(self, repo, tmp_path):
        """The persona's iter-1 prompt must contain the worktree path
        so the persona knows where to ``cd`` for git operations."""
        state = tmp_path / "state"
        store = JobStore(state)
        _make_job(store, worktree_repo=str(repo))
        backend = _OkBackend()

        with _install_stub_patches(backend):
            await run_job("deadbeef", state_dir=state)

        expected_wt = str(repo / ".worktrees" / "deadbeef")
        assert expected_wt in backend.calls[0], (
            "iter-1 prompt missing worktree path; the persona has no "
            f"way to know where to cd. Got: {backend.calls[0][:200]}"
        )

    @pytest.mark.asyncio
    async def test_no_worktree_when_meta_field_empty(self, tmp_path):
        """Legacy behaviour: empty ``worktree_repo`` means no worktree
        is created, and the prompt carries no worktree notice."""
        state = tmp_path / "state"
        store = JobStore(state)
        _make_job(store, worktree_repo="")
        backend = _OkBackend()

        with _install_stub_patches(backend):
            await run_job("deadbeef", state_dir=state)

        assert "project worktree" not in backend.calls[0]
        log_text = (state / "deadbeef" / "run.log").read_text()
        assert '"kind": "worktree_created"' not in log_text
        assert '"kind": "worktree_removed"' not in log_text

    @pytest.mark.asyncio
    async def test_create_failure_ends_job_as_error(self, tmp_path):
        """A bogus worktree_repo (not a git repo) must NOT silently
        continue with the shared tree -- that defeats the whole purpose.
        End the job as error with a clear message."""
        state = tmp_path / "state"
        store = JobStore(state)
        bogus = tmp_path / "not-a-git-repo"
        bogus.mkdir()
        _make_job(store, worktree_repo=str(bogus))
        backend = _OkBackend()

        with _install_stub_patches(backend):
            rc = await run_job("deadbeef", state_dir=state)

        assert rc == 2
        final = store.get("deadbeef")
        assert final.status == "error"
        assert "worktree" in (final.error or "").lower()
        # The backend was never invoked -- we bailed before dispatch.
        assert backend.calls == []
        log_text = (state / "deadbeef" / "run.log").read_text()
        assert '"kind": "worktree_create_failed"' in log_text

    @pytest.mark.asyncio
    async def test_cleanup_exception_is_swallowed(self, repo, tmp_path):
        """If ``_remove_worktree`` itself raises (not just a git nonzero
        returncode -- ``_remove_worktree`` swallows those by contract --
        but a genuine Python exception, e.g. subprocess.run can't find
        git), the outer cleanup must catch it so the job still ends
        cleanly and ``notify_job_end`` still runs."""
        state = tmp_path / "state"
        store = JobStore(state)
        _make_job(store, worktree_repo=str(repo))
        backend = _OkBackend()

        def _boom(repo_arg, dest):
            raise RuntimeError("simulated cleanup explosion")

        with _install_stub_patches(backend), \
             patch("tigerharness.task_runner.runner._remove_worktree",
                   side_effect=_boom):
            rc = await run_job("deadbeef", state_dir=state)

        # Job completed successfully despite the cleanup failure.
        assert rc == 0
        assert store.get("deadbeef").status == "done"

    @pytest.mark.asyncio
    async def test_continuation_iters_also_carry_notice(
        self, repo, tmp_path,
    ):
        """Iter-2+ prompts must also carry the worktree notice --
        ``/compact`` drops the iter-1 reminder, and without re-injection
        a long-running persona forgets where its worktree is mid-task."""
        state = tmp_path / "state"
        store = JobStore(state)
        _make_job(store, worktree_repo=str(repo), max_iters=3)
        backend = _OkBackend()

        with _install_stub_patches(backend):
            await run_job("deadbeef", state_dir=state)

        expected_wt = str(repo / ".worktrees" / "deadbeef")
        # Every dispatch saw the notice -- not just iter 1.
        for idx, prompt in enumerate(backend.calls, start=1):
            assert expected_wt in prompt, (
                f"iter {idx} prompt missing worktree path; "
                "/compact would have wiped it without re-injection"
            )

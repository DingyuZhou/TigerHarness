"""Runner-level integration tests for the stuck-watchdog feature.

These exercise the wiring between ``run_job`` / ``_dispatch_turn`` /
``stuck_watchdog`` — mostly the exception handshake (StuckWatchdogEscalation),
the auto-continue behavior, the slack-thread re-injection regression
fix, and the stuck-iter-1 prompt-replay semantics.

Pure unit tests for the watchdog logic itself live in
``test_stuck_watchdog.py``.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from tigerharness.task_runner.personas import clear_registry, register_persona
from tigerharness.task_runner.registry import JobMeta, JobStore
from tigerharness.task_runner import runner
from tigerharness.task_runner.runner import (
    StuckWatchdogEscalation,
    _dispatch_turn,
    run_job,
)


@pytest.fixture(autouse=True)
def _clean_personas():
    clear_registry()
    register_persona("tester", prompt="You are a tester.", cwd="/tmp")
    yield
    clear_registry()


@dataclass
class _FakeResult:
    final_output: str
    cost_usd: float = 0.01
    stop_reason: str = "end_turn"
    transcript: list = None
    usage: dict | None = None


class _FakeSession:
    """Mimics the contract of agent_sdk's Session for the stub backend.

    The first ``.run()`` call sets ``id`` (mirroring real claude_p
    behavior where the session id is assigned by the backend). The
    runner reads ``session.id`` to decide is_first_iter.
    """
    def __init__(self, *, resume_id: str | None = None) -> None:
        self.id = resume_id or ""

    async def close(self) -> None:
        return


class _StubBackend:
    """Records every dispatch prompt; assigns session.id on first call.

    ``fail_on_iter`` raises a RuntimeError from that 1-indexed iteration
    onwards (3 retry attempts inside run_with_retry consume one logical
    iter, so set high enough to actually propagate).
    """
    def __init__(self, *, fail_on_iter: int | None = None) -> None:
        self.calls: list[tuple[str, str]] = []  # (prompt, session_id_at_call)
        self.fail_on_iter = fail_on_iter
        self._iter_seen = 0

    async def open_session(self, *, resume_id: str | None = None) -> _FakeSession:
        return _FakeSession(resume_id=resume_id)

    async def run(self, config, prompt, *, session=None, approval=None) -> _FakeResult:
        self._iter_seen += 1
        if self.fail_on_iter and self._iter_seen >= self.fail_on_iter:
            raise RuntimeError(f"stub forced failure at call #{self._iter_seen}")
        if session is not None and not session.id:
            session.id = "stub-session-abc"
        self.calls.append((prompt, session.id if session else ""))
        return _FakeResult(final_output=f"ok-{self._iter_seen}", cost_usd=0.01)


def _make_job(
    store: JobStore,
    *,
    job_id: str = "deadbeef",
    max_iters: int = 3,
    compact_every: int = 0,
    continuation: str = "",
    prompt: str = "do the thing",
    name: str = "test",
    early_exit: bool = False,
    stuck_timeout: int = 1200,
    slack_thread_ts: str = "",
) -> JobMeta:
    meta = JobMeta(
        job_id=job_id,
        persona="tester",
        prompt_chars=len(prompt),
        max_iters=max_iters,
        compact_every=compact_every,
        continuation=continuation,
        name=name,
        cwd="/tmp",
        started_at=time.time(),
        status="pending",
        pid=None,
        current_iter=0,
        session_id="",
        last_update=time.time(),
        early_exit=early_exit,
        stuck_timeout=stuck_timeout,
        slack_thread_ts=slack_thread_ts,
    )
    store.set(meta)
    store.prompt_path(job_id).write_text(prompt)
    return meta


def _install_stub_patches(backend: _StubBackend):
    """Helper: stub backend + suppress real Slack DM calls + stub classifiers."""
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


# ---------------------------------------------------------------------------
# Stuck-timeout=0: bypass the watchdog entirely
# ---------------------------------------------------------------------------

class TestStuckTimeoutDisabled:
    @pytest.mark.asyncio
    async def test_no_watchdog_log_when_stuck_timeout_zero(self, tmp_path):
        store = JobStore(tmp_path)
        _make_job(store, max_iters=2, stuck_timeout=0)
        backend = _StubBackend()

        with _install_stub_patches(backend):
            rc = await run_job("deadbeef", state_dir=tmp_path)

        assert rc == 0
        log_text = store.run_log("deadbeef").read_text()
        assert "escalate" not in log_text
        assert "watchdog_fired" not in log_text
        assert store.get("deadbeef").status == "done"


# ---------------------------------------------------------------------------
# Auto-continue after stuck escalation
# ---------------------------------------------------------------------------

class TestStuckAutoContinue:
    @pytest.mark.asyncio
    async def test_stuck_escalation_continues_with_next_iter(self, tmp_path):
        """Iter 1 raises StuckWatchdogEscalation; iters 2 and 3 run normally.
        Job ends status=done."""
        store = JobStore(tmp_path)
        _make_job(store, max_iters=3)
        backend = _StubBackend()

        async def mock_dispatch_turn(
            backend_, agent_cfg, session, prompt, log_path, meta,
            *, iter_num, job_id, is_last_iter=False,
        ):
            if iter_num == 1:
                raise StuckWatchdogEscalation(iter_num, reason="test forced")
            return await runner._dispatch_one(
                backend_, agent_cfg, session, prompt, log_path,
                kind="turn", iter_num=iter_num, job_id=job_id,
            )

        with _install_stub_patches(backend), \
             patch("tigerharness.task_runner.runner._dispatch_turn",
                   mock_dispatch_turn):
            rc = await run_job("deadbeef", state_dir=tmp_path)

        assert rc == 0
        final = store.get("deadbeef")
        assert final.status == "done"
        assert final.current_iter == 3
        # Iters 2 and 3 successfully reached the backend; iter 1 raised
        # before the backend was called.
        assert len(backend.calls) == 2

        log_text = store.run_log("deadbeef").read_text()
        assert '"kind": "iter_stuck"' in log_text
        assert '"action": "continuing"' in log_text

    @pytest.mark.asyncio
    async def test_stuck_escalation_on_last_iter_ends_as_error(self, tmp_path):
        """Final iter being stuck ends the job with status=error."""
        store = JobStore(tmp_path)
        _make_job(store, max_iters=2)
        backend = _StubBackend()

        async def mock_dispatch_turn(
            backend_, agent_cfg, session, prompt, log_path, meta,
            *, iter_num, job_id, is_last_iter=False,
        ):
            if iter_num == 2:
                assert is_last_iter, "runner must flag the final iter"
                raise StuckWatchdogEscalation(iter_num, reason="last-iter test")
            return await runner._dispatch_one(
                backend_, agent_cfg, session, prompt, log_path,
                kind="turn", iter_num=iter_num, job_id=job_id,
            )

        with _install_stub_patches(backend), \
             patch("tigerharness.task_runner.runner._dispatch_turn",
                   mock_dispatch_turn):
            rc = await run_job("deadbeef", state_dir=tmp_path)

        assert rc == 1
        final = store.get("deadbeef")
        assert final.status == "error"
        assert "final iteration" in (final.error or "")
        assert "stuck-watchdog" in (final.error or "")
        assert final.current_iter == 2

        log_text = store.run_log("deadbeef").read_text()
        assert '"action": "ending"' in log_text


# ---------------------------------------------------------------------------
# Stuck-iter-1 must replay the original prompt on iter 2
# ---------------------------------------------------------------------------

class TestStuckIterOneRetry:
    @pytest.mark.asyncio
    async def test_stuck_iter_1_retries_with_initial_prompt(self, tmp_path):
        """When iter 1 is stuck before any successful dispatch, iter 2 must
        send the ORIGINAL task prompt (not a continuation) — the agent
        has never seen the task. Regression-prevention for the bug where
        ``is_first_iter`` was tied to ``i == 1`` instead of ``session.id``.
        """
        store = JobStore(tmp_path)
        _make_job(store, max_iters=3, prompt="diagnose the foo bug")
        backend = _StubBackend()

        async def mock_dispatch_turn(
            backend_, agent_cfg, session, prompt, log_path, meta,
            *, iter_num, job_id, is_last_iter=False,
        ):
            if iter_num == 1:
                raise StuckWatchdogEscalation(iter_num, reason="forced")
            return await runner._dispatch_one(
                backend_, agent_cfg, session, prompt, log_path,
                kind="turn", iter_num=iter_num, job_id=job_id,
            )

        with _install_stub_patches(backend), \
             patch("tigerharness.task_runner.runner._dispatch_turn",
                   mock_dispatch_turn):
            await run_job("deadbeef", state_dir=tmp_path)

        iter_2_prompt = backend.calls[0][0]
        assert iter_2_prompt.startswith(runner.TASK_PREAMBLE), (
            "iter 2 should send the ORIGINAL prompt because iter 1 stuck "
            "before the agent ever received it. Got a continuation instead."
        )
        assert "diagnose the foo bug" in iter_2_prompt

        iter_3_prompt = backend.calls[1][0]
        assert iter_3_prompt.startswith(runner.CONTINUATION_PREAMBLE)


# ---------------------------------------------------------------------------
# Reason flow: watchdog -> StuckWatchdogEscalation -> meta.error + log
# ---------------------------------------------------------------------------

class TestStuckReasonFlow:
    @pytest.mark.asyncio
    async def test_reason_surfaces_in_meta_error_and_log(self, tmp_path):
        store = JobStore(tmp_path)
        _make_job(store, max_iters=1)
        backend = _StubBackend()

        async def mock_dispatch_turn(*a, iter_num, **kw):
            raise StuckWatchdogEscalation(
                iter_num,
                reason="bash pid=99 subtree CPU flat over 2.0s (age 700s)",
            )

        with _install_stub_patches(backend), \
             patch("tigerharness.task_runner.runner._dispatch_turn",
                   mock_dispatch_turn):
            await run_job("deadbeef", state_dir=tmp_path)

        final = store.get("deadbeef")
        assert final.status == "error"
        assert "bash pid=99 subtree CPU flat" in (final.error or "")

        log_text = store.run_log("deadbeef").read_text()
        assert "bash pid=99 subtree CPU flat" in log_text


# ---------------------------------------------------------------------------
# Iter_log markdown formatting
# ---------------------------------------------------------------------------

class TestStuckIterLogMarkdown:
    @pytest.mark.asyncio
    async def test_iter_log_markdown_italics_close_on_both_branches(self, tmp_path):
        """Regression: the iter_log marker on the 'Final iteration -- job
        ending as error.' branch must close ``_[...]_`` italics. The
        original code dropped the closing ``]_``, leaking italics into
        the rest of the file when rendered."""
        store = JobStore(tmp_path)
        # cwd=tmp_path so the iter_log lands where we can read it.
        meta = JobMeta(
            job_id="deadbeef", persona="tester", prompt_chars=10,
            max_iters=2, compact_every=0, continuation="",
            name="md-balance", cwd=str(tmp_path),
            started_at=1715600000.0, status="pending",
            pid=None, current_iter=0, session_id="", last_update=0,
        )
        store.set(meta)
        store.prompt_path("deadbeef").write_text("do the thing")
        backend = _StubBackend()

        async def mock_dispatch_turn(
            backend_, agent_cfg, session, prompt, log_path, meta,
            *, iter_num, job_id, is_last_iter=False,
        ):
            if iter_num == 2:
                raise StuckWatchdogEscalation(iter_num, reason="forced")
            return await runner._dispatch_one(
                backend_, agent_cfg, session, prompt, log_path,
                kind="turn", iter_num=iter_num, job_id=job_id,
            )

        with _install_stub_patches(backend), \
             patch("tigerharness.task_runner.runner._dispatch_turn",
                   mock_dispatch_turn):
            await run_job("deadbeef", state_dir=tmp_path)

        iter_log = tmp_path / "lab_notebooks" / "tasks" / "md-balance--deadbeef.md"
        assert iter_log.exists()
        content = iter_log.read_text()

        open_count = content.count("_[")
        close_count = content.count("]_")
        assert open_count == close_count, (
            f"Unbalanced italics in iter_log: {open_count} '_[' vs "
            f"{close_count} ']_'."
        )
        assert "Final iteration" in content


# ---------------------------------------------------------------------------
# Slack-thread regression: notice must be on EVERY continuation, not just iter 2
# ---------------------------------------------------------------------------

class TestSlackThreadInjection:
    @pytest.mark.asyncio
    async def test_thread_notice_injected_on_every_continuation(self, tmp_path):
        """Critical regression-prevention: every iter's prompt MUST carry
        the thread_ts hint when slack_thread_ts is set. Pre-fix, only
        iter 2 had it, and ``/compact`` then dropped it from claude's
        context on iter 6+ — the agent started posting top-level DMs.
        """
        store = JobStore(tmp_path)
        _make_job(
            store, max_iters=6, compact_every=3,
            slack_thread_ts="1778713006.341509",
        )
        backend = _StubBackend()

        with _install_stub_patches(backend):
            await run_job("deadbeef", state_dir=tmp_path)

        # Skip compact-trigger prompts (they're literally "/compact").
        user_prompts = [
            p for p, _ in backend.calls if p != runner.COMPACT_TRIGGER
        ]
        assert len(user_prompts) == 6

        for idx, prompt in enumerate(user_prompts, start=1):
            assert "1778713006.341509" in prompt, (
                f"Iteration {idx}'s prompt missing the thread_ts. This "
                f"is the regression: post-compaction iters lose the "
                f"thread reminder and the agent posts top-level DMs."
            )
            assert "--thread 1778713006.341509" in prompt, (
                f"Iteration {idx}'s prompt missing the '--thread <ts>' "
                f"usage hint."
            )

    @pytest.mark.asyncio
    async def test_thread_notice_omitted_when_no_thread_ts(self, tmp_path):
        store = JobStore(tmp_path)
        _make_job(store, max_iters=4)  # slack_thread_ts defaults to ""
        backend = _StubBackend()

        with _install_stub_patches(backend):
            await run_job("deadbeef", state_dir=tmp_path)

        for prompt, _ in backend.calls:
            assert "Slack threading" not in prompt
            assert "--thread" not in prompt


# ---------------------------------------------------------------------------
# _dispatch_turn signal handshake
# ---------------------------------------------------------------------------

class TestDispatchTurnSignalHandshake:
    @pytest.mark.asyncio
    async def test_raises_stuck_escalation_when_signal_fires(self, tmp_path):
        """_dispatch_turn converts a backend exception that coincides
        with ``escalation_signal`` being set into StuckWatchdogEscalation.
        """
        store = JobStore(tmp_path)
        meta = _make_job(store, max_iters=1, stuck_timeout=1)

        async def fake_watchdog(*, stop_event, escalation_signal=None, **kwargs):
            await asyncio.sleep(0.1)
            if escalation_signal is not None:
                escalation_signal.set()

        class _SimKill:
            async def open_session(self, *, resume_id=None):
                return _FakeSession()

            async def run(self, *a, **kw):
                await asyncio.sleep(0.2)
                raise RuntimeError("simulated backend stream closure")

        backend = _SimKill()
        agent_cfg = runner._CLASSIFY_CFG
        session = await backend.open_session()

        with patch("tigerharness.task_runner.runner.stuck_watchdog",
                   fake_watchdog):
            try:
                with pytest.raises(StuckWatchdogEscalation) as exc_info:
                    await _dispatch_turn(
                        backend, agent_cfg, session, "test prompt",
                        store.run_log("deadbeef"), meta,
                        iter_num=1, job_id="deadbeef",
                    )
                assert exc_info.value.iter_num == 1
            finally:
                await session.close()

    @pytest.mark.asyncio
    async def test_propagates_real_backend_errors(self, tmp_path):
        """A genuine backend error (signal not set) propagates unchanged."""
        store = JobStore(tmp_path)
        meta = _make_job(store, max_iters=1, stuck_timeout=60)

        async def quiet_watchdog(**kwargs):
            await asyncio.sleep(10)  # will get cancelled

        class _Failing:
            async def open_session(self, *, resume_id=None):
                return _FakeSession()

            async def run(self, *a, **kw):
                raise RuntimeError("genuine backend error")

        backend = _Failing()
        agent_cfg = runner._CLASSIFY_CFG
        session = await backend.open_session()

        with patch("tigerharness.task_runner.runner.stuck_watchdog",
                   quiet_watchdog):
            try:
                with pytest.raises(RuntimeError, match="genuine backend error"):
                    await _dispatch_turn(
                        backend, agent_cfg, session, "test prompt",
                        store.run_log("deadbeef"), meta,
                        iter_num=1, job_id="deadbeef",
                    )
            finally:
                await session.close()


# ---------------------------------------------------------------------------
# Edge cases in the stuck-recovery path inside run_job
# ---------------------------------------------------------------------------

class TestStuckRecoveryEdgeCases:
    @pytest.mark.asyncio
    async def test_session_close_failure_is_swallowed_on_stuck(self, tmp_path):
        """If session.close() raises during stuck recovery, the runner
        logs and continues to re-open the session."""
        store = JobStore(tmp_path)
        _make_job(store, max_iters=2)
        backend = _StubBackend()

        async def mock_dispatch_turn(
            backend_, agent_cfg, session, prompt, log_path, meta,
            *, iter_num, job_id, is_last_iter=False,
        ):
            if iter_num == 1:
                raise StuckWatchdogEscalation(iter_num, reason="forced")
            return await runner._dispatch_one(
                backend_, agent_cfg, session, prompt, log_path,
                kind="turn", iter_num=iter_num, job_id=job_id,
            )

        # Patch session.close to raise on the FIRST close (after stuck);
        # subsequent closes are fine.
        original_open_session = backend.open_session
        close_calls = {"n": 0}

        async def bad_close_session(*, resume_id=None):
            sess = await original_open_session(resume_id=resume_id)
            real_close = sess.close

            async def raising_close():
                close_calls["n"] += 1
                if close_calls["n"] == 1:
                    raise RuntimeError("close failed")
                await real_close()

            sess.close = raising_close
            return sess

        backend.open_session = bad_close_session  # type: ignore[method-assign]

        with _install_stub_patches(backend), \
             patch("tigerharness.task_runner.runner._dispatch_turn",
                   mock_dispatch_turn):
            rc = await run_job("deadbeef", state_dir=tmp_path)

        # Should complete normally despite session.close raising once.
        assert rc == 0
        assert store.get("deadbeef").status == "done"
        assert close_calls["n"] >= 1

    @pytest.mark.asyncio
    async def test_session_reopen_failure_ends_as_error(self, tmp_path):
        """If we can't re-open the session after stuck, the job ends as
        error with a 'could not resume session' message."""
        store = JobStore(tmp_path)
        _make_job(store, max_iters=3)
        backend = _StubBackend()

        async def mock_dispatch_turn(
            backend_, agent_cfg, session, prompt, log_path, meta,
            *, iter_num, job_id, is_last_iter=False,
        ):
            raise StuckWatchdogEscalation(iter_num, reason="forced")

        original_open = backend.open_session
        call_count = {"n": 0}

        async def open_then_fail(*, resume_id=None):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise RuntimeError("backend unavailable")
            return await original_open(resume_id=resume_id)

        backend.open_session = open_then_fail  # type: ignore[method-assign]

        with _install_stub_patches(backend), \
             patch("tigerharness.task_runner.runner._dispatch_turn",
                   mock_dispatch_turn):
            rc = await run_job("deadbeef", state_dir=tmp_path)

        assert rc == 1
        final = store.get("deadbeef")
        assert final.status == "error"
        assert "could not resume session" in (final.error or "")
        assert "backend unavailable" in (final.error or "")

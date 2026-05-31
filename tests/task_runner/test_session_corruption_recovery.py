"""Runner-level tests for the upstream thinking-block 400 recovery path.

When the ``claude`` CLI / Anthropic API trips the
``'thinking' or 'redacted_thinking' blocks in the latest assistant
message cannot be modified`` 400 mid-turn, the session it was operating
on becomes permanently poisoned: every subsequent ``--resume`` of that
session re-trips the same 400 instantly because the on-disk transcript
contains an invalid signature. Tigerharness can't fix the upstream bug,
but the runner can detect it and abandon the poisoned session so the
next iteration starts fresh instead of cascading errors across the
whole iteration budget.

Detection lives in ``_dispatch_one``; recovery lives in ``run_job``'s
main loop. Pair tested here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from unittest.mock import patch

import pytest

from tigerharness.task_runner.personas import clear_registry, register_persona
from tigerharness.task_runner.registry import JobMeta, JobStore
from tigerharness.task_runner import runner
from tigerharness.task_runner.runner import (
    SESSION_CORRUPTION_RETRY_BUDGET,
    SessionCorruptedError,
    _dispatch_one,
    _is_thinking_block_corruption,
    run_job,
)


# ---------------------------------------------------------------------------
# Predicate
# ---------------------------------------------------------------------------

class TestIsThinkingBlockCorruption:
    """Pure-function predicate. No I/O."""

    def test_matches_canonical_400_text(self):
        text = (
            "API Error: 400 messages.1.content.13: `thinking` or "
            "`redacted_thinking` blocks in the latest assistant message "
            "cannot be modified. These blocks must remain as they were "
            "in the original response."
        )
        assert _is_thinking_block_corruption(text, "error") is True

    def test_case_insensitive(self):
        """Match is robust to upstream re-casing the message."""
        text = (
            "Blocks In The Latest Assistant Message Cannot Be Modified."
        )
        assert _is_thinking_block_corruption(text, "error") is True

    def test_requires_error_stop_reason(self):
        """A normal assistant turn that happens to *quote* the phrase
        must NOT be flagged -- only error-shaped results poison sessions."""
        text = (
            "I read the docs: 'blocks in the latest assistant message "
            "cannot be modified' is a known API limitation."
        )
        assert _is_thinking_block_corruption(text, "end_turn") is False

    def test_no_match_for_unrelated_error(self):
        text = "API Error: 503 Service temporarily unavailable."
        assert _is_thinking_block_corruption(text, "error") is False

    def test_empty_text_not_a_match(self):
        assert _is_thinking_block_corruption("", "error") is False

    def test_none_stop_reason_not_a_match(self):
        text = "blocks in the latest assistant message cannot be modified"
        assert _is_thinking_block_corruption(text, None) is False


# ---------------------------------------------------------------------------
# _dispatch_one detection
# ---------------------------------------------------------------------------

@dataclass
class _FakeResult:
    final_output: str
    stop_reason: str = "end_turn"
    cost_usd: float = 0.01
    transcript: list = None
    usage: dict | None = None


class _FakeSession:
    def __init__(self) -> None:
        self.id = "stub-sess-abc"

    async def close(self) -> None:
        return


class _FakeBackendReturning:
    """Backend whose single ``run`` returns a caller-controlled result."""
    def __init__(self, result: _FakeResult) -> None:
        self._result = result

    async def open_session(self, *, resume_id=None):
        return _FakeSession()

    async def run(self, config, prompt, *, session=None, approval=None):
        return self._result


class TestDispatchOneDetection:
    @pytest.mark.asyncio
    async def test_raises_on_thinking_block_400(self, tmp_path):
        """The error text comes back as the assistant's `final_output`
        with `stop_reason='error'`. _dispatch_one logs the turn entry
        first, then raises SessionCorruptedError so the runner can
        distinguish this from a regular error result."""
        result = _FakeResult(
            final_output=(
                "API Error: 400 messages.1.content.13: `thinking` or "
                "`redacted_thinking` blocks in the latest assistant "
                "message cannot be modified. These blocks must remain "
                "as they were in the original response."
            ),
            stop_reason="error",
        )
        backend = _FakeBackendReturning(result)
        session = await backend.open_session()
        log_path = tmp_path / "run.log"

        with pytest.raises(SessionCorruptedError) as exc_info:
            await _dispatch_one(
                backend, runner._CLASSIFY_CFG, session, "do the thing",
                log_path, kind="turn", iter_num=1, job_id="job-xyz",
            )

        assert exc_info.value.iter_num == 1
        assert "thinking" in exc_info.value.preview.lower()

        log_text = log_path.read_text()
        # Both the original turn entry AND the corruption marker were emitted.
        assert '"stop_reason": "error"' in log_text
        assert '"kind": "session_corruption_detected"' in log_text

    @pytest.mark.asyncio
    async def test_does_not_raise_on_unrelated_error(self, tmp_path):
        """A 503 or generic error stop_reason still returns normally
        (no recovery needed); the runner's existing classifier handles
        it via the 'consecutive_error' early-exit path."""
        result = _FakeResult(
            final_output="API Error: 503 Service temporarily unavailable.",
            stop_reason="error",
            cost_usd=0.0,
        )
        backend = _FakeBackendReturning(result)
        session = await backend.open_session()
        log_path = tmp_path / "run.log"

        text, cost = await _dispatch_one(
            backend, runner._CLASSIFY_CFG, session, "x", log_path,
            kind="turn", iter_num=1, job_id="job-xyz",
        )
        assert "503" in text
        assert cost == 0.0

    @pytest.mark.asyncio
    async def test_does_not_raise_on_success(self, tmp_path):
        result = _FakeResult(
            final_output="all good, here is the work",
            stop_reason="end_turn",
        )
        backend = _FakeBackendReturning(result)
        session = await backend.open_session()
        log_path = tmp_path / "run.log"

        text, _cost = await _dispatch_one(
            backend, runner._CLASSIFY_CFG, session, "x", log_path,
            kind="turn", iter_num=1, job_id="job-xyz",
        )
        assert text == "all good, here is the work"


# ---------------------------------------------------------------------------
# run_job recovery loop
# ---------------------------------------------------------------------------

_CORRUPT_TEXT = (
    "API Error: 400 messages.1.content.13: `thinking` or "
    "`redacted_thinking` blocks in the latest assistant message "
    "cannot be modified. These blocks must remain as they were in "
    "the original response."
)


class _ScriptedSession:
    def __init__(self, label: str) -> None:
        self.id = ""
        self.label = label

    async def close(self) -> None:
        return


class _ScriptedBackend:
    """Backend whose per-iteration behaviour is driven by a script.

    Each call to ``run`` consumes one entry from ``script``. An entry of
    ``"corrupt"`` returns a corruption-shaped result; ``"ok"`` returns a
    successful result. Sessions are minted fresh on every ``open_session``
    -- a fresh session has ``id=""`` until the first run, which mirrors
    real ``claude_p`` behaviour and lets us verify recovery clears the
    id between iterations.
    """
    def __init__(self, script: list[str]) -> None:
        self._script = list(script)
        self._call = 0
        self.calls: list[tuple[str, str, str]] = []  # (label, prompt, session_id)
        self.sessions_opened: list[str] = []  # session "labels" (sequence index)

    async def open_session(self, *, resume_id=None):
        label = f"sess-{len(self.sessions_opened)}"
        self.sessions_opened.append(label)
        sess = _ScriptedSession(label)
        if resume_id:
            sess.id = resume_id
        return sess

    async def run(self, config, prompt, *, session=None, approval=None):
        idx = self._call
        self._call += 1
        action = self._script[idx] if idx < len(self._script) else "ok"

        if session is not None and not session.id:
            session.id = f"id-{session.label}"
        self.calls.append(
            (getattr(session, "label", ""), prompt, session.id if session else "")
        )

        if action == "corrupt":
            return _FakeResult(
                final_output=_CORRUPT_TEXT,
                stop_reason="error",
                cost_usd=0.05,
            )
        return _FakeResult(
            final_output=f"ok-{idx}", stop_reason="end_turn", cost_usd=0.01,
        )


@pytest.fixture(autouse=True)
def _clean_personas():
    clear_registry()
    register_persona("tester", prompt="You are a tester.", cwd="/tmp")
    yield
    clear_registry()


def _make_job(
    store: JobStore,
    *,
    job_id: str = "deadbeef",
    max_iters: int = 3,
    prompt: str = "diagnose the foo bug",
    name: str = "corrupt-test",
    stuck_timeout: int = 0,
) -> JobMeta:
    """stuck_timeout=0 disables the watchdog path so these tests focus
    on the corruption-recovery wiring without watchdog plumbing."""
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
        stuck_timeout=stuck_timeout,
        slack_thread_ts="",
    )
    store.set(meta)
    store.prompt_path(job_id).write_text(prompt)
    return meta


def _install_stub_patches(backend: _ScriptedBackend):
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


class TestRunJobRecovery:
    @pytest.mark.asyncio
    async def test_iter_1_corruption_recovers_on_iter_2(self, tmp_path):
        """Iter 1 dies with thinking-block 400 -> iter 2 starts a fresh
        session and re-sends the ORIGINAL prompt (not a continuation) ->
        iter 3 succeeds, job ends 'done'."""
        store = JobStore(tmp_path)
        _make_job(store, max_iters=3, prompt="diagnose the foo bug")
        backend = _ScriptedBackend(["corrupt", "ok", "ok"])

        with _install_stub_patches(backend):
            rc = await run_job("deadbeef", state_dir=tmp_path)

        assert rc == 0
        final = store.get("deadbeef")
        assert final.status == "done"

        # Two sessions opened: the original + one fresh post-recovery.
        assert len(backend.sessions_opened) >= 2

        # Iter 2 (first dispatch on the fresh session) must carry the
        # original task prompt -- the agent has never received it.
        iter_2_call = backend.calls[1]
        prompt_2 = iter_2_call[1]
        assert prompt_2.startswith(runner.TASK_PREAMBLE), (
            "after corruption recovery, the next iter must re-send the "
            "original task prompt because the prior session is gone"
        )
        assert "diagnose the foo bug" in prompt_2

        # Iter 2 ran on a DIFFERENT session label than iter 1.
        assert iter_2_call[0] != backend.calls[0][0]

        # log_path captured both detection and recovery markers.
        log_text = store.run_log("deadbeef").read_text()
        assert '"kind": "session_corruption_detected"' in log_text
        assert '"kind": "session_corruption_recovery"' in log_text
        assert '"action": "fresh-session-restart"' in log_text

    @pytest.mark.asyncio
    async def test_two_consecutive_corruptions_bail(self, tmp_path):
        """Recovery itself trips the same error -> job ends 'error' with
        a clear message. We must NOT keep burning iterations."""
        store = JobStore(tmp_path)
        _make_job(store, max_iters=5)
        backend = _ScriptedBackend(["corrupt", "corrupt", "ok", "ok", "ok"])

        with _install_stub_patches(backend):
            rc = await run_job("deadbeef", state_dir=tmp_path)

        assert rc == 1
        final = store.get("deadbeef")
        assert final.status == "error"
        assert "thinking-block 400" in (final.error or "")
        assert f"{SESSION_CORRUPTION_RETRY_BUDGET} consecutive" in (
            final.error or ""
        )

        # Only two dispatches happened -- recovery did not keep retrying.
        assert len(backend.calls) == 2

        log_text = store.run_log("deadbeef").read_text()
        assert '"action": "aborting"' in log_text

    @pytest.mark.asyncio
    async def test_corruption_counter_resets_on_success(self, tmp_path):
        """A successful iter between two corruptions must reset the
        counter, so the second corruption is treated as the START of a
        new recovery attempt, not the 2nd of a fatal pair."""
        store = JobStore(tmp_path)
        _make_job(store, max_iters=5)
        backend = _ScriptedBackend(
            ["corrupt", "ok", "corrupt", "ok", "ok"]
        )

        with _install_stub_patches(backend):
            rc = await run_job("deadbeef", state_dir=tmp_path)

        assert rc == 0, (
            "two corruptions separated by a successful iter must NOT "
            "trip the consecutive-corruption fatal cap"
        )
        final = store.get("deadbeef")
        assert final.status == "done"

    @pytest.mark.asyncio
    async def test_close_failure_during_recovery_is_swallowed(self, tmp_path):
        """If the poisoned ``session.close()`` raises, recovery still
        proceeds -- the close was best-effort anyway."""
        store = JobStore(tmp_path)
        _make_job(store, max_iters=3)

        class _BadCloseBackend(_ScriptedBackend):
            async def open_session(self, *, resume_id=None):
                sess = await super().open_session(resume_id=resume_id)

                async def _bad_close():
                    raise RuntimeError("close failed -- broken pipe")
                sess.close = _bad_close  # type: ignore[method-assign]
                return sess

        backend = _BadCloseBackend(["corrupt", "ok", "ok"])

        with _install_stub_patches(backend):
            rc = await run_job("deadbeef", state_dir=tmp_path)

        assert rc == 0, "close failure must not abort the recovery"
        assert store.get("deadbeef").status == "done"

    @pytest.mark.asyncio
    async def test_open_failure_during_recovery_aborts_job(self, tmp_path):
        """If we can't open a fresh session, the runner must bail with
        a clear error rather than enter an infinite retry loop."""
        store = JobStore(tmp_path)
        _make_job(store, max_iters=3)

        class _FlakyOpenBackend(_ScriptedBackend):
            def __init__(self, script):
                super().__init__(script)
                self._opens_seen = 0

            async def open_session(self, *, resume_id=None):
                self._opens_seen += 1
                if self._opens_seen >= 2:
                    raise RuntimeError("rate limited; cannot open session")
                return await super().open_session(resume_id=resume_id)

        backend = _FlakyOpenBackend(["corrupt", "ok", "ok"])

        with _install_stub_patches(backend):
            rc = await run_job("deadbeef", state_dir=tmp_path)

        assert rc == 1
        final = store.get("deadbeef")
        assert final.status == "error"
        assert "could not open replacement session" in (final.error or "")
        assert "rate limited" in (final.error or "")

    @pytest.mark.asyncio
    async def test_iter_log_documents_recovery(self, tmp_path):
        """The persona-facing task_journal must record the recovery so
        humans (and the persona's next turn) can see what happened."""
        store = JobStore(tmp_path)
        meta = _make_job(store, max_iters=3, name="journal-test")
        meta.cwd = str(tmp_path)  # keep iter_log inside the test tmp
        store.set(meta)
        backend = _ScriptedBackend(["corrupt", "ok", "ok"])

        with _install_stub_patches(backend):
            await run_job("deadbeef", state_dir=tmp_path)

        iter_log = tmp_path / "task_journal" / "journal-test--deadbeef.md"
        assert iter_log.exists()
        content = iter_log.read_text()
        assert "extended-thinking-block 400" in content
        # Italics open/close balanced.
        assert content.count("_[") == content.count("]_")

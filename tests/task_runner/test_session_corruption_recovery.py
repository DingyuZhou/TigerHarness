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

import asyncio
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

    @pytest.mark.asyncio
    async def test_compact_turn_corruption_recovers(self, tmp_path):
        """Corruption raised by the /compact dispatch (not the user turn)
        must be caught by the compact-site try/except -- not escape to
        the outer ``except Exception`` and kill the job with a traceback.

        With ``compact_every=1`` the runner fires /compact after every
        successful user turn. Script entry 0 is the iter-1 user turn
        (``ok``); entry 1 is the immediately-following compact turn
        (``corrupt``). Without the compact-path fix, that corruption
        escapes to the outer ``except`` and the job ends ``error`` with
        an ``exception`` log entry. With the fix, the runner skips the
        compact and the loop continues; the next iter's user turn trips
        the same error and hits the main recovery branch.

        This test also pins the compact-site cost + session_id-clear
        invariants the verifier flagged (cluster A follow-up):
          * the corrupted compact turn's $0.05 must be folded into
            total_cost_usd -- the backend charged for it even though
            the response was unusable;
          * meta.session_id on disk must be CLEARED at the compact-catch
            site (verified mid-run via a backend that snapshots disk
            state on its next call) so a SIGKILL/OOM between compact-
            catch and the main recovery doesn't leave a poisoned id
            that `tigerharness continue` would pass as --resume-session.
        """
        store = JobStore(tmp_path)
        meta = _make_job(store, max_iters=3)
        meta.compact_every = 1
        store.set(meta)

        # Snapshot disk meta.session_id every time the backend gets called
        # so we can prove the compact-catch persisted "" before iter-2's
        # main recovery runs.
        class _SnapshottingBackend(_ScriptedBackend):
            def __init__(self, script, store_, job_id_):
                super().__init__(script)
                self._store = store_
                self._job_id = job_id_
                self.session_id_snapshots: list[str] = []

            async def run(self, config, prompt, *, session=None, approval=None):
                m = self._store.get(self._job_id)
                self.session_id_snapshots.append(
                    m.session_id if m else ""
                )
                return await super().run(
                    config, prompt, session=session, approval=approval,
                )

        # iter1 user-turn ok ($0.01) -> iter1 compact corrupts ($0.05) ->
        # iter2 user-turn also corrupts (still-poisoned session) ($0.05)
        # -> main recovery opens fresh session -> iter3 user-turn ok
        # ($0.01) -> done. Expected total cost: 0.12.
        backend = _SnapshottingBackend(
            ["ok", "corrupt", "corrupt", "ok"], store, "deadbeef",
        )

        with _install_stub_patches(backend):
            rc = await run_job("deadbeef", state_dir=tmp_path)

        final = store.get("deadbeef")
        assert rc == 0
        assert final.status == "done", (
            f"compact-turn corruption should be caught by the recovery "
            f"branch, not the outer except; got status={final.status!r} "
            f"error={final.error!r}"
        )
        # Negative: outer-exception path was NOT taken.
        assert not final.error
        log_text = store.run_log("deadbeef").read_text()
        assert '"kind": "exception"' not in log_text, (
            "compact-turn corruption fell through to the generic "
            "exception handler instead of being recovered"
        )
        # Positive: the compact-site detection marker fired.
        assert '"kind": "session_corruption_detected_in_compact"' in log_text

        # Cost: 0.01 (iter1 ok) + 0.05 (compact corrupt) + 0.05 (iter2
        # user-turn corrupt) + 0.01 (iter3 ok) = 0.12. Without the
        # compact-site cost fold the total would be 0.07.
        assert final.total_cost_usd == pytest.approx(0.12), (
            "compact-corruption cost was dropped from total_cost_usd; "
            f"got {final.total_cost_usd}"
        )

        # session_id on disk after compact-catch (snapshot taken at iter-2's
        # backend call) must be "" -- the compact-catch persisted the
        # cleared id before continuing. Without the fix this snapshot
        # would hold the iter-1-success session_id (which is now
        # poisoned). The snapshots are taken BEFORE the backend run, so:
        #   [0] iter-1 user-turn  -> "" (initial)
        #   [1] iter-1 compact    -> iter-1 success session_id (poisoned)
        #   [2] iter-2 user-turn  -> "" (CLEARED by compact-catch fix)
        #   [3] iter-3 user-turn  -> "" (CLEARED by main recovery)
        assert backend.session_id_snapshots[2] == "", (
            "compact-catch must persist meta.session_id='' before "
            "continuing -- otherwise a crash between compact-catch and "
            "next-iter recovery leaves the poisoned id on disk for "
            "tigerharness continue. Got "
            f"{backend.session_id_snapshots[2]!r}"
        )

    @pytest.mark.asyncio
    async def test_compact_corruption_with_zero_cost_does_not_inflate_total(self, tmp_path):
        """Compact-turn corruption reported as $0 must NOT add to total
        (false branch of ``if ccorrupt.cost_usd:`` at the compact-catch
        site). Mirrors the main-turn zero-cost test."""
        store = JobStore(tmp_path)
        meta = _make_job(store, max_iters=2)
        meta.compact_every = 1
        store.set(meta)

        class _ZeroCostCompactBackend(_ScriptedBackend):
            async def run(self, config, prompt, *, session=None, approval=None):
                idx = self._call
                self._call += 1
                action = self._script[idx] if idx < len(self._script) else "ok"
                if session is not None and not session.id:
                    session.id = f"id-{session.label}"
                self.calls.append(
                    (getattr(session, "label", ""), prompt,
                     session.id if session else "")
                )
                if action == "corrupt":
                    return _FakeResult(
                        final_output=_CORRUPT_TEXT,
                        stop_reason="error",
                        cost_usd=0.0,  # zero cost variant
                    )
                return _FakeResult(
                    final_output=f"ok-{idx}",
                    stop_reason="end_turn",
                    cost_usd=0.01,
                )

        # iter1 user-turn ok ($0.01) -> iter1 compact corrupts ($0) ->
        # iter2 user-turn corrupts ($0) -> main recovery -> would need
        # more iters, but max_iters=2 means iter2 is the final iter and
        # the final-iter guard ends as error. That's fine -- we only
        # care that the false branch of `if ccorrupt.cost_usd:` is hit.
        backend = _ZeroCostCompactBackend(["ok", "corrupt", "corrupt"])

        with _install_stub_patches(backend):
            await run_job("deadbeef", state_dir=tmp_path)

        final = store.get("deadbeef")
        # Only the successful iter's $0.01 should be billed (zero-cost
        # corruptions contributed nothing).
        assert final.total_cost_usd == pytest.approx(0.01), (
            "zero-cost compact corruption inflated total_cost_usd; "
            f"got {final.total_cost_usd}"
        )

    @pytest.mark.asyncio
    async def test_abort_path_clears_session_id_after_prior_success(self, tmp_path):
        """After a successful iter populates ``meta.session_id``, two
        consecutive corruptions bail out via the abort path. On disk,
        ``meta.session_id`` must be ``""`` so a subsequent
        ``tigerharness continue`` does not re-pass the poisoned id as
        ``--resume-session`` (which would re-trip the same 400). This
        is the regression test the verifier flagged for cluster B:
        ``[corrupt, corrupt]`` from the start passes even with the
        pre-fix code because there is no poisoned id to clear -- only
        a prior successful iter materialises the cluster B failure."""
        store = JobStore(tmp_path)
        _make_job(store, max_iters=5)
        # iter1 ok -> meta.session_id populated. iter2 corrupt (consec=1
        # recovery). iter3 corrupt (consec=2 abort).
        backend = _ScriptedBackend(["ok", "corrupt", "corrupt", "ok", "ok"])

        with _install_stub_patches(backend):
            rc = await run_job("deadbeef", state_dir=tmp_path)

        assert rc == 1
        final = store.get("deadbeef")
        assert final.status == "error"
        # The critical cluster B invariant: even on the abort path, the
        # on-disk session_id is cleared so `tigerharness continue` does
        # not poison the next subprocess via --resume-session.
        assert final.session_id == "", (
            f"abort path must persist session_id='' (cluster B); "
            f"got {final.session_id!r}"
        )

    @pytest.mark.asyncio
    async def test_corruption_on_final_iter_ends_error(self, tmp_path):
        """Corruption during the FINAL iteration must end the job as
        ``error`` -- not silently grant an extra (unauthorized) iter.

        ``max_iters=2`` + script ``[ok, corrupt]``: iter-1 succeeds,
        iter-2 corrupts. Without the final-iter guard, the recovery
        branch ``continue``s, the while-loop bumps ``i`` to 3, and an
        unbudgeted iter-3 runs. With the guard, only two dispatches
        happen and the job ends ``error`` with a clear explanation.
        """
        store = JobStore(tmp_path)
        _make_job(store, max_iters=2)
        backend = _ScriptedBackend(["ok", "corrupt", "ok"])

        with _install_stub_patches(backend):
            rc = await run_job("deadbeef", state_dir=tmp_path)

        final = store.get("deadbeef")
        # Positive: final iter corruption -> error, no recovery iter.
        assert rc == 1
        assert final.status == "error"
        # Negative: the job did NOT silently complete by burning a
        # bonus iteration past the cap.
        assert final.status != "done"
        assert len(backend.calls) == 2, (
            f"final-iter corruption must NOT spend an iter past "
            f"max_iters; got {len(backend.calls)} backend calls"
        )
        # The error message names the situation so an operator can
        # tell this apart from the consecutive-corruption bail.
        assert "final" in (final.error or "").lower()

    @pytest.mark.asyncio
    async def test_slack_thread_ts_preserved_across_recovery(self, tmp_path):
        """A concurrent CLI/slack update to ``slack_thread_ts`` during a
        long dispatch must NOT be clobbered by the recovery branch's
        ``store.set(meta)``. The recovery branch must re-read live meta
        before persisting, mirroring the successful-turn path.

        Setup: the job starts with ``slack_thread_ts='T_OLD'``. A custom
        backend simulates a concurrent update by writing
        ``slack_thread_ts='T_NEW'`` to disk at the start of the corrupt
        call (i.e. before recovery runs ``store.set``). Without the
        fix, recovery rewrites the in-memory ``T_OLD`` back to disk and
        iter-2's re-sent prompt threads to ``T_OLD``. With the fix the
        on-disk value is ``T_NEW`` and the resent prompt threads under
        ``T_NEW``.
        """
        store = JobStore(tmp_path)
        meta = _make_job(store, max_iters=3)
        meta.slack_thread_ts = "T_OLD"
        store.set(meta)

        class _MutatingBackend(_ScriptedBackend):
            def __init__(self, script, store_, job_id_, new_ts):
                super().__init__(script)
                self._store = store_
                self._job_id = job_id_
                self._new_ts = new_ts
                self._mutated = False

            async def run(self, config, prompt, *, session=None, approval=None):
                # Simulate a concurrent CLI write to slack_thread_ts
                # that lands DURING the (about-to-corrupt) dispatch.
                if not self._mutated:
                    m = self._store.get(self._job_id)
                    m.slack_thread_ts = self._new_ts
                    self._store.set(m)
                    self._mutated = True
                return await super().run(
                    config, prompt, session=session, approval=approval,
                )

        backend = _MutatingBackend(
            ["corrupt", "ok", "ok"], store, "deadbeef", "T_NEW",
        )

        with _install_stub_patches(backend):
            rc = await run_job("deadbeef", state_dir=tmp_path)

        assert rc == 0
        final = store.get("deadbeef")
        # Positive: the concurrent update survived recovery.
        assert final.slack_thread_ts == "T_NEW", (
            "recovery branch clobbered a concurrent slack_thread_ts "
            "update; expected T_NEW, got " + repr(final.slack_thread_ts)
        )
        # Negative: it is NOT the pre-recovery value.
        assert final.slack_thread_ts != "T_OLD"
        # And the re-sent original prompt on iter-2 threaded under the
        # mutated ts (so DMs from the recovered iter land in the right
        # Slack thread).
        iter_2_prompt = backend.calls[1][1]
        assert "T_NEW" in iter_2_prompt
        assert "T_OLD" not in iter_2_prompt

    @pytest.mark.asyncio
    async def test_cost_accumulated_across_recovery(self, tmp_path):
        """The cost of the failed (corrupted) turn must still be summed
        into ``total_cost_usd`` -- the user IS billed for that round
        trip even though the response was unusable. Without the cost
        fix the corruption-turn cost is dropped on the floor and the
        total reflects only successful iters.

        Script ``[corrupt, ok]``: corruption-turn cost = 0.05,
        success-turn cost = 0.01. With the fix the total is 0.06;
        without it the total is 0.01.
        """
        store = JobStore(tmp_path)
        _make_job(store, max_iters=2)
        backend = _ScriptedBackend(["corrupt", "ok"])

        with _install_stub_patches(backend):
            rc = await run_job("deadbeef", state_dir=tmp_path)

        assert rc == 0
        final = store.get("deadbeef")
        assert final.status == "done"
        # Positive: the failed-turn cost (0.05) is summed.
        assert final.total_cost_usd == pytest.approx(0.06), (
            "corruption-turn cost was dropped from total_cost_usd; "
            f"got {final.total_cost_usd}"
        )
        # Negative: the total is NOT just the success-turn cost.
        assert final.total_cost_usd != pytest.approx(0.01)

    @pytest.mark.asyncio
    async def test_corruption_with_zero_cost_does_not_add_to_total(self, tmp_path):
        """A corruption-turn that the backend reports as $0 (e.g. the API
        returned the 400 before any token charge accrued) must NOT add
        anything to total_cost_usd. Covers the false branch of the
        ``if corrupt.cost_usd:`` guard in the recovery handler."""
        store = JobStore(tmp_path)
        _make_job(store, max_iters=2)

        class _ZeroCostCorruptBackend(_ScriptedBackend):
            async def run(self, config, prompt, *, session=None, approval=None):
                # Same as _ScriptedBackend.run but force corrupt cost to 0.
                idx = self._call
                self._call += 1
                action = self._script[idx] if idx < len(self._script) else "ok"
                if session is not None and not session.id:
                    session.id = f"id-{session.label}"
                self.calls.append(
                    (getattr(session, "label", ""), prompt,
                     session.id if session else "")
                )
                if action == "corrupt":
                    return _FakeResult(
                        final_output=_CORRUPT_TEXT,
                        stop_reason="error",
                        cost_usd=0.0,
                    )
                return _FakeResult(
                    final_output=f"ok-{idx}",
                    stop_reason="end_turn",
                    cost_usd=0.01,
                )

        backend = _ZeroCostCorruptBackend(["corrupt", "ok"])

        with _install_stub_patches(backend):
            rc = await run_job("deadbeef", state_dir=tmp_path)

        assert rc == 0
        final = store.get("deadbeef")
        assert final.status == "done"
        # Only the successful iter's $0.01 should be billed.
        assert final.total_cost_usd == pytest.approx(0.01), (
            "zero-cost corruption should not inflate total_cost_usd; "
            f"got {final.total_cost_usd}"
        )

    @pytest.mark.asyncio
    async def test_watchdog_wrapper_propagates_session_corrupted_unwrapped(
        self, tmp_path,
    ):
        """When _dispatch_turn runs with the stuck-watchdog active
        (stuck_timeout > 0) AND the watchdog's escalation_signal IS set
        BEFORE the dispatch raises, the wrapper must re-raise
        SessionCorruptedError UNWRAPPED -- not relabel it as
        StuckWatchdogEscalation. Otherwise the runner would take the
        stuck-recovery path (which re-resumes meta.session_id) instead of
        the corruption-recovery path (which clears it), and the next
        iter would re-trip the same 400 instantly.

        The race must be GENUINE: a fast dispatch that raises before the
        watchdog sleeps would pass even without the cluster D fix (the
        unrelated generic ``except Exception`` only relabels when
        escalation_signal.is_set() at the moment it inspects it). The
        slow backend below makes the watchdog signal FIRST (at ~50ms)
        and the corruption raise SECOND (at ~200ms), so cluster D's
        ``except SessionCorruptedError`` is what prevents the relabel.
        """
        from unittest.mock import patch as _patch

        store = JobStore(tmp_path)
        _make_job(store, max_iters=1, stuck_timeout=1)

        class _SlowCorruptBackend(_ScriptedBackend):
            """Sleeps before returning the corrupt result so the watchdog
            fake gets a chance to set escalation_signal first -- the only
            way to exercise the cluster D race condition."""
            async def run(self, config, prompt, *, session=None, approval=None):
                await asyncio.sleep(0.2)
                return await super().run(
                    config, prompt, session=session, approval=approval,
                )

        backend = _SlowCorruptBackend(["corrupt"])

        async def _watchdog_signals_then_holds(
            *, stop_event, escalation_signal=None, **kwargs
        ):
            # Fire signal BEFORE the slow backend returns. In production
            # this is SIGTERM + 5s grace + SIGKILL racing the CLI stream.
            await asyncio.sleep(0.05)
            if escalation_signal is not None:
                escalation_signal.set()
            await stop_event.wait()

        with _install_stub_patches(backend), \
             _patch("tigerharness.task_runner.runner.stuck_watchdog",
                    _watchdog_signals_then_holds):
            rc = await run_job("deadbeef", state_dir=tmp_path)

        final = store.get("deadbeef")
        # max_iters=1 + corruption on iter-1 = final-iter bail (corruption
        # path), not the stuck-watchdog path.
        assert rc == 1
        assert final.status == "error"
        err = (final.error or "").lower()
        assert "thinking-block 400" in err, (
            "_dispatch_turn must propagate SessionCorruptedError unwrapped "
            "so the corruption-recovery branch handles it; got "
            f"error={final.error!r}"
        )
        # Negative: it is NOT the stuck-watchdog error message.
        assert "stuck-watchdog" not in err

    @pytest.mark.asyncio
    async def test_recovery_notice_injected_into_next_iter_prompt(self, tmp_path):
        """After recovery, the very next iter's prompt must carry the
        recovery notice pointing the persona at the task journal."""
        store = JobStore(tmp_path)
        meta = _make_job(store, max_iters=3, name="notice-test")
        meta.cwd = str(tmp_path)
        store.set(meta)
        backend = _ScriptedBackend(["corrupt", "ok", "ok"])

        with _install_stub_patches(backend):
            rc = await run_job("deadbeef", state_dir=tmp_path)

        assert rc == 0
        # iter-2's prompt (first call on the fresh session) carries the
        # recovery notice.
        iter_2_prompt = backend.calls[1][1]
        assert "Recovery notice" in iter_2_prompt
        assert "extended-thinking-block 400" in iter_2_prompt
        # The journal path is referenced verbatim.
        expected_journal = tmp_path / "task_journal" / "notice-test--deadbeef.md"
        assert str(expected_journal) in iter_2_prompt
        # iter-3's prompt (continuation) does NOT carry the notice --
        # the one-shot flag is cleared after iter-2 consumes it.
        iter_3_prompt = backend.calls[2][1]
        assert "Recovery notice" not in iter_3_prompt

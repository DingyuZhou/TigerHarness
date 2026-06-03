"""End-to-end scripted compile-driver tests for the journal Phase 1.5
in-session compile.

The unit tests in :mod:`test_compile_cli` exercise each CLI handler in
isolation. This module tests the **sequence** -- the actual
back-and-forth a real ``drive-journal`` session would execute against
the journal-side CLIs, with scripted drafter / critic responses
standing in for the LLM. It is the Option C equivalent of the
api-backed ``FakeSessionManager`` discipline.

The ``ScriptedDriver`` class in this module is reusable harness code:
Phase 2 / Phase 3 test files import it from here. The design doc
named this file ``scripted_compile_driver.py``; we prefix with
``test_`` so pytest collects it without a custom ``python_files``
rule, but the harness role is unchanged.

The :class:`ScriptedDriver` class replays a hand-crafted sequence of
turns against the same ``argparse``-dispatched handlers the real
session calls. Each test then composes one realistic compile
trajectory (happy-path, drafter-fixes-tier1-on-retry, critic-loop-
converges-after-2-revises, max-rounds-exhaustion, BLOCK-mid-compile,
kill-during-land, rescue-from-stale-mid-critique, etc.) and asserts
on state transitions at every checkpoint.

The scripted responses are pre-canned text blocks -- no LLM is ever
invoked. The harness models exactly the same Python entry points
(``cmd_compile_context``, ``cmd_compile_prompts``, ``cmd_validate_graph``,
``cmd_land_compile``, ``cmd_compile_fail``, ``cmd_compile_retry``) the
production driver calls, so the integration value here is the
*ordering* and the *state-machine invariants*, not the LLM behaviour.
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tigerharness.journal.compile_cli import (
    cmd_abort,
    cmd_compile_context,
    cmd_compile_fail,
    cmd_compile_prompts,
    cmd_land_compile,
    cmd_validate_graph,
)
from tigerharness.journal.models import CompilePhase, State, Status
from tigerharness.journal.paths import JournalPaths
from tigerharness.journal.scaffold import COMPILE_PERSONAS, new_workflow_task


# ---------------------------------------------------------------------------
# Pre-canned drafter + critic responses
# ---------------------------------------------------------------------------

# A canonical valid drafter bundle the parser accepts and the Tier 1
# validators pass for the default Anzai/Akagi/Ayako + Mitsui roster.
_VALID_BUNDLE = (
    "```steps-bundle\n"
    "## step: 01-anzai-plan\n"
    "---\n"
    "id: 01-anzai-plan\n"
    "persona: Anzai\n"
    "role: planner\n"
    "on_approve: 02-mitsui-impl\n"
    "on_revise: 01-anzai-plan\n"
    "on_block: __escalate__\n"
    "max_iters: 5\n"
    "timeout_sec: 1800\n"
    "parallel_with: []\n"
    "---\n"
    "Plan the work.\n"
    "## step: 02-mitsui-impl\n"
    "---\n"
    "id: 02-mitsui-impl\n"
    "persona: Mitsui\n"
    "role: developer\n"
    "on_approve: __done__\n"
    "on_revise: 02-mitsui-impl\n"
    "on_block: __escalate__\n"
    "max_iters: 5\n"
    "timeout_sec: 1800\n"
    "parallel_with: []\n"
    "---\n"
    "Implement the plan.\n"
    "```\n"
)

# A bundle that parses but fails Tier 1 -- persona "Sakuragi" is not on
# the team roster. The parser accepts it; ``validate_roster`` rejects.
_BAD_ROSTER_BUNDLE = _VALID_BUNDLE.replace(
    "persona: Anzai", "persona: Sakuragi",
)

# A bundle that fails the drafter parser entirely (no fence).
_UNPARSEABLE_BUNDLE = "this is not a steps-bundle\n"


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

def _make_team(root: Path, *, name: str = "Shohoku") -> Path:
    """Build a fake team root with all compile personas + Mitsui."""
    team = root / "teams" / name
    (team / "configs").mkdir(parents=True)
    personas = list(COMPILE_PERSONAS) + ["Mitsui"]
    lines = ["personas:\n"]
    for p in personas:
        lines.append(f"  - name: {p}\n")
    (team / "configs" / "personas.yaml").write_text("".join(lines))
    for p in personas:
        pdir = team / "personas" / p
        pdir.mkdir(parents=True)
        (pdir / "prompt.md").write_text(f"You are {p}.\n")
    return team


@pytest.fixture
def journal_dir(tmp_path):
    d = tmp_path / "journal"
    d.mkdir()
    return d


@pytest.fixture
def team_root(tmp_path, monkeypatch):
    root = _make_team(tmp_path)
    monkeypatch.chdir(root)
    return root


# ---------------------------------------------------------------------------
# The harness
# ---------------------------------------------------------------------------

@dataclass
class CliResult:
    rc: int
    stdout: str
    stderr: str


@dataclass
class ScriptedDriver:
    """Drives the journal compile CLIs in the order a real session
    would. Each method models one CLI invocation; the test composes
    them into a trajectory and asserts state at every checkpoint.

    The driver writes draft / trace / transcript artifacts to the
    journal's ``compile/`` directory exactly as the design's OPERATING.md
    sub-protocol specifies (``round-NN-draft.md`` /
    ``round-NN-akagi.md`` / ``round-NN-ayako.md`` / ``transcript.md``)
    so the assertions can sniff on-disk state directly."""

    paths: JournalPaths
    task_id: str
    round_n: int = 0
    transcript_lines: list[str] = field(default_factory=list)

    # ---- helpers ----

    @property
    def task_dir(self) -> Path:
        return self.paths.task_dir(self.task_id)

    @property
    def compile_dir(self) -> Path:
        d = self.task_dir / "compile"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def status(self) -> Status:
        return Status.from_json(self.paths.status_json(self.task_id).read_text())

    def _invoke(self, fn, ns: argparse.Namespace) -> CliResult:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = fn(ns)
        return CliResult(rc=rc, stdout=out.getvalue(), stderr=err.getvalue())

    # ---- CLI proxies ----

    def compile_context(self) -> CliResult:
        ns = argparse.Namespace(
            journal_dir=str(self.paths.root), task_id=self.task_id,
        )
        return self._invoke(cmd_compile_context, ns)

    def compile_prompts(self, *, kind: str, feedback: str | None = None,
                        draft: str | None = None, trace: str | None = None,
                        ) -> CliResult:
        ns = argparse.Namespace(
            journal_dir=str(self.paths.root),
            task=self.task_id,
            kind=kind,
            feedback=feedback,
            draft=draft,
            trace=trace,
        )
        return self._invoke(cmd_compile_prompts, ns)

    def validate_graph(self, draft_path: Path) -> CliResult:
        ns = argparse.Namespace(
            journal_dir=str(self.paths.root),
            task=self.task_id,
            draft=str(draft_path),
        )
        return self._invoke(cmd_validate_graph, ns)

    def land_compile(self, draft_path: Path, transcript_path: Path,
                     rounds: int) -> CliResult:
        ns = argparse.Namespace(
            journal_dir=str(self.paths.root),
            task=self.task_id,
            draft=str(draft_path),
            transcript=str(transcript_path),
            rounds=rounds,
        )
        return self._invoke(cmd_land_compile, ns)

    def compile_fail(self, reason: str) -> CliResult:
        ns = argparse.Namespace(
            journal_dir=str(self.paths.root),
            task_id=self.task_id,
            reason=reason,
        )
        return self._invoke(cmd_compile_fail, ns)

    def abort(self) -> CliResult:
        ns = argparse.Namespace(
            journal_dir=str(self.paths.root), task_id=self.task_id,
        )
        return self._invoke(cmd_abort, ns)

    # ---- scripted-turn helpers ----

    def begin_round(self) -> int:
        """Bump the round counter and return the new value."""
        self.round_n += 1
        return self.round_n

    def write_draft(self, body: str) -> Path:
        """Save a scripted drafter bundle to ``compile/round-NN-draft.md``."""
        p = self.compile_dir / f"round-{self.round_n:02d}-draft.md"
        p.write_text(body, encoding="utf-8")
        return p

    def write_critic(self, role: str, body: str) -> Path:
        """Save a scripted critic turn to ``compile/round-NN-<role>.md``."""
        p = self.compile_dir / f"round-{self.round_n:02d}-{role}.md"
        p.write_text(body, encoding="utf-8")
        return p

    def write_trace(self, trace_text: str) -> Path:
        """Save a Tier 1 trace text to ``compile/round-NN-trace.md``."""
        p = self.compile_dir / f"round-{self.round_n:02d}-trace.md"
        p.write_text(trace_text, encoding="utf-8")
        return p

    def append_transcript(self, line: str) -> None:
        self.transcript_lines.append(line)

    def write_transcript(self) -> Path:
        p = self.compile_dir / "transcript.md"
        p.write_text("\n".join(self.transcript_lines) + "\n", encoding="utf-8")
        return p


def _new_driver(journal_dir: Path, team_root: Path) -> ScriptedDriver:
    paths = JournalPaths(root=journal_dir)
    result = new_workflow_task(
        brief_text="# Goal\nShip the feature.\n",
        playbook_text=(
            "# Playbook\n\n"
            "Anzai drafts. Akagi reviews. Ayako reviews. Mitsui implements.\n"
        ),
        playbook_name="default",
        team_root=team_root,
        paths=paths,
        captain="Mitsui",
    )
    return ScriptedDriver(paths=paths, task_id=result.task_id)


# ---------------------------------------------------------------------------
# Scenario 1 -- happy-path single round
# ---------------------------------------------------------------------------

class TestHappyPathSingleRound:
    def test_drafter_passes_tier1_both_critics_approve_land(
        self, team_root, journal_dir,
    ):
        d = _new_driver(journal_dir, team_root)

        # 1. status starts as pending / pending.
        assert d.status().state == State.PENDING
        assert d.status().compile_pending is True
        assert d.status().compile_phase == CompilePhase.PENDING

        # 2. driver bootstraps context.
        ctx = d.compile_context()
        assert ctx.rc == 0
        assert "compile-context for task" in ctx.stdout
        assert "Drafter prompt" in ctx.stdout

        # 3. round 1: drafter writes a clean bundle.
        d.begin_round()
        draft_path = d.write_draft(_VALID_BUNDLE)

        # 4. Tier 1 passes -> proceed to critics.
        v = d.validate_graph(draft_path)
        assert v.rc == 0
        envelope = json.loads(v.stdout)
        assert envelope["ok"] is True
        trace_path = d.write_trace(envelope["trace"])

        # 5. Akagi APPROVES.
        akagi = d.compile_prompts(
            kind="akagi", draft=str(draft_path), trace=str(trace_path),
        )
        assert akagi.rc == 0
        d.write_critic("akagi", akagi.stdout + "\nWORKFLOW: APPROVE\n")

        # 6. Ayako APPROVES.
        ayako = d.compile_prompts(
            kind="ayako", draft=str(draft_path), trace=str(trace_path),
        )
        assert ayako.rc == 0
        d.write_critic("ayako", ayako.stdout + "\nWORKFLOW: APPROVE\n")

        # 7. Tier 1 (post-critique) re-runs cleanly -- defensive.
        v2 = d.validate_graph(draft_path)
        assert v2.rc == 0

        # 8. land.
        d.append_transcript("Round 1: APPROVE / APPROVE")
        transcript_path = d.write_transcript()
        landed = d.land_compile(draft_path, transcript_path, rounds=1)
        assert landed.rc == 0, landed.stderr
        assert "landed:" in landed.stdout

        # 9. terminal state checks.
        s = d.status()
        assert s.compile_pending is False
        assert s.compile_phase == CompilePhase.COMPLETE
        td = d.task_dir
        assert (td / "orchestration.json").is_file()
        assert (td / "steps" / "01-anzai-plan.md").is_file()
        assert (td / "steps" / "02-mitsui-impl.md").is_file()
        assert (td / "compile_critique.md").is_file()
        # Round files preserved for audit.
        assert (td / "compile" / "round-01-draft.md").is_file()
        assert (td / "compile" / "round-01-akagi.md").is_file()
        assert (td / "compile" / "round-01-ayako.md").is_file()
        # Promotion cleanup: no leaked compile/final/ tree.
        assert not (td / "compile" / "final").exists()


# ---------------------------------------------------------------------------
# Scenario 2 -- drafter fixes Tier 1 on retry (no critics yet)
# ---------------------------------------------------------------------------

class TestDrafterFixesTier1OnRetry:
    def test_first_draft_fails_roster_second_passes(
        self, team_root, journal_dir,
    ):
        d = _new_driver(journal_dir, team_root)

        # Round 1: bad roster persona.
        d.begin_round()
        bad = d.write_draft(_BAD_ROSTER_BUNDLE)
        v1 = d.validate_graph(bad)
        assert v1.rc == 1
        env1 = json.loads(v1.stdout)
        assert env1["ok"] is False
        assert any(e["validator"] == "roster" for e in env1["errors"])

        # Round 2: drafter re-emits with feedback; this time it parses + validates.
        d.begin_round()
        good = d.write_draft(_VALID_BUNDLE)
        v2 = d.validate_graph(good)
        assert v2.rc == 0
        assert json.loads(v2.stdout)["ok"] is True

        # We never touched status.json mid-loop -- the driver doesn't
        # flip phase on Tier 1 outcome; that's an in-session decision.
        # The status is still pending+pending until land or compile-fail.
        s = d.status()
        assert s.compile_pending is True
        assert s.compile_phase == CompilePhase.PENDING


# ---------------------------------------------------------------------------
# Scenario 3 -- critic-loop converges after 2 REVISE rounds
# ---------------------------------------------------------------------------

class TestCriticLoopConvergesAfterTwoRevises:
    def test_two_revise_rounds_then_dual_approve(
        self, team_root, journal_dir,
    ):
        d = _new_driver(journal_dir, team_root)

        # Round 1 -- Akagi REVISE.
        d.begin_round()
        draft1 = d.write_draft(_VALID_BUNDLE)
        v1 = d.validate_graph(draft1)
        trace1 = d.write_trace(json.loads(v1.stdout)["trace"])
        akagi1 = d.compile_prompts(
            kind="akagi", draft=str(draft1), trace=str(trace1),
        )
        assert akagi1.rc == 0
        d.write_critic("akagi", akagi1.stdout + "\nWORKFLOW: REVISE -- tighten step bodies\n")
        d.append_transcript("Round 1: Akagi REVISE")

        # Round 2 -- both critics REVISE.
        d.begin_round()
        draft2 = d.write_draft(_VALID_BUNDLE)
        v2 = d.validate_graph(draft2)
        trace2 = d.write_trace(json.loads(v2.stdout)["trace"])
        akagi2 = d.compile_prompts(
            kind="akagi", draft=str(draft2), trace=str(trace2),
        )
        d.write_critic("akagi", akagi2.stdout + "\nWORKFLOW: REVISE -- adjust on_block edges\n")
        ayako2 = d.compile_prompts(
            kind="ayako", draft=str(draft2), trace=str(trace2),
        )
        d.write_critic("ayako", ayako2.stdout + "\nWORKFLOW: REVISE -- clarify Anzai's body\n")
        d.append_transcript("Round 2: both REVISE")

        # Round 3 -- dual APPROVE -> land.
        d.begin_round()
        draft3 = d.write_draft(_VALID_BUNDLE)
        v3 = d.validate_graph(draft3)
        trace3 = d.write_trace(json.loads(v3.stdout)["trace"])
        akagi3 = d.compile_prompts(
            kind="akagi", draft=str(draft3), trace=str(trace3),
        )
        d.write_critic("akagi", akagi3.stdout + "\nWORKFLOW: APPROVE\n")
        ayako3 = d.compile_prompts(
            kind="ayako", draft=str(draft3), trace=str(trace3),
        )
        d.write_critic("ayako", ayako3.stdout + "\nWORKFLOW: APPROVE\n")
        d.append_transcript("Round 3: APPROVE / APPROVE -- landing")
        transcript = d.write_transcript()

        landed = d.land_compile(draft3, transcript, rounds=3)
        assert landed.rc == 0, landed.stderr

        # Round files for ALL three rounds preserved for audit.
        td = d.task_dir
        for n in (1, 2, 3):
            assert (td / "compile" / f"round-{n:02d}-akagi.md").is_file()


# ---------------------------------------------------------------------------
# Scenario 4 -- BLOCK mid-compile lands the task at compile_phase=failed
# ---------------------------------------------------------------------------

class TestBlockMidCompile:
    def test_akagi_blocks_compile_fail_flips_state(
        self, team_root, journal_dir,
    ):
        d = _new_driver(journal_dir, team_root)

        d.begin_round()
        draft = d.write_draft(_VALID_BUNDLE)
        v = d.validate_graph(draft)
        trace = d.write_trace(json.loads(v.stdout)["trace"])
        akagi = d.compile_prompts(
            kind="akagi", draft=str(draft), trace=str(trace),
        )
        d.write_critic("akagi", akagi.stdout + "\nWORKFLOW: BLOCK -- playbook is incoherent\n")

        # Driver invokes compile-fail (NOT abort).
        cf = d.compile_fail(
            "compile failed at critiquing: Akagi BLOCK -- playbook is incoherent",
        )
        assert cf.rc == 0
        assert "compile-failed:" in cf.stdout

        s = d.status()
        assert s.state == State.BLOCKED
        assert s.compile_phase == CompilePhase.FAILED
        assert "Akagi BLOCK" in s.next_action
        # NOT archived -- still in active/.
        assert d.paths.status_json(d.task_id).exists()
        assert not (d.paths.root / "done" / d.task_id).exists()


# ---------------------------------------------------------------------------
# Scenario 5 -- max-rounds exhaustion routes through compile-fail
# ---------------------------------------------------------------------------

class TestMaxRoundsExhaustion:
    def test_eight_revise_rounds_then_compile_fail(
        self, team_root, journal_dir,
    ):
        d = _new_driver(journal_dir, team_root)

        for n in range(1, 9):
            d.begin_round()
            draft = d.write_draft(_VALID_BUNDLE)
            d.write_trace("(trace)")
            d.write_critic("akagi", "WORKFLOW: REVISE -- not yet")
            d.write_critic("ayako", "WORKFLOW: REVISE -- still not yet")
            d.append_transcript(f"Round {n}: REVISE / REVISE")
        # Cap hit at round 8 -> driver calls compile-fail.
        cf = d.compile_fail(
            "compile failed at critiquing: 8 rounds without dual-APPROVE",
        )
        assert cf.rc == 0
        s = d.status()
        assert s.state == State.BLOCKED
        assert s.compile_phase == CompilePhase.FAILED
        assert "8 rounds" in s.next_action


# ---------------------------------------------------------------------------
# Scenario 6 -- unparseable draft surfaces as ok=false from validate-graph
# ---------------------------------------------------------------------------

class TestUnparseableDraft:
    def test_parse_error_routes_into_redraft(self, team_root, journal_dir):
        d = _new_driver(journal_dir, team_root)
        d.begin_round()
        bad = d.write_draft(_UNPARSEABLE_BUNDLE)
        v = d.validate_graph(bad)
        assert v.rc == 1
        env = json.loads(v.stdout)
        assert env["ok"] is False
        assert env["errors"][0]["validator"] == "parse"
        # Status untouched.
        s = d.status()
        assert s.compile_pending is True
        assert s.compile_phase == CompilePhase.PENDING


# ---------------------------------------------------------------------------
# Scenario 7 -- kill-during-land leaves consistent state
# ---------------------------------------------------------------------------

class TestKillDuringLandLeavesConsistentState:
    def test_simulated_kill_before_status_flip_leaves_pending(
        self, team_root, journal_dir, monkeypatch,
    ):
        """Simulate a process kill between staging compile/final/ and the
        status.json flip. Status MUST still read compile_pending=true
        afterwards -- the visibility gate must not have advanced."""
        d = _new_driver(journal_dir, team_root)

        d.begin_round()
        draft = d.write_draft(_VALID_BUNDLE)
        v = d.validate_graph(draft)
        d.write_trace(json.loads(v.stdout)["trace"])
        d.write_critic("akagi", "WORKFLOW: APPROVE")
        d.write_critic("ayako", "WORKFLOW: APPROVE")
        transcript = d.write_transcript()

        # Patch _write_atomic to raise on the FINAL status.json write.
        # The orchestration + steps got promoted; the status flip didn't.
        import tigerharness.journal.compile_cli as mod
        real_write = mod._write_atomic
        flips = {"count": 0}

        def flaky(path, content):
            if path.name == "status.json":
                flips["count"] += 1
                raise OSError("simulated kill before status flip")
            return real_write(path, content)

        monkeypatch.setattr(mod, "_write_atomic", flaky)
        with pytest.raises(OSError):
            d.land_compile(draft, transcript, rounds=1)

        # Status STILL reads pending+pending because the flip never happened.
        s = d.status()
        assert s.compile_pending is True
        assert s.compile_phase == CompilePhase.PENDING
        # Orchestration may or may not be on disk -- but the visibility
        # gate (status.json) is the authoritative bit, and it's still
        # blocking the graph-walker. That's the invariant we care about.

    def test_kill_before_orchestration_promote_leaves_no_canonical_files(
        self, team_root, journal_dir, monkeypatch,
    ):
        """A kill BEFORE the os.replace promotion finishes must leave no
        canonical orchestration.json / steps/. The compile/final/ tree
        may be left behind for forensics but the task dir's user-facing
        artifacts must NOT be partially-promoted."""
        d = _new_driver(journal_dir, team_root)
        d.begin_round()
        draft = d.write_draft(_VALID_BUNDLE)
        v = d.validate_graph(draft)
        d.write_trace(json.loads(v.stdout)["trace"])
        d.write_critic("akagi", "WORKFLOW: APPROVE")
        d.write_critic("ayako", "WORKFLOW: APPROVE")
        transcript = d.write_transcript()

        import tigerharness.journal.compile_cli as mod
        real_replace = mod.os.replace

        def kill_first_replace(src, dst):
            if str(dst).endswith("orchestration.json"):
                raise OSError("simulated kill during orchestration promote")
            return real_replace(src, dst)

        monkeypatch.setattr(mod.os, "replace", kill_first_replace)
        with pytest.raises(OSError):
            d.land_compile(draft, transcript, rounds=1)

        td = d.task_dir
        assert not (td / "orchestration.json").exists()
        # status.json was NOT flipped.
        s = d.status()
        assert s.compile_pending is True


# ---------------------------------------------------------------------------
# Scenario 8 -- rescue from stale mid-critique
# ---------------------------------------------------------------------------

class TestRescueFromStaleMidCritique:
    def test_second_session_picks_up_with_round_files_intact(
        self, team_root, journal_dir,
    ):
        """A previous session got partway through round 2 then died. The
        round files for round 1 (complete) + round-02-draft.md
        (incomplete: no critics) are on disk. The new session can read
        them, decide where to resume, and continue."""
        d = _new_driver(journal_dir, team_root)

        # Session 1: completes round 1 (both REVISE), then dies mid-round 2.
        d.begin_round()
        d.write_draft(_VALID_BUNDLE)
        d.write_critic("akagi", "WORKFLOW: REVISE -- v1")
        d.write_critic("ayako", "WORKFLOW: REVISE -- v1")
        d.begin_round()
        d.write_draft(_VALID_BUNDLE)  # round 2 draft written
        # ... critic files for round 2 NEVER get written -- session died.
        d.append_transcript("Round 1: both REVISE")

        # Session 2 (simulated by a fresh ScriptedDriver wrapping the
        # same on-disk task) reads the round files + decides to resume
        # at round 2's critique phase.
        d2 = ScriptedDriver(paths=d.paths, task_id=d.task_id, round_n=2)
        rounds = sorted(d2.compile_dir.glob("round-*-draft.md"))
        assert len(rounds) == 2
        # The replacing session writes the missing critic files.
        d2.write_critic("akagi", "WORKFLOW: APPROVE")
        d2.write_critic("ayako", "WORKFLOW: APPROVE")

        draft = d2.compile_dir / "round-02-draft.md"
        v = d2.validate_graph(draft)
        d2.write_trace(json.loads(v.stdout)["trace"])
        d2.append_transcript("Round 2 (rescued): APPROVE / APPROVE")
        transcript = d2.write_transcript()
        landed = d2.land_compile(draft, transcript, rounds=2)
        assert landed.rc == 0
        s = d2.status()
        assert s.compile_pending is False
        assert s.compile_phase == CompilePhase.COMPLETE


# ---------------------------------------------------------------------------
# Scenario 9 -- compile-fail then operator runs abort, archiving cleanly
# ---------------------------------------------------------------------------

class TestCompileFailThenManualAbort:
    def test_blocked_failed_then_archive(self, team_root, journal_dir):
        d = _new_driver(journal_dir, team_root)
        d.begin_round()
        d.write_draft(_VALID_BUNDLE)
        d.write_critic("akagi", "WORKFLOW: BLOCK")
        cf = d.compile_fail("compile failed at critiquing: Akagi BLOCK")
        assert cf.rc == 0
        # Operator decides to archive.
        ab = d.abort()
        assert ab.rc == 0
        assert (d.paths.root / "done" / d.task_id / "status.json").is_file()
        # next_action carries BOTH the compile-fail postmortem and the
        # abort note.
        archived = Status.from_json(
            (d.paths.root / "done" / d.task_id / "status.json").read_text(),
        )
        assert "Aborted by" in archived.next_action
        assert "compile_phase=failed" in archived.next_action


# ---------------------------------------------------------------------------
# Scenario 10 -- two parallel compile runs (one task each) don't cross
# ---------------------------------------------------------------------------

class TestParallelTasksDoNotCross:
    def test_two_tasks_compile_independently(self, team_root, journal_dir):
        d1 = _new_driver(journal_dir, team_root)
        d2 = _new_driver(journal_dir, team_root)
        assert d1.task_id != d2.task_id

        # Each task compiles its own round 1 -> land.
        for d in (d1, d2):
            d.begin_round()
            draft = d.write_draft(_VALID_BUNDLE)
            v = d.validate_graph(draft)
            d.write_trace(json.loads(v.stdout)["trace"])
            d.write_critic("akagi", "WORKFLOW: APPROVE")
            d.write_critic("ayako", "WORKFLOW: APPROVE")
            d.append_transcript("Round 1: APPROVE / APPROVE")
            transcript = d.write_transcript()
            landed = d.land_compile(draft, transcript, rounds=1)
            assert landed.rc == 0

        # Neither task's steps/ leaked into the other.
        t1_steps = sorted((d1.task_dir / "steps").iterdir())
        t2_steps = sorted((d2.task_dir / "steps").iterdir())
        assert {p.name for p in t1_steps} == {p.name for p in t2_steps}
        # And they're independent files (different dirs).
        assert (d1.task_dir / "steps") != (d2.task_dir / "steps")


# ---------------------------------------------------------------------------
# Scenario 11 -- land-compile is idempotent on retry once status flipped
# ---------------------------------------------------------------------------

class TestLandIsNotRetriableAfterSuccess:
    def test_second_land_call_no_ops_or_errors(self, team_root, journal_dir):
        """Once status.compile_pending=false, a re-invocation of
        land-compile is undefined -- we just check it doesn't corrupt
        state. The driver protocol forbids re-landing."""
        d = _new_driver(journal_dir, team_root)
        d.begin_round()
        draft = d.write_draft(_VALID_BUNDLE)
        v = d.validate_graph(draft)
        d.write_trace(json.loads(v.stdout)["trace"])
        d.write_critic("akagi", "WORKFLOW: APPROVE")
        d.write_critic("ayako", "WORKFLOW: APPROVE")
        transcript = d.write_transcript()

        first = d.land_compile(draft, transcript, rounds=1)
        assert first.rc == 0
        s1 = d.status()
        assert s1.compile_pending is False

        # A second land-compile should NOT corrupt status -- whether it
        # succeeds (re-land) or errors, the visibility bit must stay
        # at "complete".
        second = d.land_compile(draft, transcript, rounds=1)
        s2 = d.status()
        assert s2.compile_pending is False
        assert s2.compile_phase == CompilePhase.COMPLETE
        # `second` may have rc=0 (no-op-ish re-land) -- the design intent
        # is "land-compile is the visibility gate", and the gate stays
        # closed (compile_pending=false) regardless.
        assert second.rc in (0, 1)


# ---------------------------------------------------------------------------
# Scenario 12 -- ghost orphans in compile/final/steps/ never leak (regression)
# ---------------------------------------------------------------------------

class TestGhostOrphansDoNotLeak:
    def test_pre_seeded_ghost_step_does_not_promote(
        self, team_root, journal_dir,
    ):
        d = _new_driver(journal_dir, team_root)
        stale = d.task_dir / "compile" / "final" / "steps"
        stale.mkdir(parents=True)
        (stale / "ghost.md").write_text("from a crashed prior run")

        d.begin_round()
        draft = d.write_draft(_VALID_BUNDLE)
        v = d.validate_graph(draft)
        d.write_trace(json.loads(v.stdout)["trace"])
        d.write_critic("akagi", "WORKFLOW: APPROVE")
        d.write_critic("ayako", "WORKFLOW: APPROVE")
        transcript = d.write_transcript()

        landed = d.land_compile(draft, transcript, rounds=1)
        assert landed.rc == 0

        canonical = d.task_dir / "steps"
        names = {p.name for p in canonical.iterdir()}
        assert "01-anzai-plan.md" in names
        assert "02-mitsui-impl.md" in names
        assert "ghost.md" not in names


# ---------------------------------------------------------------------------
# Scenario 13 -- post-land orchestration.json is well-formed JSON
# ---------------------------------------------------------------------------

class TestPostLandOrchestrationIsValidJson:
    def test_canonical_orchestration_parses_with_expected_fields(
        self, team_root, journal_dir,
    ):
        d = _new_driver(journal_dir, team_root)
        d.begin_round()
        draft = d.write_draft(_VALID_BUNDLE)
        v = d.validate_graph(draft)
        d.write_trace(json.loads(v.stdout)["trace"])
        d.write_critic("akagi", "WORKFLOW: APPROVE")
        d.write_critic("ayako", "WORKFLOW: APPROVE")
        transcript = d.write_transcript()
        d.land_compile(draft, transcript, rounds=1)

        orch_path = d.task_dir / "orchestration.json"
        payload = json.loads(orch_path.read_text())
        assert payload["task_id"] == d.task_id
        # Orchestration.steps is a list of step IDs (the source-of-truth
        # frontmatter lives in steps/<id>.md). Entrypoint must reference
        # one of those IDs and the edge map must be well-formed.
        assert payload["steps"] == ["01-anzai-plan", "02-mitsui-impl"]
        assert payload["entrypoint"] in payload["steps"]
        assert "edges" in payload
        for step_id in payload["steps"]:
            assert step_id in payload["edges"]


# ---------------------------------------------------------------------------
# Scenario 14 -- compile-context refuses kind=task tasks
# ---------------------------------------------------------------------------

class TestCompileContextOnTaskKindIsRejected:
    def test_kind_task_rejected_with_clear_error(self, journal_dir):
        from tigerharness.journal.scaffold import new_task

        paths = JournalPaths(root=journal_dir)
        result = new_task(prd_text="# t\nx\n", persona="P", paths=paths)
        d = ScriptedDriver(paths=paths, task_id=result.task_id)
        ctx = d.compile_context()
        assert ctx.rc == 1
        assert "only operates on kind=workflow" in ctx.stderr


# ---------------------------------------------------------------------------
# Scenario 15 -- transcript text is preserved verbatim into compile_critique.md
# ---------------------------------------------------------------------------

class TestTranscriptPreservedIntoCritiqueArtifact:
    def test_compile_critique_md_matches_transcript_exact(
        self, team_root, journal_dir,
    ):
        d = _new_driver(journal_dir, team_root)
        d.begin_round()
        draft = d.write_draft(_VALID_BUNDLE)
        v = d.validate_graph(draft)
        d.write_trace(json.loads(v.stdout)["trace"])
        d.write_critic("akagi", "WORKFLOW: APPROVE")
        d.write_critic("ayako", "WORKFLOW: APPROVE")

        # A carefully-crafted multi-line transcript.
        d.append_transcript("# Compile transcript -- task X")
        d.append_transcript("")
        d.append_transcript("Round 1:")
        d.append_transcript("  Akagi: APPROVE -- clean roster, sane edges")
        d.append_transcript("  Ayako: APPROVE -- bodies are clear")
        d.append_transcript("")
        d.append_transcript("Landed in 1 round.")
        transcript_path = d.write_transcript()

        d.land_compile(draft, transcript_path, rounds=1)
        canon = d.task_dir / "compile_critique.md"
        assert canon.read_text() == transcript_path.read_text()

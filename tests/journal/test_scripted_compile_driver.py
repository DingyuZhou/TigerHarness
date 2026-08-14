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
    cmd_append_steps,
    cmd_compile_context,
    cmd_compile_fail,
    cmd_compile_prompts,
    cmd_compile_retry,
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

    def compile_retry(self) -> CliResult:
        ns = argparse.Namespace(
            journal_dir=str(self.paths.root), task_id=self.task_id,
        )
        return self._invoke(cmd_compile_retry, ns)

    def append_steps(self, bundle_path: Path) -> CliResult:
        ns = argparse.Namespace(
            journal_dir=str(self.paths.root),
            task=self.task_id,
            new_bundle=str(bundle_path),
        )
        return self._invoke(cmd_append_steps, ns)

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


def _new_driver(
    journal_dir: Path, team_root: Path, *, playbook_name: str = "default",
) -> ScriptedDriver:
    paths = JournalPaths(root=journal_dir)
    result = new_workflow_task(
        brief_text="# Goal\nShip the feature.\n",
        playbook_text=(
            "# Playbook\n\n"
            "Anzai drafts. Akagi reviews. Ayako reviews. Mitsui implements.\n"
        ),
        playbook_name=playbook_name,
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

    def test_kill_between_steps_promote_and_orchestration_promote(
        self, team_root, journal_dir, monkeypatch,
    ):
        """cmd_land_compile's promotion order is: (1) shutil.move on
        steps/, (2) os.replace on orchestration.json, (3) os.replace on
        compile_critique.md, (4) status.json flip via _write_atomic. A
        kill that fires at step 2 (orchestration.json) leaves
        canonical steps/ on disk but no canonical orchestration.json.

        The invariant the code actually upholds is **not** "no partial
        promotion ever" -- it's "the status.json flip is the visibility
        gate, and it doesn't flip until ALL the artifact promotions
        finish." So a graph-walker / list / status reader sees
        compile_pending=true and never tries to read orchestration.json
        or steps/. Pin that honestly."""
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

        def kill_at_orchestration(src, dst):
            if str(dst).endswith("orchestration.json"):
                raise OSError("simulated kill during orchestration promote")
            return real_replace(src, dst)

        monkeypatch.setattr(mod.os, "replace", kill_at_orchestration)
        with pytest.raises(OSError):
            d.land_compile(draft, transcript, rounds=1)

        td = d.task_dir
        # The orchestration.json promote never finished.
        assert not (td / "orchestration.json").exists()
        # Steps/ DID get promoted (it's the first step of the sequence).
        # That's acceptable per the design: the visibility gate is
        # status.json, NOT presence-of-steps/.
        # The VISIBILITY GATE is correct: compile_pending stayed true.
        s = d.status()
        assert s.compile_pending is True
        assert s.compile_phase == CompilePhase.PENDING

    def test_kill_at_steps_move_leaves_no_canonical_artifacts(
        self, team_root, journal_dir, monkeypatch,
    ):
        """A kill at the FIRST promotion step (shutil.move on steps/)
        leaves nothing canonicalized at all -- so no graph-walker can
        ever see a half-compiled task even via raw filesystem inspection."""
        d = _new_driver(journal_dir, team_root)
        d.begin_round()
        draft = d.write_draft(_VALID_BUNDLE)
        v = d.validate_graph(draft)
        d.write_trace(json.loads(v.stdout)["trace"])
        d.write_critic("akagi", "WORKFLOW: APPROVE")
        d.write_critic("ayako", "WORKFLOW: APPROVE")
        transcript = d.write_transcript()

        import tigerharness.journal.compile_cli as mod
        real_move = mod.shutil.move

        def kill_at_steps_move(src, dst):
            if str(dst).endswith("/steps"):
                raise OSError("simulated kill before steps promote")
            return real_move(src, dst)

        monkeypatch.setattr(mod.shutil, "move", kill_at_steps_move)
        with pytest.raises(OSError):
            d.land_compile(draft, transcript, rounds=1)

        td = d.task_dir
        assert not (td / "steps").exists()
        assert not (td / "orchestration.json").exists()
        s = d.status()
        assert s.compile_pending is True
        assert s.compile_phase == CompilePhase.PENDING


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

class TestLandIsIdempotentOnRetry:
    def test_second_land_call_is_idempotent(self, team_root, journal_dir):
        """Once status.compile_pending=false, a re-invocation of
        land-compile is **idempotent**: the function has no precondition
        check on the compile flag, so it re-runs the whole pipeline
        (Tier 1 re-validation, re-stage, re-promote via atomic
        replace/move) on the same well-formed inputs. The driver
        protocol forbids the second call, but the safety net here is
        that nothing corrupts -- and rc is deterministically 0, not
        ambiguous. We pin that so a regression that silently introduces
        a non-zero return is caught."""
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

        # Pin the deterministic idempotency: re-land succeeds + leaves
        # the visibility bit at complete.
        second = d.land_compile(draft, transcript, rounds=1)
        assert second.rc == 0, second.stderr
        s2 = d.status()
        assert s2.compile_pending is False
        assert s2.compile_phase == CompilePhase.COMPLETE


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
        # Phase 2: orchestration.playbook reflects the truthful scaffold-
        # time playbook name (no longer hardcoded "default").
        assert payload["playbook"] == "default"
        # Orchestration.steps is a list of step IDs (the source-of-truth
        # frontmatter lives in steps/<id>.md). Entrypoint must reference
        # one of those IDs and the edge map must be well-formed.
        assert payload["steps"] == ["01-anzai-plan", "02-mitsui-impl"]
        assert payload["entrypoint"] in payload["steps"]
        assert "edges" in payload
        for step_id in payload["steps"]:
            assert step_id in payload["edges"]

    def test_non_default_playbook_name_flows_through_to_orchestration(
        self, team_root, journal_dir,
    ):
        d = _new_driver(journal_dir, team_root, playbook_name="research-pass")
        d.begin_round()
        draft = d.write_draft(_VALID_BUNDLE)
        v = d.validate_graph(draft)
        d.write_trace(json.loads(v.stdout)["trace"])
        d.write_critic("akagi", "WORKFLOW: APPROVE")
        d.write_critic("ayako", "WORKFLOW: APPROVE")
        transcript = d.write_transcript()
        d.land_compile(draft, transcript, rounds=1)
        payload = json.loads((d.task_dir / "orchestration.json").read_text())
        assert payload["playbook"] == "research-pass"


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

class TestCompileFailRetrySucceedCycle:
    """End-to-end: first attempt fails (Akagi BLOCK), operator runs
    compile-retry, second attempt succeeds and lands. Round counters
    reset, task carries the SECOND attempt's compile in compile/."""

    def test_full_cycle(self, team_root, journal_dir):
        d = _new_driver(journal_dir, team_root)

        # First attempt: round 1, Akagi BLOCK -> compile-fail.
        d.begin_round()
        draft1 = d.write_draft(_VALID_BUNDLE)
        v1 = d.validate_graph(draft1)
        d.write_trace(json.loads(v1.stdout)["trace"])
        d.write_critic("akagi", "WORKFLOW: BLOCK -- nope")
        d.append_transcript("Attempt 1, Round 1: Akagi BLOCK")
        d.write_transcript()
        cf = d.compile_fail("attempt 1 failed")
        assert cf.rc == 0
        s = d.status()
        assert s.state == State.BLOCKED
        assert s.compile_phase == CompilePhase.FAILED

        # Operator inspects, decides to retry.
        retry = d.compile_retry()
        assert retry.rc == 0
        s = d.status()
        assert s.state == State.PENDING
        assert s.compile_pending is True
        assert s.compile_phase == CompilePhase.PENDING
        # compile/ wiped, including the round files from attempt 1.
        assert not (d.task_dir / "compile").exists()

        # Second attempt: round counter resets in the driver state.
        d.round_n = 0
        d.transcript_lines.clear()
        d.begin_round()
        draft2 = d.write_draft(_VALID_BUNDLE)
        v2 = d.validate_graph(draft2)
        d.write_trace(json.loads(v2.stdout)["trace"])
        d.write_critic("akagi", "WORKFLOW: APPROVE")
        d.write_critic("ayako", "WORKFLOW: APPROVE")
        d.append_transcript("Attempt 2, Round 1: APPROVE / APPROVE")
        transcript2 = d.write_transcript()
        landed = d.land_compile(draft2, transcript2, rounds=1)
        assert landed.rc == 0, landed.stderr

        s = d.status()
        assert s.compile_pending is False
        assert s.compile_phase == CompilePhase.COMPLETE
        # The brief + playbook snapshot survived the failure -> retry
        # round-trip.
        assert (d.task_dir / "task_brief.md").is_file()
        assert (d.task_dir / "playbook_snapshot.md").is_file()


class TestPhase3StepAppendEndToEnd:
    """Phase 3: drive compile to landed, then append a new step at
    runtime via the same drafter discipline + Tier 1 gate, and verify
    the resulting graph is internally consistent."""

    def test_compile_then_append(self, team_root, journal_dir, tmp_path):
        d = _new_driver(journal_dir, team_root)
        # Land the initial graph (round 1, dual APPROVE).
        d.begin_round()
        draft = d.write_draft(_VALID_BUNDLE)
        v = d.validate_graph(draft)
        d.write_trace(json.loads(v.stdout)["trace"])
        d.write_critic("akagi", "WORKFLOW: APPROVE")
        d.write_critic("ayako", "WORKFLOW: APPROVE")
        transcript = d.write_transcript()
        landed = d.land_compile(draft, transcript, rounds=1)
        assert landed.rc == 0

        # Now append-steps a follow-up QA step.
        append_bundle = (
            "```steps-bundle\n"
            "## step: 03-mitsui-qa\n"
            "---\n"
            "id: 03-mitsui-qa\n"
            "persona: Mitsui\n"
            "role: qa\n"
            "on_approve: __done__\n"
            "on_revise: 03-mitsui-qa\n"
            "on_block: __escalate__\n"
            "max_iters: 5\n"
            "timeout_sec: 1800\n"
            "parallel_with: []\n"
            "---\n"
            "QA the implementation.\n"
            "```\n"
        )
        bundle_path = tmp_path / "append.md"
        bundle_path.write_text(append_bundle)
        appended = d.append_steps(bundle_path)
        assert appended.rc == 0, appended.stderr

        # orchestration.json reflects all three steps.
        orch = json.loads(
            (d.task_dir / "orchestration.json").read_text(),
        )
        assert orch["steps"] == [
            "01-anzai-plan", "02-mitsui-impl", "03-mitsui-qa",
        ]
        # The entrypoint stayed the same (first step never changes).
        assert orch["entrypoint"] == "01-anzai-plan"
        # Each step has its edges in the edge map.
        for step_id in orch["steps"]:
            assert step_id in orch["edges"]
        # Step files are all on disk.
        for sid in orch["steps"]:
            assert (d.task_dir / "steps" / f"{sid}.md").is_file()
        # compile_phase still complete; the append doesn't change
        # the visibility gate.
        s = d.status()
        assert s.compile_phase == CompilePhase.COMPLETE


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


# ---------------------------------------------------------------------------
# Scenario 17 -- step bodies survive the compile end to end
# ---------------------------------------------------------------------------

class TestStepBodiesReachTheLandedFiles:
    """The drafter writes per-step instructions below each chunk's
    closing ``---``. Until 2026-08-14 the parser dropped them, so every
    walking seat opened a landed ``steps/<id>.md`` that held frontmatter
    and nothing else.

    These assertions run against the file ON DISK after a real land --
    constructing a ``StepFrontmatter`` with the body pre-attached would
    pass on the broken build and prove nothing."""

    def _land(self, journal_dir, team_root, bundle=_VALID_BUNDLE):
        d = _new_driver(journal_dir, team_root)
        d.begin_round()
        draft = d.write_draft(bundle)
        v = d.validate_graph(draft)
        assert v.rc == 0, v.stdout
        d.write_trace(json.loads(v.stdout)["trace"])
        d.write_critic("akagi", "WORKFLOW: APPROVE")
        d.write_critic("ayako", "WORKFLOW: APPROVE")
        landed = d.land_compile(draft, d.write_transcript(), rounds=1)
        assert landed.rc == 0, landed.stderr
        return d

    def test_landed_step_file_carries_its_body(self, team_root, journal_dir):
        d = self._land(journal_dir, team_root)

        plan = (d.task_dir / "steps" / "01-anzai-plan.md").read_text()
        impl = (d.task_dir / "steps" / "02-mitsui-impl.md").read_text()

        # Each seat gets ITS OWN instructions, not the other's.
        assert "Plan the work." in plan
        assert "Implement the plan." not in plan
        assert "Implement the plan." in impl
        assert "Plan the work." not in impl

        # And below the closing delimiter, where a reader looks for it.
        head, _, body = plan.partition("---\n")[2].partition("---\n")
        assert "Plan the work." not in head
        assert body.strip() == "Plan the work."

    def test_body_is_not_emitted_as_a_yaml_key(self, team_root, journal_dir):
        """The silence assertion. ``_render_frontmatter`` used to iterate
        ``asdict(step)``, which would sweep in the new field and emit the
        step's whole instruction text as a ``body:`` YAML key -- rendering
        that still *looks* right at a glance."""
        d = self._land(journal_dir, team_root)

        for sid in ("01-anzai-plan", "02-mitsui-impl"):
            text = (d.task_dir / "steps" / f"{sid}.md").read_text()
            frontmatter = text.partition("---\n")[2].partition("---\n")[0]
            keys = [
                line.split(":", 1)[0]
                for line in frontmatter.splitlines()
                if line.strip()
            ]
            assert "body" not in keys, frontmatter
            assert keys == [
                "id", "persona", "role", "on_approve", "on_revise",
                "on_block", "max_iters", "timeout_sec", "parallel_with",
            ]

    def test_body_never_reaches_orchestration_json(
        self, team_root, journal_dir,
    ):
        d = self._land(journal_dir, team_root)
        raw = (d.task_dir / "orchestration.json").read_text()
        assert "Plan the work." not in raw
        assert "Implement the plan." not in raw
        assert '"body"' not in raw

    def test_bodyless_bundle_renders_as_before(self, team_root, journal_dir):
        """The empty-body branch of ``_render_step_file``. A bundle whose
        chunks stop at the closing ``---`` must land byte-identically to
        what shipped before bodies existed -- no trailing blank line."""
        bodyless = _VALID_BUNDLE.replace(
            "---\nPlan the work.\n", "---\n",
        ).replace(
            "---\nImplement the plan.\n", "---\n",
        )
        d = self._land(journal_dir, team_root, bundle=bodyless)

        text = (d.task_dir / "steps" / "01-anzai-plan.md").read_text()
        assert text == (
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
        )

    def test_appended_step_carries_its_body_too(
        self, team_root, journal_dir, tmp_path,
    ):
        """``append-steps`` is the second write site. A fix that lands
        only in ``land-compile`` leaves runtime-grafted steps empty."""
        d = self._land(journal_dir, team_root)

        bundle_path = tmp_path / "append.md"
        bundle_path.write_text(
            "```steps-bundle\n"
            "## step: 03-mitsui-qa\n"
            "---\n"
            "id: 03-mitsui-qa\n"
            "persona: Mitsui\n"
            "role: qa\n"
            "on_approve: __done__\n"
            "on_revise: 03-mitsui-qa\n"
            "on_block: __escalate__\n"
            "max_iters: 5\n"
            "timeout_sec: 1800\n"
            "parallel_with: []\n"
            "---\n"
            "QA the implementation against the plan's acceptance rows.\n"
            "```\n"
        )
        assert d.append_steps(bundle_path).rc == 0

        appended = (d.task_dir / "steps" / "03-mitsui-qa.md").read_text()
        assert "QA the implementation against the plan's acceptance rows." in (
            appended.partition("---\n")[2].partition("---\n")[2]
        )

        # The append re-reads every existing step file to re-validate the
        # whole graph. It must leave them alone: an already-walked seat's
        # instructions cannot change under it mid-walk.
        plan = (d.task_dir / "steps" / "01-anzai-plan.md").read_text()
        assert plan.partition("---\n")[2].partition("---\n")[2].strip() == (
            "Plan the work."
        )

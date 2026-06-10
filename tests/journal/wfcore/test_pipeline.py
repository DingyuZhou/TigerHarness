"""Unit tests for ``tigerharness.journal.wfcore.pipeline``.

The pipeline stitches together three components. Two are real here (the
drafter + the Tier 1 validators); the Tier 2 critique loop is injected as
:class:`FakeCritiqueLoop` so this suite never spawns ``claude`` and never
depends on Rukawa's ``compile.critique`` module (which does not exist on
this branch yet).

The drafter still talks to the LLM only through ``SessionManager.invoke``;
we feed it the in-memory :class:`FakeSessionManager` from ``conftest.py``,
scripted with a ``steps-bundle`` response, so the whole pipeline runs
deterministically.

Coverage map (6 brief-required cases + the branches the 100% line+branch
floor demands):

* happy_path_writes_all_artifacts        -> brief #1
* tier1_pre_critique_fails_aborts        -> brief #2
* tier1_post_critique_fails_aborts       -> brief #3
* tier2_aborted_propagates               -> brief #4
* cost_summed_drafter_plus_critique      -> brief #5
* roster_loaded_from_personas_yaml       -> brief #6
* non_abort_exception_propagates         -> the _is_critique_aborted False arm
* critique_loop_can_invoke_redrafter     -> the _redraft closure body
* explicit_workflow_config_is_used       -> _resolve_config non-None arm
* roster_* (5 malformed-config cases)    -> _load_roster validation arms
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest

from tigerharness.journal.wfcore.pipeline import (
    CompileConfigError,
    CompileResult,
    CompileTier1Error,
    CompileTier2Error,
    CritiqueResult,
    compile_playbook,
)
from tigerharness.journal.wfcore.models import (
    Orchestration,
    StepFrontmatter,
    WorkflowConfig,
)
from tigerharness.workflow_runner.paths import TaskPaths

from tests.journal.wfcore.conftest import (
    FakeSessionManager,
    StepSpec,
    make_response,
    three_step_specs,
)


# --------------------------------------------------------------------------- #
# Fakes + fixtures
# --------------------------------------------------------------------------- #


class CritiqueAbortedError(Exception):
    """Local stand-in for Rukawa's abort exception.

    The pipeline recognises the abort by class *name* (not import), so a
    class named exactly ``CritiqueAbortedError`` reproduces production
    behaviour without coupling the test to ``compile.critique``.
    """


class FakeCritiqueLoop:
    """In-memory stand-in for Rukawa's ``run_critique_loop``.

    Records the keyword args it is called with (so tests can assert the
    pipeline threaded the trace, roster, config caps, etc.) and returns a
    canned :class:`CritiqueResult` -- or raises a scripted exception.

    Modes for the returned ``final_steps`` (first match wins):

    * ``raises`` set                -> raise it (abort / generic-error tests).
    * ``final_steps`` set           -> return exactly those (post-critique
                                       failure test).
    * ``call_redrafter_with`` set   -> call the injected ``drafter`` with
                                       that feedback and use its output
                                       (exercises the re-draft closure).
    * otherwise                     -> echo the ``initial_steps`` received
                                       ("critique returns the same steps").
    """

    def __init__(
        self,
        *,
        final_steps: Optional[list[StepFrontmatter]] = None,
        transcript: str = "FAKE CRITIQUE TRANSCRIPT\n",
        rounds: int = 3,
        cost_usd: float = 0.0,
        raises: Optional[BaseException] = None,
        call_redrafter_with: Optional[str] = None,
    ) -> None:
        self._final_steps = final_steps
        self._transcript = transcript
        self._rounds = rounds
        self._cost_usd = cost_usd
        self._raises = raises
        self._call_redrafter_with = call_redrafter_with
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        *,
        initial_steps: list[StepFrontmatter],
        drafter: Any,
        **kwargs: Any,
    ) -> CritiqueResult:
        self.calls.append(
            {"initial_steps": initial_steps, "drafter": drafter, **kwargs}
        )
        if self._raises is not None:
            raise self._raises
        if self._final_steps is not None:
            steps = self._final_steps
        elif self._call_redrafter_with is not None:
            steps = drafter(self._call_redrafter_with)
        else:
            steps = list(initial_steps)
        return CritiqueResult(
            rounds=[object()] * self._rounds,
            final_steps=steps,
            transcript=self._transcript,
            cost_usd=self._cost_usd,
        )


def _write_personas(team_root: Path, names: list[str]) -> None:
    """Write a minimal ``configs/personas.yaml`` with the given names."""
    cfg = team_root / "configs"
    cfg.mkdir(parents=True, exist_ok=True)
    lines = ["personas_dir: ../personas", "personas:"]
    lines.extend(f"  - name: {name}" for name in names)
    (cfg / "personas.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _setup_team(
    tmp_path: Path,
    *,
    roster: list[str] | None = None,
    playbook_body: str = "# Default playbook\n\nDo the steps.\n",
    team_name: str = "Shohoku",
) -> tuple[Path, Path]:
    """Create a team root with personas.yaml + a playbook; return both."""
    team_root = tmp_path / team_name
    _write_personas(team_root, roster if roster is not None else ["anzai", "akagi"])
    playbook = team_root / "workflow" / "default.md"
    playbook.parent.mkdir(parents=True, exist_ok=True)
    playbook.write_text(playbook_body, encoding="utf-8")
    return team_root, playbook


def _make_task_paths(tmp_path: Path) -> TaskPaths:
    """Mint + ensure a TaskPaths under a journal dir (as cmd_start does)."""
    return TaskPaths(
        root=tmp_path / "journal", task_id="20260602-pipe-abcd1234"
    ).ensure()


def _valid_step(
    step_id: str = "01-anzai-plan",
    *,
    persona: str = "anzai",
    on_approve: str = "__done__",
) -> StepFrontmatter:
    """A single structurally-valid StepFrontmatter for direct injection."""
    return StepFrontmatter(
        id=step_id,
        persona=persona,
        role="planner",
        on_approve=on_approve,
        on_revise=step_id,
        on_block="__escalate__",
        max_iters=5,
        timeout_sec=1800,
    )


# --------------------------------------------------------------------------- #
# Brief #1 -- happy path
# --------------------------------------------------------------------------- #


def test_happy_path_writes_all_artifacts(tmp_path: Path) -> None:
    team_root, playbook = _setup_team(tmp_path)
    task_paths = _make_task_paths(tmp_path)
    fsm = FakeSessionManager(
        stdout=make_response(three_step_specs()), cost_usd=0.10
    )
    loop = FakeCritiqueLoop(transcript="ROUND TRANSCRIPT\n", rounds=3)

    result = compile_playbook(
        playbook_path=playbook,
        task_brief="Add cache eviction to the redis layer.",
        team_root=team_root,
        task_paths=task_paths,
        session_manager=fsm,
        critique_loop=loop,
    )

    assert isinstance(result, CompileResult)
    # Final steps are the echoed 3-step plan.
    assert [s.id for s in result.steps] == [
        "01-anzai-plan",
        "02-akagi-critique",
        "03-anzai-revise",
    ]
    assert result.critique_iters == 3
    assert result.transcript == "ROUND TRANSCRIPT\n"
    assert "workflow dry-run trace" in result.trace

    # Orchestration is fully populated with real values (no placeholders).
    orch = result.orchestration
    assert isinstance(orch, Orchestration)
    assert orch.task_id == "20260602-pipe-abcd1234"
    assert orch.team == "Shohoku"
    assert orch.playbook == "default"
    assert orch.playbook_sha256  # non-empty hex digest
    assert orch.entrypoint == "01-anzai-plan"
    assert orch.compiled_by == "anzai"
    assert orch.compile_critique_iters == 3
    assert set(orch.edges) == set(orch.steps)

    # All four artifacts written, with the expected content.
    assert task_paths.compile_trace.read_text(encoding="utf-8") == result.trace
    assert (
        task_paths.compile_critique.read_text(encoding="utf-8")
        == "ROUND TRANSCRIPT\n"
    )
    assert task_paths.playbook_snapshot.read_text(
        encoding="utf-8"
    ) == playbook.read_text(encoding="utf-8")
    assert (
        task_paths.task_brief.read_text(encoding="utf-8")
        == "Add cache eviction to the redis layer."
    )

    # The loop got the validated steps, the trace, and the config caps.
    (call,) = loop.calls
    assert [s.id for s in call["initial_steps"]] == [s.id for s in result.steps]
    assert "workflow dry-run trace" in call["trace"]
    assert call["max_compile_iters"] == 8  # default config
    assert call["hard_floor_iters"] == 3


# --------------------------------------------------------------------------- #
# Brief #2 -- Tier 1 fails pre-critique
# --------------------------------------------------------------------------- #


def test_tier1_pre_critique_fails_aborts(tmp_path: Path) -> None:
    team_root, playbook = _setup_team(tmp_path, roster=["anzai", "akagi"])
    task_paths = _make_task_paths(tmp_path)
    # Drafter emits a step whose persona is off-roster -> roster validator.
    bad = make_response(
        [
            StepSpec(
                id="01-ghost-plan",
                persona="ghost",
                role="planner",
                on_approve="__done__",
                on_revise="01-ghost-plan",
            )
        ]
    )
    fsm = FakeSessionManager(stdout=bad, cost_usd=0.10)
    loop = FakeCritiqueLoop()

    with pytest.raises(CompileTier1Error) as exc_info:
        compile_playbook(
            playbook_path=playbook,
            task_brief="BRIEF",
            team_root=team_root,
            task_paths=task_paths,
            session_manager=fsm,
            critique_loop=loop,
        )

    err = exc_info.value
    assert err.stage == "pre_critique"
    assert any(e.validator == "roster" for e in err.errors)
    # The critique loop never ran, and no artifacts were written.
    assert loop.calls == []
    assert not task_paths.compile_trace.exists()
    assert not task_paths.compile_critique.exists()
    assert not task_paths.playbook_snapshot.exists()
    assert not task_paths.task_brief.exists()


# --------------------------------------------------------------------------- #
# Brief #3 -- Tier 1 fails post-critique
# --------------------------------------------------------------------------- #


def test_tier1_post_critique_fails_aborts(tmp_path: Path) -> None:
    team_root, playbook = _setup_team(tmp_path)
    task_paths = _make_task_paths(tmp_path)
    fsm = FakeSessionManager(stdout=make_response(three_step_specs()))
    # Critique returns a step whose on_approve points at a non-existent id.
    reintroduced = [_valid_step(on_approve="ghost-target")]
    loop = FakeCritiqueLoop(final_steps=reintroduced)

    with pytest.raises(CompileTier1Error) as exc_info:
        compile_playbook(
            playbook_path=playbook,
            task_brief="BRIEF",
            team_root=team_root,
            task_paths=task_paths,
            session_manager=fsm,
            critique_loop=loop,
        )

    err = exc_info.value
    assert err.stage == "post_critique"
    assert any(e.validator == "ref" for e in err.errors)
    # Loop did run, but the post-critique abort wrote no artifacts.
    assert len(loop.calls) == 1
    assert not task_paths.compile_critique.exists()
    assert not task_paths.compile_trace.exists()


# --------------------------------------------------------------------------- #
# Brief #4 -- Tier 2 abort propagates as CompileTier2Error
# --------------------------------------------------------------------------- #


def test_tier2_aborted_propagates(tmp_path: Path) -> None:
    team_root, playbook = _setup_team(tmp_path)
    task_paths = _make_task_paths(tmp_path)
    fsm = FakeSessionManager(stdout=make_response(three_step_specs()))
    abort = CritiqueAbortedError("exhausted max_compile_iters without consensus")
    loop = FakeCritiqueLoop(raises=abort)

    with pytest.raises(CompileTier2Error) as exc_info:
        compile_playbook(
            playbook_path=playbook,
            task_brief="BRIEF",
            team_root=team_root,
            task_paths=task_paths,
            session_manager=fsm,
            critique_loop=loop,
        )

    err = exc_info.value
    assert err.cause is abort
    assert err.__cause__ is abort
    assert "exhausted" in str(err)
    assert not task_paths.compile_critique.exists()


# --------------------------------------------------------------------------- #
# Brief #5 -- cost = drafter + critique
# --------------------------------------------------------------------------- #


def test_cost_summed_drafter_plus_critique(tmp_path: Path) -> None:
    team_root, playbook = _setup_team(tmp_path)
    task_paths = _make_task_paths(tmp_path)
    fsm = FakeSessionManager(
        stdout=make_response(three_step_specs()), cost_usd=0.10
    )
    loop = FakeCritiqueLoop(cost_usd=0.25)

    result = compile_playbook(
        playbook_path=playbook,
        task_brief="BRIEF",
        team_root=team_root,
        task_paths=task_paths,
        session_manager=fsm,
        critique_loop=loop,
    )

    assert result.cost_usd == pytest.approx(0.35)


# --------------------------------------------------------------------------- #
# Brief #6 -- roster loaded from personas.yaml
# --------------------------------------------------------------------------- #


def test_roster_loaded_from_personas_yaml(tmp_path: Path) -> None:
    # A distinctive extra name proves the roster came from the file and
    # was threaded into the drafter prompt.
    team_root, playbook = _setup_team(
        tmp_path, roster=["anzai", "akagi", "zzdistinct"]
    )
    task_paths = _make_task_paths(tmp_path)
    fsm = FakeSessionManager(stdout=make_response(three_step_specs()))
    loop = FakeCritiqueLoop()

    compile_playbook(
        playbook_path=playbook,
        task_brief="BRIEF",
        team_root=team_root,
        task_paths=task_paths,
        session_manager=fsm,
        critique_loop=loop,
    )

    # The drafter prompt carries the roster block from personas.yaml.
    assert len(fsm.calls) == 1
    assert "zzdistinct" in fsm.calls[0].prompt
    # And the critique loop received the same roster list.
    assert loop.calls[0]["roster"] == ["anzai", "akagi", "zzdistinct"]


# --------------------------------------------------------------------------- #
# Branch: a non-abort exception from the loop propagates unwrapped
# --------------------------------------------------------------------------- #


def test_non_abort_exception_propagates(tmp_path: Path) -> None:
    team_root, playbook = _setup_team(tmp_path)
    task_paths = _make_task_paths(tmp_path)
    fsm = FakeSessionManager(stdout=make_response(three_step_specs()))
    loop = FakeCritiqueLoop(raises=ValueError("boom"))

    # Not a CritiqueAbortedError -> the pipeline must not wrap it.
    with pytest.raises(ValueError, match="boom"):
        compile_playbook(
            playbook_path=playbook,
            task_brief="BRIEF",
            team_root=team_root,
            task_paths=task_paths,
            session_manager=fsm,
            critique_loop=loop,
        )


# --------------------------------------------------------------------------- #
# Branch: the re-draft closure body is exercised when the loop calls it
# --------------------------------------------------------------------------- #


def test_critique_loop_can_invoke_redrafter(tmp_path: Path) -> None:
    team_root, playbook = _setup_team(tmp_path)
    task_paths = _make_task_paths(tmp_path)
    fsm = FakeSessionManager(stdout=make_response(three_step_specs()))
    loop = FakeCritiqueLoop(call_redrafter_with="tighten the QA gate")

    result = compile_playbook(
        playbook_path=playbook,
        task_brief="BRIEF",
        team_root=team_root,
        task_paths=task_paths,
        session_manager=fsm,
        critique_loop=loop,
    )

    assert isinstance(result, CompileResult)
    # Two drafter invocations: the initial draft + one re-draft.
    assert len(fsm.calls) == 2
    # The re-draft prompt carried the critic feedback.
    assert "tighten the QA gate" in fsm.calls[1].prompt
    assert "Critic feedback to address" in fsm.calls[1].prompt


# --------------------------------------------------------------------------- #
# Branch: an explicit workflow_config is used (and threaded to the loop)
# --------------------------------------------------------------------------- #


def test_explicit_workflow_config_is_used(tmp_path: Path) -> None:
    team_root, playbook = _setup_team(tmp_path)
    task_paths = _make_task_paths(tmp_path)
    fsm = FakeSessionManager(stdout=make_response(three_step_specs()))
    loop = FakeCritiqueLoop()
    cfg = WorkflowConfig(
        max_compile_iters=4, human_gate_approvers=["alice"]
    )

    result = compile_playbook(
        playbook_path=playbook,
        task_brief="BRIEF",
        team_root=team_root,
        task_paths=task_paths,
        session_manager=fsm,
        critique_loop=loop,
        workflow_config=cfg,
    )

    # The supplied config is carried verbatim into the orchestration...
    assert result.orchestration.workflow_config is cfg
    # ...and its cap is the one handed to the critique loop.
    assert loop.calls[0]["max_compile_iters"] == 4


# --------------------------------------------------------------------------- #
# Branch: _load_roster rejects malformed personas.yaml
# --------------------------------------------------------------------------- #


def _compile_with_team(tmp_path: Path, team_root: Path, playbook: Path) -> None:
    """Run compile_playbook far enough to trigger roster loading."""
    task_paths = _make_task_paths(tmp_path)
    fsm = FakeSessionManager(stdout=make_response(three_step_specs()))
    compile_playbook(
        playbook_path=playbook,
        task_brief="BRIEF",
        team_root=team_root,
        task_paths=task_paths,
        session_manager=fsm,
        critique_loop=FakeCritiqueLoop(),
    )


def test_roster_missing_file_raises_filenotfound(tmp_path: Path) -> None:
    team_root = tmp_path / "Shohoku"
    playbook = team_root / "workflow" / "default.md"
    playbook.parent.mkdir(parents=True, exist_ok=True)
    playbook.write_text("# pb\n", encoding="utf-8")
    # No personas.yaml written at all.
    with pytest.raises(FileNotFoundError):
        _compile_with_team(tmp_path, team_root, playbook)


def test_roster_non_mapping_top_level_raises(tmp_path: Path) -> None:
    team_root, playbook = _setup_team(tmp_path)
    (team_root / "configs" / "personas.yaml").write_text(
        "- just\n- a\n- list\n", encoding="utf-8"
    )
    with pytest.raises(CompileConfigError, match="must be a mapping"):
        _compile_with_team(tmp_path, team_root, playbook)


def test_roster_missing_personas_list_raises(tmp_path: Path) -> None:
    team_root, playbook = _setup_team(tmp_path)
    (team_root / "configs" / "personas.yaml").write_text(
        "personas_dir: ../personas\n", encoding="utf-8"
    )
    with pytest.raises(CompileConfigError, match="non-empty 'personas' list"):
        _compile_with_team(tmp_path, team_root, playbook)


def test_roster_empty_personas_list_raises(tmp_path: Path) -> None:
    team_root, playbook = _setup_team(tmp_path)
    (team_root / "configs" / "personas.yaml").write_text(
        "personas: []\n", encoding="utf-8"
    )
    with pytest.raises(CompileConfigError, match="non-empty 'personas' list"):
        _compile_with_team(tmp_path, team_root, playbook)


def test_roster_entry_not_mapping_raises(tmp_path: Path) -> None:
    team_root, playbook = _setup_team(tmp_path)
    (team_root / "configs" / "personas.yaml").write_text(
        "personas:\n  - anzai\n  - akagi\n", encoding="utf-8"
    )
    with pytest.raises(CompileConfigError, match="must be a mapping"):
        _compile_with_team(tmp_path, team_root, playbook)


def test_roster_entry_blank_name_raises(tmp_path: Path) -> None:
    team_root, playbook = _setup_team(tmp_path)
    (team_root / "configs" / "personas.yaml").write_text(
        "personas:\n  - name: '   '\n", encoding="utf-8"
    )
    with pytest.raises(CompileConfigError, match="missing a non-empty 'name'"):
        _compile_with_team(tmp_path, team_root, playbook)


def test_roster_entry_non_string_name_raises(tmp_path: Path) -> None:
    team_root, playbook = _setup_team(tmp_path)
    # An int name trips the ``not isinstance(name, str)`` arm.
    (team_root / "configs" / "personas.yaml").write_text(
        "personas:\n  - name: 123\n", encoding="utf-8"
    )
    with pytest.raises(CompileConfigError, match="missing a non-empty 'name'"):
        _compile_with_team(tmp_path, team_root, playbook)

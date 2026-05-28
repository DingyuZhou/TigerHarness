"""Shared fixtures for the workflow-runner end-to-end tests.

What these fixtures provide
---------------------------

``e2e_steps_dir``
    Path to the canonical 3-step playbook (``01-plan`` ->
    ``02-build`` -> ``03-review``) checked in under
    ``tests/workflow_runner/e2e/steps/``. Copied into each task's
    journal by ``workflow start``.

``e2e_personas_dir``
    Temp directory with stub persona prompt files for ``anzai``,
    ``akagi``, ``rukawa`` (the personas the canonical playbook
    uses). Pointed at via ``TIGERHARNESS_PERSONAS_DIR`` so the
    :class:`SessionManager` can fall through ``load_prompt`` on
    fresh sessions.

``e2e_journal_root``
    Per-test temp journal root. Pointed at via
    ``TIGERHARNESS_WORKFLOW_JOURNAL`` so every CLI call goes to a
    private location and parallel ``pytest -n auto`` runs do not
    collide.

``e2e_fake_claude``
    Drops the fake ``claude`` binary on disk, marks it executable,
    points ``TIGERHARNESS_CLAUDE_BIN`` at it, and clears any
    inherited ``FAKE_*`` env vars. Exposes a tiny ``.set_script(...)``
    helper that writes a scripted-response JSON file and points
    ``FAKE_CLAUDE_SCRIPT`` at it.

``e2e_driver``
    The driver harness. Calls ``cli.main(["start", ...])`` against
    the canonical playbook, returns a small bundle (task_id, paths,
    fake_claude, journal_root) the test can use.

    The ``run_executor()`` callable on the bundle drives Rukawa's
    :class:`WorkflowExecutor` to a terminal phase against the bundle's
    own per-task journal. It returns the
    :class:`ExecutionOutcome` so scenarios can sanity-check the
    final phase / cost without re-reading ``status.json``, though
    most scenarios prefer ``bundle.read_status()`` for the full
    structured form.

Isolation guarantees
--------------------

Every fixture in this module is function-scoped and lives under
``tmp_path``, so a parallel ``pytest -n auto`` run never aliases
journal roots, fake-claude binaries, or persona dirs across workers.
"""

from __future__ import annotations

import json
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import pytest

# --------------------------------------------------------------------------- #
# Sibling-worktree PYTHONPATH leak guard (runs BEFORE executor import)
# --------------------------------------------------------------------------- #
#
# A working pattern across the Shohoku team is to develop in a per-persona
# git worktree (``tigerharness-haruko``, ``tigerharness-rukawa``, ...). When
# the shell exports ``PYTHONPATH=/.../tigerharness/src`` (the main worktree's
# source root) and pytest is run inside a *sibling* worktree, Python resolves
# ``tigerharness.workflow_runner`` from the main worktree -- silently
# shadowing whatever the sibling worktree's branch currently has.
#
# That hides newly-added modules: e.g. before a branch's new module is
# merged to main, importing it from a sibling worktree raises a confusing
# ``ModuleNotFoundError`` because the main-worktree copy on PYTHONPATH
# wins. We detect that with the always-present ``cli`` module as a
# canary, BEFORE we try to import anything that might only exist in one
# worktree.
import os as _os
import warnings as _warnings

from tigerharness.workflow_runner import cli as wf_cli


def _check_pythonpath_leak() -> None:
    """Warn (don't raise) if ``tigerharness`` imports from a sibling worktree.

    Uses ``cli`` as the canary because it's been in ``workflow_runner/`` since
    the package's first commit -- a brand-new module like ``executor`` is
    not a safe canary, since its absence from the shadowing worktree would
    cause the import a few lines below to raise before this guard runs.

    We *warn*, not raise: a test run that still works under a leaked
    PYTHONPATH should be allowed to proceed. The warning makes the
    misconfiguration visible so the next person to debug a stale-code
    surprise has a head start.
    """
    if not _os.environ.get("PYTHONPATH"):
        return
    cli_file = getattr(wf_cli, "__file__", None)
    if not cli_file:
        return  # pragma: no cover - defensive; cli must be loaded here.
    cli_dir = Path(cli_file).resolve().parent
    expected_src = (Path(__file__).resolve().parents[3] / "src").resolve()
    if cli_dir.is_relative_to(expected_src):
        return  # importer picked the right worktree -- nothing to flag.
    _warnings.warn(
        "PYTHONPATH points the tigerharness package at a different "
        "worktree than this conftest lives in.\n"
        f"  PYTHONPATH    : {_os.environ['PYTHONPATH']}\n"
        f"  this conftest : {Path(__file__).resolve()}\n"
        f"  cli resolved  : {cli_dir}\n"
        "If e2e tests fail with ModuleNotFoundError or stale-code "
        "behaviour, unset PYTHONPATH before invoking pytest "
        "(``unset PYTHONPATH && uv run pytest ...``).",
        stacklevel=1,
    )


_check_pythonpath_leak()

from tigerharness.workflow_runner.executor import WorkflowExecutor  # noqa: E402
from tigerharness.workflow_runner.paths import TaskPaths  # noqa: E402


# --------------------------------------------------------------------------- #
# Step playbook
# --------------------------------------------------------------------------- #


_E2E_DIR = Path(__file__).parent
_STEPS_DIR = _E2E_DIR / "steps"


@pytest.fixture
def e2e_steps_dir() -> Path:
    """Return the on-disk path to the canonical 3-step playbook.

    Re-used verbatim by every scenario. Tests that need a tweaked
    variant should copy into ``tmp_path`` and edit there rather than
    mutating the shared fixture.
    """
    assert _STEPS_DIR.is_dir(), (
        f"e2e steps dir missing: {_STEPS_DIR}. "
        "Did the package layout change?"
    )
    return _STEPS_DIR


# --------------------------------------------------------------------------- #
# Personas dir (stub prompts for the playbook's personas)
# --------------------------------------------------------------------------- #


_PERSONAS_USED = ("anzai", "akagi", "rukawa")


@pytest.fixture
def e2e_personas_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Drop stub persona prompt files and point the loader at them.

    The :class:`SessionManager` calls
    ``tigerharness.task_runner.personas.load_prompt(persona)`` on the
    *first* call per persona (i.e. when there's no ``--resume <sid>``
    to attach the prior session). Without a real prompt file the
    loader raises, so every persona used by the canonical playbook
    needs a stub here even if the test's scripted fake-claude never
    reads the prompt.
    """
    d = tmp_path / "personas"
    d.mkdir()
    for persona in _PERSONAS_USED:
        (d / f"{persona}.md").write_text(
            f"# stub prompt for {persona}\n"
            f"You are {persona}. This is an e2e test fixture.\n"
        )
    monkeypatch.setenv("TIGERHARNESS_PERSONAS_DIR", str(d))
    return d


# --------------------------------------------------------------------------- #
# Journal root (per-test)
# --------------------------------------------------------------------------- #


@pytest.fixture
def e2e_journal_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A clean per-test workflow journal root.

    Pointed at via ``TIGERHARNESS_WORKFLOW_JOURNAL`` so
    ``default_journal_root()`` -- and every CLI command that calls
    it -- ends up under ``tmp_path``. Cleared on test teardown by
    pytest's tmp_path machinery.
    """
    root = tmp_path / "journal"
    root.mkdir()
    monkeypatch.setenv("TIGERHARNESS_WORKFLOW_JOURNAL", str(root))
    return root


# --------------------------------------------------------------------------- #
# Fake claude (scripted)
# --------------------------------------------------------------------------- #


_FAKE_CLAUDE_SOURCE = (
    Path(__file__).parent.parent / "fixtures" / "fake_claude.py"
).read_text()


class FakeClaude:
    """Thin handle around the scripted fake-claude binary.

    Holds the binary path + the current script path so a test can
    call :meth:`set_script` once at setup and tweak it later if it
    needs to (rare). Also exposes :meth:`counter` so a test can
    assert how many times the executor actually called the fake.
    """

    def __init__(
        self,
        *,
        binary: Path,
        monkeypatch: pytest.MonkeyPatch,
        scratch: Path,
    ) -> None:
        self.binary = binary
        self._mp = monkeypatch
        self._scratch = scratch
        self._script_path: Path | None = None

    def set_script(self, responses: Iterable[dict[str, Any]]) -> Path:
        """Write a scripted-response file and point the fake at it.

        ``responses`` is the ordered list of per-call entries; each
        entry is the dict shape documented in ``fake_claude.py``'s
        module docstring (``trailer`` required; ``body`` /
        ``cost_usd`` / ``session_id`` / ``persona`` / ``iter``
        optional).

        Returns the script path so the caller may inspect the
        sidecar ``.counter`` file after the run.
        """
        payload = {"responses": list(responses)}
        path = self._scratch / "fake_claude_script.json"
        path.write_text(json.dumps(payload, indent=2))
        self._script_path = path
        # Reset the sidecar counter so a mid-run script rewrite starts
        # at response #0 of the new list.
        try:
            self._counter_path().unlink()
        except FileNotFoundError:
            pass
        self._mp.setenv("FAKE_CLAUDE_SCRIPT", str(path))
        return path

    def counter(self) -> int:
        """Return how many scripted responses have been consumed.

        Useful in assertions: a happy-path scenario with 3 APPROVEs
        should leave the counter at 3 exactly.
        """
        if self._script_path is None:
            return 0
        try:
            return int(self._counter_path().read_text().strip() or "0")
        except (OSError, ValueError):
            return 0

    def _counter_path(self) -> Path:
        """Sidecar counter path used by ``fake_claude.py`` invocations.

        The fake writes its consumed-response count to ``<script>.counter``;
        both :meth:`set_script` (which clears it) and :meth:`counter` (which
        reads it) route through this single derivation.
        """
        assert self._script_path is not None, (
            "_counter_path() requires set_script() to have been called first"
        )
        return self._script_path.with_suffix(self._script_path.suffix + ".counter")


@pytest.fixture
def e2e_fake_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> FakeClaude:
    """Drop the fake claude binary, wire envs, return a scripting handle.

    Each test gets its own binary path under ``tmp_path`` so parallel
    runs cannot share state. All ``FAKE_*`` env vars inherited from
    the surrounding shell are cleared up-front so a stray
    ``FAKE_SLEEP_SEC`` from a previous test process can't slip in.
    """
    binary = tmp_path / "claude-fake-e2e.py"
    binary.write_text(f"#!{sys.executable}\n{_FAKE_CLAUDE_SOURCE}")
    binary.chmod(
        binary.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
    )
    monkeypatch.setenv("TIGERHARNESS_CLAUDE_BIN", str(binary))

    # Defence in depth: scrub the env of any inherited FAKE_*
    # configuration so the scripted branch is the only thing driving
    # the fake's behaviour.
    for key in list(_os.environ):
        if key.startswith("FAKE_"):
            monkeypatch.delenv(key, raising=False)

    scratch = tmp_path / "fake_scratch"
    scratch.mkdir()
    return FakeClaude(binary=binary, monkeypatch=monkeypatch, scratch=scratch)


# --------------------------------------------------------------------------- #
# Driver harness
# --------------------------------------------------------------------------- #


@dataclass
class E2EBundle:
    """What :func:`e2e_driver` returns to a test.

    Holds the task id, the resolved per-task paths, the fake-claude
    handle, and the journal root. ``run_executor`` invokes Rukawa's
    :class:`WorkflowExecutor` against this task's journal and returns
    its :class:`ExecutionOutcome` (see :func:`e2e_driver`).
    """

    task_id: str
    paths: TaskPaths
    fake_claude: FakeClaude
    journal_root: Path
    team: str
    run_executor: Callable[[], Any]

    # --- convenience accessors ------------------------------------------- #

    def read_events(self) -> list[dict[str, Any]]:
        """Return parsed records from ``events.jsonl`` (empty list if absent)."""
        try:
            text = self.paths.events_jsonl.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        out: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out

    def read_status(self) -> dict[str, Any]:
        """Return the current ``status.json`` as a plain dict."""
        raw = json.loads(self.paths.status_json.read_text(encoding="utf-8"))
        assert isinstance(raw, dict), "status.json must be a JSON object"
        return raw


def _run_executor_for(paths: TaskPaths) -> Callable[[], Any]:
    """Build the bundle's ``run_executor`` closure.

    Captures ``paths`` (this task's resolved :class:`TaskPaths`) and,
    when called by a scenario, constructs a fresh
    :class:`WorkflowExecutor` and runs it to a terminal phase.

    The closure constructs the executor *lazily* (at call time, not at
    bundle-build time) so a scenario can configure the fake's script
    after ``e2e_driver(...)`` returns but before the loop starts.
    """

    def _run() -> Any:
        return WorkflowExecutor(paths).run()

    return _run


@pytest.fixture
def e2e_driver(
    e2e_steps_dir: Path,
    e2e_personas_dir: Path,
    e2e_journal_root: Path,
    e2e_fake_claude: FakeClaude,
) -> Callable[..., E2EBundle]:
    """Return a factory that initialises a task and returns an :class:`E2EBundle`.

    Usage::

        def test_scenario(e2e_driver):
            bundle = e2e_driver(team="Shohoku")
            bundle.fake_claude.set_script([{...}, {...}, {...}])
            bundle.run_executor()
            events = bundle.read_events()
            status = bundle.read_status()
            assert status["phase"] == "done"

    The factory is parameterised by ``team`` and ``task_id`` so a
    multi-task test (concurrency, prefix lookup) can drive several
    tasks against the same journal root.
    """
    def _factory(
        *,
        team: str = "ShohokuE2E",
        task_id: str = "",
    ) -> E2EBundle:
        argv = ["start", "--team", team, "--steps", str(e2e_steps_dir)]
        if task_id:
            argv += ["--task-id", task_id]
        rc = wf_cli.main(argv)
        assert rc == 0, (
            f"cli.main({argv!r}) returned {rc}; "
            "task initialisation failed before the test could run."
        )

        # Discover the freshly-minted task-id by inspecting the
        # journal root. ``cli start`` mints the id when --task-id is
        # not supplied, so we read it back rather than trying to
        # predict its uuid suffix.
        candidates = sorted(
            p.name for p in e2e_journal_root.iterdir() if p.is_dir()
        )
        if task_id:
            assert task_id in candidates, (
                f"--task-id {task_id!r} requested but not found in "
                f"journal root after start: {candidates}"
            )
            resolved = task_id
        else:
            # When the test does not pin a task-id, we expect exactly
            # one new task-dir to have appeared in this journal root
            # (each test gets a fresh root via ``e2e_journal_root``).
            assert len(candidates) == 1, (
                "expected exactly one task-dir under the fresh "
                f"journal root, found: {candidates}"
            )
            resolved = candidates[0]

        paths = TaskPaths(root=e2e_journal_root, task_id=resolved)
        return E2EBundle(
            task_id=resolved,
            paths=paths,
            fake_claude=e2e_fake_claude,
            journal_root=e2e_journal_root,
            team=team,
            run_executor=_run_executor_for(paths),
        )

    return _factory

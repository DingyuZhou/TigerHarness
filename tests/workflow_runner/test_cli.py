"""Integration tests for ``tigerharness.workflow_runner.cli``.

Each subcommand gets a happy-path test + a not-found / bad-input
test. We point the CLI at a tmp-dir journal root via the
``TIGERHARNESS_WORKFLOW_JOURNAL`` env var so the tests never touch
the user's real state dir.
"""

from __future__ import annotations

import json
import os
import textwrap
import threading
import time
from pathlib import Path

import pytest

from tigerharness.workflow_runner import cli


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


@pytest.fixture()
def journal_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "journal"
    root.mkdir()
    monkeypatch.setenv("TIGERHARNESS_WORKFLOW_JOURNAL", str(root))
    return root


STEP_TEMPLATE = textwrap.dedent(
    """\
    ---
    id: {id}
    persona: {persona}
    role: {role}
    on_approve: {on_approve}
    on_revise: {on_revise}
    on_block: {on_block}
    max_iters: 3
    timeout_sec: 600
    parallel_with: []
    ---

    Body for {id}.
    """
)


def _make_step_dir(base: Path, steps: list[dict[str, str]]) -> Path:
    """Create a steps dir with the given frontmatter dicts."""
    d = base / "steps_src"
    d.mkdir(parents=True, exist_ok=True)
    for i, step in enumerate(steps, start=1):
        body = STEP_TEMPLATE.format(**step)
        (d / f"{i:02d}-{step['id']}.md").write_text(body)
    return d


def _basic_steps() -> list[dict[str, str]]:
    return [
        {
            "id": "01-anzai-plan",
            "persona": "anzai",
            "role": "planner",
            "on_approve": "02-akagi-critique",
            "on_revise": "01-anzai-plan",
            "on_block": "__escalate__",
        },
        {
            "id": "02-akagi-critique",
            "persona": "akagi",
            "role": "exec_critic",
            "on_approve": "__done__",
            "on_revise": "01-anzai-plan",
            "on_block": "__escalate__",
        },
    ]


def _start_task(journal_root: Path, base: Path, *,
                team: str = "Shohoku", task_id: str = "") -> str:
    """Run ``start`` and return the task-id (only one task in root)."""
    steps_dir = _make_step_dir(base, _basic_steps())
    argv = ["start", "--team", team, "--steps", str(steps_dir)]
    if task_id:
        argv.extend(["--task-id", task_id])
    rc = cli.main(argv)
    assert rc == 0
    dirs = [p for p in journal_root.iterdir() if p.is_dir()]
    assert len(dirs) >= 1
    if task_id:
        return task_id
    return dirs[-1].name


# --------------------------------------------------------------------------- #
# start
# --------------------------------------------------------------------------- #


def test_start_happy_path(
    journal_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    steps_dir = _make_step_dir(tmp_path, _basic_steps())
    rc = cli.main(["start", "--team", "Shohoku", "--steps", str(steps_dir)])
    assert rc == 0

    out = capsys.readouterr().out
    assert "Task initialised:" in out
    assert "executor not yet wired" in out

    dirs = [p for p in journal_root.iterdir() if p.is_dir()]
    assert len(dirs) == 1
    task_dir = dirs[0]

    # Required files all exist.
    assert (task_dir / "status.json").is_file()
    assert (task_dir / "orchestration.json").is_file()
    assert (task_dir / "sessions.json").is_file()
    assert (task_dir / "events.jsonl").is_file()
    assert (task_dir / "steps").is_dir()
    assert (task_dir / "steps" / "01-anzai-plan.md").is_file()
    assert (task_dir / "steps" / "02-akagi-critique.md").is_file()

    status = json.loads((task_dir / "status.json").read_text())
    assert status["phase"] == "execute"
    assert status["current_step"] == "01-anzai-plan"
    assert status["current_iter"] == 0
    # No step has actually started yet -- step_started_at must be None
    # and iter_counts must be empty so the executor populates honestly.
    assert status["step_started_at"] is None
    assert status["iter_counts"] == {}

    orch = json.loads((task_dir / "orchestration.json").read_text())
    assert orch["team"] == "Shohoku"
    assert orch["steps"] == ["01-anzai-plan", "02-akagi-critique"]
    assert orch["entrypoint"] == "01-anzai-plan"
    assert orch["edges"]["01-anzai-plan"]["on_approve"] == "02-akagi-critique"
    assert orch["workflow_config"]["human_gate"] is False

    sessions = json.loads((task_dir / "sessions.json").read_text())
    assert sessions == {}


def test_start_explicit_task_id(journal_root: Path, tmp_path: Path) -> None:
    task_id = _start_task(journal_root, tmp_path, task_id="my-custom-id")
    assert task_id == "my-custom-id"
    assert (journal_root / task_id).is_dir()


def test_start_rejects_missing_steps_dir(
    journal_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(["start", "--team", "Shohoku",
                   "--steps", "/nonexistent/path/here"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "steps directory not found" in err


def test_start_rejects_empty_steps_dir(
    journal_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    rc = cli.main(["start", "--team", "T", "--steps", str(empty)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "no .md step files" in err


def test_start_rejects_step_without_frontmatter(
    journal_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    d = tmp_path / "steps_src"
    d.mkdir()
    (d / "01-bad.md").write_text("just a body, no fence\n")
    rc = cli.main(["start", "--team", "T", "--steps", str(d)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "no YAML frontmatter" in err


def test_start_rejects_bad_frontmatter_fields(
    journal_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    d = tmp_path / "steps_src"
    d.mkdir()
    (d / "01-bad.md").write_text(textwrap.dedent("""\
        ---
        id: foo
        persona: anzai
        role: planner
        on_approve: __done__
        on_revise: foo
        on_block: __escalate__
        max_iters: -5
        timeout_sec: 600
        ---
        body
    """))
    rc = cli.main(["start", "--team", "T", "--steps", str(d)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "max_iters" in err


def test_start_rejects_duplicate_step_ids(
    journal_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    steps = _basic_steps()
    # Make both steps share an id by reusing the first's id on the second.
    steps[1]["id"] = steps[0]["id"]
    # Adjust routing so the model itself validates per-file.
    steps[0]["on_approve"] = "__done__"
    steps[1]["on_approve"] = "__done__"
    steps_dir = _make_step_dir(tmp_path, steps)
    rc = cli.main(["start", "--team", "T", "--steps", str(steps_dir)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "duplicate step id" in err


def test_start_rejects_bad_explicit_task_id(
    journal_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    steps_dir = _make_step_dir(tmp_path, _basic_steps())
    rc = cli.main([
        "start", "--team", "T", "--steps", str(steps_dir),
        "--task-id", "bad id with spaces",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "task-id" in err


def test_start_rejects_existing_task_folder(
    journal_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    steps_dir = _make_step_dir(tmp_path, _basic_steps())
    rc1 = cli.main([
        "start", "--team", "T", "--steps", str(steps_dir),
        "--task-id", "dup",
    ])
    assert rc1 == 0
    capsys.readouterr()
    rc2 = cli.main([
        "start", "--team", "T", "--steps", str(steps_dir),
        "--task-id", "dup",
    ])
    assert rc2 == 2
    err = capsys.readouterr().err
    assert "already exists" in err


def test_start_rejects_unsafe_step_id(
    journal_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    d = tmp_path / "steps_src"
    d.mkdir()
    (d / "01-bad.md").write_text(textwrap.dedent("""\
        ---
        id: bad/slash
        persona: anzai
        role: planner
        on_approve: __done__
        on_revise: bad/slash
        on_block: __escalate__
        max_iters: 1
        timeout_sec: 600
        ---
        body
    """))
    rc = cli.main(["start", "--team", "T", "--steps", str(d)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "filename-safe" in err


def test_start_rejects_non_yaml_mapping_frontmatter(
    journal_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    d = tmp_path / "steps_src"
    d.mkdir()
    # YAML list, not a mapping -> _parse_frontmatter returns {}
    (d / "01-bad.md").write_text("---\n- one\n- two\n---\nbody\n")
    rc = cli.main(["start", "--team", "T", "--steps", str(d)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "no YAML frontmatter" in err


# --------------------------------------------------------------------------- #
# show
# --------------------------------------------------------------------------- #


def test_show_happy_path(
    journal_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    task_id = _start_task(journal_root, tmp_path, task_id="show-task")
    capsys.readouterr()

    # Inject a couple of history rows so the "last 5 per step" branch runs.
    status_p = journal_root / task_id / "status.json"
    status = json.loads(status_p.read_text())
    status["step_history"] = [
        {
            "step": "01-anzai-plan",
            "iter": 1,
            "persona": "anzai",
            "started_at": "2026-05-28T14:00:00Z",
            "ended_at": "2026-05-28T14:01:00Z",
            "verdict": "APPROVE",
            "reason": None,
            "cost_usd": 0.1,
        },
        {
            "step": "01-anzai-plan",
            "iter": 2,
            "persona": "anzai",
            "started_at": "2026-05-28T14:02:00Z",
            "ended_at": "2026-05-28T14:03:00Z",
            "verdict": "REVISE",
            "reason": "x" * 200,
            "cost_usd": 0.1,
        },
    ]
    status_p.write_text(json.dumps(status))

    rc = cli.main(["show", task_id])
    assert rc == 0
    out = capsys.readouterr().out
    assert task_id in out
    assert "phase:" in out
    assert "01-anzai-plan" in out
    assert "REVISE" in out


def test_show_prefix_lookup(
    journal_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    task_id = _start_task(journal_root, tmp_path, task_id="prefix-target")
    capsys.readouterr()
    rc = cli.main(["show", "prefix"])
    assert rc == 0
    out = capsys.readouterr().out
    assert task_id in out


def test_show_not_found(
    journal_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(["show", "missing-id"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "no task matches" in err


def test_show_ambiguous_prefix(
    journal_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _start_task(journal_root, tmp_path, task_id="amb-aaa")
    capsys.readouterr()
    # Build a second task in a fresh source dir.
    steps_dir = _make_step_dir(tmp_path / "src2", _basic_steps())
    cli.main(["start", "--team", "T", "--steps", str(steps_dir),
              "--task-id", "amb-bbb"])
    capsys.readouterr()

    rc = cli.main(["show", "amb"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "ambiguous" in err


def test_show_empty_prefix_rejected(
    journal_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(["show", ""])
    assert rc == 1
    err = capsys.readouterr().err
    assert "non-empty" in err


def test_show_handles_missing_status(
    journal_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Create a task dir with no status.json.
    bare = journal_root / "bare-task"
    bare.mkdir()
    rc = cli.main(["show", "bare-task"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "cannot read status.json" in err


def test_show_handles_non_dict_status(
    journal_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = journal_root / "bad-status"
    bad.mkdir()
    (bad / "status.json").write_text('"not a dict"')
    rc = cli.main(["show", "bad-status"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "cannot read status.json" in err


def test_show_no_root_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(
        "TIGERHARNESS_WORKFLOW_JOURNAL", str(tmp_path / "absent")
    )
    rc = cli.main(["show", "anything"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "no task matches" in err or "no journal" in err


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #


def test_list_happy_path(
    journal_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    task_id = _start_task(journal_root, tmp_path, task_id="active-1")
    capsys.readouterr()
    rc = cli.main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert task_id in out
    assert "phase" in out
    assert "execute" in out


def test_list_default_hides_terminal(
    journal_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    task_id = _start_task(journal_root, tmp_path, task_id="terminal-1")
    capsys.readouterr()
    status_p = journal_root / task_id / "status.json"
    status = json.loads(status_p.read_text())
    status["phase"] = "done"
    status_p.write_text(json.dumps(status))

    rc = cli.main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert task_id not in out
    assert "No active tasks" in out


def test_list_all_includes_terminal(
    journal_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    task_id = _start_task(journal_root, tmp_path, task_id="terminal-2")
    capsys.readouterr()
    status_p = journal_root / task_id / "status.json"
    status = json.loads(status_p.read_text())
    status["phase"] = "done"
    status_p.write_text(json.dumps(status))

    rc = cli.main(["list", "--all"])
    assert rc == 0
    out = capsys.readouterr().out
    assert task_id in out
    assert "done" in out


def test_list_filter_by_team(
    journal_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a = _make_step_dir(tmp_path / "a", _basic_steps())
    b = _make_step_dir(tmp_path / "b", _basic_steps())
    cli.main(["start", "--team", "Shohoku",
              "--steps", str(a), "--task-id", "team-a"])
    cli.main(["start", "--team", "Sannoh",
              "--steps", str(b), "--task-id", "team-b"])
    capsys.readouterr()

    rc = cli.main(["list", "--team", "shohoku"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "team-a" in out
    assert "team-b" not in out


def test_list_no_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(
        "TIGERHARNESS_WORKFLOW_JOURNAL", str(tmp_path / "missing")
    )
    rc = cli.main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "No tasks" in out


def test_list_all_empty_root(
    journal_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(["list", "--all"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "No tasks." in out


def test_list_skips_dirs_without_status(
    journal_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _start_task(journal_root, tmp_path, task_id="real-task")
    (journal_root / "garbage").mkdir()
    (journal_root / "loose-file").write_text("ignore me")
    capsys.readouterr()
    rc = cli.main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "real-task" in out
    assert "garbage" not in out


def test_list_skips_corrupt_status(
    journal_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = journal_root / "broken"
    bad.mkdir()
    (bad / "status.json").write_text("not json{{")
    rc = cli.main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    # No row for the broken task, just the "no active tasks" line.
    assert "broken" not in out


def test_list_skips_non_dict_status(
    journal_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = journal_root / "weird"
    bad.mkdir()
    (bad / "status.json").write_text('["not", "a", "dict"]')
    rc = cli.main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "weird" not in out


def test_list_team_filter_skips_when_orchestration_missing(
    journal_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _start_task(journal_root, tmp_path, task_id="real-team")
    # A task dir with a status but no orchestration.json -> _team_for None.
    odd = journal_root / "no-orch"
    odd.mkdir()
    (odd / "status.json").write_text(json.dumps({
        "task_id": "no-orch",
        "phase": "execute",
        "started_at": "2026-05-28T14:00:00Z",
        "current_step": None,
        "current_iter": 0,
    }))
    capsys.readouterr()
    rc = cli.main(["list", "--team", "Shohoku"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no-orch" not in out
    assert "real-team" in out


# --------------------------------------------------------------------------- #
# tail
# --------------------------------------------------------------------------- #


def _append_event(events_path: Path, payload: dict) -> None:
    events_path.parent.mkdir(parents=True, exist_ok=True)
    with open(events_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload) + "\n")


def test_tail_happy_path(
    journal_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    task_id = _start_task(journal_root, tmp_path, task_id="tail-task")
    capsys.readouterr()
    events_p = journal_root / task_id / "events.jsonl"
    _append_event(events_p, {"ts": "2026-05-28T14:00:00Z",
                              "kind": "task_started", "task_id": task_id})
    _append_event(events_p, {"ts": "2026-05-28T14:00:01Z",
                              "kind": "step_started",
                              "step": "01-anzai-plan", "iter": 1})

    rc = cli.main(["tail", task_id])
    assert rc == 0
    out = capsys.readouterr().out
    assert "task_started" in out
    assert "step_started" in out
    assert "01-anzai-plan" in out


def test_tail_empty_says_so(
    journal_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    task_id = _start_task(journal_root, tmp_path, task_id="empty-evt")
    capsys.readouterr()
    rc = cli.main(["tail", task_id])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no events yet" in out


def test_tail_not_found(
    journal_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(["tail", "nope"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "no task matches" in err


def test_tail_skips_corrupt_lines(
    journal_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    task_id = _start_task(journal_root, tmp_path, task_id="evt-corrupt")
    capsys.readouterr()
    events_p = journal_root / task_id / "events.jsonl"
    with open(events_p, "a") as fh:
        fh.write("not-json\n")
        fh.write(json.dumps({"ts": "2026-05-28T14:00:00Z",
                             "kind": "valid_event"}) + "\n")
        # A JSON array on its own line (not a dict) -> skipped.
        fh.write("[1,2,3]\n")
    rc = cli.main(["tail", task_id])
    assert rc == 0
    out = capsys.readouterr().out
    assert "valid_event" in out


def test_tail_follow_streams_new_events(
    journal_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    task_id = _start_task(journal_root, tmp_path, task_id="follow-task")
    capsys.readouterr()
    events_p = journal_root / task_id / "events.jsonl"

    # Pre-seed one event so the initial pass prints something.
    _append_event(events_p, {"ts": "2026-05-28T14:00:00Z",
                              "kind": "seed"})

    # Run --follow on a background thread; append events and then
    # interrupt the main thread to break the loop.
    main_tid = threading.get_ident()

    def appender() -> None:
        time.sleep(0.05)
        _append_event(events_p, {"ts": "2026-05-28T14:00:01Z",
                                  "kind": "live_event"})
        time.sleep(0.05)
        import _thread
        _thread.interrupt_main()

    t = threading.Thread(target=appender)
    t.start()
    rc = cli.main(["tail", task_id, "--follow", "--poll-interval", "0.02"])
    t.join()
    assert rc == 0
    out = capsys.readouterr().out
    assert "seed" in out
    assert "live_event" in out
    # Sanity: the appender ran on a different thread.
    assert main_tid == threading.get_ident()


def test_tail_follow_handles_truncate(
    journal_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """If events.jsonl is rewritten smaller mid-follow, the reader resets."""
    task_id = _start_task(journal_root, tmp_path, task_id="trunc-task")
    capsys.readouterr()
    events_p = journal_root / task_id / "events.jsonl"
    # Seed with several events so the initial offset is well above the
    # post-truncate file size; otherwise the size-shrank branch is a
    # no-op.
    for i in range(5):
        _append_event(events_p, {"ts": "2026-05-28T14:00:00Z",
                                  "kind": f"seed_event_long_kind_{i}"})

    def churn() -> None:
        time.sleep(0.1)
        # Truncate and write a single short record so the new size is
        # smaller than the prior offset -- forces the reset branch.
        events_p.write_text(
            json.dumps({"ts": "2026-05-28T14:00:02Z",
                        "kind": "k2"}) + "\n"
        )
        # Give the follower a couple of poll cycles to notice.
        time.sleep(0.3)
        import _thread
        _thread.interrupt_main()

    t = threading.Thread(target=churn)
    t.start()
    rc = cli.main(["tail", task_id, "--follow", "--poll-interval", "0.02"])
    t.join()
    assert rc == 0
    out = capsys.readouterr().out
    assert "k2" in out


# --------------------------------------------------------------------------- #
# cancel
# --------------------------------------------------------------------------- #


def test_cancel_happy_path(
    journal_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    task_id = _start_task(journal_root, tmp_path, task_id="to-cancel")
    capsys.readouterr()
    rc = cli.main(["cancel", task_id])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Cancel requested" in out

    cancel_flag = journal_root / task_id / ".cancel"
    assert cancel_flag.exists()
    status = json.loads(
        (journal_root / task_id / "status.json").read_text()
    )
    assert status["phase"] == "cancelling"


def test_cancel_idempotent(
    journal_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    task_id = _start_task(journal_root, tmp_path, task_id="re-cancel")
    capsys.readouterr()
    assert cli.main(["cancel", task_id]) == 0
    capsys.readouterr()
    # Remove the flag, run again -- should re-create the flag and
    # print the idempotent message.
    (journal_root / task_id / ".cancel").unlink()
    rc = cli.main(["cancel", task_id])
    assert rc == 0
    out = capsys.readouterr().out
    assert "already cancelling" in out
    assert (journal_root / task_id / ".cancel").exists()


def test_cancel_idempotent_keeps_existing_flag(
    journal_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    task_id = _start_task(journal_root, tmp_path, task_id="recancel-keep")
    capsys.readouterr()
    assert cli.main(["cancel", task_id]) == 0
    capsys.readouterr()
    rc = cli.main(["cancel", task_id])
    assert rc == 0
    out = capsys.readouterr().out
    assert "already cancelling" in out


def test_cancel_refuses_terminal(
    journal_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    task_id = _start_task(journal_root, tmp_path, task_id="finished")
    capsys.readouterr()
    status_p = journal_root / task_id / "status.json"
    status = json.loads(status_p.read_text())
    status["phase"] = "done"
    status_p.write_text(json.dumps(status))

    rc = cli.main(["cancel", task_id])
    assert rc == cli._EXIT_TERMINAL
    err = capsys.readouterr().err
    assert "already done" in err


def test_cancel_not_found(
    journal_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(["cancel", "absent"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "no task matches" in err


def test_cancel_handles_missing_status(
    journal_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bare = journal_root / "no-status"
    bare.mkdir()
    rc = cli.main(["cancel", "no-status"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "cannot read status.json" in err


# --------------------------------------------------------------------------- #
# Internals / parser
# --------------------------------------------------------------------------- #


def test_parser_requires_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main([])


def test_resolve_task_id_prefix_exact(tmp_path: Path) -> None:
    (tmp_path / "abc").mkdir()
    assert cli._resolve_task_id_prefix(tmp_path, "abc") == "abc"


def test_parse_frontmatter_unfenced_returns_empty() -> None:
    assert cli._parse_frontmatter("no fence here\n") == {}


def test_parse_frontmatter_unterminated_returns_empty() -> None:
    text = "---\nkey: value\nbut no closing fence\n"
    assert cli._parse_frontmatter(text) == {}


def test_parse_frontmatter_broken_yaml_returns_empty() -> None:
    text = "---\n: : not valid yaml :\n---\nbody\n"
    assert cli._parse_frontmatter(text) == {}


def test_parse_frontmatter_empty_block_returns_empty() -> None:
    text = "---\n---\nbody\n"
    assert cli._parse_frontmatter(text) == {}


def test_fmt_age_handles_none_and_bad() -> None:
    assert cli._fmt_age(None) == "(never)"
    assert cli._fmt_age("nonsense") == "nonsense"


def test_fmt_age_ranges() -> None:
    # Spot-check formatting of recent / old timestamps.
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    sec_ago = (now - dt.timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert cli._fmt_age(sec_ago).endswith("s ago")
    min_ago = (now - dt.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert cli._fmt_age(min_ago).endswith("m ago")
    hr_ago = (now - dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert cli._fmt_age(hr_ago).endswith("h ago")
    day_ago = (now - dt.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert cli._fmt_age(day_ago).endswith("d ago")


def test_validate_step_id_accepts_safe() -> None:
    assert cli._validate_step_id("01-anzai-plan") == "01-anzai-plan"


def test_validate_step_id_rejects_unsafe() -> None:
    from tigerharness.workflow_runner import WorkflowModelError
    with pytest.raises(WorkflowModelError):
        cli._validate_step_id("../escape")


def test_format_event_line_no_extras() -> None:
    line = cli._format_event_line({"ts": "t", "kind": "k"})
    assert line == "t  k"


def test_format_event_line_with_extras() -> None:
    line = cli._format_event_line({"ts": "t", "kind": "k", "x": 1})
    assert "x=1" in line


def test_iter_jsonl_missing_file(tmp_path: Path) -> None:
    records, offset = cli._iter_jsonl(tmp_path / "absent.jsonl", 0)
    assert records == []
    assert offset == 0


def test_iter_jsonl_partial_last_line(tmp_path: Path) -> None:
    p = tmp_path / "evt.jsonl"
    p.write_text('{"ts":"t","kind":"k"}\n{"ts":"t2","kind":"' )
    records, offset = cli._iter_jsonl(p, 0)
    assert len(records) == 1
    # Only the first complete line was consumed; offset stops at the newline.
    assert offset == len(b'{"ts":"t","kind":"k"}\n')


def test_iter_jsonl_only_partial_line(tmp_path: Path) -> None:
    """A file containing only a partial line (no \\n) yields nothing."""
    p = tmp_path / "evt.jsonl"
    p.write_text('{"ts":"t","kind":"in-flight"')
    records, offset = cli._iter_jsonl(p, 0)
    assert records == []
    assert offset == 0


def test_iter_jsonl_skips_blank_lines(tmp_path: Path) -> None:
    p = tmp_path / "evt.jsonl"
    p.write_text('\n\n{"ts":"t","kind":"k"}\n\n')
    records, _offset = cli._iter_jsonl(p, 0)
    assert len(records) == 1
    assert records[0]["kind"] == "k"


def test_fmt_age_naive_datetime() -> None:
    """Naive (tz-less) ISO strings should still parse via the UTC fallback."""
    import datetime as dt
    naive = (
        dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        - dt.timedelta(seconds=30)
    ).strftime("%Y-%m-%dT%H:%M:%S")
    out = cli._fmt_age(naive)
    assert out.endswith("ago")


def test_team_for_handles_corrupt_orchestration(tmp_path: Path) -> None:
    td = tmp_path / "task"
    td.mkdir()
    (td / "orchestration.json").write_text("not json {{")
    assert cli._team_for(td) is None


def test_team_for_handles_non_dict_orchestration(tmp_path: Path) -> None:
    td = tmp_path / "task"
    td.mkdir()
    (td / "orchestration.json").write_text('["not", "a", "dict"]')
    assert cli._team_for(td) is None


def test_team_for_handles_missing_team_field(tmp_path: Path) -> None:
    td = tmp_path / "task"
    td.mkdir()
    (td / "orchestration.json").write_text('{"no_team_here": true}')
    assert cli._team_for(td) is None


def test_start_handles_unreadable_step_file(
    journal_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OSError while reading a step file becomes a clean error message."""
    steps_dir = _make_step_dir(tmp_path, _basic_steps())
    original_read_text = Path.read_text

    def boom(self: Path, *a, **kw):
        if self.suffix == ".md" and self.parent == steps_dir:
            raise OSError("simulated read failure")
        return original_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", boom)
    rc = cli.main(["start", "--team", "T", "--steps", str(steps_dir)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "failed to read step file" in err


def test_start_handles_orchestration_build_failure(
    journal_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model-level failure during Orchestration construction is reported."""
    from tigerharness.workflow_runner import WorkflowModelError

    def boom(**kwargs):
        raise WorkflowModelError("simulated orchestration failure")

    steps_dir = _make_step_dir(tmp_path, _basic_steps())
    monkeypatch.setattr(cli, "_orchestration_for", boom)
    rc = cli.main(["start", "--team", "T", "--steps", str(steps_dir)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "orchestration build failed" in err


def test_dunder_main_module_imports() -> None:
    """``python -m tigerharness.workflow_runner`` entrypoint module is importable.

    Covers the module-level imports inside ``__main__.py``; the
    ``sys.exit(main())`` line itself is ``pragma: no cover`` per
    project convention.
    """
    from tigerharness.workflow_runner import __main__ as wf_main
    assert wf_main.main is cli.main

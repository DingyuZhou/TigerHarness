"""caplog assertions for the five load-bearing log families.

Per the logging-coverage plan: (i) journal gate refusals, (ii) sweep
classifications, (iii) retry/backoff transitions, (iv) backend spawn
nonzero-exit, (v) secret redaction. One assertion per family minimum;
these are the lines a future debugging session greps for first, so
the suite pins their existence and their logger names.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from tigerharness.journal.cli import main as journal_main


@pytest.fixture()
def journal_task(tmp_path: Path) -> tuple[Path, str]:
    """A minimal claimable kind=task journal with one pending task."""
    jdir = tmp_path / "journal"
    prd = tmp_path / "brief.md"
    prd.write_text("# do the thing\n")
    rc = journal_main([
        "--journal-dir", str(jdir), "new", "--kind", "task",
        "--prd", str(prd), "--persona", "chief", "--title", "t",
    ])
    assert rc == 0
    task_id = next(p.name for p in (jdir / "active").iterdir())
    return jdir, task_id


def test_family_i_gate_refusal_claim_busy(journal_task, caplog) -> None:
    jdir, task_id = journal_task
    assert journal_main(["--journal-dir", str(jdir), "claim", task_id]) == 0
    with caplog.at_level(logging.WARNING, "tigerharness.journal.cli"):
        rc = journal_main(["--journal-dir", str(jdir), "claim", task_id])
    assert rc != 0
    assert any(
        "claim refused" in r.message and "busy" in r.message
        for r in caplog.records
    )


def test_family_ii_sweep_classification(journal_task, caplog) -> None:
    jdir, task_id = journal_task
    assert journal_main(["--journal-dir", str(jdir), "claim", task_id]) == 0
    with caplog.at_level(logging.INFO, "tigerharness.journal.sweep"):
        assert journal_main(["--journal-dir", str(jdir), "sweep"]) == 0
    assert any(
        "classified busy" in r.message and task_id in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_family_iii_retry_backoff(caplog) -> None:
    from tigerharness.agent_sdk import AgentConfig
    from tigerharness.agent_sdk.retry import run_with_retry

    calls = {"n": 0}

    class FlakyBackend:
        async def run(self, config, prompt, *, session=None, approval=None):
            calls["n"] += 1
            if calls["n"] < 2:
                raise RuntimeError("flake")
            return "ok"

    with caplog.at_level(logging.INFO, "tigerharness.agent_sdk.retry"):
        out = asyncio.run(
            run_with_retry(
                FlakyBackend(), AgentConfig(name="x"), "hi",
                max_attempts=3, base_delay_s=0.0,
            )
        )
    assert out == "ok"
    assert any("retrying in" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


def test_family_iv_spawn_nonzero_exit(tmp_path, caplog) -> None:
    from tigerharness.agent_sdk import AgentConfig
    from tigerharness.agent_sdk.backends.claude_p import ClaudePBackend

    fake = tmp_path / "fake-claude"
    fake.write_text("#!/bin/sh\necho boom >&2\nexit 7\n")
    fake.chmod(0o755)
    backend = ClaudePBackend(cli=str(fake))
    with caplog.at_level(logging.INFO, "tigerharness.agent_sdk.backends.claude_p"):
        result = asyncio.run(backend.run(AgentConfig(name="x"), "hi"))
    assert result.stop_reason == "error"
    assert any("spawning" in r.message for r in caplog.records)
    assert any(
        "exited nonzero" in r.message and "7" in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_family_v_secret_redaction(tmp_path, caplog) -> None:
    """The lane loader's token-confirmation line must never carry a full
    secret -- only the redact_token() prefix/suffix rendering."""
    from tigerharness.slack_bridge.multi import load_multi

    bot = "xoxb-very-secret-token-value-123456"
    app = "xapp-also-secret-token-value-654321"
    team = tmp_path / "shohoku"
    (team / "configs").mkdir(parents=True)
    (team / "configs" / ".env").write_text(
        f"SLACK_APP_TOKEN={app}\nSLACK_BOT_TOKEN={bot}\n"
    )
    persona = team / "personas" / "ayako"
    persona.mkdir(parents=True)
    (persona / "prompt.md").write_text("You are ayako.")
    (team / "memories" / "ayako").mkdir(parents=True)
    (team / "memories" / "ayako" / "tiger-memory.config.yaml").write_text(
        "agent: {name: test}\n"
    )
    (team / "configs" / "personas.yaml").write_text(
        "personas:\n  - name: ayako\n"
    )
    (team / "configs" / "slack-bridge.yaml").write_text(
        "default_persona: ayako\n"
        "allowed_user_ids:\n  - U0CEO\n"
        f"state_dir: {tmp_path / 'state'}\n"
    )
    index = tmp_path / "slack-bridge.yaml"
    index.write_text("lanes:\n  - shohoku\n")

    with caplog.at_level(logging.INFO, "tigerharness.slack_bridge.multi"):
        cfg = load_multi(index)
    assert len(cfg.lanes) == 1
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "tokens loaded" in joined  # the confirmation line fired
    assert bot not in joined and app not in joined
    assert "xoxb-" in joined and "xapp-" in joined  # prefixes survive

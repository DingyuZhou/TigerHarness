"""Shared pytest fixtures for tiger-memory."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest


@pytest.fixture
def minimal_config_yaml(tmp_path: Path) -> Path:
    """A minimal valid config file. Store root = tmp_path/memory."""
    cfg = tmp_path / "tiger-memory.config.yaml"
    cfg.write_text(
        dedent(
            f"""\
            agent:
              name: TestTiger
              role: "Test consumer."
              pronouns: it/it

            store:
              root: {tmp_path}/memory

            sources:
              - kind: claude_code
                project_path: {tmp_path}/fake-claude-projects/

            summarizer:
              backend: anthropic
              model: claude-opus-4-7
              prompts: default/v1

            rebuild:
              lock_path: {tmp_path}/test.lock
            """
        )
    )
    return cfg

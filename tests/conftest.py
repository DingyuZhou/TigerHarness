"""Top-level pytest fixtures.

**Bridge env-var guard (autouse).** Running the gated suite from INSIDE a Slack
bridge session leaks bridge-injected env vars into pytest, and env-sensitive
tests false-fail — with ``TIGERHARNESS_SLACK_THREAD_TS`` set, the journal
claim-gate refuses the "bridge" session by design, so ~23 claim/sweep tests go
red even though CI (clean env) is green. This autouse fixture scrubs those vars
before every test, so the suite is environment-independent regardless of where
``uv run pytest`` is invoked. ``delenv(raising=False)`` makes it a no-op in a
clean env. Keep this list in sync with what ``slack_bridge`` injects (today:
``TIGERHARNESS_SLACK_THREAD_TS`` per turn — see ``slack_bridge/bridge.py`` — and
``TIGERHARNESS_BRIDGES_CONFIG`` in the multi-bridge environment).
"""
from __future__ import annotations

import pytest

#: Slack-bridge-injected env vars that leak into pytest from a bridge session and
#: skew env-sensitive tests (the journal claim gate, sweep classification).
BRIDGE_ENV_VARS = (
    "TIGERHARNESS_SLACK_THREAD_TS",
    "TIGERHARNESS_BRIDGES_CONFIG",
)


def scrub_env(monkeypatch: pytest.MonkeyPatch, names=BRIDGE_ENV_VARS) -> None:
    """Unset *names* from the environment via *monkeypatch* (no-op if absent)."""
    for var in names:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def scrub_bridge_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Autouse: scrub bridge env vars before every test (no-op in a clean env)."""
    scrub_env(monkeypatch)

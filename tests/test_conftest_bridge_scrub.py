"""Regression: the autouse conftest fixture scrubs bridge env vars.

Guards the env-independence fix. ``test_bridge_vars_absent_during_tests`` is the
load-bearing guard: run from inside a bridge session (vars exported) — which is
exactly the acceptance run — it stays green ONLY because the autouse conftest
fixture removed them. A future edit that drops the guard (or a named var) turns
it red there.
"""
from __future__ import annotations

import os

import pytest

# The bridge-injected vars the conftest autouse fixture scrubs (kept in sync
# with tests/conftest.py BRIDGE_ENV_VARS; asserted below so neither can shrink
# unnoticed in a bridge env).
NAMED_BRIDGE_VARS = ("TIGERHARNESS_SLACK_THREAD_TS", "TIGERHARNESS_BRIDGES_CONFIG")


@pytest.mark.parametrize("var", NAMED_BRIDGE_VARS)
def test_bridge_var_absent_during_tests(var: str):
    """The autouse fixture removed the bridge var before this test ran —
    even if the calling shell (a bridge session) exported it."""
    assert var not in os.environ, f"{var} leaked into the test env"


def test_scrub_deletes_vars_when_present(monkeypatch: pytest.MonkeyPatch):
    """delenv(raising=False) removes the var when a bridge has set it (and is a
    no-op otherwise) — the mechanism the autouse fixture relies on."""
    for var in NAMED_BRIDGE_VARS:
        monkeypatch.setenv(var, "leaked-value")
        assert os.environ[var] == "leaked-value"
    for var in NAMED_BRIDGE_VARS:
        monkeypatch.delenv(var, raising=False)
    for var in NAMED_BRIDGE_VARS:
        assert var not in os.environ

"""Top-level pytest fixtures.

**Slack env guard (autouse).** Running the gated suite from a shell that
carries Slack state — a bridge session, an autodrive fire, or a dev shell
that sourced a lane ``.env`` — leaks Slack-family env vars into pytest,
and env-sensitive tests false-fail in both directions: with
``TIGERHARNESS_SLACK_THREAD_TS`` set the journal claim-gate refuses the
"bridge" session by design (~23 claim/sweep tests), and with ambient
``SLACK_BOT_TOKEN``/allowlist vars the notify fallback finds real creds,
so the NullNotifier degradation tests flip. ``notify``'s ``.env`` loader
also writes loaded keys straight into ``os.environ``, so one test can
pollute the rest of the run. This autouse fixture scrubs the family
before every test, making the suite environment-independent regardless
of where ``uv run pytest`` is invoked; ``delenv(raising=False)`` makes it
a no-op in a clean env. Keep the list in sync with the actual
``os.environ`` readers under ``src/`` (grep for the names below).
"""
from __future__ import annotations

import pytest

#: Every Slack-family env var read from ``os.environ`` in ``src/`` (plus
#: SLACK_APP_TOKEN, which travels with SLACK_BOT_TOKEN in any sourced
#: ``.env``). Bridge-injected: THREAD_TS (per turn, see
#: ``slack_bridge/bridge.py``) and BRIDGES_CONFIG (process-wide index).
#: Notify-read: SLACK_ENV, BOT_TOKEN, CEO_USER_ID, both allowlist
#: spellings (canonical + legacy fallback). Path overrides:
#: SLACK_STATE_DIR (persistence), ATTACHMENT_DIR (downloader).
BRIDGE_ENV_VARS = (
    "TIGERHARNESS_SLACK_THREAD_TS",
    "TIGERHARNESS_BRIDGES_CONFIG",
    "TIGERHARNESS_SLACK_ENV",
    "TIGERHARNESS_SLACK_STATE_DIR",
    "TIGERHARNESS_ATTACHMENT_DIR",
    "SLACK_APP_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_CEO_USER_ID",
    "SLACK_ALLOWED_USER_IDS",
    "ALLOWED_SLACK_USER_IDS",
)


def scrub_env(monkeypatch: pytest.MonkeyPatch, names=BRIDGE_ENV_VARS) -> None:
    """Unset *names* from the environment via *monkeypatch* (no-op if absent)."""
    for var in names:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def scrub_bridge_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Autouse: scrub Slack env vars before every test (no-op in a clean env)."""
    scrub_env(monkeypatch)

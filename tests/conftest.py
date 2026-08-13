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

**Real-daemon spawn guard (autouse).** The same leak class has a far worse
consequence than a flipped assertion: with the autodrive AUTOSTART variable
visible, journal-scaffolding tests reach the auto-start hook and launch real
detached daemons that outlive pytest and fire real ``claude -p`` drives
forever. Two fixtures answer that -- the scrub removes the trigger,
``block_real_daemon_spawn`` removes the capability. Read
``AUTODRIVE_ENV_VARS`` and :class:`RealDaemonSpawnBlocked` for the full
story; it is worth knowing before touching either.
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
#: Channel overrides: BRIDGE_PROGRESS_CHANNEL and SLACK_NOTIFY_CHANNEL,
#: the turn-progress resolution chain (``slack_bridge/progress.py``);
#: the latter is autodrive's notify channel too. Unscrubbed, a dev shell
#: that exported an ops channel makes the progress-is-inert tests pass
#: for the wrong reason.
BRIDGE_ENV_VARS = (
    "TIGERHARNESS_SLACK_THREAD_TS",
    "TIGERHARNESS_BRIDGES_CONFIG",
    "TIGERHARNESS_SLACK_ENV",
    "TIGERHARNESS_SLACK_STATE_DIR",
    "TIGERHARNESS_ATTACHMENT_DIR",
    "TIGERHARNESS_BRIDGE_PROGRESS_CHANNEL",
    "SLACK_NOTIFY_CHANNEL",
    "SLACK_APP_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_CEO_USER_ID",
    "SLACK_ALLOWED_USER_IDS",
    "ALLOWED_SLACK_USER_IDS",
)

#: The autodrive family, scrubbed for a sharper reason than the Slack one:
#: ``TIGERHARNESS_AUTODRIVE_AUTOSTART`` does not merely skew an assertion,
#: it makes the suite **spawn real detached daemons**.
#:
#: ``journal new`` / ``defer`` / ``materialize`` / ``answer`` call the
#: auto-start hook once the queue write succeeds (ADR 0010). With AUTOSTART
#: visible, every journal-scaffolding test therefore launches a real
#: ``_loop`` process that outlives pytest and fires real ``claude -p``
#: drives on an interval forever, against a tmp-dir state file pytest has
#: already deleted. Thirty such orphans were found alive at once, ~100 MB
#: apiece on a 3.8 GiB box; that memory pressure is what let the OOM killer
#: take the Slack bridge down.
#:
#: An unclean dev shell is only half of it. ``slack_bridge/notify.py``
#: writes every key it loads from a team ``.env`` straight into
#: ``os.environ`` and leaves it there, so a single test loading a fixture
#: ``.env`` that contains ``AUTOSTART=1`` turns it on for the whole rest of
#: the session -- and ``monkeypatch`` cannot undo a write it did not make.
#: Scrubbing before *every* test is what breaks that propagation.
AUTODRIVE_ENV_VARS = (
    "TIGERHARNESS_AUTODRIVE_AUTOSTART",
    "TIGERHARNESS_AUTODRIVE_INTERVAL",
    "TIGERHARNESS_AUTODRIVE_MAX_BUDGET",
    "TIGERHARNESS_AUTODRIVE_DRIVER",
    "TIGERHARNESS_AUTODRIVE_NOTIFY",
    "TIGERHARNESS_AUTODRIVE_NOTIFY_CHANNEL",
)

#: Everything the autouse fixture unsets.
SCRUBBED_ENV_VARS = BRIDGE_ENV_VARS + AUTODRIVE_ENV_VARS


class RealDaemonSpawnBlocked(BaseException):
    """A test reached the real detached-daemon spawn.

    Inherits ``BaseException``, not ``Exception``, and that is the whole
    point. :func:`tigerharness.autodrive.cli.ensure_running` swallows every
    ``Exception`` on purpose -- a queued task must not be lost just because
    the daemon failed to start -- so a guard derived from ``Exception``
    would be caught, logged at WARNING, and vanish into the noise. That is
    precisely how the leak stayed invisible for as long as it did. A
    ``BaseException`` sails through the handler and fails the test loudly.
    """


@pytest.fixture(autouse=True)
def block_real_daemon_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Autouse: make it *impossible* for the suite to spawn a real daemon.

    The env scrub above removes the known trigger. This removes the
    capability, which is the part that actually holds: env hygiene is one
    forgotten variable away from failing again, and the failure mode is not
    a red test -- it is a fleet of invisible background processes burning
    the subscription until someone happens to run ``ps``.

    Tests that need to observe spawn behaviour still pass ``spawn=`` into
    :func:`~tigerharness.autodrive.cli.cmd_start` explicitly; that seam is
    untouched. Only the *implicit* default is bolted shut -- and only since
    ``cmd_start`` began resolving that default at call time, because bound
    at ``def`` time it could not be patched from out here at all.
    """
    def _refuse(*args: object, **kwargs: object) -> int:
        raise RealDaemonSpawnBlocked(
            "the test suite tried to spawn a real autodrive daemon. It "
            "would outlive pytest and fire real drives forever. Pass an "
            "explicit spawn= into cmd_start, or check whether "
            "TIGERHARNESS_AUTODRIVE_AUTOSTART leaked into os.environ."
        )

    monkeypatch.setattr(
        "tigerharness.autodrive.cli.spawn_loop_process", _refuse
    )


def scrub_env(
    monkeypatch: pytest.MonkeyPatch, names=SCRUBBED_ENV_VARS
) -> None:
    """Unset *names* from the environment via *monkeypatch* (no-op if absent)."""
    for var in names:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def scrub_bridge_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Autouse: scrub the Slack + autodrive env families before every test
    (a no-op in a clean env)."""
    scrub_env(monkeypatch)

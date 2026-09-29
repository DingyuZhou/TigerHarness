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
a no-op in a clean env.

The list below is no longer kept in sync by hand.
``tests/test_env_hygiene.py`` reads ``src/`` for every env-var-shaped
literal and fails, naming the variable and its source location, unless it
appears in a ``SCRUBBED_ENV_VARS`` family or in one of the exemption
registers further down with a reason. That guard exists because the hole
this docstring used to ask a contributor to remember went unnoticed for
weeks (PR #109): one variable was missing, the suite was red on one host
and green in CI, and nothing pointed at the cause.

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
#: ``TIGERHARNESS_SLACK_CHANNEL`` joins them because it is THREAD_TS's
#: turn-scoped twin: ``autodrive/cli.py``'s ``TURN_SCOPED_ENV_VARS`` strips
#: exactly those two at the daemon spawn boundary, and ``journal/cli.py``
#: reads it from ``os.environ`` to decide where a release notice posts.
BRIDGE_ENV_VARS = (
    "TIGERHARNESS_SLACK_THREAD_TS",
    "TIGERHARNESS_SLACK_CHANNEL",
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

#: The journal rail knob, scrubbed for the same reason as
#: ``TIGERHARNESS_SLACK_THREAD_TS`` and inseparable from it: the two are the
#: halves of one gate (``journal/cli.py:1125-1157``). THREAD_TS decides whether
#: the gate fires at all; this knob (ADR 0013) decides which way it answers.
#: Scrubbing only the first protects the ~23 tests that never set it, and
#: leaves exposed exactly the tests that set it DELIBERATELY to assert the
#: default-off rail -- which is the whole of ``TestClaimRailGuard``. A team
#: that adopted ADR 0013 puts the knob in ``configs/.env``, from where
#: ``Settings.get`` reads it after ``os.environ`` and its autodrive daemon
#: exports it into every session it spawns, including one running this suite:
#: ``claim`` then returns 0 where those tests expect 1, and the suite is red on
#: that host and green in CI. Kept its own family because it is journal-rail,
#: not Slack-family, and the grouping should say so.
JOURNAL_RAIL_ENV_VARS = (
    "TIGERHARNESS_JOURNAL_SLACK_DRIVES",
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

#: The TLS trust-store overrides. ``SSL_CERT_FILE`` is a genuine ``src/``
#: reader (rung 1 of ``notify._ssl_context``) and the team ``.env`` sets it,
#: so an unscrubbed dev shell silently green-lights the rung-1 tests for the
#: wrong reason. ``SSL_CERT_DIR`` is read *below* us, by OpenSSL inside
#: ``create_default_context()``; it is scrubbed so the rung tests start from
#: a known capath rather than the dev shell's.
#:
#: Neither variable makes a default context trust nothing, and no test may
#: assume otherwise. ``SSL_CERT_DIR`` feeds the hash-dir lookup, consulted
#: lazily at verification time and never counted by ``cert_store_stats``
#: (a correctly hashed, populated capath also reports ``x509 == 0``), while
#: *unsetting* ``SSL_CERT_FILE`` is exactly what makes OpenSSL fall back to
#: the interpreter's compiled-in bundle. What a default context trusts is a
#: property of the Python build, not of this scrub.
TLS_ENV_VARS = (
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)

#: Overrides that relocate a **real on-disk root**. The consequence class is
#: not a flipped assertion, it is a test reading or writing outside its
#: ``tmp_path``, and two of these are ambient in the very sessions that run
#: this suite:
#:
#: ``TIGERHARNESS_JOURNAL_DIR`` is exported into *every* session the autodrive
#: daemon spawns (``autodrive/cli.py``'s ``daemon_env`` sets it
#: unconditionally, to pin the daemon's own journal). It is the first rung of
#: ``journal/paths.py:default_journal_root`` and it is what
#: ``journal/cli.py:_refuse_non_team_scheduling`` consults to decide whether to
#: refuse an out-of-team schedule -- so ambient, it silently converts "refuses
#: with exit 2" into "schedules into the live team journal". The suite used to
#: defend that one variable with ten per-test ``delenv`` calls across six
#: files; this scrubs it once, and those calls become harmless no-ops.
#:
#: ``XDG_RUNTIME_DIR`` is ambient on any desktop/systemd host and gates
#: ``autodrive/cli.py:cgroup_scope_prefix``, which returns ``[]`` without it.
#: A test calling that with ``env=None`` passes for the host's reason, not the
#: code's. ``XDG_STATE_HOME`` is the second rung of the same journal resolver
#: (and of ``slack_bridge/persistence.py``); ``TIGERHARNESS_TEAMS_DIR``
#: redirects ``journal/scaffold.py``'s persona preflight;
#: ``TIGER_MEMORY_CONFIG`` makes ``tiger_memory/config.py:load_config()``
#: load a real config file where the tests expect it to raise.
PATH_ROOT_ENV_VARS = (
    "TIGERHARNESS_JOURNAL_DIR",
    "TIGERHARNESS_TEAMS_DIR",
    "TIGER_MEMORY_CONFIG",
    "XDG_RUNTIME_DIR",
    "XDG_STATE_HOME",
)

#: Bridge feature knobs read through a ``from_env(env=None)`` classmethod
#: that binds ``e = os.environ`` when no dict is passed -- the indirection
#: that hides a variable from any grep for ``os.environ.get``.
#:
#: The watchdog and catch-up families are the sharp ones: ``_env_flag``
#: gives both a default of **True**, so unlike every other knob here these
#: are already on, and an ambient value can only turn them *off*. A dev
#: shell that exported ``TIGERHARNESS_SLACK_WATCHDOG=0`` to quiet a local
#: bridge would make the watchdog-enabled tests assert against a disabled
#: watchdog.
#:
#: The idle-compact family is the ``AUTOSTART`` shape again, one step
#: removed: ``TIGERHARNESS_IDLE_COMPACT`` gates the other three, so ambient
#: ``IDLE_COMPACT=1`` plus ``IDLE_COMPACT_JOURNAL=<a real journal>`` hands
#: a test an enabled config pointed at live work.
BRIDGE_FEATURE_ENV_VARS = (
    "TIGERHARNESS_IDLE_COMPACT",
    "TIGERHARNESS_IDLE_COMPACT_JOURNAL",
    "TIGERHARNESS_IDLE_COMPACT_THRESHOLD",
    "TIGERHARNESS_IDLE_COMPACT_WINDOW",
    "TIGERHARNESS_SLACK_CATCHUP",
    "TIGERHARNESS_SLACK_CATCHUP_MAX_AGE_S",
    "TIGERHARNESS_SLACK_CATCHUP_MAX_MESSAGES",
    "TIGERHARNESS_SLACK_WATCHDOG",
    "TIGERHARNESS_SLACK_WATCHDOG_POLL_S",
    "TIGERHARNESS_SLACK_WATCHDOG_STALE_S",
)

#: Numeric/level knobs whose ambient value changes observable behaviour
#: without relocating anything. ``TIGERHARNESS_JOURNAL_STUCK_TIMEOUT`` is
#: the sweep's busy-vs-crashed boundary (``journal/sweep.py``), i.e. the
#: classification a drive acts on; ``TIGERHARNESS_LOG_LEVEL`` reconfigures
#: the root logger, which ``caplog`` assertions sit on top of.
TUNING_ENV_VARS = (
    "TIGERHARNESS_JOURNAL_STUCK_TIMEOUT",
    "TIGERHARNESS_LOG_LEVEL",
)

#: Everything the autouse fixture unsets.
SCRUBBED_ENV_VARS = (
    BRIDGE_ENV_VARS + JOURNAL_RAIL_ENV_VARS + AUTODRIVE_ENV_VARS + TLS_ENV_VARS
    + PATH_ROOT_ENV_VARS + BRIDGE_FEATURE_ENV_VARS + TUNING_ENV_VARS
)

#: ---------------------------------------------------------------------------
#: The two exemption registers below are what make
#: ``tests/test_env_hygiene.py`` an enforced invariant rather than a bigger
#: list. That guard reads the package source for every env-var-shaped literal
#: and requires each one to appear either in ``SCRUBBED_ENV_VARS`` or here.
#: Scrubbing is not the goal; the goal is that no variable is present by
#: *oversight*. An entry here with a reason is a complete answer -- an entry
#: missing from both is the bug.
#: ---------------------------------------------------------------------------

#: Env var names the package contains but deliberately does **not** scrub,
#: because no code path reads them from ``os.environ``. Scrubbing them would
#: be inert padding that implies a protection that is not there.
EXEMPT_ENV_VARS = {
    # Written, never read. ``init.py`` emits it into the team's generated
    # ``.claude/settings.json`` so the *agent harness* injects it into
    # persona sessions; no ``src/`` path reads it back. Ambient on this
    # team's box and harmless there for exactly that reason.
    "TIGERHARNESS_PERSONAS_CONFIG":
        "written into generated settings.json, never read by src/",
    # Read only out of a lane's ``.env`` file dict in ``slack_bridge/multi.py``
    # (``env_vars.get``), never out of ``os.environ``. Ambient on this box;
    # still unreachable, because the lane loader takes the file.
    "TIGER_MEMORY_CLI":
        "read from a lane .env dict in multi.py, never from os.environ",
    # Not a read at all: the default of ``normalize_tiger_memory_trigger``'s
    # ``where=`` parameter, i.e. the label a config error quotes. The mode
    # itself arrives as the YAML key ``tiger_memory_trigger``.
    "TIGER_MEMORY_TRIGGER":
        "diagnostic label in a where= default, not a read",
}

#: Literals that match the env-var *shape* but are not environment variables.
#: Identifiers re-exported through ``__all__`` are excluded structurally by
#: :func:`tests.env_hygiene.env_name_literals`; these are the rest, each a
#: string used as data.
NOT_ENV_VAR_LITERALS = {
    "DATA_THROUGH": "doctor table column header (tiger_memory/inspect_tools.py)",
    "DONE_AT": "doctor table column header (tiger_memory/inspect_tools.py)",
    "MUST_REMEMBER": "doctor table column header (tiger_memory/inspect_tools.py)",
    "PARSE_ERROR": "Literal discriminator on a trailer parse result",
    "POST_WRITE": "schedule.py CAS hook phase label",
}

#: Scrubbed names that do **not** appear in the package source, kept on
#: purpose. Asserted by the guard's reverse direction so a genuinely dead
#: entry cannot accumulate unnoticed behind this one.
SCRUBBED_NOT_IN_SOURCE = {
    # Read *below* us, by OpenSSL inside ``create_default_context()``, so it
    # never appears as a literal here. Scrubbed so the ``notify._ssl_context``
    # rung tests start from a known capath rather than the dev shell's. See
    # ``TLS_ENV_VARS`` for why neither TLS var makes a context trust nothing.
    "SSL_CERT_DIR": "read by OpenSSL beneath us, never named in src/",
}


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

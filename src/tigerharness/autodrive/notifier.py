"""Daemon-level notifications for ``tigerharness autodrive``.

The autodrive daemon posts a **heartbeat on every fire** and **threads a
real drive status + summary** under it once that fire's drive finishes. This
module is the thin, vendor-neutral seam that does the posting, kept separate
from the loop so the loop stays unit-testable with a fake notifier.

Three load-bearing rules (read before extending):

1. **Model-free.** A heartbeat is a plain Slack HTTP POST, never a spawned
   agent. The daemon body is a plain Python process; notifications must not
   cost model tokens.
2. **Never break a drive.** Every method swallows its own errors and logs.
   A Slack outage, a bad channel id, or missing creds must degrade to "no
   notification", never propagate into the loop or take the daemon down.
3. **Mutable + degradable.** ``--notify none`` (or absent creds) yields a
   :class:`NullNotifier` whose methods are no-ops. When muted, the operator's
   health window is the pull-based ``autodrive status``, not this push path.
"""

from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger(__name__)


class Notifier(Protocol):
    """The seam the loop talks to. Implementations must never raise."""

    def heartbeat(self, text: str) -> str | None:
        """Post a fire heartbeat (the parent message). Return a thread handle
        (Slack ``ts``) to thread the completion under, or ``None`` if the post
        failed or the notifier is muted."""

    def update(self, thread: str | None, text: str) -> None:
        """Post a completion/error update. ``thread`` is the handle returned by
        :meth:`heartbeat` for the same fire (``None`` if that heartbeat failed
        or was muted -- a Slack notifier then posts un-threaded so the update is
        not lost)."""


class NullNotifier:
    """No-op notifier: muted (``--notify none``) or no Slack creds."""

    def heartbeat(self, text: str) -> str | None:
        return None

    def update(self, thread: str | None, text: str) -> None:
        return None


class SlackChannelNotifier:
    """Posts heartbeats + threaded updates to one Slack channel.

    Wraps the :mod:`tigerharness.slack_bridge.notify` ``SlackNotifier`` (the
    same outbound path the ``slack-notify`` skill uses). ``channel`` is a Slack
    channel id; ``None`` falls back to the operator DM (the wrapped notifier's
    default target). Every call is wrapped so a transport error logs and
    returns harmlessly.
    """

    def __init__(self, backend, channel: str | None) -> None:
        self._backend = backend
        self._channel = channel

    def heartbeat(self, text: str) -> str | None:
        try:
            return self._backend.post_text(text, channel=self._channel)
        except Exception as exc:  # pragma: no cover - defensive belt
            log.warning("autodrive heartbeat post failed: %r", exc)
            return None

    def update(self, thread: str | None, text: str) -> None:
        try:
            self._backend.dm_text(
                text, channel=self._channel, thread_ts=thread
            )
        except Exception as exc:  # pragma: no cover - defensive belt
            log.warning("autodrive update post failed: %r", exc)


def build_notifier(notify: str, channel: str | None) -> Notifier:
    """Resolve the configured notifier for a run.

    ``notify == "slack"`` with loadable creds -> a channel notifier; muted
    (``"none"``) or unloadable creds -> :class:`NullNotifier`. Never raises:
    a notifier that cannot be built degrades to silence, not a crash.
    """
    if notify != "slack":
        return NullNotifier()
    try:
        from ..slack_bridge.notify import SlackNotifier

        backend = SlackNotifier.try_load()
    except Exception as exc:  # pragma: no cover - defensive belt
        log.warning("autodrive: could not load Slack notifier: %r", exc)
        backend = None
    if backend is None:
        log.warning(
            "autodrive: notify=slack but Slack creds unavailable; "
            "running without notifications (use `autodrive status` to check "
            "health)."
        )
        return NullNotifier()
    return SlackChannelNotifier(backend, channel)

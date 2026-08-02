"""Thread-history fetch for untracked Slack threads (join context).

Notification DMs (``tigerharness slack-bridge notify`` / the
slack-notify skill) post via raw ``chat.postMessage`` and never
register their thread with the bridge's ``ThreadStore``. When the user
later replies inside such a thread, the bridge sees an unknown
``thread_ts``, treats it as a brand-new conversation, and -- before
this module existed -- opened a fresh session whose first prompt was
only the reply text: the persona could not see the very message the
user was replying to.

This module closes that gap on the *read* side. On joining an
untracked existing thread, the bridge fetches the thread's messages
via Slack's ``conversations.replies`` API and injects a bounded
transcript into the session's first prompt (and into the persona
router's input, so a reply to "[Anzai]: task complete ..." routes to
Anzai). Fetch-on-join also heals other blind spots -- a lost
threads.json, threads that predate the bridge -- which is why the fix
lives here rather than on the notify write path (and why notify never
touches threads.json: that store stays single-writer per process
family).

Design mirrors ``downloader.py``: a ``runtime_checkable`` Protocol for
injectability in tests, an aiohttp implementation using the bot token
(no new dependency), and fail-soft behavior everywhere -- a history
fetch must never take down a dispatch.

Required Slack scope: the matching ``*:history`` scope for the
conversation type (``im:history`` for DMs -- already required for the
bridge to receive DM message events, so no new grant in practice).
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

import aiohttp


log = logging.getLogger("tigerharness.slack_bridge.history")


_SLACK_REPLIES_URL = "https://slack.com/api/conversations.replies"

#: Single-page fetch size. ``conversations.replies`` returns oldest
#: first, so one page always includes the thread root; we deliberately
#: do not follow cursors -- the transcript is bounded anyway.
_FETCH_LIMIT = 100

#: Transcript bounds: the root message is always kept; beyond
#: ``_MAX_MESSAGES`` the oldest non-root messages are dropped first
#: (the root explains what the thread is about; the recent tail
#: carries the live context).
_MAX_MESSAGES = 30
_MAX_CHARS = 8000
_PER_MESSAGE_CHARS = 2000


@runtime_checkable
class ThreadHistoryFetcher(Protocol):
    """Inject this into the bridge for testability."""

    async def fetch(
        self, channel: str, thread_ts: str
    ) -> list[dict[str, Any]] | None:
        """Return the thread's messages (oldest first, root included),
        or ``None`` on any failure."""
        ...


class SlackThreadHistoryFetcher:
    """Real fetcher -- ``aiohttp`` + bot token against
    ``conversations.replies``."""

    def __init__(self, bot_token: str, *, timeout_s: int = 30) -> None:
        self._token = bot_token
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)

    async def fetch(
        self, channel: str, thread_ts: str
    ) -> list[dict[str, Any]] | None:
        params = {
            "channel": channel,
            "ts": thread_ts,
            "limit": str(_FETCH_LIMIT),
        }
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(
                    _SLACK_REPLIES_URL, params=params, headers=headers
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
        except Exception as exc:  # noqa: BLE001 - fail-soft by contract
            log.warning(
                "conversations.replies fetch failed: channel=%s ts=%s err=%r",
                channel, thread_ts, exc,
            )
            return None
        if not isinstance(data, dict) or not data.get("ok"):
            error = data.get("error") if isinstance(data, dict) else "not-a-dict"
            log.warning(
                "conversations.replies returned not-ok: channel=%s ts=%s "
                "error=%s (missing a *:history scope?)",
                channel, thread_ts, error,
            )
            return None
        messages = data.get("messages")
        if not isinstance(messages, list):
            log.warning(
                "conversations.replies missing messages list: channel=%s ts=%s",
                channel, thread_ts,
            )
            return None
        return messages


# ---------------------------------------------------------------------------
# Transcript building
# ---------------------------------------------------------------------------

def build_transcript(
    messages: list[dict[str, Any]],
    *,
    exclude_ts: str | None,
    max_messages: int = _MAX_MESSAGES,
    max_chars: int = _MAX_CHARS,
) -> str | None:
    """Render a bounded, oldest-first transcript of *messages*.

    *exclude_ts* is the triggering reply's own ``ts`` -- it is already
    the dispatch prompt, so it is dropped from the transcript. Returns
    ``None`` when nothing usable remains (e.g. the thread root was
    deleted).

    Bounding: the root message is always kept; when over budget the
    oldest non-root messages are dropped and a gap marker records how
    many. Each message body is individually capped so one pathological
    message cannot eat the whole character budget.
    """
    entries = [
        m for m in messages
        if isinstance(m, dict)
        and m.get("ts") != exclude_ts
        and ((m.get("text") or "").strip() or m.get("files"))
    ]
    if not entries:
        return None

    omitted = 0
    if len(entries) > max_messages:
        omitted = len(entries) - max_messages
        entries = [entries[0]] + entries[-(max_messages - 1):]

    lines = [_format_line(m) for m in entries]

    # Character budget: drop the oldest non-root line until we fit,
    # always keeping at least the root and the newest message.
    while sum(len(ln) + 2 for ln in lines) > max_chars and len(lines) > 2:
        del lines[1]
        omitted += 1

    if omitted:
        lines.insert(1, f"[... {omitted} earlier message(s) omitted ...]")
    return "\n\n".join(lines)


def _format_line(m: dict[str, Any]) -> str:
    """One message -> ``sender: text`` (+ attachment note)."""
    text = (m.get("text") or "").strip()
    if len(text) > _PER_MESSAGE_CHARS:
        text = text[:_PER_MESSAGE_CHARS] + " [... truncated]"
    if m.get("bot_id"):
        sender = m.get("username") or "bot"
    elif m.get("user"):
        sender = f"<@{m['user']}>"
    else:
        sender = "unknown"
    files = m.get("files") or []
    suffix = f" [+{len(files)} attachment(s), not downloaded]" if files else ""
    if not text:
        text = "(no text)"
    return f"{sender}: {text}{suffix}"


# ---------------------------------------------------------------------------
# Prompt blocks
# ---------------------------------------------------------------------------

#: Injected when the bridge knows it is joining an existing thread but
#: could not retrieve the earlier messages (API error, missing scope,
#: or nothing usable). Honest fallback: the persona should not pretend
#: to know what came before.
CONTEXT_UNAVAILABLE_NOTE = (
    "[bridge-context] You are joining an existing Slack thread "
    "mid-conversation, but the bridge could not retrieve any earlier "
    "messages (Slack API error, a missing *:history scope, or none "
    "available). If the user's message refers to earlier context you "
    "cannot see, say so and ask them to restate it."
)


def format_context_block(transcript: str) -> str:
    """Wrap a transcript for injection ahead of the first prompt."""
    return (
        "[bridge-context] You are joining an existing Slack thread that "
        "the bridge was not tracking (it likely started with a "
        "notification DM posted outside the bridge, e.g. a work-session "
        "summary). Earlier messages in this thread, oldest first:\n"
        "--- begin thread history ---\n"
        f"{transcript}\n"
        "--- end thread history ---\n"
        "The user's newest message follows; answer it in the context of "
        "this history."
    )

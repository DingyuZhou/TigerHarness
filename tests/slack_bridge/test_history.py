"""Tests for untracked-thread join context (history.py + bridge wiring).

A reply to a notification DM (posted via ``chat.postMessage``, never
registered in ThreadStore) used to open a fresh session that saw only
the reply text. These tests pin the fix: the bridge fetches the
thread's history, injects a bounded transcript into the first prompt,
and feeds it to the persona router.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tigerharness.agent_sdk import AgentConfig
from tigerharness.slack_bridge.history import (
    CONTEXT_UNAVAILABLE_NOTE,
    SlackThreadHistoryFetcher,
    ThreadHistoryFetcher,
    build_transcript,
    format_context_block,
)
from tigerharness.slack_bridge.persistence import ThreadStore


# ---------------------------------------------------------------------------
# SlackThreadHistoryFetcher (aiohttp mocked, mirrors test_downloader.py)
# ---------------------------------------------------------------------------

def _mock_http(json_data=None, *, raise_on_status=None, get_side_effect=None):
    """Build a mocked aiohttp.ClientSession context-manager stack."""
    mock_resp = AsyncMock()
    if raise_on_status is not None:
        mock_resp.raise_for_status = MagicMock(side_effect=raise_on_status)
    else:
        mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value=json_data)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    if get_side_effect is not None:
        mock_session.get = MagicMock(side_effect=get_side_effect)
    else:
        mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


class TestSlackThreadHistoryFetcher:
    def test_is_protocol_instance(self):
        assert isinstance(
            SlackThreadHistoryFetcher("xoxb-test"), ThreadHistoryFetcher
        )

    @pytest.mark.asyncio
    async def test_fetch_success(self):
        fetcher = SlackThreadHistoryFetcher("xoxb-test")
        messages = [{"ts": "1.0", "text": "root"}, {"ts": "1.1", "text": "re"}]
        session = _mock_http({"ok": True, "messages": messages})
        with patch(
            "tigerharness.slack_bridge.history.aiohttp.ClientSession",
            return_value=session,
        ):
            result = await fetcher.fetch("D0CHAN", "1.0")
        assert result == messages
        # Bearer token + channel/ts params reached the API call.
        _, kwargs = session.get.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer xoxb-test"
        assert kwargs["params"]["channel"] == "D0CHAN"
        assert kwargs["params"]["ts"] == "1.0"

    @pytest.mark.asyncio
    async def test_fetch_api_not_ok(self):
        """Slack replies ok=false (e.g. missing_scope) -> None."""
        fetcher = SlackThreadHistoryFetcher("xoxb-test")
        session = _mock_http({"ok": False, "error": "missing_scope"})
        with patch(
            "tigerharness.slack_bridge.history.aiohttp.ClientSession",
            return_value=session,
        ):
            assert await fetcher.fetch("D0CHAN", "1.0") is None

    @pytest.mark.asyncio
    async def test_fetch_non_dict_payload(self):
        """A non-dict JSON body -> None (defensive parse)."""
        fetcher = SlackThreadHistoryFetcher("xoxb-test")
        session = _mock_http(["not", "a", "dict"])
        with patch(
            "tigerharness.slack_bridge.history.aiohttp.ClientSession",
            return_value=session,
        ):
            assert await fetcher.fetch("D0CHAN", "1.0") is None

    @pytest.mark.asyncio
    async def test_fetch_messages_not_a_list(self):
        fetcher = SlackThreadHistoryFetcher("xoxb-test")
        session = _mock_http({"ok": True, "messages": "oops"})
        with patch(
            "tigerharness.slack_bridge.history.aiohttp.ClientSession",
            return_value=session,
        ):
            assert await fetcher.fetch("D0CHAN", "1.0") is None

    @pytest.mark.asyncio
    async def test_fetch_http_error(self):
        fetcher = SlackThreadHistoryFetcher("xoxb-test")
        session = _mock_http({}, raise_on_status=RuntimeError("HTTP 500"))
        with patch(
            "tigerharness.slack_bridge.history.aiohttp.ClientSession",
            return_value=session,
        ):
            assert await fetcher.fetch("D0CHAN", "1.0") is None

    @pytest.mark.asyncio
    async def test_fetch_network_error(self):
        fetcher = SlackThreadHistoryFetcher("xoxb-test")
        session = _mock_http(None, get_side_effect=OSError("conn refused"))
        with patch(
            "tigerharness.slack_bridge.history.aiohttp.ClientSession",
            return_value=session,
        ):
            assert await fetcher.fetch("D0CHAN", "1.0") is None


# ---------------------------------------------------------------------------
# build_transcript
# ---------------------------------------------------------------------------

class TestBuildTranscript:
    def test_excludes_triggering_reply(self):
        msgs = [
            {"ts": "1.0", "bot_id": "B1", "username": "Shohoku",
             "text": "[Anzai]: Task complete."},
            {"ts": "1.1", "user": "U0CEO", "text": "Great, ship it!"},
        ]
        out = build_transcript(msgs, exclude_ts="1.1")
        assert out is not None
        assert "Task complete." in out
        assert "Great, ship it!" not in out

    def test_only_reply_present_returns_none(self):
        """Root deleted: nothing usable remains -> None."""
        msgs = [{"ts": "1.1", "user": "U0CEO", "text": "hello?"}]
        assert build_transcript(msgs, exclude_ts="1.1") is None

    def test_skips_non_dict_and_empty_messages(self):
        msgs = [
            "not-a-dict",
            {"ts": "1.0", "user": "U1", "text": "   "},  # whitespace only
            {"ts": "1.2", "user": "U1", "text": "real content"},
        ]
        out = build_transcript(msgs, exclude_ts="9.9")
        assert out == "<@U1>: real content"

    def test_sender_labels(self):
        msgs = [
            {"ts": "1.0", "bot_id": "B1", "username": "Shohoku", "text": "a"},
            {"ts": "1.1", "bot_id": "B1", "text": "b"},  # bot, no username
            {"ts": "1.2", "user": "U0CEO", "text": "c"},
            {"ts": "1.3", "text": "d"},  # neither bot nor user
        ]
        out = build_transcript(msgs, exclude_ts="9.9")
        assert "Shohoku: a" in out
        assert "bot: b" in out
        assert "<@U0CEO>: c" in out
        assert "unknown: d" in out

    def test_attachment_note_and_no_text(self):
        msgs = [
            {"ts": "1.0", "user": "U1", "text": "root"},
            {"ts": "1.1", "user": "U1", "text": "",
             "files": [{"id": "F1"}, {"id": "F2"}]},
        ]
        out = build_transcript(msgs, exclude_ts="9.9")
        assert "(no text)" in out
        assert "[+2 attachment(s), not downloaded]" in out

    def test_per_message_truncation(self):
        big = "x" * 3000
        msgs = [{"ts": "1.0", "user": "U1", "text": big}]
        out = build_transcript(msgs, exclude_ts="9.9")
        assert "x" * 2000 in out
        assert "x" * 2001 not in out
        assert "[... truncated]" in out

    def test_message_count_cap_keeps_root_and_tail(self):
        msgs = [{"ts": "0.0", "user": "U1", "text": "ROOT"}] + [
            {"ts": f"1.{i}", "user": "U1", "text": f"msg-{i}"}
            for i in range(40)
        ]
        out = build_transcript(msgs, exclude_ts="9.9", max_messages=10)
        # Root always survives; newest tail survives; a gap marker
        # records the drop count (41 entries -> root + last 9).
        assert out.startswith("ROOT") or "ROOT" in out.split("\n\n")[0]
        assert "msg-39" in out
        assert "msg-5" not in out
        assert "[... 31 earlier message(s) omitted ...]" in out

    def test_char_budget_drops_oldest_non_root(self):
        msgs = [{"ts": "0.0", "user": "U1", "text": "ROOT " + "r" * 500}] + [
            {"ts": f"1.{i}", "user": "U1", "text": f"m{i}-" + "y" * 900}
            for i in range(10)
        ]
        out = build_transcript(msgs, exclude_ts="9.9", max_chars=4000)
        assert "ROOT" in out       # root always kept
        assert "m9-" in out        # newest always kept
        assert "m0-" not in out    # oldest non-root dropped first
        assert "earlier message(s) omitted" in out
        assert len(out) < 5000

    def test_char_budget_never_drops_below_root_plus_newest(self):
        msgs = [
            {"ts": "0.0", "user": "U1", "text": "R" * 300},
            {"ts": "1.0", "user": "U1", "text": "N" * 300},
        ]
        out = build_transcript(msgs, exclude_ts="9.9", max_chars=10)
        # Budget is unsatisfiable, but the loop must stop at 2 lines.
        assert "R" * 300 in out
        assert "N" * 300 in out

    def test_empty_input_returns_none(self):
        assert build_transcript([], exclude_ts=None) is None


# ---------------------------------------------------------------------------
# Prompt blocks
# ---------------------------------------------------------------------------

class TestPromptBlocks:
    def test_format_context_block_wraps_transcript(self):
        block = format_context_block("A: hello\n\nB: hi")
        assert "--- begin thread history ---" in block
        assert "A: hello" in block
        assert "--- end thread history ---" in block
        assert "[bridge-context]" in block

    def test_unavailable_note_is_honest(self):
        assert "could not retrieve" in CONTEXT_UNAVAILABLE_NOTE
        assert "[bridge-context]" in CONTEXT_UNAVAILABLE_NOTE


# ---------------------------------------------------------------------------
# Bridge wiring: fetch-on-join in _dispatch
# ---------------------------------------------------------------------------

@dataclass
class _Sess:
    id: str = "sess-join"

    async def close(self):
        return None


class _Res:
    def __init__(self, text="ok"):
        self.final_output = text
        self.cost_usd = 0.001


def _make_bridge(tmp_path: Path, fetcher):
    from tigerharness.slack_bridge.bridge import (
        PersonaSlot, SlackBridge, TeamBridgeContext,
    )

    backend = AsyncMock()
    backend.open_session = AsyncMock(return_value=_Sess())
    personas = {
        n: PersonaSlot(name=n, agent_config=AgentConfig(name=n, instructions=n))
        for n in ("Anzai", "Ayako")
    }
    team_ctx = TeamBridgeContext(
        team_name="shohoku",
        slack_app_token="xapp-x", slack_bot_token="xoxb-x",
        allowed_user_ids=frozenset({"U0CEO"}),
        agent_cwd=str(tmp_path),
        personas=personas,
        default_persona="Ayako",
    )
    store = ThreadStore(tmp_path / "threads.json")
    downloader = MagicMock()
    downloader.download = AsyncMock(return_value=None)
    b = SlackBridge(
        team_ctx=team_ctx, backend=backend, store=store,
        downloader=downloader, history_fetcher=fetcher,
    )
    return b, backend


def _reply_event(**over):
    event = {
        "channel_type": "im",
        "channel": "D0CHAN",
        "user": "U0CEO",
        "text": "Yes, please go ahead!",
        "ts": "100.5",
        "thread_ts": "100.0",
    }
    event.update(over)
    return event


_ROOT_MSGS = [
    {"ts": "100.0", "bot_id": "B1", "username": "Shohoku",
     "text": "[Anzai]: Task complete: the fix is on the branch."},
    {"ts": "100.5", "user": "U0CEO", "text": "Yes, please go ahead!"},
]


class TestJoinContextDispatch:
    def _run(self, tmp_path, fetcher, event, *, router=None, store_seed=None,
             seed_thread=None):
        """Dispatch *event* through a bridge with *fetcher* injected;
        return (captured prompts, detect_persona mock, say mock, bridge)."""
        b, _backend = _make_bridge(tmp_path, fetcher)
        if store_seed:
            b._store.set(*store_seed[:2], persona=store_seed[2])
        if seed_thread:
            from tigerharness.slack_bridge.bridge import _ThreadState
            b._threads[seed_thread] = _ThreadState(
                session=_Sess(id="sess-mem"), persona="Ayako",
            )

        prompts: list[str] = []

        async def fake_run(_backend_, _cfg, prompt, **_kw):
            prompts.append(prompt)
            return _Res()

        router_mock = router or AsyncMock(return_value=("Anzai", 0.0))
        say = AsyncMock()

        async def go():
            with patch(
                "tigerharness.slack_bridge.bridge.detect_persona",
                new=router_mock,
            ), patch(
                "tigerharness.slack_bridge.bridge.run_with_retry",
                new=fake_run,
            ):
                await b.handle_message(event, say)

        return go, prompts, router_mock, say, b

    @pytest.mark.asyncio
    async def test_untracked_reply_injects_history(self, tmp_path):
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(return_value=list(_ROOT_MSGS))
        go, prompts, router_mock, say, _b = self._run(
            tmp_path, fetcher, _reply_event()
        )
        await go()

        fetcher.fetch.assert_awaited_once_with("D0CHAN", "100.0")
        assert len(prompts) == 1
        prompt = prompts[0]
        # Transcript block present, root text visible, own reply excluded
        # from the history (it is the prompt body itself).
        assert "--- begin thread history ---" in prompt
        assert "[Anzai]: Task complete" in prompt
        assert prompt.count("Yes, please go ahead!") == 1
        # History precedes the user text (chronological reading order).
        assert prompt.index("Task complete") < prompt.index("go ahead!")
        # Router saw the transcript too.
        assert "[Anzai]: Task complete" in router_mock.call_args.kwargs["context"]
        assert say.call_args[1]["text"] == "[Anzai]: ok"

    @pytest.mark.asyncio
    async def test_tracked_in_store_skips_fetch(self, tmp_path):
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(return_value=list(_ROOT_MSGS))
        go, prompts, _r, say, _b = self._run(
            tmp_path, fetcher, _reply_event(),
            store_seed=("100.0", "sess-old", "Anzai"),
        )
        await go()
        fetcher.fetch.assert_not_awaited()
        assert "thread history" not in prompts[0]
        say.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_tracked_in_memory_skips_fetch(self, tmp_path):
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(return_value=list(_ROOT_MSGS))
        go, prompts, _r, say, _b = self._run(
            tmp_path, fetcher, _reply_event(), seed_thread="100.0",
        )
        await go()
        fetcher.fetch.assert_not_awaited()
        assert "thread history" not in prompts[0]

    @pytest.mark.asyncio
    async def test_top_level_dm_skips_fetch(self, tmp_path):
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(return_value=list(_ROOT_MSGS))
        event = _reply_event()
        del event["thread_ts"]
        go, prompts, _r, say, _b = self._run(tmp_path, fetcher, event)
        await go()
        fetcher.fetch.assert_not_awaited()
        assert "thread history" not in prompts[0]

    @pytest.mark.asyncio
    async def test_fetch_failure_injects_unavailable_note(self, tmp_path):
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(return_value=None)
        go, prompts, router_mock, _s, _b = self._run(
            tmp_path, fetcher, _reply_event()
        )
        await go()
        assert "could not retrieve" in prompts[0]
        # Router gets no context on the failure path.
        assert router_mock.call_args.kwargs["context"] is None

    @pytest.mark.asyncio
    async def test_fetcher_raising_is_contained(self, tmp_path):
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(side_effect=RuntimeError("boom"))
        go, prompts, _r, say, _b = self._run(
            tmp_path, fetcher, _reply_event()
        )
        await go()
        assert "could not retrieve" in prompts[0]
        say.assert_awaited_once()  # dispatch survived

    @pytest.mark.asyncio
    async def test_missing_channel_skips_fetch_with_note(self, tmp_path):
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(return_value=list(_ROOT_MSGS))
        event = _reply_event()
        del event["channel"]
        go, prompts, _r, _s, _b = self._run(tmp_path, fetcher, event)
        await go()
        fetcher.fetch.assert_not_awaited()
        assert "could not retrieve" in prompts[0]

    @pytest.mark.asyncio
    async def test_empty_transcript_injects_note(self, tmp_path):
        """Fetch succeeds but only the triggering reply came back (root
        deleted) -> honest unavailable note, not an empty block."""
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(return_value=[
            {"ts": "100.5", "user": "U0CEO", "text": "Yes, please go ahead!"},
        ])
        go, prompts, _r, _s, _b = self._run(
            tmp_path, fetcher, _reply_event()
        )
        await go()
        assert "could not retrieve" in prompts[0]
        assert "--- begin thread history ---" not in prompts[0]


class TestIsUntrackedThreadReply:
    """Direct predicate coverage for every early-out branch."""

    def _bridge(self, tmp_path):
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(return_value=None)
        b, _ = _make_bridge(tmp_path, fetcher)
        return b

    def test_no_thread_ts(self, tmp_path):
        b = self._bridge(tmp_path)
        assert b._is_untracked_thread_reply({"ts": "1.0"}, "1.0") is False

    def test_thread_ts_equals_ts(self, tmp_path):
        """A parent message that carries thread_ts == ts is not a
        reply-join; there is nothing earlier to fetch."""
        b = self._bridge(tmp_path)
        event = {"ts": "1.0", "thread_ts": "1.0"}
        assert b._is_untracked_thread_reply(event, "1.0") is False

    def test_in_memory_thread(self, tmp_path):
        from tigerharness.slack_bridge.bridge import _ThreadState
        b = self._bridge(tmp_path)
        b._threads["1.0"] = _ThreadState(session=_Sess(), persona="Ayako")
        event = {"ts": "1.1", "thread_ts": "1.0"}
        assert b._is_untracked_thread_reply(event, "1.0") is False

    def test_store_record(self, tmp_path):
        b = self._bridge(tmp_path)
        b._store.set("1.0", "sess-x")
        event = {"ts": "1.1", "thread_ts": "1.0"}
        assert b._is_untracked_thread_reply(event, "1.0") is False

    def test_untracked_reply(self, tmp_path):
        b = self._bridge(tmp_path)
        event = {"ts": "1.1", "thread_ts": "1.0"}
        assert b._is_untracked_thread_reply(event, "1.0") is True

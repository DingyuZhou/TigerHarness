"""Bridge tests: message routing, thread tracking, shutdown."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tigerharness.slack_bridge.bridge import (
    SlackBridge,
    _append_bridge_context,
    _is_user_dm,
    _strip_bot_mention,
    build_agent_config,
)
from tigerharness.agent_sdk import AgentConfig
from tigerharness.slack_bridge.config import BridgeConfig
from tigerharness.slack_bridge.persistence import ThreadStore


@pytest.fixture
def cfg():
    return BridgeConfig(
        slack_app_token="xapp-test",
        slack_bot_token="xoxb-test",
        allowed_user_ids=frozenset({"U0CEO"}),
        agent_cwd="/tmp",
    )


@pytest.fixture
def store(tmp_path):
    return ThreadStore(tmp_path / "threads.json")


@pytest.fixture
def fake_backend():
    @dataclass
    class FakeSession:
        id: str = "sess-001"
        async def close(self):
            pass

    @dataclass
    class FakeResult:
        final_output: str = "Hello from Sai"
        cost_usd: float = 0.005

    backend = AsyncMock()
    backend.open_session = AsyncMock(return_value=FakeSession())
    return backend, FakeResult()


class TestHelpers:
    def test_strip_bot_mention(self):
        assert _strip_bot_mention("<@U0BOT123> hello") == "hello"
        assert _strip_bot_mention("<@U0BOT123>  hello  <@W0OTHER>world") == "hello  world"
        assert _strip_bot_mention("no mention") == "no mention"

    def test_is_user_dm(self):
        assert _is_user_dm({"channel_type": "im", "user": "U0CEO"})
        assert not _is_user_dm({"channel_type": "channel"})
        assert not _is_user_dm({"channel_type": "im", "subtype": "message_changed"})
        assert not _is_user_dm({"channel_type": "im", "bot_id": "B123"})
        # file_share subtype is accepted
        assert _is_user_dm({"channel_type": "im", "subtype": "file_share"})

    def test_append_bridge_context(self):
        result = _append_bridge_context("hello", "1234.5678", "C0CHAN")
        assert "[bridge-context]" in result
        assert "slack_thread_ts: 1234.5678" in result
        assert "slack_channel: C0CHAN" in result

    def test_append_bridge_context_no_channel(self):
        result = _append_bridge_context("hello", "1234.5678", None)
        assert "slack_thread_ts: 1234.5678" in result
        assert "slack_channel" not in result


class TestBuildAgentConfig:
    def test_with_prompt_file(self, tmp_path, monkeypatch):
        prompt = tmp_path / "agent.md"
        prompt.write_text("You are a test agent.")
        cfg = BridgeConfig(
            slack_app_token="xapp-x",
            slack_bot_token="xoxb-x",
            allowed_user_ids=frozenset({"U0X"}),
            agent_cwd="/tmp",
            agent_prompt_path=str(prompt),
        )
        agent_cfg = build_agent_config(cfg)
        assert agent_cfg.instructions == "You are a test agent."

    def test_without_prompt_file(self, caplog):
        cfg = BridgeConfig(
            slack_app_token="xapp-x",
            slack_bot_token="xoxb-x",
            allowed_user_ids=frozenset({"U0X"}),
            agent_cwd="/tmp",
        )
        with caplog.at_level("WARNING", logger="tigerharness.slack_bridge"):
            agent_cfg = build_agent_config(cfg)
        assert "helpful assistant" in agent_cfg.instructions
        # Loud at startup so operators notice the persona is missing.
        assert any(
            "TIGERHARNESS_AGENT_PROMPT" in rec.message
            for rec in caplog.records
        ), [r.message for r in caplog.records]

    def test_missing_prompt_file(self, tmp_path):
        cfg = BridgeConfig(
            slack_app_token="xapp-x",
            slack_bot_token="xoxb-x",
            allowed_user_ids=frozenset({"U0X"}),
            agent_cwd="/tmp",
            agent_prompt_path=str(tmp_path / "nonexistent.md"),
        )
        with pytest.raises(FileNotFoundError):
            build_agent_config(cfg)


class TestTeamBridgeContext:
    """Multi-persona team context: ``is_multi_persona`` toggles routing
    + reply prefix behavior, ``_format_reply`` honors it."""

    def _ctx(self, *names: str) -> "TeamBridgeContext":
        from tigerharness.slack_bridge.bridge import (
            PersonaSlot, TeamBridgeContext,
        )
        personas = {
            n: PersonaSlot(
                name=n,
                agent_config=AgentConfig(name=n, instructions=f"You are {n}"),
            )
            for n in names
        }
        return TeamBridgeContext(
            team_name="t",
            slack_app_token="xapp-x",
            slack_bot_token="xoxb-x",
            allowed_user_ids=frozenset({"U0CEO"}),
            agent_cwd="/tmp",
            personas=personas,
            default_persona=names[0],
        )

    def test_single_persona_is_not_multi(self):
        ctx = self._ctx("ayako")
        assert ctx.is_multi_persona is False

    def test_two_personas_is_multi(self):
        ctx = self._ctx("ayako", "sakuragi")
        assert ctx.is_multi_persona is True


class TestFormatReply:
    """``_format_reply`` adds ``[<persona>]:`` only in multi-persona teams.
    Single-persona output is identical to the pre-PR4 bridge."""

    def test_single_persona_no_prefix(self):
        from tigerharness.slack_bridge.bridge import (
            PersonaSlot, TeamBridgeContext, _format_reply,
        )
        ctx = TeamBridgeContext(
            team_name="", slack_app_token="x", slack_bot_token="x",
            allowed_user_ids=frozenset({"U0"}), agent_cwd="/",
            personas={"a": PersonaSlot(name="a", agent_config=AgentConfig(name="a", instructions="x"))},
            default_persona="a",
        )
        assert _format_reply("hello", "a", ctx) == "hello"

    def test_multi_persona_adds_prefix(self):
        from tigerharness.slack_bridge.bridge import (
            PersonaSlot, TeamBridgeContext, _format_reply,
        )
        personas = {
            n: PersonaSlot(name=n, agent_config=AgentConfig(name=n, instructions=n))
            for n in ("ayako", "sakuragi")
        }
        ctx = TeamBridgeContext(
            team_name="shohoku", slack_app_token="x", slack_bot_token="x",
            allowed_user_ids=frozenset({"U0"}), agent_cwd="/",
            personas=personas,
            default_persona="ayako",
        )
        assert _format_reply("hi from ayako", "ayako", ctx) == "[ayako]: hi from ayako"


class TestTeamAwarenessPreamble:
    """The preamble teaches a persona about teammates so it can handle
    misroutes politely. Single-persona teams get an empty preamble."""

    def test_single_persona_no_preamble(self):
        from tigerharness.slack_bridge.bridge import _team_awareness_preamble
        assert _team_awareness_preamble("ayako", "shohoku", ["ayako"]) == ""

    def test_multi_persona_preamble_mentions_others(self):
        from tigerharness.slack_bridge.bridge import _team_awareness_preamble
        result = _team_awareness_preamble(
            "ayako", "shohoku", ["ayako", "sakuragi", "mitsui"]
        )
        assert "ayako" in result
        assert "sakuragi" in result
        assert "mitsui" in result
        # Self should appear once (as the active persona), not as a "other".
        assert "Other team members reachable" in result

    def test_empty_team_name_uses_generic_descriptor(self):
        from tigerharness.slack_bridge.bridge import _team_awareness_preamble
        result = _team_awareness_preamble(
            "ayako", "", ["ayako", "sakuragi"]
        )
        assert "your team" in result

    def test_preamble_tells_persona_not_to_self_identify(self):
        """The bridge already prefixes every reply with [<persona>]:.
        The preamble must tell the persona not to also identify in their
        text, so users don't see `[Ayako]: I'm Ayako, here's my answer...`"""
        from tigerharness.slack_bridge.bridge import _team_awareness_preamble
        result = _team_awareness_preamble(
            "ayako", "shohoku", ["ayako", "sakuragi"]
        )
        assert "automatically labels" in result or "labels every reply" in result
        # The exact prefix format is mentioned so the persona understands
        # why it doesn't need to self-identify.
        assert "[ayako]" in result.lower()


class TestBuildPersonaAgentConfig:
    def test_appends_preamble_for_multi_persona(self):
        from tigerharness.slack_bridge.bridge import build_persona_agent_config
        cfg = build_persona_agent_config(
            persona_name="ayako",
            prompt_text="You are Ayako.",
            team_name="shohoku",
            all_personas=["ayako", "sakuragi"],
        )
        assert "You are Ayako." in cfg.instructions
        assert "sakuragi" in cfg.instructions
        assert cfg.name == "agent-ayako"

    def test_no_preamble_for_single_persona(self):
        from tigerharness.slack_bridge.bridge import build_persona_agent_config
        cfg = build_persona_agent_config(
            persona_name="ayako",
            prompt_text="You are Ayako.",
            team_name="shohoku",
            all_personas=["ayako"],
        )
        # The bare prompt is preserved, no preamble appended.
        assert cfg.instructions == "You are Ayako."


class TestBuildTeamBridge:
    """``build_team_bridge`` is the multi-persona counterpart to
    ``build_bridge``. Both factories build a ThreadStore at the given
    state_path and pass the right context to SlackBridge."""

    def test_uses_explicit_state_path(self, tmp_path: Path):
        from tigerharness.slack_bridge import bridge as bridge_mod
        from tigerharness.slack_bridge.bridge import (
            PersonaSlot, TeamBridgeContext, build_team_bridge,
        )
        team_ctx = TeamBridgeContext(
            team_name="shohoku",
            slack_app_token="xapp-1", slack_bot_token="xoxb-1",
            allowed_user_ids=frozenset({"U0CEO"}), agent_cwd=str(tmp_path),
            personas={"ayako": PersonaSlot(
                name="ayako",
                agent_config=AgentConfig(name="ayako", instructions="x"),
            )},
            default_persona="ayako",
        )
        explicit = tmp_path / "shohoku" / "threads.json"
        with patch.object(bridge_mod, "ThreadStore") as ts, \
             patch.object(bridge_mod, "get_backend"):
            build_team_bridge(team_ctx, state_path=explicit)
        ts.assert_called_once_with(explicit)


class TestSlackBridgeInitErrors:
    """``SlackBridge`` requires either (cfg + agent_cfg) or team_ctx,
    plus backend and store. Missing args produce clear errors."""

    def test_missing_cfg_and_team_ctx_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="must provide"):
            SlackBridge(backend=MagicMock(), store=MagicMock())

    def test_missing_backend_raises(self, tmp_path: Path):
        cfg = BridgeConfig(
            slack_app_token="xapp-x", slack_bot_token="xoxb-x",
            allowed_user_ids=frozenset({"U0"}), agent_cwd="/",
        )
        agent_cfg = AgentConfig(name="x", instructions="x")
        with pytest.raises(ValueError, match="backend and store"):
            SlackBridge(cfg, None, agent_cfg, MagicMock())


class TestBuildBridge:
    """`build_bridge(cfg, state_path=...)` composes the bridge wiring.

    The *state_path* kwarg lets multi-lane callers pass a per-lane path
    so two bridges in one process don't fight over the same threads.json.
    When omitted (single-tenant default), falls back to
    ``default_state_path()``.
    """

    def _cfg(self, tmp_path: Path) -> BridgeConfig:
        return BridgeConfig(
            slack_app_token="xapp-x",
            slack_bot_token="xoxb-x",
            allowed_user_ids=frozenset({"U0X"}),
            agent_cwd=str(tmp_path),
        )

    def test_explicit_state_path_used(self, tmp_path: Path):
        from tigerharness.slack_bridge import bridge as bridge_mod
        explicit = tmp_path / "lane-shohoku" / "threads.json"
        with patch.object(bridge_mod, "ThreadStore") as ts, \
             patch.object(bridge_mod, "get_backend"):
            bridge_mod.build_bridge(self._cfg(tmp_path), state_path=explicit)
        # ThreadStore must be constructed with our explicit path, not
        # whatever default_state_path() would return.
        ts.assert_called_once_with(explicit)

    def test_omitted_state_path_falls_back_to_default(self, tmp_path: Path, monkeypatch):
        """Backward-compat: single-tenant callers pass no state_path
        kwarg. Must resolve via ``default_state_path()`` -- this is the
        invariant that keeps the existing systemd unit working."""
        from tigerharness.slack_bridge import bridge as bridge_mod
        sentinel = tmp_path / "from-default" / "threads.json"
        monkeypatch.setattr(
            bridge_mod, "default_state_path", lambda: sentinel
        )
        with patch.object(bridge_mod, "ThreadStore") as ts, \
             patch.object(bridge_mod, "get_backend"):
            bridge_mod.build_bridge(self._cfg(tmp_path))  # no state_path
        ts.assert_called_once_with(sentinel)


class TestTriggerTigerMemoryRebuild:
    """The rebuild trigger now takes the active persona's slot + the
    team's tiger_memory_cli (so each lane can use its own CLI binary).
    Tests construct minimal PersonaSlot fixtures rather than going
    through the whole TeamBridgeContext."""

    def _persona(self, *, memory_path: str = "") -> "PersonaSlot":
        from tigerharness.slack_bridge.bridge import PersonaSlot
        return PersonaSlot(
            name="ayako",
            agent_config=AgentConfig(name="x", instructions="x"),
            tiger_memory_config_path=memory_path,
        )

    def test_no_config_path_is_noop(self):
        from tigerharness.slack_bridge.bridge import _trigger_tiger_memory_rebuild
        # Empty memory_path -> no-op (no exception).
        _trigger_tiger_memory_rebuild(self._persona(), "", "thread-1")

    def test_cli_not_found(self):
        from tigerharness.slack_bridge.bridge import _trigger_tiger_memory_rebuild
        with patch("tigerharness.slack_bridge.bridge.shutil.which", return_value=None):
            # CLI not on PATH + no explicit override -> warning + skip.
            _trigger_tiger_memory_rebuild(
                self._persona(memory_path="/tmp/config.yaml"),
                "",
                "thread-1",
            )

    def test_explicit_cli_spawns(self):
        from tigerharness.slack_bridge.bridge import _trigger_tiger_memory_rebuild
        with patch("tigerharness.slack_bridge.bridge.subprocess.Popen") as mock_popen:
            _trigger_tiger_memory_rebuild(
                self._persona(memory_path="/tmp/config.yaml"),
                "/usr/bin/tiger-memory",
                "thread-1",
            )
        mock_popen.assert_called_once()
        cmd = mock_popen.call_args[0][0]
        assert "/usr/bin/tiger-memory" in cmd
        assert "--config" in cmd
        assert "rebuild" in cmd

    def test_spawn_failure_logged_not_raised(self):
        from tigerharness.slack_bridge.bridge import _trigger_tiger_memory_rebuild
        with patch("tigerharness.slack_bridge.bridge.subprocess.Popen", side_effect=OSError("fail")):
            _trigger_tiger_memory_rebuild(
                self._persona(memory_path="/tmp/config.yaml"),
                "/usr/bin/tiger-memory",
                "thread-1",
            )


class TestWithThreadEnv:
    """``_with_thread_env`` returns a per-turn copy carrying the thread_ts
    in ``extra["env"]`` (the claude_p backend forwards it to the
    subprocess as TIGERHARNESS_SLACK_THREAD_TS). The original config must
    stay untouched so concurrent turns and the persona's shared slot
    don't leak each other's thread."""

    def test_injects_thread_ts_without_mutating_original(self):
        from tigerharness.slack_bridge.bridge import _with_thread_env
        cfg = AgentConfig(name="x")
        out = _with_thread_env(cfg, "123.456")
        assert out.extra["env"]["TIGERHARNESS_SLACK_THREAD_TS"] == "123.456"
        assert "env" not in cfg.extra  # original untouched

    def test_preserves_existing_extra_and_env(self):
        from tigerharness.slack_bridge.bridge import _with_thread_env
        cfg = AgentConfig(
            name="x",
            extra={"permission_mode": "plan", "env": {"FOO": "bar"}},
        )
        out = _with_thread_env(cfg, "9.9")
        assert out.extra["permission_mode"] == "plan"
        assert out.extra["env"]["FOO"] == "bar"
        assert out.extra["env"]["TIGERHARNESS_SLACK_THREAD_TS"] == "9.9"
        # original's env dict is not mutated
        assert cfg.extra["env"] == {"FOO": "bar"}


class TestSlackBridge:
    @pytest.fixture
    def bridge(self, cfg, store, fake_backend):
        from tigerharness.agent_sdk import AgentConfig
        backend, fake_result = fake_backend
        agent_cfg = AgentConfig(name="test", instructions="test")
        downloader = MagicMock()
        downloader.download = AsyncMock(return_value=None)
        b = SlackBridge(cfg, backend, agent_cfg, store, downloader=downloader)
        return b, backend, fake_result

    @pytest.mark.asyncio
    async def test_drops_non_allowed_user(self, bridge):
        b, backend, _ = bridge
        say = AsyncMock()
        event = {"channel_type": "im", "user": "U0STRANGER", "text": "hi", "ts": "1.1"}
        await b.handle_message(event, say)
        say.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_drops_non_dm(self, bridge):
        b, backend, _ = bridge
        say = AsyncMock()
        event = {"channel_type": "channel", "user": "U0CEO", "text": "hi", "ts": "1.1"}
        await b.handle_message(event, say)
        say.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dispatches_valid_dm(self, bridge):
        from unittest.mock import patch
        b, backend, fake_result = bridge

        with patch("tigerharness.slack_bridge.bridge.run_with_retry", return_value=fake_result):
            say = AsyncMock()
            event = {"channel_type": "im", "user": "U0CEO", "text": "hello", "ts": "1.1"}
            await b.handle_message(event, say)

        say.assert_awaited_once()
        call_kwargs = say.call_args[1]
        assert call_kwargs["thread_ts"] == "1.1"
        assert "Hello from Sai" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_dispatch_injects_thread_ts_env(self, bridge):
        # Harness-enforced suppression: the per-turn AgentConfig handed to
        # the backend carries this thread's ts so an in-session
        # `journal claim --driver` registers it for transcript skip.
        from unittest.mock import patch
        b, backend, fake_result = bridge

        with patch(
            "tigerharness.slack_bridge.bridge.run_with_retry",
            return_value=fake_result,
        ) as m:
            say = AsyncMock()
            event = {"channel_type": "im", "user": "U0CEO",
                     "text": "hello", "ts": "1.1"}
            await b.handle_message(event, say)

        cfg_arg = m.call_args[0][1]  # second positional: the per-turn cfg
        assert cfg_arg.extra["env"]["TIGERHARNESS_SLACK_THREAD_TS"] == "1.1"

    @pytest.mark.asyncio
    async def test_shutdown_rejects_new(self, bridge):
        b, backend, _ = bridge
        b.request_shutdown()
        say = AsyncMock()
        event = {"channel_type": "im", "user": "U0CEO", "text": "hi", "ts": "1.1"}
        await b.handle_message(event, say)
        say.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_drain_when_empty(self, bridge):
        b, _, _ = bridge
        result = await b.wait_for_drain(timeout=1.0)
        assert result is True

    @pytest.mark.asyncio
    async def test_tracked_thread_reply(self, bridge):
        """A reply in a tracked thread (via store) should be dispatched."""
        from unittest.mock import patch as _patch
        b, backend, fake_result = bridge

        # Pre-seed the store with a tracked thread
        b._store.set("parent.ts", "existing-sess")

        with _patch("tigerharness.slack_bridge.bridge.run_with_retry", return_value=fake_result):
            say = AsyncMock()
            # This is a thread reply (thread_ts present), not a DM
            event = {
                "channel_type": "channel",
                "user": "U0CEO",
                "text": "follow up",
                "ts": "child.ts",
                "thread_ts": "parent.ts",
            }
            await b.handle_message(event, say)

        say.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_untracked_thread_reply_dropped(self, bridge):
        """A reply in an untracked thread should be silently dropped."""
        b, backend, _ = bridge
        say = AsyncMock()
        event = {
            "channel_type": "channel",
            "user": "U0CEO",
            "text": "random thread reply",
            "ts": "child.ts",
            "thread_ts": "unknown-parent.ts",
        }
        await b.handle_message(event, say)
        say.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mention_strips_tag(self, bridge):
        from unittest.mock import patch
        b, backend, fake_result = bridge

        with patch("tigerharness.slack_bridge.bridge.run_with_retry", return_value=fake_result):
            say = AsyncMock()
            event = {
                "user": "U0CEO",
                "text": "<@U0BOT123> what's up",
                "ts": "2.2",
                "channel": "C0CHAN",
            }
            await b.handle_mention(event, say)

        say.assert_awaited_once()


class TestReplyPrefixOnBridgeVoice:
    """Bridge-generated messages (errors, empty replies) MUST NOT carry
    the `[<persona>]:` prefix -- that would imply the persona is
    reporting the error, when it's really the bridge itself."""

    def _multi_bridge(self, tmp_path: Path):
        """Build a multi-persona SlackBridge wired up with a mock
        downloader + an empty backend. Caller supplies a custom
        ``run_with_retry`` via ``patch`` for each scenario."""
        from tigerharness.slack_bridge.bridge import (
            PersonaSlot, SlackBridge, TeamBridgeContext,
        )

        @dataclass
        class _Sess:
            id: str = "sess-test"
            async def close(self):
                return None

        backend = AsyncMock()
        backend.open_session = AsyncMock(return_value=_Sess())

        personas = {
            n: PersonaSlot(name=n, agent_config=AgentConfig(name=n, instructions=n))
            for n in ("Ayako", "Sakuragi")
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
            team_ctx=team_ctx, backend=backend, store=store, downloader=downloader,
        )
        return b

    @pytest.mark.asyncio
    async def test_persona_reply_has_prefix(self, tmp_path: Path):
        """Sanity baseline: when the persona returns text, the prefix
        DOES appear."""
        from unittest.mock import patch as _patch

        class _Res:
            def __init__(self):
                self.final_output = "Hello!"
                self.cost_usd = 0.001

        async def fake_run(*args, **kwargs):
            return _Res()

        b = self._multi_bridge(tmp_path)
        with _patch(
            "tigerharness.slack_bridge.bridge.detect_persona",
            new=AsyncMock(return_value=("Ayako", 0.0)),
        ), _patch(
            "tigerharness.slack_bridge.bridge.run_with_retry", new=fake_run,
        ):
            say = AsyncMock()
            event = {
                "channel_type": "im", "user": "U0CEO",
                "text": "Hi", "ts": "1.1",
            }
            await b.handle_message(event, say)
        say.assert_awaited_once()
        assert say.call_args[1]["text"] == "[Ayako]: Hello!"

    @pytest.mark.asyncio
    async def test_empty_output_has_no_prefix(self, tmp_path: Path):
        """Bridge voice: ``_(empty reply)_`` posted as bare text."""
        from unittest.mock import patch as _patch

        class _Res:
            def __init__(self):
                self.final_output = None
                self.cost_usd = 0.0

        async def fake_run(*args, **kwargs):
            return _Res()

        b = self._multi_bridge(tmp_path)
        with _patch(
            "tigerharness.slack_bridge.bridge.detect_persona",
            new=AsyncMock(return_value=("Ayako", 0.0)),
        ), _patch(
            "tigerharness.slack_bridge.bridge.run_with_retry", new=fake_run,
        ):
            say = AsyncMock()
            event = {
                "channel_type": "im", "user": "U0CEO",
                "text": "Hi", "ts": "1.1",
            }
            await b.handle_message(event, say)
        say.assert_awaited_once()
        text = say.call_args[1]["text"]
        # Persona prefix must NOT appear -- the bridge is reporting empty.
        assert "[Ayako]" not in text
        assert "empty reply" in text

    @pytest.mark.asyncio
    async def test_backend_error_has_no_prefix(self, tmp_path: Path):
        """Bridge voice: backend errors posted as bare text. Otherwise
        users would think the persona itself reported the error."""
        from unittest.mock import patch as _patch

        async def fake_run(*args, **kwargs):
            raise RuntimeError("kaboom")

        b = self._multi_bridge(tmp_path)
        with _patch(
            "tigerharness.slack_bridge.bridge.detect_persona",
            new=AsyncMock(return_value=("Ayako", 0.0)),
        ), _patch(
            "tigerharness.slack_bridge.bridge.run_with_retry", new=fake_run,
        ):
            say = AsyncMock()
            event = {
                "channel_type": "im", "user": "U0CEO",
                "text": "Hi", "ts": "1.1",
            }
            await b.handle_message(event, say)
        say.assert_awaited_once()
        text = say.call_args[1]["text"]
        assert "[Ayako]" not in text
        assert "kaboom" in text
        assert "backend error" in text


class TestDrainCoversFullDispatch:
    """``_in_flight`` must cover the router LLM call and session-open,
    not just the agent run. Otherwise ``wait_for_drain`` can report
    True with init work still in flight, orphaning subprocesses on
    SIGTERM."""

    def _multi_bridge(self, tmp_path: Path):
        from tigerharness.slack_bridge.bridge import (
            PersonaSlot, SlackBridge, TeamBridgeContext,
        )

        @dataclass
        class _Sess:
            id: str = "sess"
            async def close(self):
                return None

        backend = AsyncMock()
        backend.open_session = AsyncMock(return_value=_Sess())
        personas = {
            n: PersonaSlot(name=n, agent_config=AgentConfig(name=n, instructions=n))
            for n in ("Ayako", "Sakuragi")
        }
        team_ctx = TeamBridgeContext(
            team_name="t", slack_app_token="xapp", slack_bot_token="xoxb",
            allowed_user_ids=frozenset({"U0CEO"}), agent_cwd=str(tmp_path),
            personas=personas, default_persona="Ayako",
        )
        store = ThreadStore(tmp_path / "threads.json")
        downloader = MagicMock()
        downloader.download = AsyncMock(return_value=None)
        return SlackBridge(
            team_ctx=team_ctx, backend=backend, store=store, downloader=downloader,
        )

    @pytest.mark.asyncio
    async def test_in_flight_bumps_before_router(self, tmp_path: Path):
        """Hold the dispatch inside the router call and verify
        ``_in_flight > 0`` while we're there."""
        from unittest.mock import patch as _patch
        b = self._multi_bridge(tmp_path)
        router_started = asyncio.Event()
        router_done = asyncio.Event()

        async def slow_router(*args, **kwargs):
            router_started.set()
            await router_done.wait()
            return "Ayako", 0.001

        async def fake_run(*args, **kwargs):
            class _R:
                final_output = "ok"
                cost_usd = 0.0
            return _R()

        with _patch(
            "tigerharness.slack_bridge.bridge.detect_persona", new=slow_router,
        ), _patch(
            "tigerharness.slack_bridge.bridge.run_with_retry", new=fake_run,
        ):
            say = AsyncMock()
            event = {
                "channel_type": "im", "user": "U0CEO",
                "text": "Hi", "ts": "1.1",
            }
            dispatch_task = asyncio.create_task(b.handle_message(event, say))
            # Wait until we're inside the router call
            await asyncio.wait_for(router_started.wait(), timeout=2.0)
            # While the router is running, the dispatch must be counted.
            assert b._in_flight == 1, (
                "router call must be inside the drain barrier "
                "(else SIGTERM leaves orphan subprocesses)"
            )
            assert not b._drained.is_set()
            # Let the router complete and the dispatch finish.
            router_done.set()
            await asyncio.wait_for(dispatch_task, timeout=2.0)
        assert b._in_flight == 0
        assert b._drained.is_set()

    @pytest.mark.asyncio
    async def test_shutdown_caught_after_router_returns(self, tmp_path: Path):
        """If shutdown is requested while the router is running, the
        dispatch must bail out before running the agent."""
        from unittest.mock import patch as _patch
        b = self._multi_bridge(tmp_path)
        router_started = asyncio.Event()

        async def slow_router(*args, **kwargs):
            router_started.set()
            # Let the dispatch wait here until we trigger shutdown.
            await asyncio.sleep(0.05)
            return "Ayako", 0.0

        run_called = False

        async def fake_run(*args, **kwargs):
            nonlocal run_called
            run_called = True
            class _R:
                final_output = "should not be reached"
                cost_usd = 0.0
            return _R()

        with _patch(
            "tigerharness.slack_bridge.bridge.detect_persona", new=slow_router,
        ), _patch(
            "tigerharness.slack_bridge.bridge.run_with_retry", new=fake_run,
        ):
            say = AsyncMock()
            event = {
                "channel_type": "im", "user": "U0CEO",
                "text": "Hi", "ts": "1.1",
            }
            dispatch_task = asyncio.create_task(b.handle_message(event, say))
            await asyncio.wait_for(router_started.wait(), timeout=2.0)
            # Request shutdown while the router is still running.
            b.request_shutdown()
            await asyncio.wait_for(dispatch_task, timeout=2.0)
        # The agent run should NOT have happened -- bridge bailed out
        # after the router returned because shutdown was set.
        assert run_called is False
        # And `say` should not have been called (no reply posted).
        say.assert_not_awaited()
        # Critical: the early-bail path must still decrement `_in_flight`
        # via the dispatch's `finally`. If it doesn't, `wait_for_drain`
        # would block forever and orchestrator shutdown would time out.
        assert b._in_flight == 0
        assert b._drained.is_set()


class TestBridgeCostTracking:
    """``cost_so_far`` accumulates router LLM + agent LLM spend."""

    def _multi_bridge(self, tmp_path: Path):
        from tigerharness.slack_bridge.bridge import (
            PersonaSlot, SlackBridge, TeamBridgeContext,
        )

        @dataclass
        class _Sess:
            id: str = "sess"
            async def close(self):
                return None

        backend = AsyncMock()
        backend.open_session = AsyncMock(return_value=_Sess())
        personas = {
            n: PersonaSlot(name=n, agent_config=AgentConfig(name=n, instructions=n))
            for n in ("Ayako", "Sakuragi")
        }
        team_ctx = TeamBridgeContext(
            team_name="t", slack_app_token="xapp", slack_bot_token="xoxb",
            allowed_user_ids=frozenset({"U0CEO"}), agent_cwd=str(tmp_path),
            personas=personas, default_persona="Ayako",
        )
        store = ThreadStore(tmp_path / "threads.json")
        downloader = MagicMock()
        downloader.download = AsyncMock(return_value=None)
        return SlackBridge(
            team_ctx=team_ctx, backend=backend, store=store, downloader=downloader,
        )

    def test_initial_cost_is_zero(self, tmp_path: Path):
        b = self._multi_bridge(tmp_path)
        assert b.cost_so_far == 0.0

    def test_record_cost_accumulates(self, tmp_path: Path):
        b = self._multi_bridge(tmp_path)
        b._record_cost(0.001)
        b._record_cost(0.002)
        assert b.cost_so_far == pytest.approx(0.003)

    def test_record_cost_tolerates_none(self, tmp_path: Path):
        """Backends that don't report cost (or transient None results)
        must not crash dispatch."""
        b = self._multi_bridge(tmp_path)
        b._record_cost(None)
        assert b.cost_so_far == 0.0

    def test_record_cost_tolerates_bad_types(self, tmp_path: Path):
        """Defensive: a backend returning a string instead of a float
        should be silently ignored, not crash the bridge."""
        b = self._multi_bridge(tmp_path)
        b._record_cost("not-a-number")  # type: ignore[arg-type]
        assert b.cost_so_far == 0.0

    @pytest.mark.asyncio
    async def test_dispatch_records_router_and_agent_cost(self, tmp_path: Path):
        """End-to-end: a full dispatch should accumulate BOTH the router
        call cost AND the agent call cost on ``cost_so_far``."""
        from unittest.mock import patch as _patch
        b = self._multi_bridge(tmp_path)

        async def fake_router(*args, **kwargs):
            return "Ayako", 0.0010

        async def fake_run(*args, **kwargs):
            class _R:
                final_output = "hello"
                cost_usd = 0.0020
            return _R()

        with _patch(
            "tigerharness.slack_bridge.bridge.detect_persona", new=fake_router,
        ), _patch(
            "tigerharness.slack_bridge.bridge.run_with_retry", new=fake_run,
        ):
            say = AsyncMock()
            event = {
                "channel_type": "im", "user": "U0CEO",
                "text": "Hi", "ts": "1.1",
            }
            await b.handle_message(event, say)
        assert b.cost_so_far == pytest.approx(0.0030)


class TestGetOrOpenThreadConcurrency:
    """The router LLM call is intentionally OUTSIDE ``_threads_guard``
    so multiple new threads on the same bridge can route in parallel.
    If two coroutines race for the same key, the loser closes its
    session and uses the winner's state."""

    def _multi_bridge(self, tmp_path: Path):
        from tigerharness.slack_bridge.bridge import (
            PersonaSlot, SlackBridge, TeamBridgeContext,
        )

        # Each open_session returns a DIFFERENT session so we can tell
        # winner from loser.
        sessions: list = []

        class _Sess:
            def __init__(self, sid):
                self.id = sid
                self.closed = False
            async def close(self):
                self.closed = True

        async def make_session(*args, **kwargs):
            s = _Sess(f"sess-{len(sessions)}")
            sessions.append(s)
            return s

        backend = AsyncMock()
        backend.open_session = make_session

        personas = {
            n: PersonaSlot(name=n, agent_config=AgentConfig(name=n, instructions=n))
            for n in ("Ayako", "Sakuragi")
        }
        team_ctx = TeamBridgeContext(
            team_name="t", slack_app_token="xapp", slack_bot_token="xoxb",
            allowed_user_ids=frozenset({"U0CEO"}), agent_cwd=str(tmp_path),
            personas=personas, default_persona="Ayako",
        )
        store = ThreadStore(tmp_path / "threads.json")
        downloader = MagicMock()
        downloader.download = AsyncMock(return_value=None)
        b = SlackBridge(
            team_ctx=team_ctx, backend=backend, store=store, downloader=downloader,
        )
        return b, sessions

    @pytest.mark.asyncio
    async def test_race_loser_closes_its_session(self, tmp_path: Path):
        """When two coroutines call ``_get_or_open_thread`` for the same
        key concurrently, both open sessions outside the lock. Only one
        wins the dict claim; the other must close its session to avoid
        an orphan subprocess."""
        from unittest.mock import patch as _patch
        b, sessions = self._multi_bridge(tmp_path)

        async def fake_router(*args, **kwargs):
            # Force ordering: both calls progress past the router.
            await asyncio.sleep(0.01)
            return "Ayako", 0.0

        with _patch(
            "tigerharness.slack_bridge.bridge.detect_persona", new=fake_router,
        ):
            t1 = asyncio.create_task(
                b._get_or_open_thread("thread-1", "Hi"),
            )
            t2 = asyncio.create_task(
                b._get_or_open_thread("thread-1", "Hi"),
            )
            s1, s2 = await asyncio.gather(t1, t2)
        # Both return the SAME state (winner's).
        assert s1 is s2
        # Two sessions were opened (one per coroutine).
        assert len(sessions) == 2
        # Exactly one was closed (the loser's). The winner's is still
        # alive because it's bound to the live _ThreadState.
        closed = [s for s in sessions if s.closed]
        assert len(closed) == 1
        kept = [s for s in sessions if not s.closed]
        assert len(kept) == 1
        assert kept[0].id == s1.session.id


class TestTigerMemoryTriggerGating:
    """A new thread fires the legacy ``tiger-memory rebuild`` only when
    ``tiger_memory_trigger == "rebuild"`` (the default). With ``"off"``,
    the daemon stays silent -- the in-session sweep protocol owns the
    rebuild."""

    def _bridge(self, tmp_path: Path, *, trigger: str):
        from tigerharness.slack_bridge.bridge import (
            PersonaSlot, SlackBridge, TeamBridgeContext,
        )

        @dataclass
        class _Sess:
            id: str = "sess"
            async def close(self):
                return None

        backend = AsyncMock()
        backend.open_session = AsyncMock(return_value=_Sess())
        personas = {
            "Ayako": PersonaSlot(
                name="Ayako",
                agent_config=AgentConfig(name="Ayako", instructions="x"),
                tiger_memory_config_path="/tmp/config.yaml",
            )
        }
        team_ctx = TeamBridgeContext(
            team_name="t", slack_app_token="xapp", slack_bot_token="xoxb",
            allowed_user_ids=frozenset({"U0CEO"}), agent_cwd=str(tmp_path),
            personas=personas, default_persona="Ayako",
            tiger_memory_cli="/usr/bin/tiger-memory",
            tiger_memory_trigger=trigger,
        )
        store = ThreadStore(tmp_path / "threads.json")
        downloader = MagicMock()
        downloader.download = AsyncMock(return_value=None)
        return SlackBridge(
            team_ctx=team_ctx, backend=backend, store=store, downloader=downloader,
        )

    @pytest.mark.asyncio
    async def test_rebuild_trigger_fires_on_new_thread(self, tmp_path: Path):
        from unittest.mock import patch as _patch
        b = self._bridge(tmp_path, trigger="rebuild")
        with _patch(
            "tigerharness.slack_bridge.bridge.detect_persona",
            new=AsyncMock(return_value=("Ayako", 0.0)),
        ), _patch(
            "tigerharness.slack_bridge.bridge._trigger_tiger_memory_rebuild",
        ) as mock_trigger:
            await b._get_or_open_thread("thread-1", "Hi")
        mock_trigger.assert_called_once()

    @pytest.mark.asyncio
    async def test_off_trigger_stays_silent_on_new_thread(self, tmp_path: Path):
        from unittest.mock import patch as _patch
        b = self._bridge(tmp_path, trigger="off")
        with _patch(
            "tigerharness.slack_bridge.bridge.detect_persona",
            new=AsyncMock(return_value=("Ayako", 0.0)),
        ), _patch(
            "tigerharness.slack_bridge.bridge._trigger_tiger_memory_rebuild",
        ) as mock_trigger:
            await b._get_or_open_thread("thread-1", "Hi")
        mock_trigger.assert_not_called()

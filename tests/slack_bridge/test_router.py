"""Tests for tigerharness.slack_bridge.router.

The router uses the existing agent backend (no Anthropic SDK
dependency) to identify which persona the user is addressing. These
tests mock the backend so we never touch a real LLM.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from tigerharness.slack_bridge.router import (
    _build_alias_index,
    _build_router_config,
    _format_router_prompt,
    _parse_router_response,
    detect_persona,
)


# ---------------------------------------------------------------------------
# _parse_router_response
# ---------------------------------------------------------------------------

class TestParseRouterResponse:
    def test_empty_string_returns_none(self):
        assert _parse_router_response("", ["ayako"]) is None

    def test_whitespace_only_returns_none(self):
        assert _parse_router_response("   \n  ", ["ayako"]) is None

    def test_only_punctuation_returns_none(self):
        # After stripping `.\"'`, nothing remains.
        assert _parse_router_response("...", ["ayako"]) is None

    def test_default_token_returns_default(self):
        assert _parse_router_response("default", ["ayako", "sakuragi"]) == "default"

    def test_default_token_with_punctuation_still_matches(self):
        # Models sometimes add a period.
        assert _parse_router_response("default.", ["ayako"]) == "default"

    def test_default_token_with_quotes_still_matches(self):
        assert _parse_router_response('"default"', ["ayako"]) == "default"

    def test_case_insensitive_match(self):
        # User capitalizes "Ayako" in roster; model returns "ayako".
        assert _parse_router_response("ayako", ["Ayako", "Sakuragi"]) == "Ayako"

    def test_returns_canonical_case_from_roster(self):
        # Match is canonical: returns the entry's original case.
        assert _parse_router_response("AYAKO", ["Ayako"]) == "Ayako"

    def test_off_roster_response_returns_none(self):
        assert _parse_router_response("ghost", ["ayako", "sakuragi"]) is None

    def test_trailing_punctuation_trimmed(self):
        assert _parse_router_response("ayako!", ["ayako"]) is None
        # `!` is NOT in the stripped chars -- the test is intentional:
        # we only strip `.\"'`. Anything else means "couldn't parse".

    def test_alias_match_returns_canonical_name(self):
        aliases = {"Anzai": ["安西教练", "Anxi", "Anxi Jiaolian"]}
        assert _parse_router_response(
            "Anxi", ["Ayako", "Anzai"], aliases=aliases
        ) == "Anzai"

    def test_alias_match_case_insensitive(self):
        aliases = {"Anzai": ["Anxi"]}
        assert _parse_router_response(
            "anxi", ["Ayako", "Anzai"], aliases=aliases
        ) == "Anzai"

    def test_alias_unicode_match(self):
        aliases = {"Anzai": ["安西教练", "Anxi", "Anxi Jiaolian"]}
        assert _parse_router_response(
            "安西教练", ["Ayako", "Anzai"], aliases=aliases
        ) == "Anzai"

    def test_alias_multi_word_match(self):
        """Multi-word aliases like 'Anxi Jiaolian' should match."""
        aliases = {"Anzai": ["Anxi Jiaolian"]}
        assert _parse_router_response(
            "Anxi Jiaolian", ["Ayako", "Anzai"], aliases=aliases
        ) == "Anzai"

    def test_no_alias_no_change(self):
        """When aliases=None, behavior is unchanged from pre-alias code."""
        assert _parse_router_response(
            "Anzai", ["Ayako", "Anzai"], aliases=None
        ) == "Anzai"
        assert _parse_router_response(
            "Anxi", ["Ayako", "Anzai"], aliases=None
        ) is None

    def test_alias_collision_first_canonical_wins(self):
        """If two personas share an alias, first-registered wins."""
        aliases = {"Ayako": ["Coach"], "Anzai": ["Coach"]}
        result = _parse_router_response(
            "Coach", ["Ayako", "Anzai"], aliases=aliases
        )
        assert result == "Ayako"


# ---------------------------------------------------------------------------
# _build_alias_index
# ---------------------------------------------------------------------------

class TestBuildAliasIndex:
    def test_empty_roster(self):
        assert _build_alias_index([], None) == {}

    def test_canonical_names_indexed(self):
        idx = _build_alias_index(["Ayako", "Anzai"], None)
        assert idx["ayako"] == "Ayako"
        assert idx["anzai"] == "Anzai"

    def test_aliases_indexed(self):
        aliases = {"Anzai": ["安西教练", "Anxi"]}
        idx = _build_alias_index(["Ayako", "Anzai"], aliases)
        assert idx["anxi"] == "Anzai"
        assert idx["安西教练"] == "Anzai"
        assert idx["ayako"] == "Ayako"

    def test_collision_first_wins(self):
        aliases = {"Ayako": ["shared"], "Anzai": ["shared"]}
        idx = _build_alias_index(["Ayako", "Anzai"], aliases)
        assert idx["shared"] == "Ayako"


# ---------------------------------------------------------------------------
# _format_router_prompt
# ---------------------------------------------------------------------------

class TestFormatRouterPrompt:
    def test_contains_roster_and_message(self):
        prompt = _format_router_prompt(
            "Hi Ayako!", ["ayako", "sakuragi"]
        )
        assert "ayako" in prompt
        assert "sakuragi" in prompt
        assert "Hi Ayako!" in prompt
        # Options list includes "default" as the fallback token.
        assert "default" in prompt

    def test_long_message_truncated(self):
        # A 5000-char message gets capped at 4096 so we don't burn
        # the router's token budget on pathological input.
        long_msg = "x" * 5000
        prompt = _format_router_prompt(long_msg, ["ayako"])
        # The original 5000-char run shouldn't appear verbatim.
        assert "x" * 5000 not in prompt
        # The first 4096 chars should be present.
        assert "x" * 4096 in prompt

    def test_aliases_included_in_prompt(self):
        aliases = {"Anzai": ["安西教练", "Anxi"]}
        prompt = _format_router_prompt(
            "Hi Anxi!", ["Ayako", "Anzai"], aliases=aliases
        )
        assert "安西教练" in prompt
        assert "Anxi" in prompt
        assert "also known as" in prompt

    def test_no_aliases_no_also_known_as(self):
        prompt = _format_router_prompt("Hi", ["Ayako", "Anzai"])
        assert "also known as" not in prompt

    def test_persona_without_aliases_shown_plain(self):
        aliases = {"Anzai": ["Anxi"]}
        prompt = _format_router_prompt(
            "Hi", ["Ayako", "Anzai"], aliases=aliases
        )
        # Ayako has no aliases, so shown without parenthetical
        assert "Ayako (also known as" not in prompt
        assert "Anzai (also known as: Anxi)" in prompt


# ---------------------------------------------------------------------------
# _build_router_config
# ---------------------------------------------------------------------------

class TestBuildRouterConfig:
    def test_has_strict_one_token_instructions(self):
        cfg = _build_router_config()
        assert cfg.name == "slack-bridge-router"
        assert "EXACTLY one token" in cfg.instructions
        assert "default" in cfg.instructions

    def test_tools_locked_down(self):
        """The router has no business reading files / running shells /
        fetching URLs. Tool surface must be fully restricted so a
        prompt-injection in a Slack DM can't escalate."""
        cfg = _build_router_config()
        # `plan` mode blocks write/exec tool calls at the CLI level.
        assert cfg.extra.get("permission_mode") == "plan"
        # Explicit deny list catches anything `plan` might let through.
        disallowed = cfg.extra.get("disallowed_tools") or []
        for must_be_denied in (
            "Bash", "Read", "Write", "Edit", "WebFetch", "WebSearch", "Task",
        ):
            assert must_be_denied in disallowed, (
                f"router must explicitly deny {must_be_denied} as defense in depth"
            )
        # One shot only.
        assert cfg.extra.get("max_turns") == 1


# ---------------------------------------------------------------------------
# detect_persona (the public entry point)
# ---------------------------------------------------------------------------

@dataclass
class _FakeSession:
    id: str = "router-sess"

    async def close(self):
        return None


def _fake_backend(reply: str | Exception):
    """Build a mock backend that returns *reply* (or raises) from run_with_retry.

    Patches the module-level ``run_with_retry`` import inside router.py
    rather than the backend method, because router.py calls that helper
    directly.
    """
    from unittest.mock import AsyncMock, MagicMock
    backend = MagicMock()
    backend.open_session = AsyncMock(return_value=_FakeSession())
    return backend


class TestDetectPersona:
    @pytest.mark.asyncio
    async def test_empty_roster_returns_default(self):
        # Should not even try to call the backend.
        backend = _fake_backend("anything")
        result = await detect_persona(
            backend, "Hi there", roster=[], default_persona="ayako"
        )
        assert result[0] == "ayako"
        backend.open_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_single_persona_skips_llm_call(self):
        """One-persona teams short-circuit: the router would only ever
        return that persona anyway, so we save the LLM call entirely."""
        backend = _fake_backend("anything")
        result = await detect_persona(
            backend, "Hi", roster=["ayako"], default_persona="ayako"
        )
        assert result[0] == "ayako"
        backend.open_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_happy_path_returns_matched_name(self, monkeypatch):
        """Multi-persona team + good LLM response -> matched persona."""
        from unittest.mock import AsyncMock, MagicMock
        backend = MagicMock()
        backend.open_session = AsyncMock(return_value=_FakeSession())

        async def fake_retry(_backend, _cfg, _prompt, **_kw):
            return MagicMock(final_output="sakuragi", cost_usd=0.0)

        monkeypatch.setattr(
            "tigerharness.slack_bridge.router.run_with_retry", fake_retry
        )
        result = await detect_persona(
            backend, "Hi Sakuragi!", roster=["ayako", "sakuragi"],
            default_persona="ayako",
        )
        assert result[0] == "sakuragi"

    @pytest.mark.asyncio
    async def test_default_token_response_returns_default(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock
        backend = MagicMock()
        backend.open_session = AsyncMock(return_value=_FakeSession())

        async def fake_retry(*a, **kw):
            return MagicMock(final_output="default", cost_usd=0.0)

        monkeypatch.setattr(
            "tigerharness.slack_bridge.router.run_with_retry", fake_retry
        )
        result = await detect_persona(
            backend, "no name mentioned", roster=["ayako", "sakuragi"],
            default_persona="ayako",
        )
        assert result[0] == "ayako"

    @pytest.mark.asyncio
    async def test_off_roster_response_falls_back_to_default(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock
        backend = MagicMock()
        backend.open_session = AsyncMock(return_value=_FakeSession())

        async def fake_retry(*a, **kw):
            return MagicMock(final_output="ghost", cost_usd=0.0)

        monkeypatch.setattr(
            "tigerharness.slack_bridge.router.run_with_retry", fake_retry
        )
        result = await detect_persona(
            backend, "anything", roster=["ayako", "sakuragi"],
            default_persona="ayako",
        )
        assert result[0] == "ayako"

    @pytest.mark.asyncio
    async def test_backend_failure_falls_back_to_default(self, monkeypatch):
        """If the backend explodes, the router returns default rather
        than crashing the dispatch."""
        from unittest.mock import AsyncMock, MagicMock
        backend = MagicMock()
        backend.open_session = AsyncMock(side_effect=RuntimeError("offline"))

        result = await detect_persona(
            backend, "Hi", roster=["ayako", "sakuragi"],
            default_persona="ayako",
        )
        assert result[0] == "ayako"

    @pytest.mark.asyncio
    async def test_session_close_failure_does_not_propagate(self, monkeypatch):
        """session.close() raising during cleanup should be swallowed --
        the routing decision is already made."""
        from unittest.mock import AsyncMock, MagicMock

        class _BrokenCloseSession:
            id = "x"
            async def close(self):
                raise OSError("close failed")

        backend = MagicMock()
        backend.open_session = AsyncMock(return_value=_BrokenCloseSession())

        async def fake_retry(*a, **kw):
            return MagicMock(final_output="ayako", cost_usd=0.0)

        monkeypatch.setattr(
            "tigerharness.slack_bridge.router.run_with_retry", fake_retry
        )
        # Should not raise.
        result = await detect_persona(
            backend, "Hi", roster=["ayako", "sakuragi"],
            default_persona="ayako",
        )
        assert result[0] == "ayako"

    @pytest.mark.asyncio
    async def test_empty_final_output_falls_back(self, monkeypatch):
        """``result.final_output is None`` -> empty raw -> off-roster ->
        default fallback."""
        from unittest.mock import AsyncMock, MagicMock
        backend = MagicMock()
        backend.open_session = AsyncMock(return_value=_FakeSession())

        async def fake_retry(*a, **kw):
            return MagicMock(final_output=None, cost_usd=0.0)

        monkeypatch.setattr(
            "tigerharness.slack_bridge.router.run_with_retry", fake_retry
        )
        result = await detect_persona(
            backend, "Hi", roster=["ayako", "sakuragi"],
            default_persona="ayako",
        )
        assert result[0] == "ayako"

    @pytest.mark.asyncio
    async def test_cost_propagated_from_result(self, monkeypatch):
        """``detect_persona`` returns ``(persona, cost_usd)`` so the
        bridge can sum router LLM spend across new threads."""
        from unittest.mock import AsyncMock, MagicMock
        backend = MagicMock()
        backend.open_session = AsyncMock(return_value=_FakeSession())

        async def fake_retry(*a, **kw):
            return MagicMock(final_output="ayako", cost_usd=0.0042)

        monkeypatch.setattr(
            "tigerharness.slack_bridge.router.run_with_retry", fake_retry
        )
        persona, cost = await detect_persona(
            backend, "Hi Ayako", roster=["ayako", "sakuragi"],
            default_persona="ayako",
        )
        assert persona == "ayako"
        assert cost == pytest.approx(0.0042)

    @pytest.mark.asyncio
    async def test_single_persona_returns_zero_cost(self):
        """One-persona teams skip the LLM call -- cost must be 0."""
        from unittest.mock import MagicMock
        backend = MagicMock()
        persona, cost = await detect_persona(
            backend, "Hi", roster=["ayako"], default_persona="ayako",
        )
        assert (persona, cost) == ("ayako", 0.0)

    @pytest.mark.asyncio
    async def test_backend_failure_returns_zero_cost(self):
        """When the routing call blows up, cost is 0 -- we don't know
        the real value (no API response), so report 0 rather than guess."""
        from unittest.mock import AsyncMock, MagicMock
        backend = MagicMock()
        backend.open_session = AsyncMock(side_effect=RuntimeError("offline"))
        persona, cost = await detect_persona(
            backend, "Hi", roster=["ayako", "sakuragi"],
            default_persona="ayako",
        )
        assert (persona, cost) == ("ayako", 0.0)

    @pytest.mark.asyncio
    async def test_alias_response_maps_to_canonical(self, monkeypatch):
        """LLM returns an alias -> detect_persona returns the canonical name."""
        from unittest.mock import AsyncMock, MagicMock
        backend = MagicMock()
        backend.open_session = AsyncMock(return_value=_FakeSession())

        async def fake_retry(*a, **kw):
            return MagicMock(final_output="Anxi", cost_usd=0.0)

        monkeypatch.setattr(
            "tigerharness.slack_bridge.router.run_with_retry", fake_retry
        )
        aliases = {"Anzai": ["Anxi", "安西教练"]}
        result = await detect_persona(
            backend, "Hi Anxi Jiaolian!",
            roster=["Ayako", "Anzai"],
            default_persona="Ayako",
            aliases=aliases,
        )
        assert result[0] == "Anzai"

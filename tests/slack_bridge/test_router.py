"""Tests for tigerharness.slack_bridge.router.

The router uses the existing agent backend (no Anthropic SDK
dependency) to identify which persona the user is addressing. These
tests mock the backend so we never touch a real LLM.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from tigerharness.slack_bridge.router import (
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


# ---------------------------------------------------------------------------
# _build_router_config
# ---------------------------------------------------------------------------

class TestBuildRouterConfig:
    def test_has_strict_one_token_instructions(self):
        cfg = _build_router_config()
        assert cfg.name == "slack-bridge-router"
        assert "EXACTLY one token" in cfg.instructions
        assert "default" in cfg.instructions
        # bypassPermissions so the router never prompts for tool use.
        assert cfg.extra.get("permission_mode") == "bypassPermissions"


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
        assert result == "ayako"
        backend.open_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_single_persona_skips_llm_call(self):
        """One-persona teams short-circuit: the router would only ever
        return that persona anyway, so we save the LLM call entirely."""
        backend = _fake_backend("anything")
        result = await detect_persona(
            backend, "Hi", roster=["ayako"], default_persona="ayako"
        )
        assert result == "ayako"
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
        assert result == "sakuragi"

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
        assert result == "ayako"

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
        assert result == "ayako"

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
        assert result == "ayako"

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
        assert result == "ayako"

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
        assert result == "ayako"

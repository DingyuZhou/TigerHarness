"""Config primitive tests: trigger normalization + token redaction.

The env-reading single-tenant ``load()`` was removed with the
single-tenant entrypoint (ADR 0009); lane env validation now lives in
``multi._validate_tokens`` and is tested in test_multi.py.
"""

from __future__ import annotations

import pytest

from tigerharness.slack_bridge.config import (
    normalize_tiger_memory_trigger,
    redact_token,
)


# ---------------------------------------------------------------------------
# tiger_memory_trigger
# ---------------------------------------------------------------------------

class TestNormalizeTigerMemoryTrigger:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("rebuild", "rebuild"),
            ("off", "off"),
            ("", "rebuild"),       # empty -> legacy default
            (None, "rebuild"),     # unset -> legacy default
            ("  OFF  ", "off"),    # case + whitespace tolerant
            ("Rebuild", "rebuild"),
            # YAML 1.1 coerces a bare `off` to the boolean False; recover
            # the user's intent instead of falling back to the default.
            (False, "off"),
        ],
    )
    def test_valid_values(self, raw, expected):
        assert normalize_tiger_memory_trigger(raw) == expected

    def test_unknown_raises_value_error(self):
        with pytest.raises(ValueError, match="unknown tiger_memory_trigger"):
            normalize_tiger_memory_trigger("nope")

    def test_yaml_true_bool_is_rejected(self):
        # `on`/`yes`/`true` -> YAML True -> no valid mode -> error (not a
        # silent fallback). The repr in the message reflects the bool.
        with pytest.raises(ValueError, match="unknown tiger_memory_trigger True"):
            normalize_tiger_memory_trigger(True)

    def test_error_message_carries_where(self):
        with pytest.raises(ValueError, match="lane 'shohoku'"):
            normalize_tiger_memory_trigger("bogus", where="lane 'shohoku'")


# ---------------------------------------------------------------------------
# redact_token (log family V: never a full secret)
# ---------------------------------------------------------------------------

class TestRedactToken:
    def test_long_token_keeps_prefix_and_suffix_only(self):
        tok = "xoxb-very-secret-token-value-123456"
        out = redact_token(tok)
        assert out == "xoxb-...3456"
        assert tok not in out

    def test_short_token_fully_masked(self):
        # <= 12 chars: prefix+suffix would leak most of it -- mask fully.
        assert redact_token("xoxb-1234567") == "<short>"
        assert redact_token("") == "<short>"

    def test_boundary_thirteen_chars_is_redacted_form(self):
        out = redact_token("abcdefghijklm")  # 13 chars
        assert out == "abcde...jklm"

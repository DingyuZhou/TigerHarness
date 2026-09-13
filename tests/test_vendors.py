"""Tests for ``tigerharness.vendors`` -- the vendor/model policy resolver."""

from __future__ import annotations

import builtins
import logging
from pathlib import Path

import pytest

from tigerharness import vendors
from tigerharness.vendors import (
    CLAUDE_BACKEND,
    CODEX_BACKEND,
    DEFAULT_VENDOR,
    ModelPolicy,
    backend_for_vendor,
    describe,
    is_vendor_name,
    normalize_model,
    normalize_vendor,
    read_personas_yaml,
    resolve_model_policy,
    team_default_policy,
    vendor_for_backend,
)


def _write(team: Path, body: str) -> Path:
    cfg = team / "configs"
    cfg.mkdir(parents=True, exist_ok=True)
    path = cfg / "personas.yaml"
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

class TestNames:
    @pytest.mark.parametrize("raw,expected", [
        ("claude", "claude"),
        ("Claude", "claude"),
        (" anthropic ", "claude"),
        ("claude_p", "claude"),
        ("chatgpt", "chatgpt"),
        ("OpenAI", "chatgpt"),
        ("codex", "chatgpt"),
        ("codex_exec", "chatgpt"),
        ("codex-exec", "chatgpt"),
    ])
    def test_normalize_vendor_aliases(self, raw: str, expected: str) -> None:
        assert normalize_vendor(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "  ", "default", "DEFAULT", "inherit", "team"])
    def test_normalize_vendor_inherit_values(self, raw: object) -> None:
        assert normalize_vendor(raw) is None

    def test_normalize_vendor_unknown_raises_with_known_list(self) -> None:
        with pytest.raises(ValueError, match="unknown model vendor 'chatgtp'"):
            normalize_vendor("chatgtp", where="personas.yaml: x")

    def test_normalize_vendor_non_string_raises(self) -> None:
        with pytest.raises(ValueError, match="expected a vendor name string"):
            normalize_vendor(3)

    def test_normalize_model(self) -> None:
        assert normalize_model(" gpt-6-astra ") == "gpt-6-astra"
        assert normalize_model("default") is None
        assert normalize_model(None) is None
        with pytest.raises(ValueError, match="expected a model id string"):
            normalize_model(["x"])

    def test_backend_lookups(self) -> None:
        assert backend_for_vendor("claude") == CLAUDE_BACKEND == "claude_p"
        assert backend_for_vendor("chatgpt") == CODEX_BACKEND == "codex_exec"
        assert backend_for_vendor("default") == CLAUDE_BACKEND
        assert vendor_for_backend("codex_exec") == "chatgpt"
        assert vendor_for_backend("claude_p") == "claude"
        assert vendor_for_backend("mine") is None

    def test_is_vendor_name(self) -> None:
        assert is_vendor_name("ChatGPT")
        assert is_vendor_name("codex_exec")
        assert not is_vendor_name("mine")
        assert not is_vendor_name(None)

    def test_policy_cli_and_describe(self) -> None:
        p = ModelPolicy(vendor="chatgpt", backend=CODEX_BACKEND, model="gpt-6-astra")
        assert p.cli == "codex"
        assert describe(p) == "chatgpt/gpt-6-astra"
        assert describe(ModelPolicy("claude", CLAUDE_BACKEND, None)) == "claude"


# ---------------------------------------------------------------------------
# Reading personas.yaml
# ---------------------------------------------------------------------------

class TestReadPersonasYaml:
    def test_no_team_root(self) -> None:
        assert read_personas_yaml(None) is None

    def test_missing_file(self, tmp_path: Path) -> None:
        assert read_personas_yaml(tmp_path) is None

    def test_empty_file(self, tmp_path: Path) -> None:
        _write(tmp_path, "")
        assert read_personas_yaml(tmp_path) is None

    def test_non_mapping_logs_and_returns_none(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write(tmp_path, "- just\n- a list\n")
        with caplog.at_level(logging.WARNING, logger="tigerharness.vendors"):
            assert read_personas_yaml(tmp_path) is None
        assert "not a mapping" in caplog.text

    def test_mapping(self, tmp_path: Path) -> None:
        _write(tmp_path, "default_vendor: chatgpt\npersonas: []\n")
        assert read_personas_yaml(tmp_path) == {
            "default_vendor": "chatgpt", "personas": [],
        }

    def test_malformed_yaml_raises_value_error_with_path(self, tmp_path: Path) -> None:
        _write(tmp_path, "personas: [unclosed\n")
        with pytest.raises(ValueError, match="personas.yaml: not valid YAML"):
            read_personas_yaml(tmp_path)

    def test_missing_pyyaml_warns_and_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _write(tmp_path, "default_vendor: chatgpt\n")
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):  # type: ignore[no-untyped-def]
            if name == "yaml":
                raise ImportError("no yaml here")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with caplog.at_level(logging.WARNING, logger="tigerharness.vendors"):
            assert read_personas_yaml(tmp_path) is None
        assert "pyyaml is not installed" in caplog.text


# ---------------------------------------------------------------------------
# Resolution rules
# ---------------------------------------------------------------------------

_ROSTER = """\
default_vendor: {vendor}
default_model: {model}
personas:
  - name: Ayako
    aliases: [Ayako-san, 彩子]
  - name: Rukawa
    vendor: chatgpt
  - name: Mitsui
    vendor: chatgpt
    model: gpt-6-astra
  - name: Kogure
    model: claude-haiku-4-5
  - name: Sakuragi
    vendor: default
    model: default
  - name: Haruko
    vendor: claude
  - not-a-mapping
"""


class TestTeamDefault:
    def test_builtin_when_no_file(self, tmp_path: Path) -> None:
        p = team_default_policy(tmp_path)
        assert p == ModelPolicy(DEFAULT_VENDOR, CLAUDE_BACKEND, None, "builtin")

    def test_builtin_when_no_root(self) -> None:
        assert team_default_policy(None).source == "builtin"

    def test_team_vendor_and_model(self, tmp_path: Path) -> None:
        _write(tmp_path, "default_vendor: chatgpt\ndefault_model: gpt-6-astra\n")
        p = team_default_policy(tmp_path)
        assert p == ModelPolicy("chatgpt", CODEX_BACKEND, "gpt-6-astra", "team")

    def test_model_without_vendor_binds_to_builtin_vendor(self, tmp_path: Path) -> None:
        _write(tmp_path, "default_model: claude-opus-5\n")
        p = team_default_policy(tmp_path)
        assert p == ModelPolicy("claude", CLAUDE_BACKEND, "claude-opus-5", "builtin")

    def test_blank_values_mean_unset(self, tmp_path: Path) -> None:
        _write(tmp_path, 'default_vendor: ""\ndefault_model: ""\n')
        assert team_default_policy(tmp_path).source == "builtin"

    def test_unknown_team_vendor_raises_with_path(self, tmp_path: Path) -> None:
        _write(tmp_path, "default_vendor: gemini\n")
        with pytest.raises(ValueError, match="personas.yaml: default_vendor"):
            team_default_policy(tmp_path)

    def test_data_param_skips_reading(self, tmp_path: Path) -> None:
        # No file on disk, but data supplied: the data wins.
        p = team_default_policy(tmp_path, data={"default_vendor": "chatgpt"})
        assert p.vendor == "chatgpt"
        p2 = team_default_policy(None, data={"default_model": "x"})
        assert p2.model == "x"


class TestResolveModelPolicy:
    @pytest.fixture
    def team(self, tmp_path: Path) -> Path:
        _write(tmp_path, _ROSTER.format(vendor="claude", model="claude-opus-5"))
        return tmp_path

    def test_inherits_team_vendor_and_model(self, team: Path) -> None:
        p = resolve_model_policy(team, "Ayako")
        assert p == ModelPolicy("claude", CLAUDE_BACKEND, "claude-opus-5", "team")

    def test_vendor_switch_without_model_gets_vendor_default(self, team: Path) -> None:
        # The team's claude-opus-5 must NOT leak onto a codex persona.
        p = resolve_model_policy(team, "Rukawa")
        assert p == ModelPolicy("chatgpt", CODEX_BACKEND, None, "persona")

    def test_vendor_switch_with_model(self, team: Path) -> None:
        p = resolve_model_policy(team, "Mitsui")
        assert p == ModelPolicy("chatgpt", CODEX_BACKEND, "gpt-6-astra", "persona")

    def test_model_pin_on_team_vendor(self, team: Path) -> None:
        p = resolve_model_policy(team, "Kogure")
        assert p == ModelPolicy("claude", CLAUDE_BACKEND, "claude-haiku-4-5", "persona")

    def test_literal_default_inherits(self, team: Path) -> None:
        p = resolve_model_policy(team, "Sakuragi")
        assert p == ModelPolicy("claude", CLAUDE_BACKEND, "claude-opus-5", "team")

    def test_same_vendor_as_team_inherits_team_model(self, team: Path) -> None:
        # `vendor: claude` restated explicitly, no model: the team's model applies.
        p = resolve_model_policy(team, "Haruko")
        assert p == ModelPolicy("claude", CLAUDE_BACKEND, "claude-opus-5", "persona")

    def test_unknown_persona_gets_team_default(self, team: Path) -> None:
        assert resolve_model_policy(team, "Nobody").source == "team"

    def test_none_persona_gets_team_default(self, team: Path) -> None:
        assert resolve_model_policy(team, None).source == "team"

    def test_no_file_gets_builtin(self, tmp_path: Path) -> None:
        assert resolve_model_policy(tmp_path, "Ayako").source == "builtin"

    def test_case_insensitive_and_alias_lookup(self, team: Path) -> None:
        assert resolve_model_policy(team, "mitsui").vendor == "chatgpt"
        assert resolve_model_policy(team, "彩子").model == "claude-opus-5"
        assert resolve_model_policy(team, "ayako-san").vendor == "claude"

    def test_personas_not_a_list(self, tmp_path: Path) -> None:
        _write(tmp_path, "default_vendor: chatgpt\npersonas: {}\n")
        assert resolve_model_policy(tmp_path, "Ayako").vendor == "chatgpt"

    def test_alias_entries_that_are_not_lists_are_ignored(self, tmp_path: Path) -> None:
        _write(tmp_path, "personas:\n  - name: A\n    aliases: nope\n  - name: B\n    aliases: [3, b2]\n")
        assert resolve_model_policy(tmp_path, "b2").source == "builtin"
        assert resolve_model_policy(tmp_path, "zzz").source == "builtin"

    def test_unknown_persona_vendor_raises_naming_the_persona(self, tmp_path: Path) -> None:
        _write(tmp_path, "personas:\n  - name: Rukawa\n    vendor: chatgtp\n")
        with pytest.raises(ValueError, match="persona 'Rukawa' vendor"):
            resolve_model_policy(tmp_path, "Rukawa")

    def test_team_chatgpt_persona_claude_no_model(self, tmp_path: Path) -> None:
        _write(tmp_path, _ROSTER.format(vendor="chatgpt", model="gpt-6-astra"))
        p = resolve_model_policy(tmp_path, "Haruko")
        assert p == ModelPolicy("claude", CLAUDE_BACKEND, None, "persona")
        # And the chatgpt personas inherit the team model when unpinned.
        assert resolve_model_policy(tmp_path, "Rukawa").model == "gpt-6-astra"
        assert resolve_model_policy(tmp_path, "Mitsui").model == "gpt-6-astra"

    def test_data_param_is_used_verbatim(self) -> None:
        data = {
            "default_vendor": "chatgpt",
            "personas": [{"name": "X", "model": "m1"}],
        }
        p = resolve_model_policy(None, "X", data=data)
        assert p == ModelPolicy("chatgpt", CODEX_BACKEND, "m1", "persona")

    def test_module_constants_agree(self) -> None:
        assert vendors.VENDOR_BACKENDS == {"claude": "claude_p", "chatgpt": "codex_exec"}
        assert vendors.VENDOR_CLIS == {"claude": "claude", "chatgpt": "codex"}

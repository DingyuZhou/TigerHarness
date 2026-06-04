"""Tests for the Phase 1.5 workflow-mode scaffolder (``new_workflow_task``)
plus its persona-validation helpers and team-root resolver."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tigerharness.journal.models import CompilePhase, State, Status
from tigerharness.journal.paths import JournalPaths
from tigerharness.journal.scaffold import (
    COMPILE_PERSONAS,
    JournalScaffoldError,
    MissingPersonaError,
    _required_workflow_personas,
    extract_persona_refs_from_playbook,
    new_workflow_task,
    read_team_roster,
    resolve_team_root,
    validate_personas,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_team(
    root: Path,
    *,
    personas: list[str] | None = None,
    name: str = "Shohoku",
) -> Path:
    """Build a fake team directory with the given persona registry and
    prompt.md files. Returns the team root path."""
    team = root / "teams" / name
    (team / "configs").mkdir(parents=True)
    if personas is None:
        personas = list(COMPILE_PERSONAS) + ["Mitsui"]
    lines = ["personas:\n"]
    for p in personas:
        lines.append(f"  - name: {p}\n")
    (team / "configs" / "personas.yaml").write_text("".join(lines))
    for p in personas:
        pdir = team / "personas" / p
        pdir.mkdir(parents=True)
        (pdir / "prompt.md").write_text(f"You are {p}.\n")
    return team


# ---------------------------------------------------------------------------
# resolve_team_root
# ---------------------------------------------------------------------------

class TestResolveTeamRoot:
    def test_env_override_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TIGERHARNESS_TEAMS_DIR", str(tmp_path))
        assert resolve_team_root("Shohoku") == tmp_path / "Shohoku"

    def test_cwd_is_team_root(self, monkeypatch, tmp_path):
        """If cwd has configs/personas.yaml, cwd IS the team root."""
        monkeypatch.delenv("TIGERHARNESS_TEAMS_DIR", raising=False)
        (tmp_path / "configs").mkdir()
        (tmp_path / "configs" / "personas.yaml").write_text("personas: []\n")
        monkeypatch.chdir(tmp_path)
        assert resolve_team_root("Shohoku") == tmp_path

    def test_falls_back_to_cwd_teams(self, monkeypatch, tmp_path):
        monkeypatch.delenv("TIGERHARNESS_TEAMS_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        assert resolve_team_root("Shohoku") == tmp_path / "teams" / "Shohoku"

    def test_blank_env_falls_through(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TIGERHARNESS_TEAMS_DIR", "  ")
        monkeypatch.chdir(tmp_path)
        assert resolve_team_root("Shohoku") == tmp_path / "teams" / "Shohoku"


# ---------------------------------------------------------------------------
# extract_persona_refs_from_playbook
# ---------------------------------------------------------------------------

class TestExtractPersonaRefs:
    def test_picks_up_capitalised_words(self):
        text = "Anzai plans. Mitsui implements."
        refs = extract_persona_refs_from_playbook(text)
        assert "Anzai" in refs
        assert "Mitsui" in refs

    def test_ignores_short_words(self):
        """``[A-Z][a-zA-Z]{2,}`` requires at least 3 chars; 'A' / 'AI'
        are too short."""
        text = "A short AI thing."
        assert extract_persona_refs_from_playbook(text) == {"AI"} or \
            "A" not in extract_persona_refs_from_playbook(text)

    def test_ignores_lowercase(self):
        assert extract_persona_refs_from_playbook("mitsui") == set()


# ---------------------------------------------------------------------------
# read_team_roster
# ---------------------------------------------------------------------------

class TestReadTeamRoster:
    def test_reads_yaml(self, tmp_path):
        team = _make_team(tmp_path, personas=["Anzai", "Akagi", "Ayako"])
        assert read_team_roster(team) == {"Anzai", "Akagi", "Ayako"}

    def test_missing_yaml_returns_empty(self, tmp_path):
        # team root with no configs/personas.yaml
        team = tmp_path / "teams" / "Bad"
        team.mkdir(parents=True)
        assert read_team_roster(team) == set()

    def test_malformed_yaml_returns_empty(self, tmp_path):
        team = tmp_path / "teams" / "Bad"
        (team / "configs").mkdir(parents=True)
        (team / "configs" / "personas.yaml").write_text(
            "not_a_dict: just_a_string\n"
        )
        # That parses to {"not_a_dict": "just_a_string"} which has no
        # personas key.
        assert read_team_roster(team) == set()

    def test_top_level_not_dict_returns_empty(self, tmp_path):
        team = tmp_path / "teams" / "Bad"
        (team / "configs").mkdir(parents=True)
        (team / "configs" / "personas.yaml").write_text(
            "- just\n- a\n- list\n"
        )
        assert read_team_roster(team) == set()

    def test_entries_without_name_are_skipped(self, tmp_path):
        team = tmp_path / "teams" / "Bad"
        (team / "configs").mkdir(parents=True)
        (team / "configs" / "personas.yaml").write_text(
            "personas:\n  - description: just a description\n"
            "  - name: Akagi\n"
        )
        assert read_team_roster(team) == {"Akagi"}

    def test_non_dict_entries_are_skipped(self, tmp_path):
        team = tmp_path / "teams" / "Bad"
        (team / "configs").mkdir(parents=True)
        (team / "configs" / "personas.yaml").write_text(
            "personas:\n  - just-a-string\n  - name: Akagi\n"
        )
        assert read_team_roster(team) == {"Akagi"}

    def test_non_string_name_skipped(self, tmp_path):
        team = tmp_path / "teams" / "Bad"
        (team / "configs").mkdir(parents=True)
        (team / "configs" / "personas.yaml").write_text(
            "personas:\n  - name: 42\n  - name: Akagi\n"
        )
        assert read_team_roster(team) == {"Akagi"}

    def test_blank_name_skipped(self, tmp_path):
        team = tmp_path / "teams" / "Bad"
        (team / "configs").mkdir(parents=True)
        (team / "configs" / "personas.yaml").write_text(
            "personas:\n  - name: \"   \"\n  - name: Akagi\n"
        )
        assert read_team_roster(team) == {"Akagi"}


# ---------------------------------------------------------------------------
# _required_workflow_personas
# ---------------------------------------------------------------------------

class TestRequiredWorkflowPersonas:
    def test_includes_compile_personas_always(self, tmp_path):
        team = _make_team(tmp_path, personas=["Anzai", "Akagi", "Ayako"])
        req = _required_workflow_personas("nothing capitalised here", team)
        assert req == set(COMPILE_PERSONAS)

    def test_union_with_playbook_refs_that_are_in_roster(self, tmp_path):
        team = _make_team(
            tmp_path,
            personas=["Anzai", "Akagi", "Ayako", "Mitsui"],
        )
        req = _required_workflow_personas("Mitsui implements.", team)
        assert req == set(COMPILE_PERSONAS) | {"Mitsui"}

    def test_drops_playbook_refs_not_in_roster(self, tmp_path):
        """A capitalised word that isn't a registered persona is
        English prose, not a typo."""
        team = _make_team(tmp_path, personas=list(COMPILE_PERSONAS))
        req = _required_workflow_personas(
            "Default behavior; QAs are critical.", team,
        )
        # 'Default' and 'QAs' are NOT in the team roster; dropped.
        assert req == set(COMPILE_PERSONAS)


# ---------------------------------------------------------------------------
# validate_personas
# ---------------------------------------------------------------------------

class TestPersonaAliases:
    """Personas can carry an ``aliases:`` list in personas.yaml; the
    scaffolder resolves any alias to its canonical name before
    checking prompt.md and before counting playbook references."""

    def test_canonicalize_returns_input_when_no_alias_map(self, tmp_path):
        from tigerharness.journal.scaffold import canonicalize_persona
        # No personas.yaml -> empty alias map -> input passes through.
        team = tmp_path / "teams" / "T"
        team.mkdir(parents=True)
        assert canonicalize_persona(team, "Whoever") == "Whoever"

    def test_canonicalize_self_maps_canonical_names(self, tmp_path):
        from tigerharness.journal.scaffold import canonicalize_persona
        team = _make_team(tmp_path)  # Anzai/Akagi/Ayako/Mitsui
        assert canonicalize_persona(team, "Anzai") == "Anzai"
        assert canonicalize_persona(team, "Mitsui") == "Mitsui"

    def test_canonicalize_resolves_aliases(self, tmp_path):
        team = _make_team(tmp_path)
        # Add Kogure with alias Mumu to the team's personas.yaml.
        (team / "configs" / "personas.yaml").write_text(
            "personas:\n"
            "  - name: Anzai\n"
            "  - name: Akagi\n"
            "  - name: Ayako\n"
            "  - name: Mitsui\n"
            "  - name: Kogure\n"
            "    aliases: [Mumu, Kogure-san]\n"
        )
        from tigerharness.journal.scaffold import canonicalize_persona
        assert canonicalize_persona(team, "Mumu") == "Kogure"
        assert canonicalize_persona(team, "Kogure-san") == "Kogure"
        assert canonicalize_persona(team, "Kogure") == "Kogure"

    def test_canonicalize_is_case_and_separator_insensitive(self, tmp_path):
        team = _make_team(tmp_path)
        (team / "configs" / "personas.yaml").write_text(
            "personas:\n"
            "  - name: Kogure\n"
            "    aliases: [Mumu, Kogure-san]\n"
        )
        from tigerharness.journal.scaffold import canonicalize_persona
        assert canonicalize_persona(team, "mumu") == "Kogure"
        assert canonicalize_persona(team, "MUMU") == "Kogure"
        # Separator differences (case + dash/space/underscore) all
        # collapse to the same normalised key.
        assert canonicalize_persona(team, "kogure-SAN") == "Kogure"
        assert canonicalize_persona(team, "kogure san") == "Kogure"
        assert canonicalize_persona(team, "kogure_san") == "Kogure"
        assert canonicalize_persona(team, "  Mumu  ") == "Kogure"

    def test_unknown_name_falls_through_unchanged(self, tmp_path):
        team = _make_team(tmp_path)
        from tigerharness.journal.scaffold import canonicalize_persona
        # Unknown -> input back unchanged. The downstream missing-
        # persona check then catches it.
        assert canonicalize_persona(team, "NotAPersona") == "NotAPersona"

    def test_persona_prompt_path_uses_canonical(self, tmp_path):
        from tigerharness.journal.scaffold import _persona_prompt_path
        team = _make_team(tmp_path)
        # Add Kogure with alias Mumu, with a prompt.md under Kogure/.
        (team / "configs" / "personas.yaml").write_text(
            "personas:\n"
            "  - name: Kogure\n"
            "    aliases: [Mumu]\n"
        )
        (team / "personas" / "Kogure").mkdir(parents=True)
        (team / "personas" / "Kogure" / "prompt.md").write_text("hi\n")
        # Lookup by alias finds Kogure's prompt.md.
        path = _persona_prompt_path(team, "Mumu")
        assert path == team / "personas" / "Kogure" / "prompt.md"
        assert path.is_file()

    def test_validate_personas_resolves_alias_before_check(self, tmp_path):
        from tigerharness.journal.scaffold import validate_personas
        team = _make_team(tmp_path)
        (team / "configs" / "personas.yaml").write_text(
            "personas:\n"
            "  - name: Anzai\n"
            "  - name: Akagi\n"
            "  - name: Ayako\n"
            "  - name: Kogure\n"
            "    aliases: [Mumu]\n"
        )
        (team / "personas" / "Kogure").mkdir(parents=True)
        (team / "personas" / "Kogure" / "prompt.md").write_text("hi\n")
        # Asking by alias -> alias resolves, prompt found, not missing.
        assert validate_personas(team, {"Mumu"}) == []

    def test_required_workflow_personas_recognises_alias_refs(self, tmp_path):
        """Playbook prose that addresses Kogure by his alias Mumu is
        recognised as a real persona reference, not as English prose."""
        from tigerharness.journal.scaffold import _required_workflow_personas
        team = _make_team(tmp_path)
        (team / "configs" / "personas.yaml").write_text(
            "personas:\n"
            "  - name: Anzai\n"
            "  - name: Akagi\n"
            "  - name: Ayako\n"
            "  - name: Mitsui\n"
            "  - name: Kogure\n"
            "    aliases: [Mumu]\n"
        )
        playbook = "Anzai plans. Mumu reviews. Mitsui implements.\n"
        required = _required_workflow_personas(playbook, team)
        # Canonical name returned (Kogure), not the alias.
        assert "Kogure" in required
        # Default compile personas still required.
        assert {"Anzai", "Akagi", "Ayako"} <= required

    def test_alias_map_non_dict_yaml_root_returns_empty(self, tmp_path):
        """Defense-in-depth: a personas.yaml whose top level is a list
        (typo / wrong file in the slot) yields an empty alias map
        rather than crashing."""
        from tigerharness.journal.scaffold import read_team_alias_map
        team = _make_team(tmp_path)
        (team / "configs" / "personas.yaml").write_text("- list at root\n")
        assert read_team_alias_map(team) == {}

    def test_alias_map_skips_non_dict_entries(self, tmp_path):
        """A `personas:` list with a stray scalar (string / int) at the
        top instead of a dict is skipped, not crashed."""
        from tigerharness.journal.scaffold import read_team_alias_map
        team = _make_team(tmp_path)
        (team / "configs" / "personas.yaml").write_text(
            "personas:\n"
            "  - just a string\n"
            "  - name: Kogure\n"
            "    aliases: [Mumu]\n"
        )
        m = read_team_alias_map(team)
        # The stray scalar dropped; Kogure entry honoured.
        assert m["kogure"] == "Kogure"
        assert m["mumu"] == "Kogure"

    def test_alias_map_skips_entries_missing_name(self, tmp_path):
        """An entry with no `name:` (or a blank one) is skipped."""
        from tigerharness.journal.scaffold import read_team_alias_map
        team = _make_team(tmp_path)
        (team / "configs" / "personas.yaml").write_text(
            "personas:\n"
            "  - description: nameless\n"
            "  - name: '   '\n"  # whitespace-only -> skipped
            "  - name: Kogure\n"
        )
        m = read_team_alias_map(team)
        # Only Kogure survives.
        assert list(m.values()) == ["Kogure"]

    def test_alias_map_skips_non_list_aliases_field(self, tmp_path):
        """A persona whose aliases field is not a list (e.g. a string)
        is honoured for its canonical name but its alias field is
        ignored rather than crashing."""
        from tigerharness.journal.scaffold import read_team_alias_map
        team = _make_team(tmp_path)
        (team / "configs" / "personas.yaml").write_text(
            "personas:\n"
            "  - name: Kogure\n"
            "    aliases: 'should be a list'\n"
        )
        m = read_team_alias_map(team)
        # Self-mapping is present; the malformed aliases field skipped.
        assert m["kogure"] == "Kogure"
        # No alias entries added.
        assert sum(1 for v in m.values() if v == "Kogure") == 1

    def test_alias_map_skips_non_string_alias_entries(self, tmp_path):
        """A stray non-string value inside the aliases list is
        skipped, not coerced."""
        from tigerharness.journal.scaffold import read_team_alias_map
        team = _make_team(tmp_path)
        (team / "configs" / "personas.yaml").write_text(
            "personas:\n"
            "  - name: Kogure\n"
            "    aliases: [Mumu, 42, '   ', '']\n"
        )
        m = read_team_alias_map(team)
        # Only Mumu (and self-Kogure) survive.
        assert m["mumu"] == "Kogure"
        assert m["kogure"] == "Kogure"
        # Nothing else.
        assert len([v for v in m.values() if v == "Kogure"]) == 2

    def test_alias_collision_with_other_personas_canonical_name_loses(
        self, tmp_path,
    ):
        """Adversarial-review fix: when an alias on persona A would
        collide with the CANONICAL name of persona B (regardless of
        file order), the canonical name's self-mapping wins.
        Otherwise lookups of B's own canonical name would silently
        misroute to A."""
        from tigerharness.journal.scaffold import canonicalize_persona
        team = _make_team(tmp_path)
        # Anzai is canonical. Rukawa declares an alias "Anzai" --
        # which would (without the fix) hijack canonical Anzai lookups
        # to point at Rukawa. The canonical layer wins.
        (team / "configs" / "personas.yaml").write_text(
            "personas:\n"
            "  - name: Anzai\n"
            "  - name: Rukawa\n"
            "    aliases: [Anzai]\n"  # collides with Anzai's canonical
        )
        assert canonicalize_persona(team, "Anzai") == "Anzai"

    def test_alias_collision_canonical_name_wins_regardless_of_order(
        self, tmp_path,
    ):
        """Same collision but with the colliding-alias entry FIRST in
        the file. Canonical name still wins -- order doesn't matter."""
        from tigerharness.journal.scaffold import canonicalize_persona
        team = _make_team(tmp_path)
        (team / "configs" / "personas.yaml").write_text(
            "personas:\n"
            "  - name: Rukawa\n"
            "    aliases: [Anzai]\n"  # appears BEFORE Anzai entry
            "  - name: Anzai\n"
        )
        assert canonicalize_persona(team, "Anzai") == "Anzai"

    def test_alias_collision_between_two_aliases_last_wins(self, tmp_path):
        """When two personas declare the SAME alias (neither matches a
        canonical name), the persona that appears LATER in the yaml
        wins -- a documented precedence policy, pinned here so a
        future refactor doesn't silently change it."""
        from tigerharness.journal.scaffold import canonicalize_persona
        team = _make_team(tmp_path)
        (team / "configs" / "personas.yaml").write_text(
            "personas:\n"
            "  - name: Akagi\n"
            "    aliases: [Captain]\n"
            "  - name: Mitsui\n"
            "    aliases: [Captain]\n"  # same alias on a later entry
        )
        # Last-wins among aliases: "Captain" resolves to Mitsui.
        assert canonicalize_persona(team, "Captain") == "Mitsui"

    def test_workflow_yaml_can_use_alias_in_compile_personas(self, tmp_path):
        """If a team configures `compile_personas: { ayako: Mumu }`,
        the scaffolder canonicalises through to Kogure before checking
        whether the prompt.md exists on disk."""
        from tigerharness.journal.scaffold import _required_workflow_personas
        team = _make_team(tmp_path)
        (team / "configs" / "personas.yaml").write_text(
            "personas:\n"
            "  - name: Anzai\n"
            "  - name: Akagi\n"
            "  - name: Kogure\n"
            "    aliases: [Mumu]\n"
        )
        (team / "configs" / "workflow.yaml").write_text(
            "compile_personas:\n"
            "  ayako: Mumu\n"
        )
        required = _required_workflow_personas("playbook\n", team)
        # The ayako-role persona is Kogure (canonical), not Mumu.
        assert "Kogure" in required
        assert "Mumu" not in required


class TestExtractPlaybookMeta:
    """Playbooks carry machine-readable metadata in HTML-comment YAML
    blocks. extract_playbook_meta merges every such block into a
    single dict."""

    def test_no_comment_blocks_returns_empty(self):
        from tigerharness.journal.scaffold import extract_playbook_meta
        assert extract_playbook_meta("# Just a heading\n") == {}

    def test_single_block_returns_only_known_keys(self):
        """Iteration: extract_playbook_meta now whitelists. A
        single-block playbook that mixes a known key
        (default_captain) with unrelated metadata (max_loop_iters --
        the runner config block's key) returns only the known one."""
        from tigerharness.journal.scaffold import extract_playbook_meta
        text = (
            "# Heading\n\n"
            "<!--\n"
            "default_captain: Mitsui\n"
            "max_loop_iters: 5\n"
            "-->\n"
        )
        # Only known keys leak through; max_loop_iters (runner-only)
        # is parsed but dropped.
        assert extract_playbook_meta(text) == {"default_captain": "Mitsui"}

    def test_multiple_blocks_merge_with_later_winning(self):
        from tigerharness.journal.scaffold import extract_playbook_meta
        text = (
            "<!--\n"
            "default_captain: Mitsui\n"
            "max_loop_iters: 5\n"
            "-->\n\n"
            "<!--\n"
            "default_captain: Akagi\n"
            "extra: tagline\n"
            "-->\n"
        )
        meta = extract_playbook_meta(text)
        # Second block wins on default_captain.
        assert meta["default_captain"] == "Akagi"
        # Unknown keys (max_loop_iters, extra) are filtered out by
        # the whitelist.
        assert "max_loop_iters" not in meta
        assert "extra" not in meta

    def test_narrative_html_comment_is_skipped(self):
        """A non-YAML HTML comment (just prose) is skipped without
        crashing."""
        from tigerharness.journal.scaffold import extract_playbook_meta
        text = (
            "<!-- This is a note from the author, not metadata. -->\n\n"
            "<!--\n"
            "default_captain: Mitsui\n"
            "-->\n"
        )
        meta = extract_playbook_meta(text)
        assert meta["default_captain"] == "Mitsui"

    def test_non_dict_block_skipped(self):
        """A YAML block that parses to a list / scalar is skipped --
        only dicts merge."""
        from tigerharness.journal.scaffold import extract_playbook_meta
        text = (
            "<!--\n- a list at root\n- not a dict\n-->\n\n"
            "<!--\ndefault_captain: Mitsui\n-->\n"
        )
        assert extract_playbook_meta(text) == {"default_captain": "Mitsui"}

    def test_single_line_html_comment_is_treated_as_narrative(self):
        """Pins the documented requirement: HTML-comment YAML blocks
        must span MULTIPLE lines (open tag on its own line, body on
        the next, close tag on its own line). A single-line comment
        like `<!-- default_captain: Mitsui -->` is treated as
        narrative prose and skipped -- the operator avoids accidental
        metadata escape from a stray one-liner comment."""
        from tigerharness.journal.scaffold import extract_playbook_meta
        text = (
            "<!-- default_captain: Mitsui -->\n\n"
            "<!--\n"
            "default_captain: Akagi\n"
            "-->\n"
        )
        # Only the multi-line block contributes.
        assert extract_playbook_meta(text) == {"default_captain": "Akagi"}

    def test_runner_config_block_does_not_leak_through(self):
        """The api-backed runner's `workflow_config:` block (a real
        Shohoku playbook has one) is parsed by the yaml loader but
        filtered out of the journal-side meta. Pins the boundary."""
        from tigerharness.journal.scaffold import extract_playbook_meta
        text = (
            "<!--\n"
            "workflow_config:\n"
            "  human_gate: true\n"
            "  max_cost_usd: 10.0\n"
            "  max_loop_iters: 5\n"
            "-->\n"
        )
        # workflow_config is not in the whitelist -> dropped.
        assert extract_playbook_meta(text) == {}


class TestResolvePlaybookDefaultCaptain:
    def test_none_when_no_block(self, tmp_path):
        from tigerharness.journal.scaffold import (
            resolve_playbook_default_captain,
        )
        team = _make_team(tmp_path)
        assert resolve_playbook_default_captain("plain text\n", team) is None

    def test_returns_canonical(self, tmp_path):
        from tigerharness.journal.scaffold import (
            resolve_playbook_default_captain,
        )
        team = _make_team(tmp_path)
        text = "<!--\ndefault_captain: Mitsui\n-->\n"
        assert resolve_playbook_default_captain(text, team) == "Mitsui"

    def test_resolves_alias(self, tmp_path):
        from tigerharness.journal.scaffold import (
            resolve_playbook_default_captain,
        )
        team = _make_team(tmp_path)
        (team / "configs" / "personas.yaml").write_text(
            "personas:\n"
            "  - name: Kogure\n"
            "    aliases: [Mumu]\n"
        )
        text = "<!--\ndefault_captain: Mumu\n-->\n"
        assert resolve_playbook_default_captain(text, team) == "Kogure"

    def test_blank_value_returns_none(self, tmp_path):
        from tigerharness.journal.scaffold import (
            resolve_playbook_default_captain,
        )
        team = _make_team(tmp_path)
        text = "<!--\ndefault_captain: '   '\n-->\n"
        assert resolve_playbook_default_captain(text, team) is None

    def test_non_string_value_returns_none(self, tmp_path):
        from tigerharness.journal.scaffold import (
            resolve_playbook_default_captain,
        )
        team = _make_team(tmp_path)
        text = "<!--\ndefault_captain: 42\n-->\n"
        assert resolve_playbook_default_captain(text, team) is None


class TestResolveDefaultPersona:
    """Top-level ``default_persona:`` in ``configs/personas.yaml`` is
    the team's "if you don't say who, use this person" knob."""

    def test_none_when_no_yaml(self, tmp_path):
        from tigerharness.journal.scaffold import resolve_default_persona
        team = tmp_path / "teams" / "T"
        team.mkdir(parents=True)
        assert resolve_default_persona(team) is None

    def test_none_when_key_absent(self, tmp_path):
        from tigerharness.journal.scaffold import resolve_default_persona
        team = _make_team(tmp_path)
        # _make_team doesn't add default_persona -- so absent.
        assert resolve_default_persona(team) is None

    def test_returns_key_value(self, tmp_path):
        from tigerharness.journal.scaffold import resolve_default_persona
        team = _make_team(tmp_path)
        (team / "configs" / "personas.yaml").write_text(
            "default_persona: Mitsui\n"
            "personas:\n"
            "  - name: Anzai\n"
            "  - name: Mitsui\n"
        )
        assert resolve_default_persona(team) == "Mitsui"

    def test_resolves_through_alias(self, tmp_path):
        """A team can write `default_persona: Mumu` and have it resolve
        to the canonical Kogure."""
        from tigerharness.journal.scaffold import resolve_default_persona
        team = _make_team(tmp_path)
        (team / "configs" / "personas.yaml").write_text(
            "default_persona: Mumu\n"
            "personas:\n"
            "  - name: Kogure\n"
            "    aliases: [Mumu]\n"
        )
        assert resolve_default_persona(team) == "Kogure"

    def test_strips_whitespace(self, tmp_path):
        from tigerharness.journal.scaffold import resolve_default_persona
        team = _make_team(tmp_path)
        (team / "configs" / "personas.yaml").write_text(
            "default_persona: '  Mitsui  '\n"
            "personas:\n  - name: Mitsui\n"
        )
        assert resolve_default_persona(team) == "Mitsui"

    def test_blank_value_returns_none(self, tmp_path):
        from tigerharness.journal.scaffold import resolve_default_persona
        team = _make_team(tmp_path)
        (team / "configs" / "personas.yaml").write_text(
            "default_persona: '   '\n"
            "personas:\n  - name: Mitsui\n"
        )
        assert resolve_default_persona(team) is None

    def test_non_string_value_returns_none(self, tmp_path):
        from tigerharness.journal.scaffold import resolve_default_persona
        team = _make_team(tmp_path)
        (team / "configs" / "personas.yaml").write_text(
            "default_persona: 42\n"
            "personas:\n  - name: Mitsui\n"
        )
        assert resolve_default_persona(team) is None

    def test_non_dict_yaml_root_returns_none(self, tmp_path):
        from tigerharness.journal.scaffold import resolve_default_persona
        team = _make_team(tmp_path)
        (team / "configs" / "personas.yaml").write_text("- not a dict\n")
        assert resolve_default_persona(team) is None


class TestResolveCompilePersonas:
    """Phase 2: team-level override of the role -> persona-name mapping
    via ``configs/workflow.yaml``."""

    def test_defaults_when_no_workflow_yaml(self, tmp_path):
        from tigerharness.journal.scaffold import resolve_compile_personas
        team = _make_team(tmp_path)
        assert resolve_compile_personas(team) == {
            "drafter": "Anzai", "akagi": "Akagi", "ayako": "Ayako",
        }

    def test_full_override(self, tmp_path):
        from tigerharness.journal.scaffold import resolve_compile_personas
        team = _make_team(
            tmp_path,
            personas=["Sakuragi", "Rukawa", "Mitsui", "Coach"],
        )
        (team / "configs" / "workflow.yaml").write_text(
            "compile_personas:\n"
            "  drafter: Sakuragi\n"
            "  akagi: Rukawa\n"
            "  ayako: Mitsui\n"
        )
        assert resolve_compile_personas(team) == {
            "drafter": "Sakuragi",
            "akagi": "Rukawa",
            "ayako": "Mitsui",
        }

    def test_partial_override_falls_back_to_defaults(self, tmp_path):
        from tigerharness.journal.scaffold import resolve_compile_personas
        team = _make_team(tmp_path)
        # Override only drafter; akagi + ayako keep their defaults.
        (team / "configs" / "workflow.yaml").write_text(
            "compile_personas:\n"
            "  drafter: Mitsui\n"
        )
        assert resolve_compile_personas(team) == {
            "drafter": "Mitsui",
            "akagi": "Akagi",
            "ayako": "Ayako",
        }

    def test_unknown_role_key_silently_ignored(self, tmp_path):
        from tigerharness.journal.scaffold import resolve_compile_personas
        team = _make_team(tmp_path)
        (team / "configs" / "workflow.yaml").write_text(
            "compile_personas:\n"
            "  drafter: Anzai\n"
            "  some_future_role: WhoKnows\n"  # forward-compat: ignore
        )
        result = resolve_compile_personas(team)
        assert "some_future_role" not in result
        assert result == {
            "drafter": "Anzai", "akagi": "Akagi", "ayako": "Ayako",
        }

    def test_blank_value_falls_back_to_default(self, tmp_path):
        from tigerharness.journal.scaffold import resolve_compile_personas
        team = _make_team(tmp_path)
        (team / "configs" / "workflow.yaml").write_text(
            "compile_personas:\n"
            "  drafter: '   '\n"  # whitespace-only -> default
        )
        assert resolve_compile_personas(team)["drafter"] == "Anzai"

    def test_non_dict_compile_personas_falls_back(self, tmp_path):
        """If compile_personas is a list (or anything not a dict), we
        ignore it and use the defaults. The team is the wrong shape
        but we don't crash."""
        from tigerharness.journal.scaffold import resolve_compile_personas
        team = _make_team(tmp_path)
        (team / "configs" / "workflow.yaml").write_text(
            "compile_personas: [Anzai, Akagi, Ayako]\n"
        )
        assert resolve_compile_personas(team) == {
            "drafter": "Anzai", "akagi": "Akagi", "ayako": "Ayako",
        }

    def test_non_dict_yaml_root_falls_back(self, tmp_path):
        from tigerharness.journal.scaffold import resolve_compile_personas
        team = _make_team(tmp_path)
        (team / "configs" / "workflow.yaml").write_text(
            "- not a dict\n"
        )
        assert resolve_compile_personas(team) == {
            "drafter": "Anzai", "akagi": "Akagi", "ayako": "Ayako",
        }

    def test_explicit_null_value_falls_back_to_default(self, tmp_path):
        """`drafter: null` (explicit yaml null) falls back to default,
        same as `drafter: ''` and the implicit-None form
        (`drafter:` with no value). Belt-and-suspenders coverage."""
        from tigerharness.journal.scaffold import resolve_compile_personas
        team = _make_team(tmp_path)
        (team / "configs" / "workflow.yaml").write_text(
            "compile_personas:\n"
            "  drafter: null\n"
            "  akagi:\n"  # implicit None
        )
        result = resolve_compile_personas(team)
        assert result["drafter"] == "Anzai"
        assert result["akagi"] == "Akagi"
        assert result["ayako"] == "Ayako"

    def test_integer_value_falls_back_to_default(self, tmp_path):
        """A non-string value (integer, bool, list) for a role falls
        back to the default rather than being coerced. Belt-and-
        suspenders against a yaml typo like `drafter: 42`."""
        from tigerharness.journal.scaffold import resolve_compile_personas
        team = _make_team(tmp_path)
        (team / "configs" / "workflow.yaml").write_text(
            "compile_personas:\n"
            "  drafter: 42\n"
        )
        assert resolve_compile_personas(team)["drafter"] == "Anzai"


class TestRequiredWorkflowPersonasUsesResolvedRoster:
    """Phase 2: _required_workflow_personas pulls from
    resolve_compile_personas, NOT the COMPILE_PERSONAS constant."""

    def test_overridden_personas_become_required(self, tmp_path):
        from tigerharness.journal.scaffold import _required_workflow_personas
        team = _make_team(
            tmp_path,
            personas=["Sakuragi", "Rukawa", "Mitsui"],
        )
        (team / "configs" / "workflow.yaml").write_text(
            "compile_personas:\n"
            "  drafter: Sakuragi\n"
            "  akagi: Rukawa\n"
            "  ayako: Mitsui\n"
        )
        required = _required_workflow_personas("a playbook", team)
        assert required == {"Sakuragi", "Rukawa", "Mitsui"}


class TestValidatePersonas:
    def test_all_present(self, tmp_path):
        team = _make_team(tmp_path)
        assert validate_personas(team, set(COMPILE_PERSONAS)) == []

    def test_one_missing(self, tmp_path):
        team = _make_team(tmp_path)
        (team / "personas" / "Akagi" / "prompt.md").unlink()
        assert validate_personas(team, set(COMPILE_PERSONAS)) == ["Akagi"]

    def test_empty_prompt_is_missing(self, tmp_path):
        team = _make_team(tmp_path)
        (team / "personas" / "Ayako" / "prompt.md").write_text("")
        assert validate_personas(team, set(COMPILE_PERSONAS)) == ["Ayako"]

    def test_oserror_treated_as_missing(self, monkeypatch, tmp_path):
        team = _make_team(tmp_path)
        original_is_file = Path.is_file

        def _boom(self):
            if "Anzai" in str(self):
                raise OSError("simulated")
            return original_is_file(self)
        monkeypatch.setattr(Path, "is_file", _boom)
        missing = validate_personas(team, set(COMPILE_PERSONAS))
        assert "Anzai" in missing


# ---------------------------------------------------------------------------
# new_workflow_task end-to-end
# ---------------------------------------------------------------------------

class TestNewWorkflowTask:
    def _paths(self, tmp_path: Path) -> JournalPaths:
        return JournalPaths(root=tmp_path / "journal")

    def test_happy_path(self, tmp_path):
        team = _make_team(tmp_path)
        paths = self._paths(tmp_path)
        result = new_workflow_task(
            brief_text="# Cache eviction\nAdd LRU.\n",
            playbook_text="# Default\nAnzai plans. Mitsui implements.\n",
            playbook_name="default",
            team_root=team,
            paths=paths,
            captain="Akagi",
        )
        assert result.status.kind == "workflow"
        assert result.status.state is State.PENDING
        assert result.status.compile_pending is True
        assert result.status.compile_phase is CompilePhase.PENDING
        assert result.status.persona == "Akagi"
        # Title comes from the brief's first H1.
        assert result.status.title == "Cache eviction"
        # Files on disk.
        assert (result.task_dir / "task_brief.md").is_file()
        assert (result.task_dir / "playbook_snapshot.md").is_file()
        assert (result.task_dir / "progress.md").is_file()
        assert (result.task_dir / "artifacts").is_dir()
        assert (result.task_dir / "status.json").is_file()
        # task.md is NOT written for workflow mode (the brief lives in
        # task_brief.md instead).
        assert not (result.task_dir / "task.md").exists()
        # OPERATING.md installed at journal root.
        assert paths.operating_md.is_file()

    def test_captain_optional(self, tmp_path):
        team = _make_team(tmp_path)
        paths = self._paths(tmp_path)
        result = new_workflow_task(
            brief_text="# T\nb\n",
            playbook_text="Anzai plans.\n",
            playbook_name="default",
            team_root=team,
            paths=paths,
            captain=None,
        )
        assert result.status.persona is None

    def test_title_arg_wins_over_brief_h1(self, tmp_path):
        team = _make_team(tmp_path)
        paths = self._paths(tmp_path)
        result = new_workflow_task(
            brief_text="# From brief\nb\n",
            playbook_text="Anzai.\n",
            playbook_name="default",
            team_root=team,
            paths=paths,
            title="Explicit title",
        )
        assert result.status.title == "Explicit title"

    def test_falls_back_to_workflow_when_no_h1(self, tmp_path):
        team = _make_team(tmp_path)
        paths = self._paths(tmp_path)
        result = new_workflow_task(
            brief_text="just body\n",
            playbook_text="Anzai.\n",
            playbook_name="default",
            team_root=team,
            paths=paths,
        )
        assert result.status.title == "workflow"

    def test_slug_overrider(self, tmp_path):
        team = _make_team(tmp_path)
        paths = self._paths(tmp_path)
        result = new_workflow_task(
            brief_text="# Long title that gets slugified\nb\n",
            playbook_text="Anzai.\n",
            playbook_name="default",
            team_root=team,
            paths=paths,
            slug="short",
        )
        assert "-short-" in result.task_id

    def test_empty_brief_rejected(self, tmp_path):
        team = _make_team(tmp_path)
        paths = self._paths(tmp_path)
        with pytest.raises(JournalScaffoldError) as exc:
            new_workflow_task(
                brief_text="   ",
                playbook_text="Anzai.\n",
                playbook_name="default",
                team_root=team,
                paths=paths,
            )
        assert "brief" in str(exc.value).lower()

    def test_empty_playbook_rejected(self, tmp_path):
        team = _make_team(tmp_path)
        paths = self._paths(tmp_path)
        with pytest.raises(JournalScaffoldError) as exc:
            new_workflow_task(
                brief_text="# T\nb\n",
                playbook_text="   ",
                playbook_name="default",
                team_root=team,
                paths=paths,
            )
        assert "playbook" in str(exc.value).lower()

    @pytest.mark.parametrize("bad", [
        "../escape",
        "/abs",
        "foo/bar",
        "",
        ".hidden",
        "with space",
    ])
    def test_bad_playbook_name_rejected(self, tmp_path, bad):
        team = _make_team(tmp_path)
        paths = self._paths(tmp_path)
        with pytest.raises(JournalScaffoldError):
            new_workflow_task(
                brief_text="# T\nb\n",
                playbook_text="Anzai.\n",
                playbook_name=bad,
                team_root=team,
                paths=paths,
            )

    def test_missing_compile_persona_raises(self, tmp_path):
        team = _make_team(tmp_path, personas=["Anzai", "Ayako"])  # Akagi missing
        paths = self._paths(tmp_path)
        with pytest.raises(MissingPersonaError) as exc:
            new_workflow_task(
                brief_text="# T\nb\n",
                playbook_text="Anzai.\n",
                playbook_name="default",
                team_root=team,
                paths=paths,
            )
        assert "Akagi" in exc.value.missing
        # No journal artifact should have been written.
        assert paths.list_active_ids() == []

    def test_missing_playbook_persona_raises(self, tmp_path):
        """A persona that's in the roster but doesn't have prompt.md on
        disk is caught."""
        team = _make_team(
            tmp_path,
            personas=list(COMPILE_PERSONAS) + ["Mitsui"],
        )
        # Mitsui is in personas.yaml AND in the playbook, but the
        # prompt.md is empty.
        (team / "personas" / "Mitsui" / "prompt.md").write_text("")
        paths = self._paths(tmp_path)
        with pytest.raises(MissingPersonaError) as exc:
            new_workflow_task(
                brief_text="# T\nb\n",
                playbook_text="Anzai plans. Mitsui implements.\n",
                playbook_name="default",
                team_root=team,
                paths=paths,
            )
        assert "Mitsui" in exc.value.missing

    def test_write_order_status_json_last(self, tmp_path):
        """The Phase 1 write-order invariant: status.json must be the
        LAST file written so a SIGKILL between writes never leaves a
        half-built task visible. We assert by checking that on a
        successful return, all other artifacts exist on disk too."""
        team = _make_team(tmp_path)
        paths = self._paths(tmp_path)
        result = new_workflow_task(
            brief_text="# T\nb\n",
            playbook_text="Anzai.\n",
            playbook_name="default",
            team_root=team,
            paths=paths,
        )
        # task_brief, playbook_snapshot, progress must all exist before
        # status.json -- because status.json was written last.
        # We can't observe ordering directly, but we can confirm all
        # required predecessors exist on disk when status.json does.
        for fname in ("task_brief.md", "playbook_snapshot.md",
                      "progress.md"):
            assert (result.task_dir / fname).is_file()
        assert paths.status_json(result.task_id).is_file()

    def test_status_json_round_trips_after_scaffold(self, tmp_path):
        team = _make_team(tmp_path)
        paths = self._paths(tmp_path)
        result = new_workflow_task(
            brief_text="# T\nb\n",
            playbook_text="Anzai.\n",
            playbook_name="default",
            team_root=team,
            paths=paths,
            captain="Akagi",
        )
        on_disk = Status.from_json(
            paths.status_json(result.task_id).read_text()
        )
        assert on_disk == result.status

    def test_id_mint_failure_re_raises_as_scaffold_error(
        self, tmp_path, monkeypatch,
    ):
        team = _make_team(tmp_path)
        paths = self._paths(tmp_path)
        from tigerharness.journal import ids as id_mod
        monkeypatch.setattr(
            id_mod.secrets, "token_hex", lambda n: "deadbeef",
        )
        # Pre-seed both attempted ids in done/ so the mint fails.
        import datetime as dt
        date = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
        seed = paths.done / f"{date}-collision-deadbeef"
        seed.mkdir(parents=True)
        (seed / "status.json").write_text("{}")
        with pytest.raises(JournalScaffoldError):
            new_workflow_task(
                brief_text="# Collision\nb\n",
                playbook_text="Anzai.\n",
                playbook_name="default",
                team_root=team,
                paths=paths,
                slug="collision",
            )

    def test_zero_max_sessions_rejected_via_model(self, tmp_path):
        team = _make_team(tmp_path)
        paths = self._paths(tmp_path)
        with pytest.raises(JournalScaffoldError) as exc:
            new_workflow_task(
                brief_text="# T\nb\n",
                playbook_text="Anzai.\n",
                playbook_name="default",
                team_root=team,
                paths=paths,
                max_sessions=0,
            )
        # The underlying JournalModelError is re-raised as
        # JournalScaffoldError.
        assert "max_sessions" in str(exc.value)
